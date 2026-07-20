"""Pins the Daily Ranking data path and the Dead-branded card compositors.

Covers the pieces the community-killbot "Daily Ranking" parity added:

* :func:`killboard.rankings.build_daily_ranking` — the two guild-wide fame totals
  plus the Top Kill Fame / Top Death Fame boards, aggregated from the event store.
* :func:`killboard.rankings.daily_ranking_embed` — the always-available embed
  fallback: totals in the header, both boards as fields, period heading.
* :mod:`killboard.cards` branding — ``parse_accent`` tolerance, the compact fame
  formatter, name clipping, and that both compositors return real PNG bytes with
  branding + loot value drawn (no network, synthetic gear).
"""

from __future__ import annotations

import sqlite3

import pytest

from cortana.core import db
from killboard import cards, rankings
from killboard.cards import (
    BrandStyle,
    _clip_name,
    _compose_card,
    _compose_ranking_card,
    _fmt_short,
    parse_accent,
)
from killboard.model import DEATH, KILL, EventRow
from killboard.rankings import DailyRanking, build_daily_ranking, daily_ranking_embed
from killboard.store import MIGRATIONS_DIR, KbStore

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: The compositor tests need Pillow; CI runs the pure-logic suite without it, so
#: cards degrade to embed-only there. Skip image tests when PIL isn't importable
#: (matches tests/test_killboard_cards.py).
_needs_pil = pytest.mark.skipif(not cards._PIL_OK, reason="Pillow not importable")


@pytest.fixture
def store() -> KbStore:
    conn: sqlite3.Connection = db.connect(":memory:")
    db.migrate(conn, MIGRATIONS_DIR)
    return KbStore(conn)


def _event(event_id: int, *, relation: str, total_fame: int, kid: str, vid: str) -> EventRow:
    return EventRow(
        event_id=event_id,
        timestamp="2026-07-20T12:00:00+00:00",
        killer_id=kid,
        killer_name=kid,
        killer_guild_id="G1",
        killer_ip=1000.0,
        victim_id=vid,
        victim_name=vid,
        victim_guild_id="G2",
        victim_ip=900.0,
        total_fame=total_fame,
        relation=relation,
        num_participants=1,
        battle_id=None,
        location="Camp",
    )


# ── build_daily_ranking ───────────────────────────────────────────────────────


def test_build_daily_ranking_totals_and_boards(store: KbStore) -> None:
    """Totals sum each side; the boards rank members by kill fame and death fame."""
    store.upsert_event(_event(1, relation=KILL, total_fame=116340, kid="DjPoppy", vid="X"), "{}")
    store.upsert_event(_event(2, relation=KILL, total_fame=68000, kid="Snapjlr", vid="Y"), "{}")
    store.upsert_event(_event(3, relation=DEATH, total_fame=708860, kid="Z", vid="Snapjlr"), "{}")
    store.upsert_event(_event(4, relation=DEATH, total_fame=6910, kid="W", vid="Cherry"), "{}")

    rk = build_daily_ranking(store, "2026-07-01T00:00:00+00:00", None, limit=10)

    assert rk.total_kill_fame == 116340 + 68000
    assert rk.total_death_fame == 708860 + 6910
    assert rk.top_kill_fame[0] == ("DjPoppy", 116340)
    assert rk.top_death_fame[0] == ("Snapjlr", 708860)
    # Death-only members surface on the death board (they never killed).
    assert ("Cherry", 6910) in rk.top_death_fame


def test_build_daily_ranking_empty_window(store: KbStore) -> None:
    rk = build_daily_ranking(store, "2099-01-01T00:00:00+00:00", None, limit=10)
    assert rk == DailyRanking(0, 0, [], [])


