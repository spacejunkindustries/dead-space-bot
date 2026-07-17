# AURA

Voice-activated fleet intel bot for the DEAD corp's EVE Echoes Discord. A pilot
in fleet comms says *"Aura command — hostiles in Otanuomi, five tacklers"* and
about a second and a half later an incident card is in the intel channel, the
right subscribers are pinged, and AURA confirms over voice — against 20–40
seconds for alt-tabbing out of the client to type it. No cloud services: wake
word, speech recognition, and TTS all run on one self-hosted droplet.

AURA is two processes joined by a Unix domain socket. **Ears** (Rust, Songbird
≥0.6) is a deliberately thin audio pump: it holds the Discord voice connection
(including the now-mandatory DAVE end-to-end encryption), decodes per-user PCM,
and plays TTS. **Brain** (Python, discord.py) is everything requiring
judgement: wake-word gating, Whisper STT, a fixed regex grammar (no LLM in the
command path), phonetic system-name matching against a small region-scoped
gazetteer, the incident engine, routing, and notification discipline. Every
voice command has a slash-command twin hitting the same engine, so if Discord
ever breaks voice receive, AURA degrades to a fast text bot instead of dying.

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
python -m aura --config ../config/aura.dev.yaml   # paths under ./dev-data/
```

Never add ffmpeg, PyNaCl, or `discord.py[voice]` — see CLAUDE.md constraints
2 and 3.

### Droplet deploy

CI builds the `aura-ears` release binary on every push (the 2 vCPU droplet
would thrash compiling Songbird with LTO — never build it there). Deploy:

1. Provision per GDD §17.1 (CPU-Optimized 2 vCPU / 4 GB, Ubuntu 24.04, region
   nearest your Discord voice region).
2. Download the `aura-ears` artifact from the latest green CI run.
3. `sudo deploy/install.sh path/to/aura-ears` — idempotent; installs apt deps,
   the `aura` user, `/opt/aura`, the Brain venv, config examples, systemd
   units, and tmpfiles.
4. Follow the printed checklist: token at `/etc/aura/token` (mode 0600, loaded
   via systemd `LoadCredential=` — never in config or environment), model
   files under `/opt/aura/models/`, edit the `/etc/aura/*.yaml` configs, seed
   the gazetteer.
5. `systemctl start aura-brain aura-ears`

Config reloads with `systemctl reload aura-brain`; code changes need a
restart. Ears stays connected across Brain restarts.

## Privacy

AURA puts a microphone-reading robot into a corp's social space. From GDD §19,
the governing decision: **AURA does not record anything.**

> - Audio lives in a **RAM ring buffer only**. Never written to disk.
>   Overwritten every 1.5 seconds.
> - The capture buffer is freed the instant STT returns.
> - AURA stores the **transcript of triggered commands**, never audio.
> - Non-command speech is **never transcribed at all** — the wake-word gate
>   means it never reaches the recogniser.

AURA announces itself every time it joins a voice channel, and `/optout` drops
a user's audio inside Ears, before any processing — an actual drop, not a
downstream filter. Introduce AURA to your corp with GDD §19, not with the
feature list.

## Documentation

- [`docs/GDD.md`](docs/GDD.md) — the full specification (the spec is the truth)
- [`docs/INTERFACES.md`](docs/INTERFACES.md) — Brain module interface contract
- [`CLAUDE.md`](CLAUDE.md) — hard constraints and conventions for contributors
