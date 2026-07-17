"""SQLite access layer: connection setup, migrations, query helpers, backup.

The synchronous :mod:`sqlite3` module is used deliberately — one corp, low
write volume, no concurrency pressure (GDD §14). Callers on the event loop
MUST wrap every call in ``asyncio.to_thread`` (or a dedicated executor);
nothing in this module is safe to run on the loop directly. Connections are
created with ``check_same_thread=False`` so the ``to_thread`` worker pool can
use them, but access must still be serialized by the caller — the incident
engine funnels all writes through one path, which is the intended pattern.

Schema revisions are tracked with ``PRAGMA user_version``: each file in
``brain/migrations/`` is named ``NNNN_description.sql`` and, once applied,
bumps ``user_version`` to ``NNNN``. ``brain/schema.sql`` is the reference
snapshot of the schema after all migrations; it is never executed directly.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: Default migrations directory: brain/migrations/ relative to this file.
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

_MIGRATION_NAME = re.compile(r"^(\d{4})_[A-Za-z0-9_-]+\.sql$")


class MigrationError(RuntimeError):
    """A migration file is misnamed, out of order, or failed to apply."""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the AURA database with the required pragmas.

    Applies ``journal_mode=WAL`` and ``foreign_keys=ON`` (plus a 5s busy
    timeout) and sets ``row_factory`` to :class:`sqlite3.Row`. Does NOT run
    migrations — call :func:`migrate` explicitly at startup.
    """
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    log.info("db_connected", path=str(p))
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the current ``PRAGMA user_version`` (0 on a fresh database)."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def _discover_migrations(migrations_dir: Path) -> list[tuple[int, Path]]:
    """Return ``(version, path)`` pairs sorted by version, validating names."""
    if not migrations_dir.is_dir():
        raise MigrationError(f"migrations directory not found: {migrations_dir}")
    found: dict[int, Path] = {}
    for f in sorted(migrations_dir.iterdir()):
        if f.suffix != ".sql":
            continue
        m = _MIGRATION_NAME.match(f.name)
        if m is None:
            raise MigrationError(
                f"migration filename {f.name!r} does not match NNNN_description.sql"
            )
        version = int(m.group(1))
        if version == 0:
            raise MigrationError(f"migration version must be >= 1: {f.name}")
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version:04d}: {found[version].name} and {f.name}"
            )
        found[version] = f
    ordered = sorted(found.items())
    for i, (version, path) in enumerate(ordered, start=1):
        if version != i:
            raise MigrationError(
                f"migration versions must be contiguous from 0001; expected {i:04d}, "
                f"found {version:04d} ({path.name})"
            )
    return ordered


def migrate(conn: sqlite3.Connection, migrations_dir: Path | None = None) -> int:
    """Apply all pending migrations, in filename order. Returns the final version.

    Each file runs in its own transaction; ``user_version`` is bumped in the
    same commit, so a failed migration leaves the database at the previous
    version. Raises :class:`MigrationError` on any inconsistency.
    """
    directory = migrations_dir if migrations_dir is not None else MIGRATIONS_DIR
    migrations = _discover_migrations(directory)
    current = schema_version(conn)
    if migrations and current > migrations[-1][0]:
        raise MigrationError(
            f"database user_version {current} is ahead of newest migration "
            f"{migrations[-1][0]:04d} — refusing to run against a newer schema"
        )
    for version, path in migrations:
        if version <= current:
            continue
        sql = path.read_text(encoding="utf-8")
        # Explicit BEGIN: pysqlite's implicit transactions do not cover DDL,
        # so `with conn:` alone would autocommit each CREATE statement.
        try:
            conn.execute("BEGIN")
            for statement in _split_statements(sql):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise MigrationError(f"migration {path.name} failed: {exc}") from exc
        log.info("db_migration_applied", version=version, file=path.name)
        current = version
    return current


def _split_statements(sql: str) -> list[str]:
    """Split a migration script into complete statements.

    ``executescript`` would issue an implicit COMMIT, breaking per-file
    transactionality, so statements are split and executed individually using
    :func:`sqlite3.complete_statement` to respect semicolons inside literals
    and triggers.
    """
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        stripped = line.strip()
        if not buffer and (not stripped or stripped.startswith("--")):
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        statements.append(buffer.strip())
    return statements


# ── thin typed helpers ───────────────────────────────────────────────────────


def execute(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> int:
    """Run a single INSERT/UPDATE/DELETE and commit. Returns ``lastrowid``."""
    with conn:
        cur = conn.execute(sql, params)
    return int(cur.lastrowid or 0)


def executemany(conn: sqlite3.Connection, sql: str, seq_of_params: Sequence[Sequence[Any]]) -> int:
    """Run a batched write and commit. Returns the affected row count."""
    with conn:
        cur = conn.executemany(sql, seq_of_params)
    return int(cur.rowcount)


def query(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    """Run a SELECT and return all rows (as :class:`sqlite3.Row`)."""
    return conn.execute(sql, params).fetchall()


def query_one(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    """Run a SELECT and return the first row, or None."""
    return conn.execute(sql, params).fetchone()


def query_value(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Any:
    """Run a SELECT returning a single scalar (first column of first row), or None."""
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def backup(conn: sqlite3.Connection, dest_path: str | Path) -> None:
    """Snapshot the live database to ``dest_path`` using the SQLite backup API.

    Safe against a live WAL database; used by the nightly backup job (GDD §18).
    The destination is overwritten atomically from SQLite's point of view.
    """
    dest = Path(dest_path)
    if dest.parent and not dest.parent.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(dest))
    try:
        with target:
            conn.backup(target)
    finally:
        target.close()
    log.info("db_backup_written", dest=str(dest))
