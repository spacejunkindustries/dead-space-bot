# CORTANA (codename AURA) — project guide for Claude Code

Voice-activated fleet intel bot for an EVE Echoes corp Discord. Self-hosted on a DigitalOcean droplet.

**The full specification is `docs/GDD.md`. It is the source of truth.** This file covers only what you would otherwise get wrong. Read the GDD section referenced before working on any module.

Two processes, one socket:

- **`ears/`** — Rust. Songbird ≥0.6. Joins voice, negotiates DAVE, decodes per-user PCM, plays TTS. Deliberately thin.
- **`brain/`** — Python. discord.py, wake word, STT, grammar, gazetteer, incident engine, routing, TTS synthesis. Everything requiring judgement.

---

## Before you write any voice code

**Read `reference/songbird/` first. Every time.**

Your training data predates Songbird v0.6.0 (released 2026-04-05). You know 0.4/0.5. The differences are not cosmetic:

- **DAVE support did not exist** in any version you were trained on. You cannot recall this API. You will invent it.
- **`DecodeMode::Decode` changed shape in 0.6** — decode config moved *into* the variant.
- Receive is behind the `receive` feature flag and is not on by default.

`reference/songbird/` is vendored at the `v0.6.0` tag for exactly this reason. Read the actual signatures. Do not reconstruct them from memory, and do not pattern-match from Songbird tutorials — nearly all of them predate DAVE and are wrong.

If `cargo build` fails on a Songbird symbol, that is the system working. Go read the source; don't guess a different method name.

---

## Hard constraints

These look wrong. They are not. Do not "helpfully" fix them.

