"""Pins the real behaviour of :mod:`killboard.store` against an in-memory
sqlite database migrated with the killboard's own migrations.

No network, no Discord, no real files — only ``db.connect(":memory:")`` plus the
killboard ``MIGRATIONS_DIR``. These tests lock the store's ingestion and
aggregate contracts: a fresh high-water mark, idempotent event upserts, windowed
counters scoped by relation, and the feed's oldest-first unposted dedup.
"""

from __future__ import annotations

import sqlite3

import pytest

from cortana.core import db
from killboard.model import DEATH, KILL, EventRow
from killboard.store import MIGRATIONS_DIR, KbStore


@pytest.fixture
def store() -> KbStore:
    """A :class:`KbStore` over a fresh, migrated in-memory database."""
    conn: sqlite3.Connection = db.connect(":memory:")
    db.migrate(conn, MIGRATIONS_DIR)
    return KbStore(conn)


def _event(
    event_id: int,
    *,
    relation: str = KILL,
    timestamp: str = "2026-07-20T12:00:00+00:00",
    total_fame: int = 0,
    killer_id: str | None = "K1",
    victim_id: str | None = "V1",
) -> EventRow:
    """A minimal, fully-populated :class:`EventRow` for store tests."""
    return EventRow(
        event_id=event_id,
        timestamp=timestamp,
        killer_id=killer_id,
        killer_name="Killer",
        killer_guild_id="G1",
        killer_ip=1000.0,
        victim_id=victim_id,
        victim_name="Victim",
        victim_guild_id="G2",
        victim_ip=900.0,
        total_fame=total_fame,
        relation=relation,
        num_participants=1,
        battle_id=None,
        location="Camp",
    )


def test_high_water_mark_starts_at_zero(store: KbStore) -> None:
    assert store.high_water_mark() == 0


def test_high_water_mark_falls_back_to_max_event_id(store: KbStore) -> None:
    # No poll_state row yet, so the mark comes from MAX(event_id).
    store.upsert_event(_event(10), raw_json="{}")
    store.upsert_event(_event(42), raw_json="{}")
    assert store.high_water_mark() == 42


def test_high_water_mark_prefers_poll_state(store: KbStore) -> None:
    store.upsert_event(_event(10), raw_json="{}")
    store.record_poll(last_event_id=99, advanced=True)
    assert store.high_water_mark() == 99


def test_upsert_event_is_idempotent_by_event_id(store: KbStore) -> None:
    store.upsert_event(_event(1, total_fame=500), raw_json="{}")
    # Re-seeing the same event_id must not create a second row.
    store.upsert_event(_event(1, total_fame=500), raw_json="{}")

    count = db.query_value(store._conn, "SELECT COUNT(*) FROM events")
    assert count == 1
    # And the windowed count reflects a single kill, not two.
    assert store.kill_count("2026-07-20T00:00:00+00:00") == 1


def test_upsert_event_overwrites_on_conflict(store: KbStore) -> None:
    store.upsert_event(_event(1, total_fame=100), raw_json='{"v": 1}')
    store.upsert_event(_event(1, total_fame=250), raw_json='{"v": 2}')

    fame = store.kill_fame("2026-07-20T00:00:00+00:00")
    raw = db.query_value(store._conn, "SELECT raw_json FROM events WHERE event_id = 1")
    assert fame == 250
    assert raw == '{"v": 2}'


def test_windowed_counts_respect_relation_and_window(store: KbStore) -> None:
    # Two kills and one death inside the window; one kill outside (too early).
    store.upsert_event(
        _event(1, relation=KILL, timestamp="2026-07-20T10:00:00+00:00", total_fame=100),
        raw_json="{}",
    )
    store.upsert_event(
        _event(2, relation=KILL, timestamp="2026-07-20T11:00:00+00:00", total_fame=200),
        raw_json="{}",
    )
    store.upsert_event(
        _event(3, relation=DEATH, timestamp="2026-07-20T11:30:00+00:00"),
        raw_json="{}",
    )
    store.upsert_event(
        _event(4, relation=KILL, timestamp="2026-07-19T23:00:00+00:00", total_fame=999),
        raw_json="{}",
    )

    start = "2026-07-20T00:00:00+00:00"
    end = "2026-07-21T00:00:00+00:00"

    # Only the two in-window kills count; the pre-window kill is excluded.
    assert store.kill_count(start, end) == 2
    assert store.death_count(start, end) == 1
    # Fame sums only the two in-window kills (100 + 200), not the 999 outside.
    assert store.kill_fame(start, end) == 300


