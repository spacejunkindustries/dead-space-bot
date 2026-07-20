"""Pins the real behaviour of :mod:`killboard.value` — the kill-card silver
estimate (killboard §7.1).

Two halves, both driven with hand-written fixtures and a *fake* market seam — no
network, no Discord, no sqlite:

* :func:`~killboard.value.items_of` is pinned as a pure, total extraction of
  ``(item_id, count, quality)`` triples from a synthetic gameinfo event, tolerant
  of null/missing slots, aggregating identical lines, and side-aware.
* :func:`~killboard.value.estimate_value` is pinned against a stub market whose
  ``prices`` returns canned :class:`~killboard.market.PriceRow` rows (the confirmed
  AODP field shapes, including the ``0``/epoch no-data sentinel). We pin the
  count*price sum, priced/unpriced tallies, the quality-1 fall-back, lowest-sell
  reference across cities, no-data → "unknown" (never a real ``0``),
  ``total=None`` iff nothing priced, and that a raising market is swallowed.
"""

from __future__ import annotations

from typing import Any

from killboard.market import PriceRow
from killboard.value import estimate_value, items_of

# The AODP "no data" sentinel: a 0 price stamped with the 0001-01-01 epoch date.
EPOCH = "0001-01-01T00:00:00"
_DATE = "2026-07-19T21:30:00"


# ── market seam fakes ────────────────────────────────────────────────────────


def _row(item_id: str, city: str, quality: int, sell_min: int) -> PriceRow:
    """A real-data price row (verbatim AODP field shape) with a live sell_min."""
    return PriceRow(
        item_id=item_id,
        city=city,
        quality=quality,
        sell_min=sell_min,
        sell_min_date=_DATE,
        sell_max=sell_min + 100,
        sell_max_date=_DATE,
        buy_min=0,
        buy_min_date=EPOCH,
        buy_max=0,
        buy_max_date=EPOCH,
    )


def _nodata_row(item_id: str, city: str, quality: int) -> PriceRow:
    """A no-data row: 0 price on the epoch date (``has_data`` is False)."""
    return PriceRow(
        item_id=item_id,
        city=city,
        quality=quality,
        sell_min=0,
        sell_min_date=EPOCH,
        sell_max=0,
        sell_max_date=EPOCH,
        buy_min=0,
        buy_min_date=EPOCH,
        buy_max=0,
        buy_max_date=EPOCH,
    )


class FakeMarket:
    """Stub :class:`~killboard.market.MarketClient` seam.

    Returns a fixed list of canned rows for every ``prices`` call and records each
    call's args, so a test can assert the sum, the queried qualities, and that
    only one fetch happens for a whole kit.
    """

    def __init__(self, rows: list[PriceRow]) -> None:
        self._rows = list(rows)
        self.calls: list[dict[str, Any]] = []

    async def prices(
        self,
        item_ids: list[str],
        cities: list[str] | None = None,
        qualities: list[int] | None = None,
    ) -> list[PriceRow]:
        self.calls.append(
            {
                "item_ids": list(item_ids),
                "cities": None if cities is None else list(cities),
                "qualities": None if qualities is None else list(qualities),
            }
        )
        return list(self._rows)


class RaisingMarket:
    """A market whose ``prices`` raises — pins §13 (a dead market never crashes a
    card; the exception is swallowed and everything prices as unknown)."""

    def __init__(self) -> None:
        self.calls = 0

    async def prices(self, *_a: Any, **_k: Any) -> list[PriceRow]:
        self.calls += 1
        raise RuntimeError("market is down")


