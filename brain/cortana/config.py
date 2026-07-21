"""Configuration loading, validation, and hot-reload for CORTANA Brain.

Mirrors ``config/cortana.yaml.example`` / GDD §16 one dataclass per section.
Validation is driven by the declarative table in :mod:`cortana.config_schema`
— one :class:`~cortana.config_schema.Key` per tunable carrying type, range,
allowed values, default, and reload class. ``load_config`` raises
:class:`ConfigError` messages that always follow the contract
``section.key: problem — Fix: <action>``. Unknown keys are rejected with a
did-you-mean suggestion — a typo can no longer silently revert a knob to its
default.

Hot reload: ``__main__`` owns the SIGHUP handler; :mod:`cortana.reload`
validates all config inputs together and swaps them all-or-nothing.
Long-lived objects hold the :class:`ConfigHolder` and read ``holder.current``
at the point of use, never a cached snapshot. :func:`diff_configs` buckets
changed keys by reload class so restart-bound edits are reported as
"restart pending" instead of silently absorbed.

Secrets: the Discord token is NOT config. Config carries ``discord.token_file``
(a path) only. ``cortana.dsc.bot`` reads the token at startup from
``$CREDENTIALS_DIRECTORY/token`` (systemd ``LoadCredential=``, GDD §18/§22),
falling back to ``discord.token_file`` for development runs.
"""

from __future__ import annotations

import difflib
import threading
from dataclasses import dataclass, field
from functools import reduce
from pathlib import Path
from typing import Any

import structlog
import yaml

from cortana.config_schema import (
    CROSS_CHECKS,
    KEYS,
    REQUIRED,
    Key,
    Reload,
    child_sections,
    key_by_path,
    keys_in_section,
)

log = structlog.get_logger(__name__)

_MISSING = object()

#: Legal values, re-exported for callers that render them in messages.
STT_BACKENDS = key_by_path("stt.backend").choices or ()
RELAY_MODES = key_by_path("stt.relay_mode").choices or ()


class ConfigError(Exception):
    """Config file missing, unreadable, or invalid. Message names the bad key."""


# ── section dataclasses (GDD §16) ────────────────────────────────────────────
# Field defaults mirror the schema defaults (asserted by the schema tests) so
# tests and tools can construct sections directly without re-stating them.


@dataclass(frozen=True, slots=True)
class ChannelsConfig:
    intel_alerts: int
    intel_live: int
    health: int
    #: Optional STT review log (GDD §8.7). 0 = off. When set, one line per
    #: heard utterance posts here: the transcript plus its parse outcome.
    transcript: int = 0


@dataclass(frozen=True, slots=True)
class RolesConfig:
    """Role-ID gates. 0 = not configured — that gate is simply off: without a
    pilot role anyone may trigger mentions, without an FC role fleetmode
    cannot restrict the voice path. The whole ``discord.roles`` section is
    optional for exactly this reason (an empty ``roles:`` once crash-looped
    a deployment)."""

    pilot: int = 0  # gate: may trigger mentions
    fc: int = 0  # gate under fleetmode; also grants admin commands


@dataclass(frozen=True, slots=True)
class DiscordConfig:
    token_file: str  # dev fallback only; production uses LoadCredential=
    guild_id: int
    channels: ChannelsConfig
    roles: RolesConfig
    watch_voice_channels: tuple[int, ...]
    auto_join: bool
    #: Master switch for role/@here pings. False = "silent mode": incidents and
    #: relays still post to the channel, but CORTANA mentions nobody and the
    #: @Pilot trigger gate is lifted (there is nothing to protect). Turn on once
    #: real roles are wired into routing.yaml.
    mentions_enabled: bool = True
    #: Threat levels that fire an @here (ping by colour), when mentions are on.
    #: "high" = CODE RED, "medium" = CODE ORANGE, "none" = CODE YELLOW. Default
    #: RED only — the safe choice; add "medium" to also ping on CODE ORANGE.
    here_on_severity: tuple[str, ...] = ("high",)
    #: §19 consent-announcement cadence on voice join:
    #:   every = post on every join (the original behaviour)
    #:   daily = at most once per 24h, persisted across restarts (default —
    #:           restart churn used to spam the channel with the notice)
    #:   off   = never post it (the corp accepts the consent posture is
    #:           carried by /optout + the pinned docs instead)
    join_announcement: str = "daily"


@dataclass(frozen=True, slots=True)
class WakeConfig:
    model: str
    threshold: float
    refractory_ms: int
    #: How CORTANA acknowledges the wake word to the pilot:
    #: "voice" = speak "Go ahead." (Cortana talks back), "beep" = an instant
    #: tone (fast, no synthesis latency), "none" = silent. Default "beep".
    ack: str = "beep"
    #: openWakeWord's built-in Silero VAD gate: a wake trigger only counts
    #: when the VAD simultaneously scores speech above this. Cuts false
    #: fires from music/game audio/keyboard noise — but it gates the wake
    #: model hard, and it shipped ON by default once and silently killed the
    #: wake word on a live deployment. OPT-IN only (0.0 = off, the default);
    #: enable at ~0.3-0.5 and verify wake still fires before trusting it.
    #: Applied at model build; the pool rebuilds per-user models live on
    #: reload (sighup class, like ``model`` and ``extra_models``).
    vad_threshold: float = 0.0
    #: Additional openWakeWord ONNX chains scored in parallel with ``model``
    #: — any listed phrase wakes CORTANA (run the old and new phrase side by
    #: side through a transition). ``threshold`` applies to the MAX score
    #: across all models; a broken/missing extra is logged once and skipped
    #: (only a broken PRIMARY latches the detector faulted). Each extra adds
    #: its own false-fire budget — keep the total to 2-3 models (GDD §5.2).
    extra_models: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChatConfig:
    """The "command override" out-of-band assistant (GDD §6.6).

    Deliberately OFF by default and entirely separate from the command path —
    constraint 6 (no LLM in the command path) stands: the incident grammar
    never touches this. The override channel only runs when a pilot explicitly
    says "command override …" (or uses the /ask slash twin)."""

    enabled: bool = False
    #: Who answers override questions:
    #:   "anthropic" — the cloud Claude API (needs a key, costs per question).
    #:   "local"     — an on-box OpenAI-compatible server at ``local_url``
    #:                 (llama.cpp / Ollama / …): no API, no key, no per-question
    #:                 cost. The SLM lane — conversational back-and-forth on the
    #:                 droplet itself, still OFF the command path (constraint 6).
    backend: str = "anthropic"
    #: OpenAI-compatible chat-completions endpoint for ``backend="local"``
    #: (e.g. ``http://127.0.0.1:8081/v1/chat/completions``). Empty = unset.
    local_url: str = ""
    #: Model for override replies. For ``backend="anthropic"`` a Claude model
    #: id (the default is the cheapest tier — fractions of a cent per question;
    #: claude-opus-4-8 is the smarter ~10x alternative). For ``backend="local"``
    #: the model name the local server expects.
    model: str = "claude-haiku-4-5"
    #: Dev fallback ONLY (0600). Production reads
    #: $CREDENTIALS_DIRECTORY/anthropic via systemd LoadCredential=
    #: (constraint 12) — the key itself is never in YAML.
    api_key_file: str = "/etc/cortana/anthropic"
    max_tokens: int = 300
    #: Per-user seconds between override questions — the cost throttle.
    user_cooldown_s: int = 10
    #: Wall-clock cap on one answer (includes any web search round-trips).
    timeout_s: float = 25.0
    #: Allow the model one live web search per question ("weather in
    #: Chicago"). Each search bills separately (~a cent) — the cooldown above
    #: is what keeps that bounded.
    web_search: bool = True
    #: Channel for override answers too long to speak (§12.2 cap). 0 = the
    #: intel_live channel — but chit-chat in an intel channel annoys fast;
    #: point this at a general/bot channel instead.
    answer_channel: int = 0


