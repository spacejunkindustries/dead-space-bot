"""Learned custom areas — the systemless twin of the alias table (GDD §8.5a).

A pilot reports a place that resolves to no system; CORTANA asks once ("Did you
say <word>?"); on an explicit yes the confirmed word is saved here, and every
later report of it resolves at full confidence and posts verbatim (system_id
NULL, GDD §8.6). Text only — the confirmed word, never audio (constraint 5) —
exactly like the ``aliases`` table.

Pure synchronous sqlite access over an injected connection: the read path lives
in :func:`cortana.nlu.phonetics.resolve` (which holds only a ``conn``), the
write/manage paths in the dialog engine and the ``/areas-*`` cog, all via
``asyncio.to_thread``. Importing only :mod:`cortana.core.db` keeps this free of
cycles so the resolver can reach it.
"""

from __future__ import annotations

import sqlite3

import structlog

from cortana.core import db

log = structlog.get_logger(__name__)

__all__ = [
    "count_areas",
    "forget_area",
    "list_areas",
    "lookup_area",
    "normalize_phrase",
    "save_area",
]


def normalize_phrase(text: str) -> str:
    """The single lookup-key normaliser — identical to the alias/config-alias
    keying (``text.strip().lower()``) so a place learned "The Branch" matches a
    pilot saying "the branch"."""
    return text.strip().lower()


def lookup_area(conn: sqlite3.Connection, guild_id: int, phrase: str) -> str | None:
    """The verbatim ``display_name`` for a learned area in this guild, or None.

    Read-only and never bumps ``uses`` — it runs on the resolution hot path."""
    key = normalize_phrase(phrase)
    if not key:
        return None
    return db.query_value(
        conn,
        "SELECT display_name FROM custom_areas WHERE guild_id = ? AND phrase = ?",
        (guild_id, key),
    )


def count_areas(conn: sqlite3.Connection, guild_id: int) -> int:
    """How many areas this guild has learned — for the cap and the cog."""
    return (
        db.query_value(conn, "SELECT COUNT(*) FROM custom_areas WHERE guild_id = ?", (guild_id,))
        or 0
    )


def save_area(
    conn: sqlite3.Connection,
    guild_id: int,
    display_name: str,
    learned_by: int,
    learned_at: str,
    *,
    max_areas: int,
    phrase: str | None = None,
) -> str:
    """Persist (or reinforce) one confirmed custom area.

    Returns ``"created"`` (a new place learned), ``"reinforced"`` (the phrase
    already existed — ``uses`` bumped, display refreshed), or ``"at_cap"`` (a
    *new* place was refused because the guild is at ``max_areas``). The cap
    blocks only new rows; reinforcing an existing area always succeeds, so a
    full table never stops CORTANA re-confirming a place she already knows."""
    display = display_name.strip()
    key = normalize_phrase(phrase if phrase is not None else display_name)
    if not display or not key:
        raise ValueError("save_area requires a non-empty display_name/phrase")
    existed = lookup_area(conn, guild_id, key) is not None
    if not existed and count_areas(conn, guild_id) >= max_areas:
        return "at_cap"
    db.execute(
        conn,
        "INSERT INTO custom_areas (guild_id, phrase, display_name, learned_by, learned_at, uses)"
        " VALUES (?, ?, ?, ?, ?, 1)"
        " ON CONFLICT (guild_id, phrase) DO UPDATE SET"
        "   uses = uses + 1, display_name = excluded.display_name,"
        "   learned_by = excluded.learned_by, learned_at = excluded.learned_at",
        (guild_id, key, display, learned_by, learned_at),
    )
    return "reinforced" if existed else "created"


def list_areas(conn: sqlite3.Connection, guild_id: int) -> list[sqlite3.Row]:
    """Every learned area for the guild, most-used first (the cog's view)."""
    return db.query(
        conn,
        "SELECT phrase, display_name, learned_by, learned_at, uses"
        " FROM custom_areas WHERE guild_id = ? ORDER BY uses DESC, learned_at DESC",
        (guild_id,),
    )


def forget_area(conn: sqlite3.Connection, guild_id: int, phrase: str) -> bool:
    """Delete one learned area; True if it existed. (``db.execute`` returns a
    lastrowid, not a rowcount, so existence is checked before the delete.)"""
    key = normalize_phrase(phrase)
    if not key:
        return False
    existed = lookup_area(conn, guild_id, key) is not None
    if existed:
        db.execute(
            conn, "DELETE FROM custom_areas WHERE guild_id = ? AND phrase = ?", (guild_id, key)
        )
    return existed
