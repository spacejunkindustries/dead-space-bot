"""Pure parsers: raw Albion gameinfo JSON → typed rows (killboard GDD §6, §11).

No I/O, no sqlite, no network — every function here is a pure transformation of
a plain ``dict`` (a decoded gameinfo event) into typed values, so the whole
module is trivially unit-testable with hand-written fixtures.

The gameinfo API is undocumented and best-effort (GDD §2.4): responses come back
with missing, null, or partially-populated fields, and casing is not guaranteed.
Every accessor here is defensive — ``.get`` with a default, tolerant numeric
coercion, and a couple of key aliases — so a malformed event yields a row with
nulls rather than a crash. An event is only ever *dropped* (``None``) for two
reasons: it has no ``EventId`` (there is nothing to key on), or the tracked
guild is not involved at all.

Gameinfo keys are PascalCase (``EventId``, ``Killer``/``Victim`` sub-objects with
``Id``/``Name``/``GuildId``/``AverageItemPower``, ``TotalVictimKillFame``,
``Participants``, ``numberOfParticipants``, ``BattleId``, ``Location``,
``TimeStamp``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# ── guild-relation constants (GDD §6) ────────────────────────────────────────
#: The tracked guild landed the killing blow.
KILL = "KILL"
#: A tracked-guild member was the victim.
DEATH = "DEATH"
#: A tracked-guild member dealt damage but was neither killer nor victim.
ASSIST = "ASSIST"


@dataclass(frozen=True, slots=True)
class EventRow:
    """One ingested event, flattened to the ``events`` table columns (GDD §11).

    Column names mirror the schema exactly. Optional fields are ``None`` when the
    source event omitted them; ``total_fame`` and ``num_participants`` fall back
    to ``0`` because the counters/rankings sum over them and a null would poison
    the arithmetic. ``raw_json`` (the retained full event) is added by the store
    layer, not here — this dataclass is the *parsed* projection only.
    """

    event_id: int
    timestamp: str
    killer_id: str | None
    killer_name: str | None
    killer_guild_id: str | None
    killer_ip: float | None
    victim_id: str | None
    victim_name: str | None
    victim_guild_id: str | None
    victim_ip: float | None
    total_fame: int
    relation: str
    num_participants: int
    battle_id: int | None
    location: str | None


@dataclass(frozen=True, slots=True)
class Participant:
    """One row of the ``participants`` table (GDD §11): a player's damage/heal
    share in an event, used for cards, assists, and damage rankings."""

    player_id: str | None
    player_name: str | None
    guild_id: str | None
    damage_done: float
    healing_done: float


# ── tolerant coercion helpers ────────────────────────────────────────────────


def _as_str(value: Any) -> str | None:
    """A non-empty string, or ``None``. Empty strings collapse to ``None`` so a
    blank ``GuildId`` never masquerades as a real guild in classification."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any, default: int | None = None) -> int | None:
    """Best-effort int. Tolerates ints, floats, and numeric strings; anything
    else (None, junk) returns ``default``."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (ValueError, TypeError):
            return default
    return default


def _as_float(value: Any, default: float | None = None) -> float | None:
    """Best-effort float. Tolerates numbers and numeric strings; else ``default``."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (ValueError, TypeError):
            return default
    return default


def _obj(value: Any) -> dict[str, Any]:
    """A dict view of a sub-object that might be missing or null."""
    return value if isinstance(value, dict) else {}


def _participant_list(event: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``Participants`` array as a list of dicts, tolerating null/missing."""
    raw = event.get("Participants")
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, dict)]


def _num_participants(event: dict[str, Any]) -> int:
    """Solo-vs-group count (GDD §6). Prefers ``numberOfParticipants``; falls back
    to the length of the ``Participants`` array when the field is absent."""
    n = _as_int(event.get("numberOfParticipants"))
    if n is not None:
        return n
    return len(_participant_list(event))


def _location(event: dict[str, Any]) -> str | None:
    """The kill location, under either the ``Location`` or ``KillLocation`` key."""
    return _as_str(event.get("Location")) or _as_str(event.get("KillLocation"))


# ── classification (GDD §6) ──────────────────────────────────────────────────


def classify(event: dict[str, Any], guild_id: str) -> str | None:
    """Label an event from the tracked guild's point of view.

    Returns :data:`KILL` when the final-blow killer's ``GuildId`` is the tracked
    guild, :data:`DEATH` when the victim's is, otherwise :data:`ASSIST` when any
    participant belongs to the guild (a group kill where the guild dealt damage
    but did not land the blow), or ``None`` when the guild is not involved at all.
    """
    target = _as_str(guild_id)
    if target is None:
        return None

    if _as_str(_obj(event.get("Killer")).get("GuildId")) == target:
        return KILL
    if _as_str(_obj(event.get("Victim")).get("GuildId")) == target:
        return DEATH
    for p in _participant_list(event):
        if _as_str(p.get("GuildId")) == target:
            return ASSIST
    return None


# ── event / participant parsing ──────────────────────────────────────────────


def parse_event(event: dict[str, Any], guild_id: str) -> EventRow | None:
    """Parse a raw gameinfo event into an :class:`EventRow`.

    Tolerant of missing/partial fields (GDD §2.4): only two conditions drop an
    event — a missing/unparseable ``EventId`` (nothing to key the row on) or the
    tracked guild not being involved (``classify`` → ``None``). Everything else
    degrades to ``None``/``0`` rather than raising.
    """
    if not isinstance(event, dict):
        return None

    event_id = _as_int(event.get("EventId"))
    if event_id is None:
        log.debug("event_missing_id", keys=sorted(event.keys()))
        return None

    relation = classify(event, guild_id)
    if relation is None:
        return None

    killer = _obj(event.get("Killer"))
    victim = _obj(event.get("Victim"))

    return EventRow(
        event_id=event_id,
        timestamp=_as_str(event.get("TimeStamp")) or "",
        killer_id=_as_str(killer.get("Id")),
        killer_name=_as_str(killer.get("Name")),
        killer_guild_id=_as_str(killer.get("GuildId")),
        killer_ip=_as_float(killer.get("AverageItemPower")),
        victim_id=_as_str(victim.get("Id")),
        victim_name=_as_str(victim.get("Name")),
        victim_guild_id=_as_str(victim.get("GuildId")),
        victim_ip=_as_float(victim.get("AverageItemPower")),
        total_fame=_as_int(event.get("TotalVictimKillFame"), default=0) or 0,
        relation=relation,
        num_participants=_num_participants(event),
        battle_id=_as_int(event.get("BattleId")),
        location=_location(event),
    )


def participants_of(event: dict[str, Any]) -> list[Participant]:
    """Extract the per-participant damage/heal rows (GDD §11).

    Returns an empty list when ``Participants`` is missing or null. Each entry is
    coerced tolerantly; damage/heal default to ``0.0`` so rankings can sum them
    without null checks.
    """
    out: list[Participant] = []
    for p in _participant_list(event):
        out.append(
            Participant(
                player_id=_as_str(p.get("Id")),
                player_name=_as_str(p.get("Name")),
                guild_id=_as_str(p.get("GuildId")),
                damage_done=_as_float(p.get("DamageDone"), default=0.0) or 0.0,
                healing_done=_as_float(p.get("SupportHealingDone"), default=0.0) or 0.0,
            )
        )
    return out


__all__ = [
    "ASSIST",
    "DEATH",
    "KILL",
    "EventRow",
    "Participant",
    "classify",
    "parse_event",
    "participants_of",
]
