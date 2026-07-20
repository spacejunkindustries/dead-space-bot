"""Pins the real behaviour of :mod:`killboard.items` — the id ↔ localized-name
index behind the market layer's resolve/autocomplete/name rendering.

No network, no Discord, no bundled data file: every test builds a tiny in-memory
``items.txt`` and drives :class:`~killboard.items.ItemIndex` through it. The one
async test exercises the real off-loop file read (:func:`asyncio.to_thread`) and
pins the missing-file degradation (empty index, no raise).
"""

from __future__ import annotations

from pathlib import Path

from killboard.items import (
    ItemIndex,
    parse_line,
    split_enchant,
    tier_of,
)

# A minimal but structurally faithful items.txt. Mixes the dump's indexed form
# ("  N: ID : Name") with the bare form ("ID : Name"), lists one @enchant row,
# and includes junk lines that must be skipped.
ITEMS_TEXT = """\
   1: T4_BAG : Adept's Bag
   2: T4_2H_HOLYSTAFF : Adept's Great Holy Staff
   3: T8_2H_HOLYSTAFF : Elder's Great Holy Staff
   4: T8_2H_HOLYSTAFF@3 : Elder's Great Holy Staff
T5_2H_CLAYMORE : Expert's Claymore

this line has no separator
   : name with no id
"""


def _index() -> ItemIndex:
    return ItemIndex.from_text(ITEMS_TEXT)


# ── split_enchant / tier_of (pure module functions) ──────────────────────────


def test_split_enchant_splits_trailing_at_n() -> None:
    assert split_enchant("T8_2H_HOLYSTAFF@3") == ("T8_2H_HOLYSTAFF", "@3")


def test_split_enchant_none_when_no_enchant() -> None:
    assert split_enchant("holy staff") == ("holy staff", None)


def test_tier_of_reads_id_prefix() -> None:
    assert tier_of("T4_BAG") == 4
    assert tier_of("T8_2H_HOLYSTAFF") == 8


def test_tier_of_tolerates_enchant_and_missing_prefix() -> None:
    assert tier_of("T8_2H_HOLYSTAFF@3") == 8
    assert tier_of("BAG") == 0


# ── parse_line ───────────────────────────────────────────────────────────────


def test_parse_line_indexed_form() -> None:
    assert parse_line("   2: T4_2H_HOLYSTAFF : Adept's Great Holy Staff") == (
        "T4_2H_HOLYSTAFF",
        "Adept's Great Holy Staff",
    )


def test_parse_line_bare_form() -> None:
    assert parse_line("T5_2H_CLAYMORE : Expert's Claymore") == (
        "T5_2H_CLAYMORE",
        "Expert's Claymore",
    )


def test_parse_line_rejects_malformed() -> None:
    assert parse_line("this line has no separator") is None
    assert parse_line("") is None
    assert parse_line("   : name with no id") is None


# ── name_of / tier_of on the index ───────────────────────────────────────────


def test_name_of_exact_id() -> None:
    idx = _index()
    assert idx.name_of("T4_BAG") == "Adept's Bag"
    assert idx.name_of("T8_2H_HOLYSTAFF") == "Elder's Great Holy Staff"


def test_name_of_enchant_falls_back_to_base() -> None:
    idx = _index()
    # @2 is not a listed row; the base T4_BAG is, so its name stands in.
    assert idx.name_of("T4_BAG@2") == "Adept's Bag"


def test_name_of_listed_enchant_row_directly() -> None:
    idx = _index()
    assert idx.name_of("T8_2H_HOLYSTAFF@3") == "Elder's Great Holy Staff"


def test_name_of_unknown_is_none() -> None:
    assert _index().name_of("T4_NONEXISTENT") is None


def test_index_tier_of_delegates() -> None:
    assert _index().tier_of("T8_2H_HOLYSTAFF@3") == 8


# ── resolve ──────────────────────────────────────────────────────────────────


def test_resolve_exact_id_case_insensitive() -> None:
    idx = _index()
    assert idx.resolve("T4_BAG") == "T4_BAG"
    assert idx.resolve("t4_bag") == "T4_BAG"


def test_resolve_full_id_with_enchant() -> None:
    # The whole "id@n" string is a listed exact row → returned verbatim (canon).
    assert _index().resolve("t8_2h_holystaff@3") == "T8_2H_HOLYSTAFF@3"


def test_resolve_reattaches_enchant_to_base_id() -> None:
    # Base id is listed, @2 is not seeded — resolver reattaches the requested
    # enchant to the canonical base (the AODP API accepts any @n).
    assert _index().resolve("T4_BAG@2") == "T4_BAG@2"


def test_resolve_fuzzy_localized_name() -> None:
    # "holy staff" is not an id; it fuzzy-matches the holy-staff family. Ties
    # break toward the lower tier, so the T4 entry wins.
    assert _index().resolve("holy staff") == "T4_2H_HOLYSTAFF"


def test_resolve_tier_hint_biases_to_tier() -> None:
    assert _index().resolve("t8 holy staff") == "T8_2H_HOLYSTAFF"
    assert _index().resolve("elder holy staff") == "T8_2H_HOLYSTAFF"


def test_resolve_empty_query_is_none() -> None:
    idx = _index()
    assert idx.resolve("") is None
    assert idx.resolve("   ") is None


def test_resolve_gibberish_below_floor_is_none() -> None:
    assert _index().resolve("zzzqx nonsense widget") is None


# ── search (autocomplete) ────────────────────────────────────────────────────


def test_search_returns_ranked_id_name_pairs() -> None:
    results = _index().search("holy staff")
    assert results  # non-empty
    # Only (id, name) tuples, and only base (non-@enchant) ids surface.
    for item_id, name in results:
        assert "@" not in item_id
        assert isinstance(name, str) and name
    ids = {item_id for item_id, _name in results}
    assert {"T4_2H_HOLYSTAFF", "T8_2H_HOLYSTAFF"} <= ids


def test_search_respects_limit() -> None:
    results = _index().search("holy", limit=1)
    assert len(results) == 1


def test_search_empty_query_is_empty() -> None:
    idx = _index()
    assert idx.search("") == []
    assert idx.search("   ") == []


def test_search_by_partial_id() -> None:
    results = _index().search("t4_2h_holy")
    assert results
    assert results[0][0] == "T4_2H_HOLYSTAFF"


# ── async load + missing-file degradation ────────────────────────────────────


async def test_load_reads_file_off_loop(tmp_path: Path) -> None:
    items_file = tmp_path / "items.txt"
    items_file.write_text(ITEMS_TEXT, encoding="utf-8")
    idx = ItemIndex(path=items_file)
    await idx.load()
    assert idx.resolve("T4_BAG") == "T4_BAG"
    assert idx.name_of("T5_2H_CLAYMORE") == "Expert's Claymore"


async def test_load_is_idempotent(tmp_path: Path) -> None:
    items_file = tmp_path / "items.txt"
    items_file.write_text(ITEMS_TEXT, encoding="utf-8")
    idx = ItemIndex(path=items_file)
    await idx.load()
    await idx.load()  # second call is a no-op, must not raise or double-index
    assert idx.resolve("T4_BAG") == "T4_BAG"


async def test_missing_file_yields_empty_index_without_raising(tmp_path: Path) -> None:
    idx = ItemIndex(path=tmp_path / "does_not_exist.txt")
    await idx.load()  # must not raise
    assert idx.resolve("T4_BAG") is None
    assert idx.resolve("holy staff") is None
    assert idx.search("holy") == []
    assert idx.name_of("T4_BAG") is None
