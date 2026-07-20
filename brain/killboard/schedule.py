"""Scheduled ranking posts: daily / weekly / monthly leaderboards (GDD §8.3).

The killboard posts Kill Fame leaderboards on a schedule without anyone asking.
Each scheduled post is a row in the ``schedules`` table — ``(kind, channel_id,
hour_utc, last_run)`` (GDD §11) — where ``kind`` is ``daily``, ``weekly``, or
``monthly``. A :class:`Scheduler` runs as one supervised background task
(``ctx.supervisor``) that wakes about once a minute, and for every schedule
whose period boundary has passed posts the leaderboard for the period that just
completed, then stamps ``last_run``.

Two properties matter (GDD §8.3):

* **Idempotent across restarts.** ``last_run`` is compared against the *current*
  period's start: a post fires only when the last run predates that boundary, so
  a restart within the same period never double-posts. The check is cheap and
  runs every tick.
* **Reports the completed period.** A daily post at its hour reports *yesterday*,
  a Monday weekly post reports the *previous* week, a first-of-month monthly post
  reports the *previous* month — the window is closed, not the in-progress one.

Period alignment (which day is "today", which day is Monday, which is the 1st)
is computed in the guild's configured timezone (``cfg.rankings.timezone``,
GDD §8.1); ``hour_utc`` is the UTC hour-of-day the post goes out (identical to
local time under the default ``UTC`` zone). The scheduled metric is windowed
**Kill Fame** — the figure §8.2 designates for scheduled rankings.

All blocking sqlite work (loading schedules, building the leaderboard, stamping
``last_run``) rides ``to_thread`` so the voice event loop is never stalled; the
embed itself is built by the pure helpers in :mod:`killboard.rankings`. Every
message is sent with ``allowed_mentions=discord.AllowedMentions.none()`` — the
killboard is informational and never pings (CLAUDE.md constraint 11).
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
import structlog

from cortana.core import db
from killboard.rankings import build_leaderboard, leaderboard_embed, window_for

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from discord.ext import commands
    from structlog.stdlib import BoundLogger

    from cortana.config import KillboardConfig
    from killboard.store import KbStore

log = structlog.get_logger(__name__)

#: How often the loop wakes to re-check every schedule against the clock. A
#: minute is far finer than the coarsest schedule (hourly), so no boundary is
#: ever missed by more than ~60s, and the per-tick work is a single small SELECT.
CHECK_INTERVAL_SECONDS: int = 60

#: The metric scheduled posts rank by. Windowed Kill Fame is what §8.2 assigns to
#: scheduled rankings (distinct from the API's lagging lifetime fame).
SCHEDULED_METRIC: str = "fame"

#: How many members a scheduled leaderboard lists.
DEFAULT_LIMIT: int = 10

#: ``schedules.kind`` → the named ranking window (:data:`killboard.rankings.
#: PERIODS`) whose *just-completed* instance the post reports.
KINDS: dict[str, str] = {
    "daily": "today",
    "weekly": "week",
    "monthly": "month",
}


def _zone(tz: str) -> ZoneInfo:
    """Resolve the configured timezone, degrading to UTC on a bad name.

    A garbage tz string must never crash a scheduled post; it falls back to UTC
    with a warning, matching :mod:`killboard.rankings`.
    """
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        log.warning("kb_schedule.bad_timezone", timezone=tz)
        return ZoneInfo("UTC")


def _parse_iso(value: str | None) -> datetime | None:
    """Parse a stored ``last_run`` timestamp into an aware UTC datetime, or None.

    Tolerant: a missing or unparseable value reads as "never run" so a corrupt
    stamp re-arms the schedule rather than wedging it. A naive value is assumed
    to be UTC (this module only ever writes aware UTC stamps).
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def period_start(now: datetime, kind: str, tz: str) -> datetime:
    """The start of ``kind``'s current period, as an aware datetime in ``tz``.

    ``daily`` → local midnight today, ``weekly`` → the local midnight of this
    week's Monday, ``monthly`` → the local midnight of the 1st. ``now`` is any
    aware datetime (typically ``datetime.now(UTC)``). Raises :class:`ValueError`
    for an unknown ``kind``.
    """
    zone = _zone(tz)
    local = now.astimezone(zone)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "daily":
        return midnight
    if kind == "weekly":
        return midnight - timedelta(days=local.weekday())
    if kind == "monthly":
        return midnight.replace(day=1)
    raise ValueError(f"unknown schedule kind: {kind!r}")


