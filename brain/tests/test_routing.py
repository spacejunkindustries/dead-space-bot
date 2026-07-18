"""Routing tests — GDD §10/§11. `evaluate` and `decide_mentions` are pure:
incident × rules × now × gazetteer × config in, one decision out."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from cortana.core.routing import (
    ESCALATABLE_TYPES,
    MentionDecision,
    PersonalPing,
    QuietHours,
    RoutingConfigError,
    RoutingRule,
    RuleScope,
    decide_mentions,
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
    Severity,
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


# ── decide_mentions: the single escalation authority ─────────────────────────


def decide(
    intent: Intent | None,
    *,
    severity: Severity | None = None,
    system_id: int | None = 1,
    rules: list[RoutingRule] | None = None,
    personal: tuple[PersonalPing, ...] | list[PersonalPing] = (),
    group_alias: str | None = None,
    alias_roles: dict[str, int] | None = None,
    here_on_severity: tuple[str, ...] = ("high",),
    mentions_enabled: bool = True,
    caller_may_mention: bool = True,
    now: datetime = NOW,
    gazetteer: FakeGazetteer | None = None,
) -> MentionDecision:
    """One call into the authority with the same defaults the config ships."""
    gaz = gazetteer or FakeGazetteer(
        entries={
            1: sys_entry(1, "Otanuomi", "Kisogo-region"),
            2: sys_entry(2, "Kisogo", "Kisogo-region"),
            5: sys_entry(5, "Alenia", "Lowsec-North"),
            9: sys_entry(9, "Hulmate", "Far-Region"),
        },
        jumps_map={(1, 2): 1, (1, 5): 7, (1, 9): 4},
        home=1,
    )
    use_rules = rules if rules is not None else [hd_rule(), MINERS_RULE, ROAM_RULE]
    incident = make_incident(intent, system_id) if intent is not None else None
    sev = (
        severity
        if severity is not None
        else (INTENT_SEVERITY[intent] if intent is not None else Severity.NONE)
    )
    return decide_mentions(
        intent=intent,
        severity=sev,
        now=now,
        rules=use_rules,
        incident=incident,
        gazetteer=gaz,  # type: ignore[arg-type]
        personal=personal,
        group_alias=group_alias,
        alias_roles=alias_roles if alias_roles is not None else {"miners": MINERS_ROLE},
        here_on_severity=here_on_severity,
        mentions_enabled=mentions_enabled,
        caller_may_mention=caller_may_mention,
    )


# The two structural impossibilities (constraint 11 + GDD §11.2), asserted
# over the full input cross product: every intent (plus the relay pseudo-
# intent None) × every severity × every here_on_severity config × group
# aliases × the pilot gate × mentions_enabled × quiet-hours state.
_ALL_INTENTS: tuple[Intent | None, ...] = (None, *Intent)
_HOS_CONFIGS: tuple[tuple[str, ...], ...] = (
    (),
    ("high",),
    ("high", "medium"),
    ("high", "medium", "none"),
)
_ALIASES: tuple[str | None, ...] = (None, "miners", "all_hands", "ninjas")
_QUIET_NOW = datetime(2026, 7, 17, 5, 0, tzinfo=UTC)  # inside ROAM_RULE quiet hours


def test_structurally_impossible_escalations_exhaustive() -> None:
    """@here outside UNDER_ATTACK/ASSIST_REQUEST and @here in #intel-live are
    impossible for EVERY input combination — not guarded per-path, impossible."""
    for intent, severity, hos, alias, gate, enabled, now in itertools.product(
        _ALL_INTENTS,
        tuple(Severity),
        _HOS_CONFIGS,
        _ALIASES,
        (True, False),
        (True, False),
        (NOW, _QUIET_NOW),
    ):
        decision = decide(
            intent,
            severity=severity,
            group_alias=alias,
            here_on_severity=hos,
            caller_may_mention=gate,
            mentions_enabled=enabled,
            now=now,
        )
        ctx = f"{intent=} {severity=} {hos=} {alias=} {gate=} {enabled=} {now=}"
        # Impossibility 1 — constraint 11: @here strictly for the two intents.
        if decision.here:
            assert intent in ESCALATABLE_TYPES, ctx
        # Impossibility 2 — GDD §11.2: @here (any mention) never in #intel-live.
        if decision.wants_mentions:
            assert decision.channel is AlertChannel.ALERTS, ctx
        else:
            assert decision.channel is AlertChannel.LIVE, ctx
        # The pilot gate and silent mode always strip everything.
        if not gate or not enabled:
            assert not decision.wants_mentions, ctx


def test_here_on_severity_fires_only_for_the_two_intents() -> None:
    """Severity codes request @here; only UNDER_ATTACK/ASSIST_REQUEST get it."""
    for intent in Intent:
        decision = decide(intent, severity=Severity.HIGH, here_on_severity=("high",))
        assert decision.here is (intent in ESCALATABLE_TYPES), intent


def test_here_on_severity_without_rules_never_fires_into_live() -> None:
    """THE regression: a CODE RED matching zero routing rules used to get
    here=True with the channel still #intel-live. The recomputed channel makes
    that impossible — @here always lands in #intel-alerts."""
    decision = decide(Intent.UNDER_ATTACK, severity=Severity.HIGH, rules=[])
    assert decision.here is True
    assert decision.role_ids == ()
    assert decision.channel is AlertChannel.ALERTS


