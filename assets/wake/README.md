# Bundled wake models

Community-trained "hey cortana" openWakeWord models (creator #2124 on
openwakeword.com, phrase spelled "hey cortahnah" to force the TTS
pronunciation — which is also why name searches never found them).

| File | Arch/steps | Published FA/hour | Note |
|---|---|---|---|
| `hey_cortahnah_64x1_20k.onnx` | 64x1, 20,000 | **1.0** | Deploy first — calmest false-fire rate. |
| `hey_cortahnah_128x3_80k.onnx` | 128x3, 80,000 | 3.4 | Fallback; saner rejection profile on synthetic probes but noisier measured FA. |

Deployment (install.sh copies these to `/opt/cortana/models/wake/`):

```yaml
wake:
  model: /opt/cortana/models/wake/hey_cortahnah_64x1_20k.onnx
  threshold: 0.6      # start here; tune with the near-miss logs / wake counters
```

Downloaded from openwakeword.com's community library (free download tier).
The site also publishes three sibling variants (128x3@25k FA 1.4/1.8,
96x2@35k FA 3.0) if these two disappoint. See docs/WAKE_WORDS.md.
