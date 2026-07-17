# AURA Brain — phase-1 interface contract

This document is the contract the Brain modules are built against. Signatures
here are normative; if an implementation must diverge, update this file in the
same commit. Shared vocabulary types live in `aura/types.py` and `aura/config.py`
(both already implemented — import, do not redefine).

Timestamps everywhere are ISO-8601 UTC strings (`datetime.now(timezone.utc).isoformat()`),
matching the TEXT columns in `brain/schema.sql`.

## Shared types — `aura/types.py` (implemented)

```python
class Intent(str, Enum):      # HOSTILE_SPOTTED UNDER_ATTACK ASSIST_REQUEST GATE_CAMP
                              # RESOLVE TIMER FORMUP QUERY CANCEL
class Severity(str, Enum):    # NONE="none" MEDIUM="medium" HIGH="high"
INTENT_SEVERITY: Mapping[Intent, Severity]        # GDD §6.1 defaults
class Tier(str, Enum):        # HIGH MEDIUM LOW              (GDD §8.3)
class IncidentStatus(str, Enum):   # ACTIVE STALE RESOLVED
class ResponderState(str, Enum):   # OTW WATCHING NO
class Outcome(str, Enum):     # POSTED FOLDED ASKED REJECTED (command_log.outcome)
class AlertChannel(str, Enum):     # ALERTS="alerts" LIVE="live"

ParsedCommand(intent, system_text: str|None, group_alias: str|None, detail: str|None, raw: str)
SystemEntry(id: int, name: str, region: str, constellation: str|None, metaphone: str)
MatchCandidate(system_id: int, name: str, score: float)
Resolution(tier: Tier, candidates: tuple[MatchCandidate, ...])   # top-3, best first; .best property
PriorContext(recency_min: Mapping[int, float], reporter_counts: Mapping[int, int],
             active_systems: tuple[int, ...], home_system_id: int|None)
TranscriptResult(text: str, avg_logprob: float)
ButtonSpec(custom_id, label, style="secondary", emoji=None, disabled=False)
CardRender(embed: dict, buttons: tuple[ButtonSpec, ...])   # embed = discord.Embed.from_dict shape
IncidentUpdate(user_id, text, at)
Incident(...)                 # mirrors GDD §9 / incidents row + updates + responders
IncidentOutcome(outcome: Outcome, utterance: str|None, card: CardRender|None, incident_id: int|None)
RoutingDecision(role_ids: tuple[int, ...], here: bool, channel: AlertChannel)
```

## Config — `aura/config.py` (implemented)

```python
def load_config(path: str | Path) -> AuraConfig        # raises ConfigError naming the bad key
class ConfigHolder:
    def __init__(self, path: str | Path) -> None
    path: Path
    current: AuraConfig        # property; atomic snapshot — read at point of use
    def reload(self) -> AuraConfig                     # SIGHUP handler in __main__ calls this
```

`AuraConfig` sections mirror GDD §16 one dataclass per section: `discord wake
capture stt matching(.tiers/.priors) incidents discipline(.circuit_breaker)
tts gazetteer ipc health database`. The token is **not** in config;
`discord.token_file` is a dev-only fallback path — `dsc/bot.py` reads
`$CREDENTIALS_DIRECTORY/token` first (systemd `LoadCredential=`).

## Database — `aura/core/db.py` (implemented)

```python
def connect(path) -> sqlite3.Connection          # WAL, foreign_keys=ON, Row factory
def migrate(conn, migrations_dir=None) -> int    # applies brain/migrations/NNNN_*.sql via user_version
def schema_version(conn) -> int
def execute(conn, sql, params=()) -> int         # commits; returns lastrowid
def executemany(conn, sql, seq) -> int           # commits; returns rowcount
def query(conn, sql, params=()) -> list[sqlite3.Row]
def query_one(conn, sql, params=()) -> sqlite3.Row | None
def query_value(conn, sql, params=()) -> Any
def backup(conn, dest_path) -> None              # sqlite3 backup API
```

Sync on purpose. Every caller on the event loop wraps calls in
`asyncio.to_thread(...)`; the engine funnels all writes through one path.

## NLU — `aura/nlu/`

