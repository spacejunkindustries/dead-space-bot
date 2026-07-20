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
