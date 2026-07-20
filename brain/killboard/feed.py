"""Kill & death feed: new events → Discord posts, exactly-once (killboard GDD §7).

The poller (GDD §5) durably stores every guild event and hands the genuinely-new
ones to this module. The feed turns each into a Discord post — a colour-coded
embed (``Killer ▸ Victim``, item power, fame, top damage, location/time, a link
to the official killboard) plus, when cards are enabled, a rendered kill-card
image — routes it per the operator's channel/threshold config (§7.2), sends it,
and records it in the ``posted`` table so a restart mid-batch never double-posts
(§7.3).

The design is built on one invariant: **the ``posted`` table is the source of
truth for what has been sent.** ``run`` never trusts the queue for content — the
queue is only a wake-up signal — it re-reads :meth:`~killboard.store.KbStore.unposted_events`
(oldest-first) and posts what is genuinely unposted, marking each as it goes. A
crash between "posted" and "marked" can at worst re-post a single event; a crash
anywhere else loses nothing and duplicates nothing.

Catch-up discipline (§7.3): when the bot returns from downtime and a large burst
is waiting, the feed posts the newest ``catchup_max_posts`` events individually
(rate-limited by ``post_delay_ms``), collapses the older overflow into the
``posted`` table silently, and drops a single "posted N older events" summary
rather than flooding the channel.

Every message this module sends passes ``allowed_mentions=discord.AllowedMentions.none()``:
the killboard is purely informational and must never ping anyone (CLAUDE.md
constraint 11). Blocking sqlite work rides ``to_thread`` so the voice event loop
is never stalled (GDD §14).
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import io
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import discord
import structlog

from killboard.cards import damage_shares
from killboard.model import ASSIST, DEATH, KILL
from killboard.value import estimate_value

if TYPE_CHECKING:
    from discord.ext import commands
    from structlog.stdlib import BoundLogger

    from cortana.config import AuraConfig, KbFeedConfig
    from killboard.cards import CardRenderer
    from killboard.model import EventRow, Participant
    from killboard.store import KbStore

log = structlog.get_logger(__name__)

#: The official web killboard's per-event URL (killboard GDD §7.1). The web path
#: is region-agnostic; a stale id simply 404s in the browser, which is harmless
#: for a best-effort link.
_EVENT_URL = "https://albiononline.com/killboard/kill/{event_id}"

#: Embed accents by guild relation (§7.1), matching the kill-card palette: green
#: when the guild landed the kill, red when it took the death, amber for an assist.
_COLOUR_KILL = discord.Colour(0x43A047)
_COLOUR_DEATH = discord.Colour(0xE53935)
_COLOUR_ASSIST = discord.Colour(0xFBC02D)

#: Fallback drain cadence: even with no queue signal the feed re-checks the store
#: this often, so a missed wake-up costs a little freshness, never a stuck feed.
_FALLBACK_POLL_S = 30.0

#: Max unposted events read from the store per drain fetch. Bounds one iteration's
#: work; catch-up collapse is sized against the TOTAL unposted count (not this
#: window), so a backlog larger than this still collapses correctly.
_DRAIN_WINDOW = 1000


class _PostResult(enum.Enum):
    """Outcome of trying to post one event — controls both the drain loop and
    the exactly-once guarantee (§7.3)."""

    #: Delivered to at least one channel and recorded posted.
    SENT = "sent"
    #: Intentionally not sent but recorded posted (routed nowhere, or every
    #: target is a permanently-missing channel) — removed from the backlog.
    SKIPPED = "skipped"
    #: A transient send failure (Discord 5xx/429) on a resolvable channel —
    #: NOT recorded posted, so the next drain retries it. Never dropped.
    DEFERRED = "deferred"


#: Damage contributors shown in the embed's "Top damage" field (§7.1).
_MAX_DAMAGE_ROWS = 5

#: Hard deadline for the best-effort loot-value lookup on the feed path. Loot
#: value is a decorative field; a slow (but not failing) AODP must never delay a
#: kill card, so the network fetch is bounded and simply degrades to "unknown"
#: (None) past this budget rather than serializing seconds onto every post.
_LOOT_VALUE_TIMEOUT_S = 2.0


class Feed:
    """Consumes new events and posts them to the feed channels exactly-once (§7).

    Constructed with the bot, the module's own :class:`~killboard.store.KbStore`,
    a :class:`~killboard.cards.CardRenderer`, a zero-arg ``cfg_provider`` returning
    the *current* root config (read live so a hot reload of channels/thresholds
    applies to the next post), and ``to_thread`` for the blocking sqlite calls.

    ``queue`` is the poller's new-event sink (:class:`asyncio.Queue`): its items
    are treated purely as wake-up signals — their contents are ignored, because
    :meth:`run` re-reads the store for exactly-once correctness. ``shutdown`` lets
    the loop exit promptly within the shared shutdown budget.
    """

    def __init__(
        self,
        bot: commands.Bot,
        store: KbStore,
        cards: CardRenderer,
        cfg_provider: Callable[[], AuraConfig],
        to_thread: Callable[..., Awaitable[Any]],
        log: BoundLogger | None = None,
        *,
        queue: asyncio.Queue[list[EventRow]] | None = None,
        shutdown: asyncio.Event | None = None,
        market: Any = None,
    ) -> None:
        self._bot = bot
        self._store = store
        self._cards = cards
        self._cfg_provider = cfg_provider
        self._to_thread = to_thread
        #: Optional MarketClient — when the market layer is on, kill cards carry
        #: an estimated silver loot value. None = no value shown.
        self._market = market
        self._log: Any = log if log is not None else structlog.get_logger(__name__)
        self._queue = queue
        self._shutdown = shutdown
        # Lightweight observability for the module's health report.
        self._posted_total = 0
        self._collapsed_total = 0
        self._last_post_at: str | None = None

    # ── supervised entry point ───────────────────────────────────────────────

    async def run(self) -> None:
        """Drain the store, then wait for a wake-up and drain again, until stop.

        The first iteration drains whatever the ``posted`` table shows as unsent —
        this is how a restart resumes without gaps or duplicates (§7.3). The loop
        is fully guarded: a transient failure (a send error, a store hiccup) is
        caught and logged, never propagated, so a single bad event can't kill the
        feed. ``CancelledError`` still propagates so the supervisor can stop it.
        """
        self._log.info("kb_feed.start")
        try:
            while not self._shutting_down():
                try:
                    await self._drain()
                except Exception as exc:  # noqa: BLE001 — inner net; must not escape
                    self._log.warning("kb_feed.drain_error", error=str(exc), exc_info=True)
                await self._wait_next()
        finally:
            self._log.info("kb_feed.stop")

    def snapshot(self) -> dict[str, object]:
        """Feed counters for the module's ``/botstatus`` health block (§10)."""
        return {
            "posted_total": self._posted_total,
            "collapsed_total": self._collapsed_total,
            "last_post_at": self._last_post_at,
        }

    # ── draining the unposted backlog ────────────────────────────────────────

    async def _drain(self) -> None:
        """Post every unposted event, oldest-first, with catch-up discipline (§7.3).

        Catch-up is sized against the WHOLE backlog, not one fetch window: if the
        total unposted count exceeds ``catchup_max_posts`` (returning from
        downtime, or first-run backfill), the oldest ``total - cap`` are collapsed
        into ``posted`` across as many windows as needed and summarised in ONE
        line, and only the newest ``cap`` are posted individually. A transient
        Discord outage defers (does not drop) the rest and ends the pass — the
        next drain retries. Terminates when the store reports nothing unposted or
        no forward progress is possible.
        """
        if self._shutting_down():
            return
        fc = self._cfg_provider().killboard.feed
        cap = max(1, fc.catchup_max_posts)
        delay = max(0.0, fc.post_delay_ms / 1000.0)

        total = await self._to_thread(self._store.count_unposted)
        if total == 0:
            return

        # Catch-up collapse over the whole backlog (§7.3), oldest-first.
        if total > cap:
            to_collapse = total - cap
            collapsed = 0
            while collapsed < to_collapse and not self._shutting_down():
                batch = await self._to_thread(
                    self._store.unposted_events, min(_DRAIN_WINDOW, to_collapse - collapsed)
                )
                if not batch:
                    break
                await self._collapse(batch)
                collapsed += len(batch)
            if collapsed:
                await self._post_summary(fc, collapsed)

        # Post the remaining backlog (now <= cap after any collapse) oldest-first.
        while not self._shutting_down():
            pending = await self._to_thread(self._store.unposted_events, cap)
            if not pending:
                return
            progressed = False
            for i, row in enumerate(pending):
                if self._shutting_down():
                    return
                if i > 0:
                    await self._sleep(delay)
                result = await self._post_one(row)
                if result is _PostResult.DEFERRED:
                    # Discord is refusing (5xx/429) — stop this pass rather than
                    # busy-looping the same events; the next drain retries them.
                    return
                progressed = True
                if result is _PostResult.SENT:
                    self._posted_total += 1
                    self._last_post_at = _utc_now()
            if not progressed:
                return

    async def _collapse(self, rows: list[EventRow]) -> None:
        """Silently mark a catch-up overflow as posted, without sending it (§7.3).

        These older events are represented by the single summary line rather than
        an individual card each; recording them in ``posted`` (channel/message 0)
        keeps the feed exactly-once and stops them re-appearing on the next drain.
        """
        for row in rows:
            await self._to_thread(self._store.mark_posted, row.event_id, 0, 0)
        self._collapsed_total += len(rows)

    # ── posting one event ────────────────────────────────────────────────────

    async def _post_one(self, row: EventRow) -> _PostResult:
        """Route, render, send, and record one event (§7.2/§7.3).

        Exactly-once is preserved by marking ``posted`` ONLY when the event has
        genuinely left the backlog for good:

        * routed nowhere (``min_fame`` / ``ignore_deaths_below_ip`` / no channel)
          → SKIPPED (marked posted; nothing to deliver);
        * delivered to at least one channel → SENT (marked posted with the id);
        * every target is a permanently-missing channel → SKIPPED (marked posted
          so a deleted channel can't wedge the backlog forever);
        * a resolvable channel refused transiently (5xx/429) and none succeeded →
          DEFERRED (NOT marked posted; the next drain retries — never dropped,
          the bug this guards against silently lost kills on a passing Discord
          hiccup).
        """
        fc = self._cfg_provider().killboard.feed
        targets = route_channels(row, fc)
        if not targets:
            await self._to_thread(self._store.mark_posted, row.event_id, 0, 0)
            return _PostResult.SKIPPED

        # The raw event carries the guild tags (for the header) and the item
        # loadout (for the market loot value) — data the flat row doesn't hold.
        # Compute it BEFORE rendering so the loot value can be drawn on the card.
        raw = await self._to_thread(self._store.raw_event, row.event_id)
        killer_guild, victim_guild = _guild_tags(raw)
        loot_value = await self._loot_value(raw)
        png = await self._render_card(row, raw, loot_value)
        filename = f"kill_{row.event_id}.png"
        embed = build_embed(
            row,
            [],
            killer_guild=killer_guild,
            victim_guild=victim_guild,
            loot_value=loot_value,
        )
        if png is not None:
            embed.set_image(url=f"attachment://{filename}")

        posted_channel = 0
        posted_message = 0
        transient_failure = False
        for cid in targets:
            channel = self._bot.get_channel(cid)
            if channel is None or not hasattr(channel, "send"):
                # Structural: the channel is absent/misconfigured. Skip it — a
                # retry can't resolve it, so it must not defer the event forever.
                self._log.warning("kb_feed.channel_missing", channel_id=cid, event_id=row.event_id)
                continue
            file = discord.File(io.BytesIO(png), filename=filename) if png is not None else None
            try:
                message = await channel.send(
                    embed=embed,
                    file=file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.DiscordException as exc:
                # Transient: a resolvable channel refused (rate limit / 5xx).
                self._log.warning(
                    "kb_feed.send_failed", channel_id=cid, event_id=row.event_id, error=str(exc)
                )
                transient_failure = True
                continue
            if posted_message == 0:
                posted_channel = cid
                posted_message = message.id if message is not None else 0

        if posted_message != 0:
            await self._to_thread(
                self._store.mark_posted, row.event_id, posted_message, posted_channel
            )
            return _PostResult.SENT
        if transient_failure:
            # Every resolvable target failed transiently — do NOT mark posted.
            return _PostResult.DEFERRED
        # No transient failure and nothing sent ⇒ every target was structurally
        # unresolvable. Mark posted so a permanently-bad channel can't wedge.
        await self._to_thread(self._store.mark_posted, row.event_id, 0, 0)
        return _PostResult.SKIPPED

    async def _render_card(
        self, row: EventRow, raw: dict[str, Any] | None, loot_value: int | None = None
    ) -> bytes | None:
        """Render the kill-card PNG for an event, or ``None`` to post embed-only.

        ``raw`` is the parsed full event (from :meth:`KbStore.raw_event`); it
        carries the equipment grid the flat ``row`` does not, so passing it is
        what makes the gear icons render. The renderer already swallows its own
        failures (disabled cards, Pillow missing, a dead icon fetch) and returns
        ``None`` (§7.1); this wrapper adds a last-resort guard so nothing in the
        card path can ever take down a post. ``loot_value`` (when the market
        layer priced the loadout) is drawn on the card; ``None`` omits it.
        """
        try:
            return await self._cards.render(row, [], loot_value=loot_value, raw_event=raw)
        except Exception as exc:  # noqa: BLE001 — a card must never break the feed
            self._log.warning("kb_feed.card_error", event_id=row.event_id, error=str(exc))
            return None

    async def _loot_value(self, raw: dict[str, Any] | None) -> int | None:
        """Estimated silver value of the victim's dropped loadout (§7.1), or
        ``None`` when the market layer is off or nothing could be priced. Never
        raises — a market hiccup must not break a post."""
        if self._market is None or raw is None:
            return None
        market_cfg = getattr(self._cfg_provider().killboard, "market", None)
        if not getattr(market_cfg, "enabled", False):
            return None
        try:
            result = await asyncio.wait_for(
                estimate_value(raw, self._market, side="victim"),
                timeout=_LOOT_VALUE_TIMEOUT_S,
            )
        except TimeoutError:
            # A slow-but-healthy AODP: drop the decorative value rather than let
            # it stall the timeliness-critical feed drain (§7.3).
            self._log.warning("kb_feed.value_timeout")
            return None
        except Exception as exc:  # noqa: BLE001 — value is best-effort
            self._log.warning("kb_feed.value_failed", error=str(exc))
            return None
        return result.get("total")

    async def _post_summary(self, fc: KbFeedConfig, count: int) -> None:
        """Post the single catch-up summary line to the main feed channel (§7.3)."""
        if count <= 0:
            return
        cid = fc.kills_channel or fc.deaths_channel or fc.juicy_channel or fc.blob_channel
        if not cid:
            return
        channel = self._bot.get_channel(cid)
        if channel is None or not hasattr(channel, "send"):
            return
        plural = "s" if count != 1 else ""
        try:
            await channel.send(
                content=f"Posted {count} older event{plural} from catch-up.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException as exc:
            self._log.warning("kb_feed.summary_failed", error=str(exc))

    # ── loop plumbing ────────────────────────────────────────────────────────

    def _shutting_down(self) -> bool:
        """Whether the shutdown event (if any) has been set."""
        return self._shutdown is not None and self._shutdown.is_set()

    async def _sleep(self, seconds: float) -> None:
        """Sleep ``seconds`` but return immediately once shutdown is requested."""
        if seconds <= 0:
            return
        if self._shutdown is None:
            await asyncio.sleep(seconds)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._shutdown.wait(), timeout=seconds)

    async def _wait_next(self) -> None:
        """Block until a new-event signal, shutdown, or the fallback timeout.

        Races the poller's queue against the shutdown event with a
        :data:`_FALLBACK_POLL_S` ceiling, then drains any extra queued signals so
        a burst of puts collapses into a single follow-up drain rather than one
        drain per put. Queue *contents* are discarded — the store is the source
        of truth — so losing a signal to a race never loses an event.
        """
        if self._shutting_down():
            return

        waiters: list[asyncio.Future[Any]] = []
        if self._queue is not None:
            waiters.append(asyncio.ensure_future(self._queue.get()))
        if self._shutdown is not None:
            waiters.append(asyncio.ensure_future(self._shutdown.wait()))

        if not waiters:
            await asyncio.sleep(_FALLBACK_POLL_S)
            return

        try:
            await asyncio.wait(
                waiters, timeout=_FALLBACK_POLL_S, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()

        if self._queue is not None:
            with contextlib.suppress(asyncio.QueueEmpty):
                while True:
                    self._queue.get_nowait()


# ── pure routing + embed helpers (no I/O — unit-testable) ─────────────────────


def route_channels(row: EventRow, fc: KbFeedConfig) -> list[int]:
    """Target channel ids for an event, in send order, deduped (killboard GDD §7.2).

    Applies the operator's routing and suppression rules:

    * KILL/ASSIST post to ``kills_channel``; DEATH posts to ``deaths_channel``.
    * ``min_fame`` suppresses trivial kills from the main feed; ``ignore_deaths_below_ip``
      suppresses low-IP "naked" deaths.
    * ``blob_participant_threshold`` + ``blob_channel`` redirect large-participant
      fights to the ZvZ channel instead of the main feed.
    * ``juicy_channel`` + ``juicy_min_fame`` *additionally* mirror high-value kills
      to a highlights feed, independent of the main-feed suppression.

    A channel of ``0`` is "unset" and never routed to. An empty list means the
    event is suppressed entirely; the caller still records it as handled so it
    does not linger in the unposted backlog.
    """
    relation = (row.relation or "").upper()
    fame = row.total_fame or 0
    participants = row.num_participants or 0
    is_blob = participants > fc.blob_participant_threshold

    out: list[int] = []

    def add(channel_id: int) -> None:
        if channel_id and channel_id not in out:
            out.append(channel_id)

    if relation in (KILL, ASSIST):
        if fame >= fc.min_fame:
            if fc.blob_channel and is_blob:
                add(fc.blob_channel)
            else:
                add(fc.kills_channel)
        if fame >= fc.juicy_min_fame:
            add(fc.juicy_channel)
    elif relation == DEATH:
        victim_ip = row.victim_ip or 0
        if fc.ignore_deaths_below_ip <= 0 or victim_ip >= fc.ignore_deaths_below_ip:
            if fc.blob_channel and is_blob:
                add(fc.blob_channel)
            else:
                add(fc.deaths_channel)

    return out


def build_embed(
    row: EventRow,
    participants: list[Participant],
    *,
    killer_guild: str | None = None,
    victim_guild: str | None = None,
    loot_value: int | None = None,
) -> discord.Embed:
    """Build the feed embed for one event (killboard GDD §7.1).

    Header is ``[Guild] Killer killed [Guild] Victim`` (matching the community
    killbots) linking to the official killboard, colour-coded by relation (green
    kill / red death / amber assist). Fields carry item power per side, kill
    fame, party size, top damage contributors, location, and — when the market
    layer priced it — the estimated silver loot value. Every field is read
    tolerantly (§2.4) so a partial event still yields a valid embed.

    Pure and side-effect free — the caller sends it with ``AllowedMentions.none()``
    (CLAUDE.md constraint 11).
    """
    relation = (row.relation or "").upper()
    killer = row.killer_name or "Unknown"
    victim = row.victim_name or "Unknown"
    killer_label = f"[{killer_guild}] {killer}" if killer_guild else killer
    victim_label = f"[{victim_guild}] {victim}" if victim_guild else victim
    # "[DEAD Renegadez] Snapjlr killed [MOIX] chavana" — killer always landed the
    # blow; the green/red accent carries whose perspective it is.
    title = _clip(f"{killer_label} killed {victim_label}", 256)

    embed = discord.Embed(
        title=title,
        colour=_relation_colour(relation),
        url=_EVENT_URL.format(event_id=row.event_id),
    )
    embed.add_field(
        name="Item Power",
        value=f"{_fmt_ip(row.killer_ip)} vs {_fmt_ip(row.victim_ip)}",
        inline=True,
    )
    embed.add_field(name="Fame", value=f"{row.total_fame or 0:,}", inline=True)
    if loot_value is not None:
        embed.add_field(name="💰 Loot value", value=f"~{loot_value:,} silver", inline=True)
    if row.num_participants:
        embed.add_field(name="Party", value=str(row.num_participants), inline=True)

    shares = damage_shares(participants, _MAX_DAMAGE_ROWS)
    if shares:
        lines = [f"{_clip(s.name, 24)} — {round(s.fraction * 100)}%" for s in shares]
        embed.add_field(name="Top damage", value="\n".join(lines), inline=False)

    if row.location:
        embed.add_field(name="Location", value=str(row.location), inline=True)

    timestamp = _parse_ts(row.timestamp)
    if timestamp is not None:
        embed.timestamp = timestamp

    embed.set_footer(text=_relation_label(relation))
    return embed


def _guild_tags(raw: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """Extract ``(killer_guild, victim_guild)`` display names from the raw event
    for the header, tolerant of missing fields (§2.4)."""
    if not isinstance(raw, dict):
        return None, None

    def _name(side: str) -> str | None:
        obj = raw.get(side)
        if not isinstance(obj, dict):
            return None
        name = obj.get("GuildName")
        return name.strip() if isinstance(name, str) and name.strip() else None

    return _name("Killer"), _name("Victim")


def _relation_colour(relation: str) -> discord.Colour:
    """Embed accent for a relation; unknown/assist reads amber (§7.1)."""
    if relation == KILL:
        return _COLOUR_KILL
    if relation == DEATH:
        return _COLOUR_DEATH
    return _COLOUR_ASSIST


def _relation_label(relation: str) -> str:
    """Human footer label for a relation."""
    if relation == KILL:
        return "Kill"
    if relation == DEATH:
        return "Death"
    if relation == ASSIST:
        return "Assist"
    return "Event"


def _fmt_ip(value: float | int | None) -> str:
    """Item power as a rounded integer string, or ``"?"`` when unknown (§2.4)."""
    if isinstance(value, int | float):
        return str(round(value))
    return "?"


def _clip(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` characters with an ellipsis when it overruns."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp tolerantly, returning ``None`` on any failure.

    Accepts a trailing ``Z`` (UTC) and trims over-long fractional seconds the API
    sometimes emits, so an odd timestamp costs the embed's time field, not a crash.
    """
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        if "." in text:
            head, _, tail = text.partition(".")
            frac = tail[:6]
            tz = ""
            for marker in ("+", "-"):
                idx = tail.find(marker)
                if idx != -1:
                    frac = tail[:idx][:6]
                    tz = tail[idx:]
                    break
            with contextlib.suppress(ValueError):
                return datetime.fromisoformat(f"{head}.{frac}{tz}")
        return None


def _utc_now() -> str:
    """Current instant as an ISO-8601 UTC string (matches the store's timestamps)."""
    return datetime.now(UTC).isoformat()


__all__ = [
    "Feed",
    "build_embed",
    "route_channels",
]
