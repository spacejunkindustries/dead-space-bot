# AURA — Voice-Activated Fleet Intel Bot

**Game Design Document**
EVE Echoes corp utility bot · self-hosted on DigitalOcean
Version 1.0 · July 2026

---

## 1. Product definition

AURA sits in your corp's Discord voice channel and listens for spoken reports. A player under attack says:

> **"Aura Command, hostiles Otanuomi, three battleships"**

Roughly a second and a half later, AURA posts a live incident card in `#intel-alerts`, mentions the roles that subscribe to that system, and speaks back into voice:

> *"Hostiles Otanuomi, pinged home defense."*

Corpmates tap **🚀 On my way**. AURA speaks again:

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

AURA's path: **~1–2 seconds from the moment you stop talking.** That gap is the entire product.

### 1.2 Operating envelope

AURA is built for the **solo and small-gang player** — ratting, mining, or roaming, jumped without warning, hands full, no time to type.

It is explicitly **not** built to run during structured fleet ops with an FC on comms. People shouting at a bot would step all over fleet command. AURA ships with a **fleet-ops mode** (§11.4) that restricts voice triggering to the FC role, and corps are expected to use it. Design for the lonely ratter.

### 1.3 Scope

AURA is a complete corp utility bot. It ships with:

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

Playing audio into a voice channel is a documented, first-class Discord feature supported by every voice library. AURA's spoken back-channel carries no platform risk.

### 2.2 Listening is undocumented and always has been

Voice **receive** is not documented by Discord. Every library that implements it did so by reverse engineering, and Discord has stated it is unlikely to ever officially support or document the feature. It works, it is not guaranteed, and it can break on a Discord-side change with no notice.

This is a permanent structural property of the platform, not a defect in AURA. §20 specifies how AURA absorbs it: **every voice capability has a slash-command twin backed by the same engine**, so a broken receive path costs AURA its speed advantage and nothing else.

### 2.3 DAVE: end-to-end encryption is mandatory

Discord completed its rollout of the **DAVE protocol** (Discord Audio & Video End-to-End Encryption) in March 2026. Since **March 2, 2026**, any client or application without DAVE support is rejected by the voice gateway with **close code 4017**. There is no opt-out, and the unencrypted fallback code path has been removed.

Consequences that shape this design:

- AURA must join the MLS group and negotiate DAVE simply to enter a voice channel.
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
- Per-user streams keyed by SSRC, mapped to user IDs via speaking-state events. AURA always knows *who* said something.
- Symphonia in-process; ffmpeg removed entirely. Light footprint.
- Deadline-aware scheduler — ~660 concurrent Opus-passthrough calls on a single thread on a Ryzen 5700X. AURA needs one.

### 2.5 Other platform facts AURA depends on

- **Self-deaf must be off.** A self-deafened bot receives nothing and raises no error. This is the single most common cause of a silent voice bot.
- Bots respect the voice channel user limit unless granted `MOVE_MEMBERS`.
- Discord exposes an `allow_voice_recording` voice flag per member — whether that user has consented to being clipped. AURA reads it as a consent signal (§19).
- Stage channels are excluded from DAVE and remain unencrypted. Not used by AURA.
- The `GUILD_VOICE_STATES` intent is required.
- Discord's client-side voice activity gating means AURA receives packets **only from users actively transmitting**. Idle listeners cost nothing.

### 2.6 Python is not excluded — only Python *voice* is

