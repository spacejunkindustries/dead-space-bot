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
    from PIL import Image, ImageDraw, ImageFont

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
    ) -> bytes | None:
        """Render a kill card to PNG bytes, or ``None`` to fall back to an embed.

        ``event_row`` is any mapping-like row from the ``events`` table (a
        :class:`sqlite3.Row`, a ``dict``, or the parsed
        :class:`~killboard.model.EventRow`); its ``raw_json`` column, when
        present, supplies the equipment grid. ``participants`` drives the
        damage-contribution bars. ``loot_value`` (when the market layer priced
        the victim's loadout) is drawn on the card; ``None`` omits it.

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
            raw_event = _extract_raw_event(event_row)
            victim_gear = parse_equipment(raw_event, "Victim")
            killer_gear = parse_equipment(raw_event, "Killer")

            icons: dict[str, bytes] = {}
            for item in (*victim_gear, *killer_gear):
                if item.token in icons:
                    continue
                data = await self._icon_bytes(item, cards)
                if data is not None:
                    icons[item.token] = data

            shares = damage_shares(participants, _DAMAGE_ROWS)
            fields = _header_fields(event_row)
            brand = await self._brand_style(cards)
            show_value = loot_value if getattr(cards, "show_loot_value", True) else None

            return await self._to_thread(
                _compose_card, fields, victim_gear, killer_gear, icons, shares, brand, show_value
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
        """Return the PNG bytes for an item's icon, fetching once then caching.

        Checks the on-disk cache first (``icon_cache_dir/{token}.png``); on a
        miss, fetches from the render service, writes the file, and returns the
        bytes. Any failure (bad status, network error, unwritable cache) yields
        ``None`` and the item simply renders without an icon (§7.1).
        """
        path = icon_cache_path(cards.icon_cache_dir, item.token)

        cached = await self._to_thread(_read_file, path)
        if cached is not None:
            return cached

        url = render_icon_url(cards.render_base, item.item_type, item.enchant)
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


def _compose_card(
    fields: Mapping[str, Any],
    victim_gear: list[GearItem],
    killer_gear: list[GearItem],
    icons: Mapping[str, bytes],
    shares: list[DamageShare],
    brand: BrandStyle | None = None,
    loot_value: int | None = None,
) -> bytes | None:
    """Composite the final card to PNG bytes (killboard GDD §7.1). Pure Pillow.

    Runs entirely inside ``to_thread``. Returns ``None`` if Pillow raises for any
    reason, so the caller falls back to an embed-only post. ``brand`` skins the
    card (accent stripe, watermark, footer tagline); ``loot_value`` prints the
    victim's estimated silver loot when the market layer priced it.
    """
    try:
        canvas = Image.new("RGB", (_CARD_W, _CARD_H), _COLOR_BG)
        draw = ImageDraw.Draw(canvas)

        f_title = _load_font(26)
        f_body = _load_font(18)
        f_small = _load_font(15)

        # Header bar, colour-coded by relation.
        header = relation_color(fields.get("relation"))
        draw.rectangle((0, 0, _CARD_W, _HEADER_H), fill=header)
        # Brand accent stripe just beneath the header.
        if brand is not None:
            draw.rectangle((0, _HEADER_H, _CARD_W, _HEADER_H + _ACCENT_H), fill=brand.accent)
        title = f"{fields.get('killer_name', '?')}  ▸  {fields.get('victim_name', '?')}"
        draw.text((20, _HEADER_H // 2), title, font=f_title, fill=(255, 255, 255), anchor="lm")

        # Item power + fame line.
        ip_line = f"IP  {_fmt_ip(fields.get('killer_ip'))}  vs  {_fmt_ip(fields.get('victim_ip'))}"
        draw.text((20, _HEADER_H + 22), ip_line, font=f_body, fill=_COLOR_TEXT, anchor="lm")
        fame_line = f"Fame  {_fmt_fame(_coerce_int(fields.get('total_fame'), 0))}"
        draw.text(
            (_CARD_W - 20, _HEADER_H + 22), fame_line, font=f_body, fill=_COLOR_TEXT, anchor="rm"
        )
        # Estimated loot value (market layer), under the fame line when present.
        if loot_value is not None and loot_value > 0:
            accent = brand.accent if brand is not None else _DEAD_RED_RGB
            draw.text(
                (_CARD_W - 20, _HEADER_H + 44),
                f"Loot  {_fmt_fame(int(loot_value))}",
                font=f_small,
                fill=accent,
                anchor="rm",
            )

        # Victim gear grid (left side).
        gear_origin = (20, _HEADER_H + 52)
        draw.text(
            (gear_origin[0], gear_origin[1] - 18),
            "Victim loadout",
            font=f_small,
            fill=_COLOR_SUBTLE,
            anchor="lm",
        )
        positions = grid_positions(len(victim_gear), _GEAR_COLS, _ICON_CELL, gear_origin)
        for item, (x, y) in zip(victim_gear, positions, strict=False):
            draw.rectangle((x, y, x + _ICON_CELL, y + _ICON_CELL), fill=_COLOR_PANEL)
            data = icons.get(item.token)
            if data is not None:
                _paste_icon(canvas, data, (x, y), _ICON_CELL)

        # Killer loadout, compact single row beneath the victim grid.
        rows = (len(victim_gear) + _GEAR_COLS - 1) // _GEAR_COLS if victim_gear else 0
        k_origin = (20, gear_origin[1] + rows * (_ICON_CELL + _ICON_PAD) + 24)
        draw.text(
            (k_origin[0], k_origin[1] - 16),
            "Killer",
            font=f_small,
            fill=_COLOR_SUBTLE,
            anchor="lm",
        )
        k_cell = 40
        k_positions = grid_positions(len(killer_gear), 10, k_cell, k_origin, pad=6)
        for item, (x, y) in zip(killer_gear, k_positions, strict=False):
            draw.rectangle((x, y, x + k_cell, y + k_cell), fill=_COLOR_PANEL)
            data = icons.get(item.token)
            if data is not None:
                _paste_icon(canvas, data, (x, y), k_cell)

        # Damage-contribution bars (right side).
        bar_x = 470
        bar_w = _CARD_W - bar_x - 20
        bar_y = _HEADER_H + 52
        draw.text((bar_x, bar_y - 18), "Damage", font=f_small, fill=_COLOR_SUBTLE, anchor="lm")
        for share in shares:
            draw.rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + 18), fill=_COLOR_BAR_BG)
            fill_w = int(bar_w * share.fraction)
            if fill_w > 0:
                draw.rectangle((bar_x, bar_y, bar_x + fill_w, bar_y + 18), fill=_COLOR_BAR)
            label = f"{share.name}  {int(round(share.fraction * 100))}%"
            draw.text((bar_x + 6, bar_y + 9), label, font=f_small, fill=_COLOR_TEXT, anchor="lm")
            bar_y += 26

        # Footer: brand tagline · location · timestamp.
        brand_name = brand.name if brand is not None else ""
        footer_bits = [
            str(b) for b in (brand_name, fields.get("location"), fields.get("timestamp")) if b
        ]
        if footer_bits:
            draw.text(
                (20, _CARD_H - 18),
                "  ·  ".join(footer_bits),
                font=f_small,
                fill=_COLOR_SUBTLE,
                anchor="lm",
            )

        # Brand watermark, bottom-right corner.
        if brand is not None:
            _paste_logo(
                canvas,
                brand.logo,
                (_CARD_W - _LOGO_SIZE - 14, _CARD_H - _LOGO_SIZE - 26),
                _LOGO_SIZE,
                opacity=0.9,
            )

        buffer = io.BytesIO()
        canvas.save(buffer, format="PNG")
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
    """Bleed the reaper mascot faintly off the card's right edge; no-op if absent."""
    if not data:
        return
    try:
        with Image.open(io.BytesIO(data)) as img:
            mascot = img.convert("RGBA")
        scale = height / mascot.height
        new = mascot.resize((max(1, int(mascot.width * scale)), height))
        alpha = new.getchannel("A").point(lambda a: int(a * 0.12))
        new.putalpha(alpha)
        canvas.paste(new, (_RANK_W - new.width + 60, 0), new)
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
        _paste_logo(canvas, brand.logo, (_RANK_W - 84, 18), 56, opacity=0.95)

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
