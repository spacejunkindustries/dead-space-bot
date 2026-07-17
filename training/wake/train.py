#!/usr/bin/env python3
"""Train and validate the AURA wake-word model.

OFFLINE pipeline — runs on a GPU box or Colab, never on the droplet, and is
never imported by brain/. Run generate_samples.py first; see
training/wake/README.md for the full procedure.

Training is delegated to the official openWakeWord entry point
(train.py --training_config <cfg> --train_model, per
notebooks/automatic_model_training.ipynb), which produces
<output_dir>/<model_name>.onnx.

Validation is this script's own contribution: it scores the trained model
against the held-out synthetic splits (positive_test and the adversarial
negative_test — "aura", "commander", "concord", ... per GDD §5.2) and against
the ~11 h real-speech validation feature stream, then prints false-reject and
false-accept rates across a threshold sweep so the operator can pick
wake.threshold for aura.yaml from data.

--bundle assembles the full ONNX chain (melspectrogram -> embedding ->
wakeword) that deploys to /opt/aura/models/wake/ on the droplet.
"""

from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path
from typing import Any

from generate_samples import load_config, oww_train_entry, render_oww_config, run_cmd

log = logging.getLogger("aura.wake.train")


# ------------------------------------------------------------------------- training


def trained_model_path(cfg: dict[str, Any]) -> Path:
    return Path(cfg["paths"]["output_dir"]) / f"{cfg['phrase']['model_name']}.onnx"


def stage_train(cfg: dict[str, Any], tflite: bool) -> None:
    """Run the upstream --train_model step (auto-training against the config targets)."""
    import sys

    training_cfg = render_oww_config(cfg)
    cmd = [
        sys.executable,
        str(oww_train_entry(cfg)),
        "--training_config",
        str(training_cfg),
        "--train_model",
    ]
    if tflite:
        cmd.append("--convert_to_tflite")
    run_cmd(cmd)
    model = trained_model_path(cfg)
    if not model.exists():
        raise FileNotFoundError(f"training finished but {model} was not produced")
    log.info("trained model: %s", model)


# ----------------------------------------------------------------------- validation


def score_clips(model_path: Path, wav_dir: Path, max_clips: int) -> list[float]:
    """Max wake score per held-out clip, using the deployed inference stack."""
    from openwakeword.model import Model

    wavs = sorted(wav_dir.glob("*.wav"))
    if max_clips > 0:
        wavs = wavs[:max_clips]
    if not wavs:
        raise FileNotFoundError(f"no held-out clips in {wav_dir} — run generate_samples.py first")
    oww = Model(wakeword_models=[str(model_path)], inference_framework="onnx")
    scores: list[float] = []
    for i, wav in enumerate(wavs, start=1):
        oww.reset()
        frames = oww.predict_clip(str(wav))
        scores.append(max(max(frame.values()) for frame in frames))
        if i % 250 == 0:
            log.info("  scored %d/%d clips in %s", i, len(wavs), wav_dir.name)
    return scores


def false_accept_stream_scores(model_path: Path, features_npy: Path) -> Any:
    """Score the classifier head over the pre-computed real-speech feature stream."""
    import numpy as np
    import onnxruntime as ort

    feats = np.load(features_npy).astype(np.float32)
    sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    input_name = sess.get_inputs()[0].name
    scores = np.empty(len(feats), dtype=np.float32)
    batch = 4096
    for start in range(0, len(feats), batch):
        out = sess.run(None, {input_name: feats[start : start + batch]})[0]
        scores[start : start + len(out)] = np.asarray(out).reshape(-1)
    return scores


