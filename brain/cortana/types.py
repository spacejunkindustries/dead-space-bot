"""Shared enums and dataclasses used across CORTANA Brain modules.

This module is the vocabulary of the system: every cross-module boundary in
docs/INTERFACES.md speaks in these types. It has no dependencies beyond the
standard library, so any module may import it without cycles.

Timestamps are ISO-8601 UTC strings (``datetime.now(timezone.utc).isoformat()``)
matching the TEXT columns in the SQLite schema (GDD §14).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

__all__ = [
    "AlertChannel",
    "ButtonSpec",
    "CardRender",
    "INTENT_SEVERITY",
    "MENTION_INTENTS",
    "Incident",
    "IncidentOutcome",
    "IncidentStatus",
    "IncidentUpdate",
    "Intent",
    "MatchCandidate",
    "Outcome",
    "ParsedCommand",
    "PostError",
    "PriorContext",
    "Resolution",
    "ResponderState",
    "RoutingDecision",
    "Severity",
    "SystemEntry",
    "Tier",
    "TranscriptResult",
]


class PostError(RuntimeError):
    """A Discord card post failed (permissions, deleted channel, REST error).

    Raised by the Poster so the engine can roll back the incident row instead
    of leaving an invisible ACTIVE incident that folds away later reports."""


class Intent(StrEnum):
    """Command intents — GDD §6.1. Values match the ``incidents.type`` column."""

    HOSTILE_SPOTTED = "HOSTILE_SPOTTED"
    UNDER_ATTACK = "UNDER_ATTACK"
    ASSIST_REQUEST = "ASSIST_REQUEST"
    GATE_CAMP = "GATE_CAMP"
    RESOLVE = "RESOLVE"
    CHASE_UPDATE = "CHASE_UPDATE"
    TIMER = "TIMER"
    FORMUP = "FORMUP"
    QUERY = "QUERY"
    HELP = "HELP"
    CANCEL = "CANCEL"
    REGISTER = "REGISTER"
    UNREGISTER = "UNREGISTER"
    WHOAMI = "WHOAMI"
    PING_ME = "PING_ME"
    PING_ME_CLEAR = "PING_ME_CLEAR"


class Severity(StrEnum):
    """Incident severity — values match the ``incidents.severity`` column."""

    NONE = "none"
    MEDIUM = "medium"
    HIGH = "high"


#: Default severity per intent (GDD §6.1). Group alias "all hands" and routing
#: rules may change *notification* behaviour, never the stored severity.
INTENT_SEVERITY: Mapping[Intent, Severity] = MappingProxyType(
    {
        Intent.HOSTILE_SPOTTED: Severity.MEDIUM,
        Intent.UNDER_ATTACK: Severity.HIGH,
        Intent.ASSIST_REQUEST: Severity.HIGH,
        Intent.GATE_CAMP: Severity.MEDIUM,
        Intent.RESOLVE: Severity.NONE,
        Intent.CHASE_UPDATE: Severity.NONE,
        Intent.TIMER: Severity.NONE,
        Intent.FORMUP: Severity.NONE,
        Intent.QUERY: Severity.NONE,
        Intent.HELP: Severity.NONE,
        Intent.CANCEL: Severity.NONE,
        Intent.REGISTER: Severity.NONE,
        Intent.UNREGISTER: Severity.NONE,
        Intent.WHOAMI: Severity.NONE,
        Intent.PING_ME: Severity.NONE,
        Intent.PING_ME_CLEAR: Severity.NONE,
    }
)


#: Intents whose reports trigger role mentions/@here and therefore require the
#: @Pilot role (GDD §11.1 layer 4). One shared set so the slash cog's gate and
#: the voice pipeline's gate can never silently diverge (constraint 10).
MENTION_INTENTS: frozenset[Intent] = frozenset(
    {Intent.HOSTILE_SPOTTED, Intent.UNDER_ATTACK, Intent.ASSIST_REQUEST, Intent.GATE_CAMP}
)


class Tier(StrEnum):
    """System-resolution confidence tier — GDD §8.3. Matches ``command_log.tier``."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class IncidentStatus(StrEnum):
    """Lifecycle state — matches the ``incidents.status`` column."""

    ACTIVE = "ACTIVE"
    STALE = "STALE"
    RESOLVED = "RESOLVED"


class ResponderState(StrEnum):
    """Response-button state — matches the ``responders.state`` column."""

    OTW = "OTW"
    WATCHING = "WATCHING"
    NO = "NO"


class Outcome(StrEnum):
    """What the incident engine did with a report — matches ``command_log.outcome``."""

    POSTED = "POSTED"
    FOLDED = "FOLDED"
    ASKED = "ASKED"
    REJECTED = "REJECTED"


