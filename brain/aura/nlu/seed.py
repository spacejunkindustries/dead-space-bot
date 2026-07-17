"""Operator CLI: seed the ``systems`` + ``system_adjacency`` gazetteer tables
from the EVE static data export (GDD §8.1, §14).

AURA's gazetteer is *scoped at runtime* by ``gazetteer.yaml`` (see
:mod:`aura.nlu.gazetteer`). This tool fills the underlying tables the scope
rules prune from: it loads **k-space New Eden** — every non-wormhole solar
system and the jump graph between them — so a nomadic corp can point
``include_all`` or ``within_jumps_of`` at *anywhere* they relocate without
re-running the seed. Wormhole/abyssal space is dropped by default
(``--include-wormholes`` keeps it).

Source: the Fuzzwork SDE CSV mirror (EVE Online names; EVE Echoes uses the
same New Eden map). Three files are read::

    mapRegions.csv          regionID  -> regionName        (region-name join)
    mapSolarSystems.csv     regionID, solarSystemID, solarSystemName, x, y, z
    mapSolarSystemJumps.csv fromSolarSystemID <-> toSolarSystemID  (edges)

By default the three files are downloaded over HTTPS (``urllib`` honours the
standard proxy env vars); ``--systems-csv/--jumps-csv/--regions-csv`` load
local copies instead. Files are transparently bunzip'd when bz2-compressed, so
either the plain ``.csv`` or a ``.csv.bz2`` copy works.

The reload is idempotent and atomic: the whole rebuild runs in one
transaction (DELETE adjacency, DELETE systems, bulk re-INSERT) under
``PRAGMA defer_foreign_keys=ON``, so a re-run against the same SDE leaves
identical row counts — even on a live droplet whose aliases/incidents/timers
reference the gazetteer (the nomad relocate → re-seed path) — and a failure
leaves the old gazetteer intact. Runs standalone under the Brain venv on the droplet as
root — stdlib + the ``aura`` package only, and it does NOT require the service
to be running::

    /opt/aura/brain/venv/bin/python -m aura.nlu.seed --db /var/lib/aura/aura.db
"""

from __future__ import annotations

import argparse
import bz2
import csv
import io
import sqlite3
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from pathlib import Path

from aura.core import db
from aura.nlu.phonetics import double_metaphone

__all__ = ["main"]

#: regionID at or above this is wormhole (11000000+) / abyssal (12000000+)
#: space — dropped unless ``--include-wormholes``.
WORMHOLE_REGION_MIN = 11_000_000

#: Fuzzwork's uncompressed SDE CSV mirror (verified live; the sibling
#: ``.csv.bz2`` names 404 as of 2026-07, so the plain ``.csv`` is the default —
#: the reader still bunzips transparently if a bz2 copy is fed in).
FUZZWORK_BASE = "https://www.fuzzwork.co.uk/dump/latest/csv/"
REGIONS_FILE = "mapRegions.csv"
SYSTEMS_FILE = "mapSolarSystems.csv"
JUMPS_FILE = "mapSolarSystemJumps.csv"

#: SystemRow = (id, name, region, constellation, metaphone, x, y, z)
SystemRow = tuple[int, str, str, None, str, float | None, float | None, float | None]


class SeedError(Exception):
    """Download, decode, or parse failure — surfaced as a nonzero exit."""


# ── source acquisition ───────────────────────────────────────────────────────


def _maybe_bunzip(raw: bytes) -> bytes:
    """Decompress ``raw`` if it is a bz2 stream, else return it unchanged."""
    if raw[:3] == b"BZh":
        return bz2.decompress(raw)
    return raw


def _download(filename: str) -> str:
    """Fetch one SDE CSV over HTTPS. ``urllib`` honours ``HTTPS_PROXY`` etc."""
    url = FUZZWORK_BASE + filename
    try:
        with urllib.request.urlopen(url) as resp:  # noqa: S310 — fixed https host
            raw = resp.read()
    except (urllib.error.URLError, OSError) as exc:
        raise SeedError(f"download failed for {url}: {exc}") from exc
    return _maybe_bunzip(raw).decode("utf-8-sig")