def sweep(cfg: dict[str, Any], model_path: Path) -> None:
    """Print FRR / adversarial-FA / real-speech FA-per-hour across thresholds."""
    import numpy as np

    val = cfg["validation"]
    clip_root = Path(cfg["paths"]["output_dir"]) / cfg["phrase"]["model_name"]
    max_clips = int(val["max_clips"])

    log.info("scoring held-out positive clips (synthetic 'aura command')")
    pos = np.asarray(score_clips(model_path, clip_root / "positive_test", max_clips))
    log.info("scoring held-out adversarial negatives (GDD §5.2 collision phrases)")
    neg = np.asarray(score_clips(model_path, clip_root / "negative_test", max_clips))

    features_npy = Path(cfg["paths"]["feature_dir"]) / cfg["assets"]["feature_files"]["validation"]
    stream = None
    hours = 0.0
    if features_npy.exists():
        log.info("scoring real-speech validation stream (%s)", features_npy.name)
        stream = false_accept_stream_scores(model_path, features_npy)
        hours = len(stream) * float(val["seconds_per_frame"]) / 3600.0
    else:
        log.warning("%s missing — FA/hour column will be blank", features_npy)

    thresholds = np.arange(
        float(val["threshold_start"]),
        float(val["threshold_stop"]),
        float(val["threshold_step"]),
    )
    fa_budget = float(val["max_false_accepts_per_hour"])
    lines = [
        f"model: {model_path}",
        f"positives: {len(pos)} clips   adversarial negatives: {len(neg)} clips   "
        f"real speech: {hours:.1f} h",
        "",
        f"{'threshold':>9}  {'false-reject':>12}  {'adversarial-FA':>14}  {'FA/hour':>8}",
    ]
    recommended: float | None = None
    for t in thresholds:
        frr = float((pos < t).mean()) * 100.0
        afa = float((neg >= t).mean()) * 100.0
        if stream is not None and hours > 0:
            hits = (stream >= t).astype(np.int8)
            n_fa = int(((hits[1:] == 1) & (hits[:-1] == 0)).sum()) + int(hits[0])
            fa_per_hour = n_fa / hours
            fa_col = f"{fa_per_hour:8.2f}"
            if recommended is None and fa_per_hour <= fa_budget:
                recommended = float(t)
        else:
            fa_col = f"{'-':>8}"
        lines.append(f"{t:9.2f}  {frr:11.1f}%  {afa:13.1f}%  {fa_col}")
    lines.append("")
    if recommended is not None:
        lines.append(
            f"lowest threshold meeting <= {fa_budget:.2f} false accepts/hour on real speech: "
            f"{recommended:.2f}"
        )
        lines.append(
            "starting point for wake.threshold in aura.yaml — confirm against real fleet "
            "comms before trusting it (CLAUDE.md: threshold tuning needs a human)."
        )
    report = "\n".join(lines)
    print()
    print(report)
    print()
    sweep_file = model_path.with_suffix(".thresholds.txt")
    sweep_file.write_text(report + "\n")
    log.info("sweep table also written to %s", sweep_file)


# --------------------------------------------------------------------------- bundle


def stage_bundle(cfg: dict[str, Any]) -> None:
    """Assemble the ONNX chain exactly as it deploys to /opt/aura/models/wake/."""
    import openwakeword

    bundle_dir = Path(cfg["deploy"]["bundle_dir"])
    bundle_dir.mkdir(parents=True, exist_ok=True)
    model = trained_model_path(cfg)
    resources = Path(openwakeword.__file__).parent / "resources" / "models"
    for src in (model, resources / "melspectrogram.onnx", resources / "embedding_model.onnx"):
        if not src.exists():
            raise FileNotFoundError(
                f"{src} missing — run training first (and generate_samples.py "
                "--stages base-models for the melspectrogram/embedding models)"
            )
        shutil.copy2(src, bundle_dir / src.name)
    log.info("deploy bundle ready: %s", bundle_dir)
    log.info(
        "deploy: scp %s/*.onnx aura@droplet:/opt/aura/models/wake/  then set "
        "wake.model: /opt/aura/models/wake/%s and wake.threshold from the sweep table "
        "in /etc/aura/aura.yaml",
        bundle_dir,
        model.name,
    )


# ----------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Train the AURA wake-word model via openWakeWord and print a threshold "
            "sweep for picking wake.threshold (offline; GPU box or Colab)."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "config.yaml",
        help="pipeline config (default: config.yaml next to this script)",
    )
    parser.add_argument(
        "--skip-train",
        action="store_true",
        help="skip training; only run the threshold sweep on an existing model",
    )
    parser.add_argument(
        "--skip-validate",
        action="store_true",
        help="skip the threshold sweep",
    )
    parser.add_argument(
        "--tflite",
        action="store_true",
        help="also export a .tflite model (needs the optional tensorflow deps; "
        "AURA itself deploys ONNX only)",
    )
    parser.add_argument(
        "--bundle",
        action="store_true",
        help="assemble the deployable ONNX chain in deploy.bundle_dir",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = load_config(args.config)

    if not args.skip_train:
        stage_train(cfg, tflite=args.tflite)
    model = trained_model_path(cfg)
    if not model.exists():
        log.error("no trained model at %s — run without --skip-train first", model)
        return 1
    if not args.skip_validate:
        sweep(cfg, model)
    if args.bundle:
        stage_bundle(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
