#!/usr/bin/env python3
"""Synthetic sample generation for the CORTANA wake phrase.

OFFLINE pipeline — runs on a GPU box or Colab, never on the droplet, and is
never imported by brain/. Install deps from training/wake/requirements-train.txt
into a dedicated venv; see training/wake/README.md for the full procedure.

This script mirrors the official openWakeWord training flow
(https://github.com/dscripka/openWakeWord, notebooks/automatic_model_training.ipynb):

  1. Clone piper-sample-generator (rhasspy) and openWakeWord; fetch the
     LibriTTS-R multi-speaker TTS checkpoint (~904 voices, randomised speed
     and noise per sample).
  2. Download the pre-computed negative features (~2000 h of speech/noise/
     music), the ~11 h real-speech validation features, the MIT environmental
     room impulse responses, and background noise/music (AudioSet + FMA).
  3. Render an openWakeWord training config from config.yaml, then drive the
     upstream entry point:
       train.py --training_config <cfg> --generate_clips   (positives AND
         adversarial negatives — auto-generated phonetically-close texts plus
         the custom_negative_phrases list, per GDD §5.2)
       train.py --training_config <cfg> --augment_clips    (RIR + background
         noise mixing, then feature computation)

Clip generation and augmentation are deliberately delegated to the upstream
entry point rather than re-implemented, so this pipeline tracks openWakeWord
instead of forking it. training/wake/train.py picks up from the features this
script produces.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("aura.wake.generate")

STAGES: tuple[str, ...] = (
    "repos",
    "tts-model",
    "base-models",
    "features",
    "rirs",
    "audioset",
    "fma",
    "render",
    "generate",
    "augment",
)


# --------------------------------------------------------------------------- config


def load_config(path: Path) -> dict[str, Any]:
    """Load config.yaml and resolve every path relative to the config file."""
    with path.open() as fh:
        cfg: dict[str, Any] = yaml.safe_load(fh)
    base = path.resolve().parent
    paths = cfg["paths"]
    for key, value in paths.items():
        if isinstance(value, list):
            paths[key] = [str((base / v).resolve()) for v in value]
        else:
            paths[key] = str((base / value).resolve())
    deploy = cfg.setdefault("deploy", {})
    if "bundle_dir" in deploy:
        deploy["bundle_dir"] = str((base / deploy["bundle_dir"]).resolve())
    for section in (
        "phrase",
        "samples",
        "assets",
        "augmentation",
        "training",
        "validation",
    ):
        if section not in cfg:
            raise KeyError(f"config.yaml is missing the '{section}' section")
    return cfg


def render_oww_config(cfg: dict[str, Any]) -> Path:
    """Write the openWakeWord training config derived from config.yaml.

    Key names follow openWakeWord's examples/custom_model.yml. If upstream
    adds keys, diff against the clone's example and extend here.
    """
    phrase = cfg["phrase"]
    samples = cfg["samples"]
    paths = cfg["paths"]
    training = cfg["training"]
    assets = cfg["assets"]
    feature_dir = Path(paths["feature_dir"])

    oww_cfg: dict[str, Any] = {
        "model_name": phrase["model_name"],
        "target_phrase": list(phrase["target_phrases"]),
        "custom_negative_phrases": list(phrase["custom_negative_phrases"]),
        "n_samples": int(samples["n_samples"]),
        "n_samples_val": int(samples["n_samples_val"]),
        "tts_batch_size": int(samples["tts_batch_size"]),
        "augmentation_batch_size": int(samples["augmentation_batch_size"]),
        "augmentation_rounds": int(samples["augmentation_rounds"]),
        "piper_sample_generator_path": paths["piper_sample_generator"],
        "tts_model_path": paths["tts_model"],
        "output_dir": paths["output_dir"],
        "rir_paths": [paths["rir_dir"]],
        "background_paths": list(paths["background_dirs"]),
        "background_paths_duplication_rate": list(
            cfg["augmentation"]["background_duplication_rate"]
        ),
        "false_positive_validation_data_path": str(
            feature_dir / assets["feature_files"]["validation"]
        ),
        "feature_data_files": {
            "ACAV100M_sample": str(feature_dir / assets["feature_files"]["negative"]),
        },
        "model_type": training["model_type"],
        "layer_size": int(training["layer_size"]),
        "steps": int(training["steps"]),
        "target_accuracy": float(training["target_accuracy"]),
        "target_recall": float(training["target_recall"]),
        "target_false_positives_per_hour": float(
            training["target_false_positives_per_hour"]
        ),
        "max_negative_weight": int(training["max_negative_weight"]),
        "batch_n_per_class": dict(training["batch_n_per_class"]),
    }

    out = Path(paths["work_dir"]) / "oww_training_config.yml"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        yaml.safe_dump(oww_cfg, fh, sort_keys=False)
    log.info("rendered openWakeWord training config -> %s", out)
    return out


def oww_train_entry(cfg: dict[str, Any]) -> Path:
    """Path to the upstream training entry point inside the cloned repo."""
    entry = Path(cfg["paths"]["openwakeword_repo"]) / "openwakeword" / "train.py"
    if not entry.exists():
        raise FileNotFoundError(
            f"{entry} not found — run the 'repos' stage first (or fix paths.openwakeword_repo)"
        )
    return entry


# --------------------------------------------------------------------------- helpers


def run_cmd(cmd: list[str], cwd: Path | None = None) -> None:
    log.info("exec: %s", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def download_url(url: str, dest: Path) -> None:
    import urllib.request

    if dest.exists():
        log.info("already present, skipping download: %s", dest)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.info("downloading %s -> %s", url, dest)
    urllib.request.urlretrieve(url, tmp)  # noqa: S310 - fixed https URLs from config
    tmp.rename(dest)


def write_wav_16k(dest: Path, audio: Any) -> None:
    """Write a float or int16 mono array as a 16 kHz PCM WAV."""
    import numpy as np
    from scipy.io import wavfile

    arr = np.asarray(audio)
    if arr.dtype != np.int16:
        arr = np.clip(arr, -1.0, 1.0)
        arr = (arr * 32767.0).astype(np.int16)
    wavfile.write(str(dest), 16000, arr)


def dir_has_files(path: Path, pattern: str = "*") -> bool:
    return path.is_dir() and any(path.glob(pattern))


def background_dir(cfg: dict[str, Any], index: int) -> str:
    dirs = cfg["paths"]["background_dirs"]
    if len(dirs) <= index:
        raise ValueError(
            f"paths.background_dirs needs at least {index + 1} entries "
            "(audioset first, fma second) — see config.yaml"
        )
    return dirs[index]


# --------------------------------------------------------------------------- stages


def stage_repos(cfg: dict[str, Any]) -> None:
    """Clone piper-sample-generator and openWakeWord."""
    repos = {
        cfg["paths"][
            "piper_sample_generator"
        ]: "https://github.com/rhasspy/piper-sample-generator",
        cfg["paths"]["openwakeword_repo"]: "https://github.com/dscripka/openWakeWord",
    }
    for dest, url in repos.items():
        dest_path = Path(dest)
        if (dest_path / ".git").exists():
            log.info("already cloned: %s", dest_path)
            continue
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        run_cmd(["git", "clone", "--depth", "1", url, str(dest_path)])


def stage_tts_model(cfg: dict[str, Any]) -> None:
    """Fetch the LibriTTS-R multi-speaker checkpoint used for sample synthesis."""
    download_url(cfg["assets"]["tts_model_url"], Path(cfg["paths"]["tts_model"]))


def stage_base_models(cfg: dict[str, Any]) -> None:  # noqa: ARG001 - uniform stage signature
    """Download openWakeWord's melspectrogram + embedding base models."""
    import openwakeword.utils

    openwakeword.utils.download_models()
    log.info("openWakeWord base models present (melspectrogram, embedding)")


