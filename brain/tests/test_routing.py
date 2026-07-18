"""Routing tests — GDD §10/§11. `evaluate` is pure: incident × rules × now × gazetteer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from cortana.core.routing import (
    PersonalPing,
    QuietHours,
    RoutingConfigError,
    RoutingRule,
    RuleScope,
    apply_group_alias,
    evaluate,
    load_group_aliases,
    load_rules,
    suppress,
)
from cortana.types import (
    INTENT_SEVERITY,
    AlertChannel,
    Incident,
    IncidentStatus,
    Intent,
    RoutingDecision,
    SystemEntry,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

NOW = datetime(2026, 7, 17, 18, 0, 0, tzinfo=UTC)
ISO = NOW.isoformat()

HD_ROLE = 801
MINERS_ROLE = 802
ROAM_ROLE = 803

ROLE_IDS = {"@Home-Defense": HD_ROLE, "@Miners": MINERS_ROLE, "@Roam-Crew": ROAM_ROLE}


@dataclass
class FakeGazetteer:
    """Just enough of aura.nlu.gazetteer.Gazetteer for routing: id/name/jumps."""

    entries: dict[int, SystemEntry]
    jumps_map: dict[tuple[int, int], int] = field(default_factory=dict)
    home: int | None = None

    @property
    def systems(self) -> tuple[SystemEntry, ...]:
        return tuple(self.entries.values())

    def by_id(self, system_id: int) -> SystemEntry | None:
        return self.entries.get(system_id)

    def by_name(self, name: str) -> SystemEntry | None:
        for entry in self.entries.values():
            if entry.name.lower() == name.lower():
                return entry
        return None

    def jumps(self, a_id: int, b_id: int) -> int | None:
        if a_id == b_id:
            return 0
        return self.jumps_map.get((a_id, b_id), self.jumps_map.get((b_id, a_id)))

    @property
    def home_system_id(self) -> int | None:
        return self.home


def sys_entry(system_id: int, name: str, region: str) -> SystemEntry:
    return SystemEntry(
        id=system_id, name=name, region=region, constellation=None, metaphone=name.upper()
    )


@pytest.fixture()
def gazetteer() -> FakeGazetteer:
    return FakeGazetteer(
        entries={
            1: sys_entry(1, "Otanuomi", "Kisogo-region"),
            2: sys_entry(2, "Kisogo", "Kisogo-region"),
            5: sys_entry(5, "Alenia", "Lowsec-North"),
            9: sys_entry(9, "Hulmate", "Far-Region"),
        },
        jumps_map={(1, 2): 1, (1, 5): 7, (1, 9): 4},
        home=1,
    )


def make_incident(intent: Intent, system_id: int | None = 1) -> Incident:
    return Incident(
        id=1,
        guild_id=1,
        system_id=system_id,
        system_confidence=0.9,
        type=intent,
        severity=INTENT_SEVERITY[intent],
        reporter_id=42,
        detail=None,
        opened_at=ISO,
        updated_at=ISO,
        status=IncidentStatus.ACTIVE,
        message_id=None,
        channel_id=None,
    )


def hd_rule(**overrides: object) -> RoutingRule:
    kwargs: dict = {
        "role_id": HD_ROLE,
        "types": frozenset({Intent.UNDER_ATTACK, Intent.ASSIST_REQUEST, Intent.HOSTILE_SPOTTED}),
        "scope": RuleScope(regions=("Kisogo-region",), within_jumps_of=(1, 5)),
        "escalate_at": Intent.UNDER_ATTACK,
        "quiet_hours": None,
    }
    kwargs.update(overrides)
    return RoutingRule(**kwargs)


MINERS_RULE = RoutingRule(
    role_id=MINERS_ROLE,
    types=frozenset({Intent.HOSTILE_SPOTTED, Intent.GATE_CAMP}),
    scope=RuleScope(systems=(1, 2)),
    escalate_at=None,
    quiet_hours=None,
)

ROAM_RULE = RoutingRule(
    role_id=ROAM_ROLE,
    types=frozenset({Intent.HOSTILE_SPOTTED}),
    scope=RuleScope(regions=("Lowsec-North",)),
    escalate_at=None,
    quiet_hours=QuietHours(tz="UTC", start="02:00", end="14:00"),
)


# ── role union and scope matching ────────────────────────────────────────────


def test_union_of_matching_roles_mentioned_once(gazetteer: FakeGazetteer) -> None:
    rules = [hd_rule(), MINERS_RULE, hd_rule()]  # duplicate role must not repeat
    decision = evaluate(make_incident(Intent.HOSTILE_SPOTTED, 1), rules, NOW, gazetteer=gazetteer)
    assert decision.role_ids == (HD_ROLE, MINERS_ROLE)
    assert decision.channel is AlertChannel.ALERTS


def test_type_filter_excludes_unsubscribed_roles(gazetteer: FakeGazetteer) -> None:
    rules = [hd_rule(), MINERS_RULE]
    decision = evaluate(make_incident(Intent.UNDER_ATTACK, 1), rules, NOW, gazetteer=gazetteer)
    assert MINERS_ROLE not in decision.role_ids  # miners don't take UNDER_ATTACK


def test_scope_region_match(gazetteer: FakeGazetteer) -> None:
    decision = evaluate(
        make_incident(Intent.HOSTILE_SPOTTED, 5), [ROAM_RULE], NOW, gazetteer=gazetteer
    )
    assert decision.role_ids == (ROAM_ROLE,)


def test_scope_within_jumps_match(gazetteer: FakeGazetteer) -> None:
    # System 9 is in an unlisted region but only 4 jumps from the anchor (max 5).
    decision = evaluate(
        make_incident(Intent.HOSTILE_SPOTTED, 9), [hd_rule()], NOW, gazetteer=gazetteer
    )
    assert decision.role_ids == (HD_ROLE,)


def test_scope_no_match_routes_to_live(gazetteer: FakeGazetteer) -> None:
    # System 5: wrong region for HD, 7 jumps out (> 5), not in miners' systems.
    decision = evaluate(
        make_incident(Intent.GATE_CAMP, 5), [hd_rule(), MINERS_RULE], NOW, gazetteer=gazetteer
    )
    assert decision.role_ids == ()
    assert decision.here is False
    assert decision.channel is AlertChannel.LIVE


def test_unrestricted_scope_matches_everything(gazetteer: FakeGazetteer) -> None:
    rule = hd_rule(scope=RuleScope())
    decision = evaluate(make_incident(Intent.HOSTILE_SPOTTED, 9), [rule], NOW, gazetteer=gazetteer)
    assert decision.role_ids == (HD_ROLE,)


# ── @here escalation discipline (constraint 11) ──────────────────────────────


def test_here_fires_for_under_attack_with_escalating_rule(gazetteer: FakeGazetteer) -> None:
    decision = evaluate(
        make_incident(Intent.UNDER_ATTACK, 1), [hd_rule()], NOW, gazetteer=gazetteer
    )
    assert decision.here is True
    assert decision.channel is AlertChannel.ALERTS


def test_no_here_when_no_rule_escalates_at_that_type(gazetteer: FakeGazetteer) -> None:
    # HD escalates at UNDER_ATTACK only; an ASSIST_REQUEST matches but must not @here.
    decision = evaluate(
        make_incident(Intent.ASSIST_REQUEST, 1), [hd_rule()], NOW, gazetteer=gazetteer
    )
    assert decision.here is False
    assert decision.role_ids == (HD_ROLE,)


def test_sighting_never_here_even_with_misconfigured_escalate_at(
    gazetteer: FakeGazetteer,
) -> None:
    # Bypass load-time clamping entirely: build the broken rule by hand.
    broken = hd_rule(escalate_at=Intent.HOSTILE_SPOTTED)
    decision = evaluate(
        make_incident(Intent.HOSTILE_SPOTTED, 1), [broken], NOW, gazetteer=gazetteer
    )
    assert decision.here is False
    assert decision.role_ids == (HD_ROLE,)  # still mentioned, just never @here


def test_gate_camp_never_here_either(gazetteer: FakeGazetteer) -> None:
    broken = RoutingRule(
        role_id=MINERS_ROLE,
        types=frozenset({Intent.GATE_CAMP}),
        scope=RuleScope(),
        escalate_at=Intent.GATE_CAMP,
        quiet_hours=None,
    )
    decision = evaluate(make_incident(Intent.GATE_CAMP, 1), [broken], NOW, gazetteer=gazetteer)
    assert decision.here is False


# ── quiet hours ──────────────────────────────────────────────────────────────


def test_quiet_hours_suppress_inside_window(gazetteer: FakeGazetteer) -> None:
    inside = datetime(2026, 7, 17, 5, 0, tzinfo=UTC)  # 05:00 ∈ [02:00, 14:00)
    decision = evaluate(
        make_incident(Intent.HOSTILE_SPOTTED, 5), [ROAM_RULE], inside, gazetteer=gazetteer
    )
    assert decision.role_ids == ()
    assert decision.channel is AlertChannel.LIVE


def test_quiet_hours_allow_outside_window(gazetteer: FakeGazetteer) -> None:
    outside = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)
    decision = evaluate(
        make_incident(Intent.HOSTILE_SPOTTED, 5), [ROAM_RULE], outside, gazetteer=gazetteer
    )
    assert decision.role_ids == (ROAM_ROLE,)


def test_quiet_hours_window_spanning_midnight() -> None:
    qh = QuietHours(tz="UTC", start="22:00", end="06:00")
    assert qh.active_at(datetime(2026, 7, 17, 23, 0, tzinfo=UTC))
    assert qh.active_at(datetime(2026, 7, 17, 5, 59, tzinfo=UTC))
    assert not qh.active_at(datetime(2026, 7, 17, 6, 0, tzinfo=UTC))
    assert not qh.active_at(datetime(2026, 7, 17, 12, 0, tzinfo=UTC))
    assert qh.active_at(datetime(2026, 7, 17, 22, 0, tzinfo=UTC))


def test_quiet_hours_are_timezone_aware() -> None:
    qh = QuietHours(tz="UTC", start="02:00", end="14:00")
    # 15:00 at UTC+03:00 is 12:00 UTC — inside the window despite the local hour.
    local = datetime(2026, 7, 17, 15, 0, tzinfo=timezone(timedelta(hours=3)))
    assert qh.active_at(local)
    # 04:00 at UTC+03:00 is 01:00 UTC — outside.
    assert not qh.active_at(datetime(2026, 7, 17, 4, 0, tzinfo=timezone(timedelta(hours=3))))


def test_quiet_hours_equal_bounds_never_active() -> None:
    qh = QuietHours(tz="UTC", start="08:00", end="08:00")
    assert not qh.active_at(datetime(2026, 7, 17, 8, 0, tzinfo=UTC))


# ── group aliases (GDD §6.2) ─────────────────────────────────────────────────


def test_all_hands_forces_here_and_every_subscribed_role(gazetteer: FakeGazetteer) -> None:
    rules = [hd_rule(), MINERS_RULE, ROAM_RULE]
    base = evaluate(make_incident(Intent.HOSTILE_SPOTTED, 9), rules, NOW, gazetteer=gazetteer)
    decision = apply_group_alias(base, "all_hands", rules, {})
    assert set(decision.role_ids) == {HD_ROLE, MINERS_ROLE, ROAM_ROLE}
    assert decision.here is True
    assert decision.channel is AlertChannel.ALERTS


def test_alias_restricts_to_single_role(gazetteer: FakeGazetteer) -> None:
    rules = [hd_rule(), MINERS_RULE]
    base = evaluate(make_incident(Intent.HOSTILE_SPOTTED, 1), rules, NOW, gazetteer=gazetteer)
    assert set(base.role_ids) == {HD_ROLE, MINERS_ROLE}
    decision = apply_group_alias(base, "miners", rules, {"miners": MINERS_ROLE})
    assert decision.role_ids == (MINERS_ROLE,)
    assert decision.channel is AlertChannel.ALERTS


def test_unknown_alias_leaves_decision_unchanged(gazetteer: FakeGazetteer) -> None:
    rules = [MINERS_RULE]
    base = evaluate(make_incident(Intent.GATE_CAMP, 1), rules, NOW, gazetteer=gazetteer)
    assert apply_group_alias(base, "ninjas", rules, {"miners": MINERS_ROLE}) == base
    assert apply_group_alias(base, None, rules, {}) == base


def test_suppress_strips_all_mentions() -> None:
    decision = RoutingDecision(role_ids=(HD_ROLE,), here=True, channel=AlertChannel.ALERTS)
    stripped = suppress(decision)
    assert stripped.role_ids == ()
    assert stripped.here is False
    assert stripped.channel is AlertChannel.LIVE


# ── personal pings (GDD §10.3) ───────────────────────────────────────────────


def ping(user_id: int, *types: Intent, system_id: int | None = None) -> PersonalPing:
    return PersonalPing(user_id=user_id, types=frozenset(types), system_id=system_id)


def test_personal_ping_matches_type_and_system(gazetteer: FakeGazetteer) -> None:
    personal = [
        ping(100, Intent.GATE_CAMP, system_id=1),
        ping(101, Intent.GATE_CAMP),  # all systems
        ping(102, Intent.HOSTILE_SPOTTED, system_id=1),  # wrong type
        ping(103, Intent.GATE_CAMP, system_id=2),  # wrong system
    ]
    decision = evaluate(
        make_incident(Intent.GATE_CAMP, 1), [], NOW, gazetteer=gazetteer, personal=personal
    )
    assert decision.user_ids == (100, 101)
    assert decision.role_ids == ()
    assert decision.here is False
    assert decision.channel is AlertChannel.ALERTS  # a mention is a mention


def test_personal_ping_never_pings_the_reporter(gazetteer: FakeGazetteer) -> None:
    # make_incident's reporter_id is 42.
    personal = [ping(42, Intent.GATE_CAMP), ping(100, Intent.GATE_CAMP)]
    decision = evaluate(
        make_incident(Intent.GATE_CAMP, 1), [], NOW, gazetteer=gazetteer, personal=personal
    )
    assert decision.user_ids == (100,)


def test_personal_ping_deduplicates_user(gazetteer: FakeGazetteer) -> None:
    personal = [ping(100, Intent.GATE_CAMP, system_id=1), ping(100, Intent.GATE_CAMP)]
    decision = evaluate(
        make_incident(Intent.GATE_CAMP, 1), [], NOW, gazetteer=gazetteer, personal=personal
    )
    assert decision.user_ids == (100,)


def test_personal_ping_never_causes_here(gazetteer: FakeGazetteer) -> None:
    """Constraint 11: user subscriptions cannot escalate — even on escalatable
    types, and no roles means no escalation source at all."""
    for intent in (Intent.UNDER_ATTACK, Intent.ASSIST_REQUEST, Intent.GATE_CAMP):
        decision = evaluate(
            make_incident(intent, 1),
            [],
            NOW,
            gazetteer=gazetteer,
            personal=[ping(100, intent)],
        )
        assert decision.here is False
        assert decision.user_ids == (100,)


def test_personal_ping_appended_alongside_roles(gazetteer: FakeGazetteer) -> None:
    decision = evaluate(
        make_incident(Intent.HOSTILE_SPOTTED, 1),
        [MINERS_RULE],
        NOW,
        gazetteer=gazetteer,
        personal=[ping(100, Intent.HOSTILE_SPOTTED)],
    )
    assert decision.role_ids == (MINERS_ROLE,)
    assert decision.user_ids == (100,)
    assert decision.channel is AlertChannel.ALERTS


def test_no_matching_personal_ping_stays_live(gazetteer: FakeGazetteer) -> None:
    decision = evaluate(
        make_incident(Intent.GATE_CAMP, 5),
        [],
        NOW,
        gazetteer=gazetteer,
        personal=[ping(100, Intent.GATE_CAMP, system_id=1)],
    )
    assert decision.user_ids == ()
    assert decision.channel is AlertChannel.LIVE


def test_suppress_strips_personal_pings_too() -> None:
    decision = RoutingDecision(
        role_ids=(HD_ROLE,), here=True, channel=AlertChannel.ALERTS, user_ids=(100, 101)
    )
    assert suppress(decision).user_ids == ()


def test_group_alias_preserves_personal_pings(gazetteer: FakeGazetteer) -> None:
    rules = [hd_rule(), MINERS_RULE]
    base = evaluate(
        make_incident(Intent.HOSTILE_SPOTTED, 1),
        rules,
        NOW,
        gazetteer=gazetteer,
        personal=[ping(100, Intent.HOSTILE_SPOTTED)],
    )
    restricted = apply_group_alias(base, "miners", rules, {"miners": MINERS_ROLE})
    assert restricted.user_ids == (100,)
    all_hands = apply_group_alias(base, "all_hands", rules, {})
    assert all_hands.user_ids == (100,)


# ── rule loading ─────────────────────────────────────────────────────────────


def test_load_rules_from_repo_example(gazetteer: FakeGazetteer) -> None:
    rules = load_rules(REPO_ROOT / "config" / "routing.yaml.example", gazetteer, ROLE_IDS.get)
    assert len(rules) == 3
    hd, miners, roam = rules
    assert hd.role_id == HD_ROLE
    assert hd.escalate_at is Intent.UNDER_ATTACK
    assert hd.scope.regions == ("Kisogo-region",)
    assert hd.scope.within_jumps_of == (1, 5)
    assert miners.escalate_at is None  # "never"
    assert set(miners.scope.systems) == {1, 2}
    assert roam.quiet_hours == QuietHours(tz="UTC", start="02:00", end="14:00")


def test_load_rules_skips_unresolvable_role(tmp_path: Path, gazetteer: FakeGazetteer) -> None:
    path = tmp_path / "routing.yaml"
    path.write_text(
        "- role: '@Ghost-Role'\n  types: [HOSTILE_SPOTTED]\n  scope: {}\n",
        encoding="utf-8",
    )
    assert load_rules(path, gazetteer, lambda _name: None) == []


def test_load_rules_rejects_unknown_type(tmp_path: Path, gazetteer: FakeGazetteer) -> None:
    path = tmp_path / "routing.yaml"
    path.write_text(
        "- role: '@Miners'\n  types: [NOT_A_TYPE]\n  scope: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(RoutingConfigError):
        load_rules(path, gazetteer, ROLE_IDS.get)


def test_load_rules_clamps_non_escalatable_escalate_at(
    tmp_path: Path, gazetteer: FakeGazetteer
) -> None:
    path = tmp_path / "routing.yaml"
    path.write_text(
        "- role: '@Miners'\n"
        "  types: [HOSTILE_SPOTTED]\n"
        "  scope: {}\n"
        "  escalate_at: HOSTILE_SPOTTED\n",
        encoding="utf-8",
    )
    rules = load_rules(path, gazetteer, ROLE_IDS.get)
    assert rules[0].escalate_at is None


def test_load_rules_rejects_bad_quiet_hours(tmp_path: Path, gazetteer: FakeGazetteer) -> None:
    path = tmp_path / "routing.yaml"
    path.write_text(
        "- role: '@Miners'\n"
        "  types: [HOSTILE_SPOTTED]\n"
        "  scope: {}\n"
        "  quiet_hours: { tz: UTC, from: '25:99', to: '06:00' }\n",
        encoding="utf-8",
    )
    with pytest.raises(RoutingConfigError):
        load_rules(path, gazetteer, ROLE_IDS.get)


def test_load_group_aliases_mapping_form(tmp_path: Path, gazetteer: FakeGazetteer) -> None:
    path = tmp_path / "routing.yaml"
    path.write_text(
        "rules:\n"
        "  - role: '@Miners'\n"
        "    types: [GATE_CAMP]\n"
        "    scope: {}\n"
        "group_aliases:\n"
        "  miners: '@Miners'\n"
        "  defense: '@Home-Defense'\n",
        encoding="utf-8",
    )
    assert load_rules(path, gazetteer, ROLE_IDS.get)[0].role_id == MINERS_ROLE
    aliases = load_group_aliases(path, ROLE_IDS.get)
    assert aliases == {"miners": MINERS_ROLE, "defense": HD_ROLE}


def test_load_group_aliases_absent_on_list_form() -> None:
    path = REPO_ROOT / "config" / "routing.yaml.example"
    assert load_group_aliases(path, ROLE_IDS.get) == {}
