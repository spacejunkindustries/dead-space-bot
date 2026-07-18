"""Voice-channel presence watching and Ears join/leave steering — GDD §3/§19.

Brain's discord.py process sees voice-state events; Ears owns the actual
voice connection. This module is the judgement between them: it watches the
human census of the watched channels and tells Ears when to join and leave
via IPC control messages (GDD §15, ids as strings):

    {"t": "join",    "guild_id": "…", "channel_id": "…"}
    {"t": "leave",   "guild_id": "…"}
    {"t": "optouts", "user_ids": ["…", …]}

No discord import here — the dsc layer feeds :meth:`on_voice_update` with
``(channel_id, present_count, unmuted_count)`` whenever a watched channel's
census changes: presence (mute-agnostic) drives join/leave so AURA stays with
a muted-but-present pilot, and the unmuted count feeds the §20 silence alarm.
The dsc layer also supplies an async ``announce_fn(channel_id)`` used for the
§19 consent announcement, posted **every single time** AURA joins.

Joins are debounced (5 s): a pilot popping into an empty channel and
straight back out must not drag the bot in and out behind them. Leaves are
immediate — an empty channel has nobody left to serve, and lingering looks
like lurking.

Opt-outs are enforced in Ears, before frames cross the IPC boundary
(CLAUDE.md constraint); this module's job is only to push the current
``optouts`` table across right after every join and whenever the set
changes (the ``/optout`` cog calls :meth:`push_optouts`).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable

import structlog

from aura.config import ConfigHolder
from aura.core import db
from aura.ipc import IpcServer

__all__ = ["ANNOUNCEMENT", "JOIN_DEBOUNCE_S", "VoiceGateway"]

log = structlog.get_logger(__name__)

#: §19: posted on every join, verbatim. This is the consent notice the corp
#: was promised; do not shorten, reword, or make it conditional.
ANNOUNCEMENT = (
    "🎙️ AURA is listening for commands. Audio is not recorded. `/optout` to exclude yourself."
)

#: Wait this long after the first pilot arrives before joining, so a
#: drive-by join/leave doesn't drag the bot around.
JOIN_DEBOUNCE_S = 5.0

#: "daily" join-announcement cadence: at most one §19 notice per this window.
_ANNOUNCE_DAILY_S = 24 * 3600.0

AnnounceFn = Callable[[int], Awaitable[None]]


def _log_task_failure(task: asyncio.Task[None]) -> None:
    """Done-callback for the fire-and-forget debounce task: a swallowed
    exception here would silently skip a join, so it is always logged."""
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.error("voice_join_task_failed", task=task.get_name(), exc_info=exc)


class VoiceGateway:
    """Auto-join/leave state machine over injected voice-census updates.

    One instance per process (single-guild deployment, GDD §16). The dsc
    layer calls :meth:`on_voice_update`; everything else is outbound.
    """

    def __init__(
        self,
        holder: ConfigHolder,
        ipc: IpcServer,
        conn: sqlite3.Connection,
        announce_fn: AnnounceFn,
        *,
        join_debounce_s: float = JOIN_DEBOUNCE_S,
    ) -> None:
        self._holder = holder
        self._ipc = ipc
        self._conn = conn
        self._announce = announce_fn
        self._join_debounce_s = join_debounce_s
        self._joined_channel_id: int | None = None
        self._pending_join: asyncio.Task[None] | None = None
        self._pending_channel_id: int | None = None
        self._counts: dict[int, int] = {}  # present (mute-agnostic) → join/leave
        self._unmuted: dict[int, int] = {}  # unmuted → §20 silence alarm
        self._lock = asyncio.Lock()
        self._on_census: Callable[[int], None] | None = None

    # ── wiring ───────────────────────────────────────────────────────────────

    def set_census_listener(self, listener: Callable[[int], None]) -> None:
        """Register a sync listener for human-count changes in the joined
        channel (the health reporter's ``set_humans_present``)."""
        self._on_census = listener

    @property
    def joined_channel_id(self) -> int | None:
        """The watched channel Ears is currently told to sit in, if any."""
        return self._joined_channel_id

    # ── inbound events ───────────────────────────────────────────────────────

    async def on_voice_update(
        self, channel_id: int, present_count: int, unmuted_count: int | None = None
    ) -> None:
        """A watched channel's census changed.

        ``present_count`` is every non-bot human in the channel (mute-agnostic)
        and drives auto-join/leave: AURA stays with a pilot who sits muted until
        they need to talk. ``unmuted_count`` (defaults to ``present_count`` when
        omitted) feeds the §20 voice-silence alarm via the census listener.

        The dsc layer calls this for watched channels only, on every relevant
        voice-state event; calls are idempotent and cheap.
        """
        if unmuted_count is None:
            unmuted_count = present_count
        cfg = self._holder.current.discord
        if channel_id not in cfg.watch_voice_channels:
            return
        async with self._lock:
            self._counts[channel_id] = present_count
            self._unmuted[channel_id] = unmuted_count
            if channel_id == self._joined_channel_id and self._on_census is not None:
                self._on_census(unmuted_count)

            if not cfg.auto_join:
                return

            if present_count > 0:
                if self._joined_channel_id == channel_id:
                    return  # already there
                if self._pending_channel_id == channel_id:
                    return  # debounce already running for this channel
                self._schedule_join(channel_id)
            else:
                if self._pending_channel_id == channel_id:
                    self._cancel_pending_join()
                if self._joined_channel_id == channel_id:
                    await self._leave()

    async def on_ears_hello(self) -> None:
        """Ears (re)connected: replay the join and the opt-out set.

        Ears restarts lose all session state; without this replay a Brain
        that thinks it is joined would sit deaf forever.
        """
        async with self._lock:
            if self._joined_channel_id is not None:
                # _send_join replays the opt-out set itself, right after the join.
                await self._send_join(self._joined_channel_id, announce=False)
            else:
                await self.push_optouts()

    # ── opt-outs ─────────────────────────────────────────────────────────────

    async def push_optouts(self) -> None:
        """Push the current ``optouts`` table to Ears (enforced there, pre-IPC)."""
        rows = await asyncio.to_thread(
            db.query, self._conn, "SELECT user_id FROM optouts ORDER BY user_id"
        )
        user_ids = [str(row["user_id"]) for row in rows]
        await self._ipc.send_control({"t": "optouts", "user_ids": user_ids})
        log.info("optouts_pushed", count=len(user_ids))

    # ── shutdown ─────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Cancel any pending join and tell Ears to leave."""
        async with self._lock:
            self._cancel_pending_join()
            if self._joined_channel_id is not None:
                await self._leave()

    # ── internals (call with the lock held) ──────────────────────────────────

    def _schedule_join(self, channel_id: int) -> None:
        self._cancel_pending_join()
        self._pending_channel_id = channel_id
        self._pending_join = asyncio.create_task(
            self._debounced_join(channel_id), name=f"voice-join-{channel_id}"
        )
        self._pending_join.add_done_callback(_log_task_failure)
        log.debug("join_debounce_started", channel_id=channel_id, delay_s=self._join_debounce_s)

    def _cancel_pending_join(self) -> None:
        if self._pending_join is not None:
            self._pending_join.cancel()
        self._pending_join = None
        self._pending_channel_id = None

    async def _debounced_join(self, channel_id: int) -> None:
        try:
            await asyncio.sleep(self._join_debounce_s)
        except asyncio.CancelledError:
            log.debug("join_debounce_cancelled", channel_id=channel_id)
            raise
        async with self._lock:
            self._pending_join = None
            self._pending_channel_id = None
            if self._counts.get(channel_id, 0) <= 0:
                return  # emptied while we were waiting
            await self._send_join(channel_id, announce=True)

    async def _send_join(self, channel_id: int, *, announce: bool) -> None:
        cfg = self._holder.current.discord
        await self._ipc.send_control(
            {"t": "join", "guild_id": str(cfg.guild_id), "channel_id": str(channel_id)}
        )
        self._joined_channel_id = channel_id
        if self._on_census is not None:
            self._on_census(self._unmuted.get(channel_id, 0))
        log.info("voice_join_sent", channel_id=channel_id)
        try:
            await self.push_optouts()
        except Exception:
            # Never let a DB hiccup swallow the §19 consent announcement.
            log.exception("optouts_push_failed", channel_id=channel_id)
        if announce and await self._announcement_due():
            try:
                await self._announce(channel_id)
            except Exception:
                log.exception("join_announcement_failed", channel_id=channel_id)
            else:
                await self._mark_announced()

    async def _announcement_due(self) -> bool:
        """§19 consent-announcement cadence (``discord.join_announcement``).

        "every" posts on each join; "daily" at most once per 24h — persisted
        in ``app_state`` because restart churn (the exact source of the spam)
        would reset an in-memory timestamp; "off" never posts.
        """
        mode = self._holder.current.discord.join_announcement
        if mode == "off":
            return False
        if mode == "every":
            return True
        row = await asyncio.to_thread(
            db.query_one,
            self._conn,
            "SELECT value FROM app_state WHERE key = 'last_join_announcement'",
        )
        if row is None:
            return True
        try:
            last = float(row["value"])
        except ValueError:
            return True
        return (time.time() - last) >= _ANNOUNCE_DAILY_S

    async def _mark_announced(self) -> None:
        await asyncio.to_thread(
            db.execute,
            self._conn,
            "INSERT INTO app_state (key, value) VALUES ('last_join_announcement', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(time.time()),),
        )

    async def _leave(self) -> None:
        cfg = self._holder.current.discord
        await self._ipc.send_control({"t": "leave", "guild_id": str(cfg.guild_id)})
        log.info("voice_leave_sent", channel_id=self._joined_channel_id)
        self._joined_channel_id = None
        if self._on_census is not None:
            self._on_census(0)