@dataclass(frozen=True, slots=True)
class ConversationConfig:
    """Freestyle conversation mode (GDD §6.8). Optional section; OFF by default.

    A distinct lane from the ``chat`` override channel: it defaults to the
    on-box ``local`` backend (free, high-throughput back-and-forth) while
    ``chat`` keeps its own cloud backend. Reuses the :class:`~cortana.chat.
    ChatBackend` abstraction. Never on the command path (constraint 6), never
    posts a card (constraint 9), never pings (constraint 11). A half-set config
    (enabled but no ``local_url`` / no key) degrades gracefully to dark, exactly
    like ``chat``."""

    enabled: bool = False
    #: "local" (on-box OpenAI-compatible server, free) | "anthropic" (cloud).
    backend: str = "local"
    #: OpenAI-compatible chat-completions endpoint for backend="local". Empty = dark.
    local_url: str = ""
    #: Model name (local server's name, or a Claude id for backend="anthropic").
    model: str = "qwen2.5:7b"
    #: Dev fallback ONLY (0600). Production reads $CREDENTIALS_DIRECTORY/anthropic
    #: via LoadCredential= (constraint 12). Ignored for backend="local".
    api_key_file: str = "/etc/cortana/anthropic"
    #: Hard cap per spoken reply — chit-chat is short.
    max_tokens: int = 200
    #: Wake-free window kept open after each reply for the pilot's next turn.
    turn_taking_seconds: float = 8.0
    #: Prior user+assistant exchanges replayed as context (0 = stateless).
    max_history_turns: int = 6
    #: Hard per-session turn cap; refilled only by a fresh wake (loop bound).
    max_turns: int = 8
    #: Idle lifetime of a session; after this a stale thread is dropped.
    session_ttl_seconds: int = 180
    #: Min gap between a pilot's turns (light anti-spam).
    user_cooldown_s: float = 2.0
    #: Go silent while an incident is active/recent so banter never crowds comms.
    quiet_during_ops: bool = True
    #: How long after the last active incident "ops in progress" holds (minutes).
    quiet_window_min: int = 10
    #: Answer arithmetic with the deterministic on-box evaluator (never hallucinated).
    math_tool: bool = True
    #: Wall-clock cap on producing one reply (qwen2.5:7b on 4 vCPU is slow).
    timeout_s: float = 20.0
    #: Channel for a reply too long to speak, posted AllowedMentions.none(). 0 = drop.
    overflow_channel: int = 0


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    preroll_ms: int
    endpoint_silence_ms: int
    max_utterance_ms: int
    vad_aggressiveness: int  # webrtcvad 0–3
    #: Live recognition (GDD §5.5): decode the growing capture buffer while the
    #: pilot is still talking and commit the instant a complete, confident
    #: command is present — instead of waiting for them to stop (or the hard
    #: cap) before the first decode. This is the fix for the "keep talking and
    #: it drags 10-20s" latency: a distress call lands mid-sentence, not after
    #: the channel goes quiet. Purely an endpointing optimization — the final
    #: decode on emit stays authoritative, so it can never mis-route. Needs
    #: CPU headroom (the incremental decodes are real Whisper runs): sized for
    #: a dedicated ≥4-vCPU box. Set false to fall back to decode-on-endpoint.
    streaming: bool = True
    #: Minimum NEW speech between incremental decodes — the incremental-decode
    #: rate limiter. Lower = snappier + more CPU; higher = calmer + a touch
    #: more lag. Each fires one Whisper decode of the buffer-so-far.
    partial_decode_ms: int = 1200
    #: Don't attempt an incremental decode until at least this much speech has
    #: accrued: a sub-second fragment cannot carry a whole command, so decoding
    #: it just burns CPU and risks an early clip on a half-heard word.
    partial_min_speech_ms: int = 900
    #: Confidence floor for an incremental decode to commit early. An uncertain
    #: partial keeps listening rather than clipping the pilot; the normal
    #: endpoint (silence or cap) still catches it, and the final decode's
    #: confirm-first flow (§8.3) owns the uncertainty from there.
    early_commit_min_logprob: float = -1.0


@dataclass(frozen=True, slots=True)
class DialogConfig:
    """Voice dialog engine timing and budgets (GDD §5.4).

    The whole section is optional — the defaults are the tuned live values.
    """

    #: Wall-clock lifetime of a wake-free window (armed by a say-again retry,
    #: a "code <colour>" opener, or a bare "command override"). DTX-proof:
    #: the dialog wheel expires it in real time, frames or no frames.
    window_ms: int = 4000
    #: Endpoint grace after a capture opens / a prompt is spoken — covers cue
    #: playback plus the pilot's reaction time, so waiting politely for
    #: "Go ahead." can never get a capture endpointed before the first word.
    ack_grace_ms: int = 2000
    #: Floor under capture.endpoint_silence_ms for the wall-clock endpoint:
    #: Discord DTX drops packets during brief pauses between words, so a
    #: too-eager gap would clip a pilot mid-sentence.
    endpoint_gap_floor_ms: int = 700
    #: Wake-free windows per dialog, TOTAL — subdialog openers (code/override)
    #: and say-again retries share this budget. Only a fresh wake refills it;
    #: exhaustion always ends audibly with "standing down".
    max_retries: int = 2
    #: Confirm-first for voice reports (GDD §8.3): "off" commits immediately
    #: (spoken readback only); "low" asks "Heard X — confirm?" when the
    #: system match is uncertain (LOW tier / verbatim); "always" asks for
    #: every voice report. Yes commits, no opens a say-again retry, and
    #: silence/unmatched speech commits anyway — a distress call is never
    #: lost to an unanswered question.
    confirm_reports: str = "low"
    #: Transcripts below this Whisper avg_logprob are chatter/noise: they
    #: never earn a say-again retry (the open-mic retry loop). Recognised
    #: commands are never gated by this.
    retry_min_logprob: float = -1.3


