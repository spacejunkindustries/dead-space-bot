"""Self-service subscription, consent & identity commands (GDD §7 / §10.2 / §19).

/subscribe    role picker built from the loaded routing rules
/mysubs       the member's roles ∩ routing roles, plus personal pings, ephemeral
/pingme       personal ping subscription — the slash twin of "ping me for ..."
/mypings      list this member's personal pings, ephemeral
/pingme-clear delete personal pings (all, or one by /mypings index)
/optout       exclude the member's audio entirely — the refreshed opt-out set
              is pushed to Ears immediately, where the drop is enforced BEFORE
              frames cross the IPC boundary (CLAUDE.md: opt-out lives in Ears)
/mute-voice   stop AURA speaking to this member (GDD §12.2)
/register     register a pilot callsign — the slash twin of the voice command
/unregister   delete the callsign row
/whoami       speak back the registered callsign

The callsign and personal-ping commands are thin adapters over
``IncidentEngine.report`` (constraint 10) — voice and slash hit the identical
registry paths. Identity is the member's Discord user id; this is a name
registry, never voice biometrics (GDD §19).
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

from aura import tts
from aura.core import db
from aura.core.personal_pings import PingSub
from aura.dsc.bot import resolve_typed_system
from aura.dsc.cogs.intel import outcome_text, system_autocomplete
from aura.dsc.views import SubscriptionView
from aura.nlu.grammar import PING_TYPE_ORDER, encode_ping_types, sanitize_callsign
from aura.types import Intent, ParsedCommand, Resolution

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
        # Personal pings ride underneath the role subscriptions (GDD §10.3).
        pings = self.bot.engine.personal_pings.list_for(guild.id, interaction.user.id)
        if pings:
            lines = [f"{i}. {self._ping_line(sub)}" for i, sub in enumerate(pings, start=1)]
            text += "\nPersonal pings:\n" + "\n".join(lines)
        await interaction.response.send_message(text, ephemeral=True)

    # ── personal pings: /pingme /mypings /pingme-clear (GDD §10.3) ───────────

    def _ping_line(self, sub: PingSub) -> str:
        """One /mypings line: spoken type phrase + scope, e.g.
        ``Gate camps — Otanuomi`` or ``Everything — everywhere``."""
        phrase = tts.ping_types_phrase(sub.types).capitalize()
        where = "everywhere"
        if sub.system_id is not None:
            entry = self.bot.gazetteer.by_id(sub.system_id)
            where = entry.name if entry is not None else f"system {sub.system_id}"
        return f"{phrase} — {where}"

    async def _ping_command(
        self,
        interaction: discord.Interaction,
        intent: Intent,
        detail: str | None,
        system_text: str | None,
        resolution: Resolution | None,
        raw: str,
    ) -> None:
        """Shared engine dispatch — the slash half of constraint 10."""
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        parsed = ParsedCommand(
            intent=intent, system_text=system_text, group_alias=None, detail=detail, raw=raw
        )
        outcome = await self.bot.engine.report(
            interaction.guild_id, interaction.user.id, parsed, resolution
        )
        await interaction.followup.send(
            outcome_text(outcome.outcome, outcome.utterance), ephemeral=True
        )

    @app_commands.command(
        name="pingme", description="Ping you (a user mention) when matching incidents post"
    )
    @app_commands.describe(
        type="Which incident types ping you",
        system="Limit to one system (omit for everywhere)",
    )
    @app_commands.choices(
        type=[
            app_commands.Choice(name="Hostiles", value=str(Intent.HOSTILE_SPOTTED)),
            app_commands.Choice(name="Under attack", value=str(Intent.UNDER_ATTACK)),
            app_commands.Choice(name="Assist requests", value=str(Intent.ASSIST_REQUEST)),
            app_commands.Choice(name="Gate camps", value=str(Intent.GATE_CAMP)),
            app_commands.Choice(name="Anything", value="ALL"),
        ]
    )
    @app_commands.autocomplete(system=system_autocomplete)
    async def pingme(
        self, interaction: discord.Interaction, type: str, system: str | None = None
    ) -> None:
        types = frozenset(PING_TYPE_ORDER) if type == "ALL" else frozenset((Intent(type),))
        resolution: Resolution | None = None
        if system is not None:
            resolution = resolve_typed_system(self.bot.gazetteer, system)
            if resolution is None:
                await interaction.response.send_message(
                    f"Unknown system `{system}` — pick one from the autocomplete "
                    "(the gazetteer is scoped to our operational region).",
                    ephemeral=True,
                )
                return
        raw = f"/pingme {type}" + (f" {system}" if system else "")
        await self._ping_command(
            interaction, Intent.PING_ME, encode_ping_types(types), system, resolution, raw
        )

    @app_commands.command(name="mypings", description="List your personal pings")
    async def mypings(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        pings = self.bot.engine.personal_pings.list_for(interaction.guild_id, interaction.user.id)
        if not pings:
            await interaction.response.send_message(
                "You have no pings set. `/pingme` to add one.", ephemeral=True
            )
            return
        lines = [f"{i}. {self._ping_line(sub)}" for i, sub in enumerate(pings, start=1)]
        await interaction.response.send_message(
            "Your personal pings:\n" + "\n".join(lines), ephemeral=True
        )

    @app_commands.command(
        name="pingme-clear", description="Remove your personal pings (all, or one by index)"
    )
    @app_commands.describe(index="A /mypings entry number — omit to remove all")
    async def pingme_clear(
        self, interaction: discord.Interaction, index: int | None = None
    ) -> None:
        if index is None:
            # The slash twin of "stop pinging me" — same engine path.
            await self._ping_command(
                interaction, Intent.PING_ME_CLEAR, None, None, None, "/pingme-clear"
            )
            return
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        removed = await self.bot.engine.personal_pings.remove(
            interaction.guild_id, interaction.user.id, index
        )
        if removed is None:
            await interaction.followup.send(
                f"❌ No personal ping #{index} — check `/mypings`.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"✅ Removed ping #{index}: {self._ping_line(removed)}.", ephemeral=True
            )

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
