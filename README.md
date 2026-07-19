# CORTANA

Voice-activated fleet intel bot for the DEAD corp's EVE Echoes Discord. A pilot
in fleet comms says *"Hey Cortana — please report that I'm tackled by enemies
in system M-tack-O and request heavy assistance"* and about a second and a
half later an incident card is in the intel channel, the right subscribers are
pinged, and CORTANA confirms over voice — against 20–40 seconds for alt-tabbing
out of the client to type it. No cloud services: wake word, speech
recognition, and TTS all run on one self-hosted droplet.

CORTANA is two processes joined by a Unix domain socket. **Ears** (Rust, Songbird
≥0.6) is a deliberately thin audio pump: it holds the Discord voice connection
(including the now-mandatory DAVE end-to-end encryption), decodes per-user PCM,
and plays TTS. **Brain** (Python, discord.py) is everything requiring
judgement: wake-word gating, Whisper STT, a fixed regex grammar (no LLM in the
command path), phonetic system-name matching against a small region-scoped
gazetteer, a per-user voice dialog engine (say-again retries, spoken confirms,
colour-code openers), the incident engine, routing, and notification
discipline. Every voice command has a slash-command twin hitting the same
engine, so if Discord ever breaks voice receive, CORTANA degrades to a fast text
bot instead of dying.

The grammar is padding-tolerant by design: courtesy phrasing, spelled system
names ("M tack O" → `M-OEE8`), spoken threat colours ("code red"), and radio
procedure ("report … end report", "over") all parse. Beyond intel it carries
fleet-ops utilities (structure timers, form-ups, chase mode, roll call, jump
routes, polls, reminders), an optional Claude-powered *"command override"*
side-channel for questions, and a morale kit: a bundled, offline-checked
library of 1,692 true facts across 16 categories plus a 293-line roast
generator (*"hey cortana, tell me a fact"* / *"insult this guy"* — voice-only
replies; `/fact` and `/insult` post in the invoking channel).

The full specification is [`docs/GDD.md`](docs/GDD.md) — it is the source of
truth. Contributors: read [`CLAUDE.md`](CLAUDE.md) before touching anything;
its hard constraints are absolute.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Droplet — Ubuntu 24.04 LTS                              │
│                                                          │
│  ┌──────────────────┐   UDS      ┌────────────────────┐  │
│  │  EARS   (Rust)   │  framed    │  BRAIN   (Python)  │  │
│  │  Songbird 0.6    │◄──────────►│  discord.py        │  │
│  │   DAVE / receive │  binary    │  wake word + VAD   │  │
│  │   48k→16k decim. │  + JSON    │  Whisper STT       │  │
│  │   opt-out drop   │            │  grammar+gazetteer │  │
│  │   TTS playback   │            │  incidents/routing │  │
│  └──────────────────┘            │  Piper TTS, SQLite │  │
│                                  └────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

Brain binds the socket; Ears connects and buffers through Brain restarts. The
wire format is GDD §15 and spans both languages — change both sides in one
commit or not at all.

## Quickstart

### Development

```bash
# Ears — read reference/songbird/ (vendored @ v0.6.0) before touching voice code
cd ears
cargo build --release
cargo clippy --all-targets -- -D warnings
cargo test

# Brain
cd brain
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pip install -e .
ruff check . && ruff format --check .
pytest
python -m cortana --config ../config/cortana.dev.yaml   # paths under ./dev-data/
```

Never add ffmpeg, PyNaCl, or `discord.py[voice]` — see CLAUDE.md constraints
2 and 3.

### Droplet deploy

CI builds the `cortana-ears` release binary on every main-branch push and
publishes it (binary + sha256) to the `ears-bin` branch — the 2 vCPU droplet
would thrash compiling Songbird with LTO, so it never builds Rust. The whole
deploy, first time and every time after:

```bash
cd /opt/aura-src && git pull && bash deploy/install.sh
```

`install.sh` is a staged converge-and-verify pipeline (GDD §17.5): it builds a
complete release under `/opt/cortana/releases/<id>/` (brain tree, verified
ears binary, venv — hardlink-cloned in seconds when requirements are
unchanged, and proven to import *this* release's code before anything flips),
gates on the new release's offline doctor against the live config, atomically
flips the `current` symlink, restarts only what changed, then verifies —
auto-rolling back to the previous release on any failure. `install.sh
--rollback` flips back manually.

First install: provision per GDD §17.1 (CPU-Optimized 2 vCPU / 4 GB, Ubuntu
24.04, region nearest your Discord voice region), then follow the checklist
the installer prints — token at `/etc/cortana/token` (mode 0600, loaded via
systemd `LoadCredential=` — never in config or environment), edit the
`/etc/cortana/*.yaml` configs, seed the gazetteer. Wake models ship in
`assets/wake/` and install automatically; validate any config edit with
`python -m cortana.doctor` before restarting.

Config-only changes hot-reload with `systemctl reload cortana-brain` (or the
`/reload` slash command — same transaction, with a receipt). Ears stays
connected across Brain restarts, buffering audio through them.

## Privacy

CORTANA puts a microphone-reading robot into a corp's social space. From GDD §19,
the governing decision: **CORTANA does not record anything.**

> - Audio lives in a **RAM ring buffer only**. Never written to disk.
>   Overwritten every 1.5 seconds.
> - The capture buffer is freed the instant STT returns.
> - CORTANA stores the **transcript of triggered commands**, never audio.
> - Non-command speech is **never transcribed at all** — the wake-word gate
>   means it never reaches the recogniser.

CORTANA announces itself every time it joins a voice channel, and `/optout` drops
a user's audio inside Ears, before any processing — an actual drop, not a
downstream filter. Introduce CORTANA to your corp with GDD §19, not with the
feature list.

## Documentation

- [`docs/GDD.md`](docs/GDD.md) — the full specification (the spec is the truth)
- [`docs/WAKE_WORDS.md`](docs/WAKE_WORDS.md) — the deployed "hey cortana"
  community model and every alternative wake-word option
- [`docs/INTERFACES.md`](docs/INTERFACES.md) — the historical phase-1 build
  contract (superseded by the GDD; kept for archaeology)
- [`training/wake/`](training/wake/) — offline wake-word training pipeline
  (GPU box or Colab, never the droplet) for additional/custom phrases
- [`CLAUDE.md`](CLAUDE.md) — hard constraints and conventions for contributors

In Discord, `/help` is the live manual: interactive topic pages covering every
voice phrase and slash command, kept honest by a test that fails CI when a
registered command is missing from the help text.