```python
# grammar.py — fixed regex grammar, GDD §6. No LLM (constraint 6).
def parse(transcript: str) -> ParsedCommand | None
#   None = no intent recognised. Higher-severity patterns match first
#   ("tackled, need help in Kisogo" → UNDER_ATTACK). group_alias is one of
#   "miners" | "defense" | "all_hands" | None. detail is verbatim, unparsed.

# gazetteer.py — GDD §8.1. Loads scope rules from gazetteer.yaml, systems +
# adjacency from the db (SDE-seeded). Rebuildable at runtime via /gazetteer.
class Gazetteer:
    def __init__(self, conn: sqlite3.Connection, cfg: GazetteerConfig) -> None
    def load(self) -> None                        # blocking; call via to_thread
    @property
    def systems(self) -> Sequence[SystemEntry]    # the pruned active set
    def by_id(self, system_id: int) -> SystemEntry | None
    def by_name(self, name: str) -> SystemEntry | None      # case-insensitive exact
    def jumps(self, a_id: int, b_id: int) -> int | None     # BFS, memoised; None if disconnected
    def path(self, a_id: int, b_id: int) -> tuple[int, ...] | None
    #   Shortest jump path, endpoints included; same memo style as jumps()
    #   (one BFS parent map per source). Full graph — may cross pruned systems.
    def system_name(self, system_id: int) -> str | None     # FULL systems table, for path display
    def prompt_bias_text(self) -> str             # system names for Whisper initial_prompt
    @property
    def home_system_id(self) -> int | None

# phonetics.py — GDD §8.2–8.5. double_metaphone is implemented IN-REPO in pure
# Python (the pypi `metaphone` package does not build here). Alias-table lookup
# runs BEFORE phonetic matching; scores = 0.6*phonetic + 0.4*text, then priors.
def double_metaphone(word: str) -> tuple[str, str]          # (primary, alternate)
def resolve(text: str, gazetteer: Gazetteer, priors: PriorContext,
            cfg: MatchingConfig, conn: sqlite3.Connection | None = None) -> Resolution
#   `conn` is used only for the aliases table lookup/learning read; pass None in
#   pure-function tests. Pure scoring helpers must be importable and testable.
```

## Core engine — `aura/core/`

