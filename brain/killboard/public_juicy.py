"""Public "juicy" feed — server-wide notable kills (killboard GDD §7, §9).

The guild feed (``feed.py``) only ever sees the tracked corp's own events, because
the poller queries ``/events?guildId=…``. The *public* juicy feed is the opposite:
it watches Albion's **whole-server** recent kill feed and posts the notable ones —
the highlights other corps' killbots show, where the killer and victim need have
nothing to do with the tracked guild.

It is deliberately a **sampled highlight reel, not an exact-once log**. The global
feed is a firehose (the 51 "most recent" span seconds), so this scans the top few
pages every ``interval_seconds`` and accepts that it sees a sample. Dedup is a
small in-memory ring of recently-posted ids — the global window rolls over in
under a minute, so a durable table would only grow without ever preventing a real
double-post.

Qualification is **fame first, loot second** (the operator's requested ordering,
and the lightest on the flaky AODP API): a kill clears the bar if its fame ≥
``feed.juicy_min_fame`` (free, straight off the event) OR its market loot value ≥
``feed.juicy_min_loot`` (priced only when the market layer is on). It is an OR, not
an AND, on purpose — the classic juicy gank is *low* fame / *high* loot, so an AND
would filter out exactly the kills the channel exists for.

Runs on ``ctx.supervisor`` like the other add-on loops: a crash is contained,
never fatal to voice. Every post is ``AllowedMentions.none()`` (constraint 11).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import discord
import structlog

from killboard.feed import build_embed
from killboard.model import parse_public_event
from killboard.value import estimate_value

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

    from cortana.config import AuraConfig
    from killboard.api import KbApi
    from killboard.cards import CardRenderer
    from killboard.market import MarketClient

log = structlog.get_logger(__name__)

#: The endpoint page size (the gameinfo ``/events`` cap).
_PAGE_LIMIT: int = 51
#: How many recently-posted event ids to remember for dedup. The global window
#: rolls over in well under a minute, so this only needs to cover a few scans.
_SEEN_CAP: int = 2000
#: Bound on how long the whole loot-pricing step for one kill may take, so a slow
#: AODP can never stall the scan loop (mirrors the guild feed's guard).
_LOOT_TIMEOUT_S: float = 6.0


class PublicJuicyFeed:
    """The §7 server-wide highlights loop for the juicy channel.

    Constructed with the module's :class:`~killboard.api.KbApi`, the shared
    :class:`~killboard.cards.CardRenderer`, the :class:`~killboard.market.MarketClient`
    (or ``None`` when the market layer isn't built), a zero-arg ``cfg_provider``
    returning the *current* :class:`~cortana.config.AuraConfig` (so region,
    thresholds, cadence, and the juicy channel are read live), ``to_thread`` for
    any blocking work, and the shutdown event. ``run`` is the supervised entry
    point and is safe to cancel/respawn.
    """

    def __init__(
        self,
        bot: Any,
        api: KbApi,
        cards: CardRenderer,
        market: MarketClient | None,
        cfg_provider: Callable[[], AuraConfig],
        to_thread: Callable[..., Awaitable[Any]],
        log: BoundLogger | None = None,
        *,
        shutdown: asyncio.Event | None = None,
    ) -> None:
        self._bot = bot
        self._api = api
        self._cards = cards
        self._market = market
        self._cfg_provider = cfg_provider
        self._to_thread = to_thread
        self._log: Any = log if log is not None else structlog.get_logger(__name__)
        self._shutdown = shutdown
        #: recently-posted ids, insertion-ordered so the oldest evicts first.
        self._seen: OrderedDict[int, None] = OrderedDict()

    # ── supervised entry point ───────────────────────────────────────────────

    async def run(self) -> None:
        """Scan the global feed forever until ``shutdown`` (§7, §13).

        Fully guarded: any transient failure — a flaky scan, a bad event, a send
        error — is caught and the loop recovers in place. ``CancelledError`` still
        propagates so the supervisor can cancel cleanly on shutdown.
        """
        self._log.info("kb_public_juicy.start")
        try:
            while not self._shutting_down():
                try:
                    await self._scan_once()
                except Exception as exc:  # noqa: BLE001 — inner net; must not escape
                    self._log.warning("kb_public_juicy.scan_error", error=str(exc), exc_info=True)
                await self._wait_next()
        finally:
            self._log.info("kb_public_juicy.stop")

    # ── one scan ─────────────────────────────────────────────────────────────

    async def _scan_once(self) -> None:
        """Fetch the top pages of the global feed and post the notable, unseen ones."""
        kb = self._cfg_provider().killboard
        pj = kb.public_juicy
        if not pj.enabled:
            return
        channel_id = kb.feed.juicy_channel
        if not channel_id:
            self._log.warning("kb_public_juicy.no_channel")
            return

        min_fame = kb.feed.juicy_min_fame
        min_loot = kb.feed.juicy_min_loot
        market_on = self._market is not None and getattr(kb.market, "enabled", False)

        posted = 0
        for page in range(max(1, pj.scan_pages)):
            if self._shutting_down():
                break
            events = await self._api.global_events(limit=_PAGE_LIMIT, offset=page * _PAGE_LIMIT)
            if not events:  # None (gave up) or [] (empty) — nothing to do this page
                break
            for raw in events:
                if self._shutting_down():
                    break
                if await self._maybe_post(raw, channel_id, min_fame, min_loot, market_on):
                    posted += 1
        if posted:
            self._log.info("kb_public_juicy.posted", count=posted)

    async def _maybe_post(
        self,
        raw: dict[str, Any],
        channel_id: int,
        min_fame: int,
        min_loot: int,
        market_on: bool,
    ) -> bool:
        """Qualify one global event (fame-first, loot-second) and post it if new."""
        row = parse_public_event(raw)
        if row is None or row.event_id in self._seen:
            return False

        # FAME first — free, straight off the event. LOOT second — only priced
        # when fame missed and the market layer is on (the OR gate; low-fame/high-
        # loot ganks still qualify).
        loot_value: int | None = None
        if row.total_fame >= min_fame:
            qualifies = True
            if market_on:
                loot_value = await self._loot_value(raw)  # nice-to-have on the card
        elif market_on and min_loot > 0:
            loot_value = await self._loot_value(raw)
            qualifies = loot_value is not None and loot_value >= min_loot
        else:
            qualifies = False

        if not qualifies:
            self._remember(row.event_id)  # don't re-price it next scan
            return False

        killer_guild = _guild_name(raw.get("Killer"))
        victim_guild = _guild_name(raw.get("Victim"))
        embed = build_embed(
            row, [], killer_guild=killer_guild, victim_guild=victim_guild, loot_value=loot_value
        )
        png = await self._render(row, raw, loot_value)
        sent = await self._send(channel_id, embed, png, row.event_id)
        self._remember(row.event_id)  # mark seen whether or not the send worked
        return sent

    # ── helpers ──────────────────────────────────────────────────────────────

    async def _loot_value(self, raw: dict[str, Any]) -> int | None:
        """Market loot value of the victim's drop, bounded and best-effort."""
        if self._market is None:
            return None
        try:
            result = await asyncio.wait_for(
                estimate_value(raw, self._market, side="victim"), timeout=_LOOT_TIMEOUT_S
            )
        except Exception as exc:  # noqa: BLE001 — value is optional (incl. timeout)
            self._log.warning("kb_public_juicy.value_failed", error=str(exc))
            return None
        return result.get("total")

    async def _render(self, row: Any, raw: dict[str, Any], loot_value: int | None) -> bytes | None:
        """Render the kill card, swallowing any failure (embed-only fallback)."""
        try:
            return await self._cards.render(row, [], loot_value=loot_value, raw_event=raw)
        except Exception as exc:  # noqa: BLE001 — a card must never break the feed
            self._log.warning("kb_public_juicy.card_error", event_id=row.event_id, error=str(exc))
            return None

    async def _send(
        self, channel_id: int, embed: discord.Embed, png: bytes | None, event_id: int
    ) -> bool:
        """Post one highlight to the juicy channel, non-pinging. Best-effort: any
        failure is logged and skipped (this is a sampled reel, never retried)."""
        channel = self._bot.get_channel(channel_id)
        if channel is None or not hasattr(channel, "send"):
            self._log.warning("kb_public_juicy.channel_missing", channel_id=channel_id)
            return False
        file = discord.File(io.BytesIO(png), filename=f"kill_{event_id}.png") if png else None
        if png is not None:
            embed.set_image(url=f"attachment://kill_{event_id}.png")
        try:
            await channel.send(
                embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.DiscordException as exc:
            self._log.warning(
                "kb_public_juicy.send_failed",
                channel_id=channel_id,
                event_id=event_id,
                error=str(exc),
            )
            return False
        return True

    def _remember(self, event_id: int) -> None:
        """Record an id as recently-seen, evicting the oldest past the cap."""
        self._seen[event_id] = None
        self._seen.move_to_end(event_id)
        while len(self._seen) > _SEEN_CAP:
            self._seen.popitem(last=False)

    def _shutting_down(self) -> bool:
        return self._shutdown is not None and self._shutdown.is_set()

    async def _wait_next(self) -> None:
        """Sleep until the next scan, waking early on shutdown."""
        interval = max(15, self._cfg_provider().killboard.public_juicy.interval_seconds)
        if self._shutdown is None:
            await asyncio.sleep(interval)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._shutdown.wait(), timeout=interval)


def _guild_name(player: Any) -> str | None:
    """The player's guild name tag for the embed header, or ``None``."""
    if not isinstance(player, dict):
        return None
    name = player.get("GuildName")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


__all__ = ["PublicJuicyFeed"]
