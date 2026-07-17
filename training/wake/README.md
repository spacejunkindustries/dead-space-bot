# Wake-word training — "Aura Command"

Offline pipeline that trains the custom [openWakeWord](https://github.com/dscripka/openWakeWord)
model AURA listens for. It is completely separate from `brain/` and `ears/`:
nothing here is imported at runtime, and none of these dependencies belong in
`brain/requirements.txt`. Per `CLAUDE.md`, the scripts are the automatable
part — **the run itself needs a human**, a GPU, and several hours.

## What this produces

The ONNX chain deployed to `/opt/aura/models/wake/` on the droplet
(GDD §4 assets table — melspec → embedding → wakeword):

| File | Origin | Role |
|---|---|---|
| `melspectrogram.onnx` | openWakeWord release (downloaded, not trained) | audio → mel spectrogram |
| `embedding_model.onnx` | openWakeWord release (downloaded, not trained) | spectrogram → speech embedding |
| `aura_command.onnx` | **trained here** | embedding → wake score, the phrase is baked in |

`aura.yaml` points at the trained head, and the threshold comes from this
pipeline's sweep output:

```yaml
wake:
  model:  /opt/aura/models/wake/aura_command.onnx
  threshold: 0.55        # replace with the value picked from the sweep table
```

Brain's `audio/wake.py` loads all three from the model's directory; the two
base models must sit next to `aura_command.onnx` (`train.py --bundle`
assembles exactly this trio).

## Hardware expectations

- **A GPU box or Google Colab.** TTS synthesis of ~32k clips and the
  embedding/augmentation step are GPU work; training itself is light. A free
  Colab T4 completes the whole run in a few hours. CPU-only works but takes
  a day-plus.
- Roughly 30 GB of disk for the work directory (the pre-computed negative
  features alone are ~15 GB).
- **Never the droplet.** The 2 vCPU droplet is sized to *run* the model, not
  to build it — same reasoning as the CI-built Ears binary.

## Upstream flow this mirrors

The pipeline drives openWakeWord's official training entry point rather than
re-implementing it, following `notebooks/automatic_model_training.ipynb` in
the openWakeWord repo:

1. `piper-sample-generator` (rhasspy) + the `en_US-libritts_r-medium.pt`
   checkpoint synthesise clips across ~904 voices with randomised speaking
   speed and noise.
2. `openwakeword/train.py --generate_clips` produces positive clips *and*
   adversarial negatives — auto-generated phonetically-close texts plus our
   `custom_negative_phrases` list ("aura", "or a command", "commander",
   "concord", "capsuleer", ... — the GDD §5.2 false-fire vocabulary).
3. `--augment_clips` mixes in room impulse responses (MIT survey) and
   background noise/music (AudioSet, Free Music Archive), then computes
   input features.
4. `--train_model` trains the classifier head against ~2000 h of pre-computed
   negative features, auto-tuning toward the configured false-accept target,
   and emits `aura_command.onnx`.

`generate_samples.py` wraps steps 1–3 (plus all asset downloads);
`train.py` wraps step 4 and adds the validation sweep.

## Running it

```bash
# On the GPU box / in Colab — Python 3.10 venv recommended (upstream's target)
cd training/wake
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-train.txt

# 1. Fetch assets, generate + augment samples (hours; resumable per stage)
python generate_samples.py --config config.yaml

# 2. Train, then print the threshold sweep
python train.py --config config.yaml

# 3. Assemble the deployable ONNX chain
python train.py --config config.yaml --skip-train --skip-validate --bundle
```

Every knob — phrase, sample counts, adversarial phrases, noise sources,
training targets, sweep range — lives in `config.yaml`. `generate_samples.py
--stages` reruns any subset (e.g. `--stages generate,augment` after editing
the phrase list). Both scripts are idempotent about downloads.

## Validating before deploying

`train.py` scores the trained model three ways and prints one table:

- **false-reject** on held-out synthetic positives (`positive_test/`);
- **adversarial-FA** on held-out collision phrases (`negative_test/`);
- **FA/hour** on ~11 h of real recorded speech (the openWakeWord validation
  feature stream) — the column that actually predicts night-long comms.

```
threshold  false-reject  adversarial-FA   FA/hour
     0.40          1.8%            4.1%      0.61
     0.55          3.0%            1.2%      0.17   <- e.g. picked for aura.yaml
     0.70          6.9%            0.3%      0.04
```

Pick the lowest threshold whose FA/hour you can live with (the table flags
the first one under `validation.max_false_accepts_per_hour`), set it as
`wake.threshold` in `/etc/aura/aura.yaml`, and treat it as a starting value:
GDD §16 thresholds are tuned from real fleet audio, and the wake phrase's
true false-accept rate is only measurable against real comms
(`CLAUDE.md`, "Things that need a human"). `#bot-health` reports a running
false-accept estimate once deployed — watch it for the first week.

Also sanity-check by ear: play a few `positive_test` clips and confirm the
TTS actually says "aura command" the way pilots will.

## Deploying

```bash
scp work/deploy_bundle/*.onnx aura@droplet:/opt/aura/models/wake/
# edit /etc/aura/aura.yaml: wake.threshold from the sweep table
systemctl restart aura-brain
```

## Alternate phrases

One model detects one phrase. For the GDD §5.2 supported alternative
("hey overseer"): copy `config.yaml`, change `phrase.target_phrases`,
`phrase.model_name` (e.g. `hey_overseer`), rebuild the adversarial list for
the new phonetics, and rerun `--stages generate,augment` plus `train.py`.
The asset downloads are shared and won't repeat. Point `wake.model` at
whichever `.onnx` the corp settles on.

## Caveats

- **Upstream drift.** The pipeline delegates clip generation, augmentation,
  and training to a fresh clone of openWakeWord, so it tracks upstream — but
  if upstream renames config keys, update `render_oww_config()` in
  `generate_samples.py` against the clone's `examples/custom_model.yml`.
- **Dataset mirrors move.** The RIR/AudioSet/FMA stages pull from HuggingFace
  mirrors named in `config.yaml`. If one disappears, drop any 16 kHz mono WAV
  noise/music collection into the configured directories and skip that stage.
- **Python version.** Upstream's notebook targets 3.10. The ONNX-only path
  generally works on newer interpreters, but the optional `--tflite` export
  pins ancient TensorFlow and genuinely needs 3.10. AURA deploys ONNX only.
- **Synthetic ≠ real.** Every number in the sweep table is measured on
  synthetic speech plus a generic real-speech stream — not on your pilots,
  your accents, or your comms compression. It picks a starting threshold;
  it does not replace listening to the bot in a real fleet.