discord.py's gateway, slash commands, components, roles, and REST are unaffected by the DAVE change. Only `VoiceClient` is broken. AURA therefore uses Rust exclusively for the audio socket and Python for everything else — which is where the speech and phonetics ecosystem lives.

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
│                                      /var/lib/aura/aura.db    │
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
| `audio/wake.py` | openWakeWord instances, per-user state, score thresholds |
| `audio/capture.py` | Per-user capture state machine: pre-roll ring buffer → wake hit → capture → endpoint → emit; owns the wake refractory period |
| `audio/stt.py` | `Transcriber` protocol; faster-whisper (default) and whisper.cpp HTTP backends; gazetteer prompt biasing |
| `nlu/grammar.py` | Intent extraction, group-alias extraction, detail capture |
| `nlu/gazetteer.py` | System load, region pruning, adjacency graph, jump-distance BFS with memo |
| `nlu/phonetics.py` | Double Metaphone + Levenshtein scoring, alias-table lookup, context priors, confidence tiers |
| `core/incidents.py` | Incident lifecycle, dedupe folding, staleness sweep, message rendering |
| `core/callsigns.py` | Pilot callsign registry: register/unregister/who-am-I, sync lookup mirror for renders |
| `core/personal_pings.py` | Personal ping subscriptions (§10.3): capped per-user store, sync mirror for routing |
| `core/routing.py` | Subscription evaluation, role union, escalation, quiet hours, personal-ping user mentions |
| `core/discipline.py` | Per-user cooldowns, global circuit breaker, flood control |
| `core/db.py` | SQLite access, migrations, backup hook |
| `dsc/bot.py` | discord.py client, intents, persistent view registration |
| `dsc/views.py` | Incident buttons, ambiguity resolver, subscription picker |
| `dsc/cogs/intel.py` | `/hostiles`, `/under-attack`, `/help-me`, `/camp`, `/clear`, `/status`, `/cancel` |
| `dsc/cogs/subs.py` | `/subscribe`, `/mysubs`, `/pingme`, `/mypings`, `/pingme-clear`, `/optout`, `/mute-voice`, `/register`, `/unregister`, `/whoami` |
| `dsc/cogs/ops.py` | `/timer`, `/formup`, `/rollcall`, `/jumps` |
| `dsc/cogs/admin.py` | `/routing`, `/gazetteer`, `/health`, `/fleetmode` |
| `dsc/cogs/help.py` | `/help` — interactive help topics; the slash twin of voice "help" |
| `tts.py` | Piper subprocess, raw→WAV wrapping, utterance queue, length cap |
| `health.py` | Heartbeats, degradation detection, `#bot-health` reporting |
| `voice_gateway.py` | Voice-state watch, auto-join/leave, Ears join/leave commands |

### Assets

| Path | Contents |
|---|---|
| `/opt/aura/models/wake/` | openWakeWord ONNX chain (melspec → embedding → wakeword) |
| `/opt/aura/models/whisper/` | Whisper `small` int8 CTranslate2 weights |
| `/opt/aura/models/piper/` | Piper voice `.onnx` + `.onnx.json` |
| `/etc/aura/aura.yaml` | Main configuration (§16) |
| `/etc/aura/routing.yaml` | Subscription rules (§12) |
| `/var/lib/aura/aura.db` | SQLite |

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
       ├─► openWakeWord ──── "Aura Command"?
       │        │ hit
       │        ▼
       │   capture window opens
       │   (300ms pre-roll + speech until 400ms silence, hard cap 6s)
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

**Wake acknowledgement.** The instant the capture window opens, AURA tells the pilot it is listening — so they know to start talking, not repeat the wake word. The form is `wake.ack`: `voice` speaks *"Go ahead."*, `beep` plays an instant tone, `none` is silent. `beep` is the default because the tone is instant, while a spoken cue costs one Piper synthesis; corps that want AURA to talk back set `voice`. The cue plays at ALERT priority (jumping the queue) and, since AURA never captures its own playback, never bleeds into the utterance being recorded.

### 5.1 Wake word

**Engine: openWakeWord** (Apache 2.0). Free, self-hosted, no per-seat licensing, and it benchmarks competitively against the leading commercial engine. Custom phrases are trained from a synthetic TTS pipeline; the trained ONNX chain ships in `/opt/aura/models/wake/`.

*Porcupine is the mature commercial alternative — type-to-train, instant custom words — but its free tier is scoped to personal use and commercial licensing starts around $6k/yr. A 50-pilot corp bot is not a comfortable fit for the free tier. AURA does not require it.*

### 5.2 The wake phrase

Two constraints govern the choice:

1. **At least six phonemes**, with diverse sounds. Short phrases false-fire constantly.
2. **Nobody says it by accident in fleet chat.** This rules out most EVE vocabulary — "Concord", "capsuleer", and "Aura" alone are said constantly on comms and would fire all night.

| Phrase | Verdict |
|---|---|
| "Aura" | ❌ ~3 phonemes. Fires on *hour*, *or a*, *aura*. |
| **"Aura Command"** | ✅ ~9 phonemes, distinctive, never said naturally. **Default.** |
| "Hey Overseer" | ✅ Good phonetics, low collision. Supported alternative. |

The phrase is configurable. The 6-phoneme / no-collision rule is not optional — it is the difference between a tool and a nuisance.

### 5.3 Speech recognition

**Default backend: faster-whisper, `small`, int8, CPU.** ~3.4% WER on clean English, ~2GB RAM, and sub-second on the 2–4s clips AURA actually feeds it. CTranslate2 releases the GIL during inference, so it runs in a thread pool without blocking the event loop.

**Alternative backend: whisper.cpp HTTP server.** Selectable via `stt.backend`. Its native quantized kernels are more predictable on CPU-only hosts; the tradeoff is an extra process. Both implement the same `Transcriber` protocol.