@dataclass(frozen=True, slots=True)
class SttConfig:
    backend: str  # "faster-whisper" | "whisper-cpp"
    model: str
    compute_type: str
    cpu_threads: int
    bias_with_gazetteer: bool
    whisper_cpp_url: str
    #: STT watchdog deadline (GDD §20): seconds one decode may run once it
    #: reaches the head of the serialized STT queue before the worker is
    #: respawned. Queue WAIT time never counts — overload is not a hang.
    #: After 2 consecutive respawns STT latches degraded until reload/restart.
    watchdog_s: float = 15.0
    #: Minimum Whisper avg_logprob for a transcript that matched NO grammar
    #: intent to be posted as a freeform relay (GDD §8.6). Below this the
    #: transcript is treated as unintelligible — CORTANA says "Say again" instead
    #: of posting hallucinated noise to the intel channel. Recognised commands
    #: are never gated by this (a distress call always posts).
    relay_min_logprob: float = -0.9
    #: What unmatched speech may become a relay card (GDD §8.6):
    #:   framed — only explicitly framed intel ("report …", a spoken colour
    #:            code, or an all-hands phrase); everything else gets
    #:            "Say again". The default: mishearings never become cards.
    #:   open   — any unmatched transcript relays (confidence-gated).
    #:   off    — the freeform relay never posts; commands only.
    relay_mode: str = "framed"
    #: faster-whisper `no_repeat_ngram_size` (GDD §5.3). Whisper on noisy fleet
    #: audio is prone to repetition loops — a real system name latched onto and
    #: emitted dozens of times ("0-R5TS, 0-R5TS, 0-R5TS, …") at HIGH confidence,
    #: which buries the actual callout and blocks streaming's early-commit
    #: (§5.5) from ever matching a clean command in a partial. Forbidding any
    #: n-gram of this length from repeating breaks the loop. 3 is safe for real
    #: speech (a genuine "three three three" is a 1-gram, untouched); 0 disables.
    no_repeat_ngram_size: int = 3


@dataclass(frozen=True, slots=True)
class TiersConfig:
    high_min: float
    high_margin: float
    medium_min: float


@dataclass(frozen=True, slots=True)
class PriorsConfig:
    recency_weight: float
    recency_window_min: int
    proximity_weight: float
    proximity_max_jumps: int
    reporter_history_weight: float
    home_weight: float


@dataclass(frozen=True, slots=True)
class IndexConfig:
    """Blocking-index tuning for the phonetic matcher (GDD §8.2).

    The index is a STRICT performance layer: it only decides which gazetteer
    entries the O(windows·N) scorer runs edit-distance on, never HOW they are
    scored (constraint 7). It is provably accuracy-neutral — a cheap
    length-ratio upper bound proves that no skipped entry could have entered
    brute force's rerank pool before any entry is skipped, so a disabled or
    degenerate index falls back to the identical full scan.
    """

    #: Master switch. false = always run the full brute-force scan (the old
    #: behaviour, kept as the reference path the equivalence test pins to).
    enabled: bool = True
    #: Never skip the full scan until at least this many entries have been
    #: fully scored (K_MIN). Keeps the rerank pool well-established before the
    #: upper-bound stop can fire; must be ≥ the internal rerank pool size.
    min_candidates: int = 12


@dataclass(frozen=True, slots=True)
class MatchingConfig:
    phonetic_weight: float
    text_weight: float
    tiers: TiersConfig
    priors: PriorsConfig
    #: Two-tier resolution (GDD §8.1): on a LOW-tier scoped match, fall back to
    #: scoring against the full seeded k-space map so any real system resolves.
    #: The reliability fix for a small scope / roaming corp; scoped accuracy is
    #: untouched (the fallback only runs when the scoped set already failed).
    full_map_fallback: bool = True
    #: Blocking-index tuning (GDD §8.2). Defaulted so configs and tests that
    #: predate the index get the enabled index transparently.
    index: IndexConfig = field(default_factory=IndexConfig)


@dataclass(frozen=True, slots=True)
class IncidentsConfig:
    dedupe_window_s: int
    stale_after_min: int
    cancel_window_s: int
    #: No updates for this long → the card auto-RESOLVES in place, silently
    #: (field request: cards sat open forever until someone cleared them).
    #: Timers/form-ups are exempt — their lifecycle anchors on ``fires_at``.
    #: 0 = never.
    auto_resolve_min: int = 60


@dataclass(frozen=True, slots=True)
class CircuitBreakerConfig:
    max_mentions: int
    window_min: int


@dataclass(frozen=True, slots=True)
class DisciplineConfig:
    user_cooldown_s: int
    circuit_breaker: CircuitBreakerConfig
    personal_pings_max: int = 10  # per-user cap on /pingme subscriptions (GDD §10.3)


@dataclass(frozen=True, slots=True)
class TtsConfig:
    enabled: bool
    voice: str
    binary: str
    max_utterance_s: float
    #: Post-synthesis effect over the voice: "none" or "holographic" (a chorus
    #: + subtle reverb for a ship's-AI sheen — an effect, not a voice clone).
    effect: str = "none"
    #: Spoken-line flavour: "standard" keeps the exact GDD §12.1 catalogue;
    #: "cortana" rotates acknowledgement lines ("Go ahead." / "Listening." /
    #: "Send it.") so CORTANA feels alive; "bratty" is the cortana rotation
    #: with attitude and sailor vocabulary (an adult corp's explicit choice —
    #: profanity in the ACK lines only). Info-carrying lines never vary.
    personality: str = "standard"
    # Ducking level and talk-over suppression are fixed playback mechanics in
    # Ears (ears/src/playback.rs) — deliberately not tunables here.


@dataclass(frozen=True, slots=True)
class FunConfig:
    """The fact library / insult maker (GDD §13.2). Optional section; the
    defaults ship the feature on with modest throttles."""

    enabled: bool = True
    #: Per-guild seconds between served facts / insults — comedy must never
    #: crowd real comms (ALERT speech also jumps the queue regardless).
    fact_cooldown_s: int = 10
    insult_cooldown_s: int = 10
    #: true = the full sailor-mouth pool; false = clean burns only.
    insults_spicy: bool = True
    #: Spoken-length cap for fun lines, overriding tts.max_utterance_s — a
    #: whole fact runs longer than a §12.2 command reply.
    max_speak_s: float = 20.0


@dataclass(frozen=True, slots=True)
class AreasConfig:
    """Learned custom areas (GDD §8.5a). Optional section; on by default.

    When a report names a place that resolves to no system, CORTANA asks once
    ("Did you say <word>?") and, on an explicit yes, remembers the word so it
    resolves for good — the corp's own place vocabulary, learned by talking."""

    #: false = post unknown places verbatim without ever offering to learn them.
    learn: bool = True
    #: Per-guild cap. At the cap learning pauses (reports still post) until an
    #: FC prunes with /areas-forget — the guard against a stuck mishearing.
    max_per_guild: int = 200


