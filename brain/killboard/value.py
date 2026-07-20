"""Kill *value* estimation — the silver loot figure on kill cards (killboard §7.1).

A kill card's headline number, beside fame and item power, is an estimate of how
much silver the victim was carrying: the sum of their equipped gear and inventory
priced at the current market. This module computes that figure.

It is split deliberately into a **pure** half and an **async** half:

* :func:`items_of` is a pure, total function of the raw gameinfo event — it walks
  ``Victim``/``Killer`` ``Equipment`` (the ten worn slots) and ``Inventory`` (a
  possibly-sparse array) and returns ``(item_id, count, quality)`` triples,
  aggregating identical lines. It never touches the network and is trivially
  unit-testable with hand-written fixtures (§2.4).
* :func:`estimate_value` does the pricing: it asks a :class:`MarketClient` for
  current prices, picks a sensible reference price per item (the lowest sell
  across the queried cities), is quality-aware with a fall-back to quality 1 when
  the exact quality has never traded, and sums ``count × price``.

The tolerance rule of the killboard holds throughout (§2.4, §13): a missing
player, a null slot, a typeless item, or a dead market all degrade to a smaller
figure — or ``total=None`` ("unknown") — never a crash and never a misleading
``0``. The AODP no-data sentinel (a ``0`` price on the epoch date) is filtered by
:attr:`PriceRow.has_data` before it can masquerade as a real free item.

Both the card renderer and a ``/killboard value`` slash command call
:func:`estimate_value`, so the figure a pilot sees on the card and the figure the
command reports are computed by exactly the same code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import structlog

if TYPE_CHECKING:
    from collections.abc import Mapping

    from killboard.market import MarketClient, PriceRow

log = structlog.get_logger(__name__)

Side = Literal["victim", "killer"]

#: The ten worn equipment slots on a gameinfo player object, in a stable order.
#: Matches ``cards.GEAR_SLOTS`` (kept independent here so ``value`` has no import
#: dependency on the Pillow-backed card renderer). Each slot, when populated, is
#: an object with ``Type`` / ``Count`` / ``Quality``.
_EQUIPMENT_SLOTS: tuple[str, ...] = (
    "MainHand",
    "OffHand",
    "Head",
    "Armor",
    "Shoes",
    "Cape",
    "Bag",
    "Mount",
    "Potion",
    "Food",
)

#: Map the caller's lowercase ``side`` to the PascalCase gameinfo sub-object key.
_SIDE_KEY: dict[str, str] = {"victim": "Victim", "killer": "Killer"}

#: AODP market quality band (1 Normal .. 5 Masterpiece). Anything outside clamps
#: into range so a junk ``Quality`` can't produce an un-queryable value.
_MIN_QUALITY: int = 1
_MAX_QUALITY: int = 5
#: The universal fall-back quality when the item's exact quality never traded.
_FALLBACK_QUALITY: int = 1


# ── pure item extraction ─────────────────────────────────────────────────────


def items_of(event: Mapping[str, Any], side: Side) -> list[tuple[str, int, int]]:
    """Extract a side's carried items as ``(item_id, count, quality)`` triples.

    Walks the ``Equipment`` object's ten worn slots followed by the ``Inventory``
    array for the given ``side`` (``"victim"`` or ``"killer"``). ``item_id`` is
    the gameinfo ``Type`` verbatim, including any ``@1``..``@3`` enchant suffix,
    which is exactly the id shape the AODP price API expects.

    Tolerant of the API's partial data (§2.4): a missing player, a missing or
    non-object ``Equipment``, empty/null slots, null ``Inventory`` entries, and
    typeless items are all skipped rather than raising. Identical lines — same
    ``item_id`` and ``quality`` — are aggregated, their counts summed, preserving
    first-seen order, so pricing queries stay small and a stack of potions is one
    entry. A naked player yields ``[]``.
    """
    player = event.get(_SIDE_KEY.get(side, ""))
    if not isinstance(player, dict):
        return []

    raw: list[tuple[str, int, int]] = []
    raw.extend(_from_equipment(player.get("Equipment")))
    raw.extend(_from_inventory(player.get("Inventory")))
    return _aggregate(raw)


def _from_equipment(equipment: Any) -> list[tuple[str, int, int]]:
    """Triples from the worn-slot ``Equipment`` object (skips absent slots)."""
    if not isinstance(equipment, dict):
        return []
    out: list[tuple[str, int, int]] = []
    for slot in _EQUIPMENT_SLOTS:
        triple = _slot_triple(equipment.get(slot))
        if triple is not None:
            out.append(triple)
    return out


def _from_inventory(inventory: Any) -> list[tuple[str, int, int]]:
    """Triples from the ``Inventory`` array (a list with null holes, or null)."""
    if not isinstance(inventory, list):
        return []
    out: list[tuple[str, int, int]] = []
    for entry in inventory:
        triple = _slot_triple(entry)
        if triple is not None:
            out.append(triple)
    return out


def _slot_triple(entry: Any) -> tuple[str, int, int] | None:
    """One ``(item_id, count, quality)`` from a slot/inventory object, or ``None``.

    ``None`` for anything without a usable ``Type`` (an empty slot, a null hole,
    a non-object). ``Count`` defaults to and floors at 1 (a worn item has no
    count); ``Quality`` defaults to 1 and clamps to the 1-5 market band.
    """
    if not isinstance(entry, dict):
        return None
    item_id = _first_str(entry.get("Type"))
    if item_id is None:
        return None
    count = _int(entry.get("Count"), default=1)
    if count < 1:
        count = 1
    return (item_id, count, _clamp_quality(entry.get("Quality")))


def _aggregate(triples: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    """Sum counts of identical ``(item_id, quality)`` lines, first-seen order."""
    order: list[tuple[str, int]] = []
    counts: dict[tuple[str, int], int] = {}
    for item_id, count, quality in triples:
        key = (item_id, quality)
        if key not in counts:
            counts[key] = 0
            order.append(key)
        counts[key] += count
    return [(item_id, counts[(item_id, quality)], quality) for item_id, quality in order]


# ── async pricing ────────────────────────────────────────────────────────────


async def estimate_value(
    event: Mapping[str, Any],
    market: MarketClient,
    *,
    side: Side = "victim",
    cities: list[str] | None = None,
) -> dict[str, Any]:
    """Estimate a side's carried silver value at current market prices (§7.1).

    Extracts the side's items with :func:`items_of`, fetches current prices for
    them across ``cities`` (defaulting to the market client's configured cities)
    and the qualities present, then prices each line at its **reference price** —
    the lowest ``sell_price_min`` across the queried cities for that item and
    quality, ignoring AODP no-data sentinels. When an item's exact quality has
    never traded it falls back to quality 1; if that too is unknown the item is
    counted as *unpriced* and contributes nothing.

    Returns a dict::

        {
          "total": int | None,   # summed count*price; None iff NOTHING priced
          "priced": int,         # number of item lines that got a price
          "unpriced": int,       # number of item lines with no price
          "by_item": [           # one entry per aggregated line
            {"item_id", "count", "quality", "quality_used",
             "city", "unit_price", "subtotal", "priced"}, ...
          ],
        }

    Never raises (§13): a dead market, a partial batch, or an empty kit all yield
    a well-formed dict with ``total=None`` or a smaller sum, never an exception.
    """
    items = items_of(event, side)
    if not items:
        return {"total": None, "priced": 0, "unpriced": 0, "by_item": []}

    index = await _price_index(items, market, cities)

    by_item: list[dict[str, Any]] = []
    total = 0
    priced = 0
    unpriced = 0
    for item_id, count, quality in items:
        unit_price, quality_used, city = _lookup_price(index, item_id, quality)
        if unit_price is None:
            unpriced += 1
            by_item.append(
                {
                    "item_id": item_id,
                    "count": count,
                    "quality": quality,
                    "quality_used": None,
                    "city": None,
                    "unit_price": None,
                    "subtotal": None,
                    "priced": False,
                }
            )
            continue
        subtotal = unit_price * count
        total += subtotal
        priced += 1
        by_item.append(
            {
                "item_id": item_id,
                "count": count,
                "quality": quality,
                "quality_used": quality_used,
                "city": city,
                "unit_price": unit_price,
                "subtotal": subtotal,
                "priced": True,
            }
        )

    return {
        "total": total if priced > 0 else None,
        "priced": priced,
        "unpriced": unpriced,
        "by_item": by_item,
    }


async def _price_index(
    items: list[tuple[str, int, int]],
    market: MarketClient,
    cities: list[str] | None,
) -> dict[tuple[str, int], tuple[int, str]]:
    """Build ``(item_id, quality) → (reference_sell_price, city)`` from the market.

    Queries the unique item ids across the qualities present (plus the quality-1
    fall-back), then reduces the returned rows to the lowest real sell price per
    ``(item_id, quality)``. Rows carrying the AODP no-data sentinel are dropped by
    :attr:`PriceRow.has_data`. Any failure in the market call is swallowed (§13)
    and yields an empty index, so pricing degrades to "all unknown".
    """
    item_ids = sorted({item_id for item_id, _count, _quality in items})
    qualities = sorted({quality for _id, _count, quality in items} | {_FALLBACK_QUALITY})

    try:
        rows = await market.prices(item_ids, cities=cities, qualities=qualities)
    except Exception as exc:  # noqa: BLE001 — a market lookup must never raise into a card
        log.warning("kb_value.price_lookup_failed", error=str(exc))
        rows = []

    index: dict[tuple[str, int], tuple[int, str]] = {}
    for row in rows:
        price = _reference_price(row)
        if price is None:
            continue
        key = (row.item_id, row.quality)
        current = index.get(key)
        if current is None or price < current[0]:
            index[key] = (price, row.city)
    return index


def _reference_price(row: PriceRow) -> int | None:
    """The row's usable reference sell price, or ``None`` for a no-data row.

    Uses ``sell_price_min`` (the cheapest offer a looter could actually buy the
    item back for) and only when the row carries real data — never the ``0`` of
    an epoch-dated sentinel (§2.4).
    """
    if not row.has_data:
        return None
    if row.sell_min > 0:
        return row.sell_min
    return None


def _lookup_price(
    index: dict[tuple[str, int], tuple[int, str]],
    item_id: str,
    quality: int,
) -> tuple[int | None, int | None, str | None]:
    """Reference price for an item, quality-aware with a quality-1 fall-back.

    Returns ``(unit_price, quality_used, city)``; ``(None, None, None)`` when
    neither the exact quality nor quality 1 has a real price.
    """
    hit = index.get((item_id, quality))
    if hit is not None:
        return hit[0], quality, hit[1]
    if quality != _FALLBACK_QUALITY:
        fallback = index.get((item_id, _FALLBACK_QUALITY))
        if fallback is not None:
            return fallback[0], _FALLBACK_QUALITY, fallback[1]
    return None, None, None


# ── tolerant coercion (pure) ─────────────────────────────────────────────────


def _first_str(value: Any) -> str | None:
    """A non-empty stripped string, or ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any, default: int = 0) -> int:
    """Best-effort int; junk (None, bool, non-numeric) yields ``default``."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (ValueError, TypeError):
            return default
    return default


def _clamp_quality(value: Any) -> int:
    """Coerce a ``Quality`` into the AODP 1-5 band (default 1)."""
    q = _int(value, default=_FALLBACK_QUALITY)
    if q < _MIN_QUALITY:
        return _MIN_QUALITY
    if q > _MAX_QUALITY:
        return _MAX_QUALITY
    return q


__all__ = [
    "Side",
    "estimate_value",
    "items_of",
]
