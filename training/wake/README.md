# Wake-word training — "Hey Cortana"

Offline pipeline that trains the custom [openWakeWord](https://github.com/dscripka/openWakeWord)
model CORTANA listens for. It is completely separate from `brain/` and `ears/`:
nothing here is imported at runtime, and none of these dependencies belong in
`brain/requirements.txt`. Per `CLAUDE.md`, the scripts are the automatable
part — **the run itself needs a human**, a GPU, and several hours.

## What this produces

The ONNX chain deployed to `/opt/cortana/models/wake/` on the droplet
(GDD §4 assets table — melspec → embedding → wakeword):

| File | Origin | Role |
|---|---|---|
| `melspectrogram.onnx` | openWakeWord release (downloaded, not trained) | audio → mel spectrogram |
| `embedding_model.onnx` | openWakeWord release (downloaded, not trained) | spectrogram → speech embedding |
| `aura_command.onnx` | **trained here** | embedding → wake score, the phrase is baked in |

`cortana.yaml` points at the trained head, and the threshold comes from this
pipeline's sweep output:

```yaml
wake:
  model:  /opt/cortana/models/wake/hey_cortana.onnx
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

## Running it on Kaggle (phone-friendly)

`kaggle_hey_cortana.ipynb` in this directory is a ready-to-import notebook:
kaggle.com → Create → Notebook → File → Import Notebook → upload it, set
**Accelerator: GPU T4 x2** and **Internet: ON**, then run the cells in order.
It puts the entire ~40 GB job on `/kaggle/tmp` (the ~60 GiB ephemeral disk —
`/kaggle/working` is capped at ~20 GB and WILL fill up), and copies only the
three final `.onnx` files back to `/kaggle/working` as downloadable output.
For a run that survives the phone's browser sleeping, use *Save Version →
Save & Run All (Commit)* after the interactive cells check out — it executes
headless for up to ~12 h.

## Running it

> **Python version matters.** The pin set targets **3.10** — `piper-phonemize`
> and several upstream pins publish no wheels for 3.12, and a host on 3.12
> (current Kaggle, recent Colab) aborts the install on the first missing wheel.
> Don't fight the host's interpreter; create a 3.10 environment with
> [`uv`](https://github.com/astral-sh/uv), which fetches a standalone 3.10
> build on any machine. The scripts launch every subprocess with
> `sys.executable`, so running them under the 3.10 venv keeps the whole
> pipeline — including the cloned openWakeWord trainer — on 3.10.

```bash
cd training/wake

# 3.10 environment, host-independent (works on 3.12 Kaggle/Colab too).
# --clear makes the step re-runnable: it replaces any half-built venv from a
# previous attempt instead of stopping on an interactive "replace? [y/n]".
pip install -q uv
uv venv --clear --python 3.10 .venv
uv pip install --python .venv/bin/python -r requirements-train.txt
PY=.venv/bin/python          # use this for every step below

# 1. Fetch assets, generate + augment samples (hours; resumable per stage)
$PY generate_samples.py --config config.yaml

# 2. Train, then print the threshold sweep
$PY train.py --config config.yaml

# 3. Assemble the deployable ONNX chain
$PY train.py --config config.yaml --skip-train --skip-validate --bundle
```

On a box that already runs Python 3.10/3.11 a plain
`python -m venv .venv && source .venv/bin/activate && pip install -r
requirements-train.txt` works too — the `uv` step only exists to sidestep a
3.12 host.

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
     0.55          3.0%            1.2%      0.17   <- e.g. picked for cortana.yaml
     0.70          6.9%            0.3%      0.04
```

Pick the lowest threshold whose FA/hour you can live with (the table flags
the first one under `validation.max_false_accepts_per_hour`), set it as
`wake.threshold` in `/etc/cortana/cortana.yaml`, and treat it as a starting value:
GDD §16 thresholds are tuned from real fleet audio, and the wake phrase's
true false-accept rate is only measurable against real comms
(`CLAUDE.md`, "Things that need a human"). `#bot-health` reports a running
false-accept estimate once deployed — watch it for the first week.

Also sanity-check by ear: play a few `positive_test` clips and confirm the
TTS actually says "hey cortana" the way pilots will.

## Deploying

```bash
scp work/deploy_bundle/*.onnx aura@droplet:/opt/cortana/models/wake/
# edit /etc/cortana/cortana.yaml: wake.threshold from the sweep table
systemctl restart cortana-brain
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
  pins ancient TensorFlow and genuinely needs 3.10. CORTANA deploys ONNX only.
- **Synthetic ≠ real.** Every number in the sweep table is measured on
  synthetic speech plus a generic real-speech stream — not on your pilots,
  your accents, or your comms compression. It picks a starting threshold;
  it does not replace listening to the bot in a real fleet.