**1. Songbird ≥0.6 is pinned. Never downgrade. Never propose an alternative stack.**
It is the only Discord voice library that currently does DAVE *and* receive. Discord made DAVE mandatory on 2026-03-02; anything without it gets close code 4017 and cannot join voice at all.
- `py-cord` / `discord.py` voice → 4017. Dead.
- `@discordjs/voice` → joins, but receive is broken upstream (discord.js#11419).
Yes, Rust is more work than Python here. That is not a reason to change it. GDD §2.4.

**2. Python voice is not an option, but Python is.**
discord.py's gateway, slash commands, components, roles, and REST are all fine. Only `VoiceClient` is broken. Brain stays Python.
**Do not add `PyNaCl` or `discord.py[voice]` to requirements** — their absence is what makes it impossible to accidentally wire up Python voice.

**3. No ffmpeg. Anywhere.**
Songbird ≥0.4 removed it entirely in favour of Symphonia, in-process. Every Discord audio tutorial you've seen uses ffmpeg. Ignore them. Do not add it to the Dockerfile, the install script, or the deps.

**4. `self_deaf` must be `false` on join.**
A self-deafened bot receives no audio and raises no error. Silent, total failure. This has bitten everyone who has ever built this.

**5. Audio never touches disk. Not once.**
Not for debugging. Not in tests. Not behind a feature flag. Not in a temp file. RAM ring buffer only, overwritten every 1.5s.
This is the privacy guarantee the corp is asked to trust (GDD §19) and it is the reason CORTANA sidesteps recording-consent law entirely. Breaking it once breaks it permanently.
Need to debug the audio path? Log the **transcript** and the confidence score. `command_log` exists for this.

**6. No LLM in the command path.**
The grammar is fixed regex + phonetic matching. This is deliberate. An LLM would be slower, cost money per fight, hallucinate system names, and be undebuggable at 02:00 during a hostile timer. GDD §6.

**7. Match on phonemes, not characters.**
STT errors are phonetic, not typographic. Whisper writes *"oh tan you oh me"* — that's character-distance-far from *Otanuomi* but phonetically adjacent. Levenshtein on raw text alone is the wrong tool. Metaphone first, weighted 0.6 phonetic / 0.4 text. GDD §8.2.

**8. The gazetteer stays small by default — but nomadic mode is a sanctioned exception.**
Scoped mode (~100–500 systems scoped to the corp's operational region, not all of New Eden) is the **default and the recommended path for home-region corps**. It is the core accuracy decision, not a shortcut: matching against 300 entries beats matching against 5,000. Do not silently widen a scoped gazetteer.
There is one supported exception: **`include_all` mode** (`gazetteer.yaml`). Some corps are **nomadic** — no fixed home, they relocate and must report *any* k-space system — and for them scoping to a region that changes weekly is worse than a wide gazetteer. `include_all: true` activates the entire seeded (k-space) map as a first-class mode, with `gazetteer.home_system: null` disabling the home-bias prior. The accuracy tradeoff is real (more homophones in a 5,000-entry set) and is absorbed by the confirm-flow (§8.3), context priors (§8.4), and alias learning (§8.5) — not by pretending it isn't there. `python -m cortana.nlu.seed` loads k-space wide precisely so both modes work off one seed. GDD §8.1.

**9. Incident cards are edited in place.**
One incident = one message, updated. Never post a second message for the same incident. Five pilots reporting one gate camp produce one card reading "reported by 5". GDD §9.1.

**10. Every voice command has a slash-command twin hitting the same engine.**
Non-negotiable invariant. Voice receive is undocumented and can break without notice; the slash path is what keeps CORTANA alive when it does. If you add a voice command without its twin, the PR is incomplete. GDD §20.

**11. `@here` only for `UNDER_ATTACK` and `ASSIST_REQUEST`.**
Never for sightings. Ever. Notification fatigue kills this bot faster than any bug. GDD §11.
The enforcement point is `decide_mentions()` in `brain/cortana/core/routing.py` — the single escalation authority every mention flows through; never add an `@here` (or any mention) decision anywhere else.

**12. Secrets via systemd `LoadCredential=` only.**
Never `.env`, never YAML, never `os.environ` in committed code, never a default in `config.py`.

---

## Repo layout

```
ears/                 Rust — voice socket
  src/
    main.rs           config, Serenity client, Songbird registration, signals
    voice.rs          VoiceTick / SpeakingStateUpdate handlers, SSRC↔user, opt-out drop
    dsp.rs            stereo→mono, 61-tap sinc lowpass, 3:1 decimation 48k→16k
    ipc.rs            framed UDS client, reconnect backoff, 60s ring buffer
    playback.rs       WAV → Songbird input, priority queue, talk-over suppression
brain/                Python — everything else
  cortana/
    audio/            vad, wake, capture, stt
    nlu/              grammar, gazetteer, phonetics
    core/             incidents, routing, discipline, db
    dsc/              bot, views, cogs/
    tts.py health.py ipc.py config.py voice_gateway.py
  schema.sql
config/               cortana.yaml, gazetteer.yaml, routing.yaml (examples)
deploy/               systemd units, install.sh
docs/GDD.md           ← the spec
reference/songbird/   vendored @ v0.6.0 — read before touching voice
```

The module table in **GDD §4** lists every file and its responsibility. Match it.

---

## Commands

```bash
# Rust
cd ears
cargo build --release
cargo clippy --all-targets -- -D warnings
cargo test

# Python
cd brain
ruff check . && ruff format --check .
pytest
python -m cortana --config ../config/cortana.dev.yaml
```

CI runs all of the above on every PR. **CI also builds the release binary** — the droplet is 2 vCPU and will thrash compiling Songbird with LTO, so it downloads the artifact instead of building.

---

## Architecture invariants

**Ears is thin.** It pumps audio and plays WAVs. If you find yourself adding judgement to Ears — thresholds, matching, routing, anything with a config knob about *meaning* — it belongs in Brain.

**Opt-out is enforced in Ears**, before frames cross the IPC boundary. It is an actual drop, not a downstream filter. Do not move it to Brain "for simplicity".

**The IPC wire format spans both languages.** If you change it, change both sides in the same commit and update GDD §15. A silent desync here produces a bot that connects, looks healthy, and does nothing.

```
Frame: [4-byte BE length][1-byte type][body]
  0x01  JSON control (UTF-8)
  0x02  Audio   Ears→Brain  [8B user_id LE][8B guild_id LE][8B captured_at ms LE][i16 LE PCM 16kHz mono]
  0x03  TTS     Brain→Ears  [8B guild_id LE][1B priority][WAV bytes]
```

The protocol is versioned (`IPC_PROTOCOL_VERSION`, both binaries, exchanged in `hello`); bump it with any wire change, in the same commit on both sides.

**Brain binds the socket; Ears connects.** This ordering is deliberate — it means Ears buffers through a Brain restart rather than the reverse.

**Brain does not resample.** Piper emits raw s16le, Brain wraps a WAV header in memory, Songbird's Symphonia layer handles the rest. No resampling code in Python, no temp files.

---

## Conventions

- **Rust:** `anyhow` at boundaries, `thiserror` for library errors. `tracing`, not `println!`. No `unwrap()` outside tests.
- **Python:** 3.12, full type hints, `ruff` clean. `structlog`-style JSON logging. Blocking work (STT, Piper) goes in a thread pool — never on the event loop.
- **SQL:** migrations in `brain/migrations/`, never edit `schema.sql` in place for a live change.
- **Config:** every tunable lives in `cortana.yaml` with the default in the example file. No magic numbers in code. The thresholds in GDD §16 are starting values, tuned from real fleet audio — expect them to move.
- **Tests:** the phonetic matcher, grammar parser, dedupe logic, and routing evaluator are pure functions. Test them properly. The audio path is tested with synthetic PCM fixtures generated in-memory (see constraint 5).

### Language

Don't label anything MVP, prototype, alpha, beta, or v0 — not in comments, commit messages, PR titles, docs, or user-facing strings. Modules ship complete or they don't ship. If something is genuinely unfinished, say what's missing, not what stage it's at.

---

## Definition of done

A module is done when:

1. It matches its row in GDD §4 and its detailed section.
2. `cargo clippy -- -D warnings` / `ruff check` are clean.
3. Pure logic has tests.
4. Its config surface is in `config/cortana.yaml.example` with defaults.
5. If it's a voice command, its slash twin exists and shares the engine (constraint 10).
6. If behaviour diverged from the GDD, **the GDD is updated in the same PR.** The doc is the spec; a stale spec is worse than none.

---

## Things that need a human, not a commit

Don't try to solve these in code. Flag them.

- **Verifying DAVE actually negotiates.** Needs a real token, a real voice channel, and a real person talking. Not automatable.
- **Training the openWakeWord model.** Separate synthetic-data + training run. You can write the script; you can't do the run.
- **Tuning thresholds and prior weights.** Those numbers come from recordings of real fleet ops, not from reasoning.
- **False-accept rate of the wake phrase.** Only measurable against real comms audio.

---

## Reference

- GDD: `docs/GDD.md`
- Songbird docs: https://docs.rs/songbird/latest/songbird/
- Songbird v0.6.0 release notes (DAVE): https://github.com/serenity-rs/songbird/releases
- DAVE protocol: https://daveprotocol.com/
- Discord voice connections: https://docs.discord.com/developers/topics/voice-connections
- openWakeWord: https://github.com/dscripka/openWakeWord
- Piper: https://github.com/OHF-Voice/piper1-gpl