def _fire_due(period_start_local: datetime, hour_utc: int) -> datetime:
    """The UTC instant at which the current period's post becomes due.

    The first ``hour_utc``:00 UTC at or after the period's start. Clamped to a
    valid hour so a bad config value can never raise.
    """
    hour = max(0, min(23, hour_utc))
    base = period_start_local.astimezone(UTC)
    candidate = base.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate < base:
        candidate += timedelta(days=1)
    return candidate


def should_fire(
    kind: str,
    hour_utc: int,
    last_run: str | None,
    now: datetime,
    tz: str,
) -> bool:
    """Whether this schedule should post right now (GDD §8.3).

    True when ``now`` has reached the current period's due instant *and* the last
    run predates the current period's start — the idempotency guard that makes a
    restart within the same period a no-op. Unknown ``kind`` is False (tolerant).
    """
    if kind not in KINDS:
        return False
    start_local = period_start(now, kind, tz)
    start_utc = start_local.astimezone(UTC)
    if now < _fire_due(start_local, hour_utc):
        return False
    last = _parse_iso(last_run)
    return last is None or last < start_utc


def _period_label(kind: str, start_iso: str) -> str:
    """A human label for the completed period, from its window start (§8.3).

    ``daily`` → ``"Daily — Jul 19, 2026"``, ``weekly`` → ``"Week of …"``,
    ``monthly`` → ``"July 2026"``. Falls back to the raw start on a parse miss so
    a scheduled post is never blocked by a formatting hiccup.
    """
    try:
        start = datetime.fromisoformat(start_iso)
    except (ValueError, TypeError):
        return start_iso
    if kind == "weekly":
        return f"Week of {start:%b %d, %Y}"
    if kind == "monthly":
        return f"{start:%B %Y}"
    return f"Daily — {start:%b %d, %Y}"