@dataclass(frozen=True, slots=True)
class NluConfig:
    """The LLM understanding brain (GDD §6.7). Optional section; OFF by default.

    When the fixed grammar (§6.1) can't parse a callout, an on-box model reads
    the transcript and returns the command — so pilots can say it any way they
    like. The place it names is still resolved deterministically against the
    real system map (it can't invent a system) and nothing pings until the
    pilot confirms, so a misunderstanding is caught out loud first."""

    #: Master switch. Needs ``url`` + a running local model to do anything.
    understanding: bool = False
    #: OpenAI-compatible chat-completions endpoint of the on-box model
    #: (e.g. http://127.0.0.1:11434/v1/chat/completions from Ollama). Empty = off.
    url: str = ""
    #: The model name the server expects (e.g. "llama3.2:3b").
    model: str = ""
    #: Wall-clock cap on one interpretation; the grammar already answered fast,
    #: so a slow model just means that one messy callout waits a beat.
    timeout_s: float = 8.0


@dataclass(frozen=True, slots=True)
class GazetteerConfig:
    file: str
    home_system: str | None  # None/empty = no home-bias prior (nomadic corp)
    include_all: bool = False  # nomadic mode: entire seeded map active (GDD §8.1)


@dataclass(frozen=True, slots=True)
class RoutingFileConfig:
    """Where ``routing.yaml`` lives. The empty default means "a file named
    ``routing.yaml`` next to ``cortana.yaml``" — the previously hardcoded,
    undocumented behaviour, now an explicit key (``routing.file``)."""

    file: str = ""

    def resolve(self, config_path: Path) -> Path:
        """The effective routing.yaml path, given where cortana.yaml lives."""
        if self.file:
            return Path(self.file)
        return config_path.parent / "routing.yaml"


@dataclass(frozen=True, slots=True)
class IpcConfig:
    socket: str
    # Ears' outbound ring size lives in /etc/cortana/ears.yaml (buffer_seconds):
    # the ring must survive Brain restarts, so Brain cannot own that knob.


@dataclass(frozen=True, slots=True)
class HealthConfig:
    report_interval_min: int
    voice_silence_alarm_s: int


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    path: str


@dataclass(frozen=True, slots=True)
class KbPollerConfig:
    """Albion gameinfo poll loop (killboard GDD §5). The API keeps no history,
    so this loop IS the history — persist every event, never miss a window."""

    interval_seconds: int = 45
    request_timeout_seconds: int = 10
    max_retries: int = 3
    backoff_base_seconds: float = 5.0
    #: Endpoint maximum per request (killboard GDD §5.2).
    page_limit: int = 51
    #: First-run backfill depth in pages (≈ the server's offset ceiling).
    max_backfill_pages: int = 20
    #: Also gather guild DEATHS. The guild-events endpoint is kill-only, so deaths
    #: (and Death Fame) are polled per-member from /players/{id}/deaths on a slower
    #: cadence. Off = kills-only feed and Death Fame stays 0 (killboard GDD §5).
    track_deaths: bool = True
    #: Anti-backfill-spam window for the deaths sweep: only deaths newer than this
    #: many minutes are posted to the feed; older ones are seeded (kept for Death
    #: Fame, never posted), so ingesting weeks of member history doesn't flood the
    #: channel with old death cards (killboard GDD §5).
    deaths_post_window_minutes: int = 60


@dataclass(frozen=True, slots=True)
class KbFeedConfig:
    """Kill/death feed routing + filtering (killboard GDD §7.2). Channel ids of
    0 mean "unset"; the module's enable gate needs at least one of kills/deaths."""

    kills_channel: int = 0
    deaths_channel: int = 0
    min_fame: int = 0
    juicy_channel: int = 0
    juicy_min_fame: int = 2_000_000
    #: Route a kill to the juicy channel when its estimated LOOT value (market)
    #: is at least this many silver — the low-fame/high-loot gank a fame gate
    #: misses. 0 = off; needs killboard.market.enabled. Complements juicy_min_fame
    #: (a kill mirrors to juicy if EITHER threshold is met).
    juicy_min_loot: int = 0
    ignore_deaths_below_ip: int = 0
    blob_participant_threshold: int = 20
    blob_channel: int = 0
    #: Cap on feed posts per catch-up cycle after downtime (killboard GDD §7.3).
    catchup_max_posts: int = 20
    post_delay_ms: int = 750


@dataclass(frozen=True, slots=True)
class KbCardsConfig:
    """Kill-card rendering (killboard GDD §7.1). Icons come from Albion's
    documented, cacheable render service — never the flaky gameinfo API.

    The ``brand_*``/``accent_color`` knobs skin cards for the hosting Discord
    (Dead Gaming by default): a corner watermark logo, an accent colour on the
    header/rank boards, and a footer tagline. ``brand_logo_path`` empty → the
    bundled Dead roundel; a path override swaps in any PNG. ``show_loot_value``
    prints the victim's estimated silver loot value on the card — it only
    resolves when ``killboard.market.enabled`` is on (the market client prices
    the loadout); off, the card simply omits the value."""

    enabled: bool = True
    icon_cache_dir: str = "/var/lib/cortana/killboard/icons"
    render_base: str = "https://render.albiononline.com/v1"
    brand_name: str = "Dead Gaming"
    brand_logo_path: str = ""
    accent_color: str = "#E11212"
    show_loot_value: bool = True
    daily_ranking_card: bool = True
    reaper_watermark: bool = True


@dataclass(frozen=True, slots=True)
class KbRankingsConfig:
    """Leaderboards computed from the event store (killboard GDD §8). Scheduled
    posts live in the DB (schedules table), not config — a list-of-dicts can't
    go through the flat scalar schema."""

    timezone: str = "UTC"


@dataclass(frozen=True, slots=True)
class KbBattlesConfig:
    """Large-fight summaries (killboard GDD §9); a battle posts only past a
    participation threshold so routine skirmishes don't spam."""

    channel: int = 0
    min_players: int = 20
    min_fame: int = 5_000_000


@dataclass(frozen=True, slots=True)
class KbStorageConfig:
    """The killboard's OWN sqlite file — never CORTANA's. Irreplaceable: the
    API cannot re-serve old events (killboard GDD §2.4)."""

    db_path: str = "/var/lib/cortana/killboard/killboard.db"


@dataclass(frozen=True, slots=True)
class KbStalenessConfig:
    """When a quiet poller becomes a reportable condition (killboard GDD §13)."""

    warn_after_minutes: int = 30
    no_events_notice_hours: int = 6


@dataclass(frozen=True, slots=True)
class KbMarketConfig:
    """Albion market-data layer (AODP). Prices item loot value onto kill cards
    and powers the /market lookup commands. Optional; OFF by default (it hits a
    third, crowd-sourced API, so it's opt-in). Region is inherited from
    ``killboard.region`` — the AODP host is derived from it."""

    enabled: bool = False
    #: In-memory price-cache TTL. Prices move on the minute at most, so caching
    #: hard keeps well under the AODP rate limit (180/min).
    cache_ttl_s: int = 300
    request_timeout_s: int = 10
    #: Default item quality (1 Normal … 5 Masterpiece) when a lookup omits it.
    default_quality: int = 1
    #: Cities compared by /market price and used to reference-price kill loot.
    default_cities: tuple[str, ...] = (
        "Caerleon",
        "Bridgewatch",
        "Lymhurst",
        "Martlock",
        "Fort Sterling",
        "Thetford",
    )
    user_agent: str = "DeadBot-Killboard (self-hosted; contact your guild admin)"


