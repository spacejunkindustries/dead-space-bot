# CORTANA — Voice-Activated Fleet Intel Bot

> **Naming.** The bot presents as **CORTANA** everywhere a user sees or hears it. The internal codename remains **aura**: the Python package, repository layout, systemd units (`cortana-brain`/`cortana-ears`), and filesystem paths (`/opt/cortana`, `/etc/cortana`, `/run/cortana`) keep the old name deliberately — renaming live infrastructure buys no user-visible change and risks the running deployment.

**Game Design Document**
EVE Echoes corp utility bot · self-hosted on DigitalOcean
Version 1.0 · July 2026

---

## 1. Product definition

CORTANA sits in your corp's Discord voice channel and listens for spoken reports. A player under attack says:

> **"Hey Cortana, hostiles Otanuomi, three battleships"**

Roughly a second and a half later, CORTANA posts a live incident card in `#intel-alerts`, mentions the roles that subscribe to that system, and speaks back into voice:

> *"Hostiles Otanuomi, pinged home defense."*

Corpmates tap **🚀 On my way**. CORTANA speaks again:

> *"Two responding to Otanuomi."*

The reporting player never leaves the game.

### 1.1 The problem being solved

EVE Echoes is a mobile game. Calling for help currently means putting the game down at the exact moment you cannot afford to:

| Step | Time |
|---|---|
| Minimize EVE Echoes | 2–4s |
| Open Discord, find the right channel | 5–10s |
| Type the report, spell the system correctly | 8–20s |
| Ping the right role | 2–5s |
| **Total** | **20–40s — you are already in a pod** |

CORTANA's path: **~1–2 seconds from the moment you stop talking.** That gap is the entire product.

### 1.2 Operating envelope

CORTANA is built for the **solo and small-gang player** — ratting, mining, or roaming, jumped without warning, hands full, no time to type.

It is explicitly **not** built to run during structured fleet ops with an FC on comms. People shouting at a bot would step all over fleet command. CORTANA ships with a **fleet-ops mode** (§11.4) that restricts voice triggering to the FC role, and corps are expected to use it. Design for the lonely ratter.

### 1.3 Scope

CORTANA is a complete corp utility bot. It ships with:

- Voice-triggered incident reporting
- Live incident tracking with response coordination
- Role-scoped subscription routing
- Spoken confirmations and response counts
- Structure timers and fleet form-ups (voice and slash)
- Full slash-command parity for every voice command
- Self-service subscription management
- Admin configuration, health reporting, and gazetteer management

---

## 2. Platform constraints: Discord voice in 2026

This section is normative. It dictates the tech stack, and much of the public guidance on this topic is now wrong.

### 2.1 Speaking is easy and fully supported

Playing audio into a voice channel is a documented, first-class Discord feature supported by every voice library. CORTANA's spoken back-channel carries no platform risk.

### 2.2 Listening is undocumented and always has been

Voice **receive** is not documented by Discord. Every library that implements it did so by reverse engineering, and Discord has stated it is unlikely to ever officially support or document the feature. It works, it is not guaranteed, and it can break on a Discord-side change with no notice.

This is a permanent structural property of the platform, not a defect in CORTANA. §20 specifies how CORTANA absorbs it: **every voice capability has a slash-command twin backed by the same engine**, so a broken receive path costs CORTANA its speed advantage and nothing else.

### 2.3 DAVE: end-to-end encryption is mandatory

Discord completed its rollout of the **DAVE protocol** (Discord Audio & Video End-to-End Encryption) in March 2026. Since **March 2, 2026**, any client or application without DAVE support is rejected by the voice gateway with **close code 4017**. There is no opt-out, and the unencrypted fallback code path has been removed.

Consequences that shape this design:

- CORTANA must join the MLS group and negotiate DAVE simply to enter a voice channel.
- Any bot, tutorial, or library last touched before 2026 is dead on arrival.
- **Library support is now the binding constraint on the stack.** Most options are non-viable.

### 2.4 Library support — the decision that shapes the architecture

| Stack | Joins voice (DAVE) | Receives audio | Status |
|---|---|---|---|
| **Rust — Songbird ≥0.6** | ✅ DAVE landed in v0.6.0 "Hoopoe" (2026-04-05) | ✅ `receive` feature | **Selected** |
| Node — `@discordjs/voice` + `@snazzah/davey` | ✅ (davey ships pre-installed; without it the bot sends `max_dave_protocol_version: 0` and gets 4017) | ❌ Open upstream defect — `DecryptionFailed(UnencryptedWhenPassthroughDisabled)`, reconnect loops, zero audio. Send/TTS unaffected. | Rejected |
| Python — py-cord / discord.py + `voice_recv` | ❌ 4017 on connect; no DAVE in the library | ❌ moot | Rejected for voice |

**Songbird ≥0.6 is a hard requirement of this design, not a preference.** It is currently the only stack that does both DAVE and receive.

It is also, fortunately, an excellent fit:

- **`VoiceTick` fires every 20ms** carrying **per-user, reordered, jitter-buffered, decoded PCM** — synchronised across speakers, ready to consume.
- Per-user streams keyed by SSRC, mapped to user IDs via speaking-state events. CORTANA always knows *who* said something.
- Symphonia in-process; ffmpeg removed entirely. Light footprint.
- Deadline-aware scheduler — ~660 concurrent Opus-passthrough calls on a single thread on a Ryzen 5700X. CORTANA needs one.

### 2.5 Other platform facts CORTANA depends on

- **Self-deaf must be off.** A self-deafened bot receives nothing and raises no error. This is the single most common cause of a silent voice bot.
- Bots respect the voice channel user limit unless granted `MOVE_MEMBERS`.
- Discord exposes an `allow_voice_recording` voice flag per member — whether that user has consented to being clipped. CORTANA reads it as a consent signal (§19).
- Stage channels are excluded from DAVE and remain unencrypted. Not used by CORTANA.
- The `GUILD_VOICE_STATES` intent is required.
- Discord's client-side voice activity gating means CORTANA receives packets **only from users actively transmitting**. Idle listeners cost nothing.

### 2.6 Python is not excluded — only Python *voice* is

discord.py's gateway, slash commands, components, roles, and REST are unaffected by the DAVE change. Only `VoiceClient` is broken. CORTANA therefore uses Rust exclusively for the audio socket and Python for everything else — which is where the speech and phonetics ecosystem lives.

---

## 3. System architecture

Two processes on one droplet, joined by a Unix domain socket.

```
┌───────────────────────────────────────────────────────────────┐
│  DigitalOcean droplet — Ubuntu 24.04 LTS                      │
│                                                               │
│  ┌────────────────────┐              ┌────────────────────┐  │
│  │  EARS  (Rust)      │              │  BRAIN  (Python)   │  │
│  │                    │   UDS        │                    │  │
│  │  Serenity gateway  │  framed      │  discord.py        │  │
│  │  Songbird 0.6      │◄────────────►│  slash + components│  │
│  │   ├ DAVE / MLS     │  binary +    │  wake word (oww)   │  │
│  │   ├ VoiceTick 20ms │  JSON        │  VAD (webrtcvad)   │  │
│  │   ├ SSRC→user map  │              │  STT (whisper)     │  │
│  │   ├ 48k→16k decim. │              │  grammar + gazetteer│ │
│  │   ├ opt-out drop   │              │  incident engine   │  │
│  │   └ TTS playback   │              │  routing + pings   │  │
│  └────────────────────┘              │  Piper TTS         │  │
│                                      │  SQLite            │  │
│                                      └────────────────────┘  │
│                                               │               │
│                                      /var/lib/cortana/cortana.db    │
└───────────────────────────────────────────────────────────────┘
```

### 3.1 Why the split

1. **Only Rust can hear.** Only Python has the speech stack. The socket is the seam.
2. **Ears is the fragile half.** If Discord breaks receive, Ears dies alone. Brain keeps every slash command, incident, and ping working (§20).
3. **Different latency classes.** A slow SQLite write must never jitter the audio thread. Separate processes, separate schedulers.
4. **Independent restart.** Brain redeploys without dropping the voice connection.

### 3.2 Division of labour

**Ears is deliberately thin.** It is a DAVE-capable audio pump and nothing more:

- Maintain the gateway and voice connections
- Decode per-user PCM from `VoiceTick`
- Map SSRC → user ID
- Decimate 48kHz → 16kHz mono
- Drop opted-out users' frames *before they ever leave the process*
- Forward frames for actively-speaking users to Brain
- Accept WAV bytes from Brain and play them
- Reconnect, with backoff, forever

Everything that requires judgement lives in Brain.

### 3.3 Why not run wake-word detection in Ears

Because it is unnecessary and it would cost the design its best language. Discord already gates transmission client-side, so Ears only ever receives audio from people actually talking. At 16kHz mono that is **32 KB/s per active speaker** — a fully-loaded 30-person channel with five people talking at once is 160 KB/s across a Unix socket that moves gigabytes. The bandwidth argument for pushing ML into Rust does not survive contact with arithmetic.

Brain gates wake-word inference behind VAD, so inference runs only on active speech. openWakeWord runs 15–20 models concurrently in real time on a **single Raspberry Pi 3 core**; the dedicated vCPUs in §17 are not going to notice.

---

## 4. Component manifest

Every module in the finished system.

### Ears (Rust)

| Module | Responsibility |
|---|---|
| `main.rs` | Config load, Serenity client, Songbird registration, signal handling, IPC connect loop |
| `voice.rs` | `VoiceTick` / `SpeakingStateUpdate` / `ClientDisconnect` handlers; SSRC↔user table; opt-out drop; join/leave |
| `dsp.rs` | Stereo→mono fold; 61-tap Hamming-windowed sinc lowpass @ 7.5kHz; 3:1 decimation 48k→16k |
| `ipc.rs` | Framed UDS client, reconnect with backoff, 60s outbound ring buffer, heartbeat |
| `playback.rs` | WAV bytes → Songbird input; priority queue; talk-over suppression; volume ducking |

### Brain (Python)

| Module | Responsibility |
|---|---|
| `__main__.py` | Entrypoint, task supervision, graceful shutdown |
| `config.py` | YAML load, schema validation, hot-reload on SIGHUP |
| `ipc.py` | UDS server, frame codec, per-user stream demux, Ears liveness |
| `audio/vad.py` | webrtcvad wrapper; utterance endpointing |
| `audio/wake.py` | openWakeWord instances (one per configured phrase: `wake.model` + `wake.extra_models`, scored as a max), per-user state, score thresholds; off-loop spare-model pool, fault latch, stage counters |
| `audio/capture.py` | Per-user capture state machine: pre-roll ring buffer → wake hit → capture → endpoint → emit; owns the wake refractory period |
| `audio/stt.py` | `Transcriber` protocol; faster-whisper (default) and whisper.cpp HTTP backends; gazetteer prompt biasing; bounded serialized decode queue + `stt.watchdog_s` hang watchdog |
| `nlu/grammar.py` | Intent extraction, group-alias extraction, detail capture |
| `nlu/llm_nlu.py` | The understanding brain (§6.7): on-box LLM reads a transcript the grammar missed → structured command JSON → `ParsedCommand` |
| `nlu/gazetteer.py` | System load, region pruning, adjacency graph, jump-distance BFS with memo |
| `nlu/phonetics.py` | Double Metaphone + Levenshtein scoring, alias-table lookup, context priors, confidence tiers |
| `core/incidents.py` | Incident lifecycle, dedupe folding, staleness sweep, message rendering |
| `core/callsigns.py` | Pilot callsign registry: register/unregister/who-am-I, sync lookup mirror for renders |
| `core/personal_pings.py` | Personal ping subscriptions (§10.3): capped per-user store, sync mirror for routing |
| `core/routing.py` | Subscription evaluation, role union, escalation, quiet hours, personal-ping user mentions |
| `core/discipline.py` | Per-user cooldowns, global circuit breaker, flood control |
| `core/fun.py` | Fact library + insult maker (§13.2): bundled-JSON load, per-guild shuffle bags, cooldowns, spicy filter, topic/target extraction |
| `core/areas.py` | Learned custom areas (§8.5a): pure per-guild store — save/lookup/list/forget/cap for confirmed place words |
| `core/db.py` | SQLite access, migrations, backup hook |
| `dsc/bot.py` | discord.py client, intents, persistent view registration |
| `dsc/views.py` | Incident buttons, ambiguity resolver, subscription picker |
| `dsc/cogs/intel.py` | `/hostiles`, `/under-attack`, `/help-me`, `/camp`, `/clear`, `/status`, `/cancel` |
| `dsc/cogs/subs.py` | `/subscribe`, `/mysubs`, `/pingme`, `/mypings`, `/pingme-clear`, `/optout`, `/mute-voice`, `/register`, `/unregister`, `/whoami` |
| `dsc/cogs/ops.py` | `/timer`, `/formup`, `/rollcall`, `/jumps` |
| `dsc/cogs/fun.py` | `/fact`, `/insult` — slash twins of the voice FACT/INSULT intents (§13.2) |
| `dsc/cogs/admin.py` | `/routing`, `/gazetteer`, `/health`, `/fleetmode` |
| `dsc/cogs/areas.py` | `/areas-list`, `/areas-forget`, `/areas-add` — manage learned custom areas (§8.5a) |
| `dsc/cogs/help.py` | `/help` — interactive help topics; the slash twin of voice "help" |
| `tts.py` | Piper subprocess, raw→WAV wrapping, priority-ordered utterance queue (ALERT ahead of queued NORMAL), length cap |
| `health.py` | Heartbeats, degradation detection, `#bot-health` reporting |
| `voice_gateway.py` | Voice-state watch, auto-join/leave, Ears join/leave commands |

### Assets

| Path | Contents |
|---|---|
| `/opt/cortana/models/wake/` | openWakeWord ONNX chain (melspec → embedding → wakeword) |
| `brain/cortana/data/facts/` | The bundled fact/insult library (§13.2): 16 category JSON files + 3 insult-flavour files, shipped inside the package |
| `/var/lib/cortana/hf/` | Whisper `small` int8 CTranslate2 weights — the Hugging Face cache shared between install.sh's prefetch and the service (`Environment=HF_HOME`), so a size-name `stt.model` never re-downloads at runtime |
| `/opt/cortana/models/whisper/` | Hand-installed weights for a *path-based* `stt.model` only (unused by the shipped `model: small`) |
| `/opt/cortana/models/piper/` | Piper voice `.onnx` + `.onnx.json` |
| `/etc/cortana/cortana.yaml` | Main configuration (§16) |
| `/etc/cortana/routing.yaml` | Subscription rules (§12) |
| `/var/lib/cortana/cortana.db` | SQLite |

---

## 5. Voice pipeline

Per active speaker, continuously:

```
Songbird VoiceTick (20ms, per-user decoded PCM 48kHz)
   │
   ├─ opt-out check ──► DROP (in Ears, before IPC)
   │
   ├─ stereo→mono, 48k→16k decimation                    [Ears]
   │
   └─► UDS ─────────────────────────────────────────────►[Brain]
                                                            │
       ┌────────────────────────────────────────────────────┘
       │
       ├─► pre-roll ring buffer (last 1.5s, RAM only, never written to disk)
       │
       ├─► webrtcvad ──── speech present?
       │        │ yes
       ├─► openWakeWord ──── "Hey Cortana"?
       │        │ hit
       │        ▼
       │   capture window opens
       │   (pre-roll seed + speech until the dialog wheel's wall-clock
       │    endpoint fires; frame-counted hard cap as the backstop — §5.4)
       │        │
       │        ▼
       │   STT — gazetteer-biased
       │        │
       │        ▼
       │   grammar parse → system resolution → incident engine
       │        │
       │        ├─► Discord: post/edit incident card, mention roles
       │        └─► Piper TTS → UDS → Ears → voice channel
       │
       └─► refractory period: 2s per user after a hit
```

**Wake acknowledgement.** The instant the capture window opens, CORTANA tells the pilot it is listening — so they know to start talking, not repeat the wake word. The form is `wake.ack`: `voice` speaks *"Go ahead."*, `beep` plays an instant tone, `none` is silent. `beep` is the default because the tone is instant, while a spoken cue costs one Piper synthesis; corps that want CORTANA to talk back set `voice`. The cue plays at ALERT priority (jumping the queue) and, since CORTANA never captures its own playback, never bleeds into the utterance being recorded.

### 5.1 Wake word

**Engine: openWakeWord** (Apache 2.0). Free, self-hosted, no per-seat licensing, and it benchmarks competitively against the leading commercial engine. Custom phrases are trained from a synthetic TTS pipeline; the trained ONNX chain ships in `/opt/cortana/models/wake/`.

*Porcupine is the mature commercial alternative — type-to-train, instant custom words — but its free tier is scoped to personal use and commercial licensing starts around $6k/yr. A 50-pilot corp bot is not a comfortable fit for the free tier. CORTANA does not require it.*

**Multiple wake phrases.** `wake.model` is the primary phrase; `wake.extra_models` (default empty) lists additional ONNX chains scored **in parallel** — built for phrase transitions, e.g. keeping `hey_jarvis` live beside a freshly trained custom phrase until pilots retrain their reflexes. Every 80 ms chunk runs through every configured model; the chunk's score is the **max** across them, and `wake.threshold` applies to that max, so any listed phrase wakes CORTANA. Per-model thresholds are deliberately not supported — one bar, one knob to tune. Mechanics: each per-user unit is a *bank* holding one model instance per path, generation-tagged on the full model list (any list change — add, remove, reorder — rebuilds live banks through the spare pool on reload, no restart). A missing or broken **extra** model is logged once per config generation and skipped — the primary and the remaining extras keep running; only a broken **primary** latches the detector faulted (§20). The wake stage counters report hits per model (`hits[<model>]` in `#bot-health` / `/botstatus`), so an operator can see which phrase pilots actually use and retire the old one on evidence.

### 5.2 The wake phrase

Two constraints govern the choice:

1. **At least six phonemes**, with diverse sounds. Short phrases false-fire constantly.
2. **Nobody says it by accident in fleet chat.** This rules out most EVE vocabulary — "Concord", "capsuleer", and "Aura" alone are said constantly on comms and would fire all night.

| Phrase | Verdict |
|---|---|
| "Aura" | ❌ ~3 phonemes. Fires on *hour*, *or a*, *aura*. |
| **"Hey Cortana"** | ✅ ~9 phonemes, distinctive, never said naturally. **Default.** |
| "Hey Overseer" | ✅ Good phonetics, low collision. Supported alternative. |

The phrase is configurable. The 6-phoneme / no-collision rule is not optional — it is the difference between a tool and a nuisance.

