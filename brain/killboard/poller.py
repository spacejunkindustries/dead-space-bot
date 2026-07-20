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
import enum
import json
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import structlog

from killboard.model import EventRow, parse_event, participants_of


class _StoreOutcome(enum.Enum):
    """Result of trying to store one event — distinguishing the two None-y cases
    the high-water mark must treat oppositely (data-loss guard, GDD §5).

    - ``IRRELEVANT``: not the guild's event, no id, or unparseable. Safe to let
      the watermark pass — re-fetching would never turn it into a row.
    - ``FAILED``: a *transient* persist failure (db locked, disk full). The
      watermark must NOT pass it, or the event is lost forever when the API
      window rolls off; it is retried next tick.
    """

    IRRELEVANT = "irrelevant"
    FAILED = "failed"


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

# ── deaths sweep (the guild-events endpoint is kill-only) ─────────────────────
#: Guild deaths are polled per-member on a slower cadence than the kills loop:
#: once every this-many poll ticks (deaths change slowly and a sweep is N member
#: requests, so it must not run every tick).
_DEATHS_EVERY_TICKS: int = 8
#: Recent deaths fetched per member each sweep (already-stored ones are skipped).
_DEATHS_PER_MEMBER: int = 20
#: Re-fetch the member roster at most this often (seconds) — it changes rarely.
_ROSTER_TTL_S: float = 1800.0
#: Pause between per-member death fetches, to be a polite gameinfo client (§13).
_MEMBER_THROTTLE_S: float = 0.25

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
        #: deaths-sweep state (guild-events is kill-only; deaths come per-member).
        self._tick_count: int = 0
        self._roster: list[dict[str, Any]] = []
        self._roster_at: float = 0.0

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
                # Deaths ride the SAME task (serialised writes) on a slower cadence,
                # separately guarded so a deaths hiccup never disturbs the kills loop.
                self._tick_count += 1
                if self._tick_count % _DEATHS_EVERY_TICKS == 0:
                    try:
                        await self._poll_deaths()
                    except Exception as exc:  # noqa: BLE001 — contained; kills loop unaffected
                        self._log.warning("kb_poller.deaths_error", error=str(exc), exc_info=True)
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
        event is reached or a short page signals the end of the window.

        Two failure shapes are handled so the watermark can never advance past an
        event that wasn't actually captured (data loss, GDD §2.4/§5.2):

        * A **deeper page that gave up** (``events()`` → ``None`` for a timeout /
          exhausted 5xx/429) is NOT end-of-window — those ids are unknown, so the
          watermark is left unadvanced this tick (``max_seen = hwm``) and the
          whole walk is retried next tick (idempotent; the feed dedups).
        * An event whose **persist failed transiently** caps the watermark
          strictly below it, so it (and anything older) is refetched next tick.

        A genuinely empty ``[]`` page below the first is a real end-of-window.
        """
        page_limit = max(1, poller_cfg.page_limit)
        max_pages = max(1, poller_cfg.max_backfill_pages)

        new_rows: list[EventRow] = []
        max_seen = hwm
        lowest_unstored: int | None = None
        offset = 0

        for page_no in range(max_pages):
            if offset > _SERVER_OFFSET_CEILING:
                break
            events = await self._api.events(guild_id, limit=page_limit, offset=offset)
            if events is None:
                # Gave up (not an empty window). On page 0 the whole tick failed;
                # deeper, a spike page is unfetched — don't advance the watermark
                # past events we never saw. Retry next tick.
                if page_no == 0:
                    return None
                return new_rows, hwm
            if not events:
                if page_no == 0:
                    # An active guild's live window is never truly empty — treat
                    # an empty page-0 as a failure so backoff/staleness engage.
                    return None
                break  # a real empty page below the first = end of window

            reached_known_ground = False
            for raw in events:
                eid = _raw_event_id(raw)
                if eid is None:
                    continue
                if eid <= hwm:
                    reached_known_ground = True
                    break
                outcome = await self._store_event(raw, guild_id)
                if outcome is _StoreOutcome.FAILED:
                    lowest_unstored = eid if lowest_unstored is None else min(lowest_unstored, eid)
                    continue
                # Stored or intentionally-dropped (irrelevant): safe to account
                # for — the watermark may pass it.
                if eid > max_seen:
                    max_seen = eid
                if isinstance(outcome, EventRow):
                    new_rows.append(outcome)

            if reached_known_ground:
                break
            if len(events) < page_limit:
                # A short page means the retained window is exhausted.
                break
            offset += page_limit

        if lowest_unstored is not None:
            # Never let the recorded watermark reach or pass an event that failed
            # to persist — cap strictly below the lowest such event so it (and
            # everything older) is retried next tick.
            max_seen = min(max_seen, lowest_unstored - 1)
        return new_rows, max_seen

    async def _store_event(self, raw: dict[str, Any], guild_id: str) -> EventRow | _StoreOutcome:
        """Classify, parse, and persist one raw event.

        Returns the :class:`EventRow` when stored, :attr:`_StoreOutcome.IRRELEVANT`
        when the guild isn't involved / the event can't be parsed (safe to skip
        permanently — re-fetching won't help), or :attr:`_StoreOutcome.FAILED`
        when a *transient* persist error hit (must be retried, so the caller keeps
        the watermark below it). The parse and persist failures are deliberately
        separated: a parse error is permanent (skip), a persist error is transient
        (retry) — conflating them either wedges the poller or loses data (GDD §5).
        """
        try:
            row = parse_event(raw, guild_id)
        except Exception as exc:  # noqa: BLE001 — an unparseable event is skipped, not retried
            self._log.warning("kb_poller.parse_error", error=str(exc), event_id=_raw_event_id(raw))
            return _StoreOutcome.IRRELEVANT
        if row is None:
            return _StoreOutcome.IRRELEVANT
        try:
            raw_json = json.dumps(raw, separators=(",", ":"), ensure_ascii=False, default=str)
            parts = participants_of(raw)
            await self._to_thread(self._persist, row, raw_json, parts)
        except Exception as exc:  # noqa: BLE001 — transient persist failure: retry next tick
            self._log.warning("kb_poller.persist_error", error=str(exc), event_id=row.event_id)
            return _StoreOutcome.FAILED
        return row

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

    # ── deaths sweep (per-member; guild-events is kill-only) ──────────────────

    async def _poll_deaths(self) -> None:
        """Gather guild DEATHS per-member and ingest them (§5).

        The ``/events?guildId`` endpoint only returns kills the guild landed, so
        deaths never appear there — Death Fame and the death feed stay empty. This
        sweep walks the roster, fetches each member's recent ``/players/{id}/deaths``,
        skips the ones already stored (cheap ``has_event`` dedup), and stores the
        rest. Stored DEATH events flow to the feed via its normal unposted drain
        and into the Death-Fame aggregates — no special path. Throttled and roster-
        cached to stay a polite gameinfo client.
        """
        cfg = self._cfg_provider()
        if not cfg.poller.track_deaths:
            return
        guild_id = (cfg.guild_id or "").strip()
        if not guild_id:
            return
        members = await self._current_roster(guild_id)
        if not members:
            return

        new_rows: list[EventRow] = []
        for member in members:
            if self._shutting_down():
                break
            player_id = str(member.get("Id") or "").strip()
            if not player_id:
                continue
            for raw in await self._api.player_deaths(player_id, limit=_DEATHS_PER_MEMBER):
                event_id = _raw_event_id(raw)
                if event_id is not None and await self._to_thread(self._store.has_event, event_id):
                    continue  # already ingested a previous sweep — skip re-store/re-emit
                outcome = await self._store_event(raw, guild_id)
                if isinstance(outcome, EventRow):
                    new_rows.append(outcome)
            await asyncio.sleep(_MEMBER_THROTTLE_S)

        if new_rows:
            new_rows.sort(key=lambda r: r.event_id)
            await self._emit(new_rows)
            self._log.info("kb_poller.deaths_swept", members=len(members), new_deaths=len(new_rows))

    async def _current_roster(self, guild_id: str) -> list[dict[str, Any]]:
        """The member roster, cached with a TTL; keeps the last good list on a
        transient fetch failure so one flaky call doesn't skip the whole sweep."""
        now = time.monotonic()
        if self._roster and (now - self._roster_at) < _ROSTER_TTL_S:
            return self._roster
        members = await self._api.members(guild_id)
        if members:
            self._roster = members
            self._roster_at = now
        return self._roster

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