**Gazetteer biasing** is applied on every call: the active system list is passed as Whisper's `initial_prompt`, pulling decoding toward real system names instead of English word salad.

---

## 6. Voice command reference

The grammar is **fixed and rigid**. No LLM sits in this loop — it would be slower, cost money per fight, hallucinate system names, and fail in ways nobody can debug at 02:00. A fixed grammar is deterministic, instant, and free.

```
<wake> <intent> [<system>] [<group>] [<detail>...]
```

### 6.1 Intents

| Spoken | Intent | Severity | Default notification |
|---|---|---|---|
| "hostiles \<system\>" / "reds \<system\>" / "neuts \<system\>" | `HOSTILE_SPOTTED` | medium | role mention |
| "under attack \<system\>" / "tackled \<system\>" / "point on me \<system\>" | `UNDER_ATTACK` | **high** | role mention + `@here` |
| "need help \<system\>" / "need backup \<system\>" | `ASSIST_REQUEST` | **high** | role mention + `@here` |
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

`PING_ME` (§10.3) reuses the type vocabulary above: *hostiles/reds/neuts* → `HOSTILE_SPOTTED`, *gate camp(s)* → `GATE_CAMP`, *under attack/attacks/tackled* → `UNDER_ATTACK`, *need help/need backup/assist request(s)* → `ASSIST_REQUEST`, and *"anything"/"everything"/"all"* (or no type word at all) → all four. The optional system window resolves through the same phonetic pipeline as reports (§8.2); anything below HIGH tier is treated as unresolved — a subscription silently scoped to the wrong system would never fire, so AURA answers *"Say again the system."* instead of guessing. No system means the subscription covers all systems. The recognised types travel to the engine encoded in `detail` (comma-separated `Intent` values), shared by the `/pingme` twin.

The callsign commands are a **name registry keyed on the Discord user id** Ears already attaches to every utterance (the SSRC→user map, §15). There are no voice biometrics and nothing derived from the audio — identity comes from Discord, the callsign is just a display name (§19 posture unchanged). The spoken callsign is the cleaned post-intent remainder: filler stripped, title-cased, markdown/mention characters removed, capped at 32 characters. `/register` (the typed twin) stores the callsign exactly as typed, which is also how a pilot fixes an STT misspelling.

### 6.2 Group targeting

| Spoken suffix | Effect |
|---|---|
| "miners only" | Routes to `@Miners` alone |
| "defense only" | Routes to `@Home-Defense` alone |
| "all hands" | `@here` + every subscribed role, regardless of severity |

Group aliases are configurable but deliberately few. Every alias added is another token the recogniser can confuse with a system name. Four is the recommended ceiling.

### 6.3 Detail

Anything after the system and group is captured verbatim into the incident body: *"three battleships"*, *"camping the gate"*, *"I'm in structure"*. It does not parse. It is a human note for humans.

### 6.4 Spoken threat colours

The card labels (CODE RED / CODE ORANGE / CODE YELLOW, §9.1) are also **input**: a pilot can speak the colour to set or override severity.

- **Inline** — *"code red, hostiles in UMI"*: the report parses normally and carries `severity=high`; the card renders CODE RED and, when `high` is in `here_on_severity`, fires the colour-based `@here` (§11.3). The colour phrase is stripped before intent matching, so *"code red"* can never be misread as a "reds" sighting.
- **Standalone (dialogue)** — *"code orange"* alone: AURA replies *"Code orange. Go ahead."*, reopens a wake-free capture window (the §8.3 mechanism), and the pilot's next utterance — report or freeform relay — inherits the severity. An inline colour on the follow-up wins over the opener.
- **Relays** — a colour on a freeform relay colours the relay card and rides the same `here_on_severity` escalation: *"code red, blop fleet inbound"* pings like any CODE RED.

Mapping: red → high, orange → medium, yellow → none/info. A leading *"report"* and a trailing *"end report"* / *"end of report"* / *"end transmission"* are radio-procedure framing and are stripped (*"report, I've been tackled in UMI, end report"*).

### 6.5 Examples

```
"Aura Command, hostiles Otanuomi, three battleships"
"Aura Command, tackled in Kisogo, need help"
"Aura Command, gate camp Otanuomi, miners only"
"Aura Command, clear Otanuomi"
"Aura Command, timer Kisogo four hours"
"Aura Command, form up Otanuomi fifteen minutes"
"Aura Command, status"
"Aura Command, help"
"Aura Command, cancel"
"Aura Command, register Space Junkie"
"Aura Command, ping me for gate camps in Otanuomi"
```

Fifteen commands. Short enough that pilots remember them under fire, which is the only time they matter.

---

## 7. Slash command reference

Full parity. Every voice command routes to the same engine.

