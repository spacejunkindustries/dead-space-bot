"""Personal ping subscriptions — GDD §10.3 "ping me for gate camps in Otanuomi".

A **user mention, not a role**: each subscription asks CORTANA to append this
user's mention to the mention line of matching incident cards. The role model
of GDD §10 is unchanged — personal pings are additive, capped per user
(``discipline.personal_pings_max``), and ride the exact same discipline path
as role mentions (cooldown, circuit breaker, dedupe-fold no-re-mention). They
can never cause ``@here`` (constraint 11); :func:`aura.core.routing.evaluate`
only ever appends them to ``RoutingDecision.user_ids``.

Same serialization pattern as :mod:`aura.core.callsigns`: public methods are
async, every SQLite touch rides ``asyncio.to_thread``, writes are funnelled
through one internal lock, and an in-memory mirror (primed by :meth:`load`,
kept current by every write) makes :meth:`rules_for` / :meth:`list_for` sync,
loop-safe reads for the routing evaluator and the slash cog.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from aura.config import ConfigHolder
from aura.core import db
from aura.core.routing import PersonalPing
from aura.nlu.grammar import PING_TYPE_ORDER
from aura.types import Intent

__all__ = ["PersonalPingRegistry", "PingSub", "types_from_detail"]

log = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def types_from_detail(detail: str | None) -> frozenset[Intent]:
    """Decode the PING_ME ``detail`` slot (comma-separated ``Intent`` values,
    :func:`aura.nlu.grammar.encode_ping_types`). Unknown tokens are dropped;
    nothing usable falls back to all four report types — the grammar and the
    slash cog always encode explicitly, so this is a defensive default only.
    """
    types: set[Intent] = set()
    for token in (detail or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            types.add(Intent(token))
        except ValueError:
            log.warning("ping_types_unknown_token", token=token)
    return frozenset(types & set(PING_TYPE_ORDER)) or frozenset(PING_TYPE_ORDER)


def _types_to_json(types: frozenset[Intent]) -> str:
    return json.dumps([str(t) for t in PING_TYPE_ORDER if t in types])


def _types_from_json(raw: str) -> frozenset[Intent]:
    try:
        return frozenset(Intent(item) for item in json.loads(raw))
    except (ValueError, TypeError):
        log.warning("ping_types_bad_json", raw=raw)
        return frozenset()


@dataclass(frozen=True, slots=True)
class PingSub:
    """One stored subscription — mirrors a ``personal_pings`` row."""

    id: int
    guild_id: int
    user_id: int
    types: frozenset[Intent]
    system_id: int | None
    created_at: str


class PersonalPingRegistry:
    """The single personal-ping store behind both voice and slash paths."""

    def __init__(self, conn: sqlite3.Connection, holder: ConfigHolder) -> None:
        self._conn = conn
        self._holder = holder
        self._lock = asyncio.Lock()
        # In-memory mirror of the table: guild_id → subs ordered by id. Primed
        # by load(); every write keeps it current.
        self._subs: dict[int, list[PingSub]] = {}
        # Test seam: all time flows through this attribute; tests replace it.
        self._clock: Callable[[], datetime] = _utcnow

    async def load(self) -> int:
        """Prime the in-memory mirror from the table. Returns the row count."""
        async with self._lock:
            rows = await asyncio.to_thread(
                db.query, self._conn, "SELECT * FROM personal_pings ORDER BY id", ()
            )
            self._subs = {}
            for row in rows:
                self._subs.setdefault(row["guild_id"], []).append(self._sub_from_row(row))
            count = sum(len(subs) for subs in self._subs.values())
            log.info("personal_pings_loaded", count=count)
            return count

    # ── sync mirror reads (routing evaluator, cards, /mypings) ───────────────

    def rules_for(self, guild_id: int) -> tuple[PersonalPing, ...]:
        """Routing view of this guild's subscriptions, for ``evaluate``."""
        return tuple(
            PersonalPing(user_id=s.user_id, types=s.types, system_id=s.system_id)
            for s in self._subs.get(guild_id, ())
        )

    def list_for(self, guild_id: int, user_id: int) -> tuple[PingSub, ...]:
        """This user's subscriptions, oldest first (the /mypings index order)."""
        return tuple(s for s in self._subs.get(guild_id, ()) if s.user_id == user_id)

    # ── writes ───────────────────────────────────────────────────────────────

    async def add(
        self,
        guild_id: int,
        user_id: int,
        types: frozenset[Intent],
        system_id: int | None,
    ) -> bool:
        """Store one subscription; ``False`` when the per-user cap is hit.

        An exact duplicate (same types, same system) succeeds without a new
        row — repeating the command is confirmation, not a second slot.
        """
        async with self._lock:
            existing = self.list_for(guild_id, user_id)
            if any(s.types == types and s.system_id == system_id for s in existing):
                return True
            if len(existing) >= self._holder.current.discipline.personal_pings_max:
                log.info("personal_ping_cap_hit", guild_id=guild_id, user_id=user_id)
                return False
            now = self._clock()
            created_at = now.astimezone(UTC).isoformat(timespec="microseconds")
            sub_id = await asyncio.to_thread(
                db.execute,
                self._conn,
                "INSERT INTO personal_pings (guild_id, user_id, types_json, system_id,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (guild_id, user_id, _types_to_json(types), system_id, created_at),
            )
            self._subs.setdefault(guild_id, []).append(
                PingSub(
                    id=sub_id,
                    guild_id=guild_id,
                    user_id=user_id,
                    types=types,
                    system_id=system_id,
                    created_at=created_at,
                )
            )
            log.info(
                "personal_ping_added",
                guild_id=guild_id,
                user_id=user_id,
                types=[str(t) for t in PING_TYPE_ORDER if t in types],
                system_id=system_id,
            )
            return True

    async def clear(self, guild_id: int, user_id: int) -> int:
        """Delete all of this user's subscriptions; returns how many existed."""
        async with self._lock:
            removed = len(self.list_for(guild_id, user_id))
            if removed:
                await asyncio.to_thread(
                    db.execute,
                    self._conn,
                    "DELETE FROM personal_pings WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
                self._subs[guild_id] = [
                    s for s in self._subs.get(guild_id, []) if s.user_id != user_id
                ]
                log.info("personal_pings_cleared", guild_id=guild_id, user_id=user_id, n=removed)
            return removed

    async def remove(self, guild_id: int, user_id: int, index: int) -> PingSub | None:
        """Delete this user's ``index``-th subscription (1-based, /mypings
        order); returns the removed row or ``None`` for a bad index."""
        async with self._lock:
            mine = self.list_for(guild_id, user_id)
            if not 1 <= index <= len(mine):
                return None
            sub = mine[index - 1]
            await asyncio.to_thread(
                db.execute, self._conn, "DELETE FROM personal_pings WHERE id = ?", (sub.id,)
            )
            self._subs[guild_id] = [s for s in self._subs.get(guild_id, []) if s.id != sub.id]
            log.info("personal_ping_removed", guild_id=guild_id, user_id=user_id, sub_id=sub.id)
            return sub

    # ── internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _sub_from_row(row: sqlite3.Row) -> PingSub:
        return PingSub(
            id=row["id"],
            guild_id=row["guild_id"],
            user_id=row["user_id"],
            types=_types_from_json(row["types_json"]),
            system_id=row["system_id"],
            created_at=row["created_at"],
        )