# A structurally faithful gameinfo victim: ten-slot Equipment (with a null slot and
# absent slots), a sparse Inventory (a null hole, a typeless entry), and a Killer
# side too. The Potion slot and an inventory potion share id+quality and MUST
# aggregate. Enchant suffix (@3) is preserved verbatim as the AODP price id.
def _event() -> dict[str, Any]:
    return {
        "Victim": {
            "Equipment": {
                "MainHand": {"Type": "T8_2H_HOLYSTAFF@3", "Count": 1, "Quality": 4},
                "Head": None,  # null slot — skipped
                "Armor": {"Type": "T6_ARMOR_PLATE_SET1", "Count": 1, "Quality": 2},
                "Potion": {"Type": "T4_POTION_HEAL", "Count": 3, "Quality": 1},
                # OffHand/Shoes/Cape/Bag/Mount/Food absent — skipped
            },
            "Inventory": [
                {"Type": "T4_BAG", "Count": 1, "Quality": 1},
                None,  # null hole — skipped
                {"Type": "T4_POTION_HEAL", "Count": 2, "Quality": 1},  # aggregates
                {"Count": 5, "Quality": 1},  # typeless — skipped
            ],
        },
        "Killer": {
            "Equipment": {"MainHand": {"Type": "T4_MAIN_SWORD", "Count": 1, "Quality": 1}},
            "Inventory": None,  # null inventory tolerated
        },
    }


