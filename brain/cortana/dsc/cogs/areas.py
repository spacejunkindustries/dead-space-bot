"""Custom-area management: /areas-list /areas-forget /areas-add (GDD §8.5a).

The learning itself happens by talking — a pilot confirms an unknown place word
("Did you say the branch?") and CORTANA remembers it. This cog is the admin
surface (constraint 10): review what she has learned, prune a mishearing that
slipped through, or pre-seed a place without waiting to say it on comms. Listing
is open; mutating needs Manage Guild or the FC role, like the other admin cogs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
import structlog
from discord import app_commands
from discord.ext import commands

from cortana.core import areas
from cortana.nlu import grammar

if TYPE_CHECKING:  # pragma: no cover
    from cortana.dsc.bot import AuraBot

__all__ = ["AreasCog"]

log = structlog.get_logger(__name__)


def _is_admin(interaction: discord.Interaction) -> bool:
    """Manage Guild or the configured FC role (mirrors AdminCog)."""
    member = interaction.user
    if interaction.guild is None or not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_guild:
        return True
    bot: AuraBot = interaction.client  # type: ignore[assignment]
    fc_role = bot.holder.current.discord.roles.fc
    return any(r.id == fc_role for r in member.roles)


class AreasCog(commands.Cog):
    """Review, prune, and pre-seed CORTANA's learned custom areas (§8.5a)."""

    def __init__(self, bot: AuraBot) -> None:
        self.bot = bot

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            msg = "Admin only — needs Manage Guild or the FC role."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name="areas-list", description="List the custom areas CORTANA has learned"
    )
    async def areas_list(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        rows = await asyncio.to_thread(areas.list_areas, self.bot.conn, interaction.guild_id)
        cap = self.bot.holder.current.areas.max_per_guild
        if not rows:
            await interaction.response.send_message(
                "No custom areas learned yet — report a place she doesn't know and "
                "confirm it, or add one with `/areas-add`.",
                ephemeral=True,
            )
            return
        lines = [f"**{r['display_name']}** · used {r['uses']}×" for r in rows]
        body = "\n".join(lines)
        await interaction.response.send_message(
            f"Learned areas ({len(rows)}/{cap}):\n{body}", ephemeral=True
        )

    @app_commands.command(name="areas-forget", description="Forget a learned custom area")
    @app_commands.describe(word="The area word to forget (e.g. the branch)")
    @app_commands.check(_is_admin)
    async def areas_forget(self, interaction: discord.Interaction, word: str) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        # Clean to the same key the voice path stores (GDD §8.5a): "the branch"
        # from the FC matches a voice-learned "branch".
        place = grammar.clean_place(word)
        removed = await asyncio.to_thread(
            areas.forget_area, self.bot.conn, interaction.guild_id, place
        )
        if removed:
            log.info("custom_area_forgotten", guild_id=interaction.guild_id, word=place)
            await interaction.response.send_message(f"Forgot `{place}`.", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"`{word}` isn't a learned area — check `/areas-list`.", ephemeral=True
            )

    @app_commands.command(name="areas-add", description="Pre-seed a custom area without saying it")
    @app_commands.describe(word="The area word to remember (e.g. wildlands)")
    @app_commands.check(_is_admin)
    async def areas_add(self, interaction: discord.Interaction, word: str) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        # Store the SAME cleaned form the voice path would (GDD §8.5a), so an
        # FC pre-seeding "the branch" is what a pilot saying "the branch" finds.
        place = grammar.clean_place(word)
        if not place:
            await interaction.response.send_message("Give me a word to remember.", ephemeral=True)
            return
        cap = self.bot.holder.current.areas.max_per_guild
        result = await asyncio.to_thread(
            areas.save_area,
            self.bot.conn,
            interaction.guild_id,
            place,
            interaction.user.id,
            datetime.now(UTC).isoformat(),
            max_areas=cap,
        )
        if result == "at_cap":
            await interaction.response.send_message(
                f"Area limit reached ({cap}) — forget one with `/areas-forget` first.",
                ephemeral=True,
            )
        elif result == "reinforced":
            await interaction.response.send_message(
                f"`{place}` is already known — reinforced it.", ephemeral=True
            )
        else:
            log.info("custom_area_added", guild_id=interaction.guild_id, word=place)
            await interaction.response.send_message(f"Remembered `{place}`.", ephemeral=True)


async def setup(bot: AuraBot) -> None:  # pragma: no cover — discord.py entrypoint
    await bot.add_cog(AreasCog(bot))
