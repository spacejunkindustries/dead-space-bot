"""Item id / localized-name index for the Albion killboard market layer.

The Albion Online item database (``data/items.txt``, sourced from
``ao-data/ao-bin-dumps``) maps every unique item id to its localized English
name. The market commands need to go both ways: a slash-command user types a
human name ("holy staff") and we must resolve it to the id the AODP price API
speaks (``T8_2H_HOLYSTAFF``); autocomplete must offer id/name pairs as they type;
and a card rendering a price needs the display name for an id.

:class:`ItemIndex` owns that mapping. The file is loaded and parsed exactly once,
off the event loop via :func:`asyncio.to_thread` (constraint: blocking work never
runs on the loop). The query methods — :meth:`~ItemIndex.resolve`,
:meth:`~ItemIndex.search`, :meth:`~ItemIndex.name_of`, :meth:`~ItemIndex.tier_of`
— are pure and synchronous so they are trivially testable. ``name_of``/``tier_of``
are O(1) dict/regex lookups; ``resolve``/``search`` fuzzy-scan all ~5k base
entries (tens of ms), so callers on the shared loop (autocomplete, command
handlers) must dispatch them via :func:`asyncio.to_thread` rather than call them
inline. A missing or unreadable file degrades to an empty index: ``resolve``
returns ``None``, ``search`` returns ``[]``, nothing raises.

Matching is phonetically naive but structurally aware: names carry a tier
adjective ("Elder's", "Adept's") that we strip to a *core* name so "holy staff"
matches every tier at once, while a tier hint in the query ("t8", "elder")
re-biases toward one tier. Enchant levels (``@1``..``@3``) are split off the
query, matched against the base id, and reattached to the result.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# Localized tier adjectives → tier number. These prefix every item name
# ("Elder's Great Holy Staff") and are stripped to a core name for fuzzy
# matching; a bare tier word in a query is also read as a tier hint.
TIER_WORDS: dict[str, int] = {
    "beginner": 1,
    "novice": 2,
    "journeyman": 3,
    "adept": 4,
    "expert": 5,
    "master": 6,
    "grandmaster": 7,
    "elder": 8,
}

_ENCHANT_RE = re.compile(r"^(.*?)@(\d+)$")
_TIER_ID_RE = re.compile(r"^T(\d+)_")
_TIER_TOKEN_RE = re.compile(r"^t([1-8])$")
_ITEMS_PATH = Path(__file__).parent / "data" / "items.txt"

# Minimum fuzzy score for :meth:`ItemIndex.resolve` to accept a name match.
# Below this the query is treated as "no such item" rather than returning a
# wildly unrelated best-effort id.
_RESOLVE_FLOOR = 0.55


def split_enchant(query: str) -> tuple[str, str | None]:
    """Split a trailing ``@n`` enchant off *query*.

    ``"T8_2H_HOLYSTAFF@3"`` → ``("T8_2H_HOLYSTAFF", "@3")``;
    ``"holy staff"`` → ``("holy staff", None)``. The enchant is returned with
    its ``@`` so it reattaches by concatenation.
    """
    m = _ENCHANT_RE.match(query.strip())
    if m is None:
        return query.strip(), None
    return m.group(1).strip(), "@" + m.group(2)


def tier_of(item_id: str) -> int:
    """Return the tier from an item id's ``T{n}_`` prefix, or ``0`` if none.

    Tolerates a trailing enchant (``T8_2H_HOLYSTAFF@3`` → ``8``).
    """
    base, _ = split_enchant(item_id)
    m = _TIER_ID_RE.match(base.strip().upper())
    return int(m.group(1)) if m else 0


def parse_line(line: str) -> tuple[str, str] | None:
    """Parse one ``items.txt`` line into ``(item_id, display_name)``.

    Accepts both the dump's indexed form ``"  1: UNIQUE_ID : Name"`` and a bare
    ``"UNIQUE_ID : Name"``. The id is the last whitespace token before the final
    ``" : "`` separator. Returns ``None`` for blank or malformed lines.
    """
    stripped = line.strip()
    if " : " not in stripped:
        return None
    left, _, name = stripped.rpartition(" : ")
    left = left.strip()
    name = name.strip()
    if not left or not name:
        return None
    token = left.split()[-1]
    # A leading "INDEX:" column can attach to the id if spacing is odd
    # ("1:UNIQUE_ID"); keep only the part after a stray colon.
    if ":" in token:
        token = token.rsplit(":", 1)[-1]
    if not token:
        return None
    return token, name


def _tokenize(text: str) -> list[str]:
    """Lowercase and split *text* into alphanumeric tokens, dropping the bare
    possessive ``"s"`` left over from apostrophe splitting."""
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t and t != "s"]


def _analyze_query(query: str) -> tuple[str, frozenset[str], int | None]:
    """Reduce a name query to ``(core_string, core_token_set, tier_hint)``.

    Tier words and ``t{n}`` tokens are pulled out as a tier hint and removed
    from the core, so "elder holy staff" and "t8 holy staff" both core down to
    "holy staff" while biasing toward tier 8.
    """
    tier_hint: int | None = None
    core: list[str] = []
    for tok in _tokenize(query):
        if tok in TIER_WORDS:
            tier_hint = TIER_WORDS[tok]
            continue
        m = _TIER_TOKEN_RE.match(tok)
        if m:
            tier_hint = int(m.group(1))
            continue
        core.append(tok)
    return " ".join(core), frozenset(core), tier_hint


@dataclass(frozen=True, slots=True)
class _Entry:
    """A base (unenchanted) item, precomputed for fuzzy matching."""

    item_id: str
    display: str
    tier: int
    core: str
    tokens: frozenset[str] = field(default_factory=frozenset)


class ItemIndex:
    """Bidirectional Albion item id ↔ localized name index.

    Load once with :meth:`load` (async, off-loop); then call the synchronous,
    pure query methods. Construct directly for production
    (``ItemIndex()`` uses the bundled ``data/items.txt``) or via
    :meth:`from_text` in tests.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else _ITEMS_PATH
        self._loaded = False
        self._lock = asyncio.Lock()
        # Every id (including @n variants) → display name, for exact name_of.
        self._names: dict[str, str] = {}
        # Lowercased id → canonical id, for case-insensitive exact resolve.
        self._id_lower: dict[str, str] = {}
        # Base (no-enchant) entries only, for fuzzy resolve/search.
        self._entries: list[_Entry] = []

    # -- loading -----------------------------------------------------------

    async def load(self) -> None:
        """Read and index ``items.txt`` once; a no-op after the first call.

        Both the file read *and* the ~12k-line parse/index run in a worker
        thread — the parse is CPU-bound (regex per line, thousands of frozen
        dataclasses) and must not stall the shared event loop that carries the
        voice path. A missing or unreadable file leaves the index empty (logged
        at ``warning``); it never raises.
        """
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            await asyncio.to_thread(self._read_and_build)
            self._loaded = True

    def _read_and_build(self) -> None:
        """Read the file and build the index — the whole off-loop unit."""
        self._build(self._read_text() or "")

    @classmethod
    def from_text(cls, text: str, path: Path | None = None) -> ItemIndex:
        """Build a loaded index directly from file text (for tests)."""
        idx = cls(path=path)
        idx._build(text)
        idx._loaded = True
        return idx

    def _read_text(self) -> str | None:
        try:
            return self._path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            log.warning("kb.items.missing", path=str(self._path))
            return None
        except OSError as exc:
            log.warning("kb.items.read_failed", path=str(self._path), error=str(exc))
            return None

    def _build(self, text: str) -> None:
        names: dict[str, str] = {}
        id_lower: dict[str, str] = {}
        entries: list[_Entry] = []
        for line in text.splitlines():
            parsed = parse_line(line)
            if parsed is None:
                continue
            item_id, display = parsed
            names[item_id] = display
            id_lower[item_id.lower()] = item_id
            # Fuzzy set is base ids only; @n rows share their base's name and
            # would just multiply identical candidates.
            _, ench = split_enchant(item_id)
            if ench is None:
                tokens = _tokenize(display)
                core_tokens = [t for t in tokens if t not in TIER_WORDS]
                entries.append(
                    _Entry(
                        item_id=item_id,
                        display=display,
                        tier=tier_of(item_id),
                        core=" ".join(core_tokens),
                        tokens=frozenset(core_tokens),
                    )
                )
        self._names = names
        self._id_lower = id_lower
        self._entries = entries
        log.info("kb.items.loaded", ids=len(names), base_items=len(entries))

    # -- queries -----------------------------------------------------------

    def name_of(self, item_id: str) -> str | None:
        """Return the display name for *item_id*, or ``None`` if unknown.

        Falls back to the base id when an enchanted id (``...@3``) is not itself
        listed but its base is.
        """
        if item_id in self._names:
            return self._names[item_id]
        base, _ = split_enchant(item_id)
        return self._names.get(base)

    def tier_of(self, item_id: str) -> int:
        """Tier from the id prefix; see module-level :func:`tier_of`."""
        return tier_of(item_id)

    def resolve(self, query: str) -> str | None:
        """Resolve *query* to the single best item id, or ``None``.

        Accepts an exact id (``"T8_2H_HOLYSTAFF"``, any case), an id with an
        enchant (``"...@3"``), or a fuzzy localized-name query
        (``"elder holy staff"``). Enchant handling: the ``@n`` is split off,
        matched against the base id, and reattached to the result.
        """
        if not query or not query.strip():
            return None
        raw = query.strip()

        # Whole string as an exact id, e.g. "t8_2h_holystaff@3".
        canon_full = self._id_lower.get(raw.lower())
        if canon_full is not None:
            return canon_full

        base_q, ench = split_enchant(raw)

        # Base as an exact id, then reattach the requested enchant.
        canon = self._id_lower.get(base_q.lower())
        if canon is not None:
            return self._apply_enchant(canon, ench)

        # Fuzzy name match over base entries.
        best = self._best_by_name(base_q)
        if best is None:
            return None
        return self._apply_enchant(best.item_id, ench)

    def search(self, query: str, limit: int = 25) -> list[tuple[str, str]]:
        """Return up to *limit* ``(item_id, display_name)`` candidates for
        *query*, best first — sized for slash-command autocomplete.

        Ranks over id substrings and fuzzy core-name similarity. An empty query
        or empty index yields ``[]``.
        """
        if not self._entries or limit <= 0:
            return []
        base_q, _ench = split_enchant(query)
        if not base_q.strip():
            return []
        core, tokens, tier_hint = _analyze_query(base_q)
        id_needle = base_q.strip().lower()
        scored: list[tuple[float, int, str, str]] = []
        for entry in self._entries:
            score = self._score(core, tokens, tier_hint, id_needle, entry)
            if score > 0.0:
                scored.append((score, entry.tier, entry.item_id, entry.display))
        # Highest score first; deterministic ties by tier then id.
        scored.sort(key=lambda t: (-t[0], t[1], t[2]))
        return [(item_id, display) for _s, _t, item_id, display in scored[:limit]]

    # -- internals ---------------------------------------------------------

    def _apply_enchant(self, item_id: str, ench: str | None) -> str:
        """Reattach an enchant to a resolved base id (the AODP API accepts any
        ``@n``, seeded row or not)."""
        return f"{item_id}{ench}" if ench else item_id

    def _best_by_name(self, name_query: str) -> _Entry | None:
        core, tokens, tier_hint = _analyze_query(name_query)
        if not core:
            return None
        id_needle = name_query.strip().lower()
        best: _Entry | None = None
        best_score = 0.0
        for entry in self._entries:
            score = self._score(core, tokens, tier_hint, id_needle, entry)
            # Ties broken deterministically toward the lower tier / lower id.
            if score > best_score or (
                best is not None
                and score == best_score
                and (entry.tier, entry.item_id) < (best.tier, best.item_id)
            ):
                best, best_score = entry, score
        if best is None or best_score < _RESOLVE_FLOOR:
            return None
        return best

    @staticmethod
    def _score(
        core: str,
        tokens: frozenset[str],
        tier_hint: int | None,
        id_needle: str,
        entry: _Entry,
    ) -> float:
        """Pure similarity score of a query against one entry (higher better).

        Combines fuzzy core-name ratio, substring and token-overlap bonuses, an
        id-substring signal, and a tier-hint bias. Deliberately dependency-free
        and deterministic so it can be unit-tested directly.
        """
        score = SequenceMatcher(None, core, entry.core).ratio()
        if core and core in entry.core:
            score += 0.4
        if tokens:
            overlap = len(tokens & entry.tokens) / len(tokens)
            score += 0.3 * overlap
        # Id-substring signal (handles partial id typing in autocomplete).
        if id_needle and "_" in id_needle and id_needle in entry.item_id.lower():
            score += 0.5
        if tier_hint is not None:
            score += 0.5 if entry.tier == tier_hint else -0.2
        return score