def test_windowed_end_is_exclusive(store: KbStore) -> None:
    store.upsert_event(
        _event(1, relation=KILL, timestamp="2026-07-20T12:00:00+00:00"),
        raw_json="{}",
    )
    # An event exactly at `end` is excluded (half-open interval).
    assert store.kill_count("2026-07-20T00:00:00+00:00", "2026-07-20T12:00:00+00:00") == 0
    assert store.kill_count("2026-07-20T00:00:00+00:00", "2026-07-20T12:00:01+00:00") == 1


def test_unposted_events_oldest_first_and_excludes_posted(store: KbStore) -> None:
    store.upsert_event(_event(3), raw_json="{}")
    store.upsert_event(_event(1), raw_json="{}")
    store.upsert_event(_event(2), raw_json="{}")

    # Oldest-first by monotonic event_id, regardless of insert order.
    ids = [e.event_id for e in store.unposted_events(limit=10)]
    assert ids == [1, 2, 3]

    # Marking one posted removes it from the unposted feed.
    store.mark_posted(event_id=1, message_id=555, channel_id=777)
    remaining = [e.event_id for e in store.unposted_events(limit=10)]
    assert remaining == [2, 3]


def test_unposted_events_respects_limit(store: KbStore) -> None:
    for i in range(1, 6):
        store.upsert_event(_event(i), raw_json="{}")
    ids = [e.event_id for e in store.unposted_events(limit=2)]
    assert ids == [1, 2]


# ── death fame (mirror of kill fame over DEATH events) ─────────────────────────


def test_death_fame_sums_death_events_only(store: KbStore) -> None:
    """death_fame sums total_fame over DEATH events; KILL events don't count."""
    store.upsert_event(_event(1, relation=KILL, total_fame=5000), raw_json="{}")
    store.upsert_event(_event(2, relation=DEATH, total_fame=7000, victim_id="V1"), raw_json="{}")
    store.upsert_event(_event(3, relation=DEATH, total_fame=3000, victim_id="V2"), raw_json="{}")
    # Guild-wide death fame = 7000 + 3000; kill fame is the separate 5000.
    assert store.death_fame("2026-07-01T00:00:00+00:00") == 10000
    assert store.kill_fame("2026-07-01T00:00:00+00:00") == 5000
    # Scoped to one victim.
    assert store.death_fame("2026-07-01T00:00:00+00:00", player_id="V1") == 7000


def test_leaderboard_dfame_ranks_by_death_fame(store: KbStore) -> None:
    """The dfame metric ranks members by fame lost, and surfaces death-only members."""
    # V1 dies twice (big fame), V2 once (small). Neither has any kills.
    store.upsert_event(_event(1, relation=DEATH, total_fame=700000, victim_id="V1"), raw_json="{}")
    store.upsert_event(_event(2, relation=DEATH, total_fame=8000, victim_id="V1"), raw_json="{}")
    store.upsert_event(_event(3, relation=DEATH, total_fame=6000, victim_id="V2"), raw_json="{}")
    rows = store.leaderboard("dfame", "2026-07-01T00:00:00+00:00", limit=10)
    # Ordered by death fame desc; death-only members appear (via the UNION half).
    assert [(r["player_id"], r["dfame"]) for r in rows] == [("V1", 708000), ("V2", 6000)]
    # Every row carries the new dfame column even for the kill board.
    assert all("dfame" in r for r in rows)


# ── schedule add/remove admin helpers (drive the daily-ranking scheduler) ──────


def test_schedule_upsert_list_and_remove(store: KbStore) -> None:
    """/killboard schedule-add creates one row per kind (replacing on re-add) and
    schedule-remove deletes it — the rows the scheduler fires from."""
    from killboard.commands import _delete_schedule, _list_schedules, _upsert_schedule

    _upsert_schedule(store, "daily", 111, 12)
    _upsert_schedule(store, "weekly", 222, 8)
    rows = _list_schedules(store)
    assert {(r["kind"], r["channel_id"], r["hour_utc"]) for r in rows} == {
        ("daily", 111, 12),
        ("weekly", 222, 8),
    }
    assert all(r["last_run"] is None for r in rows)

    # Re-adding a kind replaces it (at most one daily) and re-arms last_run.
    _upsert_schedule(store, "daily", 999, 5)
    daily = [r for r in _list_schedules(store) if r["kind"] == "daily"]
    assert len(daily) == 1
    assert (daily[0]["channel_id"], daily[0]["hour_utc"]) == (999, 5)

    assert _delete_schedule(store, "daily") == 1
    assert _delete_schedule(store, "daily") == 0  # already gone
    assert {r["kind"] for r in _list_schedules(store)} == {"weekly"}
