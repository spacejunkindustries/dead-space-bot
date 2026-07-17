"""Pilot callsign registry — GDD §6.1 register / unregister / who-am-I.

A name registry keyed on the Discord user id Ears already attaches to every
utterance (the SSRC→user map). There are **no voice biometrics** here and no
audio anywhere near this module — identity comes from Discord, the callsign is
just a display name pilots choose (GDD §19 posture unchanged).

Same serialization pattern as the incident engine: public methods are async,
every SQLite touch rides ``asyncio.to_thread``, and writes are funnelled
through one internal lock. The registry also keeps an in-memory mirror of the
table so :meth:`lookup` is a sync, loop-safe read for render-time callers
(card "Reported by", /rollcall) — the mirror is primed by :meth:`load` at
startup and kept current by every write.

Methods return the exact §12.1 utterance strings (from :mod:`aura.tts`) so
voice and slash speak/print identically (constraint 10).
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

import structlog

from aura import tts
from aura.core import db

__all__ = ["CallsignRegistry"]

log = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class CallsignRegistry:
    """The single callsign store behind both voice and slash paths."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()
        # In-memory mirror of the callsigns table: user_id → callsign. Primed
        # by load(); every register/unregister keeps it current.
        self._cache: dict[int, str] = {}
        # Test seam: all time flows through this attribute; tests replace it.
        self._clock: Callable[[], datetime] = _utcnow

    async def load(self) -> int:
        """Prime the in-memory mirror from the table. Returns the row count."""
        async with self._lock:
            rows = await asyncio.to_thread(
                db.query, self._conn, "SELECT user_id, callsign FROM callsigns", ()
            )
            self._cache = {row["user_id"]: row["callsign"] for row in rows}
            log.info("callsigns_loaded", count=len(self._cache))
            return len(self._cache)

    def lookup(self, user_id: int) -> str | None:
        """Sync, loop-safe mirror read — for card rendering and /rollcall."""
        return self._cache.get(user_id)

    async def register(self, user_id: int, callsign: str) -> str:
        """Upsert this user's callsign; re-registering overwrites."""
        async with self._lock:
            now = self._clock()
            await asyncio.to_thread(
                db.execute,
                self._conn,
                "INSERT INTO callsigns (user_id, callsign, registered_at) VALUES (?, ?, ?)"
                " ON CONFLICT (user_id)"
                " DO UPDATE SET callsign = excluded.callsign,"
                " registered_at = excluded.registered_at",
                (user_id, callsign, now.astimezone(UTC).isoformat(timespec="microseconds")),
            )
            self._cache[user_id] = callsign
            log.info("callsign_registered", user_id=user_id, callsign=callsign)
            return tts.registered(callsign)

    async def unregister(self, user_id: int) -> tuple[bool, str]:
        """Delete this user's row; ``(was_registered, utterance)``."""
        async with self._lock:

            def _delete() -> bool:
                existing = db.query_one(
                    self._conn, "SELECT user_id FROM callsigns WHERE user_id = ?", (user_id,)
                )
                if existing is None:
                    return False
                db.execute(self._conn, "DELETE FROM callsigns WHERE user_id = ?", (user_id,))
                return True

            removed = await asyncio.to_thread(_delete)
            self._cache.pop(user_id, None)
            if not removed:
                return False, tts.not_registered()
            log.info("callsign_unregistered", user_id=user_id)
            return True, tts.unregistered()

    async def whoami(self, user_id: int) -> str:
        """Speakable answer for who-am-I; always succeeds."""
        callsign = await asyncio.to_thread(
            db.query_value,
            self._conn,
            "SELECT callsign FROM callsigns WHERE user_id = ?",
            (user_id,),
        )
        if callsign is None:
            return tts.not_registered()
        return tts.whoami(str(callsign))