class Scheduler:
    """Posts daily/weekly/monthly ranking leaderboards on schedule (GDD §8.3).

    Constructed with the bot (for channel resolution and sending), the module's
    own :class:`~killboard.store.KbStore` (both the ``schedules`` table and the
    leaderboard aggregates live in its database), a zero-argument ``cfg_provider``
    returning the current :class:`~cortana.config.KillboardConfig` (so the
    timezone and tracked-guild name are read live and a hot reload applies on the
    next tick), ``to_thread`` for the blocking sqlite work, and a logger. Typical
    wiring mirrors the poller::

        Scheduler(
            ctx.bot,
            store,
            lambda: ctx.holder.current.killboard,
            ctx.to_thread,
            ctx.log,
            shutdown=ctx.shutdown,
        )

    :meth:`run` is the supervised entry point. It is idempotent across restarts
    (``last_run`` vs. the current period start, GDD §8.3), so the supervisor may
    cancel and respawn it freely.
    """

    def __init__(
        self,
        bot: commands.Bot,
        store: KbStore,
        cfg_provider: Callable[[], KillboardConfig],
        to_thread: Callable[..., Awaitable[Any]],
        log: BoundLogger | None = None,
        *,
        shutdown: asyncio.Event | None = None,
    ) -> None:
        self._bot = bot
        self._store = store
        self._cfg_provider = cfg_provider
        self._to_thread = to_thread
        self._log: Any = log if log is not None else structlog.get_logger(__name__)
        self._shutdown = shutdown

    # ── supervised entry point ───────────────────────────────────────────────

    async def run(self) -> None:
        """Check every schedule against the clock forever until shutdown (§8.3).

        Each tick is fully guarded: a flaky send, a bad channel, or a query error
        is caught, logged, and recovered from in place so a single broken schedule
        never propagates out and never blocks the others. ``CancelledError`` (not
        an :class:`Exception`) still propagates so the supervisor cancels cleanly.
        """
        self._log.info("kb_schedule.start")
        try:
            while not self._shutting_down():
                try:
                    await self._tick(datetime.now(UTC))
                except Exception as exc:  # noqa: BLE001 — inner net; must not escape
                    self._log.warning("kb_schedule.tick_error", error=str(exc), exc_info=True)
                await self._wait_next_tick()
        finally:
            self._log.info("kb_schedule.stop")

    # ── one check cycle ──────────────────────────────────────────────────────

    async def _tick(self, now: datetime) -> None:
        """Load every schedule and post the ones whose period boundary passed."""
        tz = self._cfg_provider().rankings.timezone
        rows = await self._to_thread(_load_schedules, self._store)
        for row in rows:
            kind = str(row["kind"] or "").strip().lower()
            channel_id = int(row["channel_id"] or 0)
            hour_utc = int(row["hour_utc"] or 0)
            last_run = row["last_run"]
            if not should_fire(kind, hour_utc, last_run, now, tz):
                continue
            try:
                await self._run_schedule(int(row["id"]), kind, channel_id, now, tz)
            except Exception as exc:  # noqa: BLE001 — one bad schedule must not stop the rest
                self._log.warning(
                    "kb_schedule.post_error",
                    error=str(exc),
                    schedule_id=int(row["id"]),
                    kind=kind,
                    exc_info=True,
                )

    async def _run_schedule(
        self, schedule_id: int, kind: str, channel_id: int, now: datetime, tz: str
    ) -> None:
        """Build and post one schedule's leaderboard, then stamp ``last_run``.

        The window is the period that *just completed*: the instant one second
        before the current period's start lands inside the previous period, and
        :func:`~killboard.rankings.window_for` turns it into that period's bounds.
        ``last_run`` is stamped only after a successful post, so a send failure
        leaves the schedule armed to retry on a later tick (never a double-post,
        since the stamp gates the next fire).
        """
        if channel_id <= 0:
            self._log.warning("kb_schedule.no_channel", schedule_id=schedule_id, kind=kind)
            return

        cfg = self._cfg_provider()
        period = KINDS[kind]
        prev_instant = period_start(now, kind, tz).astimezone(UTC) - timedelta(seconds=1)
        start_iso, end_iso = window_for(period, tz, prev_instant)

        rows = await self._to_thread(
            build_leaderboard,
            self._store,
            SCHEDULED_METRIC,
            start_iso,
            end_iso,
            DEFAULT_LIMIT,
        )
        embed = leaderboard_embed(
            SCHEDULED_METRIC,
            _period_label(kind, start_iso),
            rows,
            guild_name=cfg.guild_name or None,
            tz=tz,
            now=now,
        )

        posted = await self._post(channel_id, embed)
        if not posted:
            return
        await self._to_thread(_stamp_last_run, self._store, schedule_id, now.isoformat())
        self._log.info(
            "kb_schedule.posted",
            schedule_id=schedule_id,
            kind=kind,
            channel_id=channel_id,
            entries=len(rows),
        )

    # ── discord send ─────────────────────────────────────────────────────────

    async def _post(self, channel_id: int, embed: discord.Embed) -> bool:
        """Send ``embed`` to ``channel_id`` with mentions suppressed (constraint 11).

        Returns True on a successful send. A missing or non-messageable channel is
        logged and returns False (the schedule stays armed); network/permission
        errors propagate to the per-schedule guard in :meth:`_tick`.
        """
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            with contextlib.suppress(discord.HTTPException):
                channel = await self._bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            self._log.warning("kb_schedule.bad_channel", channel_id=channel_id)
            return False
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        return True

    # ── loop plumbing ────────────────────────────────────────────────────────

    def _shutting_down(self) -> bool:
        """Whether the shutdown event (if any) has been set."""
        return self._shutdown is not None and self._shutdown.is_set()

    async def _wait_next_tick(self) -> None:
        """Sleep ~a minute, waking immediately on shutdown so ``stop`` is prompt."""
        if self._shutdown is None:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._shutdown.wait(), timeout=CHECK_INTERVAL_SECONDS)


# ── schedules-table access (blocking sqlite; call via to_thread) ─────────────
#
# The ``schedules`` table is this module's own state, so its two tiny queries
# live here rather than widening the KbStore aggregate/ingestion surface. Both
# run on the killboard's own connection — never CORTANA's.


def _load_schedules(store: KbStore) -> list[sqlite3.Row]:
    """Every scheduled-post row (GDD §11). Blocking — call inside ``to_thread``."""
    return db.query(
        store._conn,  # noqa: SLF001 — schedules is this module's own table
        "SELECT id, kind, channel_id, hour_utc, last_run FROM schedules",
    )


def _stamp_last_run(store: KbStore, schedule_id: int, now_iso: str) -> None:
    """Record that ``schedule_id`` posted at ``now_iso`` (GDD §8.3 idempotency).

    Blocking — call inside ``to_thread``.
    """
    db.execute(
        store._conn,  # noqa: SLF001 — schedules is this module's own table
        "UPDATE schedules SET last_run = ? WHERE id = ?",
        (now_iso, schedule_id),
    )


__all__ = [
    "CHECK_INTERVAL_SECONDS",
    "DEFAULT_LIMIT",
    "KINDS",
    "SCHEDULED_METRIC",
    "Scheduler",
    "period_start",
    "should_fire",
]