def stage_features(cfg: dict[str, Any]) -> None:
    """Fetch pre-computed negative and validation feature files from HuggingFace."""
    from huggingface_hub import hf_hub_download

    feature_dir = Path(cfg["paths"]["feature_dir"])
    feature_dir.mkdir(parents=True, exist_ok=True)
    repo = cfg["assets"]["features_repo"]
    for fname in cfg["assets"]["feature_files"].values():
        if (feature_dir / fname).exists():
            log.info("already present: %s", feature_dir / fname)
            continue
        log.info("downloading %s from %s (large; be patient)", fname, repo)
        hf_hub_download(
            repo_id=repo,
            filename=fname,
            repo_type="dataset",
            local_dir=str(feature_dir),
        )


def stage_rirs(cfg: dict[str, Any]) -> None:
    """Fetch the MIT environmental impulse responses as 16 kHz WAVs."""
    import datasets

    rir_dir = Path(cfg["paths"]["rir_dir"])
    if dir_has_files(rir_dir, "*.wav"):
        log.info("RIRs already present in %s", rir_dir)
        return
    rir_dir.mkdir(parents=True, exist_ok=True)
    ds = datasets.load_dataset(cfg["assets"]["rir_repo"], split="train", streaming=True)
    ds = ds.cast_column("audio", datasets.Audio(sampling_rate=16000))
    count = 0
    for row in ds:
        name = Path(row["audio"]["path"]).name
        write_wav_16k(rir_dir / name, row["audio"]["array"])
        count += 1
        if count % 50 == 0:
            log.info("RIRs written: %d", count)
    log.info("RIR stage done: %d files in %s", count, rir_dir)