| Command | Purpose |
|---|---|
| `/hostiles system detail` | Report a sighting |
| `/under-attack system detail` | You are under attack — tackled or taking damage |
| `/help-me system detail` | High-severity assist request |
| `/camp system detail` | Report a gate camp |
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
| `/optout` | Exclude my audio from AURA entirely |
| `/mute-voice` | Stop AURA speaking to me |
| `/register callsign` | Register my pilot callsign, exactly as typed (fixes STT misspellings) |
| `/unregister` | Delete my registered callsign |
| `/whoami` | Show my registered callsign |
| `/routing` | *(admin)* Manage subscription rules |
| `/gazetteer` | *(admin)* Reload / inspect / prune systems |
| `/fleetmode` | *(admin)* Restrict voice triggering to FC role |
| `/health` | *(admin)* Pipeline status, STT confidence, incident counts |

---

## 8. System name resolution

This subsystem decides whether AURA succeeds or fails. It is specified in the most detail because it deserves it.

EVE system names are phonetically hostile — *Otanuomi*, *Kisogo*, *Alenia*, *Hulmate*, *Tannolen*. Generic English STT shreds them. And **naming the wrong system is worse than silence**: it sends the response fleet twelve jumps the wrong way while the reporter dies.

### 8.1 The gazetteer: seed wide, scope at runtime

Two layers. The **seed** fills the `systems` + `system_adjacency` tables from
the EVE static data export (Echoes uses New Eden names); the **scope rules** in
`gazetteer.yaml` pick, at runtime, which of those systems are *active* — the
set AURA will match a transcript against. The seed is wide on purpose so the
scope can point anywhere without re-seeding; accuracy comes from the scope, not
the seed.

**Seeding — `python -m aura.nlu.seed`.** An operator CLI (`aura/nlu/seed.py`)
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
  lower than a tight scoped gazetteer. AURA leans on the rest of §8 to close the
  gap rather than on a narrow set:

  - the **confidence tiers and confirm-flow** (§8.3) — AURA never silently
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

### 8.2 Resolution pipeline