@dataclass(frozen=True, slots=True)
class KbPublicJuicyConfig:
    """Server-wide "notable kills" highlights feed (the public juicy channel).

    OFF by default. When on, a separate supervised loop watches Albion's
    *whole-server* recent kill feed (not the tracked guild) and posts kills that
    clear a bar to ``killboard.feed.juicy_channel`` — the "juicy" highlights other
    corps' killbots show. A kill qualifies on FAME first (free, from the event) OR
    on market LOOT value second (priced only for the ones that miss on fame), so a
    low-fame/high-loot gank still lands. Reuses ``feed.juicy_min_fame`` /
    ``feed.juicy_min_loot`` as the two bars. When enabled it OWNS the juicy
    channel — the guild feed stops mirroring the corp's own kills there, so a
    corp kill (which also appears in the global feed) is never double-posted.
    Loot pricing needs ``killboard.market.enabled``."""

    enabled: bool = False
    #: Seconds between global-feed scans. The public feed is a sampled highlight
    #: reel, not an exact-once log, so this trades coverage for API politeness.
    interval_seconds: int = 90
    #: Pages of 51 recent global events scanned per cycle (deeper = more coverage
    #: of a fast firehose, but more requests + more loot pricing per scan).
    scan_pages: int = 2
    #: HARD cap on posts per scan — the real volume control. Every geared kill on
    #: a whole server prices in the millions, so a threshold alone lets dozens
    #: through; the scan ranks qualifiers by value and posts only the top N.
    max_posts_per_scan: int = 5
    #: Per-scan LOOT-PRICING budget. Fame is free; only sub-fame events are priced
    #: to check the loot bar, and the global firehose is ~all sub-fame — so this
    #: caps how many AODP lookups one scan makes (priced with bounded concurrency),
    #: keeping the bot a polite client of the shared, rate-limited market API.
    max_priced_per_scan: int = 60


@dataclass(frozen=True, slots=True)
class KillboardConfig:
    """Albion Online killboard add-on (killboard GDD). Optional section; OFF by
    default — the module only starts when ``enabled`` is set AND a guild AND a
    feed channel are configured (the gate lives in ``KillboardModule.enabled``,
    not validation, so a half-set config degrades gracefully instead of
    crashing the voice bot)."""

    enabled: bool = False
    #: API host selector — west | europe | east (killboard GDD §2.2).
    region: str = "west"
    #: Resolved to an id at startup via /search; or set ``guild_id`` directly.
    guild_name: str = ""
    guild_id: str = ""
    poller: KbPollerConfig = field(default_factory=KbPollerConfig)
    feed: KbFeedConfig = field(default_factory=KbFeedConfig)
    cards: KbCardsConfig = field(default_factory=KbCardsConfig)
    rankings: KbRankingsConfig = field(default_factory=KbRankingsConfig)
    battles: KbBattlesConfig = field(default_factory=KbBattlesConfig)
    storage: KbStorageConfig = field(default_factory=KbStorageConfig)
    staleness: KbStalenessConfig = field(default_factory=KbStalenessConfig)
    market: KbMarketConfig = field(default_factory=KbMarketConfig)
    public_juicy: KbPublicJuicyConfig = field(default_factory=KbPublicJuicyConfig)


@dataclass(frozen=True, slots=True)
class AuraConfig:
    discord: DiscordConfig
    wake: WakeConfig
    capture: CaptureConfig
    stt: SttConfig
    matching: MatchingConfig
    incidents: IncidentsConfig
    discipline: DisciplineConfig
    tts: TtsConfig
    gazetteer: GazetteerConfig
    ipc: IpcConfig
    health: HealthConfig
    database: DatabaseConfig
    chat: ChatConfig = field(default_factory=ChatConfig)
    conversation: ConversationConfig = field(default_factory=ConversationConfig)
    routing: RoutingFileConfig = field(default_factory=RoutingFileConfig)
    dialog: DialogConfig = field(default_factory=DialogConfig)
    fun: FunConfig = field(default_factory=FunConfig)
    areas: AreasConfig = field(default_factory=AreasConfig)
    nlu: NluConfig = field(default_factory=NluConfig)
    killboard: KillboardConfig = field(default_factory=KillboardConfig)


# ── schema-driven validation ─────────────────────────────────────────────────
# The walkers below are a generic interpreter over cortana.config_schema:
# they know how to check a Key, not what any particular key means.

#: YAML 1.1 parses bare ``off``/``no`` as boolean False and ``on``/``yes`` as
#: True. For any key whose legal values include one of those WORDS, the
#: boolean is coerced back to the word so operators don't need quotes — an
#: unquoted ``join_announcement: off`` once crash-looped a deployment.
_YAML11_WORDS: dict[bool, tuple[str, ...]] = {
    False: ("off", "no", "false"),
    True: ("on", "yes", "true"),
}


def _fix(action: str) -> str:
    return f" — Fix: {action}"


def _coerce_choice(key: Key, value: Any) -> Any:
    """Generic YAML-1.1 boolean→word coercion for a choices-key."""
    choices = key.choices or ()
    if isinstance(value, bool):
        for word in _YAML11_WORDS[value]:
            if word in choices:
                return word
    return value


def _check_choice(key: Key, value: Any, dotted: str) -> str:
    value = _coerce_choice(key, value)
    normalised = str(value).lower()
    choices = key.choices or ()
    if normalised not in choices:
        raise ConfigError(
            f"{dotted}: must be one of {'|'.join(choices)}, got {normalised!r}"
            + _fix(f"use one of {'|'.join(choices)}")
        )
    return normalised


def _check_range(key: Key, value: float, dotted: str) -> None:
    if key.exclusive_minimum:
        if key.minimum is not None and value <= key.minimum:
            bound = "> 0" if key.minimum == 0 else f"> {key.minimum:g}"
            raise ConfigError(
                f"{dotted}: must be {bound}, got {value}" + _fix(f"set a value {bound}")
            )
        return
    if key.minimum is not None and key.maximum is not None:
        if not (key.minimum <= value <= key.maximum):
            raise ConfigError(
                f"{dotted}: must be between {key.minimum:g} and {key.maximum:g}, "
                f"got {value}" + _fix(f"set a value in [{key.minimum:g}, {key.maximum:g}]")
            )
        return
    if key.minimum is not None and value < key.minimum:
        raise ConfigError(
            f"{dotted}: must be >= {key.minimum:g}, got {value}"
            + _fix(f"set a value >= {key.minimum:g}")
        )


def _check_int(value: Any, dotted: str) -> int:
    # bool is a subclass of int — reject it explicitly for numeric keys.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"{dotted}: expected int, got {type(value).__name__}" + _fix("set a plain integer")
        )
    return value


