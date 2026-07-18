"""Utility cog tests — the pure/db-backed logic behind /evetime /route
/history /remindme /poll, plus the Gazetteer.path extension. In-memory sqlite
only; no Discord objects."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortana.config import GazetteerConfig
from cortana.core import db
from cortana.core.incidents import parse_duration
from cortana.dsc.cogs.utility import (
    REMINDER_MAX_DURATION,
    add_reminder,
    close_poll,
    create_poll,
    evetime_text,
    fire_due_reminders,
    fold_poll_votes,
    format_route,
    history_line,
    parse_poll_custom_id,
    pending_reminder_count,
    poll_custom_id,
    poll_lines,
    poll_row,
    poll_vote_indices,
    recent_incidents,
    record_vote,
    reminder_fires_at,
    render_poll_embed,
)
from cortana.nlu.gazetteer import Gazetteer

NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=UTC)

# Same small universe as test_gazetteer:
#
#   Otanuomi(1) — Kisogo(2) — Alenia(3) — Hulmate(4) — Jita(5)
#        |
#   Tannolen(6)
#
SYSTEMS = [
    (1, "Otanuomi", "Home-Region"),
    (2, "Kisogo", "Home-Region"),
    (3, "Alenia", "Border-Region"),
    (4, "Hulmate", "Border-Region"),
    (5, "Jita", "The Forge"),
    (6, "Tannolen", "Home-Region"),
]
EDGES = [(1, 2), (2, 3), (3, 4), (4, 5), (1, 6)]


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    db.migrate(connection)
    for sid, name, region in SYSTEMS:
        db.execute(
            connection,
            "INSERT INTO systems (id, name, region, constellation, metaphone)"
            " VALUES (?, ?, ?, NULL, '')",
            (sid, name, region),
        )
    for a, b in EDGES:
        db.execute(connection, "INSERT INTO system_adjacency (a_id, b_id) VALUES (?, ?)", (a, b))
    return connection


@pytest.fixture
def gaz(conn: sqlite3.Connection, tmp_path: Path) -> Gazetteer:
    scope = tmp_path / "gazetteer.yaml"
    scope.write_text("regions:\n  - Home-Region\n", encoding="utf-8")
    gazetteer = Gazetteer(conn, GazetteerConfig(file=str(scope), home_system="Otanuomi"))
    gazetteer.load()
    return gazetteer


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


# ── Gazetteer.path BFS ───────────────────────────────────────────────────────


def test_path_shortest_route(gaz: Gazetteer) -> None:
    assert gaz.path(1, 5) == (1, 2, 3, 4, 5)
    assert gaz.path(6, 3) == (6, 1, 2, 3)


def test_path_same_system(gaz: Gazetteer) -> None:
    assert gaz.path(2, 2) == (2,)


def test_path_length_agrees_with_jumps(gaz: Gazetteer) -> None:
    path = gaz.path(6, 5)
    assert path is not None
    assert len(path) - 1 == gaz.jumps(6, 5) == 5


def test_path_disconnected_is_none(conn: sqlite3.Connection, tmp_path: Path) -> None:
    db.execute(
        conn,
        "INSERT INTO systems (id, name, region, constellation, metaphone)"
        " VALUES (7, 'Island', 'Home-Region', NULL, '')",
    )
    scope = tmp_path / "gazetteer.yaml"
    scope.write_text("regions:\n  - Home-Region\n", encoding="utf-8")
    gaz = Gazetteer(conn, GazetteerConfig(file=str(scope), home_system="Otanuomi"))
    gaz.load()
    assert gaz.path(1, 7) is None


def test_path_memoised_per_source(gaz: Gazetteer) -> None:
    first = gaz.path(1, 5)
    memo = gaz._parent_memo  # noqa: SLF001 — asserting the memo style
    assert 1 in memo
    assert gaz.path(1, 4) == (1, 2, 3, 4)  # served from the same parent map
    assert gaz.path(1, 5) == first
    assert list(memo) == [1]  # no extra BFS runs for the same source


def test_path_crosses_pruned_systems_and_names_resolve(gaz: Gazetteer) -> None:
    # Jita and the border chain are pruned from the active set but still on
    # the path; system_name covers them where by_id does not.
    path = gaz.path(1, 5)
    assert path is not None
    assert gaz.by_id(5) is None
    assert gaz.system_name(5) == "Jita"
    assert [gaz.system_name(i) for i in path] == [
        "Otanuomi",
        "Kisogo",
        "Alenia",
        "Hulmate",
        "Jita",
    ]


def test_format_route() -> None:
    assert format_route(["A", "B", "C"]) == "**A** → **B** → **C** (2 jumps)"
    assert format_route(["A", "B"]) == "**A** → **B** (1 jump)"
    assert format_route(["A"]) == "**A** (0 jumps)"


# ── /evetime ─────────────────────────────────────────────────────────────────


def test_evetime_text_is_utc_with_local_hint() -> None:
    text = evetime_text(NOW)
    assert "12:00:00" in text
    assert "2026-07-17" in text
    assert f"<t:{int(NOW.timestamp())}:t>" in text


# ── /remindme (duration reuse + due query) ───────────────────────────────────


def test_reminder_duration_reuses_grammar_parser() -> None:
    assert parse_duration("45 minutes") == timedelta(minutes=45)
    assert reminder_fires_at(NOW, "45 minutes") == NOW + timedelta(minutes=45)
    assert reminder_fires_at(NOW, "1h30m") == NOW + timedelta(minutes=90)


def test_reminder_rejects_unparseable_and_over_cap() -> None:
    assert reminder_fires_at(NOW, "whenever") is None
    assert reminder_fires_at(NOW, "200 hours") is None  # > 7 days
    at_cap = reminder_fires_at(NOW, "168 hours")  # exactly 7 days
    assert at_cap == NOW + REMINDER_MAX_DURATION


def test_fire_due_reminders_marks_and_returns_due_only(conn: sqlite3.Connection) -> None:
    add_reminder(conn, 1, 42, NOW - timedelta(minutes=5), "due")
    add_reminder(conn, 1, 42, NOW + timedelta(hours=1), "not yet")
    add_reminder(conn, 1, 99, NOW - timedelta(minutes=1), "also due")

    rows = fire_due_reminders(conn, NOW)
    assert [row["message"] for row in rows] == ["due", "also due"]
    # Marked fired: a second sweep returns nothing new.
    assert fire_due_reminders(conn, NOW) == []
    # The future one is still pending.
    later = fire_due_reminders(conn, NOW + timedelta(hours=2))
    assert [row["message"] for row in later] == ["not yet"]


def test_pending_reminder_count_ignores_fired(conn: sqlite3.Connection) -> None:
    add_reminder(conn, 1, 42, NOW - timedelta(minutes=5), "old")
    add_reminder(conn, 1, 42, NOW + timedelta(hours=1), "pending")
    add_reminder(conn, 1, 7, NOW + timedelta(hours=1), "someone else")
    assert pending_reminder_count(conn, 42) == 2
    fire_due_reminders(conn, NOW)
    assert pending_reminder_count(conn, 42) == 1


# ── /history ─────────────────────────────────────────────────────────────────


def _insert_incident(
    conn: sqlite3.Connection,
    guild_id: int,
    system_id: int,
    opened_at: datetime,
    *,
    status: str = "ACTIVE",
    reporter_id: int = 100,
) -> int:
    return db.execute(
        conn,
        "INSERT INTO incidents (guild_id, system_id, type, severity, reporter_id,"
        " opened_at, updated_at, status)"
        " VALUES (?, ?, 'HOSTILE_SPOTTED', 'medium', ?, ?, ?, ?)",
        (guild_id, system_id, reporter_id, _iso(opened_at), _iso(opened_at), status),
    )


def test_recent_incidents_window_and_reporter_count(conn: sqlite3.Connection) -> None:
    recent = _insert_incident(conn, 1, 2, NOW - timedelta(hours=2))
    _insert_incident(conn, 1, 2, NOW - timedelta(hours=48))  # outside 24h window
    _insert_incident(conn, 1, 3, NOW - timedelta(hours=1))  # other system
    _insert_incident(conn, 2, 2, NOW - timedelta(hours=1))  # other guild
    # Two more pilots fold in; one reports twice (still one distinct reporter).
    for user_id in (101, 102, 102):
        db.execute(
            conn,
            "INSERT INTO incident_updates (incident_id, user_id, text, at) VALUES (?, ?, NULL, ?)",
            (recent, user_id, _iso(NOW)),
        )

    rows = recent_incidents(conn, 1, 2, _iso(NOW - timedelta(hours=24)))
    assert len(rows) == 1
    assert rows[0]["id"] == recent
    assert rows[0]["reporters"] == 3  # opener + 101 + 102

    wide = recent_incidents(conn, 1, 2, _iso(NOW - timedelta(hours=72)))
    assert len(wide) == 2
    assert wide[0]["id"] == recent  # newest first


def test_history_line_compact_format() -> None:
    line = history_line("HOSTILE_SPOTTED", _iso(NOW), 3, "ACTIVE")
    assert "🟠 Hostiles" in line
    assert f"<t:{int(NOW.timestamp())}:R>" in line
    assert "reported by 3" in line
    assert "active" in line
    # Unknown types/statuses render verbatim instead of crashing.
    assert "MYSTERY" in history_line("MYSTERY", _iso(NOW), 1, "WEIRD")


# ── /poll ────────────────────────────────────────────────────────────────────


def test_poll_custom_id_roundtrip() -> None:
    cid = poll_custom_id(17, 2)
    assert cid == "aura:poll:17:2"
    assert parse_poll_custom_id(cid) == (17, 2)
    assert parse_poll_custom_id("aura:inc:17:otw") is None
    assert parse_poll_custom_id("aura:poll:17:close") is None
    assert parse_poll_custom_id("aura:poll:17:2:extra") is None


def test_fold_poll_votes() -> None:
    assert fold_poll_votes([0, 1, 1, 3], 4) == (1, 2, 0, 1)
    assert fold_poll_votes([], 2) == (0, 0)
    # Out-of-range indices are dropped, not fatal.
    assert fold_poll_votes([0, 5, -1], 2) == (1, 0)


def test_record_vote_upsert_switches_vote(conn: sqlite3.Connection) -> None:
    poll_id = create_poll(conn, 1, 42, "Doctrine?", ["Ferox", "Moa"], NOW)
    record_vote(conn, poll_id, 100, 0, NOW)
    record_vote(conn, poll_id, 101, 0, NOW)
    assert fold_poll_votes(poll_vote_indices(conn, poll_id), 2) == (2, 0)
    # Pilot 100 switches: still one vote, now on option 1.
    record_vote(conn, poll_id, 100, 1, NOW + timedelta(minutes=1))
    assert fold_poll_votes(poll_vote_indices(conn, poll_id), 2) == (1, 1)


def test_close_poll_sets_closed_at(conn: sqlite3.Connection) -> None:
    poll_id = create_poll(conn, 1, 42, "Doctrine?", ["Ferox", "Moa"], NOW)
    row = poll_row(conn, poll_id)
    assert row is not None
    assert row["closed_at"] is None
    close_poll(conn, poll_id, NOW + timedelta(hours=1))
    row = poll_row(conn, poll_id)
    assert row is not None
    assert row["closed_at"] is not None


def test_poll_lines_and_embed() -> None:
    lines = poll_lines(["Ferox", "Moa"], [3, 1])
    assert len(lines) == 2
    assert "**Ferox**" in lines[0]
    assert lines[0].endswith("3")
    assert "▰" in lines[0]
    assert "▰" not in poll_lines(["A", "B"], [0, 0])[0]  # empty poll, empty bars

    embed = render_poll_embed(
        "Doctrine?", ["Ferox", "Moa"], [3, 1], poll_id=7, author_id=42, closed=False
    )
    assert embed["title"] == "📊 Doctrine?"
    assert "4 votes" in str(embed["footer"])
    assert "Poll #7" in str(embed["footer"])
    closed = render_poll_embed(
        "Doctrine?", ["Ferox", "Moa"], [3, 1], poll_id=7, author_id=42, closed=True
    )
    assert "closed" in str(closed["footer"])