def test_build_daily_ranking_excludes_zero_fame_padding(store: KbStore) -> None:
    """A kill-only member (dfame=0) must not pad the Death Fame board, and a
    death-only member (fame=0) must not pad the Kill Fame board."""
    store.upsert_event(_event(1, relation=KILL, total_fame=5000, kid="Killer", vid="X"), "{}")
    store.upsert_event(_event(2, relation=DEATH, total_fame=9000, kid="Z", vid="Victim"), "{}")

    rk = build_daily_ranking(store, "2026-07-01T00:00:00+00:00", None, limit=10)

    assert [n for n, _ in rk.top_death_fame] == ["Victim"]  # Killer (0) filtered out
    assert [n for n, _ in rk.top_kill_fame] == ["Killer"]  # Victim (0) filtered out
    assert ("Killer", 5000) in rk.top_kill_fame
    assert ("Victim", 9000) in rk.top_death_fame


# ── daily_ranking_embed ───────────────────────────────────────────────────────


def test_daily_ranking_embed_structure() -> None:
    rk = DailyRanking(
        total_kill_fame=210762,
        total_death_fame=1049806,
        top_kill_fame=[("DjPoppy", 116340), ("Snapjlr", 68000)],
        top_death_fame=[("Snapjlr", 708860)],
    )
    embed = daily_ranking_embed(rk, "Jul 19, 2026", heading="Daily Ranking", guild_name="DEAD")

    assert "Daily Ranking" in (embed.title or "")
    assert "210,762" in (embed.description or "")
    assert "1,049,806" in (embed.description or "")
    names = [f.name for f in embed.fields]
    assert any("Kill Fame" in n for n in names)
    assert any("Death Fame" in n for n in names)
    # Compact fame formatting in the board body.
    body = "\n".join(f.value or "" for f in embed.fields)
    assert "116.34k" in body and "68k" in body
    assert int(embed.colour.value) == rankings.DEAD_RED


def test_daily_ranking_embed_empty_boards() -> None:
    embed = daily_ranking_embed(DailyRanking(0, 0, [], []), "Today")
    assert any("No activity" in (f.value or "") for f in embed.fields)


# ── compact fame + name clip ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [(744, "744"), (68000, "68k"), (116340, "116.34k"), (2_500_000, "2.5m"), (0, "0")],
)
def test_fmt_short(value: int, expected: str) -> None:
    assert _fmt_short(value) == expected
    assert rankings._fmt_fame_short(value) == expected


def test_clip_name() -> None:
    assert _clip_name("Short", 16) == "Short"
    assert _clip_name("AVeryLongPlayerName", 16) == "AVeryLongPlayer…"


# ── accent parsing ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("#E11212", (225, 18, 18)),
        ("e11212", (225, 18, 18)),
        ("#00FF00", (0, 255, 0)),
        ("", (225, 18, 18)),  # falls back to Dead red
        (None, (225, 18, 18)),
        ("nothex", (225, 18, 18)),
        ("#12345", (225, 18, 18)),  # wrong length
    ],
)
def test_parse_accent(value: str | None, expected: tuple[int, int, int]) -> None:
    assert parse_accent(value) == expected


# ── compositors produce real PNGs with branding ───────────────────────────────


def _brand() -> BrandStyle:
    return BrandStyle(name="Dead Gaming", accent=(225, 18, 18), logo=None)


@_needs_pil
def test_compose_ranking_card_returns_png() -> None:
    rk = DailyRanking(
        total_kill_fame=210762,
        total_death_fame=1049806,
        top_kill_fame=[("DjPoppy", 116340), ("Snapjlr", 68000)],
        top_death_fame=[("Snapjlr", 708860)],
    )
    png = _compose_ranking_card(rk, "Jul 19, 2026", "Daily Ranking", "DEAD", _brand(), None)
    assert png is not None and png[:8] == _PNG_MAGIC


@_needs_pil
def test_compose_ranking_card_empty_boards_still_renders() -> None:
    png = _compose_ranking_card(
        DailyRanking(0, 0, [], []), "Today", "Daily Ranking", None, _brand()
    )
    assert png is not None and png[:8] == _PNG_MAGIC