def stage_audioset(cfg: dict[str, Any]) -> None:
    """Fetch one AudioSet shard and convert clips to 16 kHz WAV background noise.

    The upstream mirror (``agkphysics/AudioSet``) restructured in 2026 from
    tar-of-flac shards (``data/bal_train09.tar``) to parquet shards
    (``data/bal_train/09.parquet``) — the old paths now 404. Both layouts are
    supported: ``assets.audioset_file`` names the shard, and the extension
    picks the decoder. ``assets.audioset_tar`` is honoured as a legacy alias.
    """
    import io
    import math

    import soundfile as sf
    from huggingface_hub import hf_hub_download
    from scipy.signal import resample_poly

    out_dir = Path(background_dir(cfg, 0))
    if dir_has_files(out_dir, "*.wav"):
        log.info("AudioSet clips already present in %s", out_dir)
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(cfg["paths"]["work_dir"])
    assets = cfg["assets"]
    shard_name = assets.get("audioset_file") or assets.get("audioset_tar")
    if not shard_name:
        raise RuntimeError("config assets: set audioset_file (or legacy audioset_tar)")
    shard_path = Path(
        hf_hub_download(
            repo_id=assets["audioset_repo"],
            filename=shard_name,
            repo_type="dataset",
            local_dir=str(work_dir / "audioset_raw"),
        )
    )
    max_clips = int(assets["max_audioset_clips"])

    def _write_clip(audio: Any, sr: int, name: str) -> None:
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sr != 16000:
            g = math.gcd(16000, sr)
            audio = resample_poly(audio, 16000 // g, sr // g)
        write_wav_16k(out_dir / name, audio)

    count = 0
    if shard_path.suffix == ".parquet":
        import pyarrow.parquet as pq

        pf = pq.ParquetFile(shard_path)
        if "audio" not in pf.schema_arrow.names:
            raise RuntimeError(
                f"AudioSet parquet shard {shard_name!r} has no 'audio' column "
                f"(columns: {pf.schema_arrow.names}) — upstream layout changed again"
            )
        for batch in pf.iter_batches(batch_size=16, columns=["audio"]):
            for rec in batch.column("audio").to_pylist():
                if count >= max_clips:
                    break
                raw = rec.get("bytes") if isinstance(rec, dict) else None
                if not raw:
                    continue
                audio, sr = sf.read(io.BytesIO(raw))
                stem = Path(str(rec.get("path") or f"audioset_{count:05d}")).stem
                _write_clip(audio, sr, f"{stem}.wav")
                count += 1
                if count % 200 == 0:
                    log.info("AudioSet clips written: %d", count)
            if count >= max_clips:
                break
    else:
        with tarfile.open(shard_path) as tar:
            for member in tar:
                if count >= max_clips:
                    break
                if not member.isfile() or not member.name.endswith(".flac"):
                    continue
                fobj = tar.extractfile(member)
                if fobj is None:
                    continue
                audio, sr = sf.read(fobj)
                _write_clip(audio, sr, Path(member.name).stem + ".wav")
                count += 1
                if count % 200 == 0:
                    log.info("AudioSet clips written: %d", count)
    log.info("AudioSet stage done: %d clips in %s", count, out_dir)


def stage_fma(cfg: dict[str, Any]) -> None:
    """Fetch Free Music Archive clips as 16 kHz WAV background music."""
    import datasets

    out_dir = Path(background_dir(cfg, 1))
    if dir_has_files(out_dir, "*.wav"):
        log.info("FMA clips already present in %s", out_dir)
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    ds = datasets.load_dataset(
        cfg["assets"]["fma_repo"],
        name=cfg["assets"]["fma_subset"],
        split="train",
        streaming=True,
    )
    ds = ds.cast_column("audio", datasets.Audio(sampling_rate=16000))
    max_clips = int(cfg["assets"]["max_fma_clips"])
    count = 0
    for row in ds:
        if count >= max_clips:
            break
        write_wav_16k(out_dir / f"fma_{count:06d}.wav", row["audio"]["array"])
        count += 1
        if count % 200 == 0:
            log.info("FMA clips written: %d", count)
    log.info("FMA stage done: %d clips in %s", count, out_dir)


def stage_render(cfg: dict[str, Any]) -> None:
    render_oww_config(cfg)


def stage_generate(cfg: dict[str, Any]) -> None:
    """Generate positive and adversarial-negative clips via the upstream entry point.

    Writes WAVs under <output_dir>/<model_name>/{positive,negative}_{train,test};
    the *_test splits are held out and later scored by train.py's threshold sweep.
    """
    training_cfg = render_oww_config(cfg)
    run_cmd(
        [
            sys.executable,
            str(oww_train_entry(cfg)),
            "--training_config",
            str(training_cfg),
            "--generate_clips",
        ]
    )


def stage_augment(cfg: dict[str, Any], overwrite: bool = False) -> None:
    """Augment clips (RIR + background noise) and compute training features."""
    training_cfg = render_oww_config(cfg)
    cmd = [
        sys.executable,
        str(oww_train_entry(cfg)),
        "--training_config",
        str(training_cfg),
        "--augment_clips",
    ]
    if overwrite:
        cmd.append("--overwrite")
    run_cmd(cmd)


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "CORTANA wake-word synthetic sample generation (offline; GPU box or Colab). "
            "Runs the asset-fetch and clip-generation stages of the official "
            "openWakeWord training flow."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "config.yaml",
        help="pipeline config (default: config.yaml next to this script)",
    )
    parser.add_argument(
        "--stages",
        default=",".join(STAGES),
        help=f"comma-separated stages to run, in order. Available: {', '.join(STAGES)}",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="recompute augmented features even if present (passes --overwrite upstream)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    requested = [s.strip() for s in args.stages.split(",") if s.strip()]
    unknown = sorted(set(requested) - set(STAGES))
    if unknown:
        parser.error(f"unknown stage(s): {', '.join(unknown)}")

    cfg = load_config(args.config)
    log.info(
        "phrase=%r model=%s positives=%d (+%d validation)",
        cfg["phrase"]["target_phrases"],
        cfg["phrase"]["model_name"],
        cfg["samples"]["n_samples"],
        cfg["samples"]["n_samples_val"],
    )

    for stage in requested:
        log.info("=== stage: %s ===", stage)
        try:
            if stage == "augment":
                stage_augment(cfg, overwrite=args.overwrite)
            else:
                globals()[f"stage_{stage.replace('-', '_')}"](cfg)
        except Exception:
            log.exception(
                "stage %r failed. Dataset mirrors move occasionally — for rirs/audioset/fma "
                "you may instead drop any 16 kHz mono WAV noise collection into the "
                "configured directories and re-run without that stage (--stages).",
                stage,
            )
            return 1
    log.info("done. Next: python train.py --config %s", args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