def test_severity_code_on_relay_never_here() -> None:
    """A freeform relay (intent None) can never @here, whatever was spoken."""
    for severity in Severity:
        decision = decide(None, severity=severity, here_on_severity=("high", "medium", "none"))
        assert decision.here is False


def test_non_pilot_code_red_gets_nothing() -> None:
    """The broadcast-bypass class: a non-Pilot 'code red' must never mention."""
    decision = decide(Intent.UNDER_ATTACK, severity=Severity.HIGH, caller_may_mention=False)
    assert decision == MentionDecision(
        role_ids=(), here=False, channel=AlertChannel.LIVE, user_ids=()
    )


def test_quiet_hours_apply_inside_decide_mentions() -> None:
    decision = decide(
        Intent.HOSTILE_SPOTTED, system_id=5, rules=[ROAM_RULE], here_on_severity=(), now=_QUIET_NOW
    )
    assert decision.role_ids == ()
    assert decision.channel is AlertChannel.LIVE


def test_personal_pings_returned_as_explicit_allowlist() -> None:
    decision = decide(
        Intent.GATE_CAMP,
        rules=[],
        here_on_severity=(),
        personal=[ping(100, Intent.GATE_CAMP), ping(101, Intent.GATE_CAMP)],
    )
    assert decision.user_ids == (100, 101)
    assert decision.here is False
    assert decision.channel is AlertChannel.ALERTS


def test_mention_decision_suppressed_strips_everything() -> None:
    decision = MentionDecision(
        role_ids=(HD_ROLE,), here=True, channel=AlertChannel.ALERTS, user_ids=(100,)
    )
    assert decision.wants_mentions
    stripped = decision.suppressed()
    assert stripped == MentionDecision(
        role_ids=(), here=False, channel=AlertChannel.LIVE, user_ids=()
    )
    assert not stripped.wants_mentions


# ── group aliases (GDD §6.2) — routed through the same clamp ─────────────────


def test_all_hands_on_escalatable_type_gets_roles_and_here() -> None:
    decision = decide(Intent.UNDER_ATTACK, group_alias="all_hands", here_on_severity=())
    assert set(decision.role_ids) == {HD_ROLE, MINERS_ROLE, ROAM_ROLE}
    assert decision.here is True
    assert decision.channel is AlertChannel.ALERTS


def test_all_hands_on_sighting_mentions_roles_but_never_here() -> None:
    """all_hands widens the roles; its @here request passes the constraint-11
    clamp, so a sighting still cannot @here (the unconditional here=True of
    the old apply_group_alias is gone)."""
    decision = decide(Intent.HOSTILE_SPOTTED, group_alias="all_hands", here_on_severity=())
    assert set(decision.role_ids) == {HD_ROLE, MINERS_ROLE, ROAM_ROLE}
    assert decision.here is False
    assert decision.channel is AlertChannel.ALERTS


def test_all_hands_on_relay_mentions_roles_never_here() -> None:
    decision = decide(None, group_alias="all_hands", here_on_severity=())
    assert set(decision.role_ids) == {HD_ROLE, MINERS_ROLE, ROAM_ROLE}
    assert decision.here is False


def test_alias_restricts_to_single_role() -> None:
    decision = decide(Intent.HOSTILE_SPOTTED, group_alias="miners", here_on_severity=())
    assert decision.role_ids == (MINERS_ROLE,)
    assert decision.channel is AlertChannel.ALERTS


def test_unknown_alias_keeps_rule_roles() -> None:
    with_alias = decide(Intent.GATE_CAMP, group_alias="ninjas", here_on_severity=())
    without = decide(Intent.GATE_CAMP, group_alias=None, here_on_severity=())
    assert with_alias == without


def test_group_alias_preserves_personal_pings() -> None:
    personal = [ping(100, Intent.HOSTILE_SPOTTED)]
    restricted = decide(
        Intent.HOSTILE_SPOTTED, group_alias="miners", here_on_severity=(), personal=personal
    )
    assert restricted.user_ids == (100,)
    all_hands = decide(
        Intent.HOSTILE_SPOTTED, group_alias="all_hands", here_on_severity=(), personal=personal
    )
    assert all_hands.user_ids == (100,)


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
