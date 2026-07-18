"""Intel slash commands: /hostiles /under-attack /help-me /camp /clear /status /cancel (GDD §7).

Every command here is the slash twin of a voice command and calls the SAME
``IncidentEngine.report`` entry point (CLAUDE.md constraint 10) — the cog only
builds ``ParsedCommand`` + a HIGH-tier ``Resolution`` from the typed,
autocompleted system name and renders the outcome back to the pilot.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import discord
import structlog
from discord import app_commands
from discord.ext import commands

from aura.core import db
from aura.dsc.bot import resolve_typed_system
from aura.types import MENTION_INTENTS, Intent, Outcome, ParsedCommand

if TYPE_CHECKING:  # pragma: no cover
    from aura.dsc.bot import AuraBot

__all__ = ["IntelCog", "outcome_text", "system_autocomplete"]

log = structlog.get_logger(__name__)

#: Slash intents that trigger mentions and therefore require @Pilot (GDD §11.1
#: layer 4). RESOLVE/QUERY/CANCEL never mention, so they stay open to everyone.
#: The set itself lives in aura.types so the voice-path gate shares it.
_MENTION_INTENTS = MENTION_INTENTS

_TYPE_BADGES: dict[str, str] = {
    str(Intent.HOSTILE_SPOTTED): "🟠 Hostiles",
    str(Intent.UNDER_ATTACK): "🔴 Under attack",
    str(Intent.ASSIST_REQUEST): "🔴 Assist request",
    str(Intent.GATE_CAMP): "🟠 Gate camp",
    str(Intent.FORMUP): "🔵 Form-up",
}


async def system_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Autocomplete from the gazetteer's pruned active set (GDD §8.1)."""
    bot = cast("AuraBot", interaction.client)
    needle = current.strip().lower()
    prefix: list[str] = []
    contains: list[str] = []
    for entry in bot.gazetteer.systems:
        name = entry.name
        lowered = name.lower()
        if not needle or lowered.startswith(needle):
            prefix.append(name)
        elif needle in lowered:
            contains.append(name)
    names = sorted(prefix) + sorted(contains)
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


def outcome_text(outcome_kind: Outcome, utterance: str | None) -> str:
    if outcome_kind is Outcome.REJECTED:
        return f"❌ {utterance or 'Rejected.'}"
    if outcome_kind is Outcome.FOLDED:
        return f"🔁 {utterance or 'Folded into the existing incident.'}"
    return f"✅ {utterance or 'Posted.'}"