class AlertChannel(StrEnum):
    """Which intel channel a card is posted to — GDD §11.2."""

    ALERTS = "alerts"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """Output of ``aura.nlu.grammar.parse`` — GDD §6.

    ``system_text`` is the raw token window believed to name a system; it has
    NOT been resolved against the gazetteer yet. ``detail`` is captured
    verbatim and never parsed (GDD §6.3).
    """

    intent: Intent
    system_text: str | None
    group_alias: str | None
    detail: str | None
    raw: str
    #: Spoken threat-colour override (GDD §6.4): "code red" → HIGH,
    #: "code orange" → MEDIUM, "code yellow" → NONE. ``None`` = not spoken;
    #: the intent's default severity applies.
    severity: Severity | None = None


@dataclass(frozen=True, slots=True)
class SystemEntry:
    """One gazetteer system, as loaded from the ``systems`` table."""

    id: int
    name: str
    region: str
    constellation: str | None
    metaphone: str


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    """One scored gazetteer candidate. ``score`` is the post-prior final score."""

    system_id: int
    name: str
    score: float


@dataclass(frozen=True, slots=True)
class Resolution:
    """Output of ``aura.nlu.phonetics.resolve`` — GDD §8.2/§8.3.

    ``candidates`` holds at most the top 3, best first. Empty only when the
    gazetteer produced no scorable candidates at all (always LOW tier then).
    """

    tier: Tier
    candidates: tuple[MatchCandidate, ...]

    @property
    def best(self) -> MatchCandidate | None:
        return self.candidates[0] if self.candidates else None


@dataclass(frozen=True, slots=True)
class PriorContext:
    """Inputs to the context priors — GDD §8.4. Built by the incident engine.

    ``recency_min`` maps system_id → minutes since that system's last incident
    (only systems inside the recency window appear). ``reporter_counts`` maps
    system_id → this reporter's recent report count. ``active_systems`` are
    systems with currently ACTIVE incidents (for the proximity prior, which
    needs ``Gazetteer.jumps``). ``home_system_id`` anchors the home bias.
    """

    recency_min: Mapping[int, float] = field(default_factory=dict)
    reporter_counts: Mapping[int, int] = field(default_factory=dict)
    active_systems: tuple[int, ...] = ()
    home_system_id: int | None = None


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    """Output of a ``Transcriber`` backend — text plus mean decoder log-probability."""

    text: str
    avg_logprob: float


@dataclass(frozen=True, slots=True)
class ButtonSpec:
    """One button in a card's view. ``style`` is a discord.py ButtonStyle name

    (``"primary" | "secondary" | "success" | "danger"``); ``custom_id`` follows
    the persistent-view scheme ``aura:inc:{incident_id}:{action}``.
    """

    custom_id: str
    label: str
    style: str = "secondary"
    emoji: str | None = None
    disabled: bool = False


@dataclass(frozen=True, slots=True)
class CardRender:
    """Discord-agnostic render of an incident card: an embed payload dict

    (the ``discord.Embed.from_dict`` shape) plus an ordered button row spec.
    Produced by the incident engine, consumed by a ``Poster`` implementation.
    """

    embed: dict[str, Any]
    buttons: tuple[ButtonSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class IncidentUpdate:
    """A folded-in subsequent report — mirrors the ``incident_updates`` table."""

    user_id: int
    text: str | None
    at: str


@dataclass(slots=True)
class Incident:
    """In-memory mirror of one ``incidents`` row plus its child rows — GDD §9."""

    id: int
    guild_id: int
    system_id: int | None
    system_confidence: float | None
    type: Intent
    severity: Severity
    reporter_id: int
    detail: str | None
    opened_at: str
    updated_at: str
    status: IncidentStatus
    message_id: int | None
    channel_id: int | None
    updates: list[IncidentUpdate] = field(default_factory=list)
    responders: dict[int, ResponderState] = field(default_factory=dict)

    @property
    def reporter_count(self) -> int:
        """Distinct reporters: the opener plus everyone folded in (GDD §9.1)."""
        return len({self.reporter_id, *(u.user_id for u in self.updates)})


@dataclass(frozen=True, slots=True)
class IncidentOutcome:
    """Result of an incident-engine call: what happened, what CORTANA should say

    into voice (None = say nothing), and the rendered card (None when nothing
    was posted or edited). ``incident_id`` is None for REJECTED outcomes.
    """

    outcome: Outcome
    utterance: str | None
    card: CardRender | None
    incident_id: int | None


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """Output of ``aura.core.routing.evaluate`` — GDD §10/§11.

    ``role_ids`` is the deduplicated union of matching subscription roles,
    mentioned once. ``here`` is True only for escalating types (constraint 11).
    ``channel`` picks ``#intel-alerts`` (any mention) vs ``#intel-live``.
    ``user_ids`` are matching personal ping subscribers (GDD §10.3) — user
    mentions appended to the mention line; they never influence ``here``.
    """

    role_ids: tuple[int, ...]
    here: bool
    channel: AlertChannel
    user_ids: tuple[int, ...] = ()
