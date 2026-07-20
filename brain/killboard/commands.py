"""The killboard's slash surface (killboard GDD §10).

Every command here reads **only the local store** and answers instantly,
independent of the gameinfo API's health — the poller writes the event store,
the cog reads it (killboard GDD §3.1). A slow or dead API costs the feed its
freshness but never blocks ``/killboard ranking`` or ``/killboard record``.

The one exception is ``/killboard battles``: battles are not persisted, so that
command asks the :class:`~killboard.battles.Battles` handle, which hits the
least-reliable endpoint (killboard GDD §9) but tolerates every failure quietly
(returns ``[]``, never raises).

Everything lives under one :class:`discord.app_commands.Group` named
``killboard``. The public subcommands (``ranking``, ``record``, ``recent``,
``topkills``, ``guild``, ``battles``) are open to anyone in the server; the
operator subcommands (``status``, ``config``, ``schedule``) are gated on the
house admin check (:func:`cortana.dsc.cogs.admin._is_admin` — Manage Guild or
the FC role), reused rather than reinvented.

The cog also implements :class:`~killboard.views.RankingPageSource`: the
restart-proof pager buttons find it duck-typed on the bot and ask it to
re-render a leaderboard page, so all the store access and window math stay here.

**Every** message this cog sends passes
``allowed_mentions=discord.AllowedMentions.none()`` — the killboard is purely
informational and must never ping anyone (CLAUDE.md constraint 11). All blocking
sqlite work rides ``to_thread`` off the voice event loop.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import discord
import structlog
from discord import app_commands
from discord.ext import commands

from cortana.core import db
from cortana.dsc.cogs.admin import _is_admin
from killboard import rankings
from killboard.model import DEATH, KILL
from killboard.views import RankingRender, RankingView

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cortana.config import KillboardConfig
    from killboard.battles import Battles
    from killboard.store import KbStore

log = structlog.get_logger(__name__)

__all__ = ["KillboardCog"]

# ── display tuning ────────────────────────────────────────────────────────────

#: Rows per leaderboard page (drives the Prev/Next pager in :mod:`killboard.views`).
_PAGE_SIZE = 10

#: ``/recent`` default and hard cap (keeps the embed within Discord limits).
_RECENT_DEFAULT = 10
_RECENT_MAX = 20

#: ``/topkills`` default and hard cap.
_TOPKILLS_DEFAULT = 5
_TOPKILLS_MAX = 10

#: ``/battles`` default and hard cap — the endpoint is flaky, keep it modest.
_BATTLES_DEFAULT = 3
_BATTLES_MAX = 5

#: Embed accents.
_KILL_COLOUR = 0x2ECC71  # green — the guild landed the kill
_DEATH_COLOUR = 0xE74C3C  # red — the guild took the death
_NEUTRAL_COLOUR = 0x3498DB  # blue — neutral summaries
_STATUS_OK = 0x2ECC71
_STATUS_WARN = 0xF1C40F

_KILLBOARD_URL = "https://albiononline.com/killboard/event/{event_id}"


Period = Literal["today", "week", "month"]
Metric = Literal["fame", "kills", "kd"]


class KillboardCog(commands.Cog):
    """The ``/killboard *`` command surface, reading only the local store (§10).

    Constructed by the killboard module's ``setup`` with the handles it already
    built:

    * ``bot`` — the shared discord.py client (service locator; ``interaction.
      client`` is this same object, which is what makes the reused admin check
      resolve the FC role).
    * ``store`` — the killboard's :class:`~killboard.store.KbStore` (its own DB).
    * ``battles`` — the :class:`~killboard.battles.Battles` handle for the one
      command that cannot read from the store.
    * ``cfg_provider`` — a zero-arg callable returning the *current*
      :class:`~cortana.config.KillboardConfig`, read live so a hot reload of the
      tracked guild, region, or thresholds applies to the next command.
    * ``to_thread`` — ``asyncio.to_thread`` (overridable for tests); every
      blocking sqlite call rides it so the voice loop never stalls.
    """

    killboard = app_commands.Group(
        name="killboard",
        description="Albion Online killboard: rankings, records, recent kills, battles.",
    )

    def __init__(
        self,
        bot: commands.Bot,
        store: KbStore,
        battles: Battles,
        cfg_provider: Callable[[], KillboardConfig],
        to_thread: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self.bot = bot
        self._store = store
        self._battles = battles
        self._cfg_provider = cfg_provider
        self._to_thread: Callable[..., Awaitable[Any]] = to_thread or asyncio.to_thread

    # ── error boundary ───────────────────────────────────────────────────────

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Turn the admin-gate rejection into a clear ephemeral note (matches the
        house pattern in ``status.py`` / ``admin.py``). Anything else falls
        through to the tree-level boundary."""
        if isinstance(error, app_commands.CheckFailure):
            message = "Admin only — needs Manage Guild or the FC role."
            if interaction.response.is_done():
                await interaction.followup.send(
                    message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
                )
            else:
                await interaction.response.send_message(
                    message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
                )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _cfg(self) -> KillboardConfig:
        return self._cfg_provider()

    # ── /killboard ranking ───────────────────────────────────────────────────

    @killboard.command(
        name="ranking", description="Leaderboard for a window and metric (from the event store)."
    )
    @app_commands.describe(
        period="today, this week, or this month",
        metric="rank by Kill Fame, kill count, or true K/D",
    )
    async def ranking(
        self,
        interaction: discord.Interaction,
        period: Period = "week",
        metric: Metric = "fame",
    ) -> None:
        """Windowed leaderboard with a restart-proof Prev/Next pager (§8, §10)."""
        await interaction.response.defer(thinking=True)
        render = await self._build_ranking_render(metric, period, 0)
        view = RankingView(metric, period, 0, has_next=render.has_next)
        await interaction.followup.send(
            embed=render.embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def render_ranking_page(self, metric: str, period: str, page: int) -> RankingRender:
        """The :class:`~killboard.views.RankingPageSource` contract: re-render a
        page for the pager buttons. All store access and window math live here so
        the button stays pure (killboard GDD §8, views module docstring)."""
        return await self._build_ranking_render(metric, period, page)

    async def _build_ranking_render(self, metric: str, period: str, page: int) -> RankingRender:
        """Compute one leaderboard page → embed + whether a further page exists.

        The store's ``leaderboard`` returns top-N (no offset), so a page is a tail
        slice of ``(page + 1) * PAGE_SIZE + 1`` rows; the extra row is how we know
        a Next page has content. Blocking sqlite rides ``to_thread``.
        """
        page = max(page, 0)
        cfg = self._cfg()
        tz = cfg.rankings.timezone
        start, end = rankings.window_for(period, tz)
        want = (page + 1) * _PAGE_SIZE + 1
        rows = await self._to_thread(
            rankings.build_leaderboard, self._store, metric, start, end, want
        )
        page_rows = rows[page * _PAGE_SIZE : (page + 1) * _PAGE_SIZE]
        has_next = len(rows) > (page + 1) * _PAGE_SIZE
        embed = rankings.leaderboard_embed(
            metric,
            rankings.PERIOD_LABELS.get(period, period),
            page_rows,
            guild_name=cfg.guild_name or None,
            tz=tz,
        )
        return RankingRender(embed=embed, has_next=has_next)

    # ── /killboard record ────────────────────────────────────────────────────

    @killboard.command(
        name="record", description="A member's kills, deaths, K/D, fame, and assists this month."
    )
    @app_commands.describe(player="exact in-game name of the guild member")
    async def record(self, interaction: discord.Interaction, player: str) -> None:
        """A member's current-month record from the event store, plus lifetime
        fame from the roster snapshot when known (§8.2, §10)."""
        await interaction.response.defer(thinking=True)
        cfg = self._cfg()
        tz = cfg.rankings.timezone
        start, end = rankings.window_for("month", tz)

        resolved = await self._to_thread(_resolve_player, self._store, player)
        if resolved is None:
            await interaction.followup.send(
                f"No stored activity for **{_clip(player, 64)}** — "
                "name must match exactly, and the member must have appeared in an "
                "ingested kill or death.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        player_id, display_name, lifetime_fame = resolved
        record = await self._to_thread(
            rankings.build_member_record, self._store, player_id, display_name, start, end
        )
        embed = rankings.record_embed(
            record,
            period_label=rankings.PERIOD_LABELS["month"],
            lifetime_fame=lifetime_fame,
            guild_name=cfg.guild_name or None,
        )
        await interaction.followup.send(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    # ── /killboard recent ────────────────────────────────────────────────────

    @killboard.command(
        name="recent", description="The guild's most recent kills and deaths, compact."
    )
    @app_commands.describe(count=f"how many events (1–{_RECENT_MAX})")
    async def recent(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, _RECENT_MAX] = _RECENT_DEFAULT,
    ) -> None:
        """Compact list of the newest ingested events (§10)."""
        await interaction.response.defer(thinking=True)
        rows = await self._to_thread(self._store.recent, int(count))
        cfg = self._cfg()
        embed = discord.Embed(
            title="Recent kills & deaths",
            colour=discord.Colour(_NEUTRAL_COLOUR),
            timestamp=datetime.now(UTC),
        )
        if not rows:
            embed.description = "_No events ingested yet._"
        else:
            embed.description = "\n".join(_event_line(r) for r in rows)
        if cfg.guild_name:
            embed.set_footer(text=cfg.guild_name)
        await interaction.followup.send(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    # ── /killboard topkills ──────────────────────────────────────────────────

    @killboard.command(
        name="topkills", description="Highest-fame kills the guild landed in a window."
    )
    @app_commands.describe(
        period="today, this week, or this month",
        count=f"how many kills (1–{_TOPKILLS_MAX})",
    )
    async def topkills(
        self,
        interaction: discord.Interaction,
        period: Period = "week",
        count: app_commands.Range[int, 1, _TOPKILLS_MAX] = _TOPKILLS_DEFAULT,
    ) -> None:
        """The window's biggest kills by fame, read from the event store (§10)."""
        await interaction.response.defer(thinking=True)
        cfg = self._cfg()
        tz = cfg.rankings.timezone
        start, end = rankings.window_for(period, tz)
        rows = await self._to_thread(_top_kill_events, self._store, start, end, int(count))
        embed = discord.Embed(
            title=f"Top kills · {rankings.PERIOD_LABELS.get(period, period)}",
            colour=discord.Colour(_KILL_COLOUR),
            timestamp=datetime.now(UTC),
        )
        if not rows:
            embed.description = "_No kills in this window._"
        else:
            embed.description = "\n".join(_top_kill_line(rank, r) for rank, r in enumerate(rows, 1))
        if cfg.guild_name:
            embed.set_footer(text=cfg.guild_name)
        await interaction.followup.send(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    # ── /killboard guild ─────────────────────────────────────────────────────

    @killboard.command(
        name="guild", description="Guild summary: roster, lifetime fame, this month's totals."
    )
    async def guild(self, interaction: discord.Interaction) -> None:
        """Roster/lifetime figures from the snapshot table plus this month's
        by-count totals from the event store (§8.2, §10)."""
        await interaction.response.defer(thinking=True)
        cfg = self._cfg()
        tz = cfg.rankings.timezone
        start, end = rankings.window_for("month", tz)

        summary = await self._to_thread(_guild_summary, self._store, start, end)
        embed = discord.Embed(
            title=cfg.guild_name or "Tracked guild",
            colour=discord.Colour(_NEUTRAL_COLOUR),
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Region", value=cfg.region, inline=True)
        embed.add_field(
            name="Roster",
            value=(str(summary["members"]) if summary["members"] else "not synced yet"),
            inline=True,
        )
        embed.add_field(
            name="Lifetime Kill Fame",
            value=(f"{summary['lifetime_fame']:,}" if summary["members"] else "—"),
            inline=True,
        )
        embed.add_field(name="Kills (month)", value=f"{summary['kills']:,}", inline=True)
        embed.add_field(name="Deaths (month)", value=f"{summary['deaths']:,}", inline=True)
        embed.add_field(name="K/D (month)", value=f"{summary['kd']:.2f}", inline=True)
        embed.add_field(name="Kill Fame (month)", value=f"{summary['fame']:,}", inline=True)
        embed.set_footer(text="Lifetime/roster from the API snapshot (≈daily); month from events.")
        await interaction.followup.send(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    # ── /killboard battles ───────────────────────────────────────────────────

    @killboard.command(
        name="battles", description="Recent large-scale battles the guild took part in."
    )
    @app_commands.describe(count=f"how many battles (1–{_BATTLES_MAX})")
    async def battles(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, _BATTLES_MAX] = _BATTLES_DEFAULT,
    ) -> None:
        """The one command that leaves the store: battles are not persisted, so
        this asks the API via the battles handle, which tolerates every failure
        quietly (returns ``[]``) — killboard GDD §9."""
        await interaction.response.defer(thinking=True)
        found = await self._battles.recent(int(count))
        if not found:
            await interaction.followup.send(
                "No recent battles cleared the size threshold — or the battles "
                "endpoint is unavailable right now (it's the flakiest one).",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        embeds = [self._battles.build_summary_embed(b) for b in found[:_BATTLES_MAX]]
        await interaction.followup.send(
            embeds=embeds, allowed_mentions=discord.AllowedMentions.none()
        )

    # ── /killboard status (admin) ────────────────────────────────────────────

    @killboard.command(name="status", description="(admin) Poller health and ingestion state.")
    @app_commands.check(_is_admin)
    async def status(self, interaction: discord.Interaction) -> None:
        """Last successful poll, last new event, events stored, and the fail
        streak — read straight from ``poll_state`` (killboard GDD §10, §13)."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg = self._cfg()
        state, stored = await self._to_thread(_status_snapshot, self._store)
        now = datetime.now(UTC)

        fails = int(state["consecutive_fails"])
        last_success = state["last_success_at"]
        warn = _older_than_minutes(last_success, cfg.staleness.warn_after_minutes, now)
        quiet = _older_than_minutes(
            state["last_advanced_at"], cfg.staleness.no_events_notice_hours * 60, now
        )

        embed = discord.Embed(
            title="Killboard poller status",
            colour=discord.Colour(_STATUS_WARN if (warn or fails) else _STATUS_OK),
            timestamp=now,
        )
        embed.add_field(name="Last successful poll", value=_age(last_success, now), inline=True)
        embed.add_field(
            name="Last new event", value=_age(state["last_advanced_at"], now), inline=True
        )
        embed.add_field(name="Consecutive failures", value=str(fails), inline=True)
        embed.add_field(name="Events stored", value=f"{stored:,}", inline=True)
        embed.add_field(name="High-water EventId", value=str(state["last_event_id"]), inline=True)
        embed.add_field(
            name="Region / guild",
            value=f"{cfg.region} · {cfg.guild_name or cfg.guild_id or 'unset'}",
            inline=True,
        )
        notes: list[str] = []
        if warn:
            notes.append(f"⚠️ No successful poll in > {cfg.staleness.warn_after_minutes}m — stale.")
        if quiet and not warn:
            notes.append(
                f"ℹ️ No new events in > {cfg.staleness.no_events_notice_hours}h — guild is quiet."
            )
        if fails:
            notes.append(
                f"⚠️ {fails} poll(s) failing in a row — backing off, feed serves stored data."
            )
        embed.description = "\n".join(notes) if notes else "All nominal."
        await interaction.followup.send(
            embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    # ── /killboard config (admin) ────────────────────────────────────────────

    @killboard.command(name="config", description="(admin) View the live killboard configuration.")
    @app_commands.check(_is_admin)
    async def config(self, interaction: discord.Interaction) -> None:
        """View channels, thresholds, region, and tracked guild. Values are set in
        ``cortana.yaml`` and hot-applied with ``/reload`` (config is file-owned,
        CLAUDE.md constraint 12) — this is the read side of §10."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg = self._cfg()
        feed = cfg.feed
        embed = discord.Embed(
            title="Killboard configuration",
            colour=discord.Colour(_NEUTRAL_COLOUR),
            timestamp=datetime.now(UTC),
        )
        embed.add_field(
            name="Target",
            value=(
                f"region: **{cfg.region}**\n"
                f"guild: **{cfg.guild_name or '—'}**\n"
                f"guild_id: `{cfg.guild_id or '—'}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Feed channels",
            value=(
                f"kills: {_chan(feed.kills_channel)}\n"
                f"deaths: {_chan(feed.deaths_channel)}\n"
                f"juicy: {_chan(feed.juicy_channel)}\n"
                f"blob: {_chan(feed.blob_channel)}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Feed thresholds",
            value=(
                f"min_fame: {feed.min_fame:,}\n"
                f"juicy_min_fame: {feed.juicy_min_fame:,}\n"
                f"ignore_deaths_below_ip: {feed.ignore_deaths_below_ip:,}\n"
                f"blob_participants: {feed.blob_participant_threshold}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Battles",
            value=(
                f"channel: {_chan(cfg.battles.channel)}\n"
                f"min_players: {cfg.battles.min_players}\n"
                f"min_fame: {cfg.battles.min_fame:,}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Rankings / storage",
            value=(
                f"timezone: {cfg.rankings.timezone}\n"
                f"poll: every {cfg.poller.interval_seconds}s\n"
                f"db: `{cfg.storage.db_path}`"
            ),
            inline=True,
        )
        embed.set_footer(text="Edit cortana.yaml then /reload to apply changes.")
        await interaction.followup.send(
            embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    # ── /killboard schedule (admin) ──────────────────────────────────────────

    @killboard.command(
        name="schedule", description="(admin) The scheduled ranking posts and when they last ran."
    )
    @app_commands.check(_is_admin)
    async def schedule(self, interaction: discord.Interaction) -> None:
        """List the ``schedules`` rows driving automated leaderboard posts (§8.3,
        §10). Schedules live in the killboard's own DB; edit them there and the
        scheduler picks them up on its next tick."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await self._to_thread(_list_schedules, self._store)
        embed = discord.Embed(
            title="Scheduled ranking posts",
            colour=discord.Colour(_NEUTRAL_COLOUR),
            timestamp=datetime.now(UTC),
        )
        if not rows:
            embed.description = (
                "_No schedules configured._ Seed the `schedules` table "
                "(kind, channel_id, hour_utc) in the killboard database."
            )
        else:
            for row in rows:
                kind = str(row["kind"] or "?")
                embed.add_field(
                    name=f"{kind.capitalize()} · {int(row['hour_utc'] or 0):02d}:00 UTC",
                    value=(
                        f"channel: {_chan(int(row['channel_id'] or 0))}\n"
                        f"last run: {row['last_run'] or 'never'}"
                    ),
                    inline=False,
                )
        await interaction.followup.send(
            embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @killboard.command(
        name="schedule-add",
        description="(admin) Set an automated daily/weekly/monthly ranking post.",
    )
    @app_commands.describe(
        kind="how often to post the ranking",
        channel="channel the ranking is posted to (start typing to filter the list)",
        hour_utc="hour of day in UTC to post (0-23)",
    )
    @app_commands.check(_is_admin)
    async def schedule_add(
        self,
        interaction: discord.Interaction,
        kind: Literal["daily", "weekly", "monthly"],
        channel: (
            discord.TextChannel | discord.VoiceChannel | discord.StageChannel | discord.Thread
        ),
        hour_utc: app_commands.Range[int, 0, 23],
    ) -> None:
        """Create/replace the ``kind`` scheduled ranking post (§8.3).

        One row per kind: adding a ``daily`` replaces any existing daily. The
        scheduler picks it up on its next tick (within ~60s) and posts the
        completed period's ranking at ``hour_utc``:00 UTC. The channel picker
        accepts text / voice / stage / thread channels (type to filter the list).
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._to_thread(_upsert_schedule, self._store, kind, channel.id, int(hour_utc))
        await interaction.followup.send(
            f"✅ **{kind.capitalize()} ranking** will post to {channel.mention} at "
            f"**{int(hour_utc):02d}:00 UTC** — it reports the completed {kind[:-2]}"
            f"{'y' if kind == 'daily' else ''} and fires on the next scheduler tick.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @killboard.command(
        name="schedule-remove", description="(admin) Stop an automated ranking post."
    )
    @app_commands.describe(kind="which scheduled post to remove")
    @app_commands.check(_is_admin)
    async def schedule_remove(
        self,
        interaction: discord.Interaction,
        kind: Literal["daily", "weekly", "monthly"],
    ) -> None:
        """Remove the ``kind`` scheduled ranking post (§8.3)."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        removed = await self._to_thread(_delete_schedule, self._store, kind)
        msg = (
            f"🗑️ Removed the **{kind}** ranking post."
            if removed
            else f"No **{kind}** ranking post was configured."
        )
        await interaction.followup.send(
            msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )


# ── blocking store helpers (run inside to_thread) ─────────────────────────────
#
# A handful of reads need the killboard's own tables (members, schedules, and
# top-of-window/name lookups) that KbStore deliberately does not surface as
# aggregate methods. They run on the killboard's own connection — never
# CORTANA's — mirroring the in-package pattern in ``killboard/schedule.py``.


def _resolve_player(store: KbStore, name: str) -> tuple[str, str, int | None] | None:
    """Resolve an exact (case-insensitive) player name → (id, display, lifetime).

    Tries the roster snapshot first (it carries lifetime Kill Fame), then falls
    back to the event store's killer/victim names. Returns ``None`` when the name
    has never been seen — the command turns that into a friendly notice.
    """
    key = name.strip().lower()
    if not key:
        return None

    row = db.query_one(
        store._conn,  # noqa: SLF001 — killboard's own roster table
        "SELECT player_id, name, kill_fame FROM members WHERE LOWER(name) = ? LIMIT 1",
        (key,),
    )
    if row is not None and row["player_id"]:
        fame = int(row["kill_fame"]) if row["kill_fame"] is not None else None
        return str(row["player_id"]), str(row["name"] or name), fame

    row = db.query_one(
        store._conn,  # noqa: SLF001 — killboard's own event store
        """
        SELECT pid, nm FROM (
            SELECT killer_id AS pid, killer_name AS nm FROM events
                WHERE killer_id IS NOT NULL AND LOWER(killer_name) = ?
            UNION
            SELECT victim_id AS pid, victim_name AS nm FROM events
                WHERE victim_id IS NOT NULL AND LOWER(victim_name) = ?
        ) LIMIT 1
        """,
        (key, key),
    )
    if row is not None and row["pid"]:
        return str(row["pid"]), str(row["nm"] or name), None
    return None


def _top_kill_events(store: KbStore, start: str, end: str, limit: int) -> list[sqlite3.Row]:
    """The highest-fame KILL events in ``[start, end)``, fame-descending (§10)."""
    return db.query(
        store._conn,  # noqa: SLF001 — killboard's own event store
        """
        SELECT event_id, killer_name, victim_name, total_fame, location
        FROM events
        WHERE relation = ? AND timestamp >= ? AND timestamp < ?
        ORDER BY total_fame DESC
        LIMIT ?
        """,
        (KILL, start, end, max(limit, 0)),
    )


def _guild_summary(store: KbStore, start: str, end: str) -> dict[str, Any]:
    """Roster count + lifetime fame (snapshot) and month-window totals (events)."""
    members = db.query_value(store._conn, "SELECT COUNT(*) FROM members")  # noqa: SLF001
    lifetime = db.query_value(  # noqa: SLF001
        store._conn, "SELECT COALESCE(SUM(kill_fame), 0) FROM members"
    )
    return {
        "members": int(members or 0),
        "lifetime_fame": int(lifetime or 0),
        "kills": store.kill_count(start, end),
        "deaths": store.death_count(start, end),
        "fame": store.kill_fame(start, end),
        "kd": store.kd(start, end),
    }


def _status_snapshot(store: KbStore) -> tuple[dict[str, Any], int]:
    """The poll-state row plus the stored-event count, in one thread hop (§13)."""
    state = store.poll_state()
    stored = db.query_value(store._conn, "SELECT COUNT(*) FROM events")  # noqa: SLF001
    return state, int(stored or 0)


def _list_schedules(store: KbStore) -> list[sqlite3.Row]:
    """Every scheduled-post row, ordered for display (§8.3)."""
    return db.query(
        store._conn,  # noqa: SLF001 — killboard's own schedules table
        "SELECT id, kind, channel_id, hour_utc, last_run FROM schedules ORDER BY kind, hour_utc",
    )


def _upsert_schedule(store: KbStore, kind: str, channel_id: int, hour_utc: int) -> None:
    """Create or update the ``kind`` schedule (one row per kind) (§8.3).

    Replaces any existing row of that kind so a guild has at most one daily /
    weekly / monthly post, and clears ``last_run`` so the new schedule can fire
    on the next tick. Blocking — call inside ``to_thread``.
    """
    conn = store._conn  # noqa: SLF001 — killboard's own schedules table
    db.execute(conn, "DELETE FROM schedules WHERE kind = ?", (kind,))
    db.execute(
        conn,
        "INSERT INTO schedules (kind, channel_id, hour_utc, last_run) VALUES (?, ?, ?, NULL)",
        (kind, channel_id, hour_utc),
    )


def _delete_schedule(store: KbStore, kind: str) -> int:
    """Remove the ``kind`` schedule; returns how many rows were deleted (§8.3)."""
    conn = store._conn  # noqa: SLF001 — killboard's own schedules table
    before = db.query_value(conn, "SELECT COUNT(*) FROM schedules WHERE kind = ?", (kind,)) or 0
    db.execute(conn, "DELETE FROM schedules WHERE kind = ?", (kind,))
    return int(before)


# ── pure formatting helpers ───────────────────────────────────────────────────


def _event_line(row: Any) -> str:
    """One compact ``/recent`` line: relation marker, killer ▸ victim, fame."""
    marker = "🟢" if row.relation == KILL else ("🔴" if row.relation == DEATH else "⚔️")
    killer = _clip(row.killer_name or "Unknown", 24)
    victim = _clip(row.victim_name or "Unknown", 24)
    loc = f" · {_clip(row.location, 20)}" if row.location else ""
    return f"{marker} **{killer}** ▸ {victim} — {_fmt_num(int(row.total_fame or 0))} fame{loc}"


def _top_kill_line(rank: int, row: sqlite3.Row) -> str:
    """One ``/topkills`` line: rank, killer ▸ victim, fame, killboard link."""
    killer = _clip(row["killer_name"] or "Unknown", 24)
    victim = _clip(row["victim_name"] or "Unknown", 24)
    fame = _fmt_num(int(row["total_fame"] or 0))
    url = _KILLBOARD_URL.format(event_id=int(row["event_id"]))
    return f"`{rank}.` **{killer}** ▸ {victim} — [{fame} fame]({url})"


def _chan(channel_id: int) -> str:
    """A channel mention for a configured id, or ``unset`` for the 0 sentinel."""
    return f"<#{channel_id}>" if channel_id else "unset"


def _fmt_num(value: int) -> str:
    """Compact human number: ``5_000_000`` → ``5.0M``, ``12_300`` → ``12.3K``."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _clip(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` chars with an ellipsis when it overruns."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _parse_iso(value: str | None) -> datetime | None:
    """Parse a stored ISO-8601 timestamp tolerantly (trailing ``Z`` accepted)."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _age(value: str | None, now: datetime) -> str:
    """A stored timestamp rendered as a coarse age (``5m ago``), or ``never``."""
    dt = _parse_iso(value)
    if dt is None:
        return "never" if not value else "unknown"
    secs = (now - dt).total_seconds()
    if secs < 0:
        return "just now"
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs // 60)}m ago"
    if secs < 172800:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _older_than_minutes(value: str | None, minutes: int, now: datetime) -> bool:
    """Whether ``value`` is missing or older than ``minutes`` before ``now``."""
    dt = _parse_iso(value)
    if dt is None:
        return True
    return (now - dt).total_seconds() > minutes * 60