```
raw transcript
   │
   ├─► alias table lookup ────────────► exact hit? done.       [learned, §8.5]
   │
   ├─► normalise (lowercase, strip filler, expand numerals)
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

### 8.3 Confidence tiers — AURA never silently guesses

| Tier | Rule | Behaviour |
|---|---|---|
| **High** | `top1 ≥ 0.80` and `top1 − top2 ≥ 0.12` | Post immediately. Speak *"Hostiles Otanuomi, pinged."* |
| **Medium** | `top1 ≥ 0.55` | **Post anyway**, flagged uncertain, with buttons `[Otanuomi] [Kisogo] [Wrong — fix]`. Speak *"Hostiles Otanuomi — say again to confirm."* Speed beats certainty when a pilot is in structure; get the ping out and let humans correct it. |
| **Low** | below | Do not post. Speak *"Say again the system."* Reopen the capture window for 4s. A bare system name spoken into the reopened window is re-bound to the rejected command's intent and resolved as its system. |

### 8.4 Context priors

Candidates are reweighted by what is plausible *right now*. A fleet fight is spatially and temporally clustered — AURA exploits that.

| Prior | Rule |
|---|---|
| **Recency** | Systems with incidents in the last 10 minutes get a strong boost. If three pilots just pinged Kisogo, *"kissogo"* is Kisogo. |
| **Proximity** | Systems within a few jumps of an active incident outrank systems forty jumps away. |
| **Reporter history** | This pilot has reported from Otanuomi six times this week. That is a prior. |
| **Home bias** | Home and adjacent systems carry a standing boost. Inactive when `gazetteer.home_system` is `null` (nomadic corps, §8.1) — the prior simply does not fire. |

Applied as a cheap multiplicative reweighting over the top-8 base-score candidates — wider than the final top-3 on purpose, so a strong prior can promote a lower-ranked but spatially plausible candidate into the top-3. Weights live in `aura.yaml`.

### 8.5 Alias learning

Every time a pilot taps `[Wrong — fix]` and picks the correct system, AURA writes `(raw transcript) → (system_id)` into the alias table, which is consulted **before** phonetic matching on every subsequent utterance.

Within a month of real use, your corp's specific accents, specific mics, and specific noisy rooms are baked in. **This is the highest-leverage component in the entire system and it is roughly forty lines of code.**

### 8.6 Recognition error is a normal operating condition

Pilots use phone mics, in noisy rooms, with a game running, stressed and talking fast. AURA is engineered on the assumption that **the transcript is sometimes wrong** — not as a limitation to apologise for, but as a fact the design absorbs. The confirmation loop, the correction buttons, the confidence tiers, and the alias table are not garnish; they are the mechanism by which an imperfect signal produces a reliable tool.

**The freeform relay is confidence-gated.** A transcript that matched no intent posts to the intel channel verbatim (the catch-all) — but only when Whisper's `avg_logprob` clears `stt.relay_min_logprob`. Below that, the transcript is treated as decoded noise ("Rens, Rens, Rens" hallucinated from silence) and AURA says *"Say again the system."* instead of posting garbage. Recognised commands are **never** gated — a distress call always posts. Stuttered three-plus word repeats in relay text collapse to one word, and every relay logs its confidence to `command_log` so the threshold is tuned from data. A successful relay is acknowledged with a spoken *"Relayed."* — without the ack, pilots repeat themselves, and every repeat is another card and another STT decode.

---

## 9. Incident engine

AURA is not a ping bot. It is an **incident tracker that happens to be voice-driven**. This is the difference between a toy and something a corp runs for years.

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
- *"Aura Command, clear Otanuomi"* edits that card to ✅ **RESOLVED** and greys it out.
- Someone scrolling back reads **state**, not archaeology.
- No updates for 20 minutes → auto-marked **STALE**, silently.

### 9.2 Dedupe rule

> Same system + same type + within 90 seconds → **fold into the existing incident**, increment the reporter count, **do not re-mention**.

This one rule is the primary defence against notification fatigue.

### 9.3 Response loop

Every card carries:

`[🚀 On my way]` `[👀 Watching]` `[❌ Can't respond]`

On the first **On my way**, AURA speaks into voice — naming the responder when it can: the registered callsign wins, then the clicker's guild display name (*"Space Junkie responding to Otanuomi."*), and only when neither is known does it fall back to the count (*"Two responding to Otanuomi."*).

That closes the loop. A pilot in structure gets an audible answer without touching their phone. It is the cheapest feature in the document and the one the corp will actually love.

Buttons use persistent views with the incident ID encoded in `custom_id`, so they survive a Brain restart.

---

## 10. Routing and subscriptions

Subscriptions are built on **Discord roles**. Roles are native, respect per-user notification settings, are already understood by every member, and mean AURA does not reinvent a permission system.

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

*"Aura Command, ping me for gate camps in Otanuomi."* A personal ping is a **user mention, not a role** — the role model above is unchanged; personal pings are additive.

- Stored in `personal_pings` (§14): incident types + an optional system (`NULL` = all systems). Capped per user by `discipline.personal_pings_max` (default 10); at the cap AURA answers *"Ping limit reached."*
- Matching = incident type ∈ the subscription's types ∧ (no system, or the incident's system). Matching subscribers' mentions are **appended to the mention line** of the incident card in `#intel-alerts` — never a separate message.
- **Same discipline, no exceptions.** Personal pings ride the exact mention path of §11: the reporter's per-user cooldown, the circuit breaker, and quiet-hour role logic all suppress them together with the roles; a dedupe fold (§9.2) never re-pings personal subscribers; the incident's own reporter is never personally pinged for their own report; and they can **never** cause `@here` (constraint 11 untouched).
- Managed by voice (`PING_ME` / `PING_ME_CLEAR`, §6.1) and the slash twins `/pingme`, `/mypings`, `/pingme-clear` — all through the same engine entry point (constraint 10).

---

## 11. Notification discipline

If AURA is annoying for one week, the corp mutes `#intel-alerts` and the project is dead — no matter how good the speech recognition is. This subsystem gets as much engineering weight as the recogniser.

### 11.1 Layered defences

1. **Dedupe window** (§9.2) — one incident, one mention.
2. **Per-user cooldown** — 30s between mentions from the same pilot. A panicking player cannot ping six times.
3. **Escalation discipline** — `@here` is reserved for `UNDER_ATTACK` and `ASSIST_REQUEST`. Sightings never `@here`. Ever.
4. **Permission gate** — only members holding `@Pilot` can trigger a mention. The new guy cannot experiment at 03:00.
5. **Global circuit breaker** — more than *N* mentions in *M* minutes → AURA stops mentioning, posts **flood control active**, and keeps logging incidents silently. Something is wrong; do not amplify it.
6. **Quiet hours** per role.

### 11.2 Two channels

| Channel | Contents |
|---|---|
| `#intel-live` | Every incident that mentions nobody. The quiet feed, for people who want it. |
| `#intel-alerts` | Only incidents that mention a role. |

Let people choose their own volume. An incident card lives in exactly one of
the two channels — one incident is one message, edited in place (§9.1), never
mirrored or reposted.

### 11.3 Health channel

`#bot-health` receives hourly self-reports: pipeline status, STT confidence distribution, incident counts, mention counts, wake-word false-accept estimate.