def _read_local(path: str) -> str:
    """Read a local CSV (or ``.csv.bz2``), decoding with BOM tolerance."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise SeedError(f"cannot read {path}: {exc}") from exc
    return _maybe_bunzip(raw).decode("utf-8-sig")


def _rows(text: str) -> Iterator[dict[str, str]]:
    """CSV DictReader over already-decoded text."""
    yield from csv.DictReader(io.StringIO(text))


# ── parsing ──────────────────────────────────────────────────────────────────


def _float_or_none(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_regions(rows: Iterable[dict[str, str]]) -> dict[int, str]:
    """regionID -> regionName, skipping rows with no usable name."""
    out: dict[int, str] = {}
    for row in rows:
        name = (row.get("regionName") or "").strip()
        if not name:
            continue
        out[int(row["regionID"])] = name
    return out


def _parse_systems(
    rows: Iterable[dict[str, str]],
    regions: dict[int, str],
    include_wormholes: bool,
) -> list[SystemRow]:
    """Solar-system rows joined to their region name.

    Drops wormhole/abyssal regions unless ``include_wormholes``, rows whose
    name is blank, and rows whose region did not resolve.
    """
    out: list[SystemRow] = []
    for row in rows:
        region_id = int(row["regionID"])
        if not include_wormholes and region_id >= WORMHOLE_REGION_MIN:
            continue
        name = (row.get("solarSystemName") or "").strip()
        if not name:
            continue
        region = regions.get(region_id)
        if region is None:
            continue
        out.append(
            (
                int(row["solarSystemID"]),
                name,
                region,
                None,
                double_metaphone(name)[0],
                _float_or_none(row.get("x")),
                _float_or_none(row.get("y")),
                _float_or_none(row.get("z")),
            )
        )
    return out


def _parse_jumps(rows: Iterable[dict[str, str]], valid_ids: set[int]) -> list[tuple[int, int]]:
    """Unordered, deduped ``(a_id, b_id)`` edges, both endpoints in ``valid_ids``.

    Each undirected edge is normalised to ``(min, max)`` so the two directed
    rows the SDE carries collapse to one, and edges touching a dropped system
    (e.g. a wormhole endpoint) are discarded.
    """
    pairs: set[tuple[int, int]] = set()
    for row in rows:
        a = int(row["fromSolarSystemID"])
        b = int(row["toSolarSystemID"])
        if a == b or a not in valid_ids or b not in valid_ids:
            continue
        pairs.add((a, b) if a < b else (b, a))
    return sorted(pairs)


# ── database reload ──────────────────────────────────────────────────────────


def _reload(
    conn: sqlite3.Connection,
    systems: list[SystemRow],
    pairs: list[tuple[int, int]],
) -> None:
    """Atomically replace the gazetteer tables in one transaction.

    ``PRAGMA defer_foreign_keys=ON`` holds FK enforcement until COMMIT so the
    ``DELETE FROM systems`` is allowed even while other tables (aliases,
    incidents, timers, personal_pings, command_log) still reference the old
    rows — the same rows are re-inserted under their stable EVE IDs before the
    transaction commits, so no reference is ever left dangling. Adjacency is
    deleted first (it is rebuilt wholesale) and re-inserted after systems; a
    failure rolls back to the previous gazetteer. The pragma is reset
    automatically at COMMIT/ROLLBACK, so it must be re-set per transaction.
    """
    try:
        conn.execute("BEGIN")
        conn.execute("PRAGMA defer_foreign_keys=ON")
        conn.execute("DELETE FROM system_adjacency")
        conn.execute("DELETE FROM systems")
        conn.executemany(
            "INSERT INTO systems (id, name, region, constellation, metaphone, x, y, z)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            systems,
        )
        conn.executemany("INSERT INTO system_adjacency (a_id, b_id) VALUES (?, ?)", pairs)
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise SeedError(f"database reload failed: {exc}") from exc


# ── CLI ──────────────────────────────────────────────────────────────────────


def _acquire(args: argparse.Namespace) -> tuple[str, str, str]:
    """Return (regions_text, systems_text, jumps_text) from local files or HTTPS."""
    local = (args.systems_csv, args.jumps_csv, args.regions_csv)
    if any(local):
        if not all(local):
            raise SeedError("--systems-csv, --jumps-csv and --regions-csv must be given together")
        return (
            _read_local(args.regions_csv),
            _read_local(args.systems_csv),
            _read_local(args.jumps_csv),
        )
    print(f"downloading SDE from {FUZZWORK_BASE} ...")
    return (
        _download(REGIONS_FILE),
        _download(SYSTEMS_FILE),
        _download(JUMPS_FILE),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aura.nlu.seed",
        description="Seed the AURA gazetteer tables from the EVE SDE (GDD §8.1).",
    )
    parser.add_argument(
        "--db", required=True, metavar="PATH", help="path to aura.db (e.g. /var/lib/aura/aura.db)"
    )
    parser.add_argument(
        "--source",
        default="fuzzwork",
        choices=["fuzzwork"],
        help="download source (default: fuzzwork)",
    )
    parser.add_argument("--systems-csv", metavar="F", help="local mapSolarSystems CSV")
    parser.add_argument("--jumps-csv", metavar="F", help="local mapSolarSystemJumps CSV")
    parser.add_argument("--regions-csv", metavar="F", help="local mapRegions CSV")
    parser.add_argument(
        "--include-wormholes",
        action="store_true",
        help="keep wormhole/abyssal regions (regionID >= 11000000); dropped by default",
    )
    args = parser.parse_args(argv)

    try:
        regions_text, systems_text, jumps_text = _acquire(args)
        regions = _parse_regions(_rows(regions_text))
        systems = _parse_systems(_rows(systems_text), regions, args.include_wormholes)
        valid_ids = {row[0] for row in systems}
        pairs = _parse_jumps(_rows(jumps_text), valid_ids)
    except (SeedError, csv.Error, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not systems:
        print(
            "error: no systems parsed from the SDE — refusing to wipe the gazetteer",
            file=sys.stderr,
        )
        return 1

    conn = db.connect(args.db)
    try:
        db.migrate(conn)
        _reload(conn, systems, pairs)
    except SeedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(f"regions: {len(regions)}")
    print(f"systems: {len(systems)}")
    print(f"jumps:   {len(pairs)}")
    names = {row[1] for row in systems}
    missing = [hub for hub in ("Jita", "Amarr") if hub not in names]
    for hub in ("Jita", "Amarr"):
        print(f"sanity: {hub} {'present' if hub not in missing else 'MISSING'}")
    if missing:
        print(f"error: expected hubs missing from seed: {', '.join(missing)}", file=sys.stderr)
        return 1
    print(f"done — gazetteer seeded into {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