Every entry in `wake.extra_models` (§5.1) must pass the same two rules, and the accuracy cost is additive: each model brings its **own** false-fire budget — false accepts across models add up, they do not average out, and one sloppy extra phrase can undo a carefully tuned primary. Run **2–3 models at most**, treat the multi-phrase state as transitional, and drop the old phrase once the per-model hit counters show pilots have switched.

### 5.3 Speech recognition

**Default backend: faster-whisper, `small`, int8, CPU.** ~3.4% WER on clean English, ~2GB RAM, and sub-second on the 2–4s clips CORTANA actually feeds it. CTranslate2 releases the GIL during inference, so it runs in a thread pool without blocking the event loop.

**Alternative backend: whisper.cpp HTTP server.** Selectable via `stt.backend`. Its native quantized kernels are more predictable on CPU-only hosts; the tradeoff is an extra process. Both implement the same `Transcriber` protocol.

**Prompt biasing** is applied on every call: Whisper's `initial_prompt` (and `hotwords`, where the backend supports it) carries the active system list **followed by the grammar's own trigger vocabulary** (`grammar.STT_VOCAB_BIAS` — hostiles, tackled, gate camp, code red, insult, roast, tell me a fact, …). Both halves matter, and the second was learned in the field: a names-only prompt biased the decoder so hard toward system names that casual command words came back system-shaped ("roast" → "Woust"). The vocabulary rides at the **tail** because Whisper truncates an over-long prompt from the front — the tail is the part guaranteed to survive.

Two known artifacts of prompt biasing, and their owners:

- **Prompt parroting.** On unclear or near-silent audio Whisper may continue the prompt instead of transcribing — a transcript that is literally a run of system names (`"Systems, Rens, Dodixie, …"`). The relay-framing gate (§8.6) is what keeps these from becoming junk cards; they die as `relay_unframed`.
- **Phonetic mangling of trigger words** ("insult" → "Insalt", "ping me" → "pink me"). Owned by `_JARGON_NORMALIZE` in the grammar: a small, auditable table of the spellings STT actually produces, extended from `command_log` evidence whenever a real mishearing slips through — never speculatively.

**Decode settings** are tuned for a busy fight, not a benchmark. Greedy decoding (`beam_size=1`, `temperature=0`, no fallback ladder) keeps a 6 s utterance in low single-digit seconds and avoids the re-decode storms that dominated an early 20–30 s voice→card latency. `condition_on_previous_text=false` stops a hallucination in one utterance from seeding the next. Silero `vad_filter` trims non-speech (keyboard, breath, game bleed) before decode. And `stt.no_repeat_ngram_size` (default 3) forbids a repeated n-gram, because even greedy decoding loops on noisy audio — one real system name emitted dozens of times (`"0-R5TS, 0-R5TS, …"`) at *high* confidence, which buries the callout and, worse, starves streaming's early-commit (§5.5) since no partial ever carries a clean command. The guard is applied where the installed faster-whisper exposes the parameter and skipped (logged) where it doesn't.

### 5.4 The dialog engine — one state machine, one clock, one door

Everything conversational — say-again retries, the "code *colour*" opener, the bare "command override" question flow, endpointing, timeouts — runs through **one per-user state machine** (`brain/cortana/dialog/`). This section is the contract; the transition function is pure and table-tested against it.

*History note: dialog state used to be five independent wall-clock dicts plus a frame-counted reopen window. Discord DTX sends no packets during silence, so the frame-counted half could never expire while the meaning half aged out on its own clock — the direct cause of the say-again loop, stuck-open wake-free windows, and mid-question interruption incidents. This design replaces that, and its three rules are what make those classes unreachable.*

**States.** `IDLE → LISTENING (capture open) → THINKING (STT/engine) → IDLE`, plus five `AWAIT_*` states in which a wake-free window is armed: `AWAIT_REPEAT` (say-again), `AWAIT_RETRY_SYSTEM` (§8.3 LOW-tier rebind), `AWAIT_SEVERITY_REPORT` (§6.4 code opener), `AWAIT_OVERRIDE_QUESTION` (§6.6), `AWAIT_CONFIRM` (§8.3 confirms — MEDIUM-tier and confirm-first).

**Two absolute exits, learned in heavy comms chatter (live complaint — the say-again loop had no spoken way out and kept recapturing conversation):**

- **Dismissal.** A standalone *"end transmission"* / *"disregard"* / *"never mind"* / *"belay that"* / *"stand down"* / *"stop"* closes the dialog from ANY point — mid-capture, any retry window, even a pending confirm-first report (an explicit abort fails closed) — answered with *"Standing down."* so the pilot hears the door shut. Standalone only: *"stop pinging me"* stays a command. *"cancel"* is deliberately NOT a dismissal — it keeps its meaning (retract the last incident).
- **The garbage gate.** A transcript below `dialog.retry_min_logprob` (default −1.3) that matched nothing is chatter/noise: the dialog closes **silently** instead of prompting *"say again"* into an open mic that will only recapture more conversation. Recognised commands are never gated by this — a quiet mic's distress call still posts.

**Rule 1 — one door.** The machine's `fail()` / `open_subdialog()` helpers are the *only* code paths that can arm a wake-free capture window. Every failure class — STT error, unmatched speech, low-confidence transcript, override noise, LOW-tier rejection — flows through the same door and draws on one per-dialog **retry budget** (`dialog.max_retries`, default 2, shared with deliberate subdialog openers). Only a fresh wake refills the budget; exhaustion always terminates *audibly* ("Standing down. Wake me to retry."). A self-sustaining reopen loop is structurally impossible, not merely guarded against.

**Rule 2 — one clock.** All dialog timing lives in the engine's 100 ms wheel on the event loop's monotonic clock: DTX-tolerant utterance endpointing (`max(capture.endpoint_silence_ms, dialog.endpoint_gap_floor_ms)` of packet silence, gated by `dialog.ack_grace_ms` after any prompt), armed-window expiry (`dialog.window_ms` of real time, frames or no frames), and the `AWAIT_*` TTLs (same deadline as their window). The capture layer keeps exactly one frame-counted duration — the hard cap — because frames flow while a pilot is actually talking.

**Rule 3 — generation tokens.** Every window arm and capture open mints/carries a generation token; emissions, STT results, and deadlines are dropped when their token is stale instead of being misattributed to a newer dialog. Pending context (an inherited severity, a retry rebind, an open override question) lives *in the session* and is consumed by the utterance its window actually captured — STT latency can never expire it mid-flight.

Further contract points:

- A capture that sees **zero speech frames** after opening (a wake-word tail and nothing else) is discarded before STT: no decode, no "Say again?", no retry spent, and the buffer is dropped at the capture layer (constraint 5).
- A window-opened capture never plays the wake acknowledgement — the prompt that armed the window *was* the acknowledgement.
- An inherited severity ("code orange" → next utterance) marks the continuation **severity-carrying but not framed**: it must still pass `relay_framed()` or parse as a command. Hallucinated noise can no longer mint a coloured card.
- Override continuations are confidence-gated (`stt.relay_min_logprob`) before any paid API call.
- Sessions are purged redundantly — IPC `left`, `on_voice_state_update` (authoritative; survives an Ears outage), and wholesale on every Ears `hello` — so no single restarting component can strand a wake-free window.

Config: the optional `dialog:` section (`window_ms`, `ack_grace_ms`, `endpoint_gap_floor_ms`, `max_retries`) — §16. The timing defaults are the values tuned on live fleet audio; expect them to move.

### 5.5 Live recognition — commit mid-sentence, not after the room goes quiet

The endpointing in §5.4 has one structural cost: a capture is only decoded **after it closes**, on a silence gap or the hard cap. When a pilot issues an order and *keeps talking* — to their fleet, over someone else's callout, in a channel that never falls silent — the capture runs to the hard cap before the first decode, and the order lands 10–20 seconds late. That is the single worst latency in the system, and it is exactly the moment latency matters most.

**Live recognition (`capture.streaming`) decodes the buffer *while the pilot is still talking* and commits the instant the command is complete.** The dialog wheel (§5.4), already walking every open capture each 100 ms tick, takes a RAM snapshot of the buffer-so-far (`CaptureManager.capture_progress`) and runs an incremental Whisper decode. The moment that partial transcript parses a **complete, confident command** — a recognised intent, plus a resolvable system name for the intents that need one (`_early_committable`) — the wheel ends the capture then and there. The pilot can keep talking; the order is already in flight.

The design's safety comes from what the incremental decode is *not* allowed to do: **it never routes the command.** It only decides "the command is complete, stop capturing now" and calls the same `force_endpoint` the silence path uses. The **final decode on emit stays authoritative** — it re-parses, re-resolves, runs the pilot gate, the confirm-first flow, everything. So a mis-parsed partial can only end the capture a little early; it can never post the wrong card, mint the wrong `@here`, or bypass a gate. Constraint 6 is untouched — this is fixed-grammar parsing over a partial transcript, no model in the routing path.

Three guards keep it cheap and correct:

