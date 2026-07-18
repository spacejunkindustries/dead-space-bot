"""Callsign registry tests — GDD §6.1. In-memory sqlite, exact §12.1 strings."""

from __future__ import annotations

import sqlite3

import pytest

from cortana.core import db
from cortana.core.callsigns import CallsignRegistry


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    db.migrate(connection)
    return connection


async def test_register_upserts_and_speaks(conn: sqlite3.Connection) -> None:
    registry = CallsignRegistry(conn)
    utterance = await registry.register(42, "Space Junkie")
    assert utterance == "Registered you as Space Junkie."
    row = db.query_one(conn, "SELECT * FROM callsigns WHERE user_id = 42")
    assert row is not None
    assert row["callsign"] == "Space Junkie"
    assert row["registered_at"]  # ISO timestamp written
    assert registry.lookup(42) == "Space Junkie"


async def test_reregister_overwrites(conn: sqlite3.Connection) -> None:
    registry = CallsignRegistry(conn)
    await registry.register(42, "Space Junkie")
    utterance = await registry.register(42, "Pod Goo")
    assert utterance == "Registered you as Pod Goo."
    rows = db.query(conn, "SELECT callsign FROM callsigns WHERE user_id = 42")
    assert [r["callsign"] for r in rows] == ["Pod Goo"]  # one row, replaced
    assert registry.lookup(42) == "Pod Goo"


async def test_unregister_removes_row(conn: sqlite3.Connection) -> None:
    registry = CallsignRegistry(conn)
    await registry.register(42, "Space Junkie")
    removed, utterance = await registry.unregister(42)
    assert removed is True
    assert utterance == "Unregistered."
    assert db.query(conn, "SELECT * FROM callsigns") == []
    assert registry.lookup(42) is None


async def test_unregister_when_not_registered(conn: sqlite3.Connection) -> None:
    registry = CallsignRegistry(conn)
    removed, utterance = await registry.unregister(42)
    assert removed is False
    assert utterance == "You are not registered."


async def test_whoami(conn: sqlite3.Connection) -> None:
    registry = CallsignRegistry(conn)
    assert await registry.whoami(42) == "You are not registered."
    await registry.register(42, "Space Junkie")
    assert await registry.whoami(42) == "You are Space Junkie."


async def test_load_primes_lookup_mirror(conn: sqlite3.Connection) -> None:
    """A fresh registry (post-restart) sees rows written by an earlier one."""
    first = CallsignRegistry(conn)
    await first.register(42, "Space Junkie")
    second = CallsignRegistry(conn)
    assert second.lookup(42) is None  # mirror not yet primed
    assert await second.load() == 1
    assert second.lookup(42) == "Space Junkie"