### 11.4 Fleet-ops mode

`/fleetmode on` restricts voice triggering to the FC role for the duration of a structured op. Slash commands remain open to everyone. This exists because §1.2 is real: during a fleet fight, twenty pilots talking to a bot is worse than no bot.

---

## 12. Voice back-channel

AURA speaking back is not decoration — it is what lets a pilot keep their eyes on the game. It is also the *easy* half of the voice problem, since the send path is fully supported (§2.1).

**Engine: Piper.** Local neural TTS, VITS models exported to ONNX. Real-time on a Raspberry Pi 5 with no GPU, roughly an order of magnitude faster than real time on a normal CPU. Voices are tens of megabytes. No API, no bill, no dependency.

Current release v1.4.2 (April 2026), maintained by the Open Home Foundation at `OHF-Voice/piper1-gpl`.

> **License note:** the original Rhasspy repo was MIT and is archived. The maintained fork is **GPL-3.0**. For a self-hosted corp bot this is immaterial — nothing is distributed. AURA invokes Piper as a separate binary over a subprocess boundary rather than linking it, which keeps the question closed even if the bot is later shared with other corps.

### 12.1 Utterance catalogue

Short. Always short. AURA is talking over a fight.

| Event | Utterance |
|---|---|
| Wake acknowledged (`wake.ack: voice`) | *"Go ahead."* |
| Ping sent | *"Hostiles Otanuomi, pinged."* |
| Ping sent, scoped | *"Hostiles Otanuomi, pinged home defense."* |
| Ambiguous system | *"Hostiles Otanuomi — say again to confirm."* |
| Unresolved system | *"Say again the system."* |
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

- **Never speak over a high-severity report in progress.** If VAD reports active speech, queue.
- Duck to 60% volume. AURA is not the FC. The duck level and talk-over suppression are fixed playback mechanics in Ears (`ears/src/playback.rs`), not config knobs.
- Hard cap **3 seconds** per utterance. If it does not fit, it goes to the channel instead.
- `/mute-voice` per user. Some pilots will hate this. They can silence it without leaving.

### 12.3 Audio path

Piper emits raw s16le at the model's native rate. Brain wraps it in a WAV header in memory and ships the bytes to Ears, where Songbird's Symphonia layer parses the header and resamples to 48kHz internally. **No resampling code exists in Brain**, and no temporary files are written.

---

## 13. Fleet ops features

Beyond intel, AURA carries the utilities a corp actually asks for. Each is voice-shaped and slash-backed.

| Feature | Voice | Value |
|---|---|---|
| **Structure timers** | *"Aura Command, timer Kisogo four hours"* | Schedules a mention ahead of a structure coming out. Enormous value in EVE, and a natural voice command — you're looking at the timer in-game right now. |
| **Form-ups** | *"Aura Command, form up Otanuomi fifteen minutes"* | Posts an op card with RSVP buttons and a countdown. |
| **Roll call** | `/rollcall` | Who's in voice, who's subscribed, who's responding. |
| **Jump distance** | *"Aura Command, jumps to Jita"* | Spoken reply, no post. BFS over the adjacency graph. |

---

## 14. Data model

SQLite. One corp, low write volume, no concurrency pressure — a managed database line-item here would be waste.

```sql
-- ── gazetteer ────────────────────────────────────────────────
-- Populated by `python -m aura.nlu.seed` from the EVE SDE (k-space New Eden,
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
    raw_system_text    TEXT               -- transcript window that named the
                                          -- system; alias key for [Wrong — fix]
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
CREATE TABLE timers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    system_id   INTEGER REFERENCES systems(id),
    fires_at    TEXT NOT NULL,
    note        TEXT,
    created_by  INTEGER NOT NULL,
    fired       INTEGER NOT NULL DEFAULT 0
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

---

## 15. IPC protocol

Framed messages over `/run/aura/aura.sock`. **Brain binds; Ears connects and reconnects with backoff.** This ordering matters: it means Ears buffers when Brain restarts, rather than the reverse.

```
Frame:  [4-byte BE length][1-byte type][body]

type 0x01  JSON control   (UTF-8)
type 0x02  Audio          Ears→Brain
           [8B user_id LE][8B guild_id LE][i16 LE PCM, 16kHz mono]
type 0x03  TTS            Brain→Ears
           [8B guild_id LE][1B priority][WAV bytes]
```

### Control messages

```jsonc
// Ears → Brain
{ "t": "hello",     "version": "1.0" }
{ "t": "speaking",  "user_id": "…", "guild_id": "…", "state": "start" }
{ "t": "left",      "user_id": "…", "guild_id": "…" }
{ "t": "heartbeat", "ticks": 15021, "active_ssrcs": 4, "connected": true }

