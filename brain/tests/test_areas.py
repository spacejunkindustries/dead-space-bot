"""Learned custom-area storage (GDD §8.5a) — the systemless alias twin.

In-memory sqlite with the real migrations applied; text only, no audio
(constraint 5)."""

from __future__ import annotations

import sqlite3

import pytest

from cortana.core import areas, db


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def test_save_then_lookup_normalizes(conn: sqlite3.Connection) -> None:
    assert areas.save_area(conn, 1, "The Branch", 42, "t0", max_areas=200) == "created"
    # Lookup normalizes both the stored key and the query (strip + lower).
    assert areas.lookup_area(conn, 1, "  the branch ") == "The Branch"
    assert areas.lookup_area(conn, 1, "THE BRANCH") == "The Branch"
    assert areas.lookup_area(conn, 1, "nope") is None


def test_reinforce_bumps_uses_and_refreshes_display(conn: sqlite3.Connection) -> None:
    areas.save_area(conn, 1, "the branch", 42, "t0", max_areas=200)
    assert areas.save_area(conn, 1, "The Branch", 99, "t1", max_areas=200) == "reinforced"
    rows = areas.list_areas(conn, 1)
    assert len(rows) == 1
    assert rows[0]["uses"] == 2
    assert rows[0]["display_name"] == "The Branch"  # display refreshed to the latest


def test_per_guild_isolation(conn: sqlite3.Connection) -> None:
    areas.save_area(conn, 1, "the branch", 42, "t0", max_areas=200)
    assert areas.lookup_area(conn, 2, "the branch") is None  # a different guild
    areas.save_area(conn, 2, "the branch", 7, "t0", max_areas=200)
    assert areas.count_areas(conn, 1) == 1
    assert areas.count_areas(conn, 2) == 1


def test_cap_blocks_new_but_allows_reinforce(conn: sqlite3.Connection) -> None:
    areas.save_area(conn, 1, "alpha", 1, "t0", max_areas=2)
    areas.save_area(conn, 1, "bravo", 1, "t0", max_areas=2)
    # At the cap: a NEW place is refused and not inserted.
    assert areas.save_area(conn, 1, "charlie", 1, "t0", max_areas=2) == "at_cap"
    assert areas.count_areas(conn, 1) == 2
    assert areas.lookup_area(conn, 1, "charlie") is None
    # ...but reinforcing an existing place still works even at the cap.
    assert areas.save_area(conn, 1, "alpha", 1, "t1", max_areas=2) == "reinforced"


def test_forget(conn: sqlite3.Connection) -> None:
    areas.save_area(conn, 1, "the branch", 1, "t0", max_areas=200)
    assert areas.forget_area(conn, 1, "THE BRANCH") is True
    assert areas.lookup_area(conn, 1, "the branch") is None
    assert areas.forget_area(conn, 1, "the branch") is False  # already gone


def test_list_ordered_by_uses(conn: sqlite3.Connection) -> None:
    areas.save_area(conn, 1, "rare", 1, "t0", max_areas=200)
    for _ in range(3):
        areas.save_area(conn, 1, "common", 1, "t1", max_areas=200)
    rows = areas.list_areas(conn, 1)
    assert [r["display_name"] for r in rows] == ["common", "rare"]  # most-used first


def test_empty_phrase_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        areas.save_area(conn, 1, "   ", 1, "t0", max_areas=200)
    assert areas.lookup_area(conn, 1, "   ") is None
    assert areas.forget_area(conn, 1, "") is False
