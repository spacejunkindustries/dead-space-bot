"""Gazetteer tests — GDD §8.1. In-memory sqlite + a scope file in tmp_path
(config YAML on disk is fine; audio never touches disk — constraint 5)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cortana.config import GazetteerConfig
from cortana.core import db
from cortana.nlu.gazetteer import Gazetteer, GazetteerError

# A small universe: two regions plus a far-away hub chain.
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


def write_scope(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "gazetteer.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def make_gazetteer(
    conn: sqlite3.Connection, tmp_path: Path, scope_yaml: str, home: str = "Otanuomi"
) -> Gazetteer:
    path = write_scope(tmp_path, scope_yaml)
    gaz = Gazetteer(conn, GazetteerConfig(file=str(path), home_system=home))
    gaz.load()
    return gaz


# ── scope rules ──────────────────────────────────────────────────────────────


def test_region_allowlist(conn: sqlite3.Connection, tmp_path: Path) -> None:
    gaz = make_gazetteer(conn, tmp_path, "regions:\n  - Home-Region\n")
    names = {e.name for e in gaz.systems}
    assert names == {"Otanuomi", "Kisogo", "Tannolen"}


def test_within_jumps_of(conn: sqlite3.Connection, tmp_path: Path) -> None:
    gaz = make_gazetteer(conn, tmp_path, "within_jumps_of:\n  system: Otanuomi\n  jumps: 2\n")
    names = {e.name for e in gaz.systems}
    assert names == {"Otanuomi", "Kisogo", "Alenia", "Tannolen"}


def test_always_include_and_exclude(conn: sqlite3.Connection, tmp_path: Path) -> None:
    gaz = make_gazetteer(
        conn,
        tmp_path,
        "regions:\n  - Home-Region\nalways_include:\n  - Jita\nexclude:\n  - Tannolen\n",
    )
    names = {e.name for e in gaz.systems}
    assert "Jita" in names
    assert "Tannolen" not in names


def test_exclude_wins_over_include(conn: sqlite3.Connection, tmp_path: Path) -> None:
    gaz = make_gazetteer(
        conn,
        tmp_path,
        "regions:\n  - Home-Region\nalways_include:\n  - Kisogo\nexclude:\n  - Kisogo\n",
    )
    assert gaz.by_name("Kisogo") is None


def test_home_system_always_in_active_set(conn: sqlite3.Connection, tmp_path: Path) -> None:
    gaz = make_gazetteer(conn, tmp_path, "regions:\n  - The Forge\n")
    assert gaz.home_system_id == 1
    assert gaz.by_name("Otanuomi") is not None


def test_unknown_home_system_is_none(conn: sqlite3.Connection, tmp_path: Path) -> None:
    gaz = make_gazetteer(conn, tmp_path, "regions:\n  - Home-Region\n", home="Nowhere")
    assert gaz.home_system_id is None


# ── include_all (nomadic) mode ───────────────────────────────────────────────


def test_include_all_active_set_is_all_seeded_minus_exclude(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    gaz = make_gazetteer(conn, tmp_path, "include_all: true\nexclude:\n  - Jita\n")
    names = {e.name for e in gaz.systems}
    # Every seeded system is active except the excluded one — regions/
    # within_jumps_of no longer narrow it.
    assert names == {"Otanuomi", "Kisogo", "Alenia", "Hulmate", "Tannolen"}
    assert "Jita" not in names


def test_include_all_ignores_regions_and_within_jumps(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    gaz = make_gazetteer(
        conn,
        tmp_path,
        "include_all: true\nregions:\n  - Home-Region\n",
    )
    # The Home-Region-only allowlist would exclude Alenia/Hulmate/Jita in
    # scoped mode; include_all activates the whole map regardless.
    names = {e.name for e in gaz.systems}
    assert names == {"Otanuomi", "Kisogo", "Alenia", "Hulmate", "Jita", "Tannolen"}


def test_include_all_null_within_jumps_does_not_raise(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # No within_jumps_of anchor at all: scoped mode would be fine, but the key
    # point is include_all never needs one and never raises the missing-anchor
    # error even when the corp has relocated away from any configured anchor.
    gaz = make_gazetteer(conn, tmp_path, "include_all: true\n", home="Nowhere")
    assert len(gaz.systems) == 6
    assert gaz.home_system_id is None


def test_include_all_config_flag_also_activates(conn: sqlite3.Connection, tmp_path: Path) -> None:
    # include_all can be switched on from cortana.yaml (GazetteerConfig) too.
    path = write_scope(tmp_path, "regions:\n  - Home-Region\n")
    from cortana.config import GazetteerConfig

    gaz = Gazetteer(conn, GazetteerConfig(file=str(path), home_system="Otanuomi", include_all=True))
    gaz.load()
    assert len(gaz.systems) == 6


# ── nullable home (nomadic) ──────────────────────────────────────────────────


def test_null_home_system_disables_bias(conn: sqlite3.Connection, tmp_path: Path) -> None:
    path = write_scope(tmp_path, "regions:\n  - Home-Region\n")
    from cortana.config import GazetteerConfig

    gaz = Gazetteer(conn, GazetteerConfig(file=str(path), home_system=None))
    gaz.load()
    assert gaz.home_system_id is None  # home-bias prior inactive, no crash
    names = {e.name for e in gaz.systems}
    assert names == {"Otanuomi", "Kisogo", "Tannolen"}


# ── empty-table ergonomics ───────────────────────────────────────────────────


def test_empty_systems_table_actionable_error(tmp_path: Path) -> None:
    empty = db.connect(":memory:")
    db.migrate(empty)  # schema present, but no systems rows seeded
    path = write_scope(tmp_path, "regions:\n  - Home-Region\n")
    from cortana.config import GazetteerConfig

    gaz = Gazetteer(empty, GazetteerConfig(file=str(path), home_system="Otanuomi"))
    with pytest.raises(GazetteerError) as excinfo:
        gaz.load()
    assert str(excinfo.value) == (
        "systems table is empty — seed it with: "
        "/opt/cortana/brain/venv/bin/python -m cortana.nlu.seed --db /var/lib/cortana/cortana.db"
    )


# ── lookups ──────────────────────────────────────────────────────────────────


def test_by_name_case_insensitive(conn: sqlite3.Connection, tmp_path: Path) -> None:
    gaz = make_gazetteer(conn, tmp_path, "regions:\n  - Home-Region\n")
    assert gaz.by_name("kisogo") is not None
    assert gaz.by_name("  KISOGO ") is not None
    assert gaz.by_name("Jita") is None  # pruned out


def test_by_id_scoped_to_active_set(conn: sqlite3.Connection, tmp_path: Path) -> None:
    gaz = make_gazetteer(conn, tmp_path, "regions:\n  - Home-Region\n")
    assert gaz.by_id(2) is not None
    assert gaz.by_id(5) is None


def test_metaphone_precomputed_at_load(conn: sqlite3.Connection, tmp_path: Path) -> None:
    gaz = make_gazetteer(conn, tmp_path, "regions:\n  - Home-Region\n")
    entry = gaz.by_name("Kisogo")
    assert entry is not None
    assert entry.metaphone == "KSK"
    stored = db.query_value(conn, "SELECT metaphone FROM systems WHERE id = 2")
    assert stored == "KSK"


# ── jumps BFS ────────────────────────────────────────────────────────────────


def test_jumps_bfs_and_memo(conn: sqlite3.Connection, tmp_path: Path) -> None:
    gaz = make_gazetteer(conn, tmp_path, "regions:\n  - Home-Region\n")
    assert gaz.jumps(1, 1) == 0
    assert gaz.jumps(1, 2) == 1
    assert gaz.jumps(1, 5) == 4  # full graph, even though Jita is pruned
    assert gaz.jumps(5, 1) == 4  # memoised reverse lookup
    assert gaz.jumps(6, 5) == 5


def test_jumps_disconnected_is_none(conn: sqlite3.Connection, tmp_path: Path) -> None:
    db.execute(
        conn,
        "INSERT INTO systems (id, name, region, constellation, metaphone)"
        " VALUES (7, 'Island', 'Home-Region', NULL, '')",
    )
    gaz = make_gazetteer(conn, tmp_path, "regions:\n  - Home-Region\n")
    assert gaz.jumps(1, 7) is None


# ── prompt bias ──────────────────────────────────────────────────────────────


def test_prompt_bias_home_first_and_bounded(conn: sqlite3.Connection, tmp_path: Path) -> None:
    gaz = make_gazetteer(conn, tmp_path, "regions:\n  - Home-Region\n")
    text = gaz.prompt_bias_text()
    assert text.startswith("Systems: Otanuomi")
    assert "Kisogo" in text
    assert len(text) <= 1000


def test_prompt_bias_empty_gazetteer(conn: sqlite3.Connection, tmp_path: Path) -> None:
    gaz = make_gazetteer(conn, tmp_path, "regions: []\n", home="Nowhere")
    assert gaz.prompt_bias_text() == ""
    assert gaz.systems == ()


# ── error handling ───────────────────────────────────────────────────────────


def test_missing_scope_file_raises(conn: sqlite3.Connection, tmp_path: Path) -> None:
    gaz = Gazetteer(conn, GazetteerConfig(file=str(tmp_path / "nope.yaml"), home_system="X"))
    with pytest.raises(GazetteerError):
        gaz.load()


def test_bad_scope_shape_raises(conn: sqlite3.Connection, tmp_path: Path) -> None:
    path = write_scope(tmp_path, "regions: notalist\n")
    gaz = Gazetteer(conn, GazetteerConfig(file=str(path), home_system="Otanuomi"))
    with pytest.raises(GazetteerError, match="regions"):
        gaz.load()


def test_bad_within_jumps_raises(conn: sqlite3.Connection, tmp_path: Path) -> None:
    path = write_scope(tmp_path, "within_jumps_of:\n  system: Otanuomi\n  jumps: nope\n")
    gaz = Gazetteer(conn, GazetteerConfig(file=str(path), home_system="Otanuomi"))
    with pytest.raises(GazetteerError, match="within_jumps_of"):
        gaz.load()


def test_unknown_anchor_raises(conn: sqlite3.Connection, tmp_path: Path) -> None:
    path = write_scope(tmp_path, "within_jumps_of:\n  system: Nowhere\n  jumps: 2\n")
    gaz = Gazetteer(conn, GazetteerConfig(file=str(path), home_system="Otanuomi"))
    with pytest.raises(GazetteerError, match="Nowhere"):
        gaz.load()


def test_failed_reload_keeps_previous_state(conn: sqlite3.Connection, tmp_path: Path) -> None:
    path = write_scope(tmp_path, "regions:\n  - Home-Region\n")
    gaz = Gazetteer(conn, GazetteerConfig(file=str(path), home_system="Otanuomi"))
    gaz.load()
    before = gaz.systems
    path.write_text("regions: broken\n", encoding="utf-8")
    with pytest.raises(GazetteerError):
        gaz.load()
    assert gaz.systems == before
