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
    #: Claude model for override replies. Default is the cheapest tier
    #: (fractions of a cent per question); claude-opus-4-8 is the smarter,
    #: ~10x-the-price alternative.
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
class CaptureConfig:
    preroll_ms: int
    endpoint_silence_ms: int
    max_utterance_ms: int
    vad_aggressiveness: int  # webrtcvad 0–3


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
    #: GDD §20 "STT worker hang" watchdog deadline, seconds. The whisper-cpp
    #: HTTP timeout is derived slightly below it (the socket gives up before
    #: the watchdog abandons the worker thread).
    watchdog_s: float = 15.0


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
class MatchingConfig:
    phonetic_weight: float
    text_weight: float
    tiers: TiersConfig
    priors: PriorsConfig


@dataclass(frozen=True, slots=True)
class IncidentsConfig:
    dedupe_window_s: int
    stale_after_min: int
    cancel_window_s: int


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
    routing: RoutingFileConfig = field(default_factory=RoutingFileConfig)
    dialog: DialogConfig = field(default_factory=DialogConfig)


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
    )


def _assemble_matching(v: dict[str, Any]) -> MatchingConfig:
    return MatchingConfig(
        phonetic_weight=v["matching.phonetic_weight"],
        text_weight=v["matching.text_weight"],
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
        model=v["chat.model"],
        api_key_file=v["chat.api_key_file"],
        max_tokens=v["chat.max_tokens"],
        user_cooldown_s=v["chat.user_cooldown_s"],
        timeout_s=v["chat.timeout_s"],
        web_search=v["chat.web_search"],
        answer_channel=v["chat.answer_channel"],
    )


def _assemble_gazetteer(v: dict[str, Any]) -> GazetteerConfig:
    return GazetteerConfig(
        file=v["gazetteer.file"],
        home_system=v["gazetteer.home_system"],
        include_all=v["gazetteer.include_all"],
    )


def _assemble_dialog(v: dict[str, Any]) -> DialogConfig:
    return DialogConfig(
        window_ms=v["dialog.window_ms"],
        ack_grace_ms=v["dialog.ack_grace_ms"],
        endpoint_gap_floor_ms=v["dialog.endpoint_gap_floor_ms"],
        max_retries=v["dialog.max_retries"],
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
        routing=RoutingFileConfig(file=values["routing.file"]),
        dialog=_assemble_dialog(values),
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