def _check_float(value: Any, dotted: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(
            f"{dotted}: expected float, got {type(value).__name__}" + _fix("set a number")
        )
    return float(value)


def _validate_key(key: Key, section: dict[str, Any]) -> Any:
    dotted = key.path
    value = section.get(key.name, _MISSING)
    if value is _MISSING:
        if key.default is REQUIRED:
            raise ConfigError(
                f"{dotted}: missing required key"
                + _fix(f"add {key.name} to the {key.section} section (see cortana.yaml.example)")
            )
        return key.default
    if key.coerce is not None:
        value = key.coerce(value)

    if key.choices is not None and key.type == "str":
        return _check_choice(key, value, dotted)

    if key.type == "int":
        out_i = _check_int(value, dotted)
        _check_range(key, out_i, dotted)
        return out_i
    if key.type == "float":
        out_f = _check_float(value, dotted)
        _check_range(key, out_f, dotted)
        return out_f
    if key.type == "bool":
        if not isinstance(value, bool):
            raise ConfigError(
                f"{dotted}: expected bool, got {type(value).__name__}" + _fix("set true or false")
            )
        return value
    if key.type == "str":
        if not isinstance(value, str):
            raise ConfigError(
                f"{dotted}: expected str, got {type(value).__name__}" + _fix("set a string")
            )
        return value
    if key.type == "opt_str":
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ConfigError(
                f"{dotted}: expected string or null, got {type(value).__name__}"
                + _fix("set a string, or null to disable")
            )
        return value
    if key.type == "int_list":
        if not isinstance(value, list):
            raise ConfigError(
                f"{dotted}: expected list, got {type(value).__name__}"
                + _fix("set a YAML list, e.g. [123, 456]")
            )
        for i, item in enumerate(value):
            _check_int(item, f"{dotted}[{i}]")
        return tuple(value)
    if key.type == "str_list":
        if not isinstance(value, list):
            raise ConfigError(
                f"{dotted}: expected list, got {type(value).__name__}"
                + _fix("set a YAML list, e.g. [high]")
            )
        if key.choices is not None:
            return tuple(_check_choice(key, item, f"{dotted}[{i}]") for i, item in enumerate(value))
        # Free-form string lists (e.g. wake.extra_models paths) keep their
        # case — Linux paths are case-sensitive.
        for i, item in enumerate(value):
            if not isinstance(item, str):
                raise ConfigError(
                    f"{dotted}[{i}]: expected str, got {type(item).__name__}" + _fix("set a string")
                )
        return tuple(value)
    raise AssertionError(f"unhandled schema type {key.type!r} for {dotted}")  # pragma: no cover


def _reject_unknown_keys(section_path: str, mapping: dict[str, Any]) -> None:
    display = section_path or "top level"
    allowed = {k.name for k in keys_in_section(section_path)} | {
        s.name for s in child_sections(section_path)
    }
    for name in mapping:
        if str(name) in allowed:
            continue
        suggestions = difflib.get_close_matches(str(name), sorted(allowed), n=1, cutoff=0.6)
        if suggestions:
            hint = _fix(f"did you mean {suggestions[0]!r}?")
        else:
            hint = _fix("remove it (unknown keys would otherwise hide a typo as a default)")
        raise ConfigError(f"{display}: unknown key {name!r}" + hint)


def _walk_section(section_path: str, mapping: dict[str, Any], values: dict[str, Any]) -> None:
    """Validate one mapping node: unknown keys, leaf keys, child sections."""
    _reject_unknown_keys(section_path, mapping)
    for key in keys_in_section(section_path):
        values[key.path] = _validate_key(key, mapping)
    for child in child_sections(section_path):
        raw = mapping.get(child.name, _MISSING)
        if raw is _MISSING:
            if not child.optional:
                raise ConfigError(
                    f"{child.path}: missing required section"
                    + _fix(f"add a {child.name}: section (see cortana.yaml.example)")
                )
            _walk_section(child.path, {}, values)
            continue
        # A bare `section:` header with every key commented out parses as
        # None. Treat it as empty so the error the operator sees names the
        # missing KEY, not "expected a mapping, got NoneType".
        if raw is None:
            _walk_section(child.path, {}, values)
            continue
        if not isinstance(raw, dict):
            raise ConfigError(
                f"{child.path}: expected a mapping, got {type(raw).__name__}"
                + _fix(f"indent the {child.name} keys under the {child.name}: header")
            )
        _walk_section(child.path, raw, values)


def _validate_tree(data: dict[str, Any]) -> dict[str, Any]:
    """Validate the whole YAML tree; returns ``{key_path: value}``."""
    values: dict[str, Any] = {}
    _walk_section("", data, values)
    return values


def _section_values(data: dict[str, Any], root: str) -> dict[str, Any]:
    """Validate the subtree rooted at top-level section ``root`` only."""
    values: dict[str, Any] = {}
    raw = data.get(root, _MISSING)
    if raw is _MISSING or raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{root}: expected a mapping, got {type(raw).__name__}"
            + _fix(f"indent the {root} keys under the {root}: header")
        )
    _walk_section(root, raw, values)
    return values


# ── dataclass assembly ───────────────────────────────────────────────────────


def _assemble_discord(v: dict[str, Any]) -> DiscordConfig:
    return DiscordConfig(
        token_file=v["discord.token_file"],
        guild_id=v["discord.guild_id"],
        channels=ChannelsConfig(
            intel_alerts=v["discord.channels.intel_alerts"],
            intel_live=v["discord.channels.intel_live"],
            health=v["discord.channels.health"],
            transcript=v["discord.channels.transcript"],
        ),
        roles=RolesConfig(
            pilot=v["discord.roles.pilot"],
            fc=v["discord.roles.fc"],
        ),
        watch_voice_channels=v["discord.watch_voice_channels"],
        auto_join=v["discord.auto_join"],
        mentions_enabled=v["discord.mentions_enabled"],
        here_on_severity=v["discord.here_on_severity"],
        join_announcement=v["discord.join_announcement"],
    )


def _assemble_wake(v: dict[str, Any]) -> WakeConfig:
    return WakeConfig(
        model=v["wake.model"],
        threshold=v["wake.threshold"],
        refractory_ms=v["wake.refractory_ms"],
        ack=v["wake.ack"],
        vad_threshold=v["wake.vad_threshold"],
        extra_models=v["wake.extra_models"],
    )


def _assemble_capture(v: dict[str, Any]) -> CaptureConfig:
    return CaptureConfig(
        preroll_ms=v["capture.preroll_ms"],
        endpoint_silence_ms=v["capture.endpoint_silence_ms"],
        max_utterance_ms=v["capture.max_utterance_ms"],
        vad_aggressiveness=v["capture.vad_aggressiveness"],
        streaming=v["capture.streaming"],
        partial_decode_ms=v["capture.partial_decode_ms"],
        partial_min_speech_ms=v["capture.partial_min_speech_ms"],
        early_commit_min_logprob=v["capture.early_commit_min_logprob"],
    )