// Brain → Ears
{ "t": "join",    "guild_id": "…", "channel_id": "…" }
{ "t": "leave",   "guild_id": "…" }
{ "t": "optouts", "user_ids": ["…", "…"] }   // enforced in Ears, pre-IPC
```

---

## 16. Configuration reference

`/etc/aura/aura.yaml` — hot-reloaded on `SIGHUP`.

```yaml
discord:
  token_file: /etc/aura/token          # 0600, root:aura
  guild_id: 000000000000000000
  channels:
    intel_alerts: 000000000000000000
    intel_live:   000000000000000000
    health:       000000000000000000
  roles:
    pilot: 000000000000000000          # gate: may trigger mentions
    fc:    000000000000000000          # gate under fleetmode
  watch_voice_channels: [000000000000000000]
  auto_join: true                       # join when a pilot enters, leave when empty

wake:
  model:  /opt/aura/models/wake/aura_command.onnx   # the phrase is baked into the model
  threshold: 0.55
  refractory_ms: 2000
  ack: beep                             # voice = speak "Go ahead." | beep = tone | none

capture:
  preroll_ms: 300
  endpoint_silence_ms: 400
  max_utterance_ms: 6000
  vad_aggressiveness: 2                 # webrtcvad 0–3

stt:
  backend: faster-whisper               # or: whisper-cpp
  model: small
  compute_type: int8
  cpu_threads: 2
  bias_with_gazetteer: true
  whisper_cpp_url: http://127.0.0.1:8080/inference
  relay_min_logprob: -0.9               # freeform-relay confidence gate (§8.6)

matching:
  phonetic_weight: 0.6
  text_weight: 0.4
  tiers:
    high_min: 0.80
    high_margin: 0.12
    medium_min: 0.55
  priors:
    recency_weight: 0.35
    recency_window_min: 10
    proximity_weight: 0.25
    proximity_max_jumps: 5
    reporter_history_weight: 0.15
    home_weight: 0.10

incidents:
  dedupe_window_s: 90
  stale_after_min: 20
  cancel_window_s: 30

discipline:
  user_cooldown_s: 30
  circuit_breaker:
    max_mentions: 12
    window_min: 10
  personal_pings_max: 10       # per-user cap on /pingme subscriptions (§10.3)

tts:
  enabled: true
  voice: /opt/aura/models/piper/en_US-amy-medium.onnx
  binary: /usr/local/bin/piper
  max_utterance_s: 3
  # Ducking (60%) and talk-over suppression are fixed playback mechanics in
  # Ears (§12.2) — deliberately not tunables here.

gazetteer:
  file: /etc/aura/gazetteer.yaml
  home_system: Otanuomi        # null/empty = no home system → home-bias prior
                               # off (nomadic corps, §8.1/§8.4)
  include_all: false           # nomadic override, mirrors gazetteer.yaml's flag;
                               # either being true activates the whole seeded map

ipc:
  socket: /run/aura/aura.sock
  # Ears' outbound ring size (buffer_seconds) lives in Ears' own config,
  # /etc/aura/ears.yaml (token_file, socket_path, buffer_seconds — see
  # ears/ears.yaml.example): the ring must survive Brain restarts, so Brain
  # cannot own that knob.

health:
  report_interval_min: 60
  voice_silence_alarm_s: 60
```

`/etc/aura/gazetteer.yaml` — scope rules over the SDE-seeded tables (§8.1). The
tables themselves are filled by `python -m aura.nlu.seed` (k-space New Eden),
which this file then scopes at runtime:

```yaml
# false (default) = scoped by the rules below (home-region corps).
# true = nomadic mode: the entire seeded map is active, regions/within_jumps_of
# are ignored, exclude still removes. Set home_system: null in aura.yaml too.
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

**Region:** choose the datacenter nearest the **Discord voice region your corp actually lands in**, not nearest your players. AURA's RTT to Discord's voice server is what sits in the audio path; your pilots' RTT to AURA is irrelevant because they never talk to it directly.

### 17.2 Host layout

