"""The killboard's *market* slash surface — AODP price lookups (killboard §13).

Where :mod:`killboard.commands` reads the local PvP event store, this cog reads
the **Albion Online Data Project** market API through :class:`~killboard.market.
MarketClient`. It is a rich, read-only lookup surface — current prices, cross-city
comparison, arbitrage, history, gold, and a quick stack appraisal — all under one
:class:`discord.app_commands.Group` named ``market`` (so ``/market price`` etc.).

Design boundaries mirror the rest of the killboard:

* **Never pings.** Every response goes out with
  ``allowed_mentions=discord.AllowedMentions.none()`` (CLAUDE.md constraint 11).
  Market data is informational; only CORTANA's ``decide_mentions()`` may escalate.
* **A flaky API costs freshness, never correctness.** The client already caches,
  retries, backs off on the AODP rate limits, and returns ``[]``/empty rather
  than raising; this cog turns "no rows" into a plain "nobody's scanned it
  recently" notice and *never* renders the AODP no-data sentinel (a ``0`` price on
  the ``0001-01-01`` epoch date) as a real free item.
* **Off by default.** ``cfg.killboard.market.enabled`` gates every command; a
  disabled or unwired market section degrades to a friendly ephemeral, never a
  crash. Config is read live through a zero-arg provider so a hot ``/reload`` of
  region / cities / quality applies to the next command.
* **Blocking work stays off the loop.** The item-database read *and* the fuzzy
  ``search``/``resolve`` scans (tens of ms over ~5k entries) are dispatched via
  ``asyncio.to_thread`` — autocomplete fires per keystroke and must never stall
  the shared voice loop — and the HTTP is async.

Item arguments accept either an AODP unique id (``T8_2H_HOLYSTAFF``) or a fuzzy
localized name ("holy staff"), resolved by :class:`~killboard.items.ItemIndex`,
and every one of them shares the same autocomplete coroutine
(:meth:`MarketCog.item_autocomplete`) backed by :meth:`ItemIndex.search`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
import structlog
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from collections.abc import Callable

    from cortana.config import KillboardConfig
    from killboard.items import ItemIndex
    from killboard.market import MarketClient, PriceRow

log = structlog.get_logger(__name__)

__all__ = ["MarketCog"]

# ── cities ────────────────────────────────────────────────────────────────────
#: The five royal cities plus Caerleon and Brecilien — everywhere you can hold a
#: market and place buy/sell orders. Order is stable for deterministic tables.
ROYAL_CITIES: tuple[str, ...] = (
    "Caerleon",
    "Bridgewatch",
    "Lymhurst",
    "Martlock",
    "Fort Sterling",
    "Thetford",
    "Brecilien",
)

#: The Black Market lives only in Caerleon and quotes sell-to (buy-order) prices —
#: it's where gear is flipped into the game's mob economy (killboard §13).
BLACK_MARKET: str = "Black Market"

#: Cities shown in a full price table: the royals plus the Black Market.
PRICE_CITIES: tuple[str, ...] = (*ROYAL_CITIES, BLACK_MARKET)

#: Case-folded name/alias → canonical city, for tolerant ``city`` arguments.
_CITY_CANON: dict[str, str] = {c.casefold(): c for c in PRICE_CITIES}
_CITY_CANON.update(
    {
        "fort": "Fort Sterling",
        "sterling": "Fort Sterling",
        "fs": "Fort Sterling",
        "caer": "Caerleon",
        "brec": "Brecilien",
        "bm": BLACK_MARKET,
        "blackmarket": BLACK_MARKET,
    }
)

#: AODP quality band (1..5) → human label.
QUALITY_NAMES: dict[int, str] = {
    1: "Normal",
    2: "Good",
    3: "Outstanding",
    4: "Excellent",
    5: "Masterpiece",
}

# ── display tuning ──────────────────────────────────────────────────────────────
#: A price older than this reads as stale (prices update on the minute at most, so
#: hours-old data is worth flagging).
_STALE_HOURS: int = 6
#: History window default / cap (days) and gold sample count.
_HISTORY_DEFAULT_DAYS: int = 7
_GOLD_SAMPLES: int = 24
#: Autocomplete choices returned per keystroke (Discord's hard cap is 25).
_AC_LIMIT: int = 25
#: Table column widths (monospace, mobile-friendly).
_W_CITY: int = 14
_W_NUM: int = 12

#: Embed accents.
_MARKET_COLOUR = 0xF1C40F  # gold — market data
_GOLD_COLOUR = 0xE1B12C  # deeper gold — the gold ticker

#: A coin marker for prose (kept out of monospace tables, where it misaligns).
_COIN = "🪙"

#: Reused, immutable "ping nobody" policy for every send (constraint 11).
_NO_PING = discord.AllowedMentions.none()

#: Unicode block ramp for text sparklines.
_SPARK = "▁▂▃▄▅▆▇█"
#: Cap sparkline width; longer series are evenly downsampled.
_SPARK_MAX = 32


class MarketCog(commands.Cog):
    """The ``/market *`` command surface over the AODP price API (killboard §13).

    Constructed by the killboard module's setup with the handles it already built:

    * ``bot`` — the shared discord.py client (service locator, mirrors
      :class:`~killboard.commands.KillboardCog`).
    * ``client`` — the :class:`~killboard.market.MarketClient` (its own cache and
      aiohttp session; closed by the module on shutdown).
    * ``items`` — the :class:`~killboard.items.ItemIndex` (id ↔ name, autocomplete).
    * ``cfg_provider`` — a zero-arg callable returning the *current*
      :class:`~cortana.config.KillboardConfig`, read live so a hot reload applies
      to the next command.

    Every command defers, does its async lookups, and replies with an embed and
    ``allowed_mentions=AllowedMentions.none()``. Nothing here raises into the
    interaction: a dead market or an unknown item becomes a plain notice.
    """

    market = app_commands.Group(
        name="market",
        description="Albion market prices: lookups, comparison, history, gold, and appraisal.",
    )

    def __init__(
        self,
        bot: commands.Bot,
        client: MarketClient,
        items: ItemIndex,
        cfg_provider: Callable[[], KillboardConfig],
    ) -> None:
        self.bot = bot
        self._client = client
        self._items = items
        self._cfg_provider = cfg_provider

    # ── shared config / service views ─────────────────────────────────────────

    def _cfg(self) -> KillboardConfig:
        return self._cfg_provider()

    def _market_section(self) -> object | None:
        return getattr(self._cfg(), "market", None)

    def _enabled(self) -> bool:
        """Whether the market layer is switched on (off by default, §12)."""
        return bool(getattr(self._market_section(), "enabled", False))

    def _quality(self, quality: int | None) -> int:
        """Resolve an explicit ``quality`` or fall back to the configured default."""
        if quality is not None:
            return int(quality)
        return int(getattr(self._market_section(), "default_quality", 1) or 1)

    # ── autocomplete (shared by every item argument) ──────────────────────────

    async def item_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Offer ``(display name → item id)`` choices for a partial item query.

        Backed by :meth:`ItemIndex.search`; the *value* returned is the AODP unique
        id so the command handler gets an exact id even when the user picks a
        localized name. Loads the item index lazily (a no-op after the first call)
        and never raises out of the callback — a failure just yields no choices.
        """
        try:
            await self._items.load()
            # search() fuzzy-scans ~5k entries (tens of ms); Discord fires this
            # per keystroke. Run it off the shared event loop so it never stalls
            # the voice path (constraint: blocking work never on the loop).
            hits = await asyncio.to_thread(self._items.search, current, _AC_LIMIT)
        except Exception as exc:  # noqa: BLE001 — autocomplete must never raise
            log.warning("kb_market.autocomplete_failed", error=str(exc))
            return []
        return [
            app_commands.Choice(name=_clip(display, 100), value=_clip(item_id, 100))
            for item_id, display in hits
        ]

    # ── /market price ─────────────────────────────────────────────────────────

    @market.command(
        name="price", description="Current buy/sell for an item — one city or every royal market."
    )
    @app_commands.describe(
        item="item name or AODP id (e.g. 'holy staff' or T8_2H_HOLYSTAFF@3)",
        city="a single city, or leave blank for all royal cities + Black Market",
        quality="1 Normal · 2 Good · 3 Outstanding · 4 Excellent · 5 Masterpiece",
    )
    @app_commands.autocomplete(item=item_autocomplete)
    async def price(
        self,
        interaction: discord.Interaction,
        item: str,
        city: str | None = None,
        quality: app_commands.Range[int, 1, 5] | None = None,
    ) -> None:
        """Current sell/buy for one item, in one city or a full royal table."""
        await interaction.response.defer(thinking=True)
        if not await self._guard(interaction):
            return
        resolved = await self._resolve(interaction, item)
        if resolved is None:
            return
        item_id, name = resolved
        q = self._quality(quality)

        if city is not None:
            canon = _canon_city(city)
            if canon is None:
                await self._text(interaction, _bad_city(city))
                return
            cities: list[str] = [canon]
        else:
            cities = list(PRICE_CITIES)

        rows = await self._client.prices([item_id], cities=cities, qualities=[q])
        by_city = _index_by_city(rows)
        now = datetime.now(UTC)

        if not _any_data(rows):
            await self._text(interaction, _no_data(name, cities[0] if city else None))
            return

        embed = discord.Embed(
            title=f"{name} · {_quality_label(q)}",
            colour=discord.Colour(_MARKET_COLOUR),
            timestamp=now,
        )
        embed.description = _price_table(cities, by_city, now)
        _stale_footer(embed, rows, now, item_id)
        await self._embed(interaction, embed)

    # ── /market compare ───────────────────────────────────────────────────────

    @market.command(
        name="compare", description="Sell & buy for an item across every market — the flip view."
    )
    @app_commands.describe(
        item="item name or AODP id",
        quality="1 Normal · 2 Good · 3 Outstanding · 4 Excellent · 5 Masterpiece",
    )
    @app_commands.autocomplete(item=item_autocomplete)
    async def compare(
        self,
        interaction: discord.Interaction,
        item: str,
        quality: app_commands.Range[int, 1, 5] | None = None,
    ) -> None:
        """Every city side by side, with the cheapest buy, best sell, and spread."""
        await interaction.response.defer(thinking=True)
        if not await self._guard(interaction):
            return
        resolved = await self._resolve(interaction, item)
        if resolved is None:
            return
        item_id, name = resolved
        q = self._quality(quality)

        cities = list(PRICE_CITIES)
        rows = await self._client.prices([item_id], cities=cities, qualities=[q])
        now = datetime.now(UTC)
        if not _any_data(rows):
            await self._text(interaction, _no_data(name))
            return

        by_city = _index_by_city(rows)
        cheapest = _cheapest_buy(rows)
        best = _best_sell(rows)

        embed = discord.Embed(
            title=f"Flip check · {name} · {_quality_label(q)}",
            colour=discord.Colour(_MARKET_COLOUR),
            timestamp=now,
        )
        embed.description = _price_table(cities, by_city, now)
        if cheapest is not None:
            embed.add_field(
                name="⬇ Cheapest to buy",
                value=f"**{cheapest.city}** — {_silver(cheapest.sell_min)}",
                inline=True,
            )
        if best is not None:
            embed.add_field(
                name="⬆ Best to sell (top buy order)",
                value=f"**{best.city}** — {_silver(best.buy_max)}",
                inline=True,
            )
        if cheapest is not None and best is not None:
            spread = best.buy_max - cheapest.sell_min
            pct = _pct(spread, cheapest.sell_min)
            sign = "+" if spread >= 0 else ""
            embed.add_field(
                name="Spread (buy → instant sell)",
                value=f"{sign}{_silver(spread)}  ({sign}{pct:.1f}%)",
                inline=False,
            )
        await self._embed(interaction, embed)

    # ── /market cheapest ──────────────────────────────────────────────────────

    @market.command(name="cheapest", description="Where to BUY an item cheapest right now.")
    @app_commands.describe(item="item name or AODP id", quality="quality 1–5 (default 1)")
    @app_commands.autocomplete(item=item_autocomplete)
    async def cheapest(
        self,
        interaction: discord.Interaction,
        item: str,
        quality: app_commands.Range[int, 1, 5] | None = None,
    ) -> None:
        """The single cheapest sell listing across the royal cities."""
        await interaction.response.defer(thinking=True)
        if not await self._guard(interaction):
            return
        resolved = await self._resolve(interaction, item)
        if resolved is None:
            return
        item_id, name = resolved
        q = self._quality(quality)

        rows = await self._client.prices([item_id], cities=list(ROYAL_CITIES), qualities=[q])
        now = datetime.now(UTC)
        cheapest = _cheapest_buy(rows)
        if cheapest is None:
            await self._text(interaction, _no_data(name))
            return

        embed = discord.Embed(
            title=f"Cheapest {name} · {_quality_label(q)}",
            colour=discord.Colour(_MARKET_COLOUR),
            description=(
                f"Buy in **{cheapest.city}** for **{_silver(cheapest.sell_min)}**\n"
                f"_priced {_age(cheapest.sell_min_date, now)} ago_"
            ),
            timestamp=now,
        )
        await self._embed(interaction, embed)

    # ── /market best ──────────────────────────────────────────────────────────

    @market.command(name="best", description="Where to SELL an item highest right now.")
    @app_commands.describe(item="item name or AODP id", quality="quality 1–5 (default 1)")
    @app_commands.autocomplete(item=item_autocomplete)
    async def best(
        self,
        interaction: discord.Interaction,
        item: str,
        quality: app_commands.Range[int, 1, 5] | None = None,
    ) -> None:
        """The single highest buy order (instant sale) across every market."""
        await interaction.response.defer(thinking=True)
        if not await self._guard(interaction):
            return
        resolved = await self._resolve(interaction, item)
        if resolved is None:
            return
        item_id, name = resolved
        q = self._quality(quality)

        rows = await self._client.prices([item_id], cities=list(PRICE_CITIES), qualities=[q])
        now = datetime.now(UTC)
        best = _best_sell(rows)
        if best is None:
            await self._text(
                interaction,
                f"No buy orders for **{name}** anywhere right now — "
                "nobody's bidding, so there's nothing to instant-sell into.",
            )
            return

        embed = discord.Embed(
            title=f"Best sale · {name} · {_quality_label(q)}",
            colour=discord.Colour(_MARKET_COLOUR),
            description=(
                f"Sell in **{best.city}** for **{_silver(best.buy_max)}** "
                "(top buy order — instant sale)\n"
                f"_bid {_age(best.buy_max_date, now)} ago_"
            ),
            timestamp=now,
        )
        await self._embed(interaction, embed)

    # ── /market history ───────────────────────────────────────────────────────

    @market.command(name="history", description="Price trend for an item over a window.")
    @app_commands.describe(
        item="item name or AODP id",
        city="market to chart (default Caerleon)",
        days="window length in days (1–30, default 7)",
    )
    @app_commands.autocomplete(item=item_autocomplete)
    async def history(
        self,
        interaction: discord.Interaction,
        item: str,
        city: str | None = None,
        days: app_commands.Range[int, 1, 30] = _HISTORY_DEFAULT_DAYS,
    ) -> None:
        """Min/avg/max plus a text sparkline over the requested window."""
        await interaction.response.defer(thinking=True)
        if not await self._guard(interaction):
            return
        resolved = await self._resolve(interaction, item)
        if resolved is None:
            return
        item_id, name = resolved

        town = "Caerleon"
        if city is not None:
            canon = _canon_city(city)
            if canon is None:
                await self._text(interaction, _bad_city(city))
                return
            town = canon

        points = await self._client.history(item_id, town, int(days))
        prices = [int(p["avg_price"]) for p in points if int(p.get("avg_price", 0)) > 0]
        if not prices:
            await self._text(
                interaction,
                f"No price history for **{name}** in **{town}** over the last {int(days)}d — "
                "nobody's scanned it recently.",
            )
            return

        volume = sum(int(p.get("item_count", 0)) for p in points)
        embed = discord.Embed(
            title=f"{name} · {town} · last {int(days)}d",
            colour=discord.Colour(_MARKET_COLOUR),
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Low", value=_silver(min(prices)), inline=True)
        embed.add_field(name="Avg", value=_silver(sum(prices) // len(prices)), inline=True)
        embed.add_field(name="High", value=_silver(max(prices)), inline=True)
        embed.add_field(name="Volume", value=f"{volume:,} traded", inline=True)
        embed.add_field(name="Data points", value=str(len(prices)), inline=True)
        spark = _sparkline(prices)
        if spark:
            embed.add_field(name="Trend (oldest → newest)", value=f"`{spark}`", inline=False)
        await self._embed(interaction, embed)

    # ── /market gold ──────────────────────────────────────────────────────────

    @market.command(name="gold", description="Current gold price and its recent trend.")
    async def gold(self, interaction: discord.Interaction) -> None:
        """The latest silver-per-gold price with an up/down trend and sparkline."""
        await interaction.response.defer(thinking=True)
        if not await self._guard(interaction):
            return

        samples = await self._client.gold(_GOLD_SAMPLES)
        prices = [int(s["price"]) for s in samples if int(s.get("price", 0)) > 0]
        if not prices:
            await self._text(
                interaction,
                "No gold price data right now — the gold endpoint returned nothing.",
            )
            return

        current = prices[-1]
        first = prices[0]
        change = current - first
        pct = _pct(change, first)
        if change > 0:
            arrow, word = "▲", "up"
        elif change < 0:
            arrow, word = "▼", "down"
        else:
            arrow, word = "▬", "flat"

        embed = discord.Embed(
            title="Gold price",
            colour=discord.Colour(_GOLD_COLOUR),
            description=f"**{current:,}** silver per gold",
            timestamp=datetime.now(UTC),
        )
        embed.add_field(
            name=f"Trend ({len(prices)} samples)",
            value=f"{arrow} {word} {abs(change):,} ({pct:+.1f}%)",
            inline=True,
        )
        spark = _sparkline(prices)
        if spark:
            embed.add_field(name="History (oldest → newest)", value=f"`{spark}`", inline=False)
        await self._embed(interaction, embed)

    # ── /market value ─────────────────────────────────────────────────────────

    @market.command(name="value", description="Quick appraisal of an item stack across cities.")
    @app_commands.describe(
        item="item name or AODP id",
        quality="quality 1–5 (default 1)",
        count="stack size to appraise (default 1)",
    )
    @app_commands.autocomplete(item=item_autocomplete)
    async def value(
        self,
        interaction: discord.Interaction,
        item: str,
        quality: app_commands.Range[int, 1, 5] | None = None,
        count: app_commands.Range[int, 1, 1_000_000] = 1,
    ) -> None:
        """Appraise a stack at both the cheapest buy and the best instant sale."""
        await interaction.response.defer(thinking=True)
        if not await self._guard(interaction):
            return
        resolved = await self._resolve(interaction, item)
        if resolved is None:
            return
        item_id, name = resolved
        q = self._quality(quality)
        n = int(count)

        rows = await self._client.prices([item_id], cities=list(PRICE_CITIES), qualities=[q])
        cheapest = _cheapest_buy(rows)
        best = _best_sell(rows)
        if cheapest is None and best is None:
            await self._text(interaction, _no_data(name))
            return

        embed = discord.Embed(
            title=f"Appraisal · {n:,} × {name} · {_quality_label(q)}",
            colour=discord.Colour(_MARKET_COLOUR),
            timestamp=datetime.now(UTC),
        )
        if cheapest is not None:
            embed.add_field(
                name="Buy cost (cheapest listing)",
                value=(
                    f"**{_silver(cheapest.sell_min * n)}**\n"
                    f"_{_silver(cheapest.sell_min)} × {n:,} in {cheapest.city}_"
                ),
                inline=False,
            )
        if best is not None:
            embed.add_field(
                name="Instant-sell value (top buy order)",
                value=(
                    f"**{_silver(best.buy_max * n)}**\n"
                    f"_{_silver(best.buy_max)} × {n:,} in {best.city}_"
                ),
                inline=False,
            )
        await self._embed(interaction, embed)

    # ── /market blackmarket ───────────────────────────────────────────────────

    @market.command(
        name="blackmarket", description="Black Market (Caerleon) sell prices — the flip target."
    )
    @app_commands.describe(item="item name or AODP id", quality="quality 1–5 (default 1)")
    @app_commands.autocomplete(item=item_autocomplete)
    async def blackmarket(
        self,
        interaction: discord.Interaction,
        item: str,
        quality: app_commands.Range[int, 1, 5] | None = None,
    ) -> None:
        """What the Black Market is paying for an item (its buy orders)."""
        await interaction.response.defer(thinking=True)
        if not await self._guard(interaction):
            return
        resolved = await self._resolve(interaction, item)
        if resolved is None:
            return
        item_id, name = resolved
        q = self._quality(quality)

        rows = await self._client.prices([item_id], cities=[BLACK_MARKET], qualities=[q])
        now = datetime.now(UTC)
        row = _index_by_city(rows).get(BLACK_MARKET.casefold())
        if row is None or not (_has_sell(row) or _has_buy(row)):
            await self._text(interaction, _no_data(name, BLACK_MARKET))
            return

        embed = discord.Embed(
            title=f"Black Market · {name} · {_quality_label(q)}",
            colour=discord.Colour(_MARKET_COLOUR),
            timestamp=now,
        )
        if _has_buy(row):
            embed.add_field(
                name="Sell to BM (top buy order)",
                value=f"**{_silver(row.buy_max)}**\n_bid {_age(row.buy_max_date, now)} ago_",
                inline=True,
            )
        else:
            embed.add_field(name="Sell to BM (top buy order)", value="no buy orders", inline=True)
        if _has_sell(row):
            embed.add_field(
                name="BM listing (sell order)",
                value=f"{_silver(row.sell_min)}\n_priced {_age(row.sell_min_date, now)} ago_",
                inline=True,
            )
        embed.set_footer(text="Black Market is Caerleon-only; its buy orders are the demand.")
        await self._embed(interaction, embed)

    # ── shared plumbing ───────────────────────────────────────────────────────

    async def _guard(self, interaction: discord.Interaction) -> bool:
        """Return True if the market layer is enabled; else send a notice and stop."""
        if self._enabled():
            return True
        await self._text(
            interaction,
            "Market data is turned off. An admin can enable it under "
            "`killboard.market` in `cortana.yaml`, then `/reload`.",
        )
        return False

    async def _resolve(self, interaction: discord.Interaction, item: str) -> tuple[str, str] | None:
        """Resolve an item arg to ``(item_id, display_name)`` or send a miss notice.

        Accepts an exact AODP id or a fuzzy localized name. Loads the item index
        lazily. On no match, sends a friendly notice and returns ``None`` so the
        caller can bail.
        """
        await self._items.load()
        # resolve() fuzzy-scans ~5k entries; keep it off the shared event loop.
        item_id = await asyncio.to_thread(self._items.resolve, item)
        if item_id is None:
            await self._text(
                interaction,
                f"Couldn't find an Albion item matching **{_clip(item, 64)}** — "
                "try a fuller name (e.g. 'elder holy staff') or an exact id "
                "(e.g. `T8_2H_HOLYSTAFF`).",
            )
            return None
        name = self._items.name_of(item_id) or item_id
        return item_id, name

    async def _embed(self, interaction: discord.Interaction, embed: discord.Embed) -> None:
        await interaction.followup.send(embed=embed, allowed_mentions=_NO_PING)

    async def _text(self, interaction: discord.Interaction, message: str) -> None:
        """Send an informational / error notice — always ephemeral.

        Every notice path (disabled market, item miss, bad city, no data) routes
        through here, so keeping it ephemeral honours the module's "degrades to a
        friendly ephemeral" contract and stops an off-by-default feature from
        spamming the channel with public "Market data is turned off" replies.
        Successful price data goes through :meth:`_embed`, which stays public.
        """
        await interaction.followup.send(message, allowed_mentions=_NO_PING, ephemeral=True)


# ── pure helpers (no discord, no I/O) ───────────────────────────────────────────


def _canon_city(name: str) -> str | None:
    """Canonicalize a free-typed city name/alias, or ``None`` if unrecognized."""
    return _CITY_CANON.get(name.strip().casefold())


def _bad_city(name: str) -> str:
    """A notice listing the valid city names for a rejected ``city`` argument."""
    return (
        f"Unknown city **{_clip(name, 40)}**. Valid: "
        + ", ".join(PRICE_CITIES)
        + " (Black Market is Caerleon-only)."
    )


def _quality_label(quality: int) -> str:
    """``"Q3 Outstanding"`` for a quality band."""
    name = QUALITY_NAMES.get(quality, "")
    return f"Q{quality} {name}".strip()


def _silver(value: int) -> str:
    """A silver amount with thousands separators and a coin marker."""
    return f"{value:,} {_COIN}"


def _pct(part: int, whole: int) -> float:
    """``part`` as a percentage of ``whole`` (0.0 when ``whole`` is 0)."""
    if whole == 0:
        return 0.0
    return part / whole * 100.0


def _index_by_city(rows: list[PriceRow]) -> dict[str, PriceRow]:
    """Map case-folded city → its row (last write wins; one row per city expected)."""
    return {row.city.casefold(): row for row in rows}


def _has_sell(row: PriceRow) -> bool:
    """True when the row carries a real sell listing (not the no-data sentinel)."""
    return row.sell_min > 0 and _parse_dt(row.sell_min_date) is not None


def _has_buy(row: PriceRow) -> bool:
    """True when the row carries a real buy order (not the no-data sentinel)."""
    return row.buy_max > 0 and _parse_dt(row.buy_max_date) is not None


def _any_data(rows: list[PriceRow]) -> bool:
    """True when at least one row has a usable sell or buy price."""
    return any(_has_sell(r) or _has_buy(r) for r in rows)


def _cheapest_buy(rows: list[PriceRow]) -> PriceRow | None:
    """The row with the lowest real sell price (cheapest to buy), or ``None``."""
    candidates = [r for r in rows if _has_sell(r)]
    if not candidates:
        return None
    return min(candidates, key=lambda r: r.sell_min)


def _best_sell(rows: list[PriceRow]) -> PriceRow | None:
    """The row with the highest real buy order (best instant sale), or ``None``."""
    candidates = [r for r in rows if _has_buy(r)]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.buy_max)


def _price_table(cities: list[str], by_city: dict[str, PriceRow], now: datetime) -> str:
    """Render a monospace ``City | Sell | Buy | Age`` table for the given cities.

    No-data cities and no-data fields show ``—`` rather than a misleading ``0``.
    Age is taken from whichever of the sell/buy prices is present.
    """
    header = f"{'City':<{_W_CITY}}{'Sell':>{_W_NUM}}{'Buy':>{_W_NUM}}  Age"
    lines = [header]
    for city in cities:
        row = by_city.get(city.casefold())
        if row is None:
            lines.append(f"{city:<{_W_CITY}}{'—':>{_W_NUM}}{'—':>{_W_NUM}}  —")
            continue
        sell = f"{row.sell_min:,}" if _has_sell(row) else "—"
        buy = f"{row.buy_max:,}" if _has_buy(row) else "—"
        if _has_sell(row):
            age = _age(row.sell_min_date, now)
        elif _has_buy(row):
            age = _age(row.buy_max_date, now)
        else:
            age = "—"
        lines.append(f"{city:<{_W_CITY}}{sell:>{_W_NUM}}{buy:>{_W_NUM}}  {age}")
    return "```\n" + "\n".join(lines) + "\n```"


def _stale_footer(embed: discord.Embed, rows: list[PriceRow], now: datetime, item_id: str) -> None:
    """Add a footer flagging that the freshest quote is older than the stale bound."""
    freshest = _freshest_dt(rows)
    tag = f"id: {item_id}"
    if freshest is not None and (now - freshest).total_seconds() > _STALE_HOURS * 3600:
        embed.set_footer(text=f"⚠️ Stale — freshest quote {_age_dt(freshest, now)} ago · {tag}")
    else:
        embed.set_footer(text=tag)


def _freshest_dt(rows: list[PriceRow]) -> datetime | None:
    """The most recent parseable price date across all rows/fields, or ``None``."""
    dts: list[datetime] = []
    for row in rows:
        for raw in (row.sell_min_date, row.buy_max_date, row.sell_max_date, row.buy_min_date):
            dt = _parse_dt(raw)
            if dt is not None:
                dts.append(dt)
    return max(dts) if dts else None


def _sparkline(values: list[int]) -> str:
    """A unicode block sparkline for a numeric series (downsampled to a max width)."""
    nums = [v for v in values if isinstance(v, int)]
    if not nums:
        return ""
    nums = _downsample(nums, _SPARK_MAX)
    lo = min(nums)
    hi = max(nums)
    if hi <= lo:
        return _SPARK[len(_SPARK) // 2] * len(nums)
    span = hi - lo
    return "".join(_SPARK[int((v - lo) / span * (len(_SPARK) - 1))] for v in nums)


def _downsample(values: list[int], width: int) -> list[int]:
    """Evenly reduce ``values`` to at most ``width`` samples (identity if shorter)."""
    if len(values) <= width or width <= 0:
        return values
    step = len(values) / width
    return [values[int(i * step)] for i in range(width)]


def _no_data(name: str, where: str | None = None) -> str:
    """The standard 'nobody's scanned it' notice — never a fake 0."""
    loc = f" in **{where}**" if where else ""
    return (
        f"No market data for **{name}**{loc} — nobody's scanned it recently. "
        "Prices come from players running the Albion Data client, so an unscanned "
        "item shows nothing rather than a misleading 0."
    )


def _clip(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` chars with an ellipsis when it overruns."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an AODP ISO date, or ``None`` for the epoch sentinel / junk.

    The AODP no-data marker (``0001-01-01T00:00:00``) parses to ``None`` so it can
    never be mistaken for a real observation. A naive stamp is assumed UTC.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.startswith("0001-01-01"):
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _age(value: str | None, now: datetime) -> str:
    """A coarse age (``2h``, ``15m``) for an AODP date, or ``—`` when unknown."""
    dt = _parse_dt(value)
    if dt is None:
        return "—"
    return _age_dt(dt, now)


def _age_dt(dt: datetime, now: datetime) -> str:
    """A coarse age string for an already-parsed datetime."""
    secs = (now - dt).total_seconds()
    if secs < 0:
        return "now"
    if secs < 90:
        return f"{int(secs)}s"
    if secs < 5400:
        return f"{int(secs // 60)}m"
    if secs < 172800:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"
