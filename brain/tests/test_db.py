"""Migration-runner tests — GDD §14: concurrent runners serialize and no-op.

The incident-7 race: Brain startup and the gazetteer seeder both ran
``migrate`` against the same fresh database; the loser re-executed
``CREATE TABLE`` and crashed. The runner now takes ``BEGIN IMMEDIATE`` and
re-checks ``user_version`` inside the transaction, so the loser skips.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from cortana.core import db


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "cortana.db"
    conn = db.connect(path)
    first = db.migrate(conn)
    assert first >= 7  # the durability migration is in the chain
    assert db.migrate(conn) == first  # second run: nothing to do, no crash
    conn.close()


def test_second_connection_no_ops_after_first_migrated(tmp_path: Path) -> None:
    """The seeder's connection, opened before Brain migrated, must skip
    cleanly once it runs — never re-execute a CREATE TABLE."""
    path = tmp_path / "cortana.db"
    a = db.connect(path)
    b = db.connect(path)  # opened at user_version 0, like the seeder
    assert db.schema_version(b) == 0
    final = db.migrate(a)
    assert db.migrate(b) == final  # in-transaction re-check skips every file
    b.close()
    a.close()


def test_concurrent_migrations_serialize(tmp_path: Path) -> None:
    """Two processes migrating the same database at once: BEGIN IMMEDIATE
    serializes the writers and the loser's re-check no-ops."""
    path = tmp_path / "cortana.db"
    barrier = threading.Barrier(2)
    results: list[int] = []
    errors: list[BaseException] = []
    # Connections opened serially (the WAL switch itself needs the file lock);
    # only the migrations race.
    conns = [db.connect(path), db.connect(path)]

    def run(conn: sqlite3.Connection) -> None:
        try:
            barrier.wait(timeout=5)
            results.append(db.migrate(conn))
        except BaseException as exc:  # noqa: BLE001 — collected for the assert
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=run, args=(conn,)) for conn in conns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]  # both settled on the same final version
    check = sqlite3.connect(path)
    try:
        tables = {
            row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        check.close()
    assert {"incidents", "timers", "app_state"} <= tables


def test_migrate_refuses_future_schema(tmp_path: Path) -> None:
    path = tmp_path / "cortana.db"
    conn = db.connect(path)
    conn.execute("PRAGMA user_version = 9999")
    try:
        db.migrate(conn)
    except db.MigrationError:
        pass
    else:  # pragma: no cover — the guard must fire
        raise AssertionError("migrate ran against a newer schema")
    finally:
        conn.close()


def test_schema_snapshot_matches_migration_chain() -> None:
    """brain/schema.sql is the regenerated snapshot of the migration chain —
    the drift (app_state missing from the snapshot) already happened once."""
    snapshot_path = Path(db.MIGRATIONS_DIR).parent / "schema.sql"
    migrated = db.connect(":memory:")
    db.migrate(migrated)
    snapshot = sqlite3.connect(":memory:")
    snapshot.executescript(snapshot_path.read_text(encoding="utf-8"))

    def shape(conn: sqlite3.Connection) -> dict[str, list[str]]:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")] for t in tables}

    try:
        assert shape(migrated) == shape(snapshot)
    finally:
        migrated.close()
        snapshot.close()