def _assemble_stt(v: dict[str, Any]) -> SttConfig:
    return SttConfig(
        backend=v["stt.backend"],
        model=v["stt.model"],
        compute_type=v["stt.compute_type"],
        cpu_threads=v["stt.cpu_threads"],
        bias_with_gazetteer=v["stt.bias_with_gazetteer"],
        whisper_cpp_url=v["stt.whisper_cpp_url"],
        relay_min_logprob=v["stt.relay_min_logprob"],
        relay_mode=v["stt.relay_mode"],
        watchdog_s=v["stt.watchdog_s"],
        no_repeat_ngram_size=v["stt.no_repeat_ngram_size"],
    )


def _assemble_matching(v: dict[str, Any]) -> MatchingConfig:
    return MatchingConfig(
        phonetic_weight=v["matching.phonetic_weight"],
        text_weight=v["matching.text_weight"],
        full_map_fallback=v["matching.full_map_fallback"],
        index=IndexConfig(
            enabled=v["matching.index.enabled"],
            min_candidates=v["matching.index.min_candidates"],
        ),
        tiers=TiersConfig(
            high_min=v["matching.tiers.high_min"],
            high_margin=v["matching.tiers.high_margin"],
            medium_min=v["matching.tiers.medium_min"],
        ),
        priors=PriorsConfig(
            recency_weight=v["matching.priors.recency_weight"],
            recency_window_min=v["matching.priors.recency_window_min"],
            proximity_weight=v["matching.priors.proximity_weight"],
            proximity_max_jumps=v["matching.priors.proximity_max_jumps"],
            reporter_history_weight=v["matching.priors.reporter_history_weight"],
            home_weight=v["matching.priors.home_weight"],
        ),
    )


def _assemble_incidents(v: dict[str, Any]) -> IncidentsConfig:
    return IncidentsConfig(
        dedupe_window_s=v["incidents.dedupe_window_s"],
        stale_after_min=v["incidents.stale_after_min"],
        cancel_window_s=v["incidents.cancel_window_s"],
        auto_resolve_min=v["incidents.auto_resolve_min"],
    )


def _assemble_discipline(v: dict[str, Any]) -> DisciplineConfig:
    return DisciplineConfig(
        user_cooldown_s=v["discipline.user_cooldown_s"],
        circuit_breaker=CircuitBreakerConfig(
            max_mentions=v["discipline.circuit_breaker.max_mentions"],
            window_min=v["discipline.circuit_breaker.window_min"],
        ),
        personal_pings_max=v["discipline.personal_pings_max"],
    )


def _assemble_tts(v: dict[str, Any]) -> TtsConfig:
    return TtsConfig(
        enabled=v["tts.enabled"],
        voice=v["tts.voice"],
        binary=v["tts.binary"],
        max_utterance_s=v["tts.max_utterance_s"],
        effect=v["tts.effect"],
        personality=v["tts.personality"],
    )


def _assemble_chat(v: dict[str, Any]) -> ChatConfig:
    return ChatConfig(
        enabled=v["chat.enabled"],
        backend=v["chat.backend"],
        local_url=v["chat.local_url"],
        model=v["chat.model"],
        api_key_file=v["chat.api_key_file"],
        max_tokens=v["chat.max_tokens"],
        user_cooldown_s=v["chat.user_cooldown_s"],
        timeout_s=v["chat.timeout_s"],
        web_search=v["chat.web_search"],
        answer_channel=v["chat.answer_channel"],
    )


def _assemble_conversation(v: dict[str, Any]) -> ConversationConfig:
    return ConversationConfig(
        enabled=v["conversation.enabled"],
        backend=v["conversation.backend"],
        local_url=v["conversation.local_url"],
        model=v["conversation.model"],
        api_key_file=v["conversation.api_key_file"],
        max_tokens=v["conversation.max_tokens"],
        turn_taking_seconds=v["conversation.turn_taking_seconds"],
        max_history_turns=v["conversation.max_history_turns"],
        max_turns=v["conversation.max_turns"],
        session_ttl_seconds=v["conversation.session_ttl_seconds"],
        user_cooldown_s=v["conversation.user_cooldown_s"],
        quiet_during_ops=v["conversation.quiet_during_ops"],
        quiet_window_min=v["conversation.quiet_window_min"],
        math_tool=v["conversation.math_tool"],
        timeout_s=v["conversation.timeout_s"],
        overflow_channel=v["conversation.overflow_channel"],
    )


def _assemble_gazetteer(v: dict[str, Any]) -> GazetteerConfig:
    return GazetteerConfig(
        file=v["gazetteer.file"],
        home_system=v["gazetteer.home_system"],
        include_all=v["gazetteer.include_all"],
    )


def _assemble_fun(v: dict[str, Any]) -> FunConfig:
    return FunConfig(
        enabled=v["fun.enabled"],
        fact_cooldown_s=v["fun.fact_cooldown_s"],
        insult_cooldown_s=v["fun.insult_cooldown_s"],
        insults_spicy=v["fun.insults_spicy"],
        max_speak_s=v["fun.max_speak_s"],
    )


def _assemble_areas(v: dict[str, Any]) -> AreasConfig:
    return AreasConfig(learn=v["areas.learn"], max_per_guild=v["areas.max_per_guild"])


def _assemble_nlu(v: dict[str, Any]) -> NluConfig:
    return NluConfig(
        understanding=v["nlu.understanding"],
        url=v["nlu.url"],
        model=v["nlu.model"],
        timeout_s=v["nlu.timeout_s"],
    )


