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
from killboard import rankings
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


def test_compose_ranking_card_returns_png() -> None:
    rk = DailyRanking(
        total_kill_fame=210762,
        total_death_fame=1049806,
        top_kill_fame=[("DjPoppy", 116340), ("Snapjlr", 68000)],
        top_death_fame=[("Snapjlr", 708860)],
    )
    png = _compose_ranking_card(rk, "Jul 19, 2026", "Daily Ranking", "DEAD", _brand(), None)
    assert png is not None and png[:8] == _PNG_MAGIC


def test_compose_ranking_card_empty_boards_still_renders() -> None:
    png = _compose_ranking_card(
        DailyRanking(0, 0, [], []), "Today", "Daily Ranking", None, _brand()
    )
    assert png is not None and png[:8] == _PNG_MAGIC


def test_compose_card_with_brand_and_loot_value() -> None:
    fields = {
        "relation": "KILL",
        "killer_name": "DjPoppy",
        "victim_name": "Snapjlr",
        "killer_ip": 1350,
        "victim_ip": 1290,
        "total_fame": 204880,
        "location": "Bridgewatch",
        "timestamp": "2026-07-20T09:26:00",
    }
    png = _compose_card(fields, [], [], {}, [], _brand(), 204880)
    assert png is not None and png[:8] == _PNG_MAGIC


def test_compose_card_without_brand_or_value_still_renders() -> None:
    fields = {"relation": "DEATH", "killer_name": "A", "victim_name": "B", "total_fame": 0}
    png = _compose_card(fields, [], [], {}, [])
    assert png is not None and png[:8] == _PNG_MAGIC


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