```
Ubuntu 24.04 LTS
├── systemd
│   ├── aura-ears.service     Rust binary,  Restart=always, RestartSec=5
│   └── aura-brain.service    Python,       Restart=always, RestartSec=5
├── /opt/aura/
│   ├── bin/aura-ears
│   ├── brain/                 venv + package
│   └── models/{wake,whisper,piper}/
├── /etc/aura/{aura.yaml,routing.yaml,gazetteer.yaml,token}
├── /var/lib/aura/aura.db
├── /run/aura/aura.sock        (tmpfiles.d, mode 0660, root:aura)
├── user: aura  (nologin, owns runtime dirs)
└── ufw: deny incoming, allow SSH only — AURA opens no listening ports
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
- `openwakeword` is installed `--no-deps`: its Linux dependency pin `tflite-runtime` has no wheels for Python ≥3.12, and AURA uses only the ONNX inference path. Its true runtime dependencies are listed explicitly in `requirements.txt`.

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

---

## 18. Operations

| Concern | Specification |
|---|---|
| **Backups** | Nightly `sqlite3 .backup` → DigitalOcean Space, 30-day retention. The gazetteer tuning and alias table are the irreplaceable assets — the rest is reconstructible. |
| **Monitoring** | External uptime check against a systemd watchdog; hourly self-report to `#bot-health`. |
| **Logs** | `journalctl`, structured JSON, 14-day retention. Transcripts of triggered commands only. |
| **Secrets** | Token in `/etc/aura/token`, mode 0600, loaded via `LoadCredential=` in the unit file. Never in the YAML, never in the environment, never in the repo. |
| **Updates** | `systemctl reload aura-brain` for config; restart for code. Ears stays connected across Brain restarts. |
| **Accuracy review** | Weekly `command_log` query: confidence distribution, mismatch rate by system, per-pilot failure rate. Feeds §8 tuning. |

---

## 19. Privacy and consent

AURA puts a microphone-reading robot into a corp's social space. Getting this wrong destroys trust permanently, and no feature recovers it.

**The governing decision: AURA does not record anything.**

- Audio lives in a **RAM ring buffer only**. Never written to disk. Overwritten every 1.5 seconds.
- The capture buffer is freed the instant STT returns.
- AURA stores the **transcript of triggered commands**, never audio.
- Non-command speech is **never transcribed at all** — the wake-word gate means it never reaches the recogniser.

This is not only good manners. Laws on recording conversations vary by country and by state and may require notice to, or consent from, **every** participant. By never recording, AURA sidesteps the entire category rather than trying to comply with all of it.

Additionally:

- **Announcement on join.** AURA posts: *"🎙️ AURA is listening for commands. Audio is not recorded. `/optout` to exclude yourself."* — every single time it joins.
- **`/optout`** drops that user's stream **inside Ears, before any processing and before it crosses the IPC boundary**. An actual drop, not a downstream filter.
- AURA reads Discord's `allow_voice_recording` voice flag as an additional consent signal.
- Callsign registration (§6.1) is keyed on the Discord user id attached to each utterance — it stores a chosen display name, never a voiceprint or anything derived from audio.
- A plain-language privacy note is pinned in the channel. Discord's Developer Policy expects a privacy policy regardless, and an honest one here is four sentences.

**Introduce AURA to the corp with this section, not with the feature list.** If half the corp is uncomfortable, that conversation is cheaper before the droplet is provisioned than after.

---

## 20. Resilience and degradation

Because voice receive is undocumented and can break without warning (§2.2), AURA is engineered to **survive the loss of its own headline feature**.

| Failure | Detection | Response |
|---|---|---|
| Discord breaks voice receive | No `VoiceTick` from anyone for 60s while ≥2 unmuted humans are in channel | Post **⚠️ Voice offline — use `/under-attack`, `/help-me` and `/hostiles`**; keep retrying. **Every slash command and the entire incident engine keep working.** |
| Voice gateway 4017 | Close code on connect | Loud alert to `#bot-health` — the Songbird version needs updating |
| DAVE session crash | Session exception | Rebuild, exponential backoff, cap retries, then degrade |
| STT worker hang | 5s watchdog | Kill, respawn, speak *"say again"* |
| Sustained low confidence | 10 consecutive low-tier results | Degrade and alert — something is wrong with the audio path |
| Brain down | Ears' socket write fails | Ears buffers 60s of frames, speaks *"system degraded"*, reconnects |
| Ears down | Brain heartbeat miss | Post the degraded notice; text path unaffected |
| Droplet down | External uptime check | `Restart=always` + a page |

**The load-bearing invariant: every voice command has a slash-command twin hitting the same engine.** The voice path is a fast front-end to a system that is complete without it. This is what makes AURA survivable on a platform that never promised to support half of it.

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

- No inbound ports. AURA dials out only; `ufw` denies all incoming except SSH.
- IPC socket is `0660 root:aura` on a `tmpfs` — not reachable off-box.
- Token via `LoadCredential=`, never in config, environment, or repo.
- Least-privilege bot permissions (§17.4). No Administrator.
- `aura` runs as an unprivileged `nologin` user.
- systemd hardening on both units: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `ReadWritePaths=/var/lib/aura /run/aura`.
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