def _kill_header(relation: str = "KILL", fame: int = 204880) -> dict:
    return {
        "relation": relation,
        "killer_name": "DjPoppy",
        "victim_name": "Snapjlr",
        "killer_guild": "DEAD Renegadez",
        "victim_guild": "Enemy",
        "killer_ip": 1350,
        "victim_ip": 1290,
        "fame": fame,
        "timestamp": "2026-07-20T09:26:00",
    }


@_needs_pil
def test_compose_card_with_brand_and_loot_value() -> None:
    killer_equip = {"MainHand": {"Type": "T8_2H_HOLYSTAFF@3", "Quality": 3, "Count": 1}}
    victim_equip = {"Head": {"Type": "T6_HEAD_CLOTH_SET1", "Quality": 2, "Count": 1}}
    inventory = [{"Type": "T4_BAG", "Quality": 1, "Count": 3}]
    participants = [{"name": "DjPoppy", "ip": 1350, "damage": 2275, "healing": 0}]
    png = _compose_card(
        _kill_header(), killer_equip, victim_equip, inventory, participants, {}, _brand(), 204880
    )
    assert png is not None and png[:8] == _PNG_MAGIC


@_needs_pil
def test_compose_card_without_brand_or_value_still_renders() -> None:
    png = _compose_card(_kill_header("DEATH", 0), {}, {}, [], [], {})
    assert png is not None and png[:8] == _PNG_MAGIC