def _by_id(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {line["item_id"]: line for line in result["by_item"]}


# ── items_of (pure extraction) ───────────────────────────────────────────────


def test_items_of_extracts_equipment_and_inventory() -> None:
    items = items_of(_event(), "victim")
    # Equipment (slot order) then inventory, with the two potions aggregated in
    # the potion's first-seen (equipment) position; typeless/null entries dropped.
    assert items == [
        ("T8_2H_HOLYSTAFF@3", 1, 4),
        ("T6_ARMOR_PLATE_SET1", 1, 2),
        ("T4_POTION_HEAL", 5, 1),  # 3 worn + 2 in bag, summed
        ("T4_BAG", 1, 1),
    ]


def test_items_of_preserves_enchant_suffix() -> None:
    ids = [item_id for item_id, _c, _q in items_of(_event(), "victim")]
    assert "T8_2H_HOLYSTAFF@3" in ids  # @3 kept verbatim for the AODP id


def test_items_of_side_selects_killer() -> None:
    assert items_of(_event(), "killer") == [("T4_MAIN_SWORD", 1, 1)]


def test_items_of_missing_side_is_empty() -> None:
    assert items_of({}, "victim") == []
    assert items_of({"Victim": None}, "victim") == []


def test_items_of_naked_player_is_empty() -> None:
    assert items_of({"Victim": {}}, "victim") == []
    assert items_of({"Victim": {"Equipment": None, "Inventory": None}}, "victim") == []


def test_items_of_tolerates_null_and_typeless_slots() -> None:
    event = {
        "Victim": {
            "Equipment": {"Head": None, "Armor": {"Count": 1}},  # null + typeless
            "Inventory": [None, {"Quality": 3}, "junk", 7],  # all unusable
        }
    }
    assert items_of(event, "victim") == []


def test_items_of_clamps_and_defaults_quality_and_count() -> None:
    event = {
        "Victim": {
            "Equipment": {
                "MainHand": {"Type": "T4_SWORD"},  # no Count/Quality → 1/1
                "Armor": {"Type": "T4_ARMOR", "Count": 0, "Quality": 99},  # floors/clamps
            }
        }
    }
    assert items_of(event, "victim") == [
        ("T4_SWORD", 1, 1),
        ("T4_ARMOR", 1, 5),  # count floored to 1, quality clamped to the 1-5 band
    ]


# ── estimate_value (async, against the fake market) ──────────────────────────


async def test_estimate_value_sums_count_times_price() -> None:
    # Bag priced at its exact quality (1); one potion line priced at quality 1.
    rows = [
        _row("T4_BAG", "Caerleon", 1, 6000),
        _row("T4_POTION_HEAL", "Caerleon", 1, 200),
    ]
    market = FakeMarket(rows)
    result = await estimate_value(_event(), market, side="victim")

    # 5 potions * 200 + 1 bag * 6000 = 7000. Staff/armor never priced.
    assert result["total"] == 7000
    assert result["priced"] == 2
    assert result["unpriced"] == 2

    lines = _by_id(result)
    assert lines["T4_POTION_HEAL"]["subtotal"] == 1000  # 5 * 200
    assert lines["T4_POTION_HEAL"]["priced"] is True
    assert lines["T4_BAG"]["subtotal"] == 6000
    # The unpriced lines report None, never a misleading 0.
    assert lines["T6_ARMOR_PLATE_SET1"]["priced"] is False
    assert lines["T6_ARMOR_PLATE_SET1"]["unit_price"] is None
    assert lines["T6_ARMOR_PLATE_SET1"]["subtotal"] is None


async def test_estimate_value_quality_fallback_to_one() -> None:
    # The staff is quality 4, but only a quality-1 price exists → fall back to q1.
    market = FakeMarket([_row("T8_2H_HOLYSTAFF@3", "Martlock", 1, 500_000)])
    result = await estimate_value(_event(), market, side="victim")

    staff = _by_id(result)["T8_2H_HOLYSTAFF@3"]
    assert staff["priced"] is True
    assert staff["quality"] == 4  # what the pilot actually carried
    assert staff["quality_used"] == 1  # what we could price it at
    assert staff["unit_price"] == 500_000
    assert result["total"] == 500_000


async def test_estimate_value_lowest_sell_across_cities_wins() -> None:
    market = FakeMarket(
        [
            _row("T4_BAG", "Caerleon", 1, 6000),
            _row("T4_BAG", "Lymhurst", 1, 4200),  # cheaper — the reference
            _row("T4_BAG", "Martlock", 1, 5100),
        ]
    )
    bag_line = _by_id(await estimate_value(_event(), market, side="victim"))["T4_BAG"]
    assert bag_line["unit_price"] == 4200
    assert bag_line["city"] == "Lymhurst"


async def test_estimate_value_nodata_sentinel_is_unknown_not_zero() -> None:
    # Only a 0/epoch sentinel comes back for the bag: it must count as unpriced,
    # never as a real free item, and the line's price stays None.
    market = FakeMarket([_nodata_row("T4_BAG", "Caerleon", 1)])
    result = await estimate_value(_event(), market, side="victim")

    bag = _by_id(result)["T4_BAG"]
    assert bag["priced"] is False
    assert bag["unit_price"] is None
    assert bag["subtotal"] is None
    # Nothing else priced either → the whole estimate is "unknown", not 0.
    assert result["total"] is None
    assert result["priced"] == 0


async def test_estimate_value_total_none_only_when_nothing_prices() -> None:
    # An empty market: every line unpriced → total is None (unknown), not 0.
    result = await estimate_value(_event(), FakeMarket([]), side="victim")
    assert result["total"] is None
    assert result["priced"] == 0
    assert result["unpriced"] == 4
    assert all(line["priced"] is False for line in result["by_item"])


async def test_estimate_value_empty_kit_short_circuits() -> None:
    market = FakeMarket([_row("T4_BAG", "Caerleon", 1, 6000)])
    result = await estimate_value({"Victim": {}}, market, side="victim")
    assert result == {"total": None, "priced": 0, "unpriced": 0, "by_item": []}
    # A naked victim never hits the market.
    assert market.calls == []


async def test_estimate_value_fetches_once_with_fallback_quality() -> None:
    market = FakeMarket([_row("T4_BAG", "Caerleon", 1, 6000)])
    await estimate_value(_event(), market, side="victim")

    # A whole kit is a single batched fetch (cache-friendly, one round trip).
    assert len(market.calls) == 1
    call = market.calls[0]
    # Unique ids only, and the queried qualities include the item qualities {1,2,4}
    # plus the quality-1 fall-back (already present here).
    assert sorted(call["item_ids"]) == [
        "T4_BAG",
        "T4_POTION_HEAL",
        "T6_ARMOR_PLATE_SET1",
        "T8_2H_HOLYSTAFF@3",
    ]
    assert sorted(call["qualities"]) == [1, 2, 4]


async def test_estimate_value_passes_cities_through() -> None:
    market = FakeMarket([])
    await estimate_value(_event(), market, side="victim", cities=["Bridgewatch"])
    assert market.calls[0]["cities"] == ["Bridgewatch"]


async def test_estimate_value_swallows_market_exception() -> None:
    # §13: a market that blows up must not raise into the card renderer; the kit
    # degrades to all-unknown with a well-formed dict.
    market = RaisingMarket()
    result = await estimate_value(_event(), market, side="victim")
    assert market.calls == 1
    assert result["total"] is None
    assert result["priced"] == 0
    assert result["unpriced"] == 4
