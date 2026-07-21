"""Synchronous SQLite access for the killboard (GDD §5, §8, §11).

The killboard owns its own database — a separate file from CORTANA's, with its
own migrations — because the ingested event store is the irreplaceable history
the gameinfo API does not keep (GDD §2.4). Nothing here touches CORTANA's
connection or tables.

Everything in this module is **synchronous** and blocking: the ``sqlite3``
driver is used directly (via the :mod:`cortana.core.db` helpers), exactly like
the rest of the bot (GDD §14). Callers on the event loop MUST wrap every method
in :func:`asyncio.to_thread` — no method here is safe to run on the loop.

Two families of query live here:

* **Ingestion** — high-water mark, event/participant upserts, poll-state
  bookkeeping, and the feed's unposted/mark-posted dedup (GDD §5).
* **Aggregates** — windowed kill/death counts, fame, assists, K/D, recent
  events, and leaderboards, all computed from the stored events rather than the
  API's lagging totals (GDD §8).

Timestamps written by this layer are ISO-8601 UTC strings. Every write method
takes an optional ``now`` for testability; when omitted it is computed fresh.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from cortana.core import db
from killboard.model import ASSIST, DEATH, KILL, EventRow, Participant

log = structlog.get_logger(__name__)

#: The killboard's own migrations directory — never CORTANA's.
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

#: Whitelisted leaderboard sort orders, keyed by public metric name. The value
#: is interpolated into the SQL ``ORDER BY`` clause, so it must never come from
#: caller input — hence the fixed mapping (GDD §8.1).
_LEADERBOARD_ORDER: dict[str, str] = {
    "fame": "fame DESC, kills DESC",
    "kills": "kills DESC, fame DESC",
    "kd": "kd DESC, kills DESC",
    "deaths": "deaths DESC, kills DESC",
    "dfame": "dfame DESC, deaths DESC",
}


#: Chunk size for ``event_id IN (...)`` batches — comfortably under SQLite's
#: default 999 bound-parameter ceiling (GDD §5).
_IN_CHUNK: int = 500


def _utc_now() -> str:
    """Current instant as an ISO-8601 UTC string (matches stored timestamps)."""
    return datetime.now(UTC).isoformat()


def open_store(db_path: str | Path) -> KbStore:
    """Open (creating if needed) the killboard database and apply its migrations.

    Connects with the standard pragmas (:func:`cortana.core.db.connect`) and runs
    every file in :data:`MIGRATIONS_DIR` before returning a ready :class:`KbStore`.
    Single-threaded startup path — safe to call directly, not via ``to_thread``.
    """
    conn = db.connect(db_path)
    version = db.migrate(conn, MIGRATIONS_DIR)
    log.info("kb_store_open", path=str(db_path), schema_version=version)
    return KbStore(conn)


class KbStore:
    """Synchronous store over the killboard's own sqlite connection (GDD §11)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # In-memory mirror of the poll_state row, kept fresh by record_poll /
        # record_poll_failure. health() (a sync BotModule method invoked on the
        # voice event loop) reads THIS, never sqlite — a loop-thread SELECT on a
        # connection concurrently written by worker threads would serialise on the
        # shared connection lock and stall the event loop. Seeded once here at
        # construction (module setup, before any worker task runs).
        self._poll_cache: dict[str, Any] = self._read_poll_state()

    def close(self) -> None:
        """Close the underlying sqlite connection (checkpoints the WAL). Idempotent
        and safe to call from shutdown or a setup-failure path; a double close or a
        close on an already-dead connection is swallowed."""
        with contextlib.suppress(sqlite3.Error):
            self._conn.close()

    # ── ingestion: high-water mark ───────────────────────────────────────────

    def high_water_mark(self) -> int:
        """The highest ``EventId`` already ingested (GDD §5.1).

        Read from ``poll_state.last_event_id`` (the poller's persisted mark),
        falling back to ``MAX(event_id)`` over the events table, and finally to
        ``0`` on a fresh database so first-run backfill (GDD §5.3) kicks in.
        """
        val = db.query_value(self._conn, "SELECT last_event_id FROM poll_state WHERE id = 1")
        if val:
            return int(val)
        mx = db.query_value(self._conn, "SELECT MAX(event_id) FROM events")
        return int(mx) if mx else 0

    def has_event(self, event_id: int) -> bool:
        """Whether ``event_id`` is already stored — cheap dedup for the deaths
        sweep, which re-fetches the same recent deaths each pass (GDD §5)."""
        val = db.query_value(
            self._conn, "SELECT 1 FROM events WHERE event_id = ? LIMIT 1", (event_id,)
        )
        return val is not None

    def has_events_many(self, event_ids: list[int]) -> set[int]:
        """Which of ``event_ids`` are already stored, as a set (GDD §5).

        The batched form of :meth:`has_event`: the deaths sweep re-fetches the
        same recent deaths per member every pass, so a per-death ``has_event``
        costs one ``to_thread`` hop + shared connection-lock acquisition each —
        up to ``members × per-member`` SELECTs contending with the live feed.
        This collapses that to a single ``SELECT event_id ... WHERE event_id IN
        (...)`` per chunk. The IN list is chunked to stay well under SQLite's
        bound-parameter ceiling (999); a sweep passes at most a few hundred ids,
        so this is one query in practice. Idempotent and read-only, mirroring the
        :meth:`mark_posted` / :meth:`mark_posted_many` precedent.
        """
        found: set[int] = set()
        if not event_ids:
            return found
        for i in range(0, len(event_ids), _IN_CHUNK):
            chunk = event_ids[i : i + _IN_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            rows = db.query(
                self._conn,
                f"SELECT event_id FROM events WHERE event_id IN ({placeholders})",  # noqa: S608 — placeholders only
                chunk,
            )
            found.update(int(r["event_id"]) for r in rows)
        return found

    # ── ingestion: event / participant upserts ───────────────────────────────

    def upsert_event(self, row: EventRow, raw_json: str, now: str | None = None) -> None:
        """Insert or update one event row plus its retained ``raw_json`` (GDD §5.4).

        Keyed on ``event_id``; a re-seen event is a harmless overwrite, which is
        what makes ingestion idempotent across restarts and overlapping polls.

        A blank event ``timestamp`` (the API occasionally omits ``TimeStamp``,
        GDD §2.4) falls back to the ingestion time ON FIRST INSERT — otherwise
        the empty string sorts before every ISO-8601 window bound and the kill,
        though it shows in the feed, silently vanishes from every windowed stat.
        Events are polled near-real-time, so ingestion time is a faithful
        stand-in. The conflict clause deliberately does NOT touch ``timestamp``:
        a re-upsert (overlapping poll, backfill) keeps the first-seen value, so
        an event that later re-appears without its ``TimeStamp`` can't have a
        good stored timestamp clobbered by a fresh ingestion time.
        """
        ts = now or _utc_now()
        timestamp = row.timestamp or ts
        db.execute(
            self._conn,
            """
            INSERT INTO events (
                event_id, timestamp, killer_id, killer_name, killer_guild_id,
                killer_ip, victim_id, victim_name, victim_guild_id, victim_ip,
                total_fame, relation, num_participants, battle_id, location,
                raw_json, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                killer_id        = excluded.killer_id,
                killer_name      = excluded.killer_name,
                killer_guild_id  = excluded.killer_guild_id,
                killer_ip        = excluded.killer_ip,
                victim_id        = excluded.victim_id,
                victim_name      = excluded.victim_name,
                victim_guild_id  = excluded.victim_guild_id,
                victim_ip        = excluded.victim_ip,
                total_fame       = excluded.total_fame,
                relation         = excluded.relation,
                num_participants = excluded.num_participants,
                battle_id        = excluded.battle_id,
                location         = excluded.location,
                raw_json         = excluded.raw_json
            """,
            (
                row.event_id,
                timestamp,
                row.killer_id,
                row.killer_name,
                row.killer_guild_id,
                row.killer_ip,
                row.victim_id,
                row.victim_name,
                row.victim_guild_id,
                row.victim_ip,
                row.total_fame,
                row.relation,
                row.num_participants,
                row.battle_id,
                row.location,
                raw_json,
                ts,
            ),
        )

    def upsert_participants(self, event_id: int, parts: list[Participant]) -> None:
        """Insert or update the damage/heal rows for one event (GDD §11).

        Participants without a ``player_id`` are skipped — the table keys on
        ``(event_id, player_id)`` and a null id has nothing to attribute.
        """
        rows = [
            (
                event_id,
                p.player_id,
                p.player_name,
                p.guild_id,
                p.damage_done,
                p.healing_done,
            )
            for p in parts
            if p.player_id is not None
        ]
        if not rows:
            return
        db.executemany(
            self._conn,
            """
            INSERT INTO participants (
                event_id, player_id, player_name, guild_id, damage_done, healing_done
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id, player_id) DO UPDATE SET
                player_name  = excluded.player_name,
                guild_id     = excluded.guild_id,
                damage_done  = excluded.damage_done,
                healing_done = excluded.healing_done
            """,
            rows,
        )

    # ── ingestion: poll-state bookkeeping ────────────────────────────────────

    def record_poll(self, last_event_id: int, advanced: bool, now: str | None = None) -> None:
        """Record a successful poll (GDD §5, §13).

        Advances ``last_event_id``, stamps ``last_success_at``, resets
        ``consecutive_fails`` to 0, and stamps ``last_advanced_at`` only when a
        genuinely new event arrived (``advanced``) — so staleness detection can
        distinguish "API is down" from "guild is quiet" (GDD §13).
        """
        ts = now or _utc_now()
        advanced_ts = ts if advanced else None
        db.execute(
            self._conn,
            """
            INSERT INTO poll_state (
                id, last_event_id, last_success_at, last_advanced_at, consecutive_fails
            ) VALUES (1, ?, ?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET
                last_event_id    = excluded.last_event_id,
                last_success_at  = excluded.last_success_at,
                last_advanced_at = CASE
                    WHEN ? THEN excluded.last_success_at
                    ELSE poll_state.last_advanced_at
                END,
                consecutive_fails = 0
            """,
            (last_event_id, ts, advanced_ts, 1 if advanced else 0),
        )
        # Mirror the write into the health cache (same semantics as the SQL:
        # last_advanced_at only moves on a genuine advance).
        self._poll_cache["last_event_id"] = int(last_event_id)
        self._poll_cache["last_success_at"] = ts
        if advanced:
            self._poll_cache["last_advanced_at"] = ts
        self._poll_cache["consecutive_fails"] = 0

    def record_poll_failure(self, now: str | None = None) -> None:
        """Record a failed poll: increment ``consecutive_fails`` (GDD §13).

        Leaves ``last_event_id``, ``last_success_at``, and ``last_advanced_at``
        untouched — a failure advances nothing, it only deepens the fail streak
        that drives backoff and the staleness warning.
        """
        del now  # failures don't stamp a success time; kept for signature parity
        db.execute(
            self._conn,
            """
            INSERT INTO poll_state (id, last_event_id, consecutive_fails)
            VALUES (1, 0, 1)
            ON CONFLICT(id) DO UPDATE SET
                consecutive_fails = poll_state.consecutive_fails + 1
            """,
        )
        self._poll_cache["consecutive_fails"] = (
            int(self._poll_cache.get("consecutive_fails", 0)) + 1
        )

    def poll_state(self) -> dict[str, Any]:
        """The current poll-state, for health/status (GDD §10).

        Serves the in-memory cache (kept in step with every ``record_poll`` /
        ``record_poll_failure``) rather than reading sqlite, so a caller on the
        voice event loop (``health()`` → ``/botstatus``) never issues a blocking
        SELECT on the connection worker threads are writing. The cache is a
        faithful mirror; it is seeded from the row at construction.
        """
        return dict(self._poll_cache)

    def _read_poll_state(self) -> dict[str, Any]:
        """Read the single poll-state row from sqlite as a plain dict.

        Returns zero/``None`` defaults when the row does not yet exist so callers
        never have to special-case a fresh database. Used to seed the in-memory
        cache at construction; not on the hot path.
        """
        row = db.query_one(
            self._conn,
            """
            SELECT last_event_id, last_success_at, last_advanced_at, consecutive_fails
            FROM poll_state WHERE id = 1
            """,
        )
        if row is None:
            return {
                "last_event_id": 0,
                "last_success_at": None,
                "last_advanced_at": None,
                "consecutive_fails": 0,
            }
        return {
            "last_event_id": int(row["last_event_id"]),
            "last_success_at": row["last_success_at"],
            "last_advanced_at": row["last_advanced_at"],
            "consecutive_fails": int(row["consecutive_fails"]),
        }

    # ── feed dedup: posted table ─────────────────────────────────────────────

    def mark_posted(
        self,
        event_id: int,
        message_id: int,
        channel_id: int,
        now: str | None = None,
    ) -> None:
        """Record that ``event_id`` has been posted to the feed (GDD §7.3).

        Keyed on ``event_id`` so a restart mid-batch never double-posts; a
        re-mark simply updates the message/channel/time.
        """
        ts = now or _utc_now()
        db.execute(
            self._conn,
            """
            INSERT INTO posted (event_id, message_id, channel_id, posted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                message_id = excluded.message_id,
                channel_id = excluded.channel_id,
                posted_at  = excluded.posted_at
            """,
            (event_id, message_id, channel_id, ts),
        )

    def mark_posted_many(self, event_ids: list[int], now: str | None = None) -> None:
        """Mark a batch of events posted (channel/message 0) in one round-trip.

        The catch-up collapse (GDD §7.3) records a large overflow as posted without
        sending it; on a first-run/downtime backlog that is thousands of rows, so a
        single ``executemany`` beats one write (and one ``to_thread`` hop) per row.
        Idempotent per ``event_id`` like :meth:`mark_posted`.
        """
        if not event_ids:
            return
        ts = now or _utc_now()
        db.executemany(
            self._conn,
            """
            INSERT INTO posted (event_id, message_id, channel_id, posted_at)
            VALUES (?, 0, 0, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                message_id = excluded.message_id,
                channel_id = excluded.channel_id,
                posted_at  = excluded.posted_at
            """,
            [(eid, ts) for eid in event_ids],
        )

    def raw_event(self, event_id: int) -> dict[str, Any] | None:
        """The stored full event JSON for ``event_id`` (§5.4), or ``None``.

        The feed uses it for the guild-tagged header and market loot-value — data
        the flat ``events`` columns don't carry. Tolerant: a missing row or
        unparseable JSON yields ``None``."""
        raw = db.query_value(
            self._conn, "SELECT raw_json FROM events WHERE event_id = ?", (event_id,)
        )
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def count_unposted(self) -> int:
        """How many ingested events are not yet in ``posted``.

        The feed uses this to size catch-up over the WHOLE backlog (§7.3) rather
        than one fetch window, so a backlog larger than a single window still
        collapses the oldest and posts only the newest ``catchup_max_posts``.
        """
        val = db.query_value(
            self._conn,
            """
            SELECT COUNT(*) FROM events e
            LEFT JOIN posted p ON e.event_id = p.event_id
            WHERE p.event_id IS NULL
            """,
        )
        return int(val) if val else 0

    def unposted_events(self, limit: int) -> list[EventRow]:
        """Ingested events not yet in ``posted``, oldest first (GDD §7.3).

        ``event_id`` is monotonic, so ordering by it ascending reads the feed
        chronologically. The feed consumes this, posts, then :meth:`mark_posted`.
        """
        rows = db.query(
            self._conn,
            """
            SELECT e.* FROM events e
            LEFT JOIN posted p ON e.event_id = p.event_id
            WHERE p.event_id IS NULL
            ORDER BY e.event_id ASC
            LIMIT ?
            """,
            (limit,),
        )
        return [_row_to_event(r) for r in rows]

    # ── aggregates: windowed counters (GDD §8) ───────────────────────────────

    def kill_count(self, start: str, end: str | None = None, player_id: str | None = None) -> int:
        """KILL count in ``[start, end)`` — guild-wide, or for one member's blows.

        The by-*count* kill stat the API never exposes (GDD §2.4, §8.1). Scoped
        to a member by their final-blow ``killer_id`` when ``player_id`` is given.
        """
        where, params = self._window(start, end, "timestamp")
        where.append("relation = ?")
        params.append(KILL)
        if player_id is not None:
            where.append("killer_id = ?")
            params.append(player_id)
        sql = f"SELECT COUNT(*) FROM events WHERE {' AND '.join(where)}"
        return int(db.query_value(self._conn, sql, params) or 0)

    def death_count(self, start: str, end: str | None = None, player_id: str | None = None) -> int:
        """DEATH count in ``[start, end)`` — guild-wide, or for one member (GDD §8.1)."""
        where, params = self._window(start, end, "timestamp")
        where.append("relation = ?")
        params.append(DEATH)
        if player_id is not None:
            where.append("victim_id = ?")
            params.append(player_id)
        sql = f"SELECT COUNT(*) FROM events WHERE {' AND '.join(where)}"
        return int(db.query_value(self._conn, sql, params) or 0)

    def kill_fame(self, start: str, end: str | None = None, player_id: str | None = None) -> int:
        """Summed awarded Kill Fame over KILL events in the window (GDD §8.2).

        Windowed fame from the event store — current to the last poll, unlike the
        API's ~daily lifetime totals. Scoped to a member's kills when given.
        """
        where, params = self._window(start, end, "timestamp")
        where.append("relation = ?")
        params.append(KILL)
        if player_id is not None:
            where.append("killer_id = ?")
            params.append(player_id)
        sql = f"SELECT COALESCE(SUM(total_fame), 0) FROM events WHERE {' AND '.join(where)}"
        return int(db.query_value(self._conn, sql, params) or 0)

    def death_fame(self, start: str, end: str | None = None, player_id: str | None = None) -> int:
        """Summed fame lost over DEATH events in the window (killboard GDD §8.2).

        The mirror of :meth:`kill_fame`: the fame value of the deaths a member (or
        the whole guild) suffered — what the *other* side earned killing them.
        Scoped to a member's own deaths by their ``victim_id`` when given.
        """
        where, params = self._window(start, end, "timestamp")
        where.append("relation = ?")
        params.append(DEATH)
        if player_id is not None:
            where.append("victim_id = ?")
            params.append(player_id)
        sql = f"SELECT COALESCE(SUM(total_fame), 0) FROM events WHERE {' AND '.join(where)}"
        return int(db.query_value(self._conn, sql, params) or 0)

    def assists(self, start: str, end: str | None = None, player_id: str | None = None) -> int:
        """Assist count in the window (GDD §6, §8.1).

        Guild-wide, counts events classified ``ASSIST``. For a member, counts
        events where they dealt damage (appear in ``participants``) but were
        neither the final-blow killer nor the victim.
        """
        if player_id is None:
            where, params = self._window(start, end, "timestamp")
            where.append("relation = ?")
            params.append(ASSIST)
            sql = f"SELECT COUNT(*) FROM events WHERE {' AND '.join(where)}"
            return int(db.query_value(self._conn, sql, params) or 0)

        where, params = self._window(start, end, "e.timestamp")
        where.append("p.player_id = ?")
        params.append(player_id)
        where.append("(e.killer_id IS NULL OR e.killer_id != ?)")
        params.append(player_id)
        where.append("(e.victim_id IS NULL OR e.victim_id != ?)")
        params.append(player_id)
        sql = (
            "SELECT COUNT(*) FROM events e "
            "JOIN participants p ON e.event_id = p.event_id "
            f"WHERE {' AND '.join(where)}"
        )
        return int(db.query_value(self._conn, sql, params) or 0)

    def kd(self, start: str, end: str | None = None, player_id: str | None = None) -> float:
        """True K/D *by count* in the window (GDD §8.1).

        ``kill_count / death_count``. With zero deaths the ratio is undefined, so
        it collapses to the raw kill count (a clean-sheet fighter's K/D reads as
        their kills rather than infinity).
        """
        kills = self.kill_count(start, end, player_id)
        deaths = self.death_count(start, end, player_id)
        if deaths == 0:
            return float(kills)
        return kills / deaths

    def recent(self, limit: int) -> list[EventRow]:
        """The most recently ingested events, newest first (GDD §10, ``/recent``)."""
        rows = db.query(
            self._conn,
            "SELECT * FROM events ORDER BY event_id DESC LIMIT ?",
            (limit,),
        )
        return [_row_to_event(r) for r in rows]

    def leaderboard(
        self, metric: str, start: str, end: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Top members by ``metric`` in the window (GDD §8.1, §8.3).

        ``metric`` is one of ``fame``, ``kills``, ``kd``, ``deaths``, ``dfame``.
        Each row aggregates a member's kills, deaths, Kill Fame, and Death Fame
        (the fame value of deaths they suffered) from KILL/DEATH events, plus a
        derived ``kd`` (deaths-zero → raw kills). Returns dicts with keys
        ``player_id``, ``player_name``, ``kills``, ``deaths``, ``fame``,
        ``dfame``, ``kd``.
        """
        order = _LEADERBOARD_ORDER.get(metric)
        if order is None:
            raise ValueError(f"unknown leaderboard metric: {metric!r}")

        win, win_params = self._window(start, end, "timestamp")
        window_sql = " AND ".join(win)
        # Window params appear once in each CTE (kills, deaths), limit at the end.
        params: list[Any] = [*win_params, *win_params, limit]
        sql = f"""
            WITH kills AS (
                SELECT killer_id AS pid,
                       MAX(killer_name) AS name,
                       COUNT(*) AS k,
                       COALESCE(SUM(total_fame), 0) AS f
                FROM events
                WHERE relation = 'KILL' AND killer_id IS NOT NULL AND {window_sql}
                GROUP BY killer_id
            ),
            deaths AS (
                SELECT victim_id AS pid,
                       MAX(victim_name) AS name,
                       COUNT(*) AS d,
                       COALESCE(SUM(total_fame), 0) AS df
                FROM events
                WHERE relation = 'DEATH' AND victim_id IS NOT NULL AND {window_sql}
                GROUP BY victim_id
            )
            SELECT k.pid AS player_id,
                   COALESCE(k.name, d.name) AS player_name,
                   k.k AS kills,
                   COALESCE(d.d, 0) AS deaths,
                   k.f AS fame,
                   COALESCE(d.df, 0) AS dfame,
                   CAST(k.k AS REAL)
                       / CASE WHEN COALESCE(d.d, 0) = 0 THEN 1 ELSE d.d END AS kd
            FROM kills k
            LEFT JOIN deaths d ON k.pid = d.pid
            UNION ALL
            SELECT d.pid AS player_id,
                   d.name AS player_name,
                   0 AS kills,
                   d.d AS deaths,
                   0 AS fame,
                   d.df AS dfame,
                   0.0 AS kd
            FROM deaths d
            WHERE d.pid NOT IN (SELECT pid FROM kills)
            ORDER BY {order}
            LIMIT ?
        """
        rows = db.query(self._conn, sql, params)
        return [
            {
                "player_id": r["player_id"],
                "player_name": r["player_name"],
                "kills": int(r["kills"]),
                "deaths": int(r["deaths"]),
                "fame": int(r["fame"]),
                "dfame": int(r["dfame"]),
                "kd": float(r["kd"]),
            }
            for r in rows
        ]

    # ── internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _window(start: str, end: str | None, column: str) -> tuple[list[str], list[Any]]:
        """Build the ``[start, end)`` timestamp predicate as (clauses, params).

        ``end`` is exclusive; when ``None`` the window is open-ended (everything at
        or after ``start``). ``column`` lets joined queries qualify the column
        (e.g. ``e.timestamp``).
        """
        clauses = [f"{column} >= ?"]
        params: list[Any] = [start]
        if end is not None:
            clauses.append(f"{column} < ?")
            params.append(end)
        return clauses, params


def _row_to_event(row: sqlite3.Row) -> EventRow:
    """Reconstruct an :class:`EventRow` from an ``events`` table row.

    Null ``total_fame``/``num_participants`` collapse to ``0`` to match the
    parser's non-null projection (arithmetic over them must never see a null).
    """
    return EventRow(
        event_id=int(row["event_id"]),
        timestamp=row["timestamp"],
        killer_id=row["killer_id"],
        killer_name=row["killer_name"],
        killer_guild_id=row["killer_guild_id"],
        killer_ip=row["killer_ip"],
        victim_id=row["victim_id"],
        victim_name=row["victim_name"],
        victim_guild_id=row["victim_guild_id"],
        victim_ip=row["victim_ip"],
        total_fame=int(row["total_fame"]) if row["total_fame"] is not None else 0,
        relation=row["relation"],
        num_participants=(
            int(row["num_participants"]) if row["num_participants"] is not None else 0
        ),
        battle_id=int(row["battle_id"]) if row["battle_id"] is not None else None,
        location=row["location"],
    )


__all__ = [
    "MIGRATIONS_DIR",
    "KbStore",
    "open_store",
]
