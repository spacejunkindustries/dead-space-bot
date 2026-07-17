"""Subscription routing — GDD §10 rule evaluation and §11 escalation discipline.

``evaluate`` is a pure function of its inputs: incident × rules × ``now``
(quiet hours) × the gazetteer (region / jump-distance scope). Rule loading
from ``routing.yaml`` is separate (:func:`load_rules`) so the evaluator stays
trivially testable.

Escalation discipline (CLAUDE.md constraint 11): ``@here`` fires ONLY when the
incident type is ``UNDER_ATTACK`` or ``ASSIST_REQUEST`` *and* a matching rule
escalates at exactly that type. A misconfigured ``escalate_at`` on any other
type is clamped at load time and re-clamped at evaluation time — a sighting
can never produce ``here=True`` through this module, full stop.

Channel semantics: a decision with any mention carries ``AlertChannel.ALERTS``;
``#intel-alerts`` receives only mentioning incidents while ``#intel-live`` is
the mention-free firehose (GDD §11.2), so ``channel == ALERTS`` always implies
the incident is also visible via the live log — the ``Poster`` implementation
mirrors alerted cards there.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
import yaml

from aura.types import AlertChannel, Incident, Intent, RoutingDecision

if TYPE_CHECKING:  # pragma: no cover — real class lands with aura.nlu.gazetteer
    from aura.nlu.gazetteer import Gazetteer

__all__ = [
    "ESCALATABLE_TYPES",
    "QuietHours",
    "RoutingConfigError",
    "RoutingRule",
    "RuleScope",
    "apply_group_alias",
    "evaluate",
    "load_group_aliases",
    "load_rules",
    "suppress",
]

log = structlog.get_logger(__name__)

#: The only incident types that may ever fire ``@here`` (constraint 11).
ESCALATABLE_TYPES: frozenset[Intent] = frozenset({Intent.UNDER_ATTACK, Intent.ASSIST_REQUEST})

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class RoutingConfigError(Exception):
    """routing.yaml is missing, unreadable, or structurally invalid."""


# ── rule model ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RuleScope:
    """Where a rule applies. Any populated constraint matching counts as a hit;

    a scope with no constraints at all matches every system (corp-wide rule).
    ``within_jumps_of`` is ``(anchor_system_id, max_jumps)``.
    """

    systems: tuple[int, ...] = ()
    regions: tuple[str, ...] = ()
    within_jumps_of: tuple[int, int] | None = None

    @property
    def unrestricted(self) -> bool:
        return not self.systems and not self.regions and self.within_jumps_of is None


@dataclass(frozen=True, slots=True)
class QuietHours:
    """A daily suppression window in a named timezone; may span midnight.

    ``start``/``end`` are ``"HH:MM"`` local to ``tz``. ``start == end`` means
    the window is empty (never active), not always-on.
    """

    tz: str
    start: str
    end: str

    def active_at(self, now: datetime) -> bool:
        """True when ``now`` (tz-aware) falls inside the quiet window."""
        local = now.astimezone(ZoneInfo(self.tz))
        cur = local.hour * 60 + local.minute
        start = _parse_hhmm(self.start, "quiet_hours.start")
        end = _parse_hhmm(self.end, "quiet_hours.end")
        if start == end:
            return False
        if start < end:
            return start <= cur < end
        return cur >= start or cur < end  # spans midnight


@dataclass(frozen=True, slots=True)
class RoutingRule:
    """One subscription rule from ``routing.yaml`` / the subscriptions table."""

    role_id: int
    types: frozenset[Intent]
    scope: RuleScope
    escalate_at: Intent | None
    quiet_hours: QuietHours | None


def _parse_hhmm(value: str, dotted: str) -> int:
    m = _HHMM.match(value)
    if m is None:
        raise RoutingConfigError(f"{dotted}: expected HH:MM, got {value!r}")
    return int(m.group(1)) * 60 + int(m.group(2))


# ── evaluation (pure) ────────────────────────────────────────────────────────


def _scope_matches(scope: RuleScope, system_id: int | None, gazetteer: Gazetteer) -> bool:
    if scope.unrestricted:
        return True
    if system_id is None:
        return False
    if system_id in scope.systems:
        return True
    if scope.regions:
        entry = gazetteer.by_id(system_id)
        if entry is not None and entry.region in scope.regions:
            return True
    if scope.within_jumps_of is not None:
        anchor, max_jumps = scope.within_jumps_of
        distance = gazetteer.jumps(anchor, system_id)
        if distance is not None and distance <= max_jumps:
            return True
    return False


def evaluate(
    incident: Incident,
    rules: Sequence[RoutingRule],
    now: datetime,
    *,
    gazetteer: Gazetteer,
) -> RoutingDecision:
    """Evaluate every rule against the incident — GDD §10.1.

    Union the matching roles (each mentioned once), decide ``@here``
    escalation under constraint 11, and pick the channel: any mention goes to
    ``#intel-alerts``, otherwise ``#intel-live``. A rule inside its quiet
    hours contributes neither its role nor its escalation.
    """
    role_ids: list[int] = []
    here = False
    for rule in rules:
        if incident.type not in rule.types:
            continue
        if not _scope_matches(rule.scope, incident.system_id, gazetteer):
            continue
        if rule.quiet_hours is not None and rule.quiet_hours.active_at(now):
            continue
        if rule.role_id not in role_ids:
            role_ids.append(rule.role_id)
        if (
            rule.escalate_at is not None
            and rule.escalate_at == incident.type
            and incident.type in ESCALATABLE_TYPES
        ):
            here = True
    if incident.type not in ESCALATABLE_TYPES:
        here = False  # constraint 11: belt for the braces above
    assert not here or incident.type in ESCALATABLE_TYPES, (
        f"@here escalation computed for non-escalatable type {incident.type} — constraint 11"
    )
    channel = AlertChannel.ALERTS if (role_ids or here) else AlertChannel.LIVE
    return RoutingDecision(role_ids=tuple(role_ids), here=here, channel=channel)


def apply_group_alias(
    decision: RoutingDecision,
    group_alias: str | None,
    rules: Sequence[RoutingRule],
    alias_roles: dict[str, int],
) -> RoutingDecision:
    """Apply a spoken group alias to a rule-derived decision — GDD §6.2.

    - ``"all_hands"`` → ``@here`` plus every subscribed role, regardless of
      severity (explicit human targeting overrides the escalation table).
    - A key of ``alias_roles`` (``"miners"``, ``"defense"``) → restrict the
      mention to that single role.
    - ``None`` or an unknown alias → decision unchanged.
    """
    if group_alias is None:
        return decision
    if group_alias == "all_hands":
        all_roles: list[int] = []
        for rule in rules:
            if rule.role_id not in all_roles:
                all_roles.append(rule.role_id)
        return RoutingDecision(role_ids=tuple(all_roles), here=True, channel=AlertChannel.ALERTS)
    role_id = alias_roles.get(group_alias)
    if role_id is None:
        log.warning("group_alias_unmapped", alias=group_alias)
        return decision
    return RoutingDecision(role_ids=(role_id,), here=decision.here, channel=AlertChannel.ALERTS)


def suppress(decision: RoutingDecision) -> RoutingDecision:
    """Strip every mention from a decision (discipline suppression — GDD §11.1).

    The incident still gets posted, to ``#intel-live`` only — the engine keeps
    logging while the circuit breaker is open or a cooldown is running.
    """
    return RoutingDecision(role_ids=(), here=False, channel=AlertChannel.LIVE)


# ── rule loading ─────────────────────────────────────────────────────────────


def _read_yaml(path: str | Path) -> Any:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise RoutingConfigError(f"cannot read routing rules {p}: {exc}") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RoutingConfigError(f"invalid YAML in {p}: {exc}") from exc


def _rule_entries(data: Any, path: str | Path) -> list[dict[str, Any]]:
    """routing.yaml is either a bare list of rules or a mapping with ``rules:``

    (the mapping form also carries the optional ``group_aliases:`` table).
    """
    if isinstance(data, dict):
        data = data.get("rules", [])
    if data is None:
        return []
    if not isinstance(data, list):
        raise RoutingConfigError(f"{path}: expected a list of rules at the top level")
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise RoutingConfigError(f"{path}: rule[{i}] must be a mapping")
    return data


def _parse_types(raw: Any, where: str) -> frozenset[Intent]:
    if not isinstance(raw, list) or not raw:
        raise RoutingConfigError(f"{where}.types: expected a non-empty list")
    types: set[Intent] = set()
    for item in raw:
        try:
            types.add(Intent(str(item)))
        except ValueError as exc:
            raise RoutingConfigError(f"{where}.types: unknown incident type {item!r}") from exc
    return frozenset(types)


def _parse_scope(raw: Any, where: str, gazetteer: Gazetteer) -> RuleScope:
    if raw is None:
        return RuleScope()
    if not isinstance(raw, dict):
        raise RoutingConfigError(f"{where}.scope: expected a mapping")
    systems: list[int] = []
    for name in raw.get("systems") or []:
        entry = gazetteer.by_name(str(name))
        if entry is None:
            log.warning("routing_unknown_system", rule=where, system=str(name))
            continue
        systems.append(entry.id)
    regions = tuple(str(r) for r in (raw.get("regions") or []))
    within: tuple[int, int] | None = None
    wjo = raw.get("within_jumps_of")
    if wjo is not None:
        if not isinstance(wjo, dict) or "system" not in wjo or "jumps" not in wjo:
            raise RoutingConfigError(
                f"{where}.scope.within_jumps_of: expected {{system, jumps}} mapping"
            )
        anchor = gazetteer.by_name(str(wjo["system"]))
        jumps = wjo["jumps"]
        if not isinstance(jumps, int) or isinstance(jumps, bool) or jumps < 0:
            raise RoutingConfigError(f"{where}.scope.within_jumps_of.jumps: expected int >= 0")
        if anchor is None:
            log.warning("routing_unknown_system", rule=where, system=str(wjo["system"]))
        else:
            within = (anchor.id, jumps)
    return RuleScope(systems=tuple(systems), regions=regions, within_jumps_of=within)


def _parse_escalate(raw: Any, where: str) -> Intent | None:
    if raw is None or raw == "never":
        return None
    try:
        intent = Intent(str(raw))
    except ValueError as exc:
        raise RoutingConfigError(f"{where}.escalate_at: unknown incident type {raw!r}") from exc
    if intent not in ESCALATABLE_TYPES:
        # Constraint 11: only UNDER_ATTACK / ASSIST_REQUEST may escalate.
        log.warning("routing_escalate_clamped", rule=where, escalate_at=str(intent))
        return None
    return intent


def _parse_quiet_hours(raw: Any, where: str) -> QuietHours | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RoutingConfigError(f"{where}.quiet_hours: expected a mapping or null")
    tz = str(raw.get("tz", "UTC"))
    start = raw.get("from", raw.get("start"))
    end = raw.get("to", raw.get("end"))
    if start is None or end is None:
        raise RoutingConfigError(f"{where}.quiet_hours: needs from/to (or start/end) times")
    _parse_hhmm(str(start), f"{where}.quiet_hours.from")
    _parse_hhmm(str(end), f"{where}.quiet_hours.to")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RoutingConfigError(f"{where}.quiet_hours.tz: unknown timezone {tz!r}") from exc
    return QuietHours(tz=tz, start=str(start), end=str(end))


def load_rules(
    path: str | Path,
    gazetteer: Gazetteer,
    resolve_role: Callable[[str], int | None],
) -> list[RoutingRule]:
    """Load and validate ``routing.yaml`` — GDD §10.1.

    ``resolve_role`` maps a role name as written in the file (``"@Miners"``)
    to a Discord role id; rules whose role cannot be resolved are skipped
    with a warning (the guild may not have the role yet). Structural errors
    raise :class:`RoutingConfigError`. Blocking file I/O — call via
    ``asyncio.to_thread`` from the event loop.
    """
    entries = _rule_entries(_read_yaml(path), path)
    rules: list[RoutingRule] = []
    for i, entry in enumerate(entries):
        where = f"{path}: rule[{i}]"
        role_name = entry.get("role")
        if not isinstance(role_name, str) or not role_name:
            raise RoutingConfigError(f"{where}.role: expected a role name string")
        role_id = resolve_role(role_name)
        if role_id is None:
            log.warning("routing_unresolved_role", rule=where, role=role_name)
            continue
        rules.append(
            RoutingRule(
                role_id=role_id,
                types=_parse_types(entry.get("types"), where),
                scope=_parse_scope(entry.get("scope"), where, gazetteer),
                escalate_at=_parse_escalate(entry.get("escalate_at"), where),
                quiet_hours=_parse_quiet_hours(entry.get("quiet_hours"), where),
            )
        )
    log.info("routing_rules_loaded", path=str(path), count=len(rules))
    return rules


def load_group_aliases(
    path: str | Path,
    resolve_role: Callable[[str], int | None],
) -> dict[str, int]:
    """Load the optional ``group_aliases:`` table from ``routing.yaml``.

    Mapping form only (``group_aliases: {miners: "@Miners", defense:
    "@Home-Defense"}``); a bare-list rules file yields an empty mapping and
    spoken "<alias> only" suffixes are then ignored by the engine.
    """
    data = _read_yaml(path)
    if not isinstance(data, dict):
        return {}
    raw = data.get("group_aliases")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RoutingConfigError(f"{path}: group_aliases must be a mapping")
    aliases: dict[str, int] = {}
    for alias, role_name in raw.items():
        role_id = resolve_role(str(role_name))
        if role_id is None:
            log.warning("routing_unresolved_role", alias=str(alias), role=str(role_name))
            continue
        aliases[str(alias)] = role_id
    return aliases