- **Rate-limited.** An incremental decode fires only after `capture.partial_min_speech_ms` of speech has accrued (a fragment can't carry a command) and then at most once per `capture.partial_decode_ms` of *new* speech, with **one decode in flight per user**. A busy channel cannot stampede the STT queue; overflow is dropped by the bounded queue (§20) and the normal endpoint still catches the command.
- **Confidence-gated.** Below `capture.early_commit_min_logprob` the partial keeps listening rather than clipping the pilot on a half-heard word; uncertainty is owned by the final decode's confirm-first flow (§8.3), not resolved by guessing early.
- **Generation-guarded.** The early-commit `force_endpoint` carries the capture generation the snapshot was taken from; if the pilot endpointed and re-woke while the partial decode was in flight, the guard makes it a no-op — a stale decision can never end a *different* capture.

This is the half of "live, flexible recognition" that the §6.1 vocabulary and the §8.3 confirm-first readback complete: she hears the order as it's spoken, reads the situation back naturally, and takes a flexible *yes* — all without the pilot having to stop and wait. It costs real CPU (each partial is a genuine decode), so it is sized for a dedicated ≥4-vCPU host and turns off cleanly (`capture.streaming: false`) on a 2-vCPU box, falling back to decode-on-endpoint. Config: the `capture.streaming` / `partial_decode_ms` / `partial_min_speech_ms` / `early_commit_min_logprob` keys — §16.

---

## 6. Voice command reference

The grammar is **fixed and rigid**. No LLM sits in this loop — it would be slower, cost money per fight, hallucinate system names, and fail in ways nobody can debug at 02:00. A fixed grammar is deterministic, instant, and free.

```
<wake> <intent> [<system>] [<group>] [<detail>...]
```

### 6.1 Intents

| Spoken | Intent | Severity | Default notification |
|---|---|---|---|
| "hostiles \<system\>" / "reds \<system\>" / "neuts \<system\>" / "enemies \<system\>" / "war target(s) \<system\>" / "gankers \<system\>" | `HOSTILE_SPOTTED` | medium | role mention |
| "under attack \<system\>" / "tackled \<system\>" / "bubbled \<system\>" / "scrambled\|scrammed \<system\>" / "pointed\|pinned \<system\>" / "webbed \<system\>" / "jammed\|neuted \<system\>" / "engaged \<system\>" / "taking fire \<system\>" / "point on me \<system\>" | `UNDER_ATTACK` | **high** | role mention + `@here` |
| "need help \<system\>" / "help me \<system\>" / "send help\|backup\|reinforcements\|the fleet \<system\>" / "request(ing) [heavy] assistance\|backup\|reinforcements\|support \<system\>" | `ASSIST_REQUEST` | **high** | role mention + `@here` |
| "gate camp \<system\>" | `GATE_CAMP` | medium | role mention |
| "clear \<system\>" | `RESOLVE` | none | edits card, no mention |
| "timer \<system\> \<duration\>" | `TIMER` | none | schedules a future ping |
| "form up \<system\> \<duration\>" | `FORMUP` | none | posts op with RSVP |
| "status" | `QUERY` | none | spoken reply only |
| "help" | `HELP` | none | speaks *"Command list posted to Discord."* and posts the `/help` front page to the intel channel |
| "cancel" | `CANCEL` | none | kills this user's last incident (30s window) |
| "register \<callsign\>" / "call me \<callsign\>" | `REGISTER` | none | spoken reply only |
| "unregister" / "unregister me" / "forget me" | `UNREGISTER` | none | spoken reply only |
| "who am I" / "whoami" | `WHOAMI` | none | spoken reply only |
| "ping me [for \<types\>] [in \<system\>]" | `PING_ME` | none | spoken reply only |
| "stop pinging me" | `PING_ME_CLEAR` | none | spoken reply only |

Higher-severity patterns are matched first, so *"tackled, need help in Kisogo"* resolves to `UNDER_ATTACK`, not a sighting. The personal-ping intents are the one exception: their utterances *contain* type words ("ping me for gate camps"), so `PING_ME`/`PING_ME_CLEAR` are matched before the type words can claim the utterance — a genuine distress call never contains "ping me".

**Padding tolerance.** Stressed pilots narrate: *"please report that I am tackled by enemies in system M tack O and request heavy assistance please"* must parse exactly like *"tackled M-OEE8"* (and it does: `UNDER_ATTACK` — tackled outranks the assist phrasing — with the spelled system intact). Two mechanisms, still fixed grammar (constraint 6): intent keywords are matched on the **raw text first**, so no courtesy word can ever break recognition; and the **system window** is then cleaned aggressively — a finite courtesy vocabulary (*please, kindly, that, by enemies/hostiles, request(ing), heavy, immediate(ly), send, right now, …* plus the phrase forms *I am / we are / I've been / be advised / thank you*) is stripped, and when the pilot anchors the name with a preposition (*"in system X"*, *"in X"*, *"at X"*), everything before the **last** anchor is treated as narrative and discarded. The stripping applies to the system window only — `detail` stays verbatim (§6.3) and callsigns keep their words. The article *"a"* survives inside a spelling (*"one d q one tack a"* → 1DQ1-A, §8.2) but is stripped everywhere else. Note *assistance/backup/reinforcements/support* are `ASSIST_REQUEST` **type words** when prefixed by *need/request* — they are matched (and removed from the window) as whole intent phrases before the courtesy set touches anything.

**Situation vocabulary.** Pilots describe the *same* situation a dozen ways and cannot enumerate them up front, so the distress pattern covers the EWAR/tackle verbs that all mean "I'm in a fight, come now" — *bubbled, scrambled/scrammed, pointed, pinned, webbed, jammed, neuted, engaged, taking fire, being/getting attacked* — whether the pilot is the one held (*"I'm scrambled"*) or holding tackle on a target (*"I've got them webbed"*). Either reading calls for the same fleet response, so both land on `UNDER_ATTACK`; all of them outrank the sighting nouns, so *"scrambled by reds"* is a distress call, never demoted. The sighting set likewise widens beyond *hostiles/reds/neuts* to *enemies/enemy*, *war target(s)* (EVE's term for a shootable pilot), and *gankers/bad guys*; and the assist set adds *help me* and *send help/backup/the fleet/the cavalry* alongside the *need/request* phrasings (a lone *"help"* still reaches the `/help` manual — only *"help me"* escalates). This is still fixed grammar (constraint 6): a small, auditable table, extended from the STT transcript log (§8.7) when a real phrasing slips through — never a model guessing intent. `STT_VOCAB_BIAS` carries the new words so Whisper decodes them.

`PING_ME` (§10.3) reuses the type vocabulary above: *hostiles/reds/neuts* → `HOSTILE_SPOTTED`, *gate camp(s)* → `GATE_CAMP`, *under attack/attacks/tackled* → `UNDER_ATTACK`, *need help/need backup/assist request(s)* → `ASSIST_REQUEST`, and *"anything"/"everything"/"all"* (or no type word at all) → all four. The optional system window resolves through the same phonetic pipeline as reports (§8.2); anything below HIGH tier is treated as unresolved — a subscription silently scoped to the wrong system would never fire, so CORTANA answers *"Say again the system."* instead of guessing. No system means the subscription covers all systems. The recognised types travel to the engine encoded in `detail` (comma-separated `Intent` values), shared by the `/pingme` twin.

The callsign commands are a **name registry keyed on the Discord user id** Ears already attaches to every utterance (the SSRC→user map, §15). There are no voice biometrics and nothing derived from the audio — identity comes from Discord, the callsign is just a display name (§19 posture unchanged). The spoken callsign is the cleaned post-intent remainder: filler stripped, title-cased, markdown/mention characters removed, capped at 32 characters. `/register` (the typed twin) stores the callsign exactly as typed, which is also how a pilot fixes an STT misspelling.

### 6.2 Group targeting

| Spoken suffix | Effect |
|---|---|
| "miners only" | Routes to `@Miners` alone |
| "defense only" | Routes to `@Home-Defense` alone |
| "all hands" | Every subscribed role, regardless of severity — and it *requests* `@here`, granted only when the report is `UNDER_ATTACK` or `ASSIST_REQUEST` (constraint 11; the request passes the same `decide_mentions()` clamp as everything else, §11.1) |

Group aliases are configurable but deliberately few. Every alias added is another token the recogniser can confuse with a system name. Four is the recommended ceiling.

### 6.3 Detail

Anything after the system and group is captured verbatim into the incident body: *"three battleships"*, *"camping the gate"*, *"I'm in structure"*. It does not parse. It is a human note for humans.

### 6.4 Spoken threat colours

The card labels (CODE RED / CODE ORANGE / CODE YELLOW, §9.1) are also **input**: a pilot can speak the colour to set or override severity.

A colour code changes what the card **says** — its severity, colour, and readback. Whether anything gets **pinged** is decided separately, by exactly one function: `decide_mentions()` (`brain/cortana/core/routing.py`), the single escalation authority every mention flows through (§11.1). Constraint 11 always wins there: `@here` fires **only** for `UNDER_ATTACK` and `ASSIST_REQUEST`. A colour code can *request* an `@here` (via `discord.here_on_severity`, §16), but the request is clamped to those two types — a severity code on a sighting, and a freeform relay of any colour, can never `@here`.

- **Inline** — *"code red, hostiles in UMI"*: the report parses normally and carries `severity=high`; the card renders CODE RED and subscribed roles are mentioned per the routing rules — but a sighting is not an escalatable type, so no `@here`. On an under-attack or help-me report, `high ∈ here_on_severity` fires the colour-based `@here` even with zero routing rules wired up, and it always lands in `#intel-alerts` (the channel is recomputed after every escalation decision — `@here` can never fire into `#intel-live`). The colour phrase is stripped before intent matching, so *"code red"* can never be misread as a "reds" sighting.
- **Standalone (dialogue)** — *"code orange"* alone: CORTANA replies *"Code orange. Go ahead."*, reopens a wake-free capture window (the §8.3 mechanism), and the pilot's next utterance — report or freeform relay — inherits the severity. An inline colour on the follow-up wins over the opener.
- **Relays** — a colour on a freeform relay colours the relay card **only**: *"code red, blop fleet inbound"* posts a red relay card but never pings by colour (constraint 11 — a relay is not an incident type). An *"all hands"* phrase mentions every subscribed role (gated on the speaker's @Pilot standing), never `@here`. A mention-free relay posts to `#intel-live`, the quiet feed (§11.2).
- **Folds** — a colour spoken on a duplicate report **raises (never lowers)** the live card's severity on the fold re-render, so a spoken escalation is never silently dropped; the fold's no-re-mention rule (§9.2) is untouched — severity display and mention policy are separable.

Mapping: red → high, orange → medium, yellow → none/info. A leading *"report"* and a trailing *"end report"* / *"end of report"* / *"end transmission"* are radio-procedure framing and are stripped (*"report, I've been tackled in UMI, end report"*).

### 6.5 Examples

```
"Hey Cortana, hostiles Otanuomi, three battleships"
"Hey Cortana, tackled in Kisogo, need help"
"Hey Cortana, gate camp Otanuomi, miners only"
"Hey Cortana, clear Otanuomi"
"Hey Cortana, timer Kisogo four hours"
"Hey Cortana, form up Otanuomi fifteen minutes"
"Hey Cortana, status"
"Hey Cortana, help"
"Hey Cortana, cancel"
"Hey Cortana, register Space Junkie"
"Hey Cortana, ping me for gate camps in Otanuomi"
```

Fifteen commands. Short enough that pilots remember them under fire, which is the only time they matter.

### 6.6 The command override — out-of-band assistant

*"Command override, what's the weather in Chicago?"*

An **explicitly-invoked** chat channel, off by default (`chat.enabled`). When a pilot opens an utterance with *"command override"* (after the wake word), everything that follows goes to a language model — general questions, banter, live facts — and the answer is spoken back (or posted to the intel channel when it exceeds the §12.2 cap, with *"Answer posted to Discord."* spoken instead). Slash twin: `/ask` (constraint 10), sharing the same client and the same per-pilot cooldown.

**Two backends, one contract (`chat.backend`).** Both satisfy the same `ChatBackend` protocol — one `ask()` — so the dialog engine and `/ask` never care which is live:

- **`anthropic`** (default) — a cloud Claude model, with one optional live web search per question (weather, prices). Costs per question; needs a key.
- **`local`** — an **on-box** OpenAI-compatible server (llama.cpp's `server`, Ollama, …) at `chat.local_url`. This is the **SLM lane**: conversational back-and-forth that runs entirely on the droplet — no API, no key, no per-question cost, no network egress. The pilot wanted CORTANA to "talk back and forth" without paying an API; this is that, on the same hardware the bot already runs on. Deploying the model + server (choosing a small quantised model that fits alongside Whisper and Piper in the box's RAM) is an operator step — CORTANA points at the URL, it does not host the model. No web search (a local model has no such tool).

**Constraint 6 is untouched, either way.** The incident grammar never sees an LLM: the override prefix is matched *first* and only in leading position, so a report containing the word "override" mid-sentence can never be diverted, and a non-override utterance never reaches the model. Both backends are instructed to never invent in-game intel — hostiles, timers, and system status come only from CORTANA's own reports. The SLM is a conversation partner, never an interpreter of reports.

**Cost posture.** For `anthropic`, the default model is the cheapest Claude tier (fractions of a cent per question); replies are capped at `chat.max_tokens`, web search at one per question, and `chat.user_cooldown_s` throttles each pilot. For `local`, there is no per-question cost — but the cooldown still applies (it also paces the box's CPU, shared with Whisper). Either way the cooldown arms only on a successful answer — a failed request must not turn the pilot's retry into a throttle message. The Claude key rides systemd `LoadCredential=` (`anthropic:` credential; constraint 12), with `chat.api_key_file` as the 0600 dev fallback; the local backend needs no credential.

**Liveness.** The whole `chat:` section applies on SIGHUP — flipping `chat.enabled`, switching `chat.backend`, or dropping a key into place takes effect on `systemctl reload cortana-brain`, no restart. A spoken "command override …" while the channel is down always gets the fixed *"Override channel unavailable."* line (never a silent fall-through to the grammar), and `/ask` distinguishes the down states: "not enabled", "no key" (anthropic), and "no url" (local).

### 6.7 The understanding brain — an LLM in the command path (opt-in)

The fixed grammar (§6.1) is fast, debuggable, and free — but it only knows the phrasings it was written for. Pilots don't talk in a fixed table (*"we got company rolling into Otanuomi"*, *"someone's melting me in Taisy get over here"*), and the corp's whole reason for existing is to catch **those** callouts, not just the textbook ones.

`nlu.understanding` (OFF by default) adds an **on-box model as the understanding layer**: Whisper turns the voice into text, and when the grammar produced no command, the model reads that text and returns one structured command as JSON — intent, place, detail, severity. It runs on the same local server as the SLM (`nlu.url`, an Ollama/llama.cpp endpoint); no cloud, no key, no per-callout cost.

**This is a deliberate, operator-authorised relaxation of the old constraint-6 ("no LLM in the command path").** That rule was written for a bot that had to be instant and could not risk a hallucinated system name mid-fight. Two design facts retire both concerns without trusting the model:

- **The model never chooses a system.** It returns the place as a *string*; the deterministic resolver (§8.2) maps that string to a real seeded system or a learned area (§8.5a), or to nothing. A model cannot invent a system that isn't in the map, and a garbled place simply falls to the confirm/learn flow like any other.
- **Nothing pings until the pilot confirms.** The LLM's command flows through the exact same confirm-first (§8.3) / learn-a-word (§8.5a) path as a grammar command, so a misunderstanding is read back out loud and vetoed before any alert fires. Escalation is still `decide_mentions()` alone (§11.1) — the LLM adds no mention authority.

And it stays cheap where it matters: **grammar-first.** The model is called *only* when the fast grammar found no command — and never for a confirm reply, an override, a dismissal, or chatter-quality noise (the garbage gate). Clear callouts stay instant; only the messy ones pay the round-trip, capped at `nlu.timeout_s`. A model that is slow, down, or returns junk degrades silently to grammar-only — it can never crash an utterance or block a distress call. Latency is acceptable because accuracy is the point (the corp explicitly traded a few seconds for "understand me the first time"). Config: the optional `nlu:` section (`understanding`, `url`, `model`, `timeout_s`).

**She says "stand by" before she thinks.** The on-box interpret is a multi-second round-trip, and a pilot who hears nothing assumes she froze — the exact "randomly slow" feeling, since only grammar-*misses* pay it. So the instant the utterance is handed to the model, CORTANA speaks an ephemeral cue (*"Stand by." / "One moment."*, personality-flavoured) — best-effort ACK-class, dropped if she can't speak, and it never delays the interpret. The wait then reads as *working*, not *broken*. The decision to consult the model lives in one pure predicate (`_llm_understand_applies`) so "is she about to think?" has a single home shared by the cue and the call.

---

## 7. Slash command reference

Full parity. Every voice command routes to the same engine.

| Command | Purpose |
|---|---|
| `/hostiles system detail [code] [audience]` | Report a sighting |
| `/under-attack system detail [code] [audience]` | You are under attack — tackled or taking damage |
| `/help-me system detail [code] [audience]` | High-severity assist request |
| `/camp system detail [code] [audience]` | Report a gate camp |
| `/relay message [code] [audience]` | Freeform intel relay, posted verbatim — twin of the voice relay (§8.6); never `@here` |
| `/clear system` | Resolve an incident |
| `/status` | Active incidents summary |
| `/help [topic]` | Interactive help: main page + topic pages (reporting, responding, subscriptions, identity, ops, privacy, admin) via a select menu; twin of voice "help" |
| `/cancel` | Retract your own last report (30s window) |
| `/timer system duration note` | Schedule a structure timer ping |
| `/formup system when note` | Post an op with RSVP |
| `/rollcall` | Who's in voice, subscribed, responding |
| `/jumps from to` | Jump distance between systems |
| `/evetime` | Current EVE time (UTC) with a local-time hint |
| `/route from to` | Full shortest jump path between two systems |
| `/history system hours` | Recent incidents in a system (default 24h, max 72h) |
| `/remindme duration message` | Personal reminder, DMed when due (max 10 pending, 7 days out) |
| `/poll create question options…` | Quick vote with buttons, live counts edited in place |
| `/poll close id` | Close a poll (author or admin) |
| `/subscribe` | Self-service role picker |
| `/mysubs` | Show my subscriptions (roles + personal pings) |
| `/pingme type [system]` | Personal ping: mention me on matching incidents (§10.3) |
| `/mypings` | List my personal pings, ephemeral |
| `/pingme-clear [index]` | Remove my personal pings — all, or one by `/mypings` index |
| `/optout` | Exclude my audio from CORTANA entirely |
| `/mute-voice` | Stop CORTANA speaking to me |
| `/register callsign` | Register my pilot callsign, exactly as typed (fixes STT misspellings) |
| `/unregister` | Delete my registered callsign |
| `/whoami` | Show my registered callsign |
| `/routing` | *(admin)* Manage subscription rules | `code: red\|orange\|yellow` and `audience: miners\|defense\|all-hands` parameters on the report commands and `/relay` are the typed twins of the spoken colour codes (§6.4) and group aliases (§6.2) — they map onto the exact `ParsedCommand.severity` / `ParsedCommand.group_alias` fields the voice grammar fills, so severity and group targeting survive a voice outage too (constraint 10).
| `/gazetteer` | *(admin)* Reload / inspect / prune systems |
| `/fleetmode` | *(admin)* Restrict voice triggering to FC role |
| `/health` | *(admin)* Pipeline status, STT confidence, incident counts |
| `/areas-list` | Show the custom areas CORTANA has learned (§8.5a) |
| `/areas-forget word` | *(admin)* Forget a learned custom area |
| `/areas-add word` | *(admin)* Pre-seed a custom area without saying it on comms |

The optional `code: red|orange|yellow` and `audience: miners|defense|all-hands` parameters on the four report commands and `/relay` are the typed twins of the spoken colour codes (§6.4) and group aliases (§6.2) — they map onto the exact `ParsedCommand.severity` / `ParsedCommand.group_alias` fields the voice grammar fills, so severity and group targeting survive a voice outage too (constraint 10).

---

## 8. System name resolution

This subsystem decides whether CORTANA succeeds or fails. It is specified in the most detail because it deserves it.

EVE system names are phonetically hostile — *Otanuomi*, *Kisogo*, *Alenia*, *Hulmate*, *Tannolen*. Generic English STT shreds them. And **naming the wrong system is worse than silence**: it sends the response fleet twelve jumps the wrong way while the reporter dies.

### 8.1 The gazetteer: seed wide, scope at runtime

Two layers. The **seed** fills the `systems` + `system_adjacency` tables from
the EVE static data export (Echoes uses New Eden names); the **scope rules** in
`gazetteer.yaml` pick, at runtime, which of those systems are *active* — the
set CORTANA will match a transcript against. The seed is wide on purpose so the
scope can point anywhere without re-seeding; accuracy comes from the scope, not
the seed.

**Seeding — `python -m cortana.nlu.seed`.** An operator CLI (`aura/nlu/seed.py`)
downloads the three Fuzzwork SDE CSVs (`mapSolarSystems`, `mapSolarSystemJumps`,
`mapRegions`), joins each system to its region name, and loads **all of k-space
New Eden** (~5000 systems) plus the jump graph. Wormhole/abyssal space
(regionID ≥ 11000000) is dropped unless `--include-wormholes`. The reload is one
atomic transaction and is idempotent, so re-running against a fresh SDE is safe.
It runs standalone under the Brain venv — it does not require the service up.

**Scoping — two modes in `gazetteer.yaml`, editable by the FC without touching
code, because corps move.**

- **Scoped mode (default, recommended for home-region corps).** The active set
  is the corp's operational area: home region(s), adjacent regions, everything
  within *N* jumps of home, and the trade hubs pilots name anyway (Jita, Amarr,
  Rens, Dodixie). That is roughly **100–500 systems, not thousands**. Matching
  against a 300-entry gazetteer is a categorically different problem from
  matching against 5,000 — for a corp with a fixed footprint this single
  decision buys more accuracy than any model upgrade at any price. This is the
  core accuracy decision, and it stays the default.

- **`include_all` mode (nomadic corps).** Set `include_all: true` and the
  **entire seeded (k-space) map is active** — any system in New Eden resolves.
  This is a first-class, supported mode: some corps have **no fixed home** and
  relocate periodically, and for them a scoped gazetteer that must be re-cut on
  every move is worse than a wide one. `regions`/`within_jumps_of` are ignored
  in this mode; `exclude` still removes (e.g. a phonetic collision the corp
  never visits), and `always_include` is a no-op.

  **The tradeoff is real and stated honestly.** A 5,000-entry candidate set has
  more near-homophones than a 300-entry one, so raw first-pass accuracy is
  lower than a tight scoped gazetteer. CORTANA leans on the rest of §8 to close the
  gap rather than on a narrow set:

  - the **confidence tiers and confirm-flow** (§8.3) — CORTANA never silently
    guesses; a MEDIUM match posts flagged with `[Wrong — fix]` and a spoken
    "say again to confirm",
  - the **context priors** (§8.4) — recency, proximity, and reporter-history
    still cluster a fleet fight spatially and temporally even without a home
    anchor,
  - **alias learning** (§8.5) — within a month the corp's specific accents and
    mics are baked in, which matters *more* at 5,000 systems, not less.

  Home bias (§8.4) is inactive in the nomadic case: `gazetteer.home_system`
  may be `null`, and the home-bias prior simply does not fire.

Either way the jump graph (`Gazetteer.jumps`/`path`, §13) always runs over the
full seeded adjacency — pruning decides what can be *named*, never how space is
shaped.

**Two-tier resolution — the reliability safety net (`matching.full_map_fallback`, default on).** Scoping is an accuracy decision, not a hard boundary: a corp with a small scope, or one that roams a jump beyond it, must still be able to report *any* real system. So resolution runs in two tiers. **Tier 1** scores the utterance against the scoped active set *with* the context priors (§8.4) — the home-region-accurate path. **Tier 2** engages only when Tier 1 produces no confident match (LOW): the same utterance is re-scored against the **entire seeded k-space map** (metaphones for all ~5,000 systems are already computed at load, so this costs nothing until it fires), *without* the home/proximity priors (they don't apply out of region), and a MEDIUM full-map hit rides the confirm-flow. An in-region corp never pays for Tier 2; a distant or roaming report resolves instead of dropping to "unknown". This is the fix for the field report where a manual report offered only the ~8 scoped systems — **typed/slash input and autocomplete resolve over the full seeded map directly** (`Gazetteer.by_name_any` / `all_systems`), and **learned aliases resolve over the full map too** (`entry_any`), so a pilot's correction is never vetoed by scope. Set `full_map_fallback: false` to restore strict scoped-only matching.

### 8.2 Resolution pipeline

```
raw transcript
   │
   ├─► alias table lookup ────────────► exact hit? done.       [learned, §8.5]
   │
   ├─► normalise (lowercase, strip filler, expand numerals,
   │              fuse spelled names — "m tack o double e 8" → "m-oee8")
   │
   ├─► sliding 1–3 token windows over the remainder
   │
   ├─► for each window × each gazetteer entry:
   │       phonetic_sim  = 1 − levenshtein(dmetaphone(a), dmetaphone(b)) / len
   │       text_sim      = 1 − levenshtein(a, b) / len
   │       base          = 0.6 · phonetic_sim + 0.4 · text_sim
   │
   ├─► top-8 by base score → context prior (§8.4):
   │       score = base × prior
   │
   └─► top-3 candidates → confidence tier (§8.3)
```

Levenshtein on raw text alone is the wrong tool: STT errors are **phonetic, not typographic**. Whisper writes *"oh tan you oh me"* — character-distance-far from *Otanuomi*, phonetically adjacent. Metaphone collapses that gap.

**The "tack" convention — spelled system names.** EVE pilots speak the hyphen in a nullsec designation as *"tack"* (also *"dash"*, *"hyphen"*) and spell the characters: M-OEE8 is said *"m tack o double e 8"*, UMI-KK is *"u m i tack k k"*, 1DQ1-A is *"one d q one tack a"*. The normalise step folds these spellings before windowing: hyphen words become `-`, adjacent single letters/digits (and spoken digit words inside a spelling) fuse into one token, and *"double e"* / *"triple x"* expand to `ee` / `xxx`. So *"m tack o double e 8"* becomes the single token `m-oee8` and scores as an exact hit against `M-OEE8`. A bare pair of numerals (*"4 4"*) is not a spelling and keeps the standalone-numeral expansion.

Hyphenated names are also matched by their **spoken short form** — the pre-hyphen prefix (*"UMI"* → UMI-KK, *"Moe 8"* → MOEE-8), promoted only when the abbreviation match is strong (≥ 0.78). Names with a **1-character head** (M-OEE8) have no speakable short form, so they get **unique-prefix promotion** instead: a collapsed spoken form (*"mo"*, *"moee"*) that is a prefix of **exactly one** active-set collapsed name promotes that name to a strong match (0.90 — HIGH-tier on its own; a deliberate spelling is not a fuzzy hearing). A prefix shared by several active names is ambiguous and never promotes — the confirm-flow (§8.3) owns that case, not a guess. Both promotions matter more in `include_all` mode (§8.1), where the wider set makes full-name scores drag.

### 8.3 Confidence tiers — CORTANA never silently guesses

| Tier | Rule | Behaviour |
|---|---|---|
| **High** | `top1 ≥ 0.80` and `top1 − top2 ≥ 0.12` | Post immediately. Speak *"Hostiles Otanuomi, pinged."* |
| **Medium** | `top1 ≥ 0.55` | **Post anyway**, flagged uncertain, with buttons `[Otanuomi] [Kisogo] [Wrong — fix]`. Speak *"Hostiles Otanuomi — say again to confirm."* and arm the wake-free confirm window (below). Speed beats certainty when a pilot is in structure; get the ping out and let humans correct it. |
| **Low** | below | **Confirm-first** (`dialog.confirm_reports: low`, the default): read the *situation* back naturally — *"Under attack in Otanuomi, by two cruisers. Confirm?"* (the intent, the system, and a short verbatim detail; a long detail is dropped from speech but kept on the card) — and hold the report in the confirm window. Naming the intent, not just the system, makes a misheard **intent** as catchable as a misheard system. Intents without a readback template fall back to *"Heard \<name\>. Confirm?"*. *Yes* — **or silence, or unmatched speech** — posts it with the heard name verbatim (§8.6: a distress call is never lost to an unanswered question); *no* opens the say-again retry that re-binds a bare system name; a dismissal retracts it. With `confirm_reports: off`, posts verbatim immediately (readback only). `always` extends the ask to HIGH-tier reports too. |

**Destructive and scheduling commands need HIGH.** The tier table above governs *reports*, which post-anyway because speed beats certainty. `clear`, `timer`, and `form up` act irreversibly on a *specific* system — resolving the wrong system's incidents or scheduling a rally in the wrong place has no undo — so they act only on a High-tier match. A Medium match answers *"Heard Otanuomi — say again to confirm."* (the same ASKED outcome as an uncertain report) and holds the command pending in the confirm window. Low still gets *"Say again the system."*

**The confirm completes by voice, end to end.** Every ASKED outcome parks the dialog in `AWAIT_CONFIRM` — a wake-free window armed through the machine's one budgeted door (§5.4), carrying the pending command and the heard candidate in the session. Into that window:

- **An affirmative** — *"yes"*, *"confirm"*, *"affirmative"*, *"correct"*, *"do it"*, *"roger"* — completes the confirm. Affirmatives are short — exactly what Whisper hallucinates from noise — so they are confidence-gated like override continuations.
- **An exact repeat** — the same intent naming the same system (the heard window or the candidate's real name), or the bare system name alone — also completes it: "say again to confirm" taken literally.
- **A negative** — *"no"*, *"cancel"*, *"wrong"* — declines. What happens next depends on which confirm this is: a **card/destructive confirm** (the command already posted, or acts irreversibly) closes silently — it fails closed, never guessed from an unmatched utterance. A **confirm-first report** (`dialog.confirm_reports` — the report is NOT posted yet) instead opens the say-again retry so the pilot can re-say the system; and for it, **timeout or unmatched speech commits the report anyway** — only an explicit decline or dismissal retracts it, because losing a distress call to an unanswered question is the worst failure (§8.6). Even a spent retry budget commits rather than standing down over an unposted report.

Completion runs the same engine path as the buttons (constraint 10, both directions). For an uncertain *report*, the confirm pins the already-posted card's heard candidate through the pick-button path (`confirm_system` → `correct_system`), so the alias table still learns (§8.5) — a confirmed hearing is as much signal as a corrected one. For `clear`/`timer`/`form up`, nothing was posted: the stored candidate re-enters `report()` as a ready-made HIGH-tier resolution, exactly as if the repeat had been heard cleanly. The `/`-command twins never need this flow — typed input is exact.

**Spoken readback.** When the pilot spoke a colour code, the confirmation reads the report back — *"Under attack UMI, code red, posted."* — so they hear exactly what the card says without looking at Discord. Combined with the catch-all posture (§8.6) this is the contract for misheard systems: **the action always continues**; the card carries the heard name verbatim, the readback surfaces it, and *"cancel"* (30s) or **[Wrong — fix]** repairs it.

### 8.4 Context priors

Candidates are reweighted by what is plausible *right now*. A fleet fight is spatially and temporally clustered — CORTANA exploits that.

| Prior | Rule |
|---|---|
| **Recency** | Systems with incidents in the last 10 minutes get a strong boost. If three pilots just pinged Kisogo, *"kissogo"* is Kisogo. |
| **Proximity** | Systems within a few jumps of an active incident outrank systems forty jumps away. |
| **Reporter history** | This pilot has reported from Otanuomi six times this week. That is a prior. |
| **Home bias** | Home and adjacent systems carry a standing boost. Inactive when `gazetteer.home_system` is `null` (nomadic corps, §8.1) — the prior simply does not fire. |

Applied as a cheap multiplicative reweighting over the top-8 base-score candidates — wider than the final top-3 on purpose, so a strong prior can promote a lower-ranked but spatially plausible candidate into the top-3. Weights live in `cortana.yaml`.

### 8.5 Alias learning — and FC-authored custom names

Two kinds of alias, both consulted **before** phonetic matching, both resolving at full confidence:

- **Learned aliases.** Every time a pilot taps `[Wrong — fix]` and picks the correct system, CORTANA writes `(raw transcript) → (system_id)` into the alias table. Within a month of real use, your corp's specific accents, specific mics, and specific noisy rooms are baked in. **This is the highest-leverage component in the entire system and it is roughly forty lines of code.**
- **Custom names (config aliases).** A corp calls places by its own words — a branch nickname, a staging name, "home", a region's foothold system. The `aliases:` map in `gazetteer.yaml` (`{"the branch": "M-OEE8", "tribute staging": "5ZXX-K"}`) is FC-authored and stable: a phrase → a real system name, resolved at full confidence before *both* the learned table and phonetics. Case-insensitive; the target may be any k-space system even one outside the active scope; a typo in a target is logged and skipped, never fatal. This is how the corp's actual comms vocabulary — not New Eden's official names — becomes what CORTANA understands. The two layers compose: config names are the deliberate vocabulary, learned aliases are what accrues from real corrections on top.

### 8.5a Custom areas — confirm-and-learn a place word

The two aliases above both point at a **system**. But corps also name places that aren't a single system — a branch, a staging, a whole region ("the pipe", "wildlands", "Tribute"). A custom **area** is the *systemless twin* of the learned-alias table: `phrase → display_name`, no system_id.

The flow is learn-by-talking, and it only fires for a genuinely unknown place:

1. A mention-report names a place that resolves to **no system** (LOW tier, and not already a known alias or area). Instead of committing an uncertain guess, CORTANA asks once: *"Did you say the branch?"*
2. **"Yes"** (confident) → she saves the word as a custom area, posts the report verbatim under it, and **never asks again** — every later report of that word resolves at HIGH confidence and posts directly. She built a piece of your map from one confirmation.
3. **"No, it's Kisogo"** → Kisogo is a real system, so she uses it and **throws the misheard word away** — a mishearing is never turned into a nickname for a real system (the corp owner's explicit rule). *"No, it's the pipe"* (another unknown place) re-enters the confirm flow for that word instead.
4. **Timeout, unmatched speech, or a low-confidence "yes"** → the report still posts verbatim, but **nothing is learned** — an unconfirmed word is not a place, and a distress call is never lost to the question (§8.6).

Guardrails: learning is **role-gated** to @Pilot (the `may_mention` gate — the same people who may report), **per-guild**, and **capped** (`areas.max_per_guild`, default 200; at the cap learning pauses but reports still post, until an FC prunes). The **garbage gate** (`dialog.retry_min_logprob`) excludes chatter-quality transcripts — a place you couldn't have said clearly is never offered up to be remembered. It is **text only** — the confirmed word, never audio or a voiceprint (constraint 5) — exactly the mechanism of the alias table, and it is **deterministic** (constraint 6 holds: the confirm is fixed `confirm_reply`/`correction_reply` grammar, the save is a table write, no model anywhere). Toggle with `areas.learn`; manage with `/areas-list`, `/areas-forget`, `/areas-add` (the FC pre-seed twin). A slash report of a learned word resolves through the same area fallback, so voice and typed reports land identically (constraint 10). One nuance: the grammar strips a leading article, so "the branch" is learned and matched as *branch* — consistently, on both the learning report and every later one.

### 8.6 Recognition error is a normal operating condition

Pilots use phone mics, in noisy rooms, with a game running, stressed and talking fast. CORTANA is engineered on the assumption that **the transcript is sometimes wrong** — not as a limitation to apologise for, but as a fact the design absorbs. The confirmation loop, the correction buttons, the confidence tiers, and the alias table are not garnish; they are the mechanism by which an imperfect signal produces a reliable tool.

**The freeform relay is framed by default.** `stt.relay_mode` decides what unmatched speech may become a relay card:

- **`framed`** (default) — only *explicitly framed* intel relays: a *"report …"* opener, a spoken colour code (inline, or inherited from a §6.4 code dialogue), or an all-hands phrase. Anything else that matched no intent gets a spoken *"Say again?"* and posts nothing. The reasoning: an unmatched, unframed transcript is far more likely an STT mishearing or channel crosstalk than intel, and every junk card costs the intel channel trust. Framing is one word of radio procedure the corp already uses.
- **`open`** — the old catch-all: any unmatched transcript relays.
- **`off`** — the freeform relay never posts; recognised commands only.

**The relay is also confidence-gated.** Whatever the mode, a relay posts only when Whisper's `avg_logprob` clears `stt.relay_min_logprob`. Below that, the transcript is treated as decoded noise ("Rens, Rens, Rens" hallucinated from silence) and CORTANA says *"Say again the system."* instead of posting garbage. Recognised commands are **never** gated — a distress call always posts. Stuttered three-plus word repeats in relay text collapse to one word, and every relay logs its confidence to `command_log` so the threshold is tuned from data.

**Relays dedupe like incidents.** Identical relay text (case-insensitive) within `incidents.dedupe_window_s` folds — the pilot hears *"Relayed."* again, but no second card posts. Pilots repeat when they miss the ack; a repeat is not fresh intel. A successful relay is acknowledged with a spoken *"Relayed."* — without the ack, pilots repeat themselves, and every repeat is another card and another STT decode.

### 8.7 The STT transcript log — the phrasing-analysis surface

Recognition is only as good as the vocabulary, and the vocabulary only grows from seeing what pilots *actually say* — including the phrasings that match nothing yet. The JSON journal records every `utterance_transcribed`, but it is 100k lines of debug noise, not something a corp admin scans.

`discord.channels.transcript` (optional, `0` = off) is the human-readable surface for exactly this. When set, **every heard utterance posts one clean line** to that channel: what CORTANA thinks it heard, how the grammar parsed it, and the decoder confidence —

```
🎧 "tackled by two cruisers in taisy" → UNDER_ATTACK · sys "taisy" · -0.21
🎧 "we're getting bubbled on the gate" → UNDER_ATTACK · -0.34
🎧 "they've got us scrammed"           → no command · -0.52
```

Unmatched utterances are logged too — that is the point: the *"no command"* lines are the backlog of phrasings the fixed grammar (§6.1) should learn next. An admin skims the channel after a fight, spots the misses, and the situation vocabulary is extended from real audio instead of guesswork. The line is a transcript only — no audio, no user id (constraint 5) — and the post is fire-and-forget, so it never sits between hearing and acting. Off by default; a corp that wants it points it at a private channel.

---

## 9. Incident engine

CORTANA is not a ping bot. It is an **incident tracker that happens to be voice-driven**. This is the difference between a toy and something a corp runs for years.

```
Incident
├─ id
├─ guild_id
├─ system_id          + system_confidence
├─ type               HOSTILE_SPOTTED | UNDER_ATTACK | GATE_CAMP | ...
├─ severity           none | medium | high
├─ reporter_id
├─ detail             "three battleships"
├─ opened_at / updated_at
├─ status             ACTIVE | STALE | RESOLVED
├─ message_id         the Discord message that IS this incident's live view
├─ updates[]          folded-in subsequent reports
└─ responders[]       user_id → OTW | WATCHING | NO
```

### 9.1 The card is a view, not a log

**The Discord message is edited in place.** It is not an append-only stream of pings. This is the core architectural choice of the engine:

- Five pilots reporting the same gate camp produce **one** card reading *"reported by 5"* — not five pings.
- *"Hey Cortana, clear Otanuomi"* edits that card to ✅ **RESOLVED** and greys it out.
- Someone scrolling back reads **state**, not archaeology.
- No updates for 20 minutes → auto-marked **STALE**, silently. Form-ups are exempt: a rally card legitimately sits quiet until it fires — its staleness is anchored by its countdown timer (§13), not by update chatter.
- No updates for `incidents.auto_resolve_min` (default 60) → auto-**RESOLVED**, silently, in place — STALE is the waypoint, this is the terminus (field request: cards sat open forever unless someone cleared them). Timers *and* form-ups are exempt here — a four-hour structure timer must not vanish at the one-hour mark. `0` disables: cards then live until `/clear`, the card button, or `/clearall`.

**Render-then-deliver.** Every engine method mutates the database and renders the card *under* the engine lock, then hands the Discord I/O to a single deliverer that runs *after* the lock is released. Discord can sleep on rate-limit buckets mid-call; under flood conditions a rate-limited edit must never block the 3-second button-interaction window. One failure policy covers every path: a fresh card's **post** failure raises `PostError` and rolls the incident row (and any discipline charge) back; an **edit** is best-effort — the DB row is the state — except for a lost message.

**Lost-message recovery.** The invariant is one **LIVE** message per incident, not one message id forever. A fold, responder press, correction, chase, resolve, or cancel that hits a NULL message id or a deleted message (`EditNotFound`) **re-posts the card and stores the new ids** — mention-free, pinned to the original channel (severity fallback when no channel was ever recorded) — instead of silently editing a ghost. The bulk sweeps (`sweep_stale`, `/clearall`) are the deliberate exception: resurrecting a lost card just to grey it out is noise, so they never re-post.

**Uncertain cards survive restarts.** A MEDIUM-tier card's confirm candidates are persisted on the incident row (`pending_candidates`) and restored at startup, so a restart never re-renders a 0.55-confidence guess as a confirmed system; the column is cleared the moment the card is confirmed, corrected, retargeted, resolved, or swept.

### 9.2 Dedupe rule

> Same system + same type + within 90 seconds → **fold into the existing incident**, increment the reporter count, **do not re-mention**.

This one rule is the primary defence against notification fatigue.

A fold **raises but never lowers** severity: a spoken colour on the duplicate (*"code red, hostiles UMI"* folding into a plain sighting) turns the card red on the re-render instead of being silently discarded. Severity display and mention policy are separable — the escalated fold still re-mentions nobody.

### 9.3 Response loop

Every card carries:

`[🚀 On my way]` `[👀 Watching]` `[❌ Can't respond]`

On the first **On my way**, CORTANA speaks into voice — naming the responder when it can: the registered callsign wins, then the clicker's guild display name (*"Space Junkie responding to Otanuomi."*), and only when neither is known does it fall back to the count (*"Two responding to Otanuomi."*).

That closes the loop. A pilot in structure gets an audible answer without touching their phone. It is the cheapest feature in the document and the one the corp will actually love.

Buttons use persistent views with the incident ID encoded in `custom_id`, so they survive a Brain restart.

---

## 10. Routing and subscriptions

Subscriptions are built on **Discord roles**. Roles are native, respect per-user notification settings, are already understood by every member, and mean CORTANA does not reinvent a permission system.

### 10.1 Rule model

```yaml
- role: "@Home-Defense"
  types: [UNDER_ATTACK, ASSIST_REQUEST, HOSTILE_SPOTTED]
  scope:
    regions: [Kisogo-region]
    within_jumps_of: { system: Otanuomi, jumps: 5 }
  escalate_at: UNDER_ATTACK        # this type also fires @here
  quiet_hours: null

- role: "@Miners"
  types: [HOSTILE_SPOTTED, GATE_CAMP]
  scope:
    systems: [Otanuomi, Kisogo]
  escalate_at: never

- role: "@Roam-Crew"
  types: [HOSTILE_SPOTTED]
  scope:
    regions: [Lowsec-North]
  escalate_at: never
  quiet_hours: { tz: UTC, from: "02:00", to: "14:00" }
```

Routing = evaluate every rule against the incident → union the matching roles → mention once.

### 10.2 Self-service

`/subscribe` presents a role picker. `/mysubs` shows current subscriptions. Nobody needs an admin to opt into home defense.

### 10.3 Personal pings

*"Hey Cortana, ping me for gate camps in Otanuomi."* A personal ping is a **user mention, not a role** — the role model above is unchanged; personal pings are additive.

- Stored in `personal_pings` (§14): incident types + an optional system (`NULL` = all systems). Capped per user by `discipline.personal_pings_max` (default 10); at the cap CORTANA answers *"Ping limit reached."*
- Matching = incident type ∈ the subscription's types ∧ (no system, or the incident's system). Matching subscribers' mentions are **appended to the mention line** of the incident card in `#intel-alerts` — never a separate message.
- **Same discipline, no exceptions.** Personal pings ride the exact mention path of §11: the reporter's per-user cooldown, the circuit breaker, and quiet-hour role logic all suppress them together with the roles; a dedupe fold (§9.2) never re-pings personal subscribers; the incident's own reporter is never personally pinged for their own report; and they can **never** cause `@here` (constraint 11 untouched).
- Managed by voice (`PING_ME` / `PING_ME_CLEAR`, §6.1) and the slash twins `/pingme`, `/mypings`, `/pingme-clear` — all through the same engine entry point (constraint 10).

---

## 11. Notification discipline

If CORTANA is annoying for one week, the corp mutes `#intel-alerts` and the project is dead — no matter how good the speech recognition is. This subsystem gets as much engineering weight as the recogniser.

### 11.1 Layered defences

1. **Dedupe window** (§9.2) — one incident, one mention.
2. **Per-user cooldown** — 30s between mentions from the same pilot. A panicking player cannot ping six times. Charged only after a post actually succeeds — a failed post never runs a cooldown or feeds the breaker.
3. **Escalation discipline** — `@here` is reserved for `UNDER_ATTACK` and `ASSIST_REQUEST`. Sightings never `@here`. Severity codes never earn it for any other type (§6.4). Freeform relays never `@here`. Ever.
4. **Permission gate** — only members holding `@Pilot` can trigger a mention. The new guy cannot experiment at 03:00. The gate result is threaded all the way into the escalation decision, so no engine path (relay escalation included) can mint a mention the caller wasn't entitled to.
5. **Global circuit breaker** — more than *N* mentions in *M* minutes → CORTANA stops mentioning, posts **flood control active**, and keeps logging incidents silently. Something is wrong; do not amplify it.
6. **Quiet hours** per role.

**One authority.** Layers 3, 4, and 6 — plus group aliases, `here_on_severity`, silent mode (`mentions_enabled`), the personal-ping user list, and the channel choice — are computed in exactly one pure function: `decide_mentions()` in `brain/cortana/core/routing.py`. Every card post and every relay flows through it; the discipline layer (2, 5) may only *suppress* its decision, never widen it. The Poster then sends `AllowedMentions` as an explicit allowlist built verbatim from the decision — the listed role ids, the listed user ids (never `users=True`), and `everyone` only when the decision granted `@here`. Two properties are therefore structural, not guarded per-path: `@here` cannot occur outside the two escalatable types, and `@here` (or any mention) cannot occur in `#intel-live` — the channel is recomputed from the final mention set.

### 11.2 Two channels

| Channel | Contents |
|---|---|
| `#intel-live` | Everything that mentions nobody — incidents and freeform relays alike. The quiet feed, for people who want it. |
| `#intel-alerts` | Only cards that mention someone (a role, `@here`, or a personal-ping subscriber). |

Let people choose their own volume. An incident card lives in exactly one of
the two channels — one incident is one message, edited in place (§9.1), never
mirrored or reposted.

### 11.3 Health channel

`#bot-health` receives hourly self-reports: pipeline status, STT confidence distribution, incident counts, mention counts, wake-word false-accept estimate.

**Alarm cards.** Every "I dropped something the corp expects" event flows through the AlarmBus (`brain/cortana/alarms.py`) and appears in `#bot-health` as **one edited-in-place card per active `(code, key)`** — the incident-card invariant (§9.1) applied to operations. A card shows first-seen, last-seen, an occurrence count, and a phone-readable fix hint; repeats edit the card, and on recovery it is edited to a ✅ *resolved* state rather than deleted, so the operator sees that it broke *and* that it healed. Card message ids are persisted in `app_state` (key `alarm:<code>:<key>`), so a Brain restart re-adopts the existing card — restarts never duplicate cards, and an alarm raised before the restart (e.g. `CONFIG_RESTART_PENDING`) is resolved on the same card after it. Raising an alarm before Discord is ready is safe: the card queues dirty and is flushed from the health loop once the bot is up. Every raise/clear is mirrored to journald **code-first** — the structured log *event* is the alarm code, so `journalctl -u cortana-brain | grep EARS_DOWN` works.

The alarm codes are a **closed** enum — a failure class earns a code or it does not get raised:

| Code | Meaning | Raised by / cleared when |
|---|---|---|
| `ROUTING_ZERO_RULES` | Zero routing rules active (guild missing, routing.yaml rejected, or genuinely empty) — cards post, nobody is mentioned | rule load at `on_ready` and every reload; cleared when >0 rules load |
| `ROLE_UNRESOLVED` | routing.yaml names roles that don't exist in the guild; those rules are skipped | rule load; cleared when all names resolve |
| `CHANNEL_UNWRITABLE` | A configured channel can't be posted to (keyed by channel id) | composition-root channel sends; cleared on the next successful send |
| `WAKE_FAULTED` | Wake model pool latched after a build failure — frames flow, nothing is scored | health check on the wake fault latch; cleared on rebuild |
| `STT_DEGRADED` | Transcription degraded: `watchdog` key = respawn-cap latch (cleared by `/reload`/restart); `confidence` key = §20's 10-consecutive-LOW streak | health checks; cleared on unlatch / next confident resolution |
| `EARS_DOWN` | Ears heartbeat missed or never seen — voice is down, slash path unaffected | health check; cleared on heartbeat recovery or the next Ears `hello` |
| `CONFIG_RESTART_PENDING` | Restart-class config keys edited but not live | the reload transaction; cleared by a reload with none pending, or by the restart itself |
| `TREE_SYNC_STALE` | Slash-command sync failed; the previous sync keeps serving | the background tree sync; cleared on the next successful/skipped sync |
| `TIMER_UNDELIVERED` | A fired timer ping could not be posted to `#intel-alerts` | the timer announcer; cleared on the next delivered ping |
| `INTERACTION_ERRORS` | A command/component handler failed repeatedly (keyed by command name; card after 3 failures) | the interaction error boundary |
| `POST_FAILURE` | An incident card post/edit failed (permissions, deleted channel) | the Poster; cleared on the next successful post |
| `VOICE_ABSENT` | No audio for `health.voice_silence_alarm_s` while Ears is connected and ≥2 unmuted pilots are present (§20 row 1) | health check; cleared when audio flows |

### 11.4 Fleet-ops mode

`/fleetmode on` restricts voice triggering to the FC role for the duration of a structured op. Slash commands remain open to everyone. This exists because §1.2 is real: during a fleet fight, twenty pilots talking to a bot is worse than no bot.

Fleetmode — together with the circuit-breaker window and per-user cooldowns (§11.1) — is snapshotted to `app_state` (key `discipline_state`) on every change and restored at startup: a Brain restart mid-op keeps the FC gate up, and a restart mid-flood does **not** close the breaker. When the restore leaves fleetmode active, startup logs `fleetmode_restored_active` so the non-default state is visible.

---

## 12. Voice back-channel

CORTANA speaking back is not decoration — it is what lets a pilot keep their eyes on the game. It is also the *easy* half of the voice problem, since the send path is fully supported (§2.1).

**Engine: Piper.** Local neural TTS, VITS models exported to ONNX. Real-time on a Raspberry Pi 5 with no GPU, roughly an order of magnitude faster than real time on a normal CPU. Voices are tens of megabytes. No API, no bill, no dependency.

Current release v1.4.2 (April 2026), maintained by the Open Home Foundation at `OHF-Voice/piper1-gpl`.

> **License note:** the original Rhasspy repo was MIT and is archived. The maintained fork is **GPL-3.0**. For a self-hosted corp bot this is immaterial — nothing is distributed. CORTANA invokes Piper as a separate binary over a subprocess boundary rather than linking it, which keeps the question closed even if the bot is later shared with other corps.

### 12.1 Utterance catalogue

Short. Always short. CORTANA is talking over a fight.

| Event | Utterance |
|---|---|
| Wake acknowledged (`wake.ack: voice`) | *"Go ahead."* |
| Ping sent | *"Hostiles Otanuomi, pinged."* |
| Ping sent, scoped | *"Hostiles Otanuomi, pinged home defense."* |
| Ambiguous system | *"Hostiles Otanuomi — say again to confirm."* |
| Unresolved system | *"Say again the system."* |
| No command, no relay frame (§8.6) — reopens ONE wake-free retry | *"Say again?"* — reopens ONE wake-free retry |
| Second consecutive parse failure | *"Standing down. Wake me to retry."* — dialogue closed, fresh wake needed |
| Card post failed (channel perms/REST) | *"Discord post failed."* — the report is rolled back, not recorded |
| Chase updated (§13.1) | *"Chase updated, Kisogo."* |
| Chase, nothing to retarget | *"No active incident to chase."* |
| Chase, no system heard | *"Say update chase and a system, or clear to finish."* |
| Responders | *"Space Junkie responding to Otanuomi."* (callsign/display name; count as fallback) |
| Resolved | *"Otanuomi clear."* |
| Timer set | *"Timer Kisogo, four hours."* |
| Flood control | *"Flood control active."* |
| Degraded | *"Voice offline, use slash commands."* |
| Help | *"Command list posted to Discord."* (the /help page posts alongside) |
| Relay posted | *"Relayed."* |
| Colour code opener | *"Code orange. Go ahead."* |
| Registered | *"Registered you as Space Junkie."* |
| Unregistered | *"Unregistered."* |
| Not registered | *"You are not registered."* |
| Who am I | *"You are Space Junkie."* |
| Unheard callsign | *"Say again the callsign."* |
| Personal ping set | *"Pinging you for gate camps in Otanuomi."* |
| Personal ping set, no system | *"Pinging you for everything everywhere."* |
| Personal pings cleared | *"No longer pinging you."* |
| No personal pings | *"You have no pings set."* |
| Personal ping cap | *"Ping limit reached."* |

Personal-ping type words pluralize naturally: *hostiles*, *attacks*, *assist requests*, *gate camps*, joined with "and"; all four collapse to *everything*.

### 12.2 Speaking rules

- **Never speak over a high-severity report in progress.** If VAD reports active speech, queue. A non-alert utterance still blocked after **3 s** of continuous human speech is **dropped** (logged), never played late — a 3-seconds-stale "Go ahead." spoken over an FC mid-report is worse than none. Alerts always play.
- **Duck, smoothly.** While a pilot is talking CORTANA dips to **75%** so she stays out of the way without vanishing. The duck is **ramped, not stepped** — she glides down over ~250 ms (attack) and eases back up over ~400 ms (release), and once ducked she stays ducked until **700 ms** of silence (release hysteresis). This asymmetry is deliberate: Discord DTX drops packets between words, so a hard on/off duck keyed on a short window pumped her volume audibly on every inter-word gap — the "her voice goes in and out" field report. The speech clock that drives it is bumped only by **actually decoded** voice, so encrypted/failed frames and pure-noise SSRCs can't flap it. Duck level, ramp rates, and talk-over suppression are fixed playback mechanics in Ears (`ears/src/playback.rs`, pure-function tested), not config knobs.
- **She must not fight the CPU for the audio path.** On the 2-vCPU droplet a Whisper decode using both cores starves Songbird's 20 ms real-time Opus mixer, which is *heard directly* as choppy/dropping-out speech (nothing buffers her outbound audio). Two mechanical guards: `stt.cpu_threads: 1` leaves a core for the mixer + event loop, and the systemd units give Ears a high `CPUWeight` (10000 vs Brain's 200) so cgroup v2 lands the mixer's frames on time under contention (§17.2). Diagnose with `mpstat -P ALL 1 10` during a reply — both cores pegged confirms the cause.
- Playback state (queue, playing slot, hold timer) and the speech-activity clock are **per guild** — speech in one voice call never gates or delays TTS in another.
- Hard cap **3 seconds** per utterance. Information-carrying replies that do not fit go to the channel instead; **acknowledgement lines never do** — an unspoken ack ("Say again?", "Standing down…") is logged and dropped, because a retry prompt pasted into the intel channel is noise.
- `/mute-voice` per user. Some pilots will hate this. They can silence it without leaving.

### 12.3 Audio path

Piper emits raw s16le at the model's native rate. Brain wraps it in a WAV header in memory and ships the bytes to Ears, where Songbird's Symphonia layer parses the header and resamples to 48kHz internally. **No resampling code exists in Brain**, and no temporary files are written.

Piper reloads its voice model on every subprocess spawn (~1s on the droplet), so the short scripted §12.1 lines are rendered once and replayed from an in-memory cache — acknowledgements like *"Go ahead."* and *"Relayed."* play near-instantly. The cache is primed in the background at startup, keyed by (text, voice, effect) so a SIGHUP voice swap misses cleanly, and bounded to the scripted-line pool (long, variable text always synthesises fresh).

### 12.4 Personality

`tts.personality` selects the spoken-line flavour. `standard` keeps the exact §12.1 catalogue. `cortana` rotates **acknowledgement lines only** through short variants — *"Go ahead." / "Listening." / "Send it." / "Copy code orange. Send it." / "On the wire."* — so CORTANA feels alive rather than canned. Information-carrying lines (system names, counts, timers, callsigns) never vary: a pilot mid-fight must never parse a surprise phrasing for facts. `bratty` is the cortana rotation with attitude and sailor vocabulary — *"Ugh, fine. Go." / "Posted. You're welcome." / "That was gibberish. Again."* — profanity confined to acknowledgement lines, chosen explicitly by the corp for an adult server. This is scripted variation, not generation — no model is involved (constraint 6), and no real person's voice is imitated.

---

## 13. Fleet ops features

Beyond intel, CORTANA carries the utilities a corp actually asks for. Each is voice-shaped and slash-backed.

| Feature | Voice | Value |
|---|---|---|
| **Structure timers** | *"Hey Cortana, timer Kisogo four hours"* | Schedules a mention ahead of a structure coming out. Enormous value in EVE, and a natural voice command — you're looking at the timer in-game right now. |
| | | Delivery is **at-least-once, two-phase**: the poll CLAIMS due rows (`fired = 1`), and only a confirmed announcement retires them (`announced_at`). A claim whose post never landed (403, crash mid-announce) is re-offered every poll tick — with the `TIMER_UNDELIVERED` alarm raised in `#bot-health` so the retrying is visible, and the spoken "timer due" line held until the channel post actually lands. A structure timer eaten silently is exactly the loss the corp set the timer to avoid. |
| **Form-ups** | *"Hey Cortana, form up Otanuomi fifteen minutes"* | Posts an op card with RSVP buttons and a countdown. |
| **Roll call** | `/rollcall` | Who's in voice, who's subscribed, who's responding. |
| **Jump distance** | *"Hey Cortana, jumps to Jita"* | Spoken reply, no post. BFS over the adjacency graph. |
| **Chase mode** | *"update chase Kisogo"* → `/chase` | Retargets your live incident card as the target moves — one card, edited in place, with a movement trail. |
| **Facts & roasts** | *"tell me a fact"* / *"insult this guy"* → `/fact` `/insult` | Morale. A bundled, accuracy-gated fact library and a roast generator — strictly throttled off the intel path (§13.2). |

### 13.1 Chase mode

A tackled target that jumps out is not a new incident — it is the same incident moving. *"update chase <system>"* (slash twin `/chase`) retargets the pilot's most recent ACTIVE incident:

- The card is **edited in place** (constraint 9) — never a second post — and each hop is appended to the card's updates as a movement trail (*chase → Kisogo → Alenia*).
- System matching is **flexible**: a confident gazetteer match binds the real system (routing and the proximity prior keep working); anything else — a misheard name, a system outside the gazetteer's scope — goes on the card **verbatim**. A chase never stops to ask *"say again the system"* mid-pursuit.
- Confirmation is spoken: *"Chase updated, Kisogo."* With no active incident of yours to retarget: *"No active incident to chase."*; a bare *"chase mode"* with no system: *"Say update chase and a system, or clear to finish."*

### 13.2 Fun commands: the fact library and the insult maker

Entertainment for the quiet stretches between fights — designed so it can never cost the intel path anything.

**Content is bundled, not fetched.** `brain/cortana/data/facts/*.json` ships **1,692 facts across 16 categories** of short, TTS-shaped true facts (space, physics, history, military, tech, animals, the human body, the ocean, gaming, math, geography, engineering, language, food, science, New Eden lore) plus a three-flavour insult pool of **293 lines** (`insults_*.json` — 113 flagged `spicy` for profanity, 180 clean; each line carries the flag so the pool can be toned down without a redeploy). Every line was written and then **accuracy/safety-gated offline** before shipping: no myths in the facts; no slurs, no protected-class jokes, no sexual content in the roasts — profanity is allowed (the corp's explicit choice, mirroring `tts.personality: bratty`), cruelty is not. Constraint 6 stands: serving a line is a lookup, not generation — no LLM, no network, deterministic and debuggable.

**One engine, two doors (constraint 10).** `cortana.core.fun.FunEngine` serves both the voice intents (`FACT`: *"tell me a fact"*, *"space fact"*, *"fact about the ocean"*; `INSULT`: *"insult this guy"*, *"roast Dave"*) and the slash twins `/fact [category]` / `/insult [target]`. A per-guild **shuffle bag** deals the whole deck before any repeat (and never repeats a line back-to-back across refills); facts and insults carry separate per-guild cooldowns (`fun.fact_cooldown_s` / `fun.insult_cooldown_s`).

**Delivery is surface-symmetric, by explicit design:**

- **Voice in → voice out, only.** A spoken request is answered in voice at NORMAL priority (ALERT incident speech always jumps it) under its own length cap (`fun.max_speak_s` — a whole fact outruns the §12.2 command-reply cap). Nothing posts to any channel; a failed synthesis is logged and dropped.
- **Slash in → invoking channel, only.** `/fact` and `/insult` reply in the channel they were run in. An `/insult` naming a member renders the mention but never notifies (`AllowedMentions.none()`) — the escalation authority (constraint 11) is untouched because this path cannot ping anyone.

**Targets.** The voice path extracts a short spoken name from the remainder (*"roast Dave"* → *"Dave. \<line\>"*), discarding pronouns ("this guy", "him") for an untargeted roast; names pass the callsign sanitiser so they can never smuggle markdown or a mention. Insults are written second-person and punch at piloting, decision-making and gaming skill — friendly fire between consenting corpmates, not harassment; `fun.insults_spicy: false` restricts to the clean pool.

**Failure posture.** Empty/missing library → the fixed line *"Fun commands are off."* (voice) or an ephemeral pointer at the logs (slash); throttled → *"Cooling down."*. `fun.enabled: false` turns both surfaces off with the same fixed line. The engine loads once at startup, off the event loop; a malformed file or line is logged and skipped — a bad joke must never stop the intel bot.

---

## 14. Data model

SQLite. One corp, low write volume, no concurrency pressure — a managed database line-item here would be waste.

```sql
-- ── gazetteer ────────────────────────────────────────────────
-- Populated by `python -m cortana.nlu.seed` from the EVE SDE (k-space New Eden,
-- ~5000 systems + the jump graph; §8.1). gazetteer.yaml scopes the ACTIVE
-- subset at runtime — the tables hold the wide seed, not the active set.
CREATE TABLE systems (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    region          TEXT NOT NULL,
    constellation   TEXT,
    metaphone       TEXT NOT NULL,      -- precomputed at load
    x REAL, y REAL, z REAL
);
CREATE INDEX idx_systems_region ON systems(region);
CREATE INDEX idx_systems_metaphone ON systems(metaphone);

CREATE TABLE system_adjacency (
    a_id INTEGER NOT NULL REFERENCES systems(id),
    b_id INTEGER NOT NULL REFERENCES systems(id),
    PRIMARY KEY (a_id, b_id)
);

-- ── learned corrections; consulted BEFORE phonetic matching ──
CREATE TABLE aliases (
    raw_text        TEXT NOT NULL,
    system_id       INTEGER NOT NULL REFERENCES systems(id),
    weight          REAL NOT NULL DEFAULT 1.0,
    learned_at      TEXT NOT NULL,
    corrected_by    INTEGER NOT NULL,
    PRIMARY KEY (raw_text, system_id)
);

-- ── learned custom areas (§8.5a); the systemless twin of aliases ──
CREATE TABLE custom_areas (
    guild_id      INTEGER NOT NULL,
    phrase        TEXT    NOT NULL,   -- normalized key: text.strip().lower()
    display_name  TEXT    NOT NULL,   -- confirmed word, shown verbatim on the card
    learned_by    INTEGER NOT NULL,
    learned_at    TEXT    NOT NULL,
    uses          INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, phrase)
);

-- ── incidents ────────────────────────────────────────────────
CREATE TABLE incidents (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id           INTEGER NOT NULL,
    system_id          INTEGER REFERENCES systems(id),
    system_confidence  REAL,
    type               TEXT NOT NULL,
    severity           TEXT NOT NULL,
    reporter_id        INTEGER NOT NULL,
    detail             TEXT,
    opened_at          TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'ACTIVE',
    message_id         INTEGER,
    channel_id         INTEGER,
    raw_system_text    TEXT,              -- transcript window that named the
                                          -- system; alias key for [Wrong — fix]
    pending_candidates TEXT               -- MEDIUM-tier confirm candidates
                                          -- (JSON [[id, name, score], …]);
                                          -- NULL once confirmed — restarts
                                          -- re-arm the pick buttons (§9.1)
);
CREATE INDEX idx_inc_active ON incidents(guild_id, status, system_id, type, opened_at);

CREATE TABLE incident_updates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id  INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL,
    text         TEXT,
    at           TEXT NOT NULL
);

CREATE TABLE responders (
    incident_id  INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL,
    state        TEXT NOT NULL,           -- OTW | WATCHING | NO
    at           TEXT NOT NULL,
    PRIMARY KEY (incident_id, user_id)
);

-- ── routing ──────────────────────────────────────────────────
CREATE TABLE subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    role_id       INTEGER NOT NULL,
    types_json    TEXT NOT NULL,
    scope_json    TEXT NOT NULL,
    escalate_at   TEXT,
    quiet_hours_json TEXT
);

-- ── personal ping subscriptions (§10.3) ──────────────────────
-- A user mention, not a role: matching incidents append <@user_id>
-- to the mention line. types_json = JSON array of Intent values;
-- system_id NULL = all systems. Capped by discipline.personal_pings_max;
-- never causes @here (constraint 11).
CREATE TABLE personal_pings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    types_json  TEXT NOT NULL,
    system_id   INTEGER REFERENCES systems(id),
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_personal_pings_guild ON personal_pings(guild_id);

-- ── scheduled ────────────────────────────────────────────────
-- Two-phase, at-least-once delivery (§13): fired=1 is the CLAIM,
-- announced_at the confirmed delivery; unannounced claims re-offer.
CREATE TABLE timers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    system_id    INTEGER REFERENCES systems(id),
    fires_at     TEXT NOT NULL,
    note         TEXT,
    created_by   INTEGER NOT NULL,
    fired        INTEGER NOT NULL DEFAULT 0,
    announced_at TEXT
);
CREATE INDEX idx_timers_pending ON timers(fired, fires_at);

-- ── personal reminders (/remindme) ───────────────────────────
CREATE TABLE reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    fires_at    TEXT NOT NULL,
    message     TEXT NOT NULL,
    fired       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_reminders_pending ON reminders(fired, fires_at);
CREATE INDEX idx_reminders_user ON reminders(user_id, fired);

-- ── quick votes (/poll) ──────────────────────────────────────
CREATE TABLE polls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER,
    message_id   INTEGER,
    author_id    INTEGER NOT NULL,
    question     TEXT NOT NULL,
    options_json TEXT NOT NULL,        -- JSON array of option labels
    opened_at    TEXT NOT NULL,
    closed_at    TEXT                  -- NULL while the poll is open
);

CREATE TABLE poll_votes (
    poll_id     INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL,
    option_idx  INTEGER NOT NULL,
    at          TEXT NOT NULL,
    PRIMARY KEY (poll_id, user_id)     -- one vote per pilot; switchable
);

-- ── consent ──────────────────────────────────────────────────
CREATE TABLE optouts (
    user_id  INTEGER PRIMARY KEY,
    at       TEXT NOT NULL
);
CREATE TABLE voice_mutes (
    user_id  INTEGER PRIMARY KEY,
    at       TEXT NOT NULL
);

-- ── pilot callsign registry ──────────────────────────────────
-- Keyed on the Discord user id Ears attaches to every utterance
-- (SSRC→user map): a name registry, no voice biometrics (§19).
CREATE TABLE callsigns (
    user_id        INTEGER PRIMARY KEY,
    callsign       TEXT NOT NULL,
    registered_at  TEXT NOT NULL
);

-- ── process-level state that must survive restarts ───────────
-- Key/value store: join-announcement cadence (§19), alarm-card message
-- ids (§11.3, alarm:<code>:<key>), the synced command-tree payload hash,
-- and the notification-discipline snapshot (§11.4, discipline_state).
CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ── observability: transcripts only, never audio ─────────────
CREATE TABLE command_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL,
    raw_transcript    TEXT NOT NULL,
    parsed_intent     TEXT,
    matched_system_id INTEGER REFERENCES systems(id),
    confidence        REAL,
    tier              TEXT,               -- HIGH | MEDIUM | LOW
    outcome           TEXT,               -- POSTED | FOLDED | ASKED | REJECTED
    at                TEXT NOT NULL
);
CREATE INDEX idx_cmdlog_at ON command_log(at);
```

`command_log` is the accuracy dashboard. Queried weekly, it shows which systems mismatch, whose voice fails most often, and how confidence is distributed — the feedback loop that tunes §8.

Schema revisions are tracked with `PRAGMA user_version` and applied by the runner in `core/db.py` (`brain/migrations/NNNN_*.sql`, one transaction per file); `brain/schema.sql` is the regenerated reference snapshot, never executed directly. Concurrent runners **serialize**: each file's transaction opens `BEGIN IMMEDIATE` and re-checks `user_version` inside it, so Brain startup and the gazetteer seeder migrating the same database at once end with the loser skipping — never a re-executed `CREATE TABLE`.

---

## 15. IPC protocol (v2)

Framed messages over `/run/cortana/cortana.sock`. **Brain binds; Ears connects and reconnects with backoff.** This ordering matters: it means Ears buffers when Brain restarts, rather than the reverse.

```
Frame:  [4-byte BE length][1-byte type][body]
        length = 1 + len(body)  (counts every byte after the length field)

type 0x01  JSON control   (UTF-8)
type 0x02  Audio          Ears→Brain
           [8B user_id LE][8B guild_id LE]
           [8B captured_at ms-since-epoch LE][i16 LE PCM, 16kHz mono]
type 0x03  TTS            Brain→Ears
           [8B guild_id LE][1B priority][WAV bytes]
```

### 15.1 Version handshake

Both binaries embed an **IPC protocol version** constant (`IPC_PROTOCOL_VERSION`, currently **2** — `ears/src/ipc.rs` and `brain/cortana/ipc.py`, bumped in lockstep, same commit, always). The first frame on every connection is Ears' `hello`, carrying it as `proto`:

```jsonc
{ "t": "hello", "proto": 2, "version": "1.0.0" }   // version = ears build (Cargo)
```

On mismatch (or a missing `proto` — a pre-v2 Ears) Brain logs `ipc_protocol_mismatch` loudly and **refuses the client**; Ears keeps retrying with backoff, so a desynced deploy is noisy on both sides and works on neither. `cortana-ears --version` prints both the build and protocol versions so an operator can confirm a matched pair. Brain drops any frame that arrives before a valid `hello`.

### 15.2 Audio timestamps and the age gate

Ears stamps every 0x02 frame with the wall-clock capture time at receipt (`captured_at`, ms since epoch). Ears applies no judgement to it — it just stamps the truth. **Brain age-gates**: frames older than `MAX_AUDIO_AGE_S` (3 s, `brain/cortana/ipc.py`) are dropped before they reach wake/capture. Ears buffers up to 60 s of audio through a Brain outage and flushes it on reconnect; without the gate that flush would replay stale wake words and feed the wall-clock endpointer a minute of audio arriving in milliseconds.

### 15.3 Control messages

```jsonc
// Ears → Brain
{ "t": "hello",     "proto": 2, "version": "1.0.0" }        // first frame, always
{ "t": "snapshot",  "guilds": [                              // immediately after hello
    { "guild_id": "…", "channel_id": "…" /* or null */,
      "connected": true,
      "users": [ { "ssrc": 12345, "user_id": "…" }, … ] } ] }
{ "t": "speaking",  "user_id": "…", "guild_id": "…", "state": "start" }
{ "t": "left",      "user_id": "…", "guild_id": "…" }
{ "t": "join_ok",   "guild_id": "…", "channel_id": "…" }
{ "t": "join_failed", "guild_id": "…", "channel_id": "…", "reason": "…" }
{ "t": "driver_disconnected", "guild_id": "…",
  "kind": "connect" /* | "reconnect" | "runtime" */, "reason": "…" }
{ "t": "ears_restarting", "guild_id": "…", "reason": "decode_wedge" }
                                             // best-effort heads-up before
                                             // the wedge watchdog exits (§20)
{ "t": "heartbeat", "ticks": 15021, "active_ssrcs": 4, "connected": true }

// Brain → Ears
{ "t": "join",    "guild_id": "…", "channel_id": "…" }
{ "t": "leave",   "guild_id": "…" }
{ "t": "optouts", "user_ids": ["…", "…"] }   // enforced in Ears, pre-IPC
{ "t": "hb_ack" }                            // answer to every heartbeat
```

**Snapshot replaces lossy deltas.** Control events generated during an IPC outage are discarded; instead, right after `hello`, Ears sends its full state — per-guild connected flag, joined channel, and the live SSRC↔user roster. Brain reconciles: users it still tracks that Ears no longer sees are purged exactly as if their `left` event had arrived. State is reset only where the views differ.

**Join results are events, not assumptions.** Every `join` command is answered with `join_ok` or `join_failed(reason)`; a voice session dying mid-flight (DAVE crash, 4014, channel deleted) produces `driver_disconnected(kind, reason)`. The retry/rejoin **policy lives in Brain** (`voice_gateway.py` re-schedules a debounced join while pilots remain; `driver_disconnected` is logged and noted in health) — Ears only reports what happened. This implements §20's "DAVE session crash → rebuild with backoff" row.

### 15.4 Replay on (re)connect

Brain replays state the moment Ears (re)connects, **opt-outs first**: on every `hello` Brain unconditionally sends `optouts` before anything else, then the current `join`. Ordering matters because Ears **fails closed** — a fresh Ears process drops ALL audio until the first `optouts` frame of its lifetime is applied (§19), so the sooner it lands, the sooner pilots are heard.

A freshly started Ears process reaches the socket before its own Discord gateway is READY, so Ears **holds join/leave commands until READY** and executes them then — the Songbird manager cannot create calls before serenity initialises it, and acting early would crash the voice control task. `join` is **idempotent**: already connected to the requested guild+channel means Ears answers `join_ok` and touches nothing — in particular it does NOT re-register receive handlers, because the SSRC↔user map lives in per-guild process state and Discord never re-announces mappings mid-session. Only a genuine driver (re)connect, where the SSRCs actually change, may clear attribution state. A routine Brain restart can therefore never deafen the bot.

### 15.5 Buffering, backpressure, and liveness

- **One ring, always on.** Ears' outbound audio ALWAYS flows through the bounded `AudioRing` (60 s / ~1.9 MB, oldest evicted first) — connected or not. A wedged-but-connected Brain costs bounded, observable audio loss, never unbounded memory.
- **Write deadline.** Every Ears socket write carries a 5 s deadline; a miss marks the connection Lost (Brain stopped reading) and Ears reconnects with backoff.
- **Inbound liveness.** Brain answers every heartbeat with `hb_ack`, so a healthy link carries Brain→Ears bytes at least every 5 s. Ears treats **20 s without any inbound bytes** as Lost.
- **Ring flush gated on proof of liveness.** After connecting, Ears sends `hello` + `snapshot` and then HOLDS the ring until Brain's first control frame arrives (in practice the `optouts` replay, within milliseconds). Only then is the buffered audio flushed and the reconnect backoff reset. A crash-looping Brain that accepts the socket and dies before speaking cannot destroy the buffer — Ears escalates backoff as if the connect had failed, ring intact.
- Brain-side sends (`send_tts`/`send_control`) return a bool: `False` when no Ears client is attached or the write failed. Callers treat `False` as "not spoken/delivered" and fall back to channel text — an Ears outage degrades loudly, never silently.

### 15.6 Preflight

`cortana-ears --check [config]` parses `ears.yaml` (unknown keys still rejected), verifies the credential is readable and the socket directory exists, and constructs the statically linked Opus decoder; exit 0 on success, 78 (`EX_CONFIG`) on config/credential problems. Wired for `ExecStartPre=` so a bad edit fails the restart, not the running service.

---

## 16. Configuration reference

`/etc/cortana/cortana.yaml`. The annotated example (`config/cortana.yaml.example`) is the operator-facing copy; the table below is **generated from the declarative schema** (`brain/cortana/config_schema.py`) by `scripts/gen_config_docs.py`, and CI fails when it drifts — the schema, the example, and this table cannot disagree silently.

**Reload classes** (what it takes for an edit to reach live behaviour): `hot` — live on `systemctl reload cortana-brain`; `sighup` — applied by an explicit step in the reload transaction; `engine` — rebuilds the gazetteer/routing engine on reload; `restart` — bound at startup, reported as "restart pending" by the reload receipt, never silently absorbed. Validate any edit before restarting with `python -m cortana.doctor --config <file>`.

<!-- BEGIN GENERATED CONFIG TABLE (scripts/gen_config_docs.py) -->

| Key | Type | Default | Reload | Purpose |
|---|---|---|---|---|
| **`discord:`** | | | | *Guild, channels, role gates, mention policy.* |
| `discord.token_file` | str | **required** | restart | Dev-fallback token path (0600). Production reads $CREDENTIALS_DIRECTORY/token via systemd LoadCredential= (constraint 12) — the token is never in YAML. |
| `discord.guild_id` | int | **required** | restart | The corp's guild snowflake. |
| `discord.channels.intel_alerts` | int | **required** | hot | Channel for incidents that mention a role (GDD §11.2). |
| `discord.channels.intel_live` | int | **required** | hot | Channel for every incident, no mentions — the firehose. |
| `discord.channels.health` | int | **required** | hot | Channel for self-reports and degradation alerts. |
| `discord.channels.transcript` | int | `0` | hot | Optional. When set, every heard utterance posts one clean line — what CORTANA thinks it heard plus how it parsed — so phrasing and misfires can be reviewed at a glance (GDD §8.7). 0 = off. |
| `discord.roles.pilot` | int | `0` | hot | Only members with this role may trigger mentions. 0 = gate off. |
| `discord.roles.fc` | int | `0` | hot | Only this role voice-triggers under fleetmode / uses admin commands without Manage Guild. 0 = gate off. |
| `discord.watch_voice_channels` | int_list | **required** | hot | Voice channels CORTANA watches / auto-joins. |
| `discord.auto_join` | bool | `True` | hot | Join when a pilot enters, leave when empty. |
| `discord.mentions_enabled` | bool | `True` | hot | false = silent mode: post cards, ping nobody. |
| `discord.here_on_severity` | str_list | `('high',)` | hot | Threat colours that fire @here: high=RED, medium=ORANGE, none=YELLOW (never fires). One of: `high`, `medium`, `none`. |
| `discord.join_announcement` | str | `'daily'` | hot | §19 consent notice cadence on voice join. One of: `every`, `daily`, `off`. |
| **`wake:`** | | | | *openWakeWord model and trigger thresholds.* |
| `wake.model` | str | **required** | sighup | Trained openWakeWord ONNX chain; per-user models are built from it at speaker onset and cached for the process lifetime. |
| `wake.extra_models` | str_list | `()` | sighup | Additional openWakeWord ONNX chains scored in parallel with wake.model — any listed phrase wakes CORTANA; wake.threshold applies to the max score across all models. Broken/missing extras are logged once and skipped. Each extra adds its own false-fire budget: keep the total to 2-3 models (GDD §5.2). |
| `wake.threshold` | float | **required** | hot | Wake score needed to open a capture window. |
| `wake.refractory_ms` | int | **required** | hot | Per-user dead time after a wake hit. |
| `wake.ack` | str | `'beep'` | hot | Wake acknowledgement: spoken, tone, or silent. One of: `voice`, `beep`, `none`. |
| `wake.vad_threshold` | float | `0.0` | sighup | OPT-IN Silero VAD gate inside openWakeWord (0.0 = off). Applied at model build; the pool rebuilds per-user models live on reload. |
| **`capture:`** | | | | *Utterance capture windows and VAD mode.* |
| `capture.preroll_ms` | int | **required** | hot | Ring-buffer audio prepended to each capture. Must fit inside the fixed 1500 ms privacy ring (cross-checked). |
| `capture.endpoint_silence_ms` | int | **required** | hot | Trailing silence that ends an utterance (wall-clock under DTX). |
| `capture.max_utterance_ms` | int | **required** | hot | Hard cap on a single capture window. |
| `capture.vad_aggressiveness` | int | **required** | restart | webrtcvad mode 0 (permissive) – 3 (aggressive); the VadGate is built once at startup. |
| `capture.streaming` | bool | `True` | hot | Live recognition (GDD §5.5): decode the growing capture while the pilot is still talking and commit the instant a complete, confident command is present, instead of waiting for silence/the hard cap. The fix for the 'keep talking and it drags' latency. Needs CPU headroom (each incremental decode is a real Whisper run) — sized for a dedicated >=4-vCPU box. false = decode-on-endpoint only. |
| `capture.partial_decode_ms` | int | `1200` | hot | Minimum new speech between incremental decodes (GDD §5.5) — the incremental-decode rate limiter. Lower = snappier + more CPU. |
| `capture.partial_min_speech_ms` | int | `900` | hot | Don't attempt an incremental decode until at least this much speech has accrued (GDD §5.5): a sub-second fragment can't carry a command. |
| `capture.early_commit_min_logprob` | float | `-1.0` | hot | Confidence floor for an incremental decode to commit early (GDD §5.5). An uncertain partial keeps listening rather than clipping the pilot; the normal endpoint still catches it. |
| **`dialog:`** | | | | *OPTIONAL voice dialog engine timing/budgets (GDD §5.4); defaults are the tuned live values.* |
| `dialog.window_ms` | int | `4000` | hot | Wall-clock lifetime of a wake-free window (say-again retry, code-colour opener, bare command override). DTX-proof: the dialog wheel expires it in real time, frames or no frames. |
| `dialog.ack_grace_ms` | int | `2000` | hot | Endpoint grace after a capture opens or a prompt is spoken — cue playback plus pilot reaction time. |
| `dialog.endpoint_gap_floor_ms` | int | `700` | hot | Floor under capture.endpoint_silence_ms for the wall-clock endpoint: DTX drops packets between words; a too-eager gap clips pilots mid-sentence. |
| `dialog.max_retries` | int | `2` | hot | Wake-free windows per dialog TOTAL (subdialog openers and say-again retries share the budget). Only a fresh wake refills it; exhaustion ends audibly with standing-down. |
| `dialog.confirm_reports` | str | `'low'` | hot | Confirm-first for voice reports (GDD §8.3): off = commit immediately (readback only); low = uncertain system matches read the situation back ("Under attack in X, confirm?") first; always = every voice report asks. Yes commits (flexibly: yes/confirm/ok/post it/send it/…), no opens a say-again retry, silence/unmatched commits anyway — a distress call is never lost. One of: `off`, `low`, `always`. |
| `dialog.retry_min_logprob` | float | `-1.3` | hot | Transcripts below this Whisper confidence are chatter/noise: they never earn a say-again retry — the dialog closes silently instead of re-prompting into an open mic. Recognised commands are never gated by this. |
| **`stt:`** | | | | *Speech-to-text backend and relay gates.* |
| `stt.backend` | str | **required** | restart | Which Transcriber engine to build at startup. One of: `faster-whisper`, `whisper-cpp`. |
| `stt.model` | str | **required** | restart | Whisper model size or path. |
| `stt.compute_type` | str | **required** | restart | CTranslate2 quantization. |
| `stt.cpu_threads` | int | `1` | restart | Whisper inference threads. On a 2-vCPU droplet use 1 — ON PURPOSE: a decode using BOTH cores starves the Ears real-time Opus mixer, which is exactly the 'her voice is choppy / drops out' symptom (the mixer misses its 20ms frame deadline). On a dedicated >=4-vCPU box set 2: decodes (including streaming's incremental ones, §5.5) run snappier while the high-CPUWeight mixer keeps its cores. The example config ships 2 for that box; drop to 1 on 2 vCPUs. |
| `stt.bias_with_gazetteer` | bool | `True` | restart | Bias Whisper toward the gazetteer's system names (via hotwords, or initial_prompt on older faster-whisper) — through exactly one channel, hard-capped so a wide include_all prompt can't overflow the decoder's 448-token position limit. |
| `stt.whisper_cpp_url` | str | `'http://127.0.0.1:8080/inference'` | restart | whisper.cpp server endpoint; required (non-empty) only when stt.backend is whisper-cpp (cross-checked). |
| `stt.watchdog_s` | float | `15.0` | restart | GDD §20 "STT worker hang" watchdog deadline. The whisper-cpp HTTP timeout is derived slightly below it so the socket gives up before the watchdog abandons the worker. |
| `stt.relay_min_logprob` | float | `-0.9` | hot | Freeform relays below this Whisper confidence are dropped with "Say again" (GDD §8.6); recognised commands are never gated. |
| `stt.relay_mode` | str | `'framed'` | hot | What unmatched speech may become a relay card (GDD §8.6). One of: `framed`, `open`, `off`. |
| `stt.no_repeat_ngram_size` | int | `3` | hot | faster-whisper repetition guard (GDD §5.3): forbid any n-gram of this length from repeating in a decode, breaking the noisy-audio loops where one system name is emitted dozens of times ("0-R5TS, 0-R5TS, …") at high confidence — which buries the callout and starves streaming's early-commit. 3 is safe for real speech; 0 disables. Ignored where the installed faster-whisper predates the parameter. |
| **`matching:`** | | | | *Phonetic system-name matcher weights (constraint 7).* |
| `matching.phonetic_weight` | float | **required** | hot | Weight of metaphone similarity (constraint 7). Must sum to 1.0 with text_weight (cross-checked). |
| `matching.text_weight` | float | **required** | hot | Weight of raw-text Levenshtein similarity. |
| `matching.full_map_fallback` | bool | `True` | hot | When a report doesn't confidently match the scoped active set, re-resolve against the ENTIRE seeded k-space map (GDD §8.1) so any real system still resolves — the reliability fix for a small scope or a roaming corp. The scoped set keeps home-region accuracy; the full-map pass runs without home/proximity priors and a MEDIUM hit asks to confirm. false = scoped set only (the old behaviour). |
| `matching.tiers.high_min` | float | **required** | hot | top1 >= this (and margin) → post immediately. |
| `matching.tiers.high_margin` | float | **required** | hot | top1 - top2 must also clear this for HIGH tier. |
| `matching.tiers.medium_min` | float | **required** | hot | top1 >= this → post flagged uncertain, with buttons. Must be <= high_min (cross-checked). |
| `matching.priors.recency_weight` | float | **required** | hot | Boost for systems with recent incidents. |
| `matching.priors.recency_window_min` | int | **required** | hot | How recent counts as recent. |
| `matching.priors.proximity_weight` | float | **required** | hot | Boost for systems near an active incident. |
| `matching.priors.proximity_max_jumps` | int | **required** | hot | Beyond this many jumps, no proximity boost. |
| `matching.priors.reporter_history_weight` | float | **required** | hot | Boost for systems this pilot reports from often. |
| `matching.priors.home_weight` | float | **required** | hot | Standing boost for home and adjacent systems. |
| **`incidents:`** | | | | *Dedupe / staleness / cancel windows.* |
| `incidents.dedupe_window_s` | int | **required** | hot | Same system + type within this window → fold (GDD §9.2). |
| `incidents.stale_after_min` | int | **required** | hot | No updates for this long → auto-STALE, silently. |
| `incidents.auto_resolve_min` | int | `60` | hot | No updates for this long → auto-RESOLVE: the card closes in place, silently, instead of sitting open until someone clears it. Timers and form-ups are exempt (their lifecycle anchors on fires_at). 0 = never auto-resolve. |
| `incidents.cancel_window_s` | int | **required** | hot | "hey cortana, cancel" kills the user's last incident inside this. |
| **`discipline:`** | | | | *Mention cooldowns and the flood breaker.* |
| `discipline.user_cooldown_s` | int | **required** | hot | Min seconds between mentions from the same pilot. |
| `discipline.circuit_breaker.max_mentions` | int | **required** | hot | More than this many mentions in window_min → flood control. |
| `discipline.circuit_breaker.window_min` | int | **required** | hot | The flood-control sliding window, in minutes. |
| `discipline.personal_pings_max` | int | `10` | hot | Max personal /pingme subscriptions per pilot (GDD §10.3). |
| **`tts:`** | | | | *Piper synthesis and spoken-line personality.* |
| `tts.enabled` | bool | `True` | hot | Spoken back-channel on/off. |
| `tts.voice` | str | **required** | hot | Piper voice model; the sample rate is re-read on config swap. |
| `tts.binary` | str | **required** | hot | Piper invoked as a subprocess per synthesis (GDD §12). |
| `tts.max_utterance_s` | float | **required** | hot | Hard cap; longer text goes to the channel instead. |
| `tts.effect` | str | `'none'` | hot | Post-synthesis effect: chorus+reverb "ship AI" sheen or none. One of: `none`, `holographic`. |
| `tts.personality` | str | `'standard'` | sighup | Spoken-line flavour; applied by set_personality() in the reload transaction. One of: `standard`, `cortana`, `bratty`. |
| **`fun:`** | | | | *OPTIONAL fact library / insult maker (GDD §13.2); absent = defaults (on).* |
| `fun.enabled` | bool | `True` | hot | The fact library and insult maker (GDD §13.2). Off = both voice intents and slash twins answer with a fixed refusal line. |
| `fun.fact_cooldown_s` | int | `10` | hot | Per-guild seconds between served facts — comedy never crowds comms. |
| `fun.insult_cooldown_s` | int | `10` | hot | Per-guild seconds between served insults. |
| `fun.insults_spicy` | bool | `True` | hot | true = the full sailor-mouth pool; false = clean burns only. |
| `fun.max_speak_s` | float | `20.0` | hot | Spoken-length cap for facts/insults, overriding tts.max_utterance_s — a whole fact runs longer than a command reply. |
| **`chat:`** | | | | *OPTIONAL "command override" assistant (GDD §6.6); absent = off.* |
| `chat.enabled` | bool | `False` | sighup | Pilots can say "command override, <question>" (/ask twin). Costs real money per question. |
| `chat.backend` | str | `'anthropic'` | sighup | Who answers override questions: 'anthropic' = the cloud Claude API (needs a key, costs per question); 'local' = an on-box OpenAI-compatible server at chat.local_url (no API, no key, no per-question cost) — the SLM lane for conversational back-and-forth on the droplet. Still OFF the command path (constraint 6). One of: `anthropic`, `local`. |
| `chat.local_url` | str | `''` | sighup | OpenAI-compatible chat-completions endpoint for backend='local' (e.g. http://127.0.0.1:8081/v1/chat/completions from llama.cpp's server or Ollama). Empty = not configured. |
| `chat.model` | str | `'claude-haiku-4-5'` | hot | Model for override replies. For backend='anthropic' a Claude model id; for backend='local' the model name the local server expects. |
| `chat.api_key_file` | str | `'/etc/cortana/anthropic'` | sighup | Dev fallback ONLY (0600); production reads $CREDENTIALS_DIRECTORY/anthropic via LoadCredential= (constraint 12). The client is rebuilt when the on-disk key changes. |
| `chat.max_tokens` | int | `300` | hot | Hard cap per answer. |
| `chat.user_cooldown_s` | int | `10` | hot | Per-pilot throttle — the cost control. |
| `chat.timeout_s` | float | `25.0` | hot | Wall-clock cap per answer incl. web search. |
| `chat.web_search` | bool | `True` | hot | Allow one live web search per question. |
| `chat.answer_channel` | int | `0` | hot | Channel for answers too long to speak. 0 = intel_live. |
| **`gazetteer:`** | | | | *Active system-set scoping — GDD §8.1.* |
| `gazetteer.file` | str | **required** | engine | Scope rules file (regions/within_jumps_of/include_all, GDD §8.1). |
| `gazetteer.home_system` | opt_str | `None` | engine | Anchor for the home-bias prior (§8.4). null/empty = no home system → prior off (nomadic corps, GDD §8.1). |
| `gazetteer.include_all` | bool | `False` | engine | Nomadic override, mirrors gazetteer.yaml include_all — either being true activates the entire seeded map. |
| **`areas:`** | | | | *OPTIONAL custom-area learning (GDD §8.5a); absent = defaults (on).* |
| `areas.learn` | bool | `True` | hot | Custom-area learning (GDD §8.5a): when a report names a place that resolves to no system, CORTANA asks once ('Did you say <word>?') and on an explicit yes remembers it as a custom area, resolving it for good. false = post unknown places verbatim without ever learning. |
| `areas.max_per_guild` | int | `200` | hot | Per-guild cap on learned areas (GDD §8.5a). At the cap learning pauses (reports still post) until an FC prunes with /areas-forget — the guard against a stuck mishearing filling the table. |
| **`nlu:`** | | | | *OPTIONAL LLM understanding brain (GDD §6.7); absent = off (grammar only).* |
| `nlu.understanding` | bool | `False` | hot | When the fixed grammar can't parse a callout, an on-box model reads the transcript and returns the command (GDD §6.7) — pilots can say it any way. The place is still resolved against the real system map (no invented systems) and nothing pings until the pilot confirms. Needs nlu.url + a running local model. false = grammar only. |
| `nlu.url` | str | `''` | hot | OpenAI-compatible chat-completions endpoint of the on-box model (e.g. http://127.0.0.1:11434/v1/chat/completions from Ollama). Empty = off. |
| `nlu.model` | str | `''` | hot | Model name the local server expects (e.g. llama3.2:3b). |
| `nlu.timeout_s` | float | `8.0` | hot | Wall-clock cap on one interpretation; the grammar already answered the clear callouts fast, so this only paces the messy ones. |
| **`killboard:`** | | | | *OPTIONAL Albion Online killboard add-on (killboard GDD); absent/off by default. A separate game module — its own SQLite file, own poll loop.* |
| `killboard.enabled` | bool | `False` | restart | Master switch for the Albion killboard module. Also needs a guild and a feed channel; the module stays dark until all are set. |
| `killboard.region` | str | `'west'` | restart | Which Albion server the guild lives on — selects the API host (killboard GDD §2.2). Hitting the wrong region returns empty data. One of: `west`, `europe`, `east`. |
| `killboard.guild_name` | str | `''` | restart | Guild name, resolved to an id once at startup via /search. Leave empty if setting killboard.guild_id directly. |
| `killboard.guild_id` | str | `''` | restart | Albion guild id, set directly to skip name resolution. Empty = resolve from killboard.guild_name. |
| `killboard.poller.interval_seconds` | int | `45` | hot | Seconds between gameinfo polls. Gentle by design (killboard GDD §15). |
| `killboard.poller.request_timeout_seconds` | int | `10` | hot | Per-request HTTP timeout against the flaky gameinfo API. |
| `killboard.poller.max_retries` | int | `3` | hot | Retries per poll before giving up until the next tick. |
| `killboard.poller.backoff_base_seconds` | float | `5.0` | hot | Exponential backoff base on request failure (5, 10, 20 …). |
| `killboard.poller.page_limit` | int | `51` | hot | Events per request; 51 is the endpoint maximum (killboard GDD §5.2). |
| `killboard.poller.max_backfill_pages` | int | `20` | restart | First-run backfill depth in pages (≈ the server offset ceiling). Bounds how much recent history the seed captures (killboard GDD §5.3). |
| `killboard.feed.kills_channel` | int | `0` | hot | Channel id for guild kills. 0 = unset. |
| `killboard.feed.deaths_channel` | int | `0` | hot | Channel id for guild deaths (may equal kills_channel). 0 = unset. |
| `killboard.feed.min_fame` | int | `0` | hot | Suppress kills below this fame from the main feed. 0 = show all. |
| `killboard.feed.juicy_channel` | int | `0` | hot | Optional highlights channel for high-value kills. 0 = off. |
| `killboard.feed.juicy_min_fame` | int | `2000000` | hot | Fame threshold for a kill to also post to juicy_channel. |
| `killboard.feed.juicy_min_loot` | int | `0` | hot | Loot-value (silver) threshold for a kill to also post to juicy_channel; catches low-fame/high-loot ganks. 0 = off; needs killboard.market.enabled. |
| `killboard.feed.ignore_deaths_below_ip` | int | `0` | hot | Skip low-item-power 'naked' deaths that just clutter the feed. 0 = none. |
| `killboard.feed.blob_participant_threshold` | int | `20` | hot | Kills with participant counts above this are flagged as blob/ZvZ and may route to blob_channel. |
| `killboard.feed.blob_channel` | int | `0` | hot | Optional ZvZ channel for blob kills. 0 = keep them in the main feed. |
| `killboard.feed.catchup_max_posts` | int | `20` | hot | Cap on feed posts per catch-up cycle after downtime; the rest get a single 'posted N older events' summary (killboard GDD §7.3). |
| `killboard.feed.post_delay_ms` | int | `750` | hot | Spacing between feed messages so a backfill burst can't hit rate limits. |
| `killboard.cards.enabled` | bool | `True` | hot | Render composited kill-card images (Pillow). false = embed only. |
| `killboard.cards.icon_cache_dir` | str | `'/var/lib/cortana/killboard/icons'` | restart | On-disk item-icon cache from the render service (fetched once each). |
| `killboard.cards.render_base` | str | `'https://render.albiononline.com/v1'` | hot | Albion render service base URL (documented, cacheable — not the gameinfo API). |
| `killboard.cards.brand_name` | str | `'Dead Gaming'` | hot | Footer tagline on cards (the hosting Discord's name). |
| `killboard.cards.brand_logo_path` | str | `''` | hot | PNG watermark logo path; empty = the bundled Dead roundel. |
| `killboard.cards.accent_color` | str | `'#E11212'` | hot | Card accent colour as #RRGGBB (headers, rank boards). |
| `killboard.cards.show_loot_value` | bool | `True` | hot | Print estimated loot value on kill cards (needs killboard.market.enabled). |
| `killboard.cards.daily_ranking_card` | bool | `True` | hot | Attach a branded Daily Ranking image to the daily scheduled post. |
| `killboard.cards.reaper_watermark` | bool | `True` | hot | Faint Dead reaper emblem behind the kill card (the corner roundel is separate). |
| `killboard.rankings.timezone` | str | `'UTC'` | hot | Timezone for daily/weekly/monthly ranking windows and schedules. |
| `killboard.battles.channel` | int | `0` | hot | Optional battle-summary channel. 0 = battle posting off. |
| `killboard.battles.min_players` | int | `20` | hot | Minimum guild participants for a battle to post (killboard GDD §9). |
| `killboard.battles.min_fame` | int | `5000000` | hot | Minimum total fame swung for a battle to post. |
| `killboard.storage.db_path` | str | `'/var/lib/cortana/killboard/killboard.db'` | restart | The killboard's own SQLite file — separate from CORTANA's. Irreplaceable (the API can't re-serve old events); back it up (killboard GDD §2.4). |
| `killboard.staleness.warn_after_minutes` | int | `30` | hot | No successful poll for this long → surface it in /killboard status. |
| `killboard.staleness.no_events_notice_hours` | int | `6` | hot | No NEW events for this long → note 'guild is quiet' (not an error). |
| `killboard.market.enabled` | bool | `False` | restart | Turn on the market layer: prices item loot value onto kill cards and enables the /market commands. Uses the crowd-sourced AODP API (its host is derived from killboard.region), so it's opt-in. |
| `killboard.market.cache_ttl_s` | int | `300` | hot | Seconds to cache a price lookup in memory. Prices move on the minute at most, so caching hard stays well under the AODP rate limit. |
| `killboard.market.request_timeout_s` | int | `10` | hot | Per-request HTTP timeout against the AODP API. |
| `killboard.market.default_quality` | int | `1` | hot | Item quality used when a lookup omits it: 1 Normal, 2 Good, 3 Outstanding, 4 Excellent, 5 Masterpiece. |
| `killboard.market.default_cities` | str_list | `('Caerleon', 'Bridgewatch', 'Lymhurst', 'Martlock', 'Fort Sterling', 'Thetford')` | hot | Cities compared by /market and used to reference-price kill loot. |
| `killboard.market.user_agent` | str | `'DeadBot-Killboard (self-hosted; contact your guild admin)'` | restart | User-Agent sent to the AODP API (be a good citizen — identify the bot). |
| **`routing:`** | | | | *OPTIONAL routing.yaml location; absent = sibling of cortana.yaml.* |
| `routing.file` | str | `''` | engine | routing.yaml location. Empty (the default) = routing.yaml in the same directory as cortana.yaml. |
| **`ipc:`** | | | | *The Brain⇄Ears unix socket (GDD §15).* |
| `ipc.socket` | str | **required** | restart | Brain binds; Ears connects (GDD §15). Bound once at startup. |
| **`health:`** | | | | *Self-report cadence and degradation alarms.* |
| `health.report_interval_min` | int | **required** | hot | Cadence of #bot-health self-reports. |
| `health.voice_silence_alarm_s` | int | **required** | hot | No VoiceTick this long with >= 2 humans present → degraded. |
| **`database:`** | | | | *SQLite location.* |
| `database.path` | str | **required** | restart | SQLite (WAL) location; opened once at startup. |

<!-- END GENERATED CONFIG TABLE -->

`/etc/cortana/gazetteer.yaml` — scope rules over the SDE-seeded tables (§8.1). The
tables themselves are filled by `python -m cortana.nlu.seed` (k-space New Eden),
which this file then scopes at runtime:

```yaml
# false (default) = scoped by the rules below (home-region corps).
# true = nomadic mode: the entire seeded map is active, regions/within_jumps_of
# are ignored, exclude still removes. Set home_system: null in cortana.yaml too.
include_all: false

regions:                 # scoped mode: regions included wholesale
  - Kisogo-region
  - Lowsec-North
within_jumps_of:         # scoped mode: everything within N jumps of the anchor
  system: Otanuomi
  jumps: 8
always_include:          # the hubs pilots name anyway (both modes)
  - Jita
  - Amarr
  - Rens
  - Dodixie
exclude: []              # dropped even if a rule above matched (both modes)
```

---

## 17. Deployment specification

### 17.1 Droplet

Real-time audio is latency-sensitive, and **shared vCPU means CPU steal, which means jitter**. This is the one line-item not to economise on.

| Plan | ~$/mo | Verdict |
|---|---|---|
| Basic (shared) 1 vCPU / 1 GB | ~$6 | Rejected — steal time wrecks the audio thread |
| Premium AMD 2 vCPU / 4 GB | ~$24–28 | Workable; still shared vCPU |
| **CPU-Optimized 2 vCPU / 4 GB** | **~$42** | **Specified.** Dedicated cores, NVMe. Whisper `small` int8 needs the headroom. |
| CPU-Optimized 4 vCPU / 8 GB | ~$84 | For Whisper `medium`, or serving multiple corps |

DigitalOcean moved to **per-second billing (60s minimum) on January 1, 2026**. List prices move a few times a year — confirm against the live pricing page before provisioning.

**Region:** choose the datacenter nearest the **Discord voice region your corp actually lands in**, not nearest your players. CORTANA's RTT to Discord's voice server is what sits in the audio path; your pilots' RTT to CORTANA is irrelevant because they never talk to it directly.

### 17.2 Host layout

```
Ubuntu 24.04 LTS
├── systemd
│   ├── cortana-ears.service     Rust binary,  Restart=always, RestartSec=5
│   └── cortana-brain.service    Python,       Restart=always, RestartSec=5
├── /opt/aura-src/             the git clone deploys run from (git pull && bash deploy/install.sh)
├── /opt/cortana/
│   ├── releases/<stamp>-<sha>/   staged releases: brain tree + venv + bin/cortana-ears (§17.5)
│   ├── current -> releases/…     the LIVE release — an atomic symlink flip
│   ├── models/{wake,whisper,piper}/
│   └── alert.sh                  OnFailure= hook → webhook/alerts.log
├── /etc/cortana/{cortana.yaml,routing.yaml,gazetteer.yaml,ears.yaml,token}
├── /var/lib/cortana/cortana.db
├── /var/lib/cortana/hf/              shared HF cache (whisper weights; HF_HOME)
├── /run/cortana/cortana.sock        (tmpfiles.d, mode 0660, root:aura)
├── user: aura  (nologin, owns runtime dirs)
└── ufw: deny incoming, allow SSH only — CORTANA opens no listening ports
```

### 17.3 Build and system dependencies

```
build-essential autoconf automake libtool m4 cmake pkg-config
libopus-dev            # required by Songbird
python3.12 python3.12-venv
piper                  # /usr/local/bin/piper
```

- **No ffmpeg.** Songbird ≥0.4 removed it entirely in favour of Symphonia.
- Compile the Rust binary in CI or on a larger machine. A 4 GB droplet will thrash building Songbird with LTO. Ship the binary, not the toolchain. CI publishes each main-branch build to the `ears-bin` branch (binary + sha256), which `install.sh` fetches with the droplet's existing clone credentials — no GitHub API token on the host.
- `openwakeword` is installed `--no-deps`: its Linux dependency pin `tflite-runtime` has no wheels for Python ≥3.12, and CORTANA uses only the ONNX inference path. Its true runtime dependencies are listed explicitly in `requirements.txt`.

### 17.4 Bot permissions and intents

| Permission | Why |
|---|---|
| `Connect`, `Speak` | Join voice, play TTS |
| `Use Voice Activity` | Receive |
| `Send Messages`, `Embed Links` | Post incident cards |
| `Read Message History` | Edit incident cards |
| `Mention Everyone` | Only if `@here` escalation is enabled |
| `Manage Roles` | `/subscribe` self-assign — bot's role must sit above the subscribable roles |
| **Intent** `GUILD_VOICE_STATES` | Required by Songbird's gateway and by auto-join |
| **Intent** `GUILD_MEMBERS` | Role gating |

**Never grant Administrator.** If the token leaks, an Administrator bot can ping everyone, forever.

### 17.5 Staged deploys — install.sh is converge-and-verify

`cd /opt/aura-src && git pull && bash deploy/install.sh` is the whole deploy. The installer is idempotent and staged; a failed deploy can strand nothing, because nothing goes live until it has been proven:

1. **STAGE** — build a complete release under `/opt/cortana/releases/<stamp>-<sha>/`: the brain source tree, the sha256-verified `cortana-ears` binary fetched from the CI-published `ears-bin` branch (no GitHub token on the host), the venv, wake-model prefetch, and the bundled `assets/wake/*.onnx`.
   - **Venv fast path**: when `requirements.txt` is unchanged from the previous complete release, the venv is hardlink-cloned (`cp -al`, seconds, ~zero RAM — the fresh-build path once thrashed the 2-vCPU droplet into a freeze while the old brain held Whisper in RAM). Two clone hazards are corrected, both learned live: cloned `bin/*` shebangs still point at the old venv's python (rewritten in place; `sed -i` replaces the inode so the write can't bleed through a hardlink), and a plain `pip install -e` skips as "already satisfied" (forced via `python -m pip … --force-reinstall`).
   - **Self-import verification**: staging then proves, with `cwd=/` so `sys.path` can't mask it, that the staged venv imports `cortana` from **this release's** directory — and dies before the flip otherwise. A release that would run some other release's code cannot go live.
2. **GATE** — install config examples (existing files kept), then run the **new release's** offline doctor as the service user against the live `/etc/cortana`. A FAIL aborts before anything changes. First installs run `--first-install` (missing-runtime FAILs degrade to WARN) and take an enable-only path.
3. **FLIP** — atomically repoint `current` at the new release.
4. **RESTART** — `cortana-brain` always; `cortana-ears` only when the binary hash or unit file changed (otherwise it keeps buffering through the brain restart — no DAVE renegotiation).
5. **VERIFY** — settle, then check: doctor result, both units active, the Ears⇄Brain handshake, migrations at head. Any failure **auto-rolls back** to the previous release and leaves a `.verify_failed` marker on the bad one. `install.sh --rollback` flips back manually; old releases are pruned, keeping the rollback target.

---

## 18. Operations

| Concern | Specification |
|---|---|
| **Backups** | Nightly `sqlite3 .backup` → DigitalOcean Space, 30-day retention. The gazetteer tuning and alias table are the irreplaceable assets — the rest is reconstructible. |
| **Monitoring** | External uptime check against a systemd watchdog; hourly self-report to `#bot-health`. |
| **Logs** | `journalctl`, structured JSON, 14-day retention. Transcripts of triggered commands only. |
| **Secrets** | Token in `/etc/cortana/token`, mode 0600, loaded via `LoadCredential=` in the unit file. Never in the YAML, never in the environment, never in the repo. |
| **Updates** | Code: `cd /opt/aura-src && git pull && bash deploy/install.sh` — the staged converge-and-verify pipeline (§17.5) with auto-rollback. Config only: `systemctl reload cortana-brain` or `/reload`. Ears stays connected across Brain restarts. |
| **Accuracy review** | Weekly `command_log` query: confidence distribution, mismatch rate by system, per-pilot failure rate. Feeds §8 tuning. |
| **Operator slash commands** | `/botstatus`, `/doctor`, `/reload`, `/restart` (all admin-gated: Manage Guild or the FC role). Slash-only is fine — constraint 10 governs the voice→slash direction only. |

**`/botstatus`** — one phone-sized embed: Ears heartbeat state and age, voice-channel presence, STT health (watchdog latch / low-confidence degradation), wake pipeline counters and fault state, dialog sessions in flight, incidents in the last hour, fleet-ops mode, uptime, and the active alarm cards (§11.3). Named `botstatus` because `/status` is the voice QUERY intent's slash twin (the pilots' active-incident summary, §7) and cannot be repurposed.

**`/doctor`** — runs the **offline** preflight checks (`cortana.doctor`, §17) in a worker thread and replies with the PASS/WARN/FAIL table ephemerally, chunked when long. Online checks stay CLI-only (`python -m cortana.doctor --online`) — this command exists precisely for when Discord-adjacent things are broken, so it depends on nothing beyond one followup message.

**`/restart`** — the remote kick for a wedged brain. Answers the interaction, then trips the same graceful-shutdown path as SIGTERM; exit 0 is restartable (only 78/69 park the unit), so systemd `Restart=always` brings the process back in seconds. Ears is deliberately NOT restarted — it stays connected and buffers audio through the gap (no DAVE renegotiation). Wake models, STT, and the IPC handshake rebuild fresh on the way up, which clears most "she went deaf" states without SSH.

**`/reload`** — the slash twin of SIGHUP. Both doors call the **same** reload transaction (`cortana.reload.reload_all`, wired by the composition root): all three config files validated together, swapped all-or-nothing, hot/sighup keys applied, engines rebuilt, and the receipt (`ReloadResult.summary()`) returned to the admin and posted to `#bot-health`. The transaction also resets the STT watchdog latch and raises/clears `CONFIG_RESTART_PENDING` (§11.3). `/routing reload` and `/gazetteer reload|prune` run through this same transaction, so the three files can never drift apart, and a rejected file always comes back as a receipt line — never an unanswered spinner.

**Journal grammar** — every alarm transition logs with the alarm code as the event name: `journalctl -u cortana-brain -o cat | grep '"event": "EARS_DOWN"'` (or simply `grep EARS_DOWN`). Slash-command sync activity logs as `app_commands_synced` / `app_commands_sync_skipped` / `app_commands_sync_failed`; command-tree syncs run in the background, gated on a payload hash persisted in `app_state`, and never block or kill startup.

---

## 19. Privacy and consent

CORTANA puts a microphone-reading robot into a corp's social space. Getting this wrong destroys trust permanently, and no feature recovers it.

**The governing decision: CORTANA does not record anything.**

- Audio lives in a **RAM ring buffer only**. Never written to disk. Overwritten every 1.5 seconds.
- The capture buffer is freed the instant STT returns.
- CORTANA stores the **transcript of triggered commands**, never audio.
- Non-command speech is **never transcribed at all** — the wake-word gate means it never reaches the recogniser.

This is not only good manners. Laws on recording conversations vary by country and by state and may require notice to, or consent from, **every** participant. By never recording, CORTANA sidesteps the entire category rather than trying to comply with all of it.

Additionally:

- **Announcement on join.** CORTANA posts: *"🎙️ CORTANA is listening for commands. Audio is not recorded. `/optout` to exclude yourself."* Cadence is `discord.join_announcement`: `every` join, at most once per 24h (`daily`, the default — persisted in `app_state` so restart/rejoin churn cannot spam the channel), or `off` (the corp accepts that the consent posture is carried by `/optout` and the pinned docs instead).
- **`/optout`** drops that user's stream **inside Ears, before any processing and before it crosses the IPC boundary**. An actual drop, not a downstream filter.
- CORTANA reads Discord's `allow_voice_recording` voice flag as an additional consent signal.
- Callsign registration (§6.1) is keyed on the Discord user id attached to each utterance — it stores a chosen display name, never a voiceprint or anything derived from audio.
- A plain-language privacy note is pinned in the channel. Discord's Developer Policy expects a privacy policy regardless, and an honest one here is four sentences.

**Introduce CORTANA to the corp with this section, not with the feature list.** If half the corp is uncomfortable, that conversation is cheaper before the droplet is provisioned than after.

---

## 20. Resilience and degradation

Because voice receive is undocumented and can break without warning (§2.2), CORTANA is engineered to **survive the loss of its own headline feature**.

| Failure | Detection | Response |
|---|---|---|
| Discord breaks voice receive | No `VoiceTick` from anyone for 60s while ≥2 unmuted humans are in channel | Post **⚠️ Voice offline — use `/under-attack`, `/help-me` and `/hostiles`**; keep retrying. **Every slash command and the entire incident engine keep working.** |
| Voice gateway 4017 | Close code on connect | Loud alert to `#bot-health` — the Songbird version needs updating |
| DAVE session crash | Session exception | Rebuild, exponential backoff, cap retries, then degrade |
| DAVE session **wedged** (stuck `PENDING` — no exception, live incident: deaf-and-mute indefinitely, receive path looked healthy) | Ears-side decode watchdog: packets flowing with ZERO successful decodes for 30s (`VoiceTick` speakers all `decoded_voice: None`) — one healthy decode resets, silence is no evidence, a fresh driver session restarts the clock | Ears sends `ears_restarting(decode_wedge)` best-effort and exits 70 → systemd `Restart=always` brings it back in seconds with a fresh join + DAVE handshake (exactly the manual fix from the incident). Brain logs `ears_self_restart`, buffers nothing (the wedged audio was garbage), and replays the join |
| STT worker hang | `stt.watchdog_s` (default 15s) watchdog on **queue-head service time** — decodes queue (depth 3, drop-oldest) behind one serialized worker, so overload shows up as queue time and never masquerades as a hang | Respawn (max 2 consecutive), speak *"say again"*; past the cap latch **STT degraded** (refuse decodes, alert `#bot-health`) until reload/restart |
| Sustained low confidence | 10 consecutive low-tier results | Degrade and alert — something is wrong with the audio path |
| Wake model build fails (bad `wake.model`, broken upgrade) | The model pool latches **faulted**; the wake stage counters (frames seen → scored → inferences → hits) reported to `#bot-health` show frames flowing with nothing scored | Score 0.0 with no per-chunk retry storm, alert `#bot-health`; a wake config change or restart clears the latch |
| Brain down | Ears' socket write fails | Ears buffers 60s of frames, speaks *"system degraded"*, reconnects |
| Ears down | Brain heartbeat miss | Post the degraded notice; text path unaffected |
| Droplet down | External uptime check | `Restart=always` + a page |

**The load-bearing invariant: every voice command has a slash-command twin hitting the same engine.** The voice path is a fast front-end to a system that is complete without it. This is what makes CORTANA survivable on a platform that never promised to support half of it. The parity surface is total: the freeform intel relay's slash twin is `/relay` (→ `IncidentEngine.broadcast`, the same `decide_mentions` authority), and spoken colour codes / group targeting ride the `code:` / `audience:` parameters on the report commands (§7) — severity and audience survive a voice outage along with the commands themselves.

---

## 21. Performance budget

Measured on the specified droplet (§17.1).

| Stage | Budget |
|---|---|
| Songbird jitter buffer (playout delay) | 40–80 ms |
| 48k→16k decimation + IPC | <5 ms |
| VAD + wake word detect | 100–200 ms |
| Capture window (silence endpointing) | 400 ms |
| STT — Whisper `small` int8, ~3s clip, dedicated cores | 400–1000 ms |
| Alias lookup / phonetic match / priors / routing | <50 ms |
| Discord REST POST | 100–300 ms |
| **Speech ends → incident card posted** | **≈ 1.0–2.0 s** |
| Piper synthesis + playback start | +300–500 ms |

Against 20–40 seconds for the manual path. That is the whole thesis, restated as arithmetic.

---

## 22. Security

- No inbound ports. CORTANA dials out only; `ufw` denies all incoming except SSH.
- IPC socket is `0660 root:aura` on a `tmpfs` — not reachable off-box.
- Token via `LoadCredential=`, never in config, environment, or repo.
- Least-privilege bot permissions (§17.4). No Administrator.
- `aura` runs as an unprivileged `nologin` user.
- systemd hardening on both units: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `ReadWritePaths=/var/lib/cortana /run/cortana`.
- Mention capability is gated on the `@Pilot` role, so a compromised member account cannot mass-ping.

---

## 23. Sources

- Discord — Voice Connections: https://docs.discord.com/developers/topics/voice-connections
- DAVE protocol whitepaper: https://daveprotocol.com/ · https://github.com/discord/dave-protocol
- libdave: https://github.com/discord/libdave
- Discord — Bringing DAVE to All Platforms: https://discord.com/blog/bringing-dave-to-all-discord-platforms
- Discord — E2EE enforcement / close code 4017: https://support.discord.com/hc/en-us/articles/38749827197591-A-V-E2EE-Enforcement-for-Non-Stage-Voice-Calls
- Discord — minimum client versions for voice: https://support.discord.com/hc/en-us/articles/38025123604631-Minimum-Client-Version-Requirements-for-Voice-Chat
- Songbird v0.6.0 "Hoopoe" (DAVE): https://github.com/serenity-rs/songbird/releases
- Songbird docs (`receive`, `VoiceTick`): https://docs.rs/songbird/latest/songbird/
- @discordjs/voice DAVE receive defect: https://github.com/discordjs/discord.js/issues/11419
- discord.js voice installation (davey): https://discordjs.guide/voice
- py-cord 4017: https://github.com/Pycord-Development/pycord/issues/3135
- discord-ext-voice-recv: https://github.com/imayhaveborkedit/discord-ext-voice-recv
- openWakeWord: https://github.com/dscripka/openWakeWord
- Porcupine wake-phrase guidance: https://picovoice.ai/docs/faq/porcupine/
- Piper TTS: https://github.com/OHF-Voice/piper1-gpl
- DigitalOcean droplet pricing: https://www.digitalocean.com/pricing/droplets
- Discord Developer Policy: https://support-dev.discord.com/hc/en-us/articles/8563934450327-Discord-Developer-Policy

---

*§2.4 (library support) and §17.1 (pricing) describe a landscape that was moving as of July 2026. Confirm both against the live sources above before provisioning.*
