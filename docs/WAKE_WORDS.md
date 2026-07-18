# Wake-word options for CORTANA

Verified against live sources 2026-07-18. CORTANA's wake detector is
openWakeWord loading `.onnx` models — any model listed here is a drop-in:

```yaml
# /etc/cortana/cortana.yaml
wake:
  model: /opt/cortana/models/wake/<file>.onnx
  extra_models: []       # optional extra phrases scored in parallel (see below)
  threshold: 0.55        # starting point; sweep 0.45-0.7 against real comms
```

then `systemctl reload cortana-brain` (or `/reload`) — wake model changes are
sighup-class: the per-user model pool rebuilds live, no restart, and the
reload receipt confirms it.

**Running more than one phrase at once.** `wake.extra_models` lists
additional ONNX chains scored in parallel with `wake.model` — any listed
phrase wakes CORTANA. This is the transition tool: keep `hey_jarvis` live
while the corp gets used to a freshly trained `hey_cortana`, then drop it.
`wake.threshold` applies to the **max** score across all models (per-model
thresholds are not supported). A broken or missing extra is logged once and
skipped — only a broken primary disables wake (GDD §5.1).

**The headline fact: no "hey cortana" (or "cortana") model exists anywhere
public** — not in the official set, not in the 100-model community collection,
not in openwakeword.com's 1,239-phrase library, not on HuggingFace. The
custom training run (`training/wake/`) is the only path to the actual phrase;
everything below is a fallback with a *different* phrase.

Phrase rules (GDD §5.2) apply to every option: **≥6 phonemes** and **nobody
says it naturally on fleet comms**. That second rule is why some famous wake
words are marked risky — EVE comms are full of "hull", "warp", real names,
and weekday callouts.

---

## Tier 1 — already on the droplet (zero download, switch today)

The installer fetches openWakeWord's official models on every run into
`/opt/cortana/models/wake/`. Benchmarked by the framework author.
License: CC BY-NC-SA 4.0 (fine for a corp hobby bot).

| Phrase | File | Verdict |
|---|---|---|
| "Hey Jarvis" | `hey_jarvis_v0.1.onnx` | **Best fallback.** ~8 phonemes, zero EVE-vocab collision. The current interim wake word. |
| "Hey Mycroft" | `hey_mycroft_v0.1.onnx` | ~9 phonemes, very low collision. Solid. |
| "Hey Rhasspy" | `hey_rhasspy_v0.1.onnx` | OK phonetics, awkward to say ("razz-pee"). |
| "Alexa" | `alexa_v0.1.onnx` | Weak: ~5 phonemes, fires on real Echo conversations. |

(`timer_v0.1` / `weather_v0.1` in the same folder are intent models, **not**
wake words — don't point `wake.model` at them.)

## Tier 2 — verified drop-in community models (one download)

| Phrase | Source / download | Notes |
|---|---|---|
| "Hey GLaDOS" | <https://huggingface.co/johnthenerd/openwakeword-hey-glados> (`hey_glados.onnx`) | Apache 2.0, thematically on-brand for an AI intel bot. |
| 100-model collection | <https://github.com/fwartner/home-assistant-wakewords-collection> — files at `raw.githubusercontent.com/fwartner/home-assistant-wakewords-collection/main/en/<name>/<file>.onnx` | MIT repo, active (2026-01). Good picks: `jarvis`, `glados`, `marvin`, `hey_spock`, `TARS`, `skynet`, `wheatley`, `andromeda`, `hey_kitt`. **No published accuracy numbers — test before an op.** |

Avoid from that collection for fleet comms: `computer`/`ok_computer`
(everyday-speech collisions), `hal` (≈3 phonemes and phonetically adjacent to
**"hull"** — called constantly in fights), `hey_friday` (op scheduling),
`lisa`/`janet`/`scarlett`/etc. (real names on comms).

## Tier 3 — openwakeword.com community library (largest, benchmarked)

<https://openwakeword.com/library> — **1,239 wake words**, and the only source
that publishes **recall and false-activations/hour** per model. Download is a
free `.onnx` per model page.

- Search "cortana": **0 results** (confirmed).
- Interesting hits: "Hey Tars", "Hey Atlas", "Hey Spock", "hey glados",
  "Joshua", "Okay HAL".
- There are even two **"Hey aura"** models (EVE's ship AI!) — but the newer
  one benchmarks at **24.64 false activations/hour**: unusable on comms, and
  the phrase fails the §5.2 rules anyway ("hour", "or a"). Listed only as a
  cautionary example: **always check FA/hr before adopting anything here** —
  aim for < ~0.5/hr.

## Plan B for the actual phrase

The same site has a **web training center** that can train a custom
"hey cortana" model from synthetic speech and export openWakeWord ONNX — an
independent second path if the Kaggle run keeps failing. Different pipeline,
same output format; the deployment steps above are identical.

## Deployment reminders

- Whatever model you choose, the two base models
  (`melspectrogram.onnx`, `embedding_model.onnx`) must sit in the same
  directory — the installer already keeps them there.
- After switching, watch `#bot-health` for the wake counters: frames flowing
  with zero wake inferences is the silent-death signature; near-miss logs
  help pick the threshold.
- One model = one phrase. Changing phrases later is a config edit, not a
  retrain — keep the `.onnx` files side by side and point `wake.model` at
  the one in use, or list a transitional second phrase in
  `wake.extra_models`.
- **Multi-phrase accuracy caveat:** every extra model adds its *own*
  false-fire budget — false accepts add across models, they don't average
  out. Hold each phrase to the §5.2 rules (≥6 phonemes, no comms
  collisions), run **2-3 models at most**, and retire the old phrase once
  the per-model hit counters (`hits[<model>]` in `#bot-health` /
  `/botstatus`) show pilots have switched.
