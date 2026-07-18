"""Fun slash commands: /fact and /insult (GDD §13.2).

The slash twins of the voice ``FACT``/``INSULT`` intents — same
:class:`~cortana.core.fun.FunEngine`, same shuffle bags, same cooldowns
(constraint 10). Delivery is the mirror of the voice path's: the reply posts
in the channel the command was invoked in, and only there — voice requests
answer in voice only.

Mentions: an ``/insult`` naming a member renders their mention but never
notifies them (``AllowedMentions.none()``) — the escalation authority
(constraint 11) stays untouched because nothing here can ping anyone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
import structlog
from discord import app_commands
from discord.ext import commands

from cortana.core.fun import CATEGORIES, FunCooldown, FunUnavailable

if TYPE_CHECKING:  # pragma: no cover
    from cortana.dsc.bot import AuraBot

__all__ = ["FunCog"]

log = structlog.get_logger(__name__)

_DISABLED = "Fun commands are off (`fun.enabled: false`)."
_EMPTY = "The library didn't load — check the deployment (`fun_library_loaded` in the logs)."


class FunCog(commands.Cog):
    """/fact and /insult — entertainment, strictly throttled off the intel path."""

    def __init__(self, bot: AuraBot) -> None:
        self.bot = bot

    def _engine(self):  # noqa: ANN202 — narrow helper
        fun = self.bot.fun
        if fun is None or not self.bot.holder.current.fun.enabled:
            return None
        return fun

    @app_commands.command(name="fact", description="A random true fact, from a huge library")
    @app_commands.describe(category="Pick a category, or leave empty for anything")
    @app_commands.choices(
        category=[
            app_commands.Choice(name=title, value=key)
            for key, (title, _aliases) in CATEGORIES.items()
        ]
    )
    async def fact(self, interaction: discord.Interaction, category: str | None = None) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        fun = self._engine()
        if fun is None:
            await interaction.response.send_message(_DISABLED, ephemeral=True)
            return
        try:
            line = fun.next_fact(interaction.guild_id, category)
        except FunCooldown as exc:
            await interaction.response.send_message(
                f"⏳ Cooling down — try again in {max(1, round(exc.remaining_s))}s.",
                ephemeral=True,
            )
            return
        except FunUnavailable:
            await interaction.response.send_message(_EMPTY, ephemeral=True)
            return
        log.info("fact_via_slash", user_id=interaction.user.id, category=line.category_key)
        await interaction.response.send_message(
            f"🧠 **{line.category_title}** · {line.text}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="insult", description="Have CORTANA roast someone (all in good fun)")
    @app_commands.describe(target="Who's catching it — leave empty for a general roast")
    async def insult(
        self, interaction: discord.Interaction, target: discord.Member | None = None
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        fun = self._engine()
        if fun is None:
            await interaction.response.send_message(_DISABLED, ephemeral=True)
            return
        try:
            line = fun.next_insult(interaction.guild_id)
        except FunCooldown as exc:
            await interaction.response.send_message(
                f"⏳ Cooling down — try again in {max(1, round(exc.remaining_s))}s.",
                ephemeral=True,
            )
            return
        except FunUnavailable:
            await interaction.response.send_message(_EMPTY, ephemeral=True)
            return
        content = f"{target.mention} — {line}" if target is not None else line
        log.info("insult_via_slash", user_id=interaction.user.id, targeted=target is not None)
        # The mention RENDERS but never notifies: AllowedMentions.none() —
        # roasting a friend must not ping them out of a fight.
        await interaction.response.send_message(
            f"🔥 {content}", allowed_mentions=discord.AllowedMentions.none()
        )
