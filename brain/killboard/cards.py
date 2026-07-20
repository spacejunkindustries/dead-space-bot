"""Kill-card image compositing with Pillow (killboard GDD §7.1).

A feed post is a Discord embed *plus* a composited kill-card PNG: a coloured
header (green when the tracked guild landed the kill, red when it took the
death), the victim's gear grid, item power per side, the kill fame, and a
damage-contribution bar list so group kills show who did the work.

Two rules shape this module:

* **Item icons come from the render service, never the gameinfo API** (§2.3).
  That host is documented, reliable, and cacheable, so each icon is fetched at
  most once and then served from an on-disk cache under
  ``cfg.killboard.cards.icon_cache_dir`` forever after (§7.1, §15).
* **Blocking work never touches the event loop** (GDD §14). All Pillow
  compositing and every disk read/write ride ``to_thread``; only the async
  ``aiohttp`` icon fetch runs on the loop.

The layout arithmetic — colours, gear parsing, damage shares, grid positions —
lives in small **pure helpers** at the bottom of the file so it is unit-testable
without Pillow, ``aiohttp``, or Discord. Everything is tolerant of the gameinfo
API's partial/missing fields (§2.4): a card with no gear still renders its
header, and any failure (disabled cards, Pillow missing, every icon fetch dead)
degrades to ``None`` so the feed falls back to an embed-only post.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import structlog

from killboard.config import USER_AGENT, render_icon_url
from killboard.model import Participant

try:  # Pillow is a declared dependency, but honour "Pillow fails → None" (§7.1).
    from PIL import Image, ImageChops, ImageDraw, ImageFont

    _PIL_OK = True
except Exception:  # pragma: no cover - only when Pillow is genuinely absent
    _PIL_OK = False

if TYPE_CHECKING:
    from cortana.config import KillboardConfig
    from killboard.rankings import DailyRanking

log = structlog.get_logger(__name__)

#: The bundled Dead Gaming roundel, used as the default card watermark when
#: ``cfg.killboard.cards.brand_logo_path`` is empty. Shipped as package data.
_BRAND_LOGO_DEFAULT: Path = Path(__file__).parent / "assets" / "brand_logo.png"

#: The bundled Dead reaper mascot, a faded side graphic on the ranking card.
_BRAND_MASCOT_DEFAULT: Path = Path(__file__).parent / "assets" / "brand_mascot.png"

#: Fallback accent when the configured ``accent_color`` can't be parsed — Dead
#: Gaming's brand red, matching :data:`killboard.rankings.DEAD_RED`.
_DEAD_RED_RGB: tuple[int, int, int] = (225, 18, 18)

#: Per-icon fetch timeout. Icons and feed posts are sequential, so an unbounded
#: fetch against a hung render host would stall the whole feed drain; this keeps
#: a bad host to a brief, bounded stall that degrades to a no-icon card.
_ICON_TIMEOUT = aiohttp.ClientTimeout(total=10.0)

#: Aggregate wall-clock budget for ALL of one card's icon fetches. A detailed
#: card can reference ~50 unique item types; fetching them serially with only a
#: per-icon timeout means a cold cache against a black-hole host could stall the
#: (sequential) feed drain for minutes. This caps the whole batch — icons not
#: fetched by the deadline just render icon-less, exactly as a failed fetch does.
_ICON_BATCH_TIMEOUT_S = 20.0


# ── layout constants ─────────────────────────────────────────────────────────

#: Victim equipment slots rendered on the card, in display order, paired with a
#: human label (killboard GDD §7.1). The keys are the gameinfo ``Equipment``
#: sub-object's slot names; a missing/empty slot is simply skipped.
GEAR_SLOTS: tuple[tuple[str, str], ...] = (
    ("MainHand", "weapon"),
    ("OffHand", "off-hand"),
    ("Head", "head"),
    ("Armor", "chest"),
    ("Shoes", "boots"),
    ("Cape", "cape"),
    ("Bag", "bag"),
    ("Mount", "mount"),
    ("Potion", "potion"),
    ("Food", "food"),
)

#: Header colours by guild relation (§7.1): green KILL, red DEATH, amber ASSIST.
_COLOR_KILL: tuple[int, int, int] = (67, 160, 71)
_COLOR_DEATH: tuple[int, int, int] = (229, 57, 53)
_COLOR_ASSIST: tuple[int, int, int] = (251, 192, 45)

_COLOR_BG: tuple[int, int, int] = (30, 33, 36)
_COLOR_PANEL: tuple[int, int, int] = (43, 47, 51)
_COLOR_TEXT: tuple[int, int, int] = (236, 237, 238)
_COLOR_SUBTLE: tuple[int, int, int] = (163, 166, 170)
_COLOR_BAR: tuple[int, int, int] = (88, 140, 204)
_COLOR_BAR_BG: tuple[int, int, int] = (58, 62, 66)

#: Overall card geometry (pixels).
_CARD_W: int = 820
_CARD_H: int = 360
_HEADER_H: int = 58
_ICON_CELL: int = 62
_ICON_PAD: int = 8
_GEAR_COLS: int = 5
_DAMAGE_ROWS: int = 5

#: Brand accent stripe height beneath the header, and watermark logo size.
_ACCENT_H: int = 4
_LOGO_SIZE: int = 44

# ── detailed Albion-style kill card ──────────────────────────────────────────

#: Detailed-card geometry. Killer paperdoll (left) + centre stats + victim
#: paperdoll (right), then damage/healing, then the dropped-inventory grid.
_DC_W: int = 860
_DC_CELL: int = 70  # equipment slot cell
_DC_INV_CELL: int = 50  # dropped-loot cell
_DC_INV_COLS: int = 14
#: Cap on dropped-loot items rendered (and icons fetched) per card — bounds the
#: card height and the first-render icon fetch burst on the shared feed drain.
_INV_MAX: int = 42

#: Item quality → border colour, matching Albion's in-game rarity tints
#: (Normal, Good, Outstanding, Excellent, Masterpiece).
_QUALITY_COLORS: dict[int, tuple[int, int, int]] = {
    1: (128, 130, 134),
    2: (86, 170, 74),
    3: (58, 130, 214),
    4: (150, 90, 200),
    5: (224, 176, 40),
}

#: Item-tier digit → roman numeral badge (T4 → IV …), drawn top-left of a cell.
_TIER_ROMAN: dict[int, str] = {
    1: "I",
    2: "II",
    3: "III",
    4: "IV",
    5: "V",
    6: "VI",
    7: "VII",
    8: "VIII",
}

#: The in-game equipment paperdoll: (col, row) → gameinfo Equipment slot name.
#: Mount sits centred on a fourth row (handled separately).
_ALBION_SLOTS: dict[tuple[int, int], str] = {
    (0, 0): "Bag",
    (1, 0): "Head",
    (2, 0): "Cape",
    (0, 1): "MainHand",
    (1, 1): "Armor",
    (2, 1): "OffHand",
    (0, 2): "Potion",
    (1, 2): "Shoes",
    (2, 2): "Food",
}
_MOUNT_SLOT: str = "Mount"

#: Damage-bar segment colours (cycled across contributors), and the healing tint.
_DC_DMG_SEGMENTS: tuple[tuple[int, int, int], ...] = (
    (196, 60, 55),
    (200, 130, 50),
    (210, 180, 60),
    (150, 110, 60),
)
_DC_HEAL_COLOR: tuple[int, int, int] = (60, 160, 90)

#: Opacity of the reaper-mascot background emblem on the detailed kill card —
#: faint enough to read as a Dead Gaming watermark behind the content, not
#: compete with it. The corner roundel is a separate, opaque mark.
_REAPER_OPACITY: float = 0.10

#: Daily-ranking card geometry (a taller two-column board).
_RANK_W: int = 720
_RANK_HEADER_H: int = 132
_RANK_ROWS: int = 10


@dataclass(frozen=True, slots=True)
class BrandStyle:
    """Resolved per-card branding read from ``cfg.killboard.cards`` (§7.1).

    ``accent`` is the parsed ``accent_color``; ``logo`` is the watermark PNG
    bytes (bundled roundel or a configured override), or ``None`` when the file
    is missing/unreadable — branding degrades to no-watermark, never an error.
    """

    name: str
    accent: tuple[int, int, int]
    logo: bytes | None


def parse_accent(value: str | None) -> tuple[int, int, int]:
    """Parse a ``#RRGGBB`` (or ``RRGGBB``) accent into an RGB tuple, tolerantly.

    A missing, malformed, or out-of-range value falls back to Dead Gaming's red
    (:data:`_DEAD_RED_RGB`) rather than raising — a bad colour in config must
    never break a card (§7.1).
    """
    if not value:
        return _DEAD_RED_RGB
    text = value.strip().lstrip("#")
    if len(text) != 6:
        return _DEAD_RED_RGB
    try:
        r, g, b = (int(text[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return _DEAD_RED_RGB
    return (r, g, b)


@dataclass(frozen=True, slots=True)
class GearItem:
    """One equipped item pulled from a side's ``Equipment`` (killboard GDD §7.1).

    ``item_type`` is the base render-service type with any ``@enchant`` suffix
    stripped off into ``enchant`` (0-3), so the icon URL and the on-disk cache
    key are built consistently regardless of how the API encoded the level.
    """

    slot: str
    item_type: str
    enchant: int
    quality: int

    @property
    def token(self) -> str:
        """Stable cache/identity token, e.g. ``T4_MAIN_SWORD@2`` or ``T4_BAG``."""
        return f"{self.item_type}@{self.enchant}" if self.enchant else self.item_type


@dataclass(frozen=True, slots=True)
class DamageShare:
    """One row of the damage-contribution list (killboard GDD §7.1).

    ``fraction`` is this participant's share of the total damage across the
    top-N shown (0.0-1.0), used to draw the bar width.
    """

    name: str
    damage: float
    fraction: float


class CardRenderer:
    """Composites kill-card PNGs, with an on-disk render-service icon cache.

    Constructed with:

    * ``cfg_provider`` — zero-arg callable returning the *current*
      :class:`~cortana.config.KillboardConfig`, read on every call so a hot
      reload of ``cards.enabled`` / ``render_base`` / ``icon_cache_dir`` applies
      to the next card without rebuilding the renderer.
    * ``to_thread`` — ``asyncio.to_thread`` (from the module context). Every
      Pillow op and every disk read/write is dispatched through it so the voice
      event loop never stalls (GDD §14).
    * ``session_provider`` — optional zero-arg callable returning a shared
      :class:`aiohttp.ClientSession` (e.g. the API client's). When omitted the
      renderer lazily creates and owns one; :meth:`close` frees it.
    """

    def __init__(
        self,
        cfg_provider: Callable[[], KillboardConfig],
        to_thread: Callable[..., Awaitable[Any]],
        session_provider: Callable[[], aiohttp.ClientSession] | None = None,
        log: Any = log,
    ) -> None:
        self._cfg_provider = cfg_provider
        self._to_thread = to_thread
        self._session_provider = session_provider
        self._own_session: aiohttp.ClientSession | None = None
        self._log = log
        #: path → logo bytes (or None if unreadable), read once off the loop.
        self._logo_cache: dict[str, bytes | None] = {}

    async def render(
        self,
        event_row: Mapping[str, Any] | Any,
        participants: list[Participant],
        *,
        loot_value: int | None = None,
        raw_event: Mapping[str, Any] | None = None,
    ) -> bytes | None:
        """Render a kill card to PNG bytes, or ``None`` to fall back to an embed.

        ``event_row`` supplies the header fields (names, item power, fame,
        location, timestamp, relation). The equipment grid comes from
        ``raw_event`` — the *parsed* full event dict (with ``Victim``/``Killer``/
        ``Equipment``), which the feed fetches via :meth:`KbStore.raw_event`. When
        ``raw_event`` is omitted it is decoded from ``event_row``'s ``raw_json``
        column if present; a bare :class:`~killboard.model.EventRow` carries no
        ``raw_json``, so passing ``raw_event`` explicitly is what makes the gear
        show. ``participants`` drives the damage bars; ``loot_value`` (when the
        market layer priced the loadout) is drawn on the card.

        Returns ``None`` — never raises — when cards are disabled, Pillow is
        unavailable, or anything in the fetch/composite path fails, so the feed
        can post an embed alone (§7.1). A card with no resolvable gear still
        renders (header, item power, fame, damage).
        """
        if not _PIL_OK:
            self._log.warning("kb_cards.pillow_unavailable")
            return None

        try:
            cards = self._cfg_provider().killboard.cards
            if not cards.enabled:
                return None
            raw = dict(raw_event) if raw_event is not None else _extract_raw_event(event_row)

            killer = raw.get("Killer") if isinstance(raw.get("Killer"), dict) else {}
            victim = raw.get("Victim") if isinstance(raw.get("Victim"), dict) else {}
            killer_equip = _slot_items(killer.get("Equipment"))
            victim_equip = _slot_items(victim.get("Equipment"))
            inventory = _inventory_items(victim.get("Inventory"))[:_INV_MAX]
            participants = _damage_rows(raw.get("Participants"))

            # Fetch an icon for every unique item Type across both loadouts and the
            # dropped inventory (cached on disk after the first render). The whole
            # batch is bounded by one wall-clock deadline so a degraded icon host
            # can't stall the sequential feed drain for minutes; icons past the
            # deadline just render icon-less (the ``icons`` dict fills in place, so
            # whatever was fetched before the timeout is kept).
            icons: dict[str, bytes] = {}

            async def _fetch_icons() -> None:
                for item_type in _unique_types(killer_equip, victim_equip, inventory):
                    data = await self._icon_for_type(item_type, cards)
                    if data is not None:
                        icons[item_type] = data

            try:
                await asyncio.wait_for(_fetch_icons(), timeout=_ICON_BATCH_TIMEOUT_S)
            except TimeoutError:
                self._log.warning("kb_cards.icon_batch_timeout", fetched=len(icons))

            brand = await self._brand_style(cards)
            show_value = loot_value if getattr(cards, "show_loot_value", True) else None
            header = _header_data(event_row, raw, killer, victim)
            mascot = (
                await self._cached_file(_BRAND_MASCOT_DEFAULT)
                if getattr(cards, "reaper_watermark", True)
                else None
            )

            return await self._to_thread(
                _compose_card,
                header,
                killer_equip,
                victim_equip,
                inventory,
                participants,
                icons,
                brand,
                show_value,
                mascot,
            )
        except Exception as exc:  # never let a card crash the feed (§7.1, §13)
            self._log.warning("kb_cards.render_failed", error=str(exc))
            return None

    async def render_ranking_card(
        self,
        ranking: DailyRanking,
        period_label: str,
        *,
        heading: str = "Daily Ranking",
        guild_name: str | None = None,
    ) -> bytes | None:
        """Render the branded Daily Ranking image (§8.3), or ``None`` to fall back.

        Composites the two guild-wide fame totals and the Top Kill/Death Fame
        boards onto a Dead-branded card — the image twin of
        :func:`killboard.rankings.daily_ranking_embed`. Returns ``None`` (never
        raises) when cards are disabled or Pillow is absent, so the scheduler
        posts the embed alone.
        """
        if not _PIL_OK:
            self._log.warning("kb_cards.pillow_unavailable")
            return None
        try:
            cards = self._cfg_provider().killboard.cards
            if not cards.enabled or not getattr(cards, "daily_ranking_card", True):
                return None
            brand = await self._brand_style(cards)
            mascot = await self._cached_file(_BRAND_MASCOT_DEFAULT)
            return await self._to_thread(
                _compose_ranking_card, ranking, period_label, heading, guild_name, brand, mascot
            )
        except Exception as exc:  # never let a card crash the scheduled post (§7.1)
            self._log.warning("kb_cards.ranking_render_failed", error=str(exc))
            return None

    async def _brand_style(self, cards: Any) -> BrandStyle:
        """Resolve the current branding: name, accent RGB, and watermark bytes."""
        return BrandStyle(
            name=str(getattr(cards, "brand_name", "") or ""),
            accent=parse_accent(getattr(cards, "accent_color", None)),
            logo=await self._logo_bytes(cards),
        )

    async def _logo_bytes(self, cards: Any) -> bytes | None:
        """The watermark PNG bytes (configured override or bundled roundel).

        Read once per path off the event loop and cached (including a ``None``
        for an unreadable path, so a missing file isn't re-statted every card).
        """
        override = str(getattr(cards, "brand_logo_path", "") or "").strip()
        path = Path(override) if override else _BRAND_LOGO_DEFAULT
        return await self._cached_file(path)

    async def _cached_file(self, path: Path) -> bytes | None:
        """Read a brand asset once per path off the loop, caching the result.

        A successful read and a *genuinely-absent* file are both cached (so a
        missing logo isn't re-statted every card). A ``None`` from a **transient**
        read error on a file that still exists is NOT cached, so a one-off OSError
        (e.g. fd exhaustion) can't permanently blank branding — the next card
        retries."""
        key = str(path)
        if key in self._logo_cache:
            return self._logo_cache[key]
        data = await self._to_thread(_read_file, path)
        if data is not None or not await self._to_thread(path.is_file):
            self._logo_cache[key] = data
        return data

    async def close(self) -> None:
        """Close the owned session, if one was created. Idempotent; a
        caller-supplied session (via ``session_provider``) is never touched."""
        if self._own_session is not None and not self._own_session.closed:
            await self._own_session.close()
        self._own_session = None

    # ── icon fetch + on-disk cache ───────────────────────────────────────────

    async def _icon_bytes(self, item: GearItem, cards: Any) -> bytes | None:
        """Icon PNG bytes for a parsed :class:`GearItem` (see :meth:`_icon_by_key`)."""
        return await self._icon_by_key(item.item_type, item.enchant, cards)

    async def _icon_for_type(self, item_type: str, cards: Any) -> bytes | None:
        """Icon PNG bytes for a raw API item ``Type`` (e.g. ``"T4_2H_AXE@1"``)."""
        base, enchant = split_enchant(str(item_type))
        return await self._icon_by_key(base, enchant, cards)

    async def _icon_by_key(self, base: str, enchant: int, cards: Any) -> bytes | None:
        """Return the PNG bytes for an item's icon, fetching once then caching.

        Checks the on-disk cache first (``icon_cache_dir/{token}.png``); on a
        miss, fetches from the render service, writes the file, and returns the
        bytes. Any failure (bad status, network error, unwritable cache) yields
        ``None`` and the item simply renders without an icon (§7.1).
        """
        token = f"{base}@{enchant}" if enchant else base
        path = icon_cache_path(cards.icon_cache_dir, token)

        cached = await self._to_thread(_read_file, path)
        if cached is not None:
            return cached

        url = render_icon_url(cards.render_base, base, enchant)
        try:
            session = await self._get_session()
            # Bound the icon fetch: neither the owned session nor the shared API
            # session sets a session-level timeout, so without this a hung render
            # host would block on aiohttp's 300s default and, since icons and feed
            # posts are sequential, wedge the whole feed drain for minutes.
            async with session.get(url, timeout=_ICON_TIMEOUT) as resp:
                if resp.status != 200:
                    self._log.warning("kb_cards.icon_status", url=url, status=resp.status)
                    return None
                data = await resp.read()
        except (TimeoutError, aiohttp.ClientError) as exc:
            self._log.warning("kb_cards.icon_fetch_failed", url=url, error=str(exc))
            return None

        try:
            await self._to_thread(_write_file, path, data)
        except OSError as exc:
            # A cache we can't write to is a warning, not a failure — still usable.
            self._log.warning("kb_cards.icon_cache_write_failed", path=str(path), error=str(exc))
        return data

    async def _get_session(self) -> aiohttp.ClientSession:
        """The shared session from the provider, or a lazily-created owned one."""
        if self._session_provider is not None:
            return self._session_provider()
        if self._own_session is None or self._own_session.closed:
            self._own_session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
        return self._own_session


# ── pure helpers (no Pillow / no I/O — unit-testable) ─────────────────────────


def relation_color(relation: str | None) -> tuple[int, int, int]:
    """Header colour for a guild relation (killboard GDD §7.1).

    Green for ``KILL`` (the guild got the kill), red for ``DEATH``, amber for
    ``ASSIST`` or anything unrecognised — a null/odd relation never crashes the
    card, it just reads as an assist-toned neutral.
    """
    key = (relation or "").strip().upper()
    if key == "KILL":
        return _COLOR_KILL
    if key == "DEATH":
        return _COLOR_DEATH
    return _COLOR_ASSIST


def split_enchant(item_type: str) -> tuple[str, int]:
    """Split a render-service item type into ``(base, enchant)`` (killboard GDD §7.1).

    ``"T4_MAIN_SWORD@2"`` → ``("T4_MAIN_SWORD", 2)``. A bare type yields enchant
    ``0``; a non-numeric or out-of-range (``>3``) suffix collapses to ``0`` since
    the render service only serves levels 1-3 (§2.3).
    """
    base, sep, tail = item_type.partition("@")
    if not sep:
        return item_type, 0
    try:
        level = int(tail)
    except ValueError:
        return base, 0
    return base, level if 1 <= level <= 3 else 0


def parse_equipment(raw_event: Mapping[str, Any], side: str) -> list[GearItem]:
    """Extract a side's equipped items in display order (killboard GDD §7.1).

    ``side`` is ``"Victim"`` or ``"Killer"``. Tolerant of the gameinfo API's
    partial data (§2.4): a missing player, missing ``Equipment``, empty slots,
    and null/typeless items are all skipped rather than raising, so a naked or
    partially-geared player yields a short list (possibly empty), never an error.
    """
    player = raw_event.get(side)
    equipment = player.get("Equipment") if isinstance(player, dict) else None
    if not isinstance(equipment, dict):
        return []

    items: list[GearItem] = []
    for api_slot, label in GEAR_SLOTS:
        entry = equipment.get(api_slot)
        if not isinstance(entry, dict):
            continue
        raw_type = entry.get("Type")
        if not raw_type:
            continue
        base, enchant = split_enchant(str(raw_type))
        items.append(
            GearItem(
                slot=label,
                item_type=base,
                enchant=enchant,
                quality=_coerce_int(entry.get("Quality"), 0),
            )
        )
    return items


def damage_shares(participants: list[Participant], top_n: int) -> list[DamageShare]:
    """Top participants by damage, with each one's share of the shown total.

    Sorted highest-damage first, capped at ``top_n``. ``fraction`` is normalised
    over the sum of *all* participants' damage so bars are comparable even when
    only the top few are shown; when nobody dealt damage every fraction is 0.0.
    A missing name renders as ``"?"``. (killboard GDD §7.1.)
    """
    total = sum(p.damage_done for p in participants if p.damage_done > 0)
    ranked = sorted(participants, key=lambda p: p.damage_done, reverse=True)
    out: list[DamageShare] = []
    for p in ranked[: max(top_n, 0)]:
        fraction = (p.damage_done / total) if total > 0 else 0.0
        out.append(
            DamageShare(
                name=p.player_name or "?",
                damage=p.damage_done,
                fraction=max(0.0, min(1.0, fraction)),
            )
        )
    return out


def grid_positions(
    count: int,
    cols: int,
    cell: int,
    origin: tuple[int, int],
    pad: int = _ICON_PAD,
) -> list[tuple[int, int]]:
    """Top-left ``(x, y)`` of each cell in a left-to-right, top-to-bottom grid.

    Pure geometry for the gear grid (killboard GDD §7.1): ``count`` cells of
    ``cell`` pixels laid out in ``cols`` columns from ``origin``, separated by
    ``pad``. ``cols`` is clamped to at least 1 so a bad config can't divide by
    zero.
    """
    ox, oy = origin
    columns = max(cols, 1)
    positions: list[tuple[int, int]] = []
    for i in range(max(count, 0)):
        row, col = divmod(i, columns)
        positions.append((ox + col * (cell + pad), oy + row * (cell + pad)))
    return positions


def icon_cache_path(cache_dir: str | Path, token: str) -> Path:
    """On-disk cache path for an item icon (killboard GDD §7.1).

    ``{cache_dir}/{token}.png`` with the token sanitised to a filesystem-safe
    name (item types never contain path separators, but this makes the mapping
    total and injection-proof).
    """
    safe = re.sub(r"[^A-Za-z0-9@._-]", "_", token) or "unknown"
    return Path(cache_dir) / f"{safe}.png"


def _first_str(value: Any) -> str | None:
    """A non-empty stripped string, or ``None`` (tolerant parse path §2.4)."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int(value: Any, default: int) -> int:
    """Best-effort int for the tolerant parse path (§2.4)."""
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


def _field(row: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a sqlite3.Row / dict / dataclass row, tolerantly.

    Handles the three shapes a feed might pass a card: a ``Mapping``, a
    :class:`sqlite3.Row` (has ``keys()`` and index access but no ``.get``), or a
    :class:`~killboard.model.EventRow` (attribute access). Missing → ``default``.
    """
    if isinstance(row, Mapping):
        return row.get(key, default)
    keys = getattr(row, "keys", None)
    if callable(keys):
        try:
            if key in keys():
                return row[key]
        except (KeyError, IndexError, TypeError):
            return default
    return getattr(row, key, default)


def _extract_raw_event(row: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Decode the retained ``raw_json`` column into a dict, or ``{}`` (§2.4, §5.4).

    Equipment lives only in the full stored event, not the flattened columns, so
    the card reads it back from ``raw_json``. A missing/blank/corrupt value (or a
    row that carries no ``raw_json`` at all, e.g. a bare ``EventRow``) yields an
    empty dict and the card simply renders without gear.
    """
    raw = _field(row, "raw_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _header_fields(row: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Project the header/footer text fields off a row into a plain dict.

    Everything the compositor draws that is *not* gear or damage: names, item
    power, fame, location, timestamp, and the relation that colours the header.
    Kept pure and Pillow-free so the compositor takes a simple mapping.
    """
    return {
        "relation": _field(row, "relation"),
        "killer_name": _field(row, "killer_name") or "Unknown",
        "victim_name": _field(row, "victim_name") or "Unknown",
        "killer_ip": _field(row, "killer_ip"),
        "victim_ip": _field(row, "victim_ip"),
        "total_fame": _coerce_int(_field(row, "total_fame"), 0),
        "location": _field(row, "location"),
        "timestamp": _field(row, "timestamp"),
    }


def _fmt_ip(value: Any) -> str:
    """Item power as a rounded integer string, or ``"?"`` when unknown."""
    if isinstance(value, int | float):
        return str(int(round(value)))
    return "?"


def _fmt_fame(value: int) -> str:
    """Fame with thousands separators, e.g. ``1,204,880``."""
    return f"{value:,}"


# ── disk I/O helpers (run under to_thread) ────────────────────────────────────


def _read_file(path: Path) -> bytes | None:
    """Read cached icon bytes, or ``None`` if absent OR empty. A 0-byte file is
    treated as a MISS (re-fetch), not a valid cache hit — otherwise a truncated
    write would blank that icon on every future card."""
    try:
        data = path.read_bytes()
    except (FileNotFoundError, OSError):
        return None
    return data or None


def _write_file(path: Path, data: bytes) -> None:
    """Write icon bytes to the cache ATOMICALLY (tmp file + os.replace), so a
    reader never observes a partial file if the process is killed mid-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        # If os.replace ran, tmp is gone; otherwise drop the partial temp file.
        with contextlib.suppress(FileNotFoundError, OSError):
            tmp.unlink()


# ── Pillow compositor (runs under to_thread) ──────────────────────────────────


def _load_font(size: int) -> Any:
    """A truetype font at ``size`` if the box has DejaVu, else Pillow's default."""
    for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    # No system DejaVu (e.g. a minimal CI runner). Pillow ≥10.1 bundles a
    # scalable default via load_default(size=...) that supports the anchor=
    # kwarg the compositor relies on; the bare load_default() returns a fixed
    # bitmap font that raises on anchors, so prefer the sized form.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - only on Pillow <10.1
        return ImageFont.load_default()


def _paste_icon(canvas: Any, data: bytes, box: tuple[int, int], size: int) -> None:
    """Decode PNG bytes and paste a resized RGBA icon; swallow decode errors."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            icon = img.convert("RGBA").resize((size, size))
        canvas.paste(icon, box, icon)
    except Exception:  # a single bad icon must not fail the whole card (§7.1)
        return


def _paste_logo(
    canvas: Any, data: bytes | None, box: tuple[int, int], size: int, opacity: float = 1.0
) -> None:
    """Paste a brand logo (RGBA) fit into a ``size`` box at ``opacity``; no-op on
    missing/bad data. Aspect ratio is preserved (letterboxed within the box)."""
    if not data:
        return
    try:
        with Image.open(io.BytesIO(data)) as img:
            logo = img.convert("RGBA")
        logo.thumbnail((size, size))
        if opacity < 1.0:
            alpha = logo.getchannel("A").point(lambda a: int(a * max(0.0, min(1.0, opacity))))
            logo.putalpha(alpha)
        ox = box[0] + (size - logo.width) // 2
        oy = box[1] + (size - logo.height) // 2
        canvas.paste(logo, (ox, oy), logo)
    except Exception:  # branding is decorative — never fail the card over it (§7.1)
        return


def _fmt_short(value: int) -> str:
    """Compact fame for a card: ``116.34k`` / ``68k`` / ``744`` / ``2.5m``."""
    v = int(value)
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.2f}".rstrip("0").rstrip(".") + "m"
    if abs(v) >= 1_000:
        return f"{v / 1_000:.2f}".rstrip("0").rstrip(".") + "k"
    return str(v)


# ── detailed-card data extraction (pure; tolerant of the API's partial data) ──


def _tier_of(item_type: str) -> int:
    """Tier digit from an item type prefix (``T4_...`` → 4), or 0 if absent."""
    m = re.match(r"[Tt]([1-8])_", item_type or "")
    return int(m.group(1)) if m else 0


def _slot_items(equipment: Any) -> dict[str, dict[str, Any]]:
    """A ``slot → item`` map for the paperdoll, keeping only real, typed slots."""
    if not isinstance(equipment, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for slot, item in equipment.items():
        if isinstance(item, dict) and item.get("Type"):
            out[str(slot)] = item
    return out


def _inventory_items(inventory: Any) -> list[dict[str, Any]]:
    """The victim's dropped inventory as a flat list of real, typed items."""
    if not isinstance(inventory, list):
        return []
    return [it for it in inventory if isinstance(it, dict) and it.get("Type")]


def _damage_rows(participants: Any) -> list[dict[str, Any]]:
    """Normalise the ``Participants`` list into ``{name, ip, damage, healing}``."""
    if not isinstance(participants, list):
        return []
    rows: list[dict[str, Any]] = []
    for p in participants:
        if not isinstance(p, dict):
            continue
        rows.append(
            {
                "name": _first_str(p.get("Name")) or "?",
                "ip": _coerce_int(p.get("AverageItemPower"), 0),
                "damage": _coerce_int(p.get("DamageDone"), 0),
                "healing": _coerce_int(p.get("SupportHealingDone"), 0),
            }
        )
    return rows


def _unique_types(*groups: Any) -> list[str]:
    """Every distinct item ``Type`` across slot-maps and inventory lists."""
    seen: dict[str, None] = {}
    for group in groups:
        items = group.values() if isinstance(group, dict) else group
        for it in items:
            if isinstance(it, dict) and it.get("Type"):
                seen.setdefault(str(it["Type"]), None)
    return list(seen)


def _header_data(
    event_row: Mapping[str, Any] | Any,
    raw: Mapping[str, Any],
    killer: Mapping[str, Any],
    victim: Mapping[str, Any],
) -> dict[str, Any]:
    """Header text: names, guilds, item power, fame, timestamp, relation.

    Names/guilds/IP come from the richer raw event; relation/timestamp fall back
    to the flat row so a card still labels itself when the raw event is sparse.
    """
    return {
        "relation": _field(event_row, "relation"),
        "killer_name": _first_str(killer.get("Name")) or _field(event_row, "killer_name") or "?",
        "victim_name": _first_str(victim.get("Name")) or _field(event_row, "victim_name") or "?",
        "killer_guild": _first_str(killer.get("GuildName")) or "",
        "victim_guild": _first_str(victim.get("GuildName")) or "",
        "killer_ip": _coerce_int(killer.get("AverageItemPower"), 0)
        or _coerce_int(_field(event_row, "killer_ip"), 0),
        "victim_ip": _coerce_int(victim.get("AverageItemPower"), 0)
        or _coerce_int(_field(event_row, "victim_ip"), 0),
        "fame": _coerce_int(_field(event_row, "total_fame"), 0)
        or _coerce_int(raw.get("TotalVictimKillFame"), 0),
        "timestamp": _first_str(_field(event_row, "timestamp")) or _first_str(raw.get("TimeStamp")),
    }


# ── detailed-card compositor (pure Pillow, runs under to_thread) ───────────────


def _scrim(
    canvas: Any, box: tuple[float, float, float, float], rgba: tuple[int, int, int, int]
) -> None:
    """Paint a translucent filled rectangle onto an RGB canvas.

    A plain ``draw.rectangle(fill=(r, g, b, a))`` on an RGB ``ImageDraw`` silently
    drops the alpha byte and paints the box fully opaque (the canvas has no alpha
    channel). To get the intended semi-transparent scrim we composite a small RGBA
    overlay through its own alpha instead.
    """
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    overlay = Image.new("RGBA", (w, h), rgba)
    canvas.paste(overlay, (x0, y0), overlay)


def _dc_cell(
    canvas: Any,
    draw: Any,
    item: Mapping[str, Any] | None,
    icons: Mapping[str, bytes],
    box: tuple[int, int],
    size: int,
    f_badge: Any,
    *,
    show_count: bool,
) -> None:
    """Draw one Albion-style item slot: quality border, icon, tier + count badges."""
    x, y = box
    if not item or not item.get("Type"):
        draw.rounded_rectangle(
            (x, y, x + size, y + size), radius=6, fill=(38, 41, 45), outline=(60, 64, 69), width=1
        )
        return
    item_type = str(item["Type"])
    quality = _coerce_int(item.get("Quality"), 1) or 1
    border = _QUALITY_COLORS.get(quality, _QUALITY_COLORS[1])
    draw.rounded_rectangle(
        (x, y, x + size, y + size), radius=6, fill=_COLOR_PANEL, outline=border, width=2
    )

    data = icons.get(item_type)
    if data is not None:
        try:
            with Image.open(io.BytesIO(data)) as img:
                pad = max(2, size // 20)
                ic = img.convert("RGBA").resize((size - 2 * pad, size - 2 * pad))
            canvas.paste(ic, (x + pad, y + pad), ic)
        except Exception:  # a single bad icon never fails the card (§7.1)
            pass

    tier = _tier_of(item_type)
    if tier:
        label = _TIER_ROMAN.get(tier, "")
        tw = draw.textlength(label, font=f_badge)
        _scrim(canvas, (x + 2, y + 2, x + 8 + tw, y + 18), (0, 0, 0, 160))
        draw.text((x + 5, y + 3), label, font=f_badge, fill=(235, 235, 235))
    count = _coerce_int(item.get("Count"), 1)
    if show_count and count > 1:
        s = str(count)
        sw = draw.textlength(s, font=f_badge)
        _scrim(
            canvas, (x + size - sw - 7, y + size - 17, x + size - 1, y + size - 1), (0, 0, 0, 160)
        )
        draw.text((x + size - sw - 4, y + size - 16), s, font=f_badge, fill=(255, 255, 210))


def _dc_paperdoll(
    canvas: Any,
    draw: Any,
    equip: dict[str, dict[str, Any]],
    icons: Mapping[str, bytes],
    origin: tuple[int, int],
    size: int,
    f_badge: Any,
) -> int:
    """Draw a 3×3 + mount equipment paperdoll; return the y just past its bottom."""
    ox, oy = origin
    step = size + 6
    for (col, row), slot in _ALBION_SLOTS.items():
        _dc_cell(
            canvas,
            draw,
            equip.get(slot),
            icons,
            (ox + col * step, oy + row * step),
            size,
            f_badge,
            show_count=False,
        )
    _dc_cell(
        canvas,
        draw,
        equip.get(_MOUNT_SLOT),
        icons,
        (ox + step, oy + 3 * step),
        size,
        f_badge,
        show_count=False,
    )
    return oy + 4 * step


def _reaper_background(content: Any, mascot: bytes | None) -> Any:
    """Composite the finished card over a faint, centred reaper emblem.

    ``content`` is the rendered card (opaque). The reaper is drawn on a fresh
    background at :data:`_REAPER_OPACITY`, then the card's non-background pixels
    are laid over it via a difference mask — so the emblem shows only through the
    card's empty gaps (centre column, margins), never over icons or text. Returns
    ``content`` unchanged on any failure (branding is decorative, §7.1)."""
    if not mascot:
        return content
    try:
        w, h = content.size
        with Image.open(io.BytesIO(mascot)) as img:
            m = img.convert("RGBA")
        target_h = int(h * 0.86)
        scale = target_h / m.height
        m = m.resize((max(1, int(m.width * scale)), target_h))
        alpha = m.getchannel("A").point(lambda a: int(a * _REAPER_OPACITY))
        m.putalpha(alpha)
        bg = Image.new("RGB", (w, h), _COLOR_BG)
        bg.paste(m, ((w - m.width) // 2, int(h * 0.07)), m)
        # Mask = where the card differs from a flat background → its real content.
        flat = Image.new("RGB", (w, h), _COLOR_BG)
        mask = (
            ImageChops.difference(content.convert("RGB"), flat)
            .convert("L")
            .point(lambda p: 255 if p > 2 else 0)
        )
        bg.paste(content, (0, 0), mask)
        return bg
    except Exception:  # emblem is decorative — never fail the card over it (§7.1)
        return content


def _compose_card(
    header: Mapping[str, Any],
    killer_equip: dict[str, dict[str, Any]],
    victim_equip: dict[str, dict[str, Any]],
    inventory: list[dict[str, Any]],
    participants: list[dict[str, Any]],
    icons: Mapping[str, bytes],
    brand: BrandStyle | None = None,
    loot_value: int | None = None,
    mascot: bytes | None = None,
) -> bytes | None:
    """Composite the detailed Albion-style kill card to PNG bytes (killboard §7.1).

    Killer paperdoll (left) and victim paperdoll (right) in the in-game slot
    layout, a centre column of party/fame/loot, damage + healing bars with their
    contributor lists, and the victim's dropped-inventory grid. Pure Pillow, run
    inside ``to_thread``. Returns ``None`` on any Pillow failure so the feed falls
    back to an embed-only post.
    """
    try:
        w = _DC_W
        canvas = Image.new("RGB", (w, 1600), _COLOR_BG)
        d = ImageDraw.Draw(canvas)
        f_guild = _load_font(15)
        f_name = _load_font(26)
        f_ip = _load_font(15)
        f_badge = _load_font(13)
        f_stat = _load_font(24)
        f_lbl = _load_font(13)
        f_row = _load_font(15)

        # ── header: killer (L) · timestamp (C) · victim (R) ──
        kg = header.get("killer_guild")
        vg = header.get("victim_guild")
        if kg:
            d.text((24, 14), f"[{kg}]", font=f_guild, fill=_COLOR_SUBTLE)
        d.text((24, 34), str(header.get("killer_name", "?")), font=f_name, fill=_COLOR_TEXT)
        d.text(
            (24, 66), f"IP {_coerce_int(header.get('killer_ip'), 0)}", font=f_ip, fill=_COLOR_SUBTLE
        )
        if vg:
            d.text((w - 24, 14), f"[{vg}]", font=f_guild, fill=_COLOR_SUBTLE, anchor="ra")
        d.text(
            (w - 24, 34),
            str(header.get("victim_name", "?")),
            font=f_name,
            fill=_COLOR_TEXT,
            anchor="ra",
        )
        d.text(
            (w - 24, 66),
            f"IP {_coerce_int(header.get('victim_ip'), 0)}",
            font=f_ip,
            fill=_COLOR_SUBTLE,
            anchor="ra",
        )
        ts = str(header.get("timestamp") or "")[:16].replace("T", " ")
        d.text((w // 2, 30), ts, font=f_ip, fill=_COLOR_SUBTLE, anchor="mm")
        accent = relation_color(header.get("relation"))
        d.rectangle((0, 92, w, 92 + _ACCENT_H), fill=accent)

        # ── paperdolls + centre stats ──
        cell = _DC_CELL
        y0 = 108
        k_ox = 24
        v_ox = w - 24 - (3 * cell + 2 * 6)
        bottom = _dc_paperdoll(canvas, d, killer_equip, icons, (k_ox, y0), cell, f_badge)
        _dc_paperdoll(canvas, d, victim_equip, icons, (v_ox, y0), cell, f_badge)

        cx = w // 2
        stats = [
            ("PARTY", f"{max(len(participants), 1):,}", _COLOR_TEXT),
            ("FAME", f"{_coerce_int(header.get('fame'), 0):,}", (240, 220, 120)),
        ]
        if loot_value is not None and loot_value > 0:
            stats.append(("LOOT", f"{int(loot_value):,}", (240, 220, 120)))
        sy = y0 + 22
        for label, value, colour in stats:
            d.text((cx, sy), label, font=f_lbl, fill=_COLOR_SUBTLE, anchor="mm")
            d.text((cx, sy + 22), value, font=f_stat, fill=colour, anchor="mm")
            sy += 74

        # ── damage / healing ──
        dmg = [p for p in participants if p["damage"] > 0]
        heal = [p for p in participants if p["healing"] > 0]
        dy = bottom + 16
        if dmg or heal:
            d.text((24, dy), "Damage", font=f_lbl, fill=_COLOR_SUBTLE)
            if heal:
                d.text((w // 2 + 16, dy), "Healing", font=f_lbl, fill=_COLOR_SUBTLE)
            dy += 20
            bar_l, bar_w = 24, w // 2 - 44
            total_d = sum(p["damage"] for p in dmg) or 1
            x = bar_l
            for i, p in enumerate(dmg):
                seg = int(bar_w * p["damage"] / total_d)
                if seg > 0:
                    d.rectangle(
                        (x, dy, x + seg, dy + 16), fill=_DC_DMG_SEGMENTS[i % len(_DC_DMG_SEGMENTS)]
                    )
                    x += seg
            hb_l = w // 2 + 16
            hb_w = w - 24 - hb_l
            if heal:
                d.rectangle((hb_l, dy, hb_l + hb_w, dy + 16), fill=_DC_HEAL_COLOR)
            dy += 24
            list_top = dy
            for p in dmg[:_DAMAGE_ROWS]:
                d.text((24, dy), f"{p['name']} [{p['ip']}]", font=f_row, fill=_COLOR_TEXT)
                d.text(
                    (w // 2 - 44, dy),
                    f"{p['damage']:,}",
                    font=f_row,
                    fill=(230, 120, 110),
                    anchor="ra",
                )
                dy += 22
            hy = list_top
            for p in heal[:_DAMAGE_ROWS]:
                d.text((hb_l, hy), f"{p['name']} [{p['ip']}]", font=f_row, fill=_COLOR_TEXT)
                d.text(
                    (w - 24, hy), f"{p['healing']:,}", font=f_row, fill=(120, 210, 140), anchor="ra"
                )
                hy += 22
            dy = max(dy, hy)
        dy += 12

        # ── dropped-loot grid ──
        if inventory:
            d.text((24, dy), "Dropped loot", font=f_lbl, fill=_COLOR_SUBTLE)
            dy += 20
            isz, ipad, cols = _DC_INV_CELL, 5, _DC_INV_COLS
            for i, it in enumerate(inventory):
                col, row = i % cols, i // cols
                _dc_cell(
                    canvas,
                    d,
                    it,
                    icons,
                    (24 + col * (isz + ipad), dy + row * (isz + ipad)),
                    isz,
                    f_badge,
                    show_count=True,
                )
            rows = (len(inventory) + cols - 1) // cols
            dy += rows * (isz + ipad)
        dy += 12

        # ── footer ──
        footer = brand.name if brand is not None else ""
        if footer:
            d.text((24, dy), footer, font=f_lbl, fill=_COLOR_SUBTLE)
        if loot_value is not None and loot_value > 0:
            d.text(
                (w - 24, dy),
                f"Total loot  {int(loot_value):,}",
                font=f_row,
                fill=(240, 220, 120),
                anchor="ra",
            )
        if brand is not None:
            _paste_logo(canvas, brand.logo, (w - 58, dy - 46), 40, opacity=0.9)
        dy += 26

        out = canvas.crop((0, 0, w, min(dy, canvas.height)))
        out = _reaper_background(out, mascot)
        buffer = io.BytesIO()
        out.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:  # pragma: no cover - defensive; any Pillow failure → embed
        log.warning("kb_cards.compose_failed")
        return None


def _rank_board(
    draw: Any,
    rows: list[tuple[str, int]],
    origin: tuple[int, int],
    right_x: int,
    fonts: tuple[Any, Any],
    accent: tuple[int, int, int],
) -> None:
    """Draw one numbered ``rank. name … value`` board column (killboard §8.3)."""
    f_row, f_rank = fonts
    x, y = origin
    if not rows:
        draw.text((x, y), "No activity.", font=f_row, fill=_COLOR_SUBTLE, anchor="lm")
        return
    width = len(str(len(rows)))
    for rank, (name, value) in enumerate(rows, start=1):
        draw.text((x, y), f"{str(rank).rjust(width)}.", font=f_rank, fill=accent, anchor="lm")
        draw.text((x + 34, y), _clip_name(name, 16), font=f_row, fill=_COLOR_TEXT, anchor="lm")
        draw.text((right_x, y), _fmt_short(value), font=f_row, fill=_COLOR_TEXT, anchor="rm")
        y += 26


def _clip_name(name: str, limit: int) -> str:
    """Truncate a player name to ``limit`` chars with an ellipsis when longer."""
    text = str(name)
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def _paste_mascot(canvas: Any, data: bytes | None, height: int) -> None:
    """Centre the reaper mascot faintly behind the ranking card; no-op if absent."""
    if not data:
        return
    try:
        with Image.open(io.BytesIO(data)) as img:
            mascot = img.convert("RGBA")
        scale = height / mascot.height
        new = mascot.resize((max(1, int(mascot.width * scale)), height))
        alpha = new.getchannel("A").point(lambda a: int(a * 0.12))
        new.putalpha(alpha)
        canvas.paste(new, ((_RANK_W - new.width) // 2, 0), new)
    except Exception:  # decorative — never fail the ranking card over it (§7.1)
        return


def _compose_ranking_card(
    ranking: DailyRanking,
    period_label: str,
    heading: str,
    guild_name: str | None,
    brand: BrandStyle,
    mascot: bytes | None = None,
) -> bytes | None:
    """Composite the branded Daily Ranking card to PNG bytes (killboard §8.3).

    Pure Pillow, run inside ``to_thread``. A header carrying the two guild-wide
    fame totals over a faded reaper, then Top Kill Fame (left) and Top Death Fame
    (right) boards. Returns ``None`` on any Pillow failure so the scheduler posts
    the embed alone.
    """
    try:
        n = min(_RANK_ROWS, max(len(ranking.top_kill_fame), len(ranking.top_death_fame), 1))
        height = _RANK_HEADER_H + 30 + n * 26 + 34
        canvas = Image.new("RGB", (_RANK_W, height), _COLOR_BG)
        draw = ImageDraw.Draw(canvas)

        _paste_mascot(canvas, mascot, height)

        f_title = _load_font(30)
        f_sub = _load_font(16)
        f_total = _load_font(20)
        f_head = _load_font(16)
        f_row = _load_font(17)
        f_rank = _load_font(15)

        # Top accent stripe + title block.
        draw.rectangle((0, 0, _RANK_W, _ACCENT_H + 2), fill=brand.accent)
        draw.text(
            (28, 22), f"☠  {heading.upper()}", font=f_title, fill=(255, 255, 255), anchor="lm"
        )
        draw.text((30, 52), period_label, font=f_sub, fill=_COLOR_SUBTLE, anchor="lm")
        # Roundel in the BOTTOM-right corner (out of the reaper mascot's way, which
        # bleeds down the right side); footer tagline sits bottom-left.
        _paste_logo(canvas, brand.logo, (_RANK_W - 58, height - 56), 46, opacity=0.95)

        # Guild-wide totals.
        draw.text(
            (28, 84),
            f"Total Kill Fame:  {_fmt_fame(ranking.total_kill_fame)}",
            font=f_total,
            fill=_COLOR_KILL,
            anchor="lm",
        )
        draw.text(
            (28, 110),
            f"Total Death Fame:  {_fmt_fame(ranking.total_death_fame)}",
            font=f_total,
            fill=_COLOR_DEATH,
            anchor="lm",
        )

        # Column headers + boards.
        head_y = _RANK_HEADER_H + 6
        col_l_x, col_l_right = 28, 348
        col_r_x, col_r_right = 372, _RANK_W - 24
        draw.text((col_l_x, head_y), "TOP KILL FAME", font=f_head, fill=_COLOR_KILL, anchor="lm")
        draw.text((col_r_x, head_y), "TOP DEATH FAME", font=f_head, fill=_COLOR_DEATH, anchor="lm")
        rows_y = head_y + 26
        _rank_board(
            draw,
            ranking.top_kill_fame[:n],
            (col_l_x, rows_y),
            col_l_right,
            (f_row, f_rank),
            brand.accent,
        )
        _rank_board(
            draw,
            ranking.top_death_fame[:n],
            (col_r_x, rows_y),
            col_r_right,
            (f_row, f_rank),
            brand.accent,
        )

        # Footer tagline.
        footer = " · ".join(p for p in (brand.name, guild_name) if p)
        if footer:
            draw.text((28, height - 18), footer, font=f_sub, fill=_COLOR_SUBTLE, anchor="lm")

        buffer = io.BytesIO()
        canvas.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:  # pragma: no cover - defensive; any Pillow failure → embed
        log.warning("kb_cards.ranking_compose_failed")
        return None


__all__ = [
    "GEAR_SLOTS",
    "BrandStyle",
    "CardRenderer",
    "DamageShare",
    "GearItem",
    "damage_shares",
    "grid_positions",
    "icon_cache_path",
    "parse_accent",
    "parse_equipment",
    "relation_color",
    "split_enchant",
]