class IntelCog(commands.Cog):
    """Thin adapters over ``IncidentEngine`` — no business logic lives here."""

    def __init__(self, bot: AuraBot) -> None:
        self.bot = bot

    # ── shared report path (constraint 10) ───────────────────────────────────

    async def _report(
        self,
        interaction: discord.Interaction,
        intent: Intent,
        system: str,
        detail: str | None,
        command_name: str,
    ) -> None:
        if interaction.guild_id is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        member = interaction.user
        # Constraint 10 parity with the voice path: silent mode lifts the
        # Pilot gate — with pings off there is no mention to protect, so
        # anyone may post (and no roles need wiring up first).
        if (
            self.bot.holder.current.discord.mentions_enabled
            and intent in _MENTION_INTENTS
            and not self.bot.discipline.may_mention(r.id for r in member.roles)
        ):
            await interaction.response.send_message(
                "Reporting requires the Pilot role (GDD §11.1).", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        resolution = resolve_typed_system(self.bot.gazetteer, system)
        if resolution is None:
            await interaction.followup.send(
                f"Unknown system `{system}` — pick one from the autocomplete "
                "(the gazetteer is scoped to our operational region).",
                ephemeral=True,
            )
            return

        raw = f"/{command_name} {system}" + (f" {detail}" if detail else "")
        parsed = ParsedCommand(
            intent=intent, system_text=system, group_alias=None, detail=detail, raw=raw
        )
        outcome = await self.bot.engine.report(interaction.guild_id, member.id, parsed, resolution)
        await interaction.followup.send(
            outcome_text(outcome.outcome, outcome.utterance), ephemeral=True
        )

    # ── commands ─────────────────────────────────────────────────────────────

    @app_commands.command(name="hostiles", description="Report hostiles in a system")
    @app_commands.describe(system="System name", detail="Free-text note, e.g. 'three battleships'")
    @app_commands.autocomplete(system=system_autocomplete)
    async def hostiles(
        self, interaction: discord.Interaction, system: str, detail: str | None = None
    ) -> None:
        await self._report(interaction, Intent.HOSTILE_SPOTTED, system, detail, "hostiles")

    @app_commands.command(
        name="help-me", description="High-severity assist request — you are in trouble"
    )
    @app_commands.describe(system="System name", detail="Free-text note, e.g. 'tackled on gate'")
    @app_commands.autocomplete(system=system_autocomplete)
    async def help_me(
        self, interaction: discord.Interaction, system: str, detail: str | None = None
    ) -> None:
        await self._report(interaction, Intent.ASSIST_REQUEST, system, detail, "help-me")

    @app_commands.command(
        name="under-attack", description="You are under attack — tackled or taking damage"
    )
    @app_commands.describe(system="System name", detail="Free-text note, e.g. 'pointed on gate'")
    @app_commands.autocomplete(system=system_autocomplete)
    async def under_attack(
        self, interaction: discord.Interaction, system: str, detail: str | None = None
    ) -> None:
        await self._report(interaction, Intent.UNDER_ATTACK, system, detail, "under-attack")

    @app_commands.command(name="camp", description="Report a gate camp")
    @app_commands.describe(system="System name", detail="Free-text note, e.g. 'camping the gate'")
    @app_commands.autocomplete(system=system_autocomplete)
    async def camp(
        self, interaction: discord.Interaction, system: str, detail: str | None = None
    ) -> None:
        await self._report(interaction, Intent.GATE_CAMP, system, detail, "camp")

    @app_commands.command(name="clear", description="Resolve the incidents in a system")
    @app_commands.describe(system="System name")
    @app_commands.autocomplete(system=system_autocomplete)
    async def clear(self, interaction: discord.Interaction, system: str) -> None:
        await self._report(interaction, Intent.RESOLVE, system, None, "clear")

    @app_commands.command(name="status", description="Active incidents summary")
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        parsed = ParsedCommand(
            intent=Intent.QUERY, system_text=None, group_alias=None, detail=None, raw="/status"
        )
        outcome = await self.bot.engine.report(
            interaction.guild_id, interaction.user.id, parsed, None
        )
        embed = await self._status_embed(interaction.guild_id)
        if embed is None:
            await interaction.followup.send(outcome.utterance or "All clear.", ephemeral=True)
        else:
            await interaction.followup.send(
                outcome.utterance or "All clear.", embed=embed, ephemeral=True
            )

    @app_commands.command(name="cancel", description="Retract your last report (30s window)")
    async def cancel(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        parsed = ParsedCommand(
            intent=Intent.CANCEL, system_text=None, group_alias=None, detail=None, raw="/cancel"
        )
        # Shared entry point (constraint 10): report() enforces the
        # incidents.cancel_window_s window and writes the command_log row.
        outcome = await self.bot.engine.report(
            interaction.guild_id, interaction.user.id, parsed, None
        )
        await interaction.followup.send(
            outcome_text(outcome.outcome, outcome.utterance), ephemeral=True
        )

    async def _status_embed(self, guild_id: int) -> discord.Embed | None:
        """Read-only view of the active incident list — rendering, not judgement."""
        rows = await asyncio.to_thread(
            db.query,
            self.bot.conn,
            "SELECT id, system_id, type, detail, opened_at FROM incidents"
            " WHERE guild_id = ? AND status = 'ACTIVE' ORDER BY updated_at DESC LIMIT 10",
            (guild_id,),
        )
        if not rows:
            return None
        lines: list[str] = []
        for row in rows:
            entry = (
                self.bot.gazetteer.by_id(row["system_id"]) if row["system_id"] is not None else None
            )
            system = entry.name if entry is not None else "unknown"
            badge = _TYPE_BADGES.get(row["type"], row["type"])
            opened = int(datetime.fromisoformat(row["opened_at"]).timestamp())
            detail = f" — {row['detail']}" if row["detail"] else ""
            lines.append(f"{badge} **{system}** (<t:{opened}:R>){detail}")
        return discord.Embed(
            title="Active incidents",
            description="\n".join(lines),
            color=0x3498DB,
            timestamp=datetime.now(UTC),
        )
