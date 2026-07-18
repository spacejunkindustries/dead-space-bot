"""Configuration loading, validation, and hot-reload for AURA Brain.

Mirrors ``config/aura.yaml.example`` / GDD §16 one dataclass per section.
``load_config`` validates types and ranges and raises :class:`ConfigError`
messages that always name the offending key path (``wake.threshold: ...``).

Hot reload: ``__main__`` owns the SIGHUP handler and calls
``ConfigHolder.reload()``; long-lived objects hold the :class:`ConfigHolder`
and read ``holder.current`` at the point of use, never a cached snapshot.

Secrets: the Discord token is NOT config. Config carries ``discord.token_file``
(a path) only. ``aura.dsc.bot`` reads the token at startup from
``$CREDENTIALS_DIRECTORY/token`` (systemd ``LoadCredential=``, GDD §18/§22),
falling back to ``discord.token_file`` for development runs.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml

log = structlog.get_logger(__name__)

_MISSING = object()

STT_BACKENDS = ("faster-whisper", "whisper-cpp")


class ConfigError(Exception):
    """Config file missing, unreadable, or invalid. Message names the bad key."""


# ── section dataclasses (GDD §16) ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ChannelsConfig:
    intel_alerts: int
    intel_live: int
    health: int


@dataclass(frozen=True, slots=True)
class RolesConfig:
    pilot: int  # gate: may trigger mentions
    fc: int  # gate under fleetmode


@dataclass(frozen=True, slots=True)
class DiscordConfig:
    token_file: str  # dev fallback only; production uses LoadCredential=
    guild_id: int
    channels: ChannelsConfig
    roles: RolesConfig
    watch_voice_channels: tuple[int, ...]
    auto_join: bool
    #: Master switch for role/@here pings. False = "silent mode": incidents and
    #: relays still post to the channel, but AURA mentions nobody and the
    #: @Pilot trigger gate is lifted (there is nothing to protect). Turn on once
    #: real roles are wired into routing.yaml.
    mentions_enabled: bool = True


@dataclass(frozen=True, slots=True)
class WakeConfig:
    model: str
    threshold: float
    refractory_ms: int


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    preroll_ms: int
    endpoint_silence_ms: int
    max_utterance_ms: int
    vad_aggressiveness: int  # webrtcvad 0–3


@dataclass(frozen=True, slots=True)
class SttConfig:
    backend: str  # "faster-whisper" | "whisper-cpp"
    model: str
    compute_type: str
    cpu_threads: int
    bias_with_gazetteer: bool
    whisper_cpp_url: str


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
    # Ducking level and talk-over suppression are fixed playback mechanics in
    # Ears (ears/src/playback.rs) — deliberately not tunables here.


@dataclass(frozen=True, slots=True)
class GazetteerConfig:
    file: str
    home_system: str | None  # None/empty = no home-bias prior (nomadic corp)
    include_all: bool = False  # nomadic mode: entire seeded map active (GDD §8.1)


@dataclass(frozen=True, slots=True)
class IpcConfig:
    socket: str
    # Ears' outbound ring size lives in /etc/aura/ears.yaml (buffer_seconds):
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


# ── validation helpers ───────────────────────────────────────────────────────


def _mapping(data: Any, dotted: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ConfigError(f"{dotted}: expected a mapping, got {type(data).__name__}")
    return data


def _section(data: dict[str, Any], dotted: str) -> dict[str, Any]:
    value = data.get(dotted.rsplit(".", 1)[-1], _MISSING)
    if value is _MISSING:
        raise ConfigError(f"{dotted}: missing required section")
    return _mapping(value, dotted)


def _get(section: dict[str, Any], dotted: str, expected: type, default: Any = _MISSING) -> Any:
    key = dotted.rsplit(".", 1)[-1]
    value = section.get(key, _MISSING)
    if value is _MISSING:
        if default is _MISSING:
            raise ConfigError(f"{dotted}: missing required key")
        return default
    # bool is a subclass of int — reject it explicitly for numeric keys.
    if isinstance(value, bool) and expected in (int, float):
        raise ConfigError(f"{dotted}: expected {expected.__name__}, got bool")
    if expected is float and isinstance(value, int):
        return float(value)
    if not isinstance(value, expected):
        raise ConfigError(f"{dotted}: expected {expected.__name__}, got {type(value).__name__}")
    return value


def _int_list(section: dict[str, Any], dotted: str) -> tuple[int, ...]:
    value = _get(section, dotted, list)
    for i, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, int):
            raise ConfigError(f"{dotted}[{i}]: expected int, got {type(item).__name__}")
    return tuple(value)


def _in_range(value: float, dotted: str, lo: float, hi: float) -> float:
    if not (lo <= value <= hi):
        raise ConfigError(f"{dotted}: must be between {lo} and {hi}, got {value}")
    return value


def _positive(value: float, dotted: str) -> Any:
    if value <= 0:
        raise ConfigError(f"{dotted}: must be > 0, got {value}")
    return value


def _non_negative(value: float, dotted: str) -> Any:
    if value < 0:
        raise ConfigError(f"{dotted}: must be >= 0, got {value}")
    return value


# ── section builders ─────────────────────────────────────────────────────────


def _build_discord(data: dict[str, Any]) -> DiscordConfig:
    s = _section(data, "discord")
    channels = _section(s, "discord.channels")
    roles = _section(s, "discord.roles")
    return DiscordConfig(
        token_file=_get(s, "discord.token_file", str),
        guild_id=_get(s, "discord.guild_id", int),
        channels=ChannelsConfig(
            intel_alerts=_get(channels, "discord.channels.intel_alerts", int),
            intel_live=_get(channels, "discord.channels.intel_live", int),
            health=_get(channels, "discord.channels.health", int),
        ),
        roles=RolesConfig(
            pilot=_get(roles, "discord.roles.pilot", int),
            fc=_get(roles, "discord.roles.fc", int),
        ),
        watch_voice_channels=_int_list(s, "discord.watch_voice_channels"),
        auto_join=_get(s, "discord.auto_join", bool, default=True),
        mentions_enabled=_get(s, "discord.mentions_enabled", bool, default=True),
    )


def _build_wake(data: dict[str, Any]) -> WakeConfig:
    s = _section(data, "wake")
    return WakeConfig(
        model=_get(s, "wake.model", str),
        threshold=_in_range(_get(s, "wake.threshold", float), "wake.threshold", 0.0, 1.0),
        refractory_ms=_positive(_get(s, "wake.refractory_ms", int), "wake.refractory_ms"),
    )


def _build_capture(data: dict[str, Any]) -> CaptureConfig:
    s = _section(data, "capture")
    return CaptureConfig(
        preroll_ms=_positive(_get(s, "capture.preroll_ms", int), "capture.preroll_ms"),
        endpoint_silence_ms=_positive(
            _get(s, "capture.endpoint_silence_ms", int), "capture.endpoint_silence_ms"
        ),
        max_utterance_ms=_positive(
            _get(s, "capture.max_utterance_ms", int), "capture.max_utterance_ms"
        ),
        vad_aggressiveness=int(
            _in_range(
                _get(s, "capture.vad_aggressiveness", int), "capture.vad_aggressiveness", 0, 3
            )
        ),
    )


def _build_stt(data: dict[str, Any]) -> SttConfig:
    s = _section(data, "stt")
    backend = _get(s, "stt.backend", str)
    if backend not in STT_BACKENDS:
        raise ConfigError(f"stt.backend: must be one of {list(STT_BACKENDS)}, got {backend!r}")
    return SttConfig(
        backend=backend,
        model=_get(s, "stt.model", str),
        compute_type=_get(s, "stt.compute_type", str),
        cpu_threads=_positive(_get(s, "stt.cpu_threads", int), "stt.cpu_threads"),
        bias_with_gazetteer=_get(s, "stt.bias_with_gazetteer", bool, default=True),
        whisper_cpp_url=_get(s, "stt.whisper_cpp_url", str),
    )


def _build_matching(data: dict[str, Any]) -> MatchingConfig:
    s = _section(data, "matching")
    tiers = _section(s, "matching.tiers")
    priors = _section(s, "matching.priors")
    return MatchingConfig(
        phonetic_weight=_in_range(
            _get(s, "matching.phonetic_weight", float), "matching.phonetic_weight", 0.0, 1.0
        ),
        text_weight=_in_range(
            _get(s, "matching.text_weight", float), "matching.text_weight", 0.0, 1.0
        ),
        tiers=TiersConfig(
            high_min=_in_range(
                _get(tiers, "matching.tiers.high_min", float), "matching.tiers.high_min", 0.0, 1.0
            ),
            high_margin=_in_range(
                _get(tiers, "matching.tiers.high_margin", float),
                "matching.tiers.high_margin",
                0.0,
                1.0,
            ),
            medium_min=_in_range(
                _get(tiers, "matching.tiers.medium_min", float),
                "matching.tiers.medium_min",
                0.0,
                1.0,
            ),
        ),
        priors=PriorsConfig(
            recency_weight=_non_negative(
                _get(priors, "matching.priors.recency_weight", float),
                "matching.priors.recency_weight",
            ),
            recency_window_min=_positive(
                _get(priors, "matching.priors.recency_window_min", int),
                "matching.priors.recency_window_min",
            ),
            proximity_weight=_non_negative(
                _get(priors, "matching.priors.proximity_weight", float),
                "matching.priors.proximity_weight",
            ),
            proximity_max_jumps=_positive(
                _get(priors, "matching.priors.proximity_max_jumps", int),
                "matching.priors.proximity_max_jumps",
            ),
            reporter_history_weight=_non_negative(
                _get(priors, "matching.priors.reporter_history_weight", float),
                "matching.priors.reporter_history_weight",
            ),
            home_weight=_non_negative(
                _get(priors, "matching.priors.home_weight", float), "matching.priors.home_weight"
            ),
        ),
    )


def _build_incidents(data: dict[str, Any]) -> IncidentsConfig:
    s = _section(data, "incidents")
    return IncidentsConfig(
        dedupe_window_s=_positive(
            _get(s, "incidents.dedupe_window_s", int), "incidents.dedupe_window_s"
        ),
        stale_after_min=_positive(
            _get(s, "incidents.stale_after_min", int), "incidents.stale_after_min"
        ),
        cancel_window_s=_positive(
            _get(s, "incidents.cancel_window_s", int), "incidents.cancel_window_s"
        ),
    )


def _build_discipline(data: dict[str, Any]) -> DisciplineConfig:
    s = _section(data, "discipline")
    cb = _section(s, "discipline.circuit_breaker")
    return DisciplineConfig(
        user_cooldown_s=_positive(
            _get(s, "discipline.user_cooldown_s", int), "discipline.user_cooldown_s"
        ),
        circuit_breaker=CircuitBreakerConfig(
            max_mentions=_positive(
                _get(cb, "discipline.circuit_breaker.max_mentions", int),
                "discipline.circuit_breaker.max_mentions",
            ),
            window_min=_positive(
                _get(cb, "discipline.circuit_breaker.window_min", int),
                "discipline.circuit_breaker.window_min",
            ),
        ),
        personal_pings_max=_positive(
            _get(s, "discipline.personal_pings_max", int, default=10),
            "discipline.personal_pings_max",
        ),
    )


def _build_tts(data: dict[str, Any]) -> TtsConfig:
    s = _section(data, "tts")
    return TtsConfig(
        enabled=_get(s, "tts.enabled", bool, default=True),
        voice=_get(s, "tts.voice", str),
        binary=_get(s, "tts.binary", str),
        max_utterance_s=_positive(_get(s, "tts.max_utterance_s", float), "tts.max_utterance_s"),
    )


def _build_gazetteer(data: dict[str, Any]) -> GazetteerConfig:
    s = _section(data, "gazetteer")
    # home_system is optional: an explicit null/empty (or missing) disables the
    # home-bias prior — nomadic corps have no home system (GDD §8.1/§8.4).
    raw_home = s.get("home_system", _MISSING)
    if raw_home is _MISSING or raw_home is None or raw_home == "":
        home_system: str | None = None
    elif isinstance(raw_home, str):
        home_system = raw_home
    else:
        raise ConfigError(
            f"gazetteer.home_system: expected string or null, got {type(raw_home).__name__}"
        )
    return GazetteerConfig(
        file=_get(s, "gazetteer.file", str),
        home_system=home_system,
        include_all=_get(s, "gazetteer.include_all", bool, default=False),
    )


def _build_ipc(data: dict[str, Any]) -> IpcConfig:
    s = _section(data, "ipc")
    return IpcConfig(
        socket=_get(s, "ipc.socket", str),
    )


def _build_health(data: dict[str, Any]) -> HealthConfig:
    s = _section(data, "health")
    return HealthConfig(
        report_interval_min=_positive(
            _get(s, "health.report_interval_min", int), "health.report_interval_min"
        ),
        voice_silence_alarm_s=_positive(
            _get(s, "health.voice_silence_alarm_s", int), "health.voice_silence_alarm_s"
        ),
    )


def _build_database(data: dict[str, Any]) -> DatabaseConfig:
    s = _section(data, "database")
    return DatabaseConfig(path=_get(s, "database.path", str))


# ── public API ───────────────────────────────────────────────────────────────


def load_config(path: str | Path) -> AuraConfig:
    """Read and validate ``aura.yaml``. Raises :class:`ConfigError` on any problem."""
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

    return AuraConfig(
        discord=_build_discord(data),
        wake=_build_wake(data),
        capture=_build_capture(data),
        stt=_build_stt(data),
        matching=_build_matching(data),
        incidents=_build_incidents(data),
        discipline=_build_discipline(data),
        tts=_build_tts(data),
        gazetteer=_build_gazetteer(data),
        ipc=_build_ipc(data),
        health=_build_health(data),
        database=_build_database(data),
    )


class ConfigHolder:
    """Holds the live :class:`AuraConfig` and swaps it atomically on reload.

    ``__main__`` installs the SIGHUP handler and calls :meth:`reload`; every
    other module keeps a reference to the holder and reads :attr:`current` at
    the point of use. If a reload fails validation the previous config stays
    in force and the :class:`ConfigError` propagates to the caller.
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

    def reload(self) -> AuraConfig:
        """Re-read the file. On failure the old config is kept and the error raised."""
        new = load_config(self._path)
        with self._lock:
            self._current = new
        log.info("config_reloaded", path=str(self._path))
        return new