```python
# incidents.py — GDD §9. Discord-agnostic: renders CardRender and calls an
# injected Poster. All methods async; db work via to_thread inside.
class Poster(Protocol):
    async def post(self, guild_id: int, channel: AlertChannel, content: str,
                   card: CardRender) -> tuple[int, int]     # -> (channel_id, message_id)
    async def edit(self, channel_id: int, message_id: int, content: str,
                   card: CardRender) -> None

class IncidentEngine:
    def __init__(self, conn: sqlite3.Connection, holder: ConfigHolder,
                 gazetteer: Gazetteer, discipline: Discipline, poster: Poster,
                 rules_path: str | Path) -> None            # rules_path = routing.yaml
    async def report(self, guild_id: int, reporter_id: int, parsed: ParsedCommand,
                     resolution: Resolution | None) -> IncidentOutcome
    #   The single entry point for BOTH voice and slash paths (constraint 10).
    #   Handles tiers (§8.3), dedupe folding (§9.2), routing, discipline,
    #   command_log write. resolution=None for QUERY/CANCEL.
    async def resolve_system(self, guild_id: int, user_id: int,
                             system_id: int) -> IncidentOutcome        # "clear X" / /clear
    async def cancel(self, guild_id: int, user_id: int) -> IncidentOutcome  # 30s window
    async def respond(self, incident_id: int, user_id: int,
                      state: ResponderState) -> IncidentOutcome
    #   Button press; edits card in place; utterance like "Two responding to X"
    #   only on OTW transitions.
    async def correct_system(self, incident_id: int, user_id: int, system_id: int,
                             raw_text: str) -> IncidentOutcome
    #   [Wrong — fix] path: updates card AND learns alias (§8.5). An explicit
    #   caller-supplied raw_text wins; when it is empty (button presses carry
    #   no transcript) the alias key falls back to the incident row's stored
    #   raw_system_text, so button corrections learn across restarts too.
    async def sweep_stale(self) -> list[int]     # ids marked STALE; called by a periodic task
    def build_prior_context(self, guild_id: int, reporter_id: int) -> PriorContext  # blocking
    def load_routing_rules(self, resolve_role: Callable[[str], int | None]) -> int  # blocking
    #   (Re)loads routing.yaml + group aliases through the engine. Needs the
    #   guild role cache: AuraBot runs it (via to_thread) in on_ready, and
    #   /routing reload re-runs it. Until it succeeds the engine holds zero
    #   rules — cards still post, nobody is mentioned.
    async def fire_due_timers(self, now: datetime) -> list[TimerPing]
    #   TimerPing(timer_id, guild_id, system_id, system_name, note, fires_at,
    #   created_by) — exported from incidents.py; rows are marked fired, the
    #   caller (__main__'s timer loop) announces and speaks.

def parse_duration(text: str) -> timedelta | None    # public: engine + /timer//formup share it
def render_card(...) -> CardRender                   # public for tests/views smoke checks

# routing.py — GDD §10/§11. Pure evaluation; rule loading is separate.
@dataclass RoutingRule(role_id: int, types: frozenset[Intent], scope: RuleScope,
                       escalate_at: Intent | None, quiet_hours: QuietHours | None)
@dataclass RuleScope(systems: tuple[int, ...], regions: tuple[str, ...],
                     within_jumps_of: tuple[int, int] | None)   # (system_id, jumps)
@dataclass QuietHours(tz: str, start: str, end: str)            # "HH:MM"
def load_rules(path: str | Path, gazetteer: Gazetteer,
               resolve_role: Callable[[str], int | None]) -> list[RoutingRule]
def evaluate(incident: Incident, rules: Sequence[RoutingRule], now: datetime,
             *, gazetteer: Gazetteer) -> RoutingDecision
#   Pure given its inputs. here=True ONLY when a matched rule's escalate_at
#   equals incident.type, and only for UNDER_ATTACK/ASSIST_REQUEST (constraint 11);
#   group_alias "all_hands" is applied by the engine, not here.
#   Channel semantics: any mention → ALERTS, else LIVE. A card lives in
#   exactly ONE channel — never mirrored (constraint 9).

ESCALATABLE_TYPES: frozenset[Intent]     # {UNDER_ATTACK, ASSIST_REQUEST}
def apply_group_alias(decision, group_alias, rules, alias_roles) -> RoutingDecision
def suppress(decision) -> RoutingDecision
#   Discipline suppression: RoutingDecision has no `suppressed` flag — a
#   suppressed report becomes RoutingDecision((), False, LIVE): still posted,
#   mention-free.
def load_group_aliases(path, resolve_role) -> dict[str, int]
class RoutingConfigError(Exception)
#   routing.yaml accepts the GDD §10.1 bare list of rules, OR a mapping form
#   {rules: [...], group_aliases: {miners: "@Miners", defense: "@Home-Defense"}}.

# discipline.py — GDD §11.1. Pure state machine over injected `now` datetimes.
class Discipline:
    def __init__(self, holder: ConfigHolder) -> None
    def allow_mention(self, user_id: int, now: datetime) -> bool
    #   False if user cooldown active OR circuit breaker open. Does NOT record.
    def record_mention(self, user_id: int, now: datetime) -> None
    def breaker_open(self, now: datetime) -> bool
    def set_fleetmode(self, enabled: bool) -> None
    def may_voice_trigger(self, member_role_ids: Iterable[int]) -> bool
    #   fleetmode → requires fc role; always requires pilot role for mentions.
    def may_mention(self, member_role_ids: Iterable[int]) -> bool      # pilot-role gate
    def should_announce_flood(self, now: datetime) -> bool   # True once per breaker episode
    def check(self, member_role_ids: Iterable[int], source: str) -> bool  # "voice" | "slash"
    fleetmode: bool                                          # read-only property
```

## Audio — `aura/audio/`  (RAM only — constraint 5)

Frames are **20 ms, 16 kHz, mono, s16le** → exactly 640 bytes each, as emitted
by Ears (GDD §15 type 0x02 body, after the two id headers).

