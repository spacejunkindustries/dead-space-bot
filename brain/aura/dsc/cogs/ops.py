"""Fleet-ops slash commands: /timer /formup /rollcall /jumps (GDD §7 / §13).

Timers and form-ups are the slash twins of their voice commands and route
through the same ``IncidentEngine.report`` entry point (constraint 10);
/rollcall and /jumps are read-only views over voice presence, routing roles,
and the gazetteer's adjacency graph.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
import structlog
from discord import app_commands
from discord.ext import commands

from aura.core import db
from aura.core.incidents import parse_duration
from aura.dsc.bot import resolve_typed_system
from aura.dsc.cogs.intel import outcome_text, system_autocomplete
from aura.types import Intent, ParsedCommand

if TYPE_CHECKING:  # pragma: no cover
    from aura.dsc.bot import AuraBot

__all__ = ["OpsCog"]

log = structlog.get_logger(__name__)


class OpsCog(commands.Cog):
    """Timers, form-ups, roll call, and jump distance."""

    def __init__(self, bot: AuraBot) -> None:
        self.bot = bot

    # ── /timer and /formup share the engine report path ──────────────────────

    async def _scheduled(
        self,
        interaction: discord.Interaction,
        intent: Intent,
        system: str,
        when: str,
        note: str | None,
        command_name: str,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if parse_duration(when) is None:
            await interaction.response.send_message(
                f"Couldn't read a duration from `{when}` — try `4 hours`, `90 minutes`, `1h30m`.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        resolution = resolve_typed_system(self.bot.gazetteer, system)
        if resolution is None:
            await interaction.followup.send(
                f"Unknown system `{system}` — pick one from the autocomplete.", ephemeral=True
            )
            return
        # The engine parses the duration out of ``detail``, exactly as the
        # voice grammar delivers it (GDD §6.1: "timer <system> <duration>").
        detail = when + (f" {note}" if note else "")
        raw = f"/{command_name} {system} {detail}"
        parsed = ParsedCommand(
            intent=intent, system_text=system, group_alias=None, detail=detail, raw=raw
        )
        outcome = await self.bot.engine.report(
            interaction.guild_id, interaction.user.id, parsed, resolution
        )
        await interaction.followup.send(
            outcome_text(outcome.outcome, outcome.utterance), ephemeral=True
        )

    @app_commands.command(name="timer", description="Schedule a structure timer ping")
    @app_commands.describe(
        system="System name",
        duration="How far out, e.g. '4 hours' or '90 minutes'",
        note="Optional note, e.g. 'armor timer, Astrahus'",
    )
    @app_commands.autocomplete(system=system_autocomplete)
    async def timer(
        self,
        interaction: discord.Interaction,
        system: str,
        duration: str,
        note: str | None = None,
    ) -> None:
        await self._scheduled(interaction, Intent.TIMER, system, duration, note, "timer")

    @app_commands.command(name="formup", description="Post an op with RSVP buttons")
    @app_commands.describe(
        system="Form-up system",
        when="How soon, e.g. '15 minutes'",
        note="Optional note, e.g. 'kitchen sink, bring tackle'",
    )
    @app_commands.autocomplete(system=system_autocomplete)
    async def formup(
        self,
        interaction: discord.Interaction,
        system: str,
        when: str,
        note: str | None = None,
    ) -> None:
        await self._scheduled(interaction, Intent.FORMUP, system, when, note, "formup")

    # ── /rollcall ────────────────────────────────────────────────────────────

    @app_commands.command(
        name="rollcall", description="Who's in voice, subscribed, and responding"
    )
    async def rollcall(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = discord.Embed(
            title="Roll call", color=0x3498DB, timestamp=datetime.now(UTC)
        )
        embed.add_field(name="In voice", value=self._voice_field(guild), inline=False)
        embed.add_field(name="Subscriptions", value=self._subs_field(guild), inline=False)
        embed.add_field(
            name="Responding", value=await self._responders_field(guild.id), inline=False
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    def _voice_field(self, guild: discord.Guild) -> str:
        lines: list[str] = []
        for channel_id in self.bot.holder.current.discord.watch_voice_channels:
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                continue
            pilots = [m.display_name for m in channel.members if not m.bot]
            if pilots:
                lines.append(f"**{channel.name}** ({len(pilots)}): {', '.join(sorted(pilots))}")
            else:
                lines.append(f"**{channel.name}**: empty")
        return "\n".join(lines) or "No watched voice channels configured."

    def _subs_field(self, guild: discord.Guild) -> str:
        lines: list[str] = []
        for role_id, name in self.bot.subscription_role_pairs(guild):
            role = guild.get_role(role_id)
            count = len(role.members) if role is not None else 0
            lines.append(f"**{name}**: {count}")
        return "\n".join(lines) or "No routing rules loaded."

    async def _responders_field(self, guild_id: int) -> str:
        rows = await asyncio.to_thread(
            db.query,
            self.bot.conn,
            "SELECT i.system_id,"
            " SUM(CASE WHEN r.state = 'OTW' THEN 1 ELSE 0 END) AS otw,"
            " SUM(CASE WHEN r.state = 'WATCHING' THEN 1 ELSE 0 END) AS watching"
            " FROM incidents i JOIN responders r ON r.incident_id = i.id"
            " WHERE i.guild_id = ? AND i.status = 'ACTIVE'"
            " GROUP BY i.id ORDER BY i.updated_at DESC LIMIT 10",
            (guild_id,),
        )
        if not rows:
            return "No active incidents."
        lines: list[str] = []
        for row in rows:
            entry = (
                self.bot.gazetteer.by_id(row["system_id"])
                if row["system_id"] is not None
                else None
            )
            system = entry.name if entry is not None else "unknown"
            lines.append(f"**{system}**: 🚀 {row['otw']} · 👀 {row['watching']}")
        return "\n".join(lines)

    # ── /jumps ───────────────────────────────────────────────────────────────

    @app_commands.command(name="jumps", description="Jump distance between two systems")
    @app_commands.describe(from_system="Start system", to_system="Destination system")
    @app_commands.rename(from_system="from", to_system="to")
    @app_commands.autocomplete(from_system=system_autocomplete, to_system=system_autocomplete)
    async def jumps(
        self, interaction: discord.Interaction, from_system: str, to_system: str
    ) -> None:
        gaz = self.bot.gazetteer
        origin = gaz.by_name(from_system.strip())
        dest = gaz.by_name(to_system.strip())
        missing = [
            name
            for name, entry in ((from_system, origin), (to_system, dest))
            if entry is None
        ]
        if origin is None or dest is None:
            await interaction.response.send_message(
                "Unknown system: " + ", ".join(f"`{n}`" for n in missing),
                ephemeral=True,
            )
            return
        distance = gaz.jumps(origin.id, dest.id)
        if distance is None:
            text = f"**{origin.name}** and **{dest.name}** are not connected in the gazetteer."
        else:
            plural = "jump" if distance == 1 else "jumps"
            text = f"**{origin.name}** → **{dest.name}**: {distance} {plural}."
        await interaction.response.send_message(text, ephemeral=True)