def _assemble_killboard(v: dict[str, Any]) -> KillboardConfig:
    return KillboardConfig(
        enabled=v["killboard.enabled"],
        region=v["killboard.region"],
        guild_name=v["killboard.guild_name"],
        guild_id=v["killboard.guild_id"],
        poller=KbPollerConfig(
            interval_seconds=v["killboard.poller.interval_seconds"],
            request_timeout_seconds=v["killboard.poller.request_timeout_seconds"],
            max_retries=v["killboard.poller.max_retries"],
            backoff_base_seconds=v["killboard.poller.backoff_base_seconds"],
            page_limit=v["killboard.poller.page_limit"],
            max_backfill_pages=v["killboard.poller.max_backfill_pages"],
            track_deaths=v["killboard.poller.track_deaths"],
            deaths_post_window_minutes=v["killboard.poller.deaths_post_window_minutes"],
        ),
        feed=KbFeedConfig(
            kills_channel=v["killboard.feed.kills_channel"],
            deaths_channel=v["killboard.feed.deaths_channel"],
            min_fame=v["killboard.feed.min_fame"],
            juicy_channel=v["killboard.feed.juicy_channel"],
            juicy_min_fame=v["killboard.feed.juicy_min_fame"],
            juicy_min_loot=v["killboard.feed.juicy_min_loot"],
            ignore_deaths_below_ip=v["killboard.feed.ignore_deaths_below_ip"],
            blob_participant_threshold=v["killboard.feed.blob_participant_threshold"],
            blob_channel=v["killboard.feed.blob_channel"],
            catchup_max_posts=v["killboard.feed.catchup_max_posts"],
            post_delay_ms=v["killboard.feed.post_delay_ms"],
        ),
        cards=KbCardsConfig(
            enabled=v["killboard.cards.enabled"],
            icon_cache_dir=v["killboard.cards.icon_cache_dir"],
            render_base=v["killboard.cards.render_base"],
            brand_name=v["killboard.cards.brand_name"],
            brand_logo_path=v["killboard.cards.brand_logo_path"],
            accent_color=v["killboard.cards.accent_color"],
            show_loot_value=v["killboard.cards.show_loot_value"],
            daily_ranking_card=v["killboard.cards.daily_ranking_card"],
            reaper_watermark=v["killboard.cards.reaper_watermark"],
        ),
        rankings=KbRankingsConfig(timezone=v["killboard.rankings.timezone"]),
        battles=KbBattlesConfig(
            channel=v["killboard.battles.channel"],
            min_players=v["killboard.battles.min_players"],
            min_fame=v["killboard.battles.min_fame"],
        ),
        storage=KbStorageConfig(db_path=v["killboard.storage.db_path"]),
        staleness=KbStalenessConfig(
            warn_after_minutes=v["killboard.staleness.warn_after_minutes"],
            no_events_notice_hours=v["killboard.staleness.no_events_notice_hours"],
        ),
        market=KbMarketConfig(
            enabled=v["killboard.market.enabled"],
            cache_ttl_s=v["killboard.market.cache_ttl_s"],
            request_timeout_s=v["killboard.market.request_timeout_s"],
            default_quality=v["killboard.market.default_quality"],
            default_cities=v["killboard.market.default_cities"],
            user_agent=v["killboard.market.user_agent"],
        ),
        public_juicy=KbPublicJuicyConfig(
            enabled=v["killboard.public_juicy.enabled"],
            interval_seconds=v["killboard.public_juicy.interval_seconds"],
            scan_pages=v["killboard.public_juicy.scan_pages"],
            max_posts_per_scan=v["killboard.public_juicy.max_posts_per_scan"],
            max_priced_per_scan=v["killboard.public_juicy.max_priced_per_scan"],
        ),
    )


def _assemble_dialog(v: dict[str, Any]) -> DialogConfig:
    return DialogConfig(
        window_ms=v["dialog.window_ms"],
        ack_grace_ms=v["dialog.ack_grace_ms"],
        endpoint_gap_floor_ms=v["dialog.endpoint_gap_floor_ms"],
        max_retries=v["dialog.max_retries"],
        confirm_reports=v["dialog.confirm_reports"],
        retry_min_logprob=v["dialog.retry_min_logprob"],
    )


def _assemble(values: dict[str, Any]) -> AuraConfig:
    return AuraConfig(
        discord=_assemble_discord(values),
        wake=_assemble_wake(values),
        capture=_assemble_capture(values),
        stt=_assemble_stt(values),
        matching=_assemble_matching(values),
        incidents=_assemble_incidents(values),
        discipline=_assemble_discipline(values),
        tts=_assemble_tts(values),
        gazetteer=_assemble_gazetteer(values),
        ipc=IpcConfig(socket=values["ipc.socket"]),
        health=HealthConfig(
            report_interval_min=values["health.report_interval_min"],
            voice_silence_alarm_s=values["health.voice_silence_alarm_s"],
        ),
        database=DatabaseConfig(path=values["database.path"]),
        chat=_assemble_chat(values),
        conversation=_assemble_conversation(values),
        routing=RoutingFileConfig(file=values["routing.file"]),
        dialog=_assemble_dialog(values),
        fun=_assemble_fun(values),
        areas=_assemble_areas(values),
        nlu=_assemble_nlu(values),
        killboard=_assemble_killboard(values),
    )


# ── single-section builders (kept for tests and targeted validation) ─────────


def _build_discord(data: dict[str, Any]) -> DiscordConfig:
    return _assemble_discord(_section_values(data, "discord"))


def _build_wake(data: dict[str, Any]) -> WakeConfig:
    return _assemble_wake(_section_values(data, "wake"))


def _build_dialog(data: dict[str, Any]) -> DialogConfig:
    return _assemble_dialog(_section_values(data, "dialog"))


def _build_capture(data: dict[str, Any]) -> CaptureConfig:
    return _assemble_capture(_section_values(data, "capture"))


def _build_stt(data: dict[str, Any]) -> SttConfig:
    return _assemble_stt(_section_values(data, "stt"))


def _build_chat(data: dict[str, Any]) -> ChatConfig:
    return _assemble_chat(_section_values(data, "chat"))


def _build_gazetteer(data: dict[str, Any]) -> GazetteerConfig:
    return _assemble_gazetteer(_section_values(data, "gazetteer"))


def _personality(value: str) -> str:
    """Validate a tts.personality value (schema-driven; kept as a helper)."""
    key = key_by_path("tts.personality")
    return _check_choice(key, value, key.path)


# ── public API ───────────────────────────────────────────────────────────────


def load_config(path: str | Path) -> AuraConfig:
    """Read and validate ``cortana.yaml``. Raises :class:`ConfigError` on any problem."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {p}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: top level must be a mapping")

    cfg = _assemble(_validate_tree(data))
    for check in CROSS_CHECKS:
        problem = check.fn(cfg)
        if problem is not None:
            raise ConfigError(problem)
    return cfg


def _value_at(cfg: AuraConfig, path: str) -> Any:
    """Resolve a schema key path against the loaded dataclass tree."""
    return reduce(getattr, path.split("."), cfg)


def diff_configs(old: AuraConfig, new: AuraConfig) -> dict[Reload, tuple[str, ...]]:
    """Bucket every changed key by its reload class.

    Every bucket is present (possibly empty) so callers can render the full
    receipt without membership checks. Used by :mod:`cortana.reload` to
    apply hot keys, trigger engine reloads, and report restart-bound edits
    as "restart pending" instead of silently absorbing them.
    """
    buckets: dict[Reload, list[str]] = {r: [] for r in Reload}
    for key in KEYS:
        if _value_at(old, key.path) != _value_at(new, key.path):
            buckets[key.reload].append(key.path)
    return {r: tuple(paths) for r, paths in buckets.items()}


class ConfigHolder:
    """Holds the live :class:`AuraConfig` and swaps it atomically on reload.

    ``__main__`` installs the SIGHUP handler and calls :meth:`reload` (or,
    preferably, :func:`cortana.reload.reload_all`, which validates every
    config input together and swaps all-or-nothing via :meth:`replace`);
    every other module keeps a reference to the holder and reads
    :attr:`current` at the point of use. If a reload fails validation the
    previous config stays in force and the :class:`ConfigError` propagates
    to the caller.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._current = load_config(self._path)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def current(self) -> AuraConfig:
        with self._lock:
            return self._current

    def replace(self, new: AuraConfig) -> None:
        """Swap in an already-validated config (the reload transaction)."""
        with self._lock:
            self._current = new
        log.info("config_swapped", path=str(self._path))

    def reload(self) -> AuraConfig:
        """Re-read the file. On failure the old config is kept and the error raised."""
        new = load_config(self._path)
        with self._lock:
            self._current = new
        log.info("config_reloaded", path=str(self._path))
        return new
