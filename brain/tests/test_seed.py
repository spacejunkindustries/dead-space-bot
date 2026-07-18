"""Seed CLI tests — GDD §8.1 / §14. Small in-repo fixture CSVs, NO network:
the parse helpers and the atomic reload are exercised against local files
written into tmp_path (config/data on disk is fine; only *audio* never
touches disk — constraint 5)."""

from __future__ import annotations

from pathlib import Path

from cortana.core import db
from cortana.nlu import seed
from cortana.nlu.phonetics import double_metaphone

# Two k-space regions and one wormhole region (regionID >= 11_000_000).
REGIONS_CSV = (
    '"regionID","regionName"\n"10000001","Derelik"\n"10000002","The Forge"\n"11000001","A-R00001"\n'
)

# columns mirror mapSolarSystems.csv (only a subset is read).
SYSTEMS_CSV = (
    '"regionID","constellationID","solarSystemID","solarSystemName","x","y","z"\n'
    '"10000002","20000001","30000142","Jita","1.0","2.0","3.0"\n'
    '"10000001","20000002","30002187","Amarr","4.0","5.0","6.0"\n'
    '"10000001","20000002","30000003","Otanuomi","7.0","8.0","9.0"\n'
    '"10000001","20000002","30000004","","0","0","0"\n'  # blank name → dropped
    '"11000001","21000001","31000001","J123456","0","0","0"\n'  # wormhole
)

# columns mirror mapSolarSystemJumps.csv. Jita<->Amarr appears in both
# directions; Otanuomi<->J123456 crosses into wormhole space.
JUMPS_CSV = (
    '"fromRegionID","fromConstellationID","fromSolarSystemID",'
    '"toSolarSystemID","toConstellationID","toRegionID"\n'
    '"10000002","20000001","30000142","30002187","20000002","10000001"\n'
    '"10000001","20000002","30002187","30000142","20000001","10000002"\n'
    '"10000001","20000002","30002187","30000003","20000002","10000001"\n'
    '"10000001","20000002","30000003","31000001","21000001","11000001"\n'
)


def _regions() -> dict[int, str]:
    return seed._parse_regions(seed._rows(REGIONS_CSV))


# ── parse helpers ─────────────────────────────────────────────────────────────


def test_region_name_join() -> None:
    regions = _regions()
    systems = seed._parse_systems(seed._rows(SYSTEMS_CSV), regions, include_wormholes=False)
    by_name = {row[1]: row for row in systems}
    assert by_name["Jita"][2] == "The Forge"  # region resolved via regionID
    assert by_name["Amarr"][2] == "Derelik"


def test_kspace_filter_drops_wormholes_and_blank_names() -> None:
    regions = _regions()
    systems = seed._parse_systems(seed._rows(SYSTEMS_CSV), regions, include_wormholes=False)
    names = {row[1] for row in systems}
    assert names == {"Jita", "Amarr", "Otanuomi"}
    assert "J123456" not in names  # wormhole region dropped
    assert "" not in names  # blank name dropped


def test_wormhole_opt_in() -> None:
    regions = _regions()
    systems = seed._parse_systems(seed._rows(SYSTEMS_CSV), regions, include_wormholes=True)
    by_name = {row[1]: row for row in systems}
    assert "J123456" in by_name
    assert by_name["J123456"][2] == "A-R00001"


def test_unordered_pair_dedupe() -> None:
    regions = _regions()
    systems = seed._parse_systems(seed._rows(SYSTEMS_CSV), regions, include_wormholes=False)
    valid = {row[0] for row in systems}
    pairs = seed._parse_jumps(seed._rows(JUMPS_CSV), valid)
    # Jita<->Amarr (both directions) collapses to ONE (min,max) pair;
    # Amarr<->Otanuomi is the other. The wormhole jump is excluded.
    assert set(pairs) == {(30000142, 30002187), (30000003, 30002187)}
    assert len(pairs) == 2  # no duplicate from the reversed direction
    assert all(a < b for a, b in pairs)  # normalised to (min, max)
    assert pairs == sorted(pairs)  # returned sorted for stable inserts


def test_both_endpoints_required_for_jump() -> None:
    regions = _regions()
    kspace = {row[0] for row in seed._parse_systems(seed._rows(SYSTEMS_CSV), regions, False)}
    pairs = seed._parse_jumps(seed._rows(JUMPS_CSV), kspace)
    # Otanuomi(30000003)<->J123456(31000001) dropped: J123456 not seeded.
    assert (30000003, 31000001) not in pairs
    assert not any(31000001 in pair for pair in pairs)

    withwh = {row[0] for row in seed._parse_systems(seed._rows(SYSTEMS_CSV), regions, True)}
    pairs_wh = seed._parse_jumps(seed._rows(JUMPS_CSV), withwh)
    assert (30000003, 31000001) in pairs_wh  # now both endpoints exist


