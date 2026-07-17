"""Pure-helper tests for aura.dsc.views: custom_id round-trips, payload building.

No Discord objects, no network, no event loop — only the parse/build/layout
functions the persistent-view machinery (GDD §9.3) is built on.
"""

from __future__ import annotations

import pytest

from aura.dsc.views import (
    ComponentAction,
    incident_button_rows,
    incident_custom_id,
    parse_custom_id,
    subscription_buttons,
    subscription_custom_id,
)
from aura.types import ButtonSpec, ResponderState

# ── custom_id building ────────────────────────────────────────────────────────


def test_incident_custom_id_respond_actions() -> None:
    assert incident_custom_id(7, "otw") == "aura:inc:7:otw"
    assert incident_custom_id(7, "watch") == "aura:inc:7:watch"
    assert incident_custom_id(7, "no") == "aura:inc:7:no"


def test_incident_custom_id_fix_and_pick() -> None:
    assert incident_custom_id(42, "fix") == "aura:inc:42:fix"
    assert incident_custom_id(42, "pick", system_id=30003397) == "aura:inc:42:pick:30003397"


def test_incident_custom_id_pick_requires_system() -> None:
    with pytest.raises(ValueError):
        incident_custom_id(1, "pick")


def test_incident_custom_id_rejects_unknown_action() -> None:
    with pytest.raises(ValueError):
        incident_custom_id(1, "explode")


def test_subscription_custom_id() -> None:
    assert subscription_custom_id(123456789012345678) == "aura:sub:123456789012345678"


# ── round trips ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("action", "state"),
    [
        ("otw", ResponderState.OTW),
        ("watch", ResponderState.WATCHING),
        ("no", ResponderState.NO),
    ],
)
def test_respond_round_trip(action: str, state: ResponderState) -> None:
    parsed = parse_custom_id(incident_custom_id(99, action))
    assert parsed == ComponentAction(kind="respond", incident_id=99, state=state)


def test_pick_round_trip() -> None:
    parsed = parse_custom_id(incident_custom_id(5, "pick", system_id=30001234))
    assert parsed == ComponentAction(kind="pick", incident_id=5, system_id=30001234)


def test_fix_round_trip() -> None:
    parsed = parse_custom_id(incident_custom_id(5, "fix"))
    assert parsed == ComponentAction(kind="fix", incident_id=5)


def test_subscription_round_trip() -> None:
    parsed = parse_custom_id(subscription_custom_id(777))
    assert parsed == ComponentAction(kind="sub", role_id=777)


def test_round_trip_matches_engine_rendered_ids() -> None:
    """The engine renders these exact shapes in render_card — keep in lockstep."""
    for custom_id, expected in [
        ("aura:inc:12:otw", ComponentAction(kind="respond", incident_id=12, state=ResponderState.OTW)),
        ("aura:inc:12:pick:30002187", ComponentAction(kind="pick", incident_id=12, system_id=30002187)),
        ("aura:inc:12:fix", ComponentAction(kind="fix", incident_id=12)),
    ]:
        assert parse_custom_id(custom_id) == expected


# ── rejection of foreign / malformed ids ─────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "aura",
        "aura:inc",
        "aura:inc:12",
        "aura:inc:12:",
        "aura:inc:12:maybe",
        "aura:inc:abc:otw",
        "aura:inc:12:pick",
        "aura:inc:12:pick:",
        "aura:inc:12:pick:xyz",
        "aura:inc:12:otw:extra",
        "aura:sub:",
        "aura:sub:notanumber",
        "aura:fix:12:30001234",  # the pre-contract shape — must NOT parse
        "other:inc:12:otw",
        "AURA:INC:12:OTW",  # scheme is case-sensitive
    ],
)
def test_parse_rejects_malformed(bad: str) -> None:
    assert parse_custom_id(bad) is None


# ── row layout ────────────────────────────────────────────────────────────────


def _spec(custom_id: str, label: str = "x") -> ButtonSpec:
    return ButtonSpec(custom_id=custom_id, label=label)


def test_rows_plain_card_auto_flow() -> None:
    specs = [_spec("aura:inc:1:otw"), _spec("aura:inc:1:watch"), _spec("aura:inc:1:no")]
    assert incident_button_rows(specs) == (None, None, None)


def test_rows_uncertain_card_splits_pick_and_respond() -> None:
    specs = [
        _spec("aura:inc:1:pick:100"),
        _spec("aura:inc:1:pick:200"),
        _spec("aura:inc:1:pick:300"),
        _spec("aura:inc:1:fix"),
        _spec("aura:inc:1:otw"),
        _spec("aura:inc:1:watch"),
        _spec("aura:inc:1:no"),
    ]
    assert incident_button_rows(specs) == (0, 0, 0, 0, 1, 1, 1)


def test_rows_never_overflow_discord_row_width() -> None:
    specs = [
        _spec("aura:inc:1:pick:100"),
        _spec("aura:inc:1:pick:200"),
        _spec("aura:inc:1:pick:300"),
        _spec("aura:inc:1:fix"),
        _spec("aura:inc:1:otw"),
        _spec("aura:inc:1:watch"),
        _spec("aura:inc:1:no"),
    ]
    rows = incident_button_rows(specs)
    for row in set(rows):
        assert sum(1 for r in rows if r == row) <= 5


# ── subscription payload building ────────────────────────────────────────────


def test_subscription_buttons_reflect_membership() -> None:
    roles = [(111, "Home-Defense"), (222, "Miners"), (333, "Roam-Crew")]
    specs = subscription_buttons(roles, member_role_ids={222})

    assert [s.custom_id for s in specs] == ["aura:sub:111", "aura:sub:222", "aura:sub:333"]
    assert [s.label for s in specs] == ["Home-Defense", "Miners", "Roam-Crew"]

    by_id = {s.custom_id: s for s in specs}
    assert by_id["aura:sub:222"].style == "success"
    assert by_id["aura:sub:222"].emoji == "🔔"
    assert by_id["aura:sub:111"].style == "secondary"
    assert by_id["aura:sub:111"].emoji is None
    assert by_id["aura:sub:333"].style == "secondary"


def test_subscription_buttons_parse_back() -> None:
    specs = subscription_buttons([(42, "Miners")], member_role_ids=set())
    parsed = parse_custom_id(specs[0].custom_id)
    assert parsed == ComponentAction(kind="sub", role_id=42)


def test_subscription_buttons_empty_roles() -> None:
    assert subscription_buttons([], member_role_ids={1, 2}) == ()


def test_subscription_buttons_capped_at_25() -> None:
    roles = [(i, f"Role-{i}") for i in range(40)]
    specs = subscription_buttons(roles, member_role_ids=set())
    assert len(specs) == 25
    # Deterministic: the first 25 in rule order, not an arbitrary subset.
    assert specs[0].custom_id == "aura:sub:0"
    assert specs[-1].custom_id == "aura:sub:24"
