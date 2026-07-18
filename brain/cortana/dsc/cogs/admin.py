"""Admin slash commands: /routing /gazetteer /fleetmode /health (GDD §7).

Gated on Manage Guild or the FC role. These are operational levers, not
business logic: rule loading stays in the engine, gazetteer loading in the
gazetteer, degradation state in the health reporter.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, cast

import discord
import structlog
from discord import app_commands
from discord.ext import commands

from cortana.core import db

if TYPE_CHECKING:  # pragma: no cover
    from cortana.dsc.bot import AuraBot
    from cortana.reload import ReloadResult

__all__ = ["AdminCog"]

log = structlog.get_logger(__name__)


def _is_admin(interaction: discord.Interaction) -> bool:
    """Manage Guild or the configured FC role (GDD §7 admin gate)."""
    member = interaction.user
    if interaction.guild is None or not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_guild:
        return True
    bot = cast("AuraBot", interaction.client)
    fc_role = bot.holder.current.discord.roles.fc
    return any(r.id == fc_role for r in member.roles)


class AdminCog(commands.Cog):
    """Operational levers for the people running the bot."""

    def __init__(self, bot: AuraBot) -> None:
        self.bot = bot

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Admin only — needs Manage Guild or the FC role.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "Admin only — needs Manage Guild or the FC role.", ephemeral=True
                )
        # Anything else falls through to the tree-level error boundary
        # (AuraBot._on_app_command_error), which is invoked regardless.

    async def _run_reload(self, interaction: discord.Interaction) -> ReloadResult | None:
        """Run the one reload transaction (the SIGHUP/​/reload door) and
        report internal failures ephemerally; None means already answered.

        All three config files are validated together and swapped
        all-or-nothing — a bad gazetteer.yaml or routing.yaml comes back as
        a rejection line in the receipt (GazetteerError / RoutingConfigError
        included), never as an unhandled exception + eternal spinner."""
        request_reload = getattr(self.bot, "request_reload", None)
        if request_reload is None:
            await interaction.followup.send(
                "Reload isn't wired in this process — restart the service instead.",
                ephemeral=True,
            )
            return None
        try:
            return await request_reload()
        except Exception:
            log.exception("reload_via_slash_failed")
            await interaction.followup.send(
                "❌ Reload failed internally — logged. The old config stays in force.",
                ephemeral=True,
            )
            return None

    # ── /routing ─────────────────────────────────────────────────────────────

    @app_commands.command(name="routing", description="(admin) Manage subscription rules")
    @app_commands.describe(action="list the loaded rules, or reload routing.yaml")
    @app_commands.check(_is_admin)
    async def routing(
        self, interaction: discord.Interaction, action: Literal["list", "reload"]
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if action == "reload":
            result = await self._run_reload(interaction)
            if result is None:
                return
            count = len(getattr(self.bot.engine, "_rules", []))
            log.info("routing_reloaded_via_slash", count=count, user_id=interaction.user.id)
            marker = "✅" if result.ok else "⚠️"
            await interaction.followup.send(
                f"{marker} {result.summary()}\nRouting rules active: {count}.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(embed=self._rules_embed(guild), ephemeral=True)

    def _rules_embed(self, guild: discord.Guild) -> discord.Embed:
        rules = getattr(self.bot.engine, "_rules", [])
        embed = discord.Embed(title="Routing rules", color=0x3498DB, timestamp=datetime.now(UTC))
        if not rules:
            embed.description = "No rules loaded. `/routing reload` after editing routing.yaml."
            return embed
        for i, rule in enumerate(rules[:25]):
            role = guild.get_role(rule.role_id)
            name = role.name if role is not None else f"role {rule.role_id}"
            types = ", ".join(sorted(str(t) for t in rule.types))
            scope_bits: list[str] = []
            if rule.scope.systems:
                names = [
                    e.name
                    for sid in rule.scope.systems
                    if (e := self.bot.gazetteer.by_id(sid)) is not None
                ]
                scope_bits.append("systems: " + ", ".join(names))
            if rule.scope.regions:
                scope_bits.append("regions: " + ", ".join(rule.scope.regions))
            if rule.scope.within_jumps_of is not None:
                anchor_id, max_jumps = rule.scope.within_jumps_of
                anchor = self.bot.gazetteer.by_id(anchor_id)
                anchor_name = anchor.name if anchor is not None else str(anchor_id)
                scope_bits.append(f"≤{max_jumps} jumps of {anchor_name}")
            scope = "; ".join(scope_bits) or "everywhere"
            escalate = str(rule.escalate_at) if rule.escalate_at is not None else "never"
            quiet = (
                f"{rule.quiet_hours.start}–{rule.quiet_hours.end} {rule.quiet_hours.tz}"
                if rule.quiet_hours is not None
                else "none"
            )
            embed.add_field(
                name=f"{i + 1}. @{name}",
                value=f"types: {types}\nscope: {scope}\n@here at: {escalate} · quiet: {quiet}",
                inline=False,
            )
        return embed

    # ── /gazetteer ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="gazetteer", description="(admin) Inspect, reload, or prune the system gazetteer"
    )
    @app_commands.describe(action="info shows the active set; reload/prune re-apply scope rules")
    @app_commands.check(_is_admin)
    async def gazetteer(
        self, interaction: discord.Interaction, action: Literal["info", "reload", "prune"]
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        gaz = self.bot.gazetteer
        if action == "info":
            await interaction.followup.send(embed=self._gazetteer_embed(), ephemeral=True)
            return
        # reload and prune both rebuild the active set from gazetteer.yaml's
        # scope rules — pruning IS the reload (GDD §8.1: the set stays small).
        # Runs through the one reload transaction so all three config files
        # stay in lockstep, and a bad gazetteer.yaml reports its rejection
        # instead of leaving the admin staring at an eternal spinner.
        before = len(gaz.systems)
        result = await self._run_reload(interaction)
        if result is None:
            return
        after = len(gaz.systems)
        log.info(
            "gazetteer_reloaded_via_slash",
            action=action,
            before=before,
            after=after,
            user_id=interaction.user.id,
        )
        marker = "✅" if result.ok else "⚠️"
        await interaction.followup.send(
            f"{marker} {result.summary()}\nGazetteer active set: {before} → {after} systems.",
            ephemeral=True,
        )

    def _gazetteer_embed(self) -> discord.Embed:
        gaz = self.bot.gazetteer
        systems = list(gaz.systems)
        regions = Counter(s.region for s in systems)
        home = gaz.by_id(gaz.home_system_id) if gaz.home_system_id is not None else None
        region_lines = [f"**{name}**: {count}" for name, count in regions.most_common(10)]
        embed = discord.Embed(title="Gazetteer", color=0x3498DB, timestamp=datetime.now(UTC))
        embed.add_field(name="Active systems", value=str(len(systems)), inline=True)
        embed.add_field(
            name="Home system", value=home.name if home is not None else "unset", inline=True
        )
        embed.add_field(name="Regions", value="\n".join(region_lines) or "none", inline=False)
        return embed

    # ── /fleetmode ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="fleetmode", description="(admin) Restrict voice triggering to the FC role"
    )
    @app_commands.describe(action="on during structured ops; off to reopen voice to all pilots")
    @app_commands.check(_is_admin)
    async def fleetmode(
        self, interaction: discord.Interaction, action: Literal["on", "off"]
    ) -> None:
        enabled = action == "on"
        self.bot.discipline.set_fleetmode(enabled)
        log.info("fleetmode_via_slash", enabled=enabled, user_id=interaction.user.id)
        text = (
            "🎯 Fleet-ops mode ON — only the FC role can voice-trigger. "
            "Slash commands stay open to everyone (GDD §11.4)."
            if enabled
            else "✅ Fleet-ops mode OFF — voice triggering reopened to all pilots."
        )
        await interaction.response.send_message(text, ephemeral=True)

    # ── /clearall ────────────────────────────────────────────────────────────

    @app_commands.command(
        name="clearall", description="(admin) Resolve EVERY active incident card at once"
    )
    @app_commands.check(_is_admin)
    async def clearall(self, interaction: discord.Interaction) -> None:
        """The board-wipe: after an op (or a pile of test cards), close every
        active incident in one go. Cards are edited to RESOLVED in place —
        history stays readable, nothing is deleted."""
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        cleared = await self.bot.engine.clear_all(interaction.guild_id)
        log.info("clearall_via_slash", user_id=interaction.user.id, count=len(cleared))
        if not cleared:
            await interaction.followup.send("Board already clear — no active incidents.")
        else:
            await interaction.followup.send(
                f"🧹 Cleared **{len(cleared)}** active incident"
                f"{'s' if len(cleared) != 1 else ''} — cards marked resolved."
            )

    # ── /health ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="health", description="(admin) Pipeline status, STT confidence, incident counts"
    )
    @app_commands.check(_is_admin)
    async def health(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        embeds: list[discord.Embed] = []
        reporter = self.bot.health_reporter
        if reporter is not None:
            embeds.append(discord.Embed.from_dict(reporter.build_report_embed(datetime.now(UTC))))
        embeds.append(await self._db_health_embed(interaction.guild_id))
        await interaction.followup.send(embeds=embeds, ephemeral=True)

    async def _db_health_embed(self, guild_id: int) -> discord.Embed:
        def _collect() -> tuple[list, list]:
            status_rows = db.query(
                self.bot.conn,
                "SELECT status, COUNT(*) AS n FROM incidents WHERE guild_id = ?"
                " AND opened_at >= ? GROUP BY status",
                (guild_id, (datetime.now(UTC) - timedelta(hours=24)).isoformat()),
            )
            tier_rows = db.query(
                self.bot.conn,
                "SELECT tier, outcome, confidence FROM command_log ORDER BY id DESC LIMIT 100",
            )
            return status_rows, tier_rows

        status_rows, tier_rows = await asyncio.to_thread(_collect)
        status_line = " · ".join(f"{row['status']}: {row['n']}" for row in status_rows) or "none"
        tiers = Counter(row["tier"] or "n/a" for row in tier_rows)
        outcomes = Counter(row["outcome"] or "n/a" for row in tier_rows)
        confidences = [row["confidence"] for row in tier_rows if row["confidence"] is not None]
        avg_conf = f"{sum(confidences) / len(confidences):.2f}" if confidences else "n/a"
        tier_line = " · ".join(f"{t}: {n}" for t, n in sorted(tiers.items())) or "no commands yet"
        outcome_line = (
            " · ".join(f"{o}: {n}" for o, n in sorted(outcomes.items())) or "no commands yet"
        )
        embed = discord.Embed(
            title="Command log (last 100) & incidents (24h)",
            color=0x3498DB,
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Incidents (24h)", value=status_line, inline=False)
        embed.add_field(name="Resolution tiers", value=tier_line, inline=False)
        embed.add_field(name="Outcomes", value=outcome_line, inline=False)
        embed.add_field(name="Mean match confidence", value=avg_conf, inline=True)
        embed.add_field(
            name="Fleet-ops mode",
            value="on" if self.bot.discipline.fleetmode else "off",
            inline=True,
        )
        return embed