```python
# vad.py
class VadGate:
    def __init__(self, aggressiveness: int) -> None          # webrtcvad 0–3
    def is_speech(self, frame: bytes) -> bool                # one 20ms frame

# wake.py
class WakeDetector(Protocol):
    def score(self, user_id: int, frame: bytes) -> float     # 0..1, per-user state
    def reset(self, user_id: int) -> None
class OpenWakeWordDetector(WakeDetector):
    def __init__(self, holder: ConfigHolder) -> None         # reads holder.current.wake live
#   Refractory suppression is owned by CaptureManager; a hit only clears the
#   detector's per-user streaming state (pending bytes, held score, model).

# capture.py — per-user state machine: preroll ring → wake hit → capture →
# endpoint (silence) → emit. Ring buffers overwritten every ~1.5s; capture
# buffer freed the moment on_utterance returns.
class CaptureManager:
    def __init__(self, holder: ConfigHolder, vad: VadGate, wake: WakeDetector,
                 on_utterance: Callable[[int, int, bytes], Awaitable[None]]) -> None
    def feed(self, user_id: int, guild_id: int, frame: bytes) -> None   # sync, hot path
    def reopen(self, user_id: int, guild_id: int, window_ms: int = 4000) -> None
    #   LOW-tier "say again": next utterance captured without a wake hit.
    def drop_user(self, user_id: int) -> None                # left channel / opted out

# stt.py — blocking; ALWAYS called via asyncio.to_thread.
class Transcriber(Protocol):
    def transcribe(self, pcm16k: bytes, bias: str) -> TranscriptResult
class FasterWhisperTranscriber(Transcriber): ...
class WhisperCppTranscriber(Transcriber): ...
def make_transcriber(cfg: SttConfig) -> Transcriber
```

## IPC — `aura/ipc.py`  (GDD §15; wire format spans both languages)

```
Frame: [4-byte BE u32 length][1-byte type][body]
       length = 1 + len(body): it counts every byte AFTER the length field
       (the type byte plus the body). A frame with an empty body has length 1.
type 0x01 CONTROL  body = UTF-8 JSON object
type 0x02 AUDIO    body = [8B user_id u64 LE][8B guild_id u64 LE][s16le PCM 16kHz mono]
type 0x03 TTS      body = [8B guild_id u64 LE][1B priority u8][WAV bytes]
```

