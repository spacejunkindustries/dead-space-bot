"""Self-service subscription, consent & identity commands (GDD §7 / §10.2 / §19).

/subscribe   role picker built from the loaded routing rules
/mysubs      the member's roles ∩ routing roles, ephemeral
/optout      exclude the member's audio entirely — the refreshed opt-out set
             is pushed to Ears immediately, where the drop is enforced BEFORE
             frames cross the IPC boundary (CLAUDE.md: opt-out lives in Ears)
/mute-voice  stop AURA speaking to this member (GDD §12.2)
/register    register a pilot callsign — the slash twin of the voice command
/unregister  delete the callsign row
/whoami      speak back the registered callsign

The callsign commands are thin adapters over ``IncidentEngine.report``
(constraint 10) — voice and slash hit the identical registry path. Identity is
the member's Discord user id; this is a name registry, never voice biometrics
(GDD §19).
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
import structlog
from discord import app_commands
from discord.ext import commands

from aura.core import db
from aura.dsc.cogs.intel import outcome_text
from aura.dsc.views import SubscriptionView
from aura.nlu.grammar import sanitize_callsign
from aura.types import Intent, ParsedCommand

if TYPE_CHECKING:  # pragma: no cover
    from aura.dsc.bot import AuraBot

__all__ = ["SubsCog"]

log = structlog.get_logger(__name__)


def _toggle_row(conn: sqlite3.Connection, table: str, user_id: int) -> bool:
    """Toggle a (user_id, at) row; returns True when the row now exists.

    ``table`` is one of the two consent tables — never interpolate user input.
    """
    if table not in ("optouts", "voice_mutes"):  # defence in depth
        raise ValueError(f"not a consent table: {table}")
    existing = db.query_one(conn, f"SELECT user_id FROM {table} WHERE user_id = ?", (user_id,))
    if existing is not None:
        db.execute(conn, f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        return False
    db.execute(
        conn,
        f"INSERT INTO {table} (user_id, at) VALUES (?, ?)",
        (user_id, datetime.now(UTC).isoformat()),
    )
    return True


class SubsCog(commands.Cog):
    """Subscriptions and per-user consent toggles."""

    def __init__(self, bot: AuraBot) -> None:
        self.bot = bot

    # ── /subscribe ───────────────────────────────────────────────────────────

    @app_commands.command(name="subscribe", description="Pick which alert roles you subscribe to")
    async def subscribe(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        pairs = self.bot.subscription_role_pairs(guild)
        if not pairs:
            await interaction.response.send_message(
                "No subscription roles are configured — an admin needs to load "
                "routing rules (`/routing reload`).",
                ephemeral=True,
            )
            return
        member_role_ids = {r.id for r in interaction.user.roles}
        await interaction.response.send_message(
            "Toggle your subscriptions:",
            view=SubscriptionView(pairs, member_role_ids),
            ephemeral=True,
        )

    # ── /mysubs ──────────────────────────────────────────────────────────────

    @app_commands.command(name="mysubs", description="Show your alert subscriptions")
    async def mysubs(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        routing_ids = set(self.bot.routing_role_ids())
        held = [r.name for r in interaction.user.roles if r.id in routing_ids]
        if held:
            text = "You are subscribed to: " + ", ".join(f"**{n}**" for n in sorted(held))
        else:
            text = "You have no alert subscriptions. `/subscribe` to pick some."
        await interaction.response.send_message(text, ephemeral=True)

    # ── /optout (GDD §19) ────────────────────────────────────────────────────

    @app_commands.command(
        name="optout", description="Toggle: exclude your audio from AURA entirely"
    )
    async def optout(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = interaction.user.id
        opted_out = await asyncio.to_thread(_toggle_row, self.bot.conn, "optouts", user_id)
        # Push the refreshed set to Ears NOW — the drop is enforced there,
        # before any frame crosses the IPC boundary.
        gateway = self.bot.voice_gateway
        if gateway is not None:
            await gateway.push_optouts()
        else:  # pragma: no cover — only during partial wiring
            log.warning("optout_push_skipped_no_gateway", user_id=user_id)
        log.info("optout_toggled", user_id=user_id, opted_out=opted_out)
        if opted_out:
            text = (
                "🔇 You are opted out. Your audio is dropped inside Ears before any "
                "processing — it never reaches the recogniser. `/optout` again to opt back in."
            )
        else:
            text = "🎙️ You are opted back in. AURA will listen for the wake phrase from you."
        await interaction.followup.send(text, ephemeral=True)

    # ── /mute-voice (GDD §12.2) ──────────────────────────────────────────────

    @app_commands.command(
        name="mute-voice", description="Toggle: stop AURA speaking replies to you"
    )
    async def mute_voice(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = interaction.user.id
        muted = await asyncio.to_thread(_toggle_row, self.bot.conn, "voice_mutes", user_id)
        self.bot.speaker.set_muted(user_id, muted)
        log.info("voice_mute_toggled", user_id=user_id, muted=muted)
        text = (
            "🔕 AURA will not speak replies to your commands. `/mute-voice` again to undo."
            if muted
            else "🔔 AURA will speak replies to your commands again."
        )
        await interaction.followup.send(text, ephemeral=True)

    # ── callsign registry: /register /unregister /whoami (GDD §6.1) ──────────

    async def _callsign_command(
        self, interaction: discord.Interaction, intent: Intent, detail: str | None, raw: str
    ) -> None:
        """Shared engine dispatch — the slash half of constraint 10."""
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        parsed = ParsedCommand(
            intent=intent, system_text=None, group_alias=None, detail=detail, raw=raw
        )
        outcome = await self.bot.engine.report(
            interaction.guild_id, interaction.user.id, parsed, None
        )
        await interaction.followup.send(
            outcome_text(outcome.outcome, outcome.utterance), ephemeral=True
        )

    @app_commands.command(
        name="register",
        description="Register your pilot callsign (a name, tied to your Discord id)",
    )
    @app_commands.describe(callsign="Your callsign, e.g. 'Space Junkie' (max 32 characters)")
    async def register(self, interaction: discord.Interaction, callsign: str) -> None:
        # Typed input is exact — the sanitiser only strips markdown/mention
        # characters and caps length; case is preserved. This is also how a
        # pilot fixes an STT misspelling from the voice path.
        cleaned = sanitize_callsign(callsign)
        await self._callsign_command(interaction, Intent.REGISTER, cleaned, f"/register {callsign}")

    @app_commands.command(name="unregister", description="Delete your registered callsign")
    async def unregister(self, interaction: discord.Interaction) -> None:
        await self._callsign_command(interaction, Intent.UNREGISTER, None, "/unregister")

    @app_commands.command(name="whoami", description="Show your registered callsign")
    async def whoami(self, interaction: discord.Interaction) -> None:
        await self._callsign_command(interaction, Intent.WHOAMI, None, "/whoami")
