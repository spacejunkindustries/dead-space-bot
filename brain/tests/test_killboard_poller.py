"""Pins the real behaviour of :mod:`killboard.poller` — the §5 ingestion loop.

No network, no Discord, no real files: a scripted fake :class:`~killboard.api.KbApi`
feeds event pages, a real :class:`~killboard.store.KbStore` over an in-memory
sqlite database absorbs the writes, and ``to_thread`` runs the (already
synchronous, same-thread) store calls inline for determinism. These tests lock
the high-water-mark walk: only genuinely-new events advance and enqueue, the walk
stops at stored ground (no re-ingest), a page that is entirely newer auto-paginates
(§5.2), first-run backfill is bounded by ``max_backfill_pages`` (§5.3), an
already-ingested event is never double-enqueued on a later tick, and an empty
first page records a poll failure.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest

from cortana.config import KbPollerConfig, KillboardConfig
from cortana.core import db
from killboard.model import EventRow
from killboard.poller import Poller
from killboard.store import MIGRATIONS_DIR, KbStore

GUILD_ID = "G1"


# ── seams ────────────────────────────────────────────────────────────────────


class FakeApi:
    """A scripted stand-in for :class:`~killboard.api.KbApi`.

    Holds one newest-first list of raw events (the API's rolling window) and
    serves ``events(...)`` by slicing on ``offset``/``limit`` — exactly the shape
    real pagination sees. Every call is recorded in :attr:`calls` for assertions.
    """

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.window = list(events)
        self.calls: list[tuple[str, int, int]] = []

    async def events(self, guild_id: str, limit: int = 51, offset: int = 0) -> list[dict[str, Any]]:
        self.calls.append((guild_id, limit, offset))
        return list(self.window[offset : offset + limit])


async def _inline_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run the store's (same-thread, synchronous) call inline — no worker pool,
    so ordering across a tick is fully deterministic in the test."""
    return fn(*args, **kwargs)


def _raw(event_id: int, *, guild: str = GUILD_ID, fame: int = 100) -> dict[str, Any]:
    """A minimal raw gameinfo event the tracked guild landed the blow on, so
    :func:`~killboard.model.parse_event` classifies it as a KILL and keeps it."""
    return {
        "EventId": event_id,
        "TimeStamp": "2026-07-20T12:00:00Z",
        "Killer": {"Id": "K", "Name": "Killer", "GuildId": guild},
        "Victim": {"Id": f"V{event_id}", "Name": "Victim", "GuildId": "OTHER"},
        "TotalVictimKillFame": fame,
    }


def _cfg(*, page_limit: int = 51, max_backfill_pages: int = 20) -> KillboardConfig:
    """A killboard config with the tracked guild and the poller knobs under test."""
    return KillboardConfig(
        guild_id=GUILD_ID,
        poller=KbPollerConfig(
            interval_seconds=1,
            page_limit=page_limit,
            max_backfill_pages=max_backfill_pages,
        ),
    )


@pytest.fixture
def store() -> KbStore:
    """A :class:`KbStore` over a fresh, migrated in-memory database."""
    conn: sqlite3.Connection = db.connect(":memory:")
    db.migrate(conn, MIGRATIONS_DIR)
    return KbStore(conn)


def _make_poller(
    store: KbStore,
    api: FakeApi,
    sink: Any,
    cfg: KillboardConfig,
    shutdown: asyncio.Event | None = None,
) -> Poller:
    return Poller(store, api, lambda: cfg, sink, _inline_to_thread, shutdown=shutdown)  # type: ignore[arg-type]


def _ids(batch: list[EventRow]) -> list[int]:
    return [row.event_id for row in batch]


# ── new-only ingest + stop at the mark ───────────────────────────────────────


@pytest.mark.asyncio
async def test_ingests_only_events_above_high_water_mark(store: KbStore) -> None:
    """Events above the mark are stored and enqueued chronologically; events at
    or below it are neither re-ingested nor enqueued, and the mark advances."""
    store.record_poll(100, advanced=True)
    api = FakeApi([_raw(103), _raw(102), _raw(101), _raw(100), _raw(99)])
    queue: asyncio.Queue[list[EventRow]] = asyncio.Queue()
    poller = _make_poller(store, api, queue, _cfg())

    await poller._poll_once()

    batch = queue.get_nowait()
    assert _ids(batch) == [101, 102, 103]  # oldest-first hand-off to the feed
    assert queue.empty()
    assert store.high_water_mark() == 103
    # 100 and 99 (at/below the mark) were never written to the events table.
    assert sorted(row.event_id for row in store.recent(50)) == [101, 102, 103]


# ── auto-pagination on an all-newer page (§5.2) ──────────────────────────────


@pytest.mark.asyncio
async def test_auto_paginates_when_full_page_is_all_newer(store: KbStore) -> None:
    """A page entirely newer than the mark triggers the next fetch (offset +=
    page_limit) until stored ground is reached — a spike above one page is not
    partially dropped (§5.2)."""
    store.record_poll(50, advanced=True)
    window = [_raw(eid) for eid in (56, 55, 54, 53, 52, 51, 50, 49, 48)]
    api = FakeApi(window)
    queue: asyncio.Queue[list[EventRow]] = asyncio.Queue()
    poller = _make_poller(store, api, queue, _cfg(page_limit=3, max_backfill_pages=10))

    await poller._poll_once()

    # Three pages walked: offsets 0, 3, 6 — the third contains the mark (50).
    assert [offset for _g, _l, offset in api.calls] == [0, 3, 6]
    assert _ids(queue.get_nowait()) == [51, 52, 53, 54, 55, 56]
    assert store.high_water_mark() == 56


# ── first-run backfill bound (§5.3) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_run_backfill_bounded_by_max_backfill_pages(store: KbStore) -> None:
    """With a fresh mark of 0 every event is new, so the walk would page forever;
    ``max_backfill_pages`` caps how deep the first-run seed reaches (§5.3)."""
    assert store.high_water_mark() == 0
    window = [_raw(eid) for eid in range(10, 0, -1)]  # 10..1, all newer than 0
    api = FakeApi(window)
    queue: asyncio.Queue[list[EventRow]] = asyncio.Queue()
    poller = _make_poller(store, api, queue, _cfg(page_limit=2, max_backfill_pages=3))

    await poller._poll_once()

    # Exactly 3 pages of 2 events — the bound stops the backfill, not the window.
    assert [offset for _g, _l, offset in api.calls] == [0, 2, 4]
    assert _ids(queue.get_nowait()) == [5, 6, 7, 8, 9, 10]
    # Older events (4..1) beyond the cap were left un-ingested this tick.
    assert sorted(row.event_id for row in store.recent(50)) == [5, 6, 7, 8, 9, 10]


# ── no double-enqueue across ticks ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_tick_does_not_re_enqueue_already_ingested(store: KbStore) -> None:
    """A second poll over an unchanged API window enqueues nothing new: the mark
    advanced on tick one, so every event now reads as stored ground."""
    api = FakeApi([_raw(103), _raw(102), _raw(101)])
    queue: asyncio.Queue[list[EventRow]] = asyncio.Queue()
    poller = _make_poller(store, api, queue, _cfg())

    await poller._poll_once()
    assert _ids(queue.get_nowait()) == [101, 102, 103]
    assert store.high_water_mark() == 103

    await poller._poll_once()  # same window, mark now 103
    assert queue.empty()
    assert store.high_water_mark() == 103
    # Still exactly the three original rows — no duplicates written.
    assert sorted(row.event_id for row in store.recent(50)) == [101, 102, 103]
    assert store.poll_state()["consecutive_fails"] == 0


# ── empty first page is a recorded failure ───────────────────────────────────


@pytest.mark.asyncio
async def test_empty_first_page_records_failure_and_enqueues_nothing(store: KbStore) -> None:
    """An empty first page means the API gave nothing back — a failure, so the
    fail streak increments and no batch is emitted (§5, §13)."""
    api = FakeApi([])
    queue: asyncio.Queue[list[EventRow]] = asyncio.Queue()
    poller = _make_poller(store, api, queue, _cfg())

    await poller._poll_once()

    assert queue.empty()
    assert store.poll_state()["consecutive_fails"] == 1
    assert store.high_water_mark() == 0


# ── no guild configured is a clean no-op ─────────────────────────────────────


@pytest.mark.asyncio
async def test_no_guild_id_is_a_noop_not_a_failure(store: KbStore) -> None:
    """Before a guild is resolved the tick does nothing — no API call, no write,
    and crucially no recorded failure (staleness must stay clean)."""
    api = FakeApi([_raw(1)])
    queue: asyncio.Queue[list[EventRow]] = asyncio.Queue()
    cfg = KillboardConfig(guild_id="", poller=KbPollerConfig(interval_seconds=1))
    poller = _make_poller(store, api, queue, cfg)

    await poller._poll_once()

    assert api.calls == []
    assert queue.empty()
    assert store.poll_state()["consecutive_fails"] == 0


# ── run() loop plumbing: one tick then a prompt shutdown ──────────────────────


@pytest.mark.asyncio
async def test_run_polls_then_exits_promptly_on_shutdown(store: KbStore) -> None:
    """Driving the supervised ``run`` entry point: one poll fires, the sink trips
    shutdown, and the loop wakes early and exits without a second tick."""
    store.record_poll(100, advanced=True)
    api = FakeApi([_raw(101)])
    shutdown = asyncio.Event()
    received: list[list[EventRow]] = []

    async def on_new(rows: list[EventRow]) -> None:
        received.append(rows)
        shutdown.set()

    poller = _make_poller(store, api, on_new, _cfg(), shutdown=shutdown)

    await asyncio.wait_for(poller.run(), timeout=2.0)

    assert len(received) == 1
    assert _ids(received[0]) == [101]


# ── data-loss guards: a fetch/persist failure must NOT advance the mark ───────


class _GiveUpApi(FakeApi):
    """Like :class:`FakeApi`, but returns ``None`` (a give-up, not an empty
    window) for a configured set of offsets — the real ``events()`` contract
    after retries are exhausted."""

    def __init__(self, events: list[dict[str, Any]], give_up_offsets: set[int]) -> None:
        super().__init__(events)
        self._give_up = give_up_offsets

    async def events(
        self, guild_id: str, limit: int = 51, offset: int = 0
    ) -> list[dict[str, Any]] | None:
        self.calls.append((guild_id, limit, offset))
        if offset in self._give_up:
            return None
        return list(self.window[offset : offset + limit])


class _FlakyStore:
    """Wraps a real :class:`KbStore` but raises on ``upsert_event`` for chosen
    ids — a transient persist failure (db locked / disk full)."""

    def __init__(self, inner: KbStore, fail_ids: set[int]) -> None:
        self._inner = inner
        self._fail_ids = fail_ids

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def upsert_event(self, row: EventRow, raw_json: str, now: str | None = None) -> None:
        if row.event_id in self._fail_ids:
            raise sqlite3.OperationalError("database is locked")
        self._inner.upsert_event(row, raw_json, now)


@pytest.mark.asyncio
async def test_deeper_page_giveup_does_not_advance_mark(store: KbStore) -> None:
    """A spike whose deeper page GAVE UP (None, not empty) must not advance the
    high-water mark past the events on that unfetched page — otherwise they are
    lost forever when the API window rolls off (§5.2)."""
    store.record_poll(50, advanced=True)
    # page 0 (offset 0) = 56,55,54 — all newer; the deeper page at offset 3 gives up.
    window = [_raw(eid) for eid in (56, 55, 54, 53, 52, 51, 50)]
    api = _GiveUpApi(window, give_up_offsets={3})
    queue: asyncio.Queue[list[EventRow]] = asyncio.Queue()
    poller = _make_poller(store, api, queue, _cfg(page_limit=3, max_backfill_pages=10))

    await poller._poll_once()

    # The mark did NOT advance — next tick re-scans from page 0 and retries the
    # spike, so 51/52/53 are not silently skipped.
    assert store.high_water_mark() == 50


@pytest.mark.asyncio
async def test_persist_failure_keeps_mark_below_the_failed_event(store: KbStore) -> None:
    """A transient persist failure on a new event caps the mark strictly below
    it, so it (and anything older) is retried next tick instead of being lost."""
    store.record_poll(50, advanced=True)
    flaky = _FlakyStore(store, fail_ids={52})
    api = FakeApi([_raw(53), _raw(52), _raw(51)])  # all newer than 50
    queue: asyncio.Queue[list[EventRow]] = asyncio.Queue()
    poller = _make_poller(flaky, api, queue, _cfg())  # type: ignore[arg-type]

    await poller._poll_once()

    # 52 failed to persist → the mark is capped at 51 (below 52), so 52 is
    # refetched next tick. It was NOT written; 51 and 53 were.
    assert store.high_water_mark() == 51
    assert sorted(row.event_id for row in store.recent(50)) == [51, 53]
