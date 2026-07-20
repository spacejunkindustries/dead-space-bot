"""Pins the pure layout/colour helpers of :mod:`killboard.cards`, plus the
disabled / missing-icon degrade-to-``None`` contract of :class:`CardRenderer`.

No network, no Discord, no real config object — the renderer's seams are faked:
its ``cfg_provider`` returns a tiny namespace, its ``session_provider`` returns
an in-memory stub aiohttp session, and ``to_thread`` is the real
``asyncio.to_thread``. The icon cache is a pytest ``tmp_path``. Pillow rendering
is exercised only when Pillow is importable (guarded by ``_PIL_OK``); the pure
arithmetic and colour/geometry helpers run unconditionally.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from killboard import cards
from killboard.cards import (
    _COLOR_ASSIST,
    _COLOR_DEATH,
    _COLOR_KILL,
    CardRenderer,
    GearItem,
    damage_shares,
    grid_positions,
    icon_cache_path,
    parse_equipment,
    relation_color,
    split_enchant,
)
from killboard.model import Participant

# ── fakes for the CardRenderer seams ─────────────────────────────────────────


def _make_cfg(cache_dir: Path | str, *, enabled: bool = True) -> SimpleNamespace:
    """A minimal stand-in for the live config: only ``.killboard.cards`` is read."""
    cards_cfg = SimpleNamespace(
        enabled=enabled,
        icon_cache_dir=str(cache_dir),
        render_base="https://render.albiononline.com/v1",
    )
    return SimpleNamespace(killboard=SimpleNamespace(cards=cards_cfg))


class _FakeResponse:
    def __init__(self, status: int, data: bytes) -> None:
        self.status = status
        self._data = data

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def read(self) -> bytes:
        return self._data


class _FakeSession:
    """Records every URL fetched and returns a fixed status/body."""

    def __init__(self, status: int = 200, data: bytes = b"") -> None:
        self.status = status
        self.data = data
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs: object) -> _FakeResponse:
        # **_kwargs absorbs the per-request timeout the renderer now passes.
        self.calls.append(url)
        return _FakeResponse(self.status, self.data)


def _tiny_png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (4, 4), (10, 20, 30, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _event_with_gear() -> dict[str, Any]:
    return {
        "relation": "KILL",
        "killer_name": "Alice",
        "victim_name": "Bob",
        "killer_ip": 1234.6,
        "victim_ip": 900.0,
        "total_fame": 1_204_880,
        "location": "Camp",
        "timestamp": "2026-07-20T12:00:00+00:00",
        "raw_json": {
            "Victim": {
                "Equipment": {
                    "MainHand": {"Type": "T4_MAIN_SWORD@2", "Quality": 3},
                    "Head": {"Type": "T4_HEAD_PLATE_SET1"},
                    "OffHand": None,
                }
            },
            "Killer": {"Equipment": {"MainHand": {"Type": "T5_MAIN_AXE"}}},
        },
    }


# ── relation_color ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("relation", "expected"),
    [
        ("KILL", _COLOR_KILL),
        ("kill", _COLOR_KILL),
        ("  Kill  ", _COLOR_KILL),
        ("DEATH", _COLOR_DEATH),
        ("death", _COLOR_DEATH),
        ("ASSIST", _COLOR_ASSIST),
        ("weird", _COLOR_ASSIST),
        ("", _COLOR_ASSIST),
        (None, _COLOR_ASSIST),
    ],
)
def test_relation_color(relation: str | None, expected: tuple[int, int, int]) -> None:
    assert relation_color(relation) == expected


# ── split_enchant ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("item_type", "expected"),
    [
        ("T4_MAIN_SWORD", ("T4_MAIN_SWORD", 0)),
        ("T4_MAIN_SWORD@1", ("T4_MAIN_SWORD", 1)),
        ("T4_MAIN_SWORD@3", ("T4_MAIN_SWORD", 3)),
        ("T4_MAIN_SWORD@0", ("T4_MAIN_SWORD", 0)),
        ("T4_MAIN_SWORD@4", ("T4_MAIN_SWORD", 0)),
        ("T4_MAIN_SWORD@x", ("T4_MAIN_SWORD", 0)),
    ],
)
def test_split_enchant(item_type: str, expected: tuple[str, int]) -> None:
    assert split_enchant(item_type) == expected


def test_gearitem_token() -> None:
    assert GearItem("weapon", "T4_MAIN_SWORD", 2, 3).token == "T4_MAIN_SWORD@2"
    assert GearItem("bag", "T4_BAG", 0, 0).token == "T4_BAG"


# ── parse_equipment ──────────────────────────────────────────────────────────


def test_parse_equipment_order_and_enchant() -> None:
    raw = _event_with_gear()["raw_json"]
    items = parse_equipment(raw, "Victim")
    # Display order follows GEAR_SLOTS: MainHand before Head; OffHand (null) skipped.
    assert [g.slot for g in items] == ["weapon", "head"]
    main = items[0]
    assert main.item_type == "T4_MAIN_SWORD"
    assert main.enchant == 2
    assert main.quality == 3
    assert items[1].item_type == "T4_HEAD_PLATE_SET1"
    assert items[1].enchant == 0


@pytest.mark.parametrize(
    "raw",
    [
        {},  # no player
        {"Victim": None},  # null player
        {"Victim": {}},  # no Equipment
        {"Victim": {"Equipment": None}},  # null Equipment
        {"Victim": {"Equipment": {"MainHand": None}}},  # null slot
        {"Victim": {"Equipment": {"MainHand": {"Quality": 2}}}},  # slot with no Type
        {"Victim": {"Equipment": {"MainHand": {"Type": ""}}}},  # empty Type
    ],
)
def test_parse_equipment_tolerant(raw: dict[str, Any]) -> None:
    assert parse_equipment(raw, "Victim") == []


def test_parse_equipment_bad_quality_defaults_zero() -> None:
    raw = {"Victim": {"Equipment": {"MainHand": {"Type": "T4_X", "Quality": "junk"}}}}
    items = parse_equipment(raw, "Victim")
    assert len(items) == 1
    assert items[0].quality == 0


# ── damage_shares ────────────────────────────────────────────────────────────


def _p(name: str | None, dmg: float) -> Participant:
    return Participant(
        player_id=None, player_name=name, guild_id=None, damage_done=dmg, healing_done=0.0
    )


def test_damage_shares_sorted_and_normalised() -> None:
    parts = [_p("A", 100.0), _p("B", 300.0), _p("C", 100.0)]
    shares = damage_shares(parts, top_n=5)
    assert [s.name for s in shares] == ["B", "A", "C"]
    assert shares[0].fraction == pytest.approx(0.6)
    assert shares[1].fraction == pytest.approx(0.2)
    assert sum(s.fraction for s in shares) == pytest.approx(1.0)


def test_damage_shares_caps_top_n_but_normalises_over_full_total() -> None:
    parts = [_p("A", 50.0), _p("B", 30.0), _p("C", 20.0)]
    shares = damage_shares(parts, top_n=2)
    assert [s.name for s in shares] == ["A", "B"]
    # Fractions are over the total of ALL participants (100), not just the shown 2.
    assert shares[0].fraction == pytest.approx(0.5)
    assert shares[1].fraction == pytest.approx(0.3)


def test_damage_shares_zero_damage_all_zero() -> None:
    shares = damage_shares([_p("A", 0.0), _p("B", 0.0)], top_n=5)
    assert all(s.fraction == 0.0 for s in shares)


def test_damage_shares_missing_name_becomes_question_mark() -> None:
    shares = damage_shares([_p(None, 10.0)], top_n=5)
    assert shares[0].name == "?"


def test_damage_shares_nonpositive_top_n_is_empty() -> None:
    assert damage_shares([_p("A", 10.0)], top_n=0) == []
    assert damage_shares([_p("A", 10.0)], top_n=-3) == []


# ── grid_positions ───────────────────────────────────────────────────────────


def test_grid_positions_layout() -> None:
    pos = grid_positions(count=3, cols=2, cell=10, origin=(0, 0), pad=2)
    assert pos == [(0, 0), (12, 0), (0, 12)]


def test_grid_positions_origin_offset() -> None:
    pos = grid_positions(count=2, cols=5, cell=10, origin=(100, 50), pad=2)
    assert pos == [(100, 50), (112, 50)]


def test_grid_positions_cols_clamped_to_one() -> None:
    pos = grid_positions(count=2, cols=0, cell=10, origin=(0, 0), pad=0)
    assert pos == [(0, 0), (0, 10)]


def test_grid_positions_nonpositive_count_empty() -> None:
    assert grid_positions(count=0, cols=3, cell=10, origin=(0, 0)) == []
    assert grid_positions(count=-5, cols=3, cell=10, origin=(0, 0)) == []


# ── icon_cache_path ──────────────────────────────────────────────────────────


def test_icon_cache_path_basic() -> None:
    p = icon_cache_path("/cache", "T4_MAIN_SWORD@2")
    assert p == Path("/cache/T4_MAIN_SWORD@2.png")


def test_icon_cache_path_sanitises_separators() -> None:
    p = icon_cache_path("/cache", "evil/../x")
    assert "/" not in p.name
    assert p.parent == Path("/cache")
    assert p.name.endswith(".png")


def test_icon_cache_path_empty_token() -> None:
    assert icon_cache_path("/cache", "").name == "unknown.png"


# ── CardRenderer degrade paths ───────────────────────────────────────────────


async def test_render_disabled_returns_none(tmp_path: Path) -> None:
    renderer = CardRenderer(
        cfg_provider=lambda: _make_cfg(tmp_path, enabled=False),
        to_thread=asyncio.to_thread,
        session_provider=lambda: _FakeSession(),
    )
    assert await renderer.render({"relation": "KILL"}, []) is None


async def test_render_reads_cfg_each_call(tmp_path: Path) -> None:
    """A hot reload of ``cards.enabled`` applies to the next card (cfg re-read)."""
    state = {"enabled": False}
    renderer = CardRenderer(
        cfg_provider=lambda: _make_cfg(tmp_path, enabled=state["enabled"]),
        to_thread=asyncio.to_thread,
        session_provider=lambda: _FakeSession(status=404),
    )
    assert await renderer.render(_event_with_gear(), []) is None
    if not cards._PIL_OK:
        pytest.skip("Pillow unavailable; enabled path cannot render")
    state["enabled"] = True
    out = await renderer.render(_event_with_gear(), [])
    assert isinstance(out, bytes) and out[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.skipif(not cards._PIL_OK, reason="Pillow not importable")
async def test_render_missing_icons_still_returns_png(tmp_path: Path) -> None:
    """Every icon fetch 404s, yet the card still composites (header/fame/gear)."""
    session = _FakeSession(status=404)
    renderer = CardRenderer(
        cfg_provider=lambda: _make_cfg(tmp_path),
        to_thread=asyncio.to_thread,
        session_provider=lambda: session,
    )
    out = await renderer.render(_event_with_gear(), [_p("A", 100.0)])
    assert isinstance(out, bytes)
    assert out[:8] == b"\x89PNG\r\n\x1a\n"
    assert session.calls  # it did attempt to fetch the gear icons


@pytest.mark.skipif(not cards._PIL_OK, reason="Pillow not importable")
async def test_render_caches_icons_on_disk(tmp_path: Path) -> None:
    """A 200 icon is fetched once, written to the cache dir, and reused next time."""
    session = _FakeSession(status=200, data=_tiny_png())
    renderer = CardRenderer(
        cfg_provider=lambda: _make_cfg(tmp_path),
        to_thread=asyncio.to_thread,
        session_provider=lambda: session,
    )
    first = await renderer.render(_event_with_gear(), [])
    assert isinstance(first, bytes)
    cached = icon_cache_path(tmp_path, "T4_MAIN_SWORD@2")
    assert cached.exists()
    fetches_after_first = len(session.calls)

    second = await renderer.render(_event_with_gear(), [])
    assert isinstance(second, bytes)
    # No new network fetches: everything served from the on-disk cache.
    assert len(session.calls) == fetches_after_first


async def test_close_is_idempotent_without_own_session(tmp_path: Path) -> None:
    renderer = CardRenderer(
        cfg_provider=lambda: _make_cfg(tmp_path),
        to_thread=asyncio.to_thread,
        session_provider=lambda: _FakeSession(),
    )
    await renderer.close()
    await renderer.close()