Priorities: `PRIORITY_LOW = 0`, `PRIORITY_NORMAL = 1`, `PRIORITY_ALERT = 2`
(module constants; higher preempts queue order in Ears' playback).

```python
@dataclass ControlFrame(msg: dict)
@dataclass AudioFrame(user_id: int, guild_id: int, pcm: bytes)
@dataclass TtsFrame(guild_id: int, priority: int, wav: bytes)

class FrameCodec:                     # stream decoder tolerant of partial reads
    def feed(self, data: bytes) -> list[ControlFrame | AudioFrame | TtsFrame]
    @staticmethod
    def encode_control(msg: dict) -> bytes
    @staticmethod
    def encode_audio(user_id: int, guild_id: int, pcm: bytes) -> bytes
    @staticmethod
    def encode_tts(guild_id: int, priority: int, wav: bytes) -> bytes

class IpcServer:                      # Brain BINDS the socket; Ears connects
    def __init__(self, holder: ConfigHolder,
                 on_audio: Callable[[int, int, bytes], None],         # sync, hot path
                 on_control: Callable[[dict], Awaitable[None]]) -> None
    async def start(self) -> None     # unlink stale socket, bind, accept loop
    async def stop(self) -> None
    async def send_tts(self, guild_id: int, priority: int, wav_bytes: bytes) -> None
    async def send_control(self, msg: dict) -> None          # join/leave/optouts
    @property
    def connected(self) -> bool       # Ears currently attached
```

Control message shapes are exactly GDD §15 (`hello`, `speaking`, `left`,
`heartbeat` inbound; `join`, `leave`, `optouts` outbound; ids as strings).

## TTS — `aura/tts.py`

```python
class Speaker:
    def __init__(self, holder: ConfigHolder, ipc: IpcServer) -> None
    async def say(self, guild_id: int, text: str, priority: int = PRIORITY_NORMAL,
                  *, user_id: int | None = None) -> bool
    #   Runs the Piper binary as a subprocess (stdin=text, stdout=raw s16le),
    #   wraps a WAV header IN MEMORY (no resampling, no temp files — GDD §12.3),
    #   enforces the max_utterance_s cap, then awaits ipc.send_tts.
    #   Subprocess I/O off the event loop. Respects voice_mutes and tts.enabled.
    #   Returns True once the WAV reached IPC; False when speech was suppressed
    #   (disabled, muted trigger user, synthesis failure, or over the §12.2
    #   length cap) — the caller falls back to channel text. ``user_id`` is the
    #   triggering pilot, checked against the /mute-voice set
    #   (set_voice_mutes/set_muted keep that set current).
    def set_voice_mutes(self, user_ids: set[int]) -> None
    def set_muted(self, user_id: int, muted: bool) -> None
    def is_muted(self, user_id: int) -> bool
    async def close(self) -> None
#   The §12.1 utterance catalogue lives here as pure functions returning the
#   exact GDD strings: ping_sent(system, group=None, *, type_word="Hostiles"),
#   ambiguous(type_word, system), say_again(), responders(n, system),
#   resolved(system), timer_set(system, duration_words), flood_control(),
#   degraded(), number_word(n).
```

## Discord layer — `aura/dsc/`

```python
# bot.py
class AuraBot(discord.ext.commands.Bot):
    def __init__(self, holder: ConfigHolder, engine: IncidentEngine,
                 gazetteer: Gazetteer, discipline: Discipline, speaker: Speaker,
                 conn: sqlite3.Connection) -> None
def read_token(cfg: DiscordConfig) -> str
#   $CREDENTIALS_DIRECTORY/token (systemd LoadCredential) first; falls back to
#   cfg.token_file for dev. Never logged, never in config values.
```

- `bot.py` also implements the `Poster` protocol (posts/edits cards, builds
  `discord.ui.View` from `CardRender.buttons`) and registers persistent views.
- Cogs (`cogs/intel.py subs.py ops.py utility.py admin.py`, GDD §7) call the
  **same `IncidentEngine`** methods as the voice path — constraint 10. A slash
  report builds `ParsedCommand` + a HIGH-tier `Resolution` from the typed
  system name (autocompleted from `gazetteer.systems`). `utility.py` carries
  the slash-only quality-of-life commands (/evetime /route /history /remindme
  /poll) — no voice twins because they trigger no alerts; it also exports
  `ReminderService(conn, bot)` with `deliver_due(now) -> int` (DM, falling
  back to an #intel-live mention), driven by `__main__`'s reminder poll loop.
- Views: `custom_id = f"aura:inc:{incident_id}:{action}"` with `action` in
  `otw | watch | no | fix | pick:{system_id}`. Poll vote buttons use
  `aura:poll:{poll_id}:{option_idx}` (handled in `cogs/utility.py`).
  Persistent (`timeout=None`), re-registered on startup so buttons survive
  restarts (GDD §9.3).

## Wiring — `aura/__main__.py`

Ownership: `__main__` builds, in order: `ConfigHolder` → `db.connect`+`migrate`
(via to_thread) → `Gazetteer.load` → `Discipline` → `IpcServer` → `Speaker` →
`make_transcriber` → `CaptureManager` → `IncidentEngine` (Poster = AuraBot) →
`AuraBot`. One asyncio event loop, owned by `__main__` (`asyncio.run`);
`bot.start()`, `ipc.start()`, the stale sweep, timers, reminders, and health
reporter run as supervised tasks. SIGHUP → `holder.reload()`; SIGTERM → graceful shutdown
(stop IPC, close bot, close db). `voice_gateway.py` watches voice states and
sends `join`/`leave`/`optouts` control messages.

Threading rules:
- Event loop: all Discord I/O, IPC socket I/O, engine orchestration.
- `asyncio.to_thread`: STT inference, Piper subprocess I/O, ALL sqlite calls,
  gazetteer (re)load.
- Sync hot path (called directly from the IPC reader): `on_audio` →
  `CaptureManager.feed` → VAD/wake scoring. Must never block on db or network.
- Audio bytes live only in ring buffers and capture buffers in RAM (constraint 5).
