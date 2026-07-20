"""The ingestion loop — the heart of the killboard (GDD §5).

The gameinfo API keeps no history: its ``/events`` feed is a shallow, volatile
rolling window that drops old events within minutes (GDD §2.4, trap 2). Miss a
polling window and those events are gone *forever*. This loop therefore **is**
the history — it polls continuously, deduplicates by the monotonic ``EventId``
high-water mark, and persists every guild event to the killboard's own database
before the API can forget it.

One :class:`Poller` runs as a supervised background task (``ctx.supervisor``).
Each tick it fetches the newest page of events, walks newest→oldest until it hits
already-stored ground, classifies + upserts every new event, and hands the
genuinely-new ones to the feed. Two shapes of catch-up share the same walk:

* **Spike auto-pagination** (GDD §5.2) — when a page is entirely newer than the
  high-water mark, fetch the next page (``offset += page_limit``) until known
  ground or the server's offset ceiling is reached, so a ZvZ burst above the
  per-page limit is never partially dropped.
* **First-run backfill** (GDD §5.3) — on a fresh database the high-water mark is
  ``0``, so *every* event is new and the same walk pages backward through the
  API's retained window (up to ``max_backfill_pages``) to seed recent history.

The loop is defensive by construction (GDD §13): a flaky API costs *freshness*,
never *correctness*, and never a *crash*. The per-request retry/backoff lives in
:class:`~killboard.api.KbApi`; this loop's inner net catches whatever still
escapes, records the failure (``record_poll_failure``), and recovers in place —
``run`` never raises a transient error out to the supervisor, and it exits
promptly when ``shutdown`` is set. All blocking sqlite work rides
``to_thread`` so the voice event loop is never stalled.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import structlog

from killboard.model import EventRow, parse_event, participants_of

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

    from cortana.config import KbPollerConfig, KillboardConfig
    from killboard.api import KbApi
    from killboard.store import KbStore

log = structlog.get_logger(__name__)

#: The gameinfo ``/events`` endpoint refuses offsets past roughly this value
#: (GDD §5.2). Pagination stops here even if pages are still full, so a runaway
#: spike can never spin against a wall of ``504``s.
_SERVER_OFFSET_CEILING: int = 1000

#: A sink for genuinely-new events handed to the feed: either an async callback
#: invoked with the batch, or an :class:`asyncio.Queue` the batch is pushed onto.
NewEventSink = Callable[[list[EventRow]], Awaitable[None]] | asyncio.Queue[list[EventRow]]


class Poller:
    """The §5 ingestion loop for one tracked guild.

    Constructed with the module's own :class:`~killboard.store.KbStore` and
    :class:`~killboard.api.KbApi`, a zero-argument ``cfg_provider`` returning the
    *current* :class:`~cortana.config.KillboardConfig` (so region, interval, and
    page settings are read live and a hot reload applies on the next tick), an
    ``on_new_events`` sink for the feed, and ``to_thread`` (``asyncio.to_thread``)
    for the blocking sqlite writes. Typical wiring::

        Poller(
            store,
            api,
            lambda: ctx.holder.current.killboard,
            feed_queue,
            ctx.to_thread,
            ctx.log,
            shutdown=ctx.shutdown,
        )

    ``run`` is the supervised entry point. It is idempotent across restarts —
    dedup is the ``EventId`` high-water mark plus the ``events`` primary key
    (GDD §5.1) — so the supervisor may cancel and respawn it freely.
    """

    def __init__(
        self,
        store: KbStore,
        api: KbApi,
        cfg_provider: Callable[[], KillboardConfig],
        on_new_events: NewEventSink,
        to_thread: Callable[..., Awaitable[Any]],
        log: BoundLogger | None = None,
        *,
        shutdown: asyncio.Event | None = None,
    ) -> None:
        self._store = store
        self._api = api
        self._cfg_provider = cfg_provider
        self._on_new_events = on_new_events
        self._to_thread = to_thread
        self._log: Any = log if log is not None else structlog.get_logger(__name__)
        self._shutdown = shutdown

    # ── supervised entry point ───────────────────────────────────────────────

    async def run(self) -> None:
        """Poll forever until ``shutdown`` is set (GDD §5, §13).

        The loop body is fully guarded: any transient failure — a flaky API tick,
        a parse hiccup, a write error — is caught, recorded, and recovered from
        in place. It never propagates out of ``run`` as a transient error (the
        supervisor would restart the task, but in-place recovery is the inner
        net that keeps history contiguous across a wobble). ``CancelledError``
        (a :class:`BaseException`, not caught by ``except Exception``) still
        propagates so the supervisor can cancel the task cleanly on shutdown.
        """
        self._log.info("kb_poller.start")
        try:
            while not self._shutting_down():
                try:
                    await self._poll_once()
                except Exception as exc:  # noqa: BLE001 — inner net; must not escape
                    self._log.warning("kb_poller.tick_error", error=str(exc), exc_info=True)
                    await self._safe_record_failure()
                await self._wait_next_tick()
        finally:
            self._log.info("kb_poller.stop")

    # ── one poll cycle ───────────────────────────────────────────────────────

    async def _poll_once(self) -> None:
        """Fetch, walk, persist, and record one poll (GDD §5).

        Reads the guild id and poller settings live from ``cfg_provider``. When no
        guild is configured yet (resolution happens elsewhere at startup) the tick
        is a no-op — not a failure, so the staleness signal stays clean. An empty
        first page is treated as a failed fetch (the API is down or timing out;
        the endpoint always returns recent events for an active guild), recording
        a failure so backoff/staleness engage. Otherwise the poll state advances
        and the newly-ingested events are handed to the feed oldest-first.
        """
        cfg = self._cfg_provider()
        guild_id = (cfg.guild_id or "").strip()
        if not guild_id:
            self._log.warning("kb_poller.no_guild_id")
            return

        hwm = await self._to_thread(self._store.high_water_mark)
        ingested = await self._ingest(guild_id, cfg.poller, hwm)
        if ingested is None:
            # No data obtained this tick — the API gave nothing back.
            await self._to_thread(self._store.record_poll_failure)
            self._log.warning("kb_poller.empty_fetch", guild_id=guild_id, hwm=hwm)
            return

        new_rows, max_seen = ingested
        new_hwm = max(hwm, max_seen)
        advanced = bool(new_rows)
        await self._to_thread(self._store.record_poll, new_hwm, advanced)
        self._log.info(
            "kb_poller.tick",
            guild_id=guild_id,
            new_events=len(new_rows),
            last_event_id=new_hwm,
            backfill=hwm <= 0,
        )
        if new_rows:
            # new_rows is newest→oldest across pages; the feed posts chronologically.
            await self._emit(list(reversed(new_rows)))

    async def _ingest(
        self, guild_id: str, poller_cfg: KbPollerConfig, hwm: int
    ) -> tuple[list[EventRow], int] | None:
        """Walk pages newest→oldest, upserting every event above ``hwm``.

        Returns ``(new_rows, max_event_id_seen)`` on any successful fetch — even a
        quiet one with no new events — or ``None`` when the very first page comes
        back empty (an API failure, since an active guild's window is never empty).

        Pagination continues while a page is *entirely* newer than ``hwm`` (the
        oldest event in it still unknown), bounded by ``max_backfill_pages`` and
        the server offset ceiling (GDD §5.2/§5.3). It stops as soon as a stored
        event is reached, a short page signals the end of the window, or an empty
        deeper page appears (the per-request retries in ``KbApi`` have already run,
        so an empty page below the first reads as end-of-window, not a blip).
        """
        page_limit = max(1, poller_cfg.page_limit)
        max_pages = max(1, poller_cfg.max_backfill_pages)

        new_rows: list[EventRow] = []
        max_seen = hwm
        offset = 0

        for page_no in range(max_pages):
            if offset > _SERVER_OFFSET_CEILING:
                break
            events = await self._api.events(guild_id, limit=page_limit, offset=offset)
            if not events:
                if page_no == 0:
                    return None
                break

            reached_known_ground = False
            for raw in events:
                eid = _raw_event_id(raw)
                if eid is None:
                    continue
                if eid > max_seen:
                    max_seen = eid
                if eid <= hwm:
                    reached_known_ground = True
                    break
                row = await self._store_event(raw, guild_id)
                if row is not None:
                    new_rows.append(row)

            if reached_known_ground:
                break
            if len(events) < page_limit:
                # A short page means the retained window is exhausted; nothing
                # older remains to fetch.
                break
            offset += page_limit

        return new_rows, max_seen

    async def _store_event(self, raw: dict[str, Any], guild_id: str) -> EventRow | None:
        """Classify, parse, and persist one raw event; return its row or ``None``.

        Tolerant per-event (GDD §2.4): the parsers never raise, but any surprise
        is caught so a single malformed event can never take down the whole page.
        Returns ``None`` when the guild is not actually involved (``classify`` →
        ``None``), when the event lacks an id, or on an unexpected failure — in
        every case the walk simply continues.
        """
        try:
            row = parse_event(raw, guild_id)
            if row is None:
                return None
            raw_json = json.dumps(raw, separators=(",", ":"), ensure_ascii=False, default=str)
            parts = participants_of(raw)
            await self._to_thread(self._persist, row, raw_json, parts)
            return row
        except Exception as exc:  # noqa: BLE001 — never drop a page for one bad row
            self._log.warning("kb_poller.event_error", error=str(exc), event_id=_raw_event_id(raw))
            return None

    def _persist(self, row: EventRow, raw_json: str, parts: list[Any]) -> None:
        """Synchronous upsert of one event plus its participants (runs off-loop).

        Bundled into a single ``to_thread`` hop so the event and its damage rows
        land together. Both writes are idempotent (keyed on ``event_id`` and
        ``(event_id, player_id)``), so a re-seen event is a harmless overwrite.
        """
        self._store.upsert_event(row, raw_json)
        self._store.upsert_participants(row.event_id, parts)

    # ── feed hand-off ────────────────────────────────────────────────────────

    async def _emit(self, rows: list[EventRow]) -> None:
        """Hand a batch of new events to the feed sink (GDD §5, §7.3).

        Accepts either an async callback or an :class:`asyncio.Queue`. A failure
        here is contained — the events are already durably stored, and the feed's
        own ``posted`` dedup means a missed or repeated signal never double-posts
        nor loses a card, so the ingestion loop must not fall over for it.
        """
        if not rows:
            return
        sink = self._on_new_events
        try:
            if isinstance(sink, asyncio.Queue):
                sink.put_nowait(rows)
            else:
                await sink(rows)
        except Exception as exc:  # noqa: BLE001 — feed hand-off must not break ingest
            self._log.warning("kb_poller.emit_error", error=str(exc), count=len(rows))

    # ── loop plumbing ────────────────────────────────────────────────────────

    def _shutting_down(self) -> bool:
        """Whether the shutdown event (if any) has been set."""
        return self._shutdown is not None and self._shutdown.is_set()

    async def _wait_next_tick(self) -> None:
        """Sleep until the next poll, waking early on shutdown (GDD §5).

        Waits ``interval_seconds`` (read live, floored at 1s) but returns
        immediately once ``shutdown`` is set, so ``stop`` is prompt.
        """
        interval = max(1, self._cfg_provider().poller.interval_seconds)
        if self._shutdown is None:
            await asyncio.sleep(interval)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._shutdown.wait(), timeout=interval)

    async def _safe_record_failure(self) -> None:
        """Record a poll failure, swallowing any error from the write itself.

        Called from the loop's inner net, where even the bookkeeping write must
        not be allowed to escape and restart the task.
        """
        try:
            await self._to_thread(self._store.record_poll_failure)
        except Exception as exc:  # noqa: BLE001 — bookkeeping must not escape
            self._log.warning("kb_poller.record_failure_error", error=str(exc))


def _raw_event_id(raw: dict[str, Any]) -> int | None:
    """Tolerantly read ``EventId`` from a raw event for the high-water walk.

    The walk needs the id of *every* returned event — including ones the guild
    is not involved in — to decide when it has reached stored ground, so it can't
    lean on :func:`~killboard.model.parse_event` (which drops uninvolved events).
    Mirrors the parser's tolerant coercion (GDD §2.4): ints, floats, and numeric
    strings are accepted; ``bool`` and junk yield ``None``.
    """
    val = raw.get("EventId")
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if isinstance(val, str):
        try:
            return int(float(val.strip()))
        except (ValueError, TypeError):
            return None
    return None


__all__ = [
    "NewEventSink",
    "Poller",
]
