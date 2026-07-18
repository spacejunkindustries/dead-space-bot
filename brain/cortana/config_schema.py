"""Declarative configuration schema for ``cortana.yaml`` — GDD §16.

One :class:`Key` row per tunable. The table is the single source of truth
for:

- **validation** — type, range, allowed values, required/default, coercion
  (``cortana.config`` is a generic interpreter over this table);
- **reload classes** — what a change to each key needs to take effect
  (:class:`Reload`), driving ``cortana.config.diff_configs`` and the
  ``cortana.reload`` receipt ("applied" vs "restart pending");
- **documentation** — every row carries the doc line the example file and
  GDD §16 table describe it with.

The reload-semantics three-way disagreement (GDD §16 "hot-reloaded" vs the
example file's incomplete restart list vs module docstrings) ends here: a
key's ``reload`` attribute is the contract, and the reload transaction
reports against it instead of silently absorbing restart-bound edits.

Cross-field rules that no single key can express (weight sums, tier
ordering, backend-conditional requirements) live in :data:`CROSS_CHECKS`.

YAML 1.1 coercion is generic: for any key with ``choices``, a bare ``off``
(which YAML parses as boolean ``False``) is mapped back to the word when the
word is one of the legal values — no per-key special-casing.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "CROSS_CHECKS",
    "KEYS",
    "REQUIRED",
    "SECTIONS",
    "CrossCheck",
    "Key",
    "Reload",
    "Section",
    "key_by_path",
    "keys_in_section",
    "section_by_path",
]


class Reload(enum.Enum):
    """What it takes for a changed key to reach live behaviour.

    - ``HOT``     — consumers read ``holder.current`` at the point of use;
      the swap alone applies it.
    - ``SIGHUP``  — applied by an explicit step in the reload transaction
      (e.g. ``set_personality()``, ChatClient rebuild) — still no restart.
    - ``ENGINE``  — needs an engine rebuild (gazetteer / routing reload),
      triggered by the reload transaction.
    - ``RESTART`` — bound at process startup (models, sockets, database);
      a change is reported as "restart pending", never silently absorbed.
    """

    HOT = "hot"
    SIGHUP = "sighup"
    ENGINE = "engine"
    RESTART = "restart"


class _Required:
    """Sentinel: the key has no default and must be present."""

    def __repr__(self) -> str:  # pragma: no cover — repr cosmetics
        return "REQUIRED"


REQUIRED: Final = _Required()


@dataclass(frozen=True, slots=True)
class Key:
    """One tunable: its location, shape, constraints, and reload class.

    ``type`` is one of ``int | float | str | bool | int_list | str_list |
    opt_str`` (``opt_str``: ``null``/empty/missing all mean ``None``).
    ``choices`` constrains a ``str`` value (or every ``str_list`` member),
    case-insensitively, with generic YAML-1.1 boolean-to-word coercion.
    ``minimum``/``maximum`` are inclusive bounds; ``exclusive_minimum``
    turns ``minimum`` into a strict bound (``> 0`` style).
    ``coerce`` runs on the raw YAML value before any validation.
    """

    path: str
    type: str
    reload: Reload
    doc: str
    default: Any = REQUIRED
    choices: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: bool = False
    coerce: Callable[[Any], Any] | None = None

    @property
    def name(self) -> str:
        return self.path.rsplit(".", 1)[-1]

    @property
    def section(self) -> str:
        return self.path.rsplit(".", 1)[0]


@dataclass(frozen=True, slots=True)
class Section:
    """A mapping node in the YAML tree. ``optional`` sections may be absent
    or empty — every key under them must then carry a default."""

    path: str
    doc: str
    optional: bool = False

    @property
    def name(self) -> str:
        return self.path.rsplit(".", 1)[-1]

    @property
    def parent(self) -> str:
        return self.path.rsplit(".", 1)[0] if "." in self.path else ""


@dataclass(frozen=True, slots=True)
class CrossCheck:
    """A constraint spanning multiple keys. ``fn`` receives the loaded
    ``AuraConfig`` and returns an error message (already in the
    ``section.key: problem — Fix: action`` contract) or ``None``."""

    name: str
    doc: str
    fn: Callable[[Any], str | None]


# ── sections ─────────────────────────────────────────────────────────────────

SECTIONS: Final[tuple[Section, ...]] = (
    Section("discord", "Guild, channels, role gates, mention policy."),
    Section("discord.channels", "Where cards and health reports post."),
    Section(
        "discord.roles",
        "OPTIONAL role-id gates; absent/empty section = gates off (0).",
        optional=True,
    ),
    Section("wake", "openWakeWord model and trigger thresholds."),
    Section("capture", "Utterance capture windows and VAD mode."),
    Section(
        "dialog",
        "OPTIONAL voice dialog engine timing/budgets (GDD §5.4); "
        "defaults are the tuned live values.",
        optional=True,
    ),
    Section("stt", "Speech-to-text backend and relay gates."),
    Section("matching", "Phonetic system-name matcher weights (constraint 7)."),
    Section("matching.tiers", "Confidence tiers — GDD §8.3."),
    Section("matching.priors", "Context reweighting — GDD §8.4."),
    Section("incidents", "Dedupe / staleness / cancel windows."),
    Section("discipline", "Mention cooldowns and the flood breaker."),
    Section("discipline.circuit_breaker", "Corp-wide mention flood control."),
    Section("tts", "Piper synthesis and spoken-line personality."),
    Section(
        "chat",
        'OPTIONAL "command override" assistant (GDD §6.6); absent = off.',
        optional=True,
    ),
    Section("gazetteer", "Active system-set scoping — GDD §8.1."),
    Section(
        "routing",
        "OPTIONAL routing.yaml location; absent = sibling of cortana.yaml.",
        optional=True,
    ),
    Section("ipc", "The Brain⇄Ears unix socket (GDD §15)."),
    Section("health", "Self-report cadence and degradation alarms."),
    Section("database", "SQLite location."),
)


# ── keys ─────────────────────────────────────────────────────────────────────

_SEVERITIES: Final = ("high", "medium", "none")

KEYS: Final[tuple[Key, ...]] = (
    # discord
    Key(
        "discord.token_file",
        "str",
        Reload.RESTART,
        "Dev-fallback token path (0600). Production reads "
        "$CREDENTIALS_DIRECTORY/token via systemd LoadCredential= "
        "(constraint 12) — the token is never in YAML.",
    ),
    Key(
        "discord.guild_id",
        "int",
        Reload.RESTART,
        "The corp's guild snowflake.",
        minimum=0,
    ),
    Key(
        "discord.channels.intel_alerts",
        "int",
        Reload.HOT,
        "Channel for incidents that mention a role (GDD §11.2).",
        minimum=0,
    ),
    Key(
        "discord.channels.intel_live",
        "int",
        Reload.HOT,
        "Channel for every incident, no mentions — the firehose.",
        minimum=0,
    ),
    Key(
        "discord.channels.health",
        "int",
        Reload.HOT,
        "Channel for self-reports and degradation alerts.",
        minimum=0,
    ),
    Key(
        "discord.roles.pilot",
        "int",
        Reload.HOT,
        "Only members with this role may trigger mentions. 0 = gate off.",
        default=0,
        minimum=0,
    ),
    Key(
        "discord.roles.fc",
        "int",
        Reload.HOT,
        "Only this role voice-triggers under fleetmode / uses admin "
        "commands without Manage Guild. 0 = gate off.",
        default=0,
        minimum=0,
    ),
    Key(
        "discord.watch_voice_channels",
        "int_list",
        Reload.HOT,
        "Voice channels CORTANA watches / auto-joins.",
    ),
    Key(
        "discord.auto_join",
        "bool",
        Reload.HOT,
        "Join when a pilot enters, leave when empty.",
        default=True,
    ),
    Key(
        "discord.mentions_enabled",
        "bool",
        Reload.HOT,
        "false = silent mode: post cards, ping nobody.",
        default=True,
    ),
    Key(
        "discord.here_on_severity",
        "str_list",
        Reload.HOT,
        "Threat colours that fire @here: high=RED, medium=ORANGE, none=YELLOW (never fires).",
        default=("high",),
        choices=_SEVERITIES,
    ),
    Key(
        "discord.join_announcement",
        "str",
        Reload.HOT,
        "§19 consent notice cadence on voice join.",
        default="daily",
        choices=("every", "daily", "off"),
    ),
    # wake
    Key(
        "wake.model",
        "str",
        Reload.SIGHUP,
        "Trained openWakeWord ONNX chain; per-user models are built from "
        "it at speaker onset and cached for the process lifetime.",
    ),
    Key(
        "wake.threshold",
        "float",
        Reload.HOT,
        "Wake score needed to open a capture window.",
        minimum=0.0,
        maximum=1.0,
    ),
    Key(
        "wake.refractory_ms",
        "int",
        Reload.HOT,
        "Per-user dead time after a wake hit.",
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "wake.ack",
        "str",
        Reload.HOT,
        "Wake acknowledgement: spoken, tone, or silent.",
        default="beep",
        choices=("voice", "beep", "none"),
    ),
    Key(
        "wake.vad_threshold",
        "float",
        Reload.SIGHUP,
        "OPT-IN Silero VAD gate inside openWakeWord (0.0 = off). Applied at "
        "model build; the pool rebuilds per-user models live on reload.",
        default=0.0,
        minimum=0.0,
        maximum=1.0,
    ),
    # capture
    Key(
        "capture.preroll_ms",
        "int",
        Reload.HOT,
        "Ring-buffer audio prepended to each capture. Must fit inside the "
        "fixed 1500 ms privacy ring (cross-checked).",
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "capture.endpoint_silence_ms",
        "int",
        Reload.HOT,
        "Trailing silence that ends an utterance (wall-clock under DTX).",
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "capture.max_utterance_ms",
        "int",
        Reload.HOT,
        "Hard cap on a single capture window.",
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "capture.vad_aggressiveness",
        "int",
        Reload.RESTART,
        "webrtcvad mode 0 (permissive) – 3 (aggressive); the VadGate is built once at startup.",
        minimum=0,
        maximum=3,
    ),
    # stt
    Key(
        "dialog.window_ms",
        "int",
        Reload.HOT,
        "Wall-clock lifetime of a wake-free window (say-again retry, "
        "code-colour opener, bare command override). DTX-proof: the dialog "
        "wheel expires it in real time, frames or no frames.",
        default=4000,
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "dialog.ack_grace_ms",
        "int",
        Reload.HOT,
        "Endpoint grace after a capture opens or a prompt is spoken — cue "
        "playback plus pilot reaction time.",
        default=2000,
        minimum=0,
    ),
    Key(
        "dialog.endpoint_gap_floor_ms",
        "int",
        Reload.HOT,
        "Floor under capture.endpoint_silence_ms for the wall-clock "
        "endpoint: DTX drops packets between words; a too-eager gap clips "
        "pilots mid-sentence.",
        default=700,
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "dialog.max_retries",
        "int",
        Reload.HOT,
        "Wake-free windows per dialog TOTAL (subdialog openers and "
        "say-again retries share the budget). Only a fresh wake refills it; "
        "exhaustion ends audibly with standing-down.",
        default=2,
        minimum=0,
    ),
    Key(
        "stt.backend",
        "str",
        Reload.RESTART,
        "Which Transcriber engine to build at startup.",
        choices=("faster-whisper", "whisper-cpp"),
    ),
    Key("stt.model", "str", Reload.RESTART, "Whisper model size or path."),
    Key("stt.compute_type", "str", Reload.RESTART, "CTranslate2 quantization."),
    Key(
        "stt.cpu_threads",
        "int",
        Reload.RESTART,
        "Inference threads (the droplet has 2 dedicated vCPUs).",
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "stt.bias_with_gazetteer",
        "bool",
        Reload.RESTART,
        "Pass system names as the Whisper initial_prompt.",
        default=True,
    ),
    Key(
        "stt.whisper_cpp_url",
        "str",
        Reload.RESTART,
        "whisper.cpp server endpoint; required (non-empty) only when "
        "stt.backend is whisper-cpp (cross-checked).",
        default="http://127.0.0.1:8080/inference",
    ),
    Key(
        "stt.watchdog_s",
        "float",
        Reload.RESTART,
        'GDD §20 "STT worker hang" watchdog deadline. The whisper-cpp HTTP '
        "timeout is derived slightly below it so the socket gives up before "
        "the watchdog abandons the worker.",
        default=15.0,
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "stt.relay_min_logprob",
        "float",
        Reload.HOT,
        "Freeform relays below this Whisper confidence are dropped with "
        '"Say again" (GDD §8.6); recognised commands are never gated.',
        default=-0.9,
    ),
    Key(
        "stt.relay_mode",
        "str",
        Reload.HOT,
        "What unmatched speech may become a relay card (GDD §8.6).",
        default="framed",
        choices=("framed", "open", "off"),
    ),
    # matching
    Key(
        "matching.phonetic_weight",
        "float",
        Reload.HOT,
        "Weight of metaphone similarity (constraint 7). Must sum to 1.0 "
        "with text_weight (cross-checked).",
        minimum=0.0,
        maximum=1.0,
    ),
    Key(
        "matching.text_weight",
        "float",
        Reload.HOT,
        "Weight of raw-text Levenshtein similarity.",
        minimum=0.0,
        maximum=1.0,
    ),
    Key(
        "matching.tiers.high_min",
        "float",
        Reload.HOT,
        "top1 >= this (and margin) → post immediately.",
        minimum=0.0,
        maximum=1.0,
    ),
    Key(
        "matching.tiers.high_margin",
        "float",
        Reload.HOT,
        "top1 - top2 must also clear this for HIGH tier.",
        minimum=0.0,
        maximum=1.0,
    ),
    Key(
        "matching.tiers.medium_min",
        "float",
        Reload.HOT,
        "top1 >= this → post flagged uncertain, with buttons. Must be <= high_min (cross-checked).",
        minimum=0.0,
        maximum=1.0,
    ),
    Key(
        "matching.priors.recency_weight",
        "float",
        Reload.HOT,
        "Boost for systems with recent incidents.",
        minimum=0.0,
    ),
    Key(
        "matching.priors.recency_window_min",
        "int",
        Reload.HOT,
        "How recent counts as recent.",
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "matching.priors.proximity_weight",
        "float",
        Reload.HOT,
        "Boost for systems near an active incident.",
        minimum=0.0,
    ),
    Key(
        "matching.priors.proximity_max_jumps",
        "int",
        Reload.HOT,
        "Beyond this many jumps, no proximity boost.",
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "matching.priors.reporter_history_weight",
        "float",
        Reload.HOT,
        "Boost for systems this pilot reports from often.",
        minimum=0.0,
    ),
    Key(
        "matching.priors.home_weight",
        "float",
        Reload.HOT,
        "Standing boost for home and adjacent systems.",
        minimum=0.0,
    ),
    # incidents
    Key(
        "incidents.dedupe_window_s",
        "int",
        Reload.HOT,
        "Same system + type within this window → fold (GDD §9.2).",
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "incidents.stale_after_min",
        "int",
        Reload.HOT,
        "No updates for this long → auto-STALE, silently.",
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "incidents.cancel_window_s",
        "int",
        Reload.HOT,
        '"hey cortana, cancel" kills the user\'s last incident inside this.',
        minimum=0,
        exclusive_minimum=True,
    ),
    # discipline
    Key(
        "discipline.user_cooldown_s",
        "int",
        Reload.HOT,
        "Min seconds between mentions from the same pilot.",
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "discipline.circuit_breaker.max_mentions",
        "int",
        Reload.HOT,
        "More than this many mentions in window_min → flood control.",
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "discipline.circuit_breaker.window_min",
        "int",
        Reload.HOT,
        "The flood-control sliding window, in minutes.",
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "discipline.personal_pings_max",
        "int",
        Reload.HOT,
        "Max personal /pingme subscriptions per pilot (GDD §10.3).",
        default=10,
        minimum=0,
        exclusive_minimum=True,
    ),
    # tts
    Key(
        "tts.enabled",
        "bool",
        Reload.HOT,
        "Spoken back-channel on/off.",
        default=True,
    ),
    Key(
        "tts.voice",
        "str",
        Reload.HOT,
        "Piper voice model; the sample rate is re-read on config swap.",
    ),
    Key(
        "tts.binary",
        "str",
        Reload.HOT,
        "Piper invoked as a subprocess per synthesis (GDD §12).",
    ),
    Key(
        "tts.max_utterance_s",
        "float",
        Reload.HOT,
        "Hard cap; longer text goes to the channel instead.",
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "tts.effect",
        "str",
        Reload.HOT,
        'Post-synthesis effect: chorus+reverb "ship AI" sheen or none.',
        default="none",
        choices=("none", "holographic"),
    ),
    Key(
        "tts.personality",
        "str",
        Reload.SIGHUP,
        "Spoken-line flavour; applied by set_personality() in the reload transaction.",
        default="standard",
        choices=("standard", "cortana", "bratty"),
    ),
    # chat (optional section)
    Key(
        "chat.enabled",
        "bool",
        Reload.SIGHUP,
        'Pilots can say "command override, <question>" (/ask twin). Costs real money per question.',
        default=False,
    ),
    Key(
        "chat.model",
        "str",
        Reload.HOT,
        "Claude model for override replies.",
        default="claude-haiku-4-5",
    ),
    Key(
        "chat.api_key_file",
        "str",
        Reload.SIGHUP,
        "Dev fallback ONLY (0600); production reads "
        "$CREDENTIALS_DIRECTORY/anthropic via LoadCredential= "
        "(constraint 12). The client is rebuilt when the on-disk key "
        "changes.",
        default="/etc/cortana/anthropic",
    ),
    Key(
        "chat.max_tokens",
        "int",
        Reload.HOT,
        "Hard cap per answer.",
        default=300,
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "chat.user_cooldown_s",
        "int",
        Reload.HOT,
        "Per-pilot throttle — the cost control.",
        default=10,
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "chat.timeout_s",
        "float",
        Reload.HOT,
        "Wall-clock cap per answer incl. web search.",
        default=25.0,
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "chat.web_search",
        "bool",
        Reload.HOT,
        "Allow one live web search per question.",
        default=True,
    ),
    Key(
        "chat.answer_channel",
        "int",
        Reload.HOT,
        "Channel for answers too long to speak. 0 = intel_live.",
        default=0,
        minimum=0,
    ),
    # gazetteer
    Key(
        "gazetteer.file",
        "str",
        Reload.ENGINE,
        "Scope rules file (regions/within_jumps_of/include_all, GDD §8.1).",
    ),
    Key(
        "gazetteer.home_system",
        "opt_str",
        Reload.ENGINE,
        "Anchor for the home-bias prior (§8.4). null/empty = no home "
        "system → prior off (nomadic corps, GDD §8.1).",
        default=None,
    ),
    Key(
        "gazetteer.include_all",
        "bool",
        Reload.ENGINE,
        "Nomadic override, mirrors gazetteer.yaml include_all — either "
        "being true activates the entire seeded map.",
        default=False,
    ),
    # routing (optional section)
    Key(
        "routing.file",
        "str",
        Reload.ENGINE,
        "routing.yaml location. Empty (the default) = routing.yaml in the "
        "same directory as cortana.yaml.",
        default="",
    ),
    # ipc
    Key(
        "ipc.socket",
        "str",
        Reload.RESTART,
        "Brain binds; Ears connects (GDD §15). Bound once at startup.",
    ),
    # health
    Key(
        "health.report_interval_min",
        "int",
        Reload.HOT,
        "Cadence of #bot-health self-reports.",
        minimum=0,
        exclusive_minimum=True,
    ),
    Key(
        "health.voice_silence_alarm_s",
        "int",
        Reload.HOT,
        "No VoiceTick this long with >= 2 humans present → degraded.",
        minimum=0,
        exclusive_minimum=True,
    ),
    # database
    Key(
        "database.path",
        "str",
        Reload.RESTART,
        "SQLite (WAL) location; opened once at startup.",
    ),
)


# ── cross-field checks ───────────────────────────────────────────────────────


def _check_weight_sum(cfg: Any) -> str | None:
    total = cfg.matching.phonetic_weight + cfg.matching.text_weight
    if abs(total - 1.0) >= 1e-6:
        return (
            "matching.phonetic_weight: phonetic_weight + text_weight must sum "
            f"to 1.0, got {total:g} — Fix: adjust the two weights so they sum "
            "to 1.0 (they rescale every match score against the fixed tiers)"
        )
    return None


def _check_tier_order(cfg: Any) -> str | None:
    tiers = cfg.matching.tiers
    if tiers.medium_min > tiers.high_min:
        return (
            "matching.tiers.medium_min: must be <= matching.tiers.high_min "
            f"({tiers.medium_min:g} > {tiers.high_min:g}) — Fix: lower "
            "medium_min or raise high_min"
        )
    return None


def _check_preroll_fits_ring(cfg: Any) -> str | None:
    # Lazy import: cortana.audio.capture imports cortana.config, so the
    # constant cannot be imported at module load. Environments without the
    # audio stack (webrtcvad) skip the check rather than fail config load.
    try:
        from cortana.audio.capture import RING_MS
    except ImportError:  # pragma: no cover — audio stack absent
        return None
    if cfg.capture.preroll_ms > RING_MS:
        return (
            f"capture.preroll_ms: exceeds the fixed {RING_MS} ms privacy "
            f"ring, got {cfg.capture.preroll_ms} — Fix: set preroll_ms <= "
            f"{RING_MS} (the ring is the constraint-5 guarantee and cannot "
            "grow from config)"
        )
    return None


def _check_whisper_cpp_url(cfg: Any) -> str | None:
    if cfg.stt.backend == "whisper-cpp" and not cfg.stt.whisper_cpp_url.strip():
        return (
            "stt.whisper_cpp_url: required when stt.backend is whisper-cpp — "
            "Fix: set the whisper.cpp server URL "
            "(e.g. http://127.0.0.1:8080/inference)"
        )
    return None


CROSS_CHECKS: Final[tuple[CrossCheck, ...]] = (
    CrossCheck(
        "matching_weights_sum",
        "phonetic_weight + text_weight must sum to 1.0.",
        _check_weight_sum,
    ),
    CrossCheck(
        "matching_tier_order",
        "tiers.medium_min must not exceed tiers.high_min.",
        _check_tier_order,
    ),
    CrossCheck(
        "capture_preroll_fits_ring",
        "preroll_ms must fit the fixed 1500 ms privacy ring.",
        _check_preroll_fits_ring,
    ),
    CrossCheck(
        "stt_whisper_cpp_url_required",
        "whisper_cpp_url must be non-empty when backend is whisper-cpp.",
        _check_whisper_cpp_url,
    ),
)


# ── lookup helpers ───────────────────────────────────────────────────────────


def key_by_path(path: str) -> Key:
    """Return the :class:`Key` for a dotted path. Raises ``KeyError``."""
    return _KEY_INDEX[path]


def section_by_path(path: str) -> Section:
    """Return the :class:`Section` for a dotted path. Raises ``KeyError``."""
    return _SECTION_INDEX[path]


def keys_in_section(section_path: str) -> tuple[Key, ...]:
    """The keys whose immediate parent is ``section_path``."""
    return tuple(k for k in KEYS if k.section == section_path)


def child_sections(section_path: str) -> tuple[Section, ...]:
    """The sections whose immediate parent is ``section_path`` ("" = top)."""
    return tuple(s for s in SECTIONS if s.parent == section_path)


_KEY_INDEX: Final[dict[str, Key]] = {k.path: k for k in KEYS}
_SECTION_INDEX: Final[dict[str, Section]] = {s.path: s for s in SECTIONS}