@_needs_pil
def test_compositors_render_without_system_dejavu(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both compositors must render on a box with NO system DejaVu font (a minimal
    CI runner): _load_font falls back to Pillow's bundled scalable default, which
    must still support the anchor= draws. Guards the CI-only font regression."""
    from PIL import ImageFont

    from killboard import cards as cards_mod

    real_truetype = ImageFont.truetype

    def _no_dejavu(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(name, str) and "DejaVu" in name:
            raise OSError("simulated: no system DejaVu")
        return real_truetype(name, *args, **kwargs)

    monkeypatch.setattr(cards_mod.ImageFont, "truetype", _no_dejavu)

    rk = DailyRanking(210762, 1049806, [("DjPoppy", 116340)], [("Snapjlr", 708860)])
    ranking_png = _compose_ranking_card(rk, "Jul 19", "Daily Ranking", "DEAD", _brand(), None)
    assert ranking_png is not None and ranking_png[:8] == _PNG_MAGIC

    kill_png = _compose_card(_kill_header("KILL", 5), {}, {}, [], [], {}, _brand(), 5)
    assert kill_png is not None and kill_png[:8] == _PNG_MAGIC


# ── renderer contract: a config-shape mismatch must degrade, never raise ───────


async def _inline_to_thread(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
    return fn(*args, **kwargs)


async def test_render_degrades_when_given_wrong_config_shape() -> None:
    """The renderer reads cfg.killboard.cards (root AuraConfig). If wired to a
    KillboardConfig provider by mistake (no .killboard), both entry points must
    return None — degrade to embed-only — never raise into the feed/scheduler."""
    from killboard.cards import CardRenderer

    class _NoKillboard:  # a stand-in with no `.killboard` attribute
        pass

    renderer = CardRenderer(lambda: _NoKillboard(), _inline_to_thread)
    assert await renderer.render({}, []) is None
    assert await renderer.render_ranking_card(DailyRanking(0, 0, [], []), "Today") is None


async def test_cached_file_absent_is_cached_transient_is_not(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely-absent asset caches None (no re-stat); a transient read error
    on a file that still exists is NOT cached, so branding self-heals."""
    from pathlib import Path

    from killboard import cards as cards_mod
    from killboard.cards import CardRenderer

    renderer = CardRenderer(lambda: None, _inline_to_thread)

    # Absent file → None, and the negative is cached (key present).
    missing = Path(tmp_path) / "missing.png"
    assert await renderer._cached_file(missing) is None
    assert str(missing) in renderer._logo_cache

    # A present file whose read transiently fails → None, but NOT cached.
    present = Path(tmp_path) / "present.png"
    present.write_bytes(b"data")
    calls = {"n": 0}

    def _flaky_read(_path: Path) -> bytes | None:
        calls["n"] += 1
        return None  # simulate a transient OSError degrade

    monkeypatch.setattr(cards_mod, "_read_file", _flaky_read)
    assert await renderer._cached_file(present) is None
    assert await renderer._cached_file(present) is None
    assert calls["n"] == 2  # re-read each time — the transient None was not cached
    assert str(present) not in renderer._logo_cache


# ── detailed kill-card data extraction ────────────────────────────────────────


def test_detailed_card_data_helpers() -> None:
    """The pure extractors turn a raw gameinfo event into paperdoll/inventory/
    damage data, tolerating the API's partial shapes."""
    from killboard.cards import (
        _damage_rows,
        _header_data,
        _inventory_items,
        _slot_items,
        _tier_of,
        _unique_types,
    )

    raw = {
        "Killer": {
            "Name": "SuperTazz",
            "GuildName": "DEAD",
            "AverageItemPower": 1340.6,
            "Equipment": {
                "MainHand": {"Type": "T8_2H_AXE@1", "Quality": 3},
                "OffHand": None,
                "Head": {},
            },
        },
        "Victim": {
            "Name": "HunterCDH",
            "GuildName": "Enemy",
            "AverageItemPower": 1075.0,
            "Equipment": {"Head": {"Type": "T6_HEAD_LEATHER", "Quality": 2}},
            "Inventory": [{"Type": "T4_BAG", "Count": 3}, None, {"Count": 5}],
        },
        "TotalVictimKillFame": 36288,
        "TimeStamp": "2026-07-20T08:04:39",
        "Participants": [
            {"Name": "SuperTazz", "AverageItemPower": 1340, "DamageDone": 2275.0},
            {"Name": "Cherry", "SupportHealingDone": 2096.0},
        ],
    }

    ke = _slot_items(raw["Killer"]["Equipment"])
    assert set(ke) == {"MainHand"}  # None / typeless slots dropped
    inv = _inventory_items(raw["Victim"]["Inventory"])
    assert [i["Type"] for i in inv] == ["T4_BAG"]  # junk rows filtered
    dmg = _damage_rows(raw["Participants"])
    assert dmg[0] == {"name": "SuperTazz", "ip": 1340, "damage": 2275, "healing": 0}
    assert dmg[1]["healing"] == 2096
    assert "T8_2H_AXE@1" in _unique_types(ke, inv)
    assert _tier_of("T8_2H_AXE@1") == 8 and _tier_of("junk") == 0

    header = _header_data({"relation": "KILL"}, raw, raw["Killer"], raw["Victim"])
    assert header["killer_name"] == "SuperTazz" and header["killer_guild"] == "DEAD"
    assert header["killer_ip"] == 1340 and header["fame"] == 36288


@_needs_pil
def test_detailed_card_renders_full_layout() -> None:
    """A full kill card with both paperdolls, participants and dropped loot
    composites to a real PNG (no network — icons omitted)."""
    killer_equip = {
        "MainHand": {"Type": "T8_2H_AXE@1", "Quality": 3, "Count": 1},
        "Armor": {"Type": "T7_ARMOR_PLATE_SET1", "Quality": 2, "Count": 1},
        "Mount": {"Type": "T8_MOUNT_HORSE", "Quality": 1, "Count": 1},
    }
    victim_equip = {"Head": {"Type": "T6_HEAD_LEATHER_SET1", "Quality": 4, "Count": 1}}
    inventory = [{"Type": f"T{4 + (i % 4)}_RUNE", "Quality": 1, "Count": i + 1} for i in range(20)]
    participants = [
        {"name": "SuperTazz", "ip": 1340, "damage": 2275, "healing": 0},
        {"name": "Cherry", "ip": 1251, "damage": 0, "healing": 2096},
    ]
    png = _compose_card(
        _kill_header(), killer_equip, victim_equip, inventory, participants, {}, _brand(), 15090028
    )
    assert png is not None and png[:8] == _PNG_MAGIC
