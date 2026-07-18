"""Personal ping registry tests — GDD §10.3. In-memory sqlite, stubbed cap."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from cortana.core import db
from cortana.core.personal_pings import PersonalPingRegistry, types_from_detail
from cortana.core.routing import PersonalPing
from cortana.nlu.grammar import PING_TYPE_ORDER, encode_ping_types
from cortana.types import Intent

GUILD = 1

ALL_TYPES = frozenset(PING_TYPE_ORDER)
CAMPS = frozenset({Intent.GATE_CAMP})


def make_holder(cap: int = 10) -> SimpleNamespace:
    """Duck-typed ConfigHolder — only discipline.personal_pings_max is read."""
    discipline = SimpleNamespace(personal_pings_max=cap)
    return SimpleNamespace(current=SimpleNamespace(discipline=discipline))


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    db.migrate(connection)
    db.execute(
        connection,
        "INSERT INTO systems (id, name, region, metaphone) VALUES (1, 'Otanuomi', 'R', 'OTNM')",
    )
    return connection


def registry(conn: sqlite3.Connection, cap: int = 10) -> PersonalPingRegistry:
    return PersonalPingRegistry(conn, make_holder(cap))  # type: ignore[arg-type]


# ── detail encoding round-trip (shared with grammar / the slash cog) ─────────


def test_types_from_detail_round_trips_grammar_encoding() -> None:
    assert types_from_detail(encode_ping_types(CAMPS)) == CAMPS
    assert types_from_detail(encode_ping_types(ALL_TYPES)) == ALL_TYPES


def test_types_from_detail_defensive_fallback() -> None:
    assert types_from_detail(None) == ALL_TYPES
    assert types_from_detail("") == ALL_TYPES
    assert types_from_detail("NOT_A_TYPE") == ALL_TYPES
    assert types_from_detail("GATE_CAMP,NOT_A_TYPE") == CAMPS


# ── CRUD + mirror ────────────────────────────────────────────────────────────


async def test_add_persists_and_mirrors(conn: sqlite3.Connection) -> None:
    reg = registry(conn)
    assert await reg.add(GUILD, 42, CAMPS, 1) is True
    row = db.query_one(conn, "SELECT * FROM personal_pings")
    assert row is not None
    assert row["guild_id"] == GUILD
    assert row["user_id"] == 42
    assert row["system_id"] == 1
    assert row["types_json"] == '["GATE_CAMP"]'
    assert row["created_at"]  # ISO timestamp written
    assert reg.rules_for(GUILD) == (PersonalPing(user_id=42, types=CAMPS, system_id=1),)
    subs = reg.list_for(GUILD, 42)
    assert len(subs) == 1
    assert subs[0].types == CAMPS


async def test_load_primes_mirror_from_table(conn: sqlite3.Connection) -> None:
    reg = registry(conn)
    await reg.add(GUILD, 42, CAMPS, None)
    await reg.add(GUILD, 43, ALL_TYPES, 1)
    fresh = registry(conn)
    assert fresh.rules_for(GUILD) == ()  # unprimed mirror is empty
    assert await fresh.load() == 2
    assert {p.user_id for p in fresh.rules_for(GUILD)} == {42, 43}


async def test_exact_duplicate_is_idempotent(conn: sqlite3.Connection) -> None:
    reg = registry(conn)
    assert await reg.add(GUILD, 42, CAMPS, 1) is True
    assert await reg.add(GUILD, 42, CAMPS, 1) is True  # confirmation, not a slot
    assert len(db.query(conn, "SELECT * FROM personal_pings")) == 1


async def test_cap_enforced_per_user(conn: sqlite3.Connection) -> None:
    reg = registry(conn, cap=2)
    assert await reg.add(GUILD, 42, CAMPS, None) is True
    assert await reg.add(GUILD, 42, CAMPS, 1) is True
    assert await reg.add(GUILD, 42, ALL_TYPES, 1) is False  # cap hit
    assert len(reg.list_for(GUILD, 42)) == 2
    # Another user is unaffected by 42's cap.
    assert await reg.add(GUILD, 43, CAMPS, None) is True


async def test_clear_removes_only_that_user(conn: sqlite3.Connection) -> None:
    reg = registry(conn)
    await reg.add(GUILD, 42, CAMPS, None)
    await reg.add(GUILD, 42, ALL_TYPES, 1)
    await reg.add(GUILD, 43, CAMPS, None)
    assert await reg.clear(GUILD, 42) == 2
    assert reg.list_for(GUILD, 42) == ()
    assert len(reg.list_for(GUILD, 43)) == 1
    rows = db.query(conn, "SELECT user_id FROM personal_pings")
    assert [r["user_id"] for r in rows] == [43]
    assert await reg.clear(GUILD, 42) == 0  # nothing left


async def test_remove_by_index_is_one_based_mypings_order(conn: sqlite3.Connection) -> None:
    reg = registry(conn)
    await reg.add(GUILD, 42, CAMPS, None)
    await reg.add(GUILD, 42, ALL_TYPES, 1)
    removed = await reg.remove(GUILD, 42, 1)
    assert removed is not None
    assert removed.types == CAMPS
    remaining = reg.list_for(GUILD, 42)
    assert len(remaining) == 1
    assert remaining[0].types == ALL_TYPES
    assert await reg.remove(GUILD, 42, 5) is None  # bad index
    assert await reg.remove(GUILD, 42, 0) is None