# ── full CLI reload ───────────────────────────────────────────────────────────


def _write_fixtures(tmp_path: Path) -> tuple[str, str, str]:
    reg = tmp_path / "regions.csv"
    sysf = tmp_path / "systems.csv"
    jmp = tmp_path / "jumps.csv"
    # utf-8-sig writes a real BOM, exercising _read_local's BOM-tolerant decode
    # (Fuzzwork's mapRegions.csv ships with one).
    reg.write_text(REGIONS_CSV, encoding="utf-8-sig")
    sysf.write_text(SYSTEMS_CSV, encoding="utf-8")
    jmp.write_text(JUMPS_CSV, encoding="utf-8")
    return str(sysf), str(jmp), str(reg)


def _run_seed(tmp_path: Path, db_path: Path) -> int:
    systems_csv, jumps_csv, regions_csv = _write_fixtures(tmp_path)
    return seed.main(
        [
            "--db",
            str(db_path),
            "--systems-csv",
            systems_csv,
            "--jumps-csv",
            jumps_csv,
            "--regions-csv",
            regions_csv,
        ]
    )


def test_cli_seeds_metaphone_and_coords(tmp_path: Path) -> None:
    db_path = tmp_path / "cortana.db"
    assert _run_seed(tmp_path, db_path) == 0

    conn = db.connect(db_path)
    try:
        row = db.query_one(
            conn, "SELECT name, region, metaphone, x, y, z FROM systems WHERE id=?", (30000142,)
        )
        assert row is not None
        assert row["name"] == "Jita"
        assert row["region"] == "The Forge"
        assert row["metaphone"] == double_metaphone("Jita")[0]  # precomputed at seed
        assert row["x"] == 1.0 and row["z"] == 3.0
        assert db.query_value(conn, "SELECT COUNT(*) FROM systems") == 3
        assert db.query_value(conn, "SELECT COUNT(*) FROM system_adjacency") == 2
    finally:
        conn.close()


def test_cli_idempotent_rerun(tmp_path: Path) -> None:
    db_path = tmp_path / "cortana.db"
    assert _run_seed(tmp_path, db_path) == 0
    conn = db.connect(db_path)
    first_sys = db.query_value(conn, "SELECT COUNT(*) FROM systems")
    first_adj = db.query_value(conn, "SELECT COUNT(*) FROM system_adjacency")
    conn.close()

    assert _run_seed(tmp_path, db_path) == 0  # re-run against the same SDE
    conn = db.connect(db_path)
    try:
        assert db.query_value(conn, "SELECT COUNT(*) FROM systems") == first_sys
        assert db.query_value(conn, "SELECT COUNT(*) FROM system_adjacency") == first_adj
    finally:
        conn.close()


def test_cli_reseed_with_referencing_rows(tmp_path: Path) -> None:
    """Nomad relocate → re-seed on a live DB: rows in OTHER tables reference
    systems(id). With ``foreign_keys=ON`` a naive ``DELETE FROM systems`` is
    rejected by those child FKs; the deferred-FK reload must succeed and keep
    the referencing rows intact."""
    db_path = tmp_path / "cortana.db"
    assert _run_seed(tmp_path, db_path) == 0

    # Simulate a live droplet: an operator correction (alias) and an open
    # incident, both pointing at a seeded system (Jita).
    conn = db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO aliases (raw_text, system_id, weight, learned_at, corrected_by)"
            " VALUES (?, ?, ?, ?, ?)",
            ("jeetah", 30000142, 1.0, "2026-07-17T00:00:00Z", 42),
        )
        conn.execute(
            "INSERT INTO incidents"
            " (guild_id, system_id, type, severity, reporter_id, opened_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, 30000142, "GATE_CAMP", "HIGH", 7, "2026-07-17T00:00:00Z", "2026-07-17T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    # Re-run the identical seed — must NOT abort on FOREIGN KEY constraint.
    assert _run_seed(tmp_path, db_path) == 0

    conn = db.connect(db_path)
    try:
        assert db.query_value(conn, "SELECT COUNT(*) FROM systems") == 3
        assert db.query_value(conn, "SELECT COUNT(*) FROM system_adjacency") == 2
        # referencing rows survived and still resolve to the re-seeded system
        assert db.query_value(conn, "SELECT COUNT(*) FROM aliases") == 1
        assert (
            db.query_value(conn, "SELECT system_id FROM incidents WHERE reporter_id=7") == 30000142
        )
        assert db.query_value(conn, "PRAGMA foreign_key_check") is None
    finally:
        conn.close()


def test_cli_local_requires_all_three(tmp_path: Path, capsys) -> None:
    # Only one of the three local paths given → clean nonzero exit, no wipe.
    rc = seed.main(["--db", str(tmp_path / "cortana.db"), "--systems-csv", "x.csv"])
    assert rc != 0
    assert "must be given together" in capsys.readouterr().err
