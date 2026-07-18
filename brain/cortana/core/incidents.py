"""Incident engine — GDD §9: lifecycle, dedupe folding, cards, timers, form-ups.

This is THE single engine both the voice and slash paths hit (CLAUDE.md
constraint 10): ``report`` is the one entry point for every command, and every
report writes a ``command_log`` row. The engine is Discord-agnostic — it
renders a :class:`~aura.types.CardRender` (plain embed dict + button specs)
and hands it to an injected :class:`Poster`; no discord.py import here.

Core invariants (GDD §9.1/§9.2, constraints 9 and 11):

- **One incident == one message, edited in place, forever.** Folds, responder
  updates, resolution, staleness — all edits, never a second post.
- **Dedupe**: same system + same type + within ``incidents.dedupe_window_s``
  → fold into the existing incident, bump the reporter set, edit the card,
  and never re-mention.
- Mentions pass through routing (:mod:`aura.core.routing`) and discipline
  (:mod:`aura.core.discipline`); when discipline says no, the decision is
  suppressed — the card still posts, just without mentions.

All public methods are async; every SQLite touch goes through
``asyncio.to_thread`` and all writes are funnelled through one internal lock.
Time is read through ``self._clock`` (an attribute defaulting to UTC now) so
tests inject a deterministic clock without changing any signature.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import structlog

from cortana import tts
from cortana.config import ConfigHolder
from cortana.core import db
from cortana.core.callsigns import CallsignRegistry
from cortana.core.discipline import Discipline
from cortana.core.personal_pings import PersonalPingRegistry, types_from_detail
from cortana.core.routing import MentionDecision, RoutingRule, decide_mentions, load_group_aliases
from cortana.core.routing import load_rules as load_routing_rules_file
from cortana.types import (
    INTENT_SEVERITY,
    MENTION_INTENTS,
    AlertChannel,
    ButtonSpec,
    CardRender,
    Incident,
    IncidentOutcome,
    IncidentStatus,
    IncidentUpdate,
    Intent,
    MatchCandidate,
    Outcome,
    ParsedCommand,
    PostError,
    PriorContext,
    Resolution,
    ResponderState,
    Severity,
    Tier,
)

if TYPE_CHECKING:  # pragma: no cover — real class lands with aura.nlu.gazetteer
    from cortana.nlu.gazetteer import Gazetteer

__all__ = ["IncidentEngine", "Poster", "TimerPing", "render_card"]

log = structlog.get_logger(__name__)

# ── presentation tables (GDD §9.1 / §12.1) ───────────────────────────────────

_TYPE_LABELS: dict[Intent, str] = {
    Intent.HOSTILE_SPOTTED: "Hostiles",
    Intent.UNDER_ATTACK: "Under attack",
    Intent.ASSIST_REQUEST: "Assist request",
    Intent.GATE_CAMP: "Gate camp",
    Intent.FORMUP: "Form-up",
    Intent.TIMER: "Timer",
}

_SEVERITY_EMOJI: dict[Severity, str] = {
    Severity.HIGH: "🔴",
    Severity.MEDIUM: "🟠",
    Severity.NONE: "🟡",
}

#: Spoken/written threat level shown on the card — red/orange/yellow so the
#: severity reads at a glance (and can drive pings via discord.here_on_severity).
_SEVERITY_CODE: dict[Severity, str] = {
    Severity.HIGH: "CODE RED",
    Severity.MEDIUM: "CODE ORANGE",
    Severity.NONE: "CODE YELLOW",
}

#: Spoken colour words for the §8.3 readback ("…, code red, posted.").
_SEVERITY_SPOKEN_WORD: dict[Severity, str] = {
    Severity.HIGH: "red",
    Severity.MEDIUM: "orange",
    Severity.NONE: "yellow",
}

_COLOR_BY_SEVERITY: dict[Severity, int] = {
    Severity.HIGH: 0xED4245,  # red
    Severity.MEDIUM: 0xE67E22,  # orange
    Severity.NONE: 0xF1C40F,  # yellow
}
#: Severity ordering for the fold raises-never-lowers rule (GDD §9.2).
_SEVERITY_RANK: dict[Severity, int] = {Severity.NONE: 0, Severity.MEDIUM: 1, Severity.HIGH: 2}

_COLOR_RESOLVED = 0x95A5A6  # grey
_COLOR_STALE = 0x607D8B  # dim slate
_COLOR_BROADCAST = 0xF1C40F  # yellow — freeform intel relay is CODE YELLOW (GDD §8.6)

_GROUP_ALIAS_SPOKEN: dict[str, str] = {
    "miners": "miners",
    "defense": "home defense",
    "all_hands": "all hands",
}

_NUMBER_WORDS: dict[int, str] = {
    1: "One",
    2: "Two",
    3: "Three",
    4: "Four",
    5: "Five",
    6: "Six",
    7: "Seven",
    8: "Eight",
    9: "Nine",
    10: "Ten",
    11: "Eleven",
    12: "Twelve",
}

#: Intents that open (or fold into) an incident card — exactly the
#: mention-bearing set (aura.types.MENTION_INTENTS), shared so the report/gate
#: sets cannot silently diverge.
_REPORT_INTENTS = MENTION_INTENTS

#: Card label when a report gave no usable location at all — the alert still
#: posts (GDD §8.6 catch-all) so a corpmate can ask "where?".
_UNKNOWN_LOCATION = "location unclear"


class Poster(Protocol):
    """Injected Discord side — implemented by ``aura.dsc.bot`` (INTERFACES.md)."""

    async def post(
        self,
        guild_id: int,
        channel: AlertChannel,
        content: str,
        card: CardRender,
        *,
        mentions: MentionDecision | None = None,
    ) -> tuple[int, int]:
        """Post a card; returns ``(channel_id, message_id)``.

        ``mentions`` is the escalation authority's grant: the implementation
        builds its ``AllowedMentions`` allowlist from it verbatim (explicit
        user ids, explicit role ids, ``everyone`` only when ``here``).
        ``None`` means nothing in the content may ping.
        """
        ...

    async def edit(self, channel_id: int, message_id: int, content: str, card: CardRender) -> None:
        """Edit the card in place. ``content == ""`` means keep/clear mentions."""
        ...


@dataclass(frozen=True, slots=True)
class TimerPing:
    """One due timer, ready for the caller to announce (GDD §13)."""

    timer_id: int
    guild_id: int
    system_id: int | None
    system_name: str | None
    note: str | None
    fires_at: str
    created_by: int


# ── small pure helpers ───────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    """Canonical timestamp format: fixed-width so string comparison == time order."""
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def _number_word(n: int) -> str:
    return _NUMBER_WORDS.get(n, str(n))


_DURATION_UNITS: dict[str, float] = {
    "hour": 3600.0,
    "hours": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "h": 3600.0,
    "minute": 60.0,
    "minutes": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "m": 60.0,
}

_DURATION_WORDS: dict[str, float] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "ninety": 90,
    "half": 0.5,
}


def parse_duration(text: str) -> timedelta | None:
    """Parse a spoken/typed duration: "four hours", "15 minutes", "1h", …

    Returns None when no positive duration can be extracted — the caller asks
    the pilot to say it again rather than guessing (GDD §8.3 spirit).
    """
    total = 0.0
    num: float | None = None
    # Split digit runs from letter runs so "1h" / "30min" parse like "1 h".
    for tok in re.findall(r"\d+(?:\.\d+)?|[a-z]+", text.lower().replace("-", " ")):
        if tok in _DURATION_UNITS:
            total += (1.0 if num is None else num) * _DURATION_UNITS[tok]
            num = None
        elif tok in ("a", "an"):
            if num is None:
                num = 1.0
        elif tok in _DURATION_WORDS:
            num = (num or 0.0) + _DURATION_WORDS[tok]
        else:
            try:
                num = float(tok)
            except ValueError:
                continue  # filler word ("in", "about")
    if total <= 0:
        return None
    return timedelta(seconds=total)


_DURATION_ONES: tuple[str, ...] = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
    "twenty",
)

_DURATION_TENS: dict[int, str] = {20: "twenty", 30: "thirty", 40: "forty", 50: "fifty"}


def _duration_word(n: int) -> str:
    """Lowercase number words for spoken durations (§12.1): 0–20 plus tens
    compounds up to fifty nine — the vocabulary ``parse_duration`` accepts.
    Anything larger falls back to digits."""
    if 0 <= n <= 20:
        return _DURATION_ONES[n]
    if 20 < n < 60:
        tens, ones = divmod(n, 10)
        word = _DURATION_TENS[tens * 10]
        return f"{word} {_DURATION_ONES[ones]}" if ones else word
    return str(n)


def _format_duration(delta: timedelta) -> str:
    """Spoken duration, worded per the §12.1 catalogue: "four hours",
    "one hour thirty minutes", "fifteen minutes"."""
    minutes = int(delta.total_seconds() // 60)
    hours, mins = divmod(minutes, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{_duration_word(hours)} hour{'s' if hours != 1 else ''}")
    if mins or not parts:
        parts.append(f"{_duration_word(mins)} minute{'s' if mins != 1 else ''}")
    return " ".join(parts)


def _mention_content(decision: MentionDecision) -> str:
    parts: list[str] = []
    if decision.here:
        parts.append("@here")
    parts.extend(f"<@&{role_id}>" for role_id in decision.role_ids)
    # Personal ping subscribers (GDD §10.3): user mentions appended after the
    # roles — never @here, never a separate message.
    parts.extend(f"<@{user_id}>" for user_id in decision.user_ids)
    return " ".join(parts)


# ── card rendering (pure — GDD §9.1) ─────────────────────────────────────────


def render_card(
    incident: Incident,
    system_name: str,
    *,
    uncertain: bool = False,
    candidates: tuple[MatchCandidate, ...] = (),
    cancelled: bool = False,
    formup_at: str | None = None,
    reporter_callsign: str | None = None,
) -> CardRender:
    """Render an incident as a plain embed dict + button specs.

    The view layer (``aura.dsc``) turns this into a ``discord.Embed`` and a
    persistent ``View``; nothing Discord-specific leaks in here so the render
    is directly assertable in tests. ``reporter_callsign`` names a sole
    registered reporter on the card; folded multi-reporter cards keep the
    distinct count ("reported by 5", GDD §9.1).
    """
    status = incident.status
    if status is IncidentStatus.RESOLVED:
        emoji = "❌" if cancelled else "✅"
        color = _COLOR_RESOLVED
    elif status is IncidentStatus.STALE:
        emoji = "⏳"
        color = _COLOR_STALE
    else:
        emoji = _SEVERITY_EMOJI[incident.severity]
        color = _COLOR_BY_SEVERITY[incident.severity]

    label = _TYPE_LABELS.get(incident.type, str(incident.type))
    code = _SEVERITY_CODE.get(incident.severity) if status is IncidentStatus.ACTIVE else None
    prefix = f"{emoji} {code} · " if code else f"{emoji} "
    title = f"{prefix}{label} — {system_name}"

    desc_lines: list[str] = []
    if incident.detail:
        desc_lines.append(incident.detail)
    if formup_at is not None:
        desc_lines.append(f"Forms up at {formup_at}")
    if uncertain and status is IncidentStatus.ACTIVE:
        conf = incident.system_confidence
        marker = f" ({conf:.0%})" if conf is not None else ""
        desc_lines.append(f"❓ System unconfirmed{marker} — tap the correct system below.")

    otw = sum(1 for s in incident.responders.values() if s is ResponderState.OTW)
    watching = sum(1 for s in incident.responders.values() if s is ResponderState.WATCHING)
    declined = sum(1 for s in incident.responders.values() if s is ResponderState.NO)

    system_value = system_name
    if uncertain and incident.system_confidence is not None:
        system_value = f"{system_name} ❓ {incident.system_confidence:.0%}"

    if status is IncidentStatus.RESOLVED:
        footer = "❌ Cancelled by reporter" if cancelled else "✅ Resolved"
    elif status is IncidentStatus.STALE:
        footer = "⏳ Stale — no recent updates"
    else:
        footer = "Active"

    reporter_value = (
        reporter_callsign
        if reporter_callsign is not None and incident.reporter_count == 1
        else str(incident.reporter_count)
    )

    embed: dict[str, object] = {
        "title": title,
        "color": color,
        "timestamp": incident.opened_at,
        "fields": [
            {"name": "System", "value": system_value, "inline": True},
            {"name": "Reported by", "value": reporter_value, "inline": True},
            {
                "name": "Responders",
                "value": f"🚀 {otw} · 👀 {watching} · ❌ {declined}",
                "inline": True,
            },
        ],
        "footer": {"text": f"{footer} · CORTANA"},
    }
    if desc_lines:
        embed["description"] = "\n".join(desc_lines)

    buttons: tuple[ButtonSpec, ...] = ()
    if status is not IncidentStatus.RESOLVED:
        pick_row: list[ButtonSpec] = []
        if uncertain:
            pick_row = [
                ButtonSpec(
                    custom_id=f"aura:inc:{incident.id}:pick:{c.system_id}",
                    label=c.name,
                    style="secondary",
                )
                for c in candidates[:3]
            ]
            pick_row.append(
                ButtonSpec(
                    custom_id=f"aura:inc:{incident.id}:fix",
                    label="Wrong — fix",
                    style="danger",
                )
            )
        respond_row = [
            ButtonSpec(
                custom_id=f"aura:inc:{incident.id}:otw",
                label="On my way",
                style="primary",
                emoji="🚀",
            ),
            ButtonSpec(
                custom_id=f"aura:inc:{incident.id}:watch",
                label="Watching",
                style="secondary",
                emoji="👀",
            ),
            ButtonSpec(
                custom_id=f"aura:inc:{incident.id}:no",
                label="Can't respond",
                style="secondary",
                emoji="❌",
            ),
        ]
        buttons = tuple(pick_row + respond_row)

    return CardRender(embed=embed, buttons=buttons)


# ── sync db helpers (always called via asyncio.to_thread) ────────────────────


def _load_incident(conn: sqlite3.Connection, incident_id: int) -> Incident | None:
    row = db.query_one(conn, "SELECT * FROM incidents WHERE id = ?", (incident_id,))
    if row is None:
        return None
    updates = [
        IncidentUpdate(user_id=u["user_id"], text=u["text"], at=u["at"])
        for u in db.query(
            conn,
            "SELECT user_id, text, at FROM incident_updates WHERE incident_id = ? ORDER BY id",
            (incident_id,),
        )
    ]
    responders = {
        r["user_id"]: ResponderState(r["state"])
        for r in db.query(
            conn,
            "SELECT user_id, state FROM responders WHERE incident_id = ?",
            (incident_id,),
        )
    }
    return Incident(
        id=row["id"],
        guild_id=row["guild_id"],
        system_id=row["system_id"],
        system_confidence=row["system_confidence"],
        type=Intent(row["type"]),
        severity=Severity(row["severity"]),
        reporter_id=row["reporter_id"],
        detail=row["detail"],
        opened_at=row["opened_at"],
        updated_at=row["updated_at"],
        status=IncidentStatus(row["status"]),
        message_id=row["message_id"],
        channel_id=row["channel_id"],
        updates=updates,
        responders=responders,
    )


class IncidentEngine:
    """The single incident engine behind both voice and slash paths — GDD §9."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        holder: ConfigHolder,
        gazetteer: Gazetteer,
        discipline: Discipline,
        poster: Poster,
        rules_path: str | Path,
        *,
        on_mention: Callable[[], None] | None = None,
    ) -> None:
        self._conn = conn
        self._holder = holder
        self._gazetteer = gazetteer
        self._discipline = discipline
        self._poster = poster
        # Fired once per mention actually sent (health counter); optional so
        # the engine stays decoupled from HealthReporter.
        self._on_mention = on_mention
        self._rules_path = Path(rules_path)
        self._rules: list[RoutingRule] = []
        self._alias_roles: dict[str, int] = {}
        self._lock = asyncio.Lock()
        # Callsign registry (GDD §6.1): same conn, own write lock. Exposed via
        # the ``callsigns`` property so /rollcall and the cogs share it.
        self._callsigns = CallsignRegistry(conn)
        # Personal ping subscriptions (GDD §10.3): same pattern; the routing
        # evaluator reads its mirror on every incident open.
        self._personal_pings = PersonalPingRegistry(conn, holder)
        # In-memory only: candidate lists for uncertain (MEDIUM-tier) cards so
        # re-renders keep their pick buttons until confirmed/corrected. After a
        # restart an uncertain card keeps its already-posted buttons (persistent
        # views) but re-renders fall back to a confirmed layout.
        self._pending_candidates: dict[int, tuple[MatchCandidate, ...]] = {}
        # Freeform-relay dedupe (constraint 9 spirit): identical relay text
        # within incidents.dedupe_window_s folds instead of posting a second
        # card — pilots repeat when they miss the ack, and STT decodes the
        # same phrase again. Keyed (guild_id, casefolded text); in-memory
        # only, bounded by pruning on every use.
        self._recent_relays: dict[tuple[int, str], datetime] = {}
        # Test seam: all time flows through this attribute; tests replace it.
        self._clock: Callable[[], datetime] = _utcnow

    # ── setup ────────────────────────────────────────────────────────────────

    @property
    def callsigns(self) -> CallsignRegistry:
        """The pilot callsign registry both paths dispatch through."""
        return self._callsigns

    @property
    def personal_pings(self) -> PersonalPingRegistry:
        """The personal ping registry both paths dispatch through (GDD §10.3)."""
        return self._personal_pings

    def load_routing_rules(self, resolve_role: Callable[[str], int | None]) -> int:
        """(Re)load ``routing.yaml``. Blocking file I/O — call via ``to_thread``.

        ``resolve_role`` maps role names to ids (the bot supplies a guild
        lookup). Returns the number of active rules.
        """
        self._rules = load_routing_rules_file(self._rules_path, self._gazetteer, resolve_role)
        self._alias_roles = load_group_aliases(self._rules_path, resolve_role)
        return len(self._rules)

    # ── the single entry point (constraint 10) ───────────────────────────────

    async def report(
        self,
        guild_id: int,
        reporter_id: int,
        parsed: ParsedCommand,
        resolution: Resolution | None,
        *,
        caller_may_mention: bool = True,
    ) -> IncidentOutcome:
        """Handle one parsed command from either path; always logs to command_log.

        ``caller_may_mention`` is the @Pilot gate result (GDD §11.1 layer 4),
        threaded through to :func:`decide_mentions` so a caller who may not
        trigger mentions can never earn one — defence in depth behind the
        surface-level rejection both paths already do.
        """
        intent = parsed.intent

        if intent is Intent.CANCEL:
            outcome = await self.cancel(guild_id, reporter_id)
            await self._log_command(reporter_id, parsed, None, None, outcome.outcome)
            return outcome

        if intent is Intent.QUERY:
            outcome = await self._query_status(guild_id)
            await self._log_command(reporter_id, parsed, None, None, outcome.outcome)
            return outcome

        if intent is Intent.HELP:
            # Voice "help" (GDD §6.1): no card, no mentions — the spoken hint
            # points at /help, the real manual, plus the command_log row.
            outcome = IncidentOutcome(Outcome.POSTED, tts.help_hint(), None, None)
            await self._log_command(reporter_id, parsed, None, None, outcome.outcome)
            return outcome

        # Callsign registry (GDD §6.1): systemless, no card, no mentions —
        # a spoken/ephemeral reply plus the command_log row, nothing else.
        if intent is Intent.REGISTER:
            callsign = (parsed.detail or "").strip()
            if not callsign:
                outcome = IncidentOutcome(Outcome.REJECTED, tts.say_again_callsign(), None, None)
            else:
                utterance = await self._callsigns.register(reporter_id, callsign)
                outcome = IncidentOutcome(Outcome.POSTED, utterance, None, None)
            await self._log_command(reporter_id, parsed, None, None, outcome.outcome)
            return outcome

        if intent is Intent.UNREGISTER:
            removed, utterance = await self._callsigns.unregister(reporter_id)
            kind = Outcome.POSTED if removed else Outcome.REJECTED
            outcome = IncidentOutcome(kind, utterance, None, None)
            await self._log_command(reporter_id, parsed, None, None, outcome.outcome)
            return outcome

        if intent is Intent.WHOAMI:
            utterance = await self._callsigns.whoami(reporter_id)
            outcome = IncidentOutcome(Outcome.POSTED, utterance, None, None)
            await self._log_command(reporter_id, parsed, None, None, outcome.outcome)
            return outcome

        # Personal pings (GDD §10.3): no card, no mentions from the command
        # itself — a spoken/ephemeral reply plus the command_log row.
        if intent is Intent.PING_ME:
            outcome = await self._ping_me(guild_id, reporter_id, parsed, resolution)
            await self._log_command(reporter_id, parsed, resolution, None, outcome.outcome)
            return outcome

        if intent is Intent.PING_ME_CLEAR:
            removed = await self._personal_pings.clear(guild_id, reporter_id)
            if removed:
                outcome = IncidentOutcome(Outcome.POSTED, tts.ping_cleared(), None, None)
            else:
                outcome = IncidentOutcome(Outcome.REJECTED, tts.no_pings(), None, None)
            await self._log_command(reporter_id, parsed, None, None, outcome.outcome)
            return outcome

        # Report intents are CATCH-ALL (GDD §8.6): a distress call always
        # posts. When the spoken location does not confidently match the
        # gazetteer, it goes on the card verbatim rather than being dropped —
        # corps use region callsigns ("branch", "tribute") and raw system IDs
        # ("NVLF6-2K") that STT mangles, and losing a real call is the worst
        # possible failure. The gazetteer match, when it lands, still drives
        # routing and proximity; when it doesn't, the report survives anyway.
        if intent in _REPORT_INTENTS:
            outcome = await self._report_incident(
                guild_id, reporter_id, parsed, resolution, caller_may_mention
            )
            tier = resolution.tier if resolution is not None else None
            await self._log_command(reporter_id, parsed, resolution, tier, outcome.outcome)
            return outcome

        # Chase mode (GDD §13): retargets the pilot's live incident. Flexible
        # like the report intents — a chase must never stop to ask "say
        # again" while the pilot is mid-pursuit.
        if intent is Intent.CHASE_UPDATE:
            outcome = await self.chase_update(guild_id, reporter_id, parsed, resolution)
            tier = resolution.tier if resolution is not None else None
            await self._log_command(reporter_id, parsed, resolution, tier, outcome.outcome)
            return outcome

        # The remaining intents (clear / timer / form up) act on a *specific*
        # real system, so they still require a confident match.
        if resolution is None or resolution.tier is Tier.LOW or resolution.best is None:
            outcome = IncidentOutcome(
                outcome=Outcome.REJECTED,
                utterance="Say again the system.",
                card=None,
                incident_id=None,
            )
            await self._log_command(reporter_id, parsed, resolution, Tier.LOW, Outcome.REJECTED)
            return outcome

        best = resolution.best
        # MEDIUM ("borderline") never drives a destructive/scheduling action:
        # resolving or timing against a guessed system has no undo, so CORTANA
        # asks to confirm instead of acting — the same ASKED outcome the
        # uncertain-report flow already speaks (GDD §8.3: never silently
        # guess). The pilot repeats the command; a confirmed retry lands HIGH.
        if resolution.tier is not Tier.HIGH:
            name = self._system_name(best.system_id, best.name)
            outcome = IncidentOutcome(
                outcome=Outcome.ASKED,
                utterance=f"Heard {name} — say again to confirm.",
                card=None,
                incident_id=None,
            )
            await self._log_command(reporter_id, parsed, resolution, None, Outcome.ASKED)
            return outcome
        if intent is Intent.RESOLVE:
            outcome = await self.resolve_system(guild_id, reporter_id, best.system_id)
            await self._log_command(reporter_id, parsed, resolution, None, outcome.outcome)
            return outcome

        if intent is Intent.TIMER:
            outcome = await self._create_timer(guild_id, reporter_id, parsed, best)
            await self._log_command(reporter_id, parsed, resolution, None, outcome.outcome)
            return outcome

        if intent is Intent.FORMUP:
            outcome = await self._create_formup(guild_id, reporter_id, parsed, best)
            await self._log_command(reporter_id, parsed, resolution, None, outcome.outcome)
            return outcome

        log.warning("report_unhandled_intent", intent=str(intent))
        outcome = IncidentOutcome(
            outcome=Outcome.REJECTED, utterance=None, card=None, incident_id=None
        )
        await self._log_command(reporter_id, parsed, resolution, None, Outcome.REJECTED)
        return outcome

    # ── freeform intel relay (GDD §8.6 catch-all) ────────────────────────────

    async def broadcast(
        self,
        guild_id: int,
        reporter_id: int,
        text: str,
        *,
        here: bool = False,
        severity: Severity | None = None,
        confidence: float | None = None,
        group_alias: str | None = None,
        caller_may_mention: bool = True,
    ) -> IncidentOutcome:
        """Post any spoken/typed message to the intel channel verbatim.

        The fallback when nothing matched the fixed grammar: fleet movements
        ("blop fleet moving to Moe 8 gate"), region callsigns, freeform
        sitreps — a nullsec corp's comms are lively and unstructured, and
        dropping a call because it wasn't a recognised keyword is the wrong
        default. Slash twin: ``/relay`` (constraint 10).

        Mentions go through :func:`decide_mentions` like every other path:
        a relay is not an escalatable incident type, so **a relay can never
        @here** (constraint 11) — a spoken colour code colours the card only,
        and an "all hands" phrase (``group_alias="all_hands"``; ``here=True``
        is the deprecated voice-path spelling of the same request) mentions
        every subscribed role, gated by the caller's @Pilot standing and the
        same cooldown/circuit-breaker as any mention. A mention-free relay
        posts to ``#intel-live`` (GDD §11.2). ``confidence`` is the STT
        avg_logprob, recorded in ``command_log`` so the relay gate threshold
        can be tuned from real data."""
        text = text.strip()
        if not text:
            return IncidentOutcome(Outcome.REJECTED, None, None, None)
        card_severity = severity if severity is not None else Severity.NONE
        alias = group_alias if group_alias is not None else ("all_hands" if here else None)
        async with self._lock:
            now = self._clock()
            # Dedupe: the same relay text again within the incident dedupe
            # window is a repeat (missed ack, STT double-decode), not fresh
            # intel — ack the pilot, post nothing.
            window = timedelta(seconds=self._holder.current.incidents.dedupe_window_s)
            self._recent_relays = {
                k: at for k, at in self._recent_relays.items() if now - at <= window
            }
            relay_key = (guild_id, text.casefold())
            if relay_key in self._recent_relays:
                log.info("relay_deduped", reporter_id=reporter_id, text=text)
                return IncidentOutcome(Outcome.FOLDED, tts.relayed(), None, None)
            self._recent_relays[relay_key] = now
            who = self._callsigns.lookup(reporter_id) or f"<@{reporter_id}>"
            cfg = self._holder.current.discord
            decision = decide_mentions(
                intent=None,  # a freeform relay: structurally never @here
                severity=card_severity,
                now=now,
                rules=self._rules,
                group_alias=alias,
                alias_roles=self._alias_roles,
                here_on_severity=cfg.here_on_severity,
                mentions_enabled=cfg.mentions_enabled,
                caller_may_mention=caller_may_mention,
            )
            if decision.wants_mentions and not self._discipline.allow_mention(reporter_id, now):
                decision = decision.suppressed()
            content = _mention_content(decision)
            emoji = _SEVERITY_EMOJI[card_severity]
            code = _SEVERITY_CODE[card_severity]
            card = CardRender(
                embed={
                    "title": f"{emoji} {code} · Intel relay",
                    "description": text,
                    "color": _COLOR_BY_SEVERITY[card_severity],
                    "timestamp": _iso(now),
                    "fields": [{"name": "From", "value": who, "inline": True}],
                    "footer": {"text": "CORTANA voice relay"},
                }
            )
            try:
                await self._poster.post(
                    guild_id, decision.channel, content, card, mentions=decision
                )
            except PostError as exc:
                del self._recent_relays[relay_key]  # a failed relay is not "seen"
                log.warning("relay_post_failed", reporter_id=reporter_id, error=str(exc))
                return IncidentOutcome(Outcome.REJECTED, tts.post_failed(), None, None)
            # Discipline is charged only after the post actually went out —
            # a phantom mention from a failed post must never open the breaker.
            if decision.wants_mentions:
                self._discipline.record_mention(reporter_id, now)
                if self._on_mention is not None:
                    self._on_mention()
        await asyncio.to_thread(
            db.execute,
            self._conn,
            "INSERT INTO command_log (user_id, raw_transcript, parsed_intent,"
            " matched_system_id, confidence, tier, outcome, at)"
            " VALUES (?, ?, 'BROADCAST', NULL, ?, NULL, 'POSTED', ?)",
            (reporter_id, text, confidence, _iso(now)),
        )
        log.info(
            "intel_broadcast",
            reporter_id=reporter_id,
            roles=len(decision.role_ids),
            severity=str(card_severity),
        )
        return IncidentOutcome(Outcome.POSTED, tts.relayed(), card, None)

    # ── personal pings (GDD §10.3) ───────────────────────────────────────────

    async def _ping_me(
        self,
        guild_id: int,
        reporter_id: int,
        parsed: ParsedCommand,
        resolution: Resolution | None,
    ) -> IncidentOutcome:
        """Store one personal ping subscription and speak the confirmation.

        The system window resolves through the same phonetic pipeline as
        reports; anything below HIGH tier is treated as unresolved — a stored
        subscription silently scoped to the wrong system would never fire, so
        CORTANA asks again instead of guessing (GDD §8.3 posture).
        """
        types = types_from_detail(parsed.detail)
        system_id: int | None = None
        system_name: str | None = None
        if parsed.system_text:
            if resolution is None or resolution.tier is not Tier.HIGH or resolution.best is None:
                return IncidentOutcome(Outcome.REJECTED, tts.say_again(), None, None)
            best = resolution.best
            system_id = best.system_id
            system_name = self._system_name(best.system_id, best.name)
        added = await self._personal_pings.add(guild_id, reporter_id, types, system_id)
        if not added:
            return IncidentOutcome(Outcome.REJECTED, tts.ping_limit(), None, None)
        utterance = tts.pinging_you(tts.ping_types_phrase(types), system_name)
        return IncidentOutcome(Outcome.POSTED, utterance, None, None)

    # ── incident reports: dedupe fold or create (GDD §9.2) ───────────────────

    async def _report_incident(
        self,
        guild_id: int,
        reporter_id: int,
        parsed: ParsedCommand,
        resolution: Resolution,
        caller_may_mention: bool,
    ) -> IncidentOutcome:
        async with self._lock:
            now = self._clock()
            cfg = self._holder.current
            best = resolution.best if resolution is not None else None
            resolved = best is not None and resolution.tier is not Tier.LOW
            window = timedelta(seconds=cfg.incidents.dedupe_window_s)

            if resolved:
                system_name = self._system_name(best.system_id, best.name)
                existing_id = await asyncio.to_thread(
                    db.query_value,
                    self._conn,
                    "SELECT id FROM incidents WHERE guild_id = ? AND system_id = ? AND type = ?"
                    " AND status = 'ACTIVE' AND updated_at >= ? ORDER BY updated_at DESC LIMIT 1",
                    (guild_id, best.system_id, str(parsed.intent), _iso(now - window)),
                )
            else:
                # Unmatched location: post it verbatim. Dedupe on the raw text
                # so repeats of the same spoken location still fold to one card.
                system_name = parsed.system_text or _UNKNOWN_LOCATION
                existing_id = await asyncio.to_thread(
                    db.query_value,
                    self._conn,
                    "SELECT id FROM incidents WHERE guild_id = ? AND system_id IS NULL"
                    " AND raw_system_text IS ? AND type = ? AND status = 'ACTIVE'"
                    " AND updated_at >= ? ORDER BY updated_at DESC LIMIT 1",
                    (guild_id, parsed.system_text, str(parsed.intent), _iso(now - window)),
                )
            if existing_id is not None:
                return await self._fold(int(existing_id), reporter_id, parsed, now, system_name)
            return await self._open_incident(
                guild_id,
                reporter_id,
                parsed,
                resolution,
                now,
                system_name,
                resolved,
                caller_may_mention,
            )

    async def _fold(
        self,
        incident_id: int,
        reporter_id: int,
        parsed: ParsedCommand,
        now: datetime,
        system_name: str,
    ) -> IncidentOutcome:
        """Fold a duplicate report into the live incident: edit, never re-mention.

        Severity raises but never lowers: a pilot escalating with a spoken
        colour ("code red, hostiles UMI" folding into a plain sighting) turns
        the card red on the re-render instead of being silently dropped. The
        no-re-mention rule is untouched — severity display and mention policy
        are separable (GDD §9.2).
        """

        def _write() -> Incident | None:
            db.execute(
                self._conn,
                "INSERT INTO incident_updates (incident_id, user_id, text, at) VALUES (?, ?, ?, ?)",
                (incident_id, reporter_id, parsed.detail, _iso(now)),
            )
            db.execute(
                self._conn,
                "UPDATE incidents SET updated_at = ? WHERE id = ?",
                (_iso(now), incident_id),
            )
            if parsed.severity is not None:
                stored = db.query_value(
                    self._conn, "SELECT severity FROM incidents WHERE id = ?", (incident_id,)
                )
                if (
                    stored is not None
                    and _SEVERITY_RANK[parsed.severity] > _SEVERITY_RANK[Severity(stored)]
                ):
                    db.execute(
                        self._conn,
                        "UPDATE incidents SET severity = ? WHERE id = ?",
                        (str(parsed.severity), incident_id),
                    )
            return _load_incident(self._conn, incident_id)

        incident = await asyncio.to_thread(_write)
        if incident is None:  # pragma: no cover — row deleted mid-flight
            return IncidentOutcome(Outcome.REJECTED, None, None, None)
        card = self._render(incident, system_name)
        if incident.channel_id is not None and incident.message_id is not None:
            await self._poster.edit(incident.channel_id, incident.message_id, "", card)
        log.info(
            "incident_folded",
            incident_id=incident_id,
            reporter_id=reporter_id,
            reporter_count=incident.reporter_count,
        )
        utterance = f"Added to {system_name}, reported by {incident.reporter_count}."
        return IncidentOutcome(Outcome.FOLDED, utterance, card, incident_id)

    async def _open_incident(
        self,
        guild_id: int,
        reporter_id: int,
        parsed: ParsedCommand,
        resolution: Resolution | None,
        now: datetime,
        system_name: str,
        resolved: bool,
        caller_may_mention: bool,
    ) -> IncidentOutcome:
        best = resolution.best if resolution is not None else None
        # A spoken colour code (GDD §6.4) overrides the intent's default:
        # "code red, hostiles in UMI" is a HIGH-severity sighting.
        severity = (
            parsed.severity if parsed.severity is not None else INTENT_SEVERITY[parsed.intent]
        )
        # Only a resolved-but-borderline (MEDIUM) match offers the confirm
        # buttons; an unmatched location has nothing to confirm against.
        uncertain = resolved and resolution is not None and resolution.tier is Tier.MEDIUM
        system_id = best.system_id if resolved and best is not None else None
        confidence = best.score if resolved and best is not None else None

        incident_id = await asyncio.to_thread(
            db.execute,
            self._conn,
            "INSERT INTO incidents (guild_id, system_id, system_confidence, type, severity,"
            " reporter_id, detail, opened_at, updated_at, status, raw_system_text)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)",
            (
                guild_id,
                system_id,
                confidence,
                str(parsed.intent),
                str(severity),
                reporter_id,
                parsed.detail,
                _iso(now),
                _iso(now),
                # The transcript window that named the system (§8.5): survives
                # restarts so a later [Wrong — fix] press can learn the alias.
                parsed.system_text,
            ),
        )
        incident = await asyncio.to_thread(_load_incident, self._conn, incident_id)
        assert incident is not None

        # ONE escalation decision (GDD §6.4/§10/§11): rule evaluation, group
        # alias, ping-by-colour, the @Pilot gate, silent mode, and the channel
        # all resolve inside decide_mentions — the engine adds nothing on top.
        # Personal subscribers (GDD §10.3) ride the same decision, so
        # cooldown/breaker suppression and the dedupe fold's no-re-mention
        # rule apply to them identically.
        cfg_discord = self._holder.current.discord
        decision = decide_mentions(
            intent=parsed.intent,
            severity=severity,
            now=now,
            rules=self._rules,
            incident=incident,
            gazetteer=self._gazetteer,
            personal=self._personal_pings.rules_for(guild_id),
            group_alias=parsed.group_alias,
            alias_roles=self._alias_roles,
            here_on_severity=cfg_discord.here_on_severity,
            mentions_enabled=cfg_discord.mentions_enabled,
            caller_may_mention=caller_may_mention,
        )
        flood_announced = False
        if decision.wants_mentions and not self._discipline.allow_mention(reporter_id, now):
            decision = decision.suppressed()
            flood_announced = self._discipline.should_announce_flood(now)

        if uncertain:
            self._pending_candidates[incident_id] = resolution.candidates

        card = self._render(incident, system_name)
        content = (
            "⚠️ Flood control active — mentions suppressed."
            if flood_announced
            else _mention_content(decision)
        )
        try:
            channel_id, message_id = await self._poster.post(
                guild_id, decision.channel, content, card, mentions=decision
            )
        except PostError as exc:
            # Roll the incident back: an ACTIVE row with no message would
            # silently fold every duplicate report into a card that exists
            # nowhere — pilots would hear confirmations for an alert nobody
            # can see. The pilot is told the post failed instead. Discipline
            # was never charged — a phantom mention must not run a cooldown
            # or open the breaker.
            self._pending_candidates.pop(incident_id, None)
            await asyncio.to_thread(
                db.execute, self._conn, "DELETE FROM incidents WHERE id = ?", (incident_id,)
            )
            log.warning("incident_rolled_back_post_failed", incident_id=incident_id, error=str(exc))
            return IncidentOutcome(Outcome.REJECTED, tts.post_failed(), None, None)
        # Discipline is charged only after the post actually went out.
        if decision.wants_mentions:
            self._discipline.record_mention(reporter_id, now)
            if self._on_mention is not None:
                self._on_mention()
        await asyncio.to_thread(
            db.execute,
            self._conn,
            "UPDATE incidents SET channel_id = ?, message_id = ? WHERE id = ?",
            (channel_id, message_id, incident_id),
        )

        label = _TYPE_LABELS[parsed.intent]
        # Readback (GDD §8.3): when the pilot SPOKE a colour code, it comes
        # back in the confirmation — "Tackled UMI, code red, posted." — so
        # they hear exactly what the card says without looking at Discord.
        spoken_code = (
            f", code {_SEVERITY_SPOKEN_WORD[severity]}" if parsed.severity is not None else ""
        )
        if flood_announced:
            utterance = "Flood control active."
        elif uncertain:
            utterance = f"{label} {system_name} — say again to confirm."
        elif decision.role_ids or decision.here or decision.user_ids:
            scoped = _GROUP_ALIAS_SPOKEN.get(parsed.group_alias or "")
            utterance = (
                f"{label} {system_name}{spoken_code}, pinged {scoped}."
                if scoped
                else f"{label} {system_name}{spoken_code}, pinged."
            )
        else:
            utterance = f"{label} {system_name}{spoken_code}, posted."

        log.info(
            "incident_opened",
            incident_id=incident_id,
            type=str(parsed.intent),
            system=system_name,
            severity=str(severity),
            uncertain=uncertain,
            mentions=len(decision.role_ids),
            personal_pings=len(decision.user_ids),
            here=decision.here,
        )
        outcome = Outcome.ASKED if uncertain else Outcome.POSTED
        return IncidentOutcome(outcome, utterance, card, incident_id)

    # ── resolve / cancel (GDD §9.1, §6.1) ────────────────────────────────────

    async def chase_update(
        self,
        guild_id: int,
        reporter_id: int,
        parsed: ParsedCommand,
        resolution: Resolution | None,
    ) -> IncidentOutcome:
        """ "update chase <system>" / ``/chase`` — retarget the pilot's live
        incident card as the target moves (GDD §13).

        The card is edited in place (constraint 9) and keeps a movement trail
        in its updates. System matching is flexible: a confident gazetteer
        match binds the real system (routing/proximity keep working); anything
        else goes on the card verbatim — a chase never stops to ask
        "say again the system" mid-pursuit.
        """
        system_text = (parsed.system_text or "").strip()
        if not system_text:
            return IncidentOutcome(Outcome.REJECTED, tts.chase_hint(), None, None)
        async with self._lock:
            now = self._clock()
            # Only REPORT cards are chaseable — form-ups and timers share the
            # incidents table and an unfiltered "latest ACTIVE" once rewrote a
            # fleet's rally card to the chase system. A pilot whose duplicate
            # report was dedupe-folded into someone else's card is on that
            # card's reporter set (incident_updates), so they may chase too.
            report_types = tuple(str(i) for i in sorted(_REPORT_INTENTS))
            placeholders = ", ".join("?" for _ in report_types)
            row = await asyncio.to_thread(
                db.query_one,
                self._conn,
                "SELECT id FROM incidents WHERE guild_id = ? AND status = 'ACTIVE'"
                f" AND type IN ({placeholders})"
                " AND (reporter_id = ? OR id IN"
                "      (SELECT incident_id FROM incident_updates WHERE user_id = ?))"
                " ORDER BY id DESC LIMIT 1",
                (guild_id, *report_types, reporter_id, reporter_id),
            )
            if row is None:
                return IncidentOutcome(Outcome.REJECTED, tts.chase_no_incident(), None, None)
            incident_id = int(row["id"])

            confident = (
                resolution is not None
                and resolution.best is not None
                and resolution.tier is not Tier.LOW
            )
            if confident:
                assert resolution is not None and resolution.best is not None
                system_id: int | None = resolution.best.system_id
                system_name = self._system_name(system_id, resolution.best.name)
                confidence: float | None = resolution.best.score
            else:
                system_id = None
                system_name = system_text
                confidence = None

            def _write() -> Incident | None:
                db.execute(
                    self._conn,
                    "UPDATE incidents SET system_id = ?, system_confidence = ?,"
                    " raw_system_text = ?, updated_at = ? WHERE id = ?",
                    (system_id, confidence, system_text, _iso(now), incident_id),
                )
                db.execute(
                    self._conn,
                    "INSERT INTO incident_updates (incident_id, user_id, text, at)"
                    " VALUES (?, ?, ?, ?)",
                    (incident_id, reporter_id, f"chase → {system_name}", _iso(now)),
                )
                return _load_incident(self._conn, incident_id)

            incident = await asyncio.to_thread(_write)
            if incident is None:  # pragma: no cover — row deleted mid-flight
                return IncidentOutcome(Outcome.REJECTED, None, None, None)
            # The card's location changed: any pending confirm-candidates
            # pointed at the OLD heard name.
            self._pending_candidates.pop(incident_id, None)
            card = self._render(incident, system_name)
            if incident.channel_id is not None and incident.message_id is not None:
                await self._poster.edit(incident.channel_id, incident.message_id, "", card)
            log.info(
                "chase_updated",
                incident_id=incident_id,
                system=system_name,
                resolved=system_id is not None,
            )
            return IncidentOutcome(
                Outcome.POSTED, tts.chase_updated(system_name), card, incident_id
            )

    async def resolve_system(self, guild_id: int, user_id: int, system_id: int) -> IncidentOutcome:
        """ "clear <system>" / ``/clear`` — resolve every open incident there."""
        async with self._lock:
            now = self._clock()
            system_name = self._system_name(system_id, str(system_id))

            def _resolve() -> list[Incident]:
                rows = db.query(
                    self._conn,
                    "SELECT id FROM incidents WHERE guild_id = ? AND system_id = ?"
                    " AND status != 'RESOLVED'",
                    (guild_id, system_id),
                )
                resolved: list[Incident] = []
                for row in rows:
                    db.execute(
                        self._conn,
                        "UPDATE incidents SET status = 'RESOLVED', updated_at = ? WHERE id = ?",
                        (_iso(now), row["id"]),
                    )
                    incident = _load_incident(self._conn, row["id"])
                    if incident is not None:
                        resolved.append(incident)
                return resolved

            resolved = await asyncio.to_thread(_resolve)
            if not resolved:
                return IncidentOutcome(
                    Outcome.REJECTED, f"No active incident for {system_name}.", None, None
                )
            card: CardRender | None = None
            for incident in resolved:
                self._pending_candidates.pop(incident.id, None)
                card = self._render(incident, system_name)
                if incident.channel_id is not None and incident.message_id is not None:
                    await self._poster.edit(incident.channel_id, incident.message_id, "", card)
            log.info(
                "incidents_resolved",
                system=system_name,
                count=len(resolved),
                user_id=user_id,
            )
            return IncidentOutcome(Outcome.POSTED, f"{system_name} clear.", card, resolved[-1].id)

    async def cancel(self, guild_id: int, user_id: int) -> IncidentOutcome:
        """Kill this user's last incident, inside ``incidents.cancel_window_s``."""
        async with self._lock:
            now = self._clock()
            window = timedelta(seconds=self._holder.current.incidents.cancel_window_s)

            def _cancel() -> Incident | None:
                row = db.query_one(
                    self._conn,
                    "SELECT id FROM incidents WHERE guild_id = ? AND reporter_id = ?"
                    " AND status = 'ACTIVE' AND opened_at >= ?"
                    " ORDER BY opened_at DESC LIMIT 1",
                    (guild_id, user_id, _iso(now - window)),
                )
                if row is None:
                    return None
                db.execute(
                    self._conn,
                    "UPDATE incidents SET status = 'RESOLVED', updated_at = ? WHERE id = ?",
                    (_iso(now), row["id"]),
                )
                return _load_incident(self._conn, row["id"])

            incident = await asyncio.to_thread(_cancel)
            if incident is None:
                return IncidentOutcome(Outcome.REJECTED, "Nothing to cancel.", None, None)
            self._pending_candidates.pop(incident.id, None)
            system_name = self._system_name(incident.system_id, "unknown")
            card = self._render(incident, system_name, cancelled=True)
            if incident.channel_id is not None and incident.message_id is not None:
                await self._poster.edit(incident.channel_id, incident.message_id, "", card)
            log.info("incident_cancelled", incident_id=incident.id, user_id=user_id)
            return IncidentOutcome(Outcome.POSTED, "Cancelled.", card, incident.id)

    # ── response loop (GDD §9.3) ─────────────────────────────────────────────

    async def respond(
        self,
        incident_id: int,
        user_id: int,
        state: ResponderState,
        *,
        display_name: str | None = None,
    ) -> IncidentOutcome:
        """Button press: upsert the responder row and edit the card in place.

        ``display_name`` is the clicker's guild display name; the registered
        callsign wins over it, and either makes the spoken callout name WHO is
        coming — "Space Junkie responding to Otanuomi." — instead of a bare
        count (the count remains the fallback when no name is known)."""
        async with self._lock:
            now = self._clock()

            def _respond() -> tuple[Incident | None, ResponderState | None]:
                incident = _load_incident(self._conn, incident_id)
                if incident is None or incident.status is IncidentStatus.RESOLVED:
                    return None, None
                previous = incident.responders.get(user_id)
                db.execute(
                    self._conn,
                    "INSERT INTO responders (incident_id, user_id, state, at)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (incident_id, user_id)"
                    " DO UPDATE SET state = excluded.state, at = excluded.at",
                    (incident_id, user_id, str(state), _iso(now)),
                )
                db.execute(
                    self._conn,
                    "UPDATE incidents SET updated_at = ? WHERE id = ?",
                    (_iso(now), incident_id),
                )
                return _load_incident(self._conn, incident_id), previous

            incident, previous = await asyncio.to_thread(_respond)
            if incident is None:
                return IncidentOutcome(Outcome.REJECTED, None, None, None)
            system_name = self._system_name(incident.system_id, "unknown")
            card = self._render(incident, system_name)
            if incident.channel_id is not None and incident.message_id is not None:
                await self._poster.edit(incident.channel_id, incident.message_id, "", card)
            utterance: str | None = None
            if state is ResponderState.OTW and previous is not ResponderState.OTW:
                name = self._callsigns.lookup(user_id) or display_name
                if name:
                    spoken_system = system_name if system_name != "unknown" else None
                    utterance = tts.responder_named(name, spoken_system)
                else:
                    otw = sum(1 for s in incident.responders.values() if s is ResponderState.OTW)
                    utterance = f"{_number_word(otw)} responding to {system_name}."
            log.info(
                "responder_updated",
                incident_id=incident_id,
                user_id=user_id,
                state=str(state),
            )
            return IncidentOutcome(Outcome.POSTED, utterance, card, incident_id)

    # ── [Wrong — fix] correction + alias learning (GDD §8.5) ─────────────────

    async def correct_system(
        self, incident_id: int, user_id: int, system_id: int, raw_text: str
    ) -> IncidentOutcome:
        """Apply a human correction: update the card AND learn the alias.

        An explicit ``raw_text`` (a caller-supplied transcript) wins; when it
        is empty — the pick/fix buttons carry no transcript — the alias key
        falls back to the ``raw_system_text`` stored with the incident when it
        opened, so button corrections still learn (§8.5), even after a
        restart (the buttons are restart-proof, GDD §9.3).
        """
        async with self._lock:
            now = self._clock()

            def _correct() -> tuple[Incident | None, str] | None:
                if _load_incident(self._conn, incident_id) is None:
                    return None
                alias_key = raw_text.strip().lower()
                if not alias_key:
                    stored = db.query_value(
                        self._conn,
                        "SELECT raw_system_text FROM incidents WHERE id = ?",
                        (incident_id,),
                    )
                    alias_key = (stored or "").strip().lower()
                db.execute(
                    self._conn,
                    "UPDATE incidents SET system_id = ?, system_confidence = 1.0,"
                    " updated_at = ? WHERE id = ?",
                    (system_id, _iso(now), incident_id),
                )
                if alias_key:
                    db.execute(
                        self._conn,
                        "INSERT INTO aliases (raw_text, system_id, weight, learned_at,"
                        " corrected_by) VALUES (?, ?, 1.0, ?, ?)"
                        " ON CONFLICT (raw_text, system_id)"
                        " DO UPDATE SET weight = weight + 1.0, learned_at = excluded.learned_at,"
                        " corrected_by = excluded.corrected_by",
                        (alias_key, system_id, _iso(now), user_id),
                    )
                return _load_incident(self._conn, incident_id), alias_key

            incident, learned_alias = await asyncio.to_thread(_correct) or (None, "")
            if incident is None:
                return IncidentOutcome(Outcome.REJECTED, None, None, None)
            self._pending_candidates.pop(incident_id, None)
            system_name = self._system_name(system_id, str(system_id))
            card = self._render(incident, system_name)
            if incident.channel_id is not None and incident.message_id is not None:
                await self._poster.edit(incident.channel_id, incident.message_id, "", card)
            log.info(
                "system_corrected",
                incident_id=incident_id,
                system=system_name,
                alias=learned_alias,
                user_id=user_id,
            )
            return IncidentOutcome(
                Outcome.POSTED, f"Corrected to {system_name}.", card, incident_id
            )

    # ── staleness sweep (GDD §9.1) ───────────────────────────────────────────

    async def sweep_stale(self) -> list[int]:
        """Mark incidents with no updates for ``stale_after_min`` STALE, silently.

        FORMUP is excluded: a rally card legitimately sits quiet until its
        ``fires_at`` — staleness is anchored by the countdown timer, not by
        update chatter (GDD §13).
        """
        async with self._lock:
            now = self._clock()
            cutoff = now - timedelta(minutes=self._holder.current.incidents.stale_after_min)

            def _sweep() -> list[Incident]:
                rows = db.query(
                    self._conn,
                    "SELECT id FROM incidents WHERE status = 'ACTIVE' AND updated_at < ?"
                    " AND type != 'FORMUP'",
                    (_iso(cutoff),),
                )
                stale: list[Incident] = []
                for row in rows:
                    db.execute(
                        self._conn,
                        "UPDATE incidents SET status = 'STALE' WHERE id = ?",
                        (row["id"],),
                    )
                    incident = _load_incident(self._conn, row["id"])
                    if incident is not None:
                        stale.append(incident)
                return stale

            stale = await asyncio.to_thread(_sweep)
            cards: list[tuple[Incident, CardRender]] = []
            for incident in stale:
                # STALE is a terminal state for unconfirmed cards — drop their
                # candidate lists or they leak for the process lifetime.
                self._pending_candidates.pop(incident.id, None)
                system_name = self._system_name(incident.system_id, "unknown")
                cards.append((incident, self._render(incident, system_name)))
        # Card edits happen OUTSIDE the engine lock: discord.py can sleep on
        # rate-limit buckets mid-edit, and holding the lock through a
        # multi-card sweep would block every report, fold, and button press
        # for the duration (button interactions have a 3s answer window).
        for incident, card in cards:
            if incident.channel_id is not None and incident.message_id is not None:
                await self._poster.edit(incident.channel_id, incident.message_id, "", card)
        if stale:
            log.info("incidents_stale", ids=[i.id for i in stale])
        return [i.id for i in stale]

    # ── timers and form-ups (GDD §13) ────────────────────────────────────────

    async def _create_timer(
        self, guild_id: int, created_by: int, parsed: ParsedCommand, best: MatchCandidate
    ) -> IncidentOutcome:
        duration = parse_duration(parsed.detail or "")
        if duration is None:
            return IncidentOutcome(Outcome.REJECTED, "Say again the timer duration.", None, None)
        async with self._lock:
            now = self._clock()
            fires_at = now + duration
            system_name = self._system_name(best.system_id, best.name)
            await asyncio.to_thread(
                db.execute,
                self._conn,
                "INSERT INTO timers (guild_id, system_id, fires_at, note, created_by, fired)"
                " VALUES (?, ?, ?, ?, ?, 0)",
                (guild_id, best.system_id, _iso(fires_at), parsed.detail, created_by),
            )
            log.info(
                "timer_created",
                guild_id=guild_id,
                system=system_name,
                fires_at=_iso(fires_at),
            )
            utterance = tts.timer_set(system_name, _format_duration(duration))
            return IncidentOutcome(Outcome.POSTED, utterance, None, None)

    async def _create_formup(
        self, guild_id: int, created_by: int, parsed: ParsedCommand, best: MatchCandidate
    ) -> IncidentOutcome:
        duration = parse_duration(parsed.detail or "")
        if duration is None:
            return IncidentOutcome(Outcome.REJECTED, "Say again the form-up time.", None, None)
        async with self._lock:
            now = self._clock()
            fires_at = now + duration
            system_name = self._system_name(best.system_id, best.name)
            incident_id = await asyncio.to_thread(
                db.execute,
                self._conn,
                "INSERT INTO incidents (guild_id, system_id, system_confidence, type, severity,"
                " reporter_id, detail, opened_at, updated_at, status)"
                " VALUES (?, ?, ?, 'FORMUP', 'none', ?, ?, ?, ?, 'ACTIVE')",
                (
                    guild_id,
                    best.system_id,
                    best.score,
                    created_by,
                    parsed.detail,
                    _iso(now),
                    _iso(now),
                ),
            )
            incident = await asyncio.to_thread(_load_incident, self._conn, incident_id)
            assert incident is not None
            card = self._render(incident, system_name, formup_at=_iso(fires_at))
            try:
                channel_id, message_id = await self._poster.post(
                    guild_id, AlertChannel.LIVE, "", card
                )
            except PostError as exc:
                # Same rollback guard as _open_incident: an ACTIVE FORMUP row
                # with no message (and no countdown timer yet) is invisible —
                # delete it and tell the pilot the post failed.
                await asyncio.to_thread(
                    db.execute, self._conn, "DELETE FROM incidents WHERE id = ?", (incident_id,)
                )
                log.warning(
                    "formup_rolled_back_post_failed", incident_id=incident_id, error=str(exc)
                )
                return IncidentOutcome(Outcome.REJECTED, tts.post_failed(), None, None)
            await asyncio.to_thread(
                db.execute,
                self._conn,
                "UPDATE incidents SET channel_id = ?, message_id = ? WHERE id = ?",
                (channel_id, message_id, incident_id),
            )
            # Countdown ping when the form-up comes due.
            await asyncio.to_thread(
                db.execute,
                self._conn,
                "INSERT INTO timers (guild_id, system_id, fires_at, note, created_by, fired)"
                " VALUES (?, ?, ?, ?, ?, 0)",
                (
                    guild_id,
                    best.system_id,
                    _iso(fires_at),
                    f"Form-up {system_name}",
                    created_by,
                ),
            )
            log.info(
                "formup_created",
                incident_id=incident_id,
                system=system_name,
                fires_at=_iso(fires_at),
            )
            utterance = f"Form up {system_name}, {_format_duration(duration)}."
            return IncidentOutcome(Outcome.POSTED, utterance, card, incident_id)

    async def fire_due_timers(self, now: datetime) -> list[TimerPing]:
        """Poll for due timers, mark them fired, and return their ping payloads."""
        async with self._lock:

            def _fire() -> list[sqlite3.Row]:
                rows = db.query(
                    self._conn,
                    "SELECT * FROM timers WHERE fired = 0 AND fires_at <= ? ORDER BY fires_at",
                    (_iso(now),),
                )
                for row in rows:
                    db.execute(self._conn, "UPDATE timers SET fired = 1 WHERE id = ?", (row["id"],))
                return rows

            rows = await asyncio.to_thread(_fire)
            pings = [
                TimerPing(
                    timer_id=row["id"],
                    guild_id=row["guild_id"],
                    system_id=row["system_id"],
                    system_name=(
                        self._system_name(row["system_id"], None)
                        if row["system_id"] is not None
                        else None
                    ),
                    note=row["note"],
                    fires_at=row["fires_at"],
                    created_by=row["created_by"],
                )
                for row in rows
            ]
            if pings:
                log.info("timers_fired", ids=[p.timer_id for p in pings])
            return pings

    # ── status query (GDD §6.1 "status") ─────────────────────────────────────

    async def _query_status(self, guild_id: int) -> IncidentOutcome:
        rows = await asyncio.to_thread(
            db.query,
            self._conn,
            "SELECT system_id FROM incidents WHERE guild_id = ? AND status = 'ACTIVE'"
            " ORDER BY updated_at DESC",
            (guild_id,),
        )
        if not rows:
            return IncidentOutcome(Outcome.POSTED, "All clear, no active incidents.", None, None)
        names: list[str] = []
        for row in rows[:3]:
            name = self._system_name(row["system_id"], "unknown")
            if name not in names:
                names.append(name)
        count = len(rows)
        plural = "incident" if count == 1 else "incidents"
        utterance = f"{_number_word(count)} active {plural}: {', '.join(names)}."
        return IncidentOutcome(Outcome.POSTED, utterance, None, None)

    # ── context priors input (GDD §8.4) ──────────────────────────────────────

    def build_prior_context(self, guild_id: int, reporter_id: int) -> PriorContext:
        """Blocking — callers on the event loop wrap this in ``asyncio.to_thread``."""
        now = self._clock()
        priors = self._holder.current.matching.priors
        recency_cutoff = _iso(now - timedelta(minutes=priors.recency_window_min))
        recency: dict[int, float] = {}
        for row in db.query(
            self._conn,
            "SELECT system_id, MAX(updated_at) AS last_at FROM incidents"
            " WHERE guild_id = ? AND system_id IS NOT NULL AND updated_at >= ?"
            " GROUP BY system_id",
            (guild_id, recency_cutoff),
        ):
            last = datetime.fromisoformat(row["last_at"])
            recency[row["system_id"]] = max((now - last).total_seconds() / 60.0, 0.0)

        history_cutoff = _iso(now - timedelta(days=7))
        reporter_counts: dict[int, int] = {
            row["system_id"]: row["n"]
            for row in db.query(
                self._conn,
                "SELECT system_id, COUNT(*) AS n FROM incidents"
                " WHERE guild_id = ? AND reporter_id = ? AND system_id IS NOT NULL"
                " AND opened_at >= ? GROUP BY system_id",
                (guild_id, reporter_id, history_cutoff),
            )
        }
        active_systems = tuple(
            row["system_id"]
            for row in db.query(
                self._conn,
                "SELECT DISTINCT system_id FROM incidents WHERE guild_id = ?"
                " AND status = 'ACTIVE' AND system_id IS NOT NULL",
                (guild_id,),
            )
        )
        return PriorContext(
            recency_min=recency,
            reporter_counts=reporter_counts,
            active_systems=active_systems,
            home_system_id=self._gazetteer.home_system_id,
        )

    # ── internals ────────────────────────────────────────────────────────────

    def _system_name(self, system_id: int | None, fallback: str | None) -> str:
        if system_id is not None:
            entry = self._gazetteer.by_id(system_id)
            if entry is not None:
                return entry.name
        return fallback if fallback is not None else "unknown"

    def _render(
        self,
        incident: Incident,
        system_name: str,
        *,
        cancelled: bool = False,
        formup_at: str | None = None,
    ) -> CardRender:
        candidates = self._pending_candidates.get(incident.id, ())
        return render_card(
            incident,
            system_name,
            uncertain=bool(candidates) and incident.status is IncidentStatus.ACTIVE,
            candidates=candidates,
            cancelled=cancelled,
            formup_at=formup_at,
            reporter_callsign=self._callsigns.lookup(incident.reporter_id),
        )

    async def _log_command(
        self,
        user_id: int,
        parsed: ParsedCommand,
        resolution: Resolution | None,
        tier_override: Tier | None,
        outcome: Outcome,
    ) -> None:
        """Write the command_log row — transcripts only, never audio (GDD §19)."""
        best = resolution.best if resolution is not None else None
        tier = (
            tier_override
            if tier_override is not None
            else (resolution.tier if resolution is not None else None)
        )
        await asyncio.to_thread(
            db.execute,
            self._conn,
            "INSERT INTO command_log (user_id, raw_transcript, parsed_intent,"
            " matched_system_id, confidence, tier, outcome, at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                parsed.raw,
                str(parsed.intent),
                best.system_id if best is not None else None,
                best.score if best is not None else None,
                str(tier) if tier is not None else None,
                str(outcome),
                _iso(self._clock()),
            ),
        )
