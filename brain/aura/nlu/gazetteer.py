"""Constrained gazetteer — GDD §8.1.

AURA does NOT load New Eden. It loads the corp's operational area — roughly
100–500 systems (constraint 8: the small gazetteer IS the accuracy decision,
not a shortcut). The ``systems`` and ``system_adjacency`` tables are seeded
from the EVE SDE once at install time; this module prunes that set at runtime
by the scope rules in ``gazetteer.yaml`` (editable by the FC, because corps
move):

- ``regions``: regions included wholesale
- ``within_jumps_of``: everything within N jumps of an anchor system
- ``always_include``: the trade hubs pilots name anyway
- ``exclude``: dropped even when a rule above matched

``load()`` is blocking (sqlite + YAML + BFS) — callers run it via
``asyncio.to_thread``. It builds the whole new state locally and swaps it in
with single attribute assignments, so concurrent readers in other threads
always see a coherent snapshot; ``/gazetteer reload`` reuses the same path.

Jump distances (:meth:`Gazetteer.jumps`) run BFS over the FULL adjacency
graph (pruning is about what can be *named*, not how space is shaped) and
memoise the distance map per source system.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from pathlib import Path
from typing import Any

import structlog
import yaml

from aura.config import GazetteerConfig
from aura.core import db
from aura.nlu.phonetics import double_metaphone
from aura.types import SystemEntry

__all__ = ["Gazetteer", "GazetteerError"]

log = structlog.get_logger(__name__)

#: Character budget for the Whisper ``initial_prompt`` bias text. Whisper
#: keeps roughly the last 224 tokens of the prompt, so the text is ordered
#: home-first and truncated on a name boundary.
PROMPT_BIAS_MAX_CHARS = 900


class GazetteerError(Exception):
    """gazetteer.yaml is missing, unreadable, or structurally invalid."""


def _str_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise GazetteerError(f"gazetteer.yaml: {key} must be a list of strings")
    return value


def _load_scope(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GazetteerError(f"cannot read gazetteer file {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise GazetteerError(f"invalid YAML in {path}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise GazetteerError(f"{path}: top level must be a mapping")

    scope: dict[str, Any] = {
        "regions": _str_list(data, "regions"),
        "always_include": _str_list(data, "always_include"),
        "exclude": _str_list(data, "exclude"),
        "within_jumps_of": None,
    }
    wjo = data.get("within_jumps_of")
    if wjo is not None:
        if (
            not isinstance(wjo, dict)
            or not isinstance(wjo.get("system"), str)
            or isinstance(wjo.get("jumps"), bool)
            or not isinstance(wjo.get("jumps"), int)
            or wjo["jumps"] < 0
        ):
            raise GazetteerError(
                "gazetteer.yaml: within_jumps_of must be {system: <name>, jumps: <int ≥ 0>}"
            )
        scope["within_jumps_of"] = (wjo["system"], wjo["jumps"])
    return scope


class Gazetteer:
    """The pruned active system set + adjacency graph with memoised BFS."""

    def __init__(self, conn: sqlite3.Connection, cfg: GazetteerConfig) -> None:
        self._conn = conn
        self._cfg = cfg
        self._systems: tuple[SystemEntry, ...] = ()
        self._by_id: dict[int, SystemEntry] = {}
        self._by_name: dict[str, SystemEntry] = {}
        self._adjacency: dict[int, tuple[int, ...]] = {}
        self._dist_memo: dict[int, dict[int, int]] = {}
        self._parent_memo: dict[int, dict[int, int]] = {}
        self._all_names: dict[int, str] = {}
        self._home_system_id: int | None = None
        self._prompt_bias: str = ""

    # ── loading (blocking — call via asyncio.to_thread) ──────────────────────

    def load(self) -> None:
        """(Re)build the active set from gazetteer.yaml scope rules + the db.

        Raises :class:`GazetteerError` on a bad scope file; on failure the
        previously loaded state stays in force.
        """
        scope = _load_scope(Path(self._cfg.file))

        rows = db.query(
            self._conn, "SELECT id, name, region, constellation FROM systems ORDER BY name"
        )
        all_by_id: dict[int, SystemEntry] = {}
        all_by_name: dict[str, SystemEntry] = {}
        for row in rows:
            entry = SystemEntry(
                id=row["id"],
                name=row["name"],
                region=row["region"],
                constellation=row["constellation"],
                metaphone=double_metaphone(row["name"])[0],
            )
            all_by_id[entry.id] = entry
            all_by_name[entry.name.lower()] = entry

        adjacency: dict[int, list[int]] = {}
        for row in db.query(self._conn, "SELECT a_id, b_id FROM system_adjacency"):
            adjacency.setdefault(row["a_id"], []).append(row["b_id"])
            adjacency.setdefault(row["b_id"], []).append(row["a_id"])
        adj: dict[int, tuple[int, ...]] = {k: tuple(v) for k, v in adjacency.items()}

        active: set[int] = set()

        regions = set(scope["regions"])
        if regions:
            active.update(e.id for e in all_by_id.values() if e.region in regions)

        if scope["within_jumps_of"] is not None:
            anchor_name, max_jumps = scope["within_jumps_of"]
            anchor = all_by_name.get(anchor_name.lower())
            if anchor is None:
                raise GazetteerError(
                    f"gazetteer.yaml: within_jumps_of.system {anchor_name!r} "
                    "is not in the systems table"
                )
            active.update(_bfs_within(anchor.id, max_jumps, adj))

        for name in scope["always_include"]:
            entry = all_by_name.get(name.lower())
            if entry is None:
                log.warning("gazetteer_always_include_unknown", name=name)
            else:
                active.add(entry.id)

        home = all_by_name.get(self._cfg.home_system.lower())
        if home is None:
            log.warning("gazetteer_home_system_unknown", name=self._cfg.home_system)
        else:
            active.add(home.id)

        for name in scope["exclude"]:
            entry = all_by_name.get(name.lower())
            if entry is not None:
                active.discard(entry.id)
                if home is not None and entry.id == home.id:
                    log.warning("gazetteer_home_system_excluded", name=entry.name)
                    home = None

        systems = tuple(sorted((all_by_id[i] for i in active), key=lambda e: e.name.lower()))
        if not systems:
            log.warning("gazetteer_empty_active_set", file=self._cfg.file)
        if len(systems) > 500:
            # Constraint 8: the gazetteer stays small ON PURPOSE. A huge set
            # is an FC misconfiguration worth shouting about, not an upgrade.
            log.warning("gazetteer_oversized", count=len(systems), recommended_max=500)

        self._persist_metaphones(systems)

        # Swap the whole snapshot in; each assignment is atomic under the GIL
        # and readers only ever traverse one structure per call.
        self._by_id = {e.id: e for e in systems}
        self._by_name = {e.name.lower(): e for e in systems}
        self._adjacency = adj
        self._dist_memo = {}
        self._parent_memo = {}
        self._all_names = {e.id: e.name for e in all_by_id.values()}
        self._home_system_id = home.id if home is not None else None
        self._systems = systems
        self._prompt_bias = _build_prompt_bias(systems, self._home_system_id)
        log.info(
            "gazetteer_loaded",
            active=len(systems),
            regions=sorted(regions),
            home=self._cfg.home_system,
        )

    def _persist_metaphones(self, systems: tuple[SystemEntry, ...]) -> None:
        """Keep the ``systems.metaphone`` column in step with the in-repo
        encoder ("precomputed at load", GDD §14) so its index stays usable."""
        stale = [
            (e.metaphone, e.id)
            for e in systems
            if db.query_value(self._conn, "SELECT metaphone FROM systems WHERE id = ?", (e.id,))
            != e.metaphone
        ]
        if stale:
            db.executemany(self._conn, "UPDATE systems SET metaphone = ? WHERE id = ?", stale)
            log.info("gazetteer_metaphones_refreshed", count=len(stale))

    # ── read API (cheap; safe from any thread) ───────────────────────────────

    @property
    def systems(self) -> tuple[SystemEntry, ...]:
        """The pruned active set, sorted by name."""
        return self._systems

    @property
    def home_system_id(self) -> int | None:
        return self._home_system_id

    def by_id(self, system_id: int) -> SystemEntry | None:
        return self._by_id.get(system_id)

    def by_name(self, name: str) -> SystemEntry | None:
        """Case-insensitive exact name lookup within the active set."""
        return self._by_name.get(name.strip().lower())

    def jumps(self, a_id: int, b_id: int) -> int | None:
        """Jump distance over the full adjacency graph; None if disconnected.

        One BFS per source system, memoised until the next :meth:`load`.
        """
        if a_id == b_id:
            return 0
        for src, dst in ((a_id, b_id), (b_id, a_id)):
            memo = self._dist_memo.get(src)
            if memo is not None:
                return memo.get(dst)
        distances = _bfs_distances(a_id, self._adjacency)
        self._dist_memo[a_id] = distances
        return distances.get(b_id)

    def path(self, a_id: int, b_id: int) -> tuple[int, ...] | None:
        """Shortest jump path a→b, both endpoints included; None if disconnected.

        Like :meth:`jumps`, this runs over the FULL adjacency graph, so the
        path may pass through systems pruned from the active set (use
        :meth:`system_name` to render them). Same memo style as ``jumps``:
        one BFS parent map per source system, kept until the next
        :meth:`load`.
        """
        if a_id == b_id:
            return (a_id,)
        parents = self._parent_memo.get(a_id)
        if parents is None:
            parents = _bfs_parents(a_id, self._adjacency)
            self._parent_memo[a_id] = parents
        if b_id not in parents:
            return None
        out = [b_id]
        node = b_id
        while node != a_id:
            node = parents[node]
            out.append(node)
        out.reverse()
        return tuple(out)

    def system_name(self, system_id: int) -> str | None:
        """Name lookup over the FULL systems table, not just the active set.

        Route rendering needs names for pruned waypoint systems; everything
        that must stay scoped to the active set uses :meth:`by_id` instead.
        """
        return self._all_names.get(system_id)

    def prompt_bias_text(self) -> str:
        """System names for the Whisper ``initial_prompt`` (GDD §5.3)."""
        return self._prompt_bias


def _bfs_within(start: int, max_jumps: int, adj: dict[int, tuple[int, ...]]) -> set[int]:
    reached = {start}
    frontier = deque([(start, 0)])
    while frontier:
        node, depth = frontier.popleft()
        if depth == max_jumps:
            continue
        for neighbour in adj.get(node, ()):
            if neighbour not in reached:
                reached.add(neighbour)
                frontier.append((neighbour, depth + 1))
    return reached


def _bfs_parents(start: int, adj: dict[int, tuple[int, ...]]) -> dict[int, int]:
    """BFS parent pointers from ``start``; the start maps to itself."""
    parents: dict[int, int] = {start: start}
    frontier = deque([start])
    while frontier:
        node = frontier.popleft()
        for neighbour in adj.get(node, ()):
            if neighbour not in parents:
                parents[neighbour] = node
                frontier.append(neighbour)
    return parents


def _bfs_distances(start: int, adj: dict[int, tuple[int, ...]]) -> dict[int, int]:
    distances = {start: 0}
    frontier = deque([start])
    while frontier:
        node = frontier.popleft()
        for neighbour in adj.get(node, ()):
            if neighbour not in distances:
                distances[neighbour] = distances[node] + 1
                frontier.append(neighbour)
    return distances


def _build_prompt_bias(systems: tuple[SystemEntry, ...], home_id: int | None) -> str:
    """Home system first (Whisper truncates the head of long prompts, but we
    cap well under its window so ordering is about salience), then the rest
    alphabetically until the character budget runs out."""
    names: list[str] = []
    if home_id is not None:
        for entry in systems:
            if entry.id == home_id:
                names.append(entry.name)
                break
    names.extend(e.name for e in systems if not (home_id is not None and e.id == home_id))
    out: list[str] = []
    used = len("Systems: ")
    for name in names:
        extra = len(name) + (2 if out else 0)
        if used + extra > PROMPT_BIAS_MAX_CHARS:
            break
        out.append(name)
        used += extra
    if not out:
        return ""
    return "Systems: " + ", ".join(out)
