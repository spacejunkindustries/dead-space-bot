"""Intel slash commands: /hostiles /under-attack /help-me /camp /clear /relay
/status /cancel (GDD §7).

Every command here is the slash twin of a voice command and calls the SAME
``IncidentEngine`` entry point (CLAUDE.md constraint 10) — the cog only builds
``ParsedCommand`` + a HIGH-tier ``Resolution`` from the typed, autocompleted
system name and renders the outcome back to the pilot. The optional ``code:``
and ``audience:`` parameters map onto exactly the parsed fields the voice
path produces (spoken colour codes → ``ParsedCommand.severity``, "miners
only"/"all hands" → ``ParsedCommand.group_alias``), so severity and group
targeting survive a voice outage too.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

import discord
import structlog
from discord import app_commands
from discord.ext import commands

from cortana.core import areas, db
from cortana.dsc.bot import resolve_typed_system
from cortana.nlu import grammar
from cortana.types import (
    MENTION_INTENTS,
    Intent,
    Outcome,
    ParsedCommand,
    Resolution,
    Severity,
    Tier,
)

if TYPE_CHECKING:  # pragma: no cover
    from cortana.dsc.bot import AuraBot

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

#: Typed twins of the spoken threat colours (GDD §6.4) and group aliases
#: (GDD §6.2) — mapped onto the exact ParsedCommand fields the voice grammar
#: fills, so both paths hand the engine identical inputs (constraint 10).
_ThreatCode = Literal["red", "orange", "yellow"]
_Audience = Literal["miners", "defense", "all-hands"]

_CODE_SEVERITY: dict[str, Severity] = {
    "red": Severity.HIGH,
    "orange": Severity.MEDIUM,
    "yellow": Severity.NONE,
}

_AUDIENCE_ALIAS: dict[str, str] = {
    "miners": "miners",
    "defense": "defense",
    "all-hands": "all_hands",
}


async def system_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Autocomplete over the FULL seeded k-space map (GDD §8.1).

    Typed reports may name any real system, not just the scoped active set —
    autocompleting only the active set is what made a manual report offer
    ~8 systems (field report). The scoped set is surfaced FIRST (home region
    is what pilots usually want), then the rest of k-space fills in.
    """
    bot = cast("AuraBot", interaction.client)
    needle = current.strip().lower()
    scoped_ids = {e.id for e in bot.gazetteer.systems}
    scoped_prefix: list[str] = []
    scoped_contains: list[str] = []
    other_prefix: list[str] = []
    other_contains: list[str] = []
    for entry in bot.gazetteer.all_systems:
        lowered = entry.name.lower()
        in_scope = entry.id in scoped_ids
        if not needle or lowered.startswith(needle):
            (scoped_prefix if in_scope else other_prefix).append(entry.name)
        elif needle in lowered:
            (scoped_contains if in_scope else other_contains).append(entry.name)
    # Home-region matches first, then the rest of the map — each block sorted.
    names = (
        sorted(scoped_prefix)
        + sorted(scoped_contains)
        + sorted(other_prefix)
        + sorted(other_contains)
    )
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
        *,
        code: _ThreatCode | None = None,
        audience: _Audience | None = None,
    ) -> None:
        if interaction.guild_id is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        member = interaction.user
        may_mention = self.bot.discipline.may_mention(r.id for r in member.roles)
        # Constraint 10 parity with the voice path: silent mode lifts the
        # Pilot gate — with pings off there is no mention to protect, so
        # anyone may post (and no roles need wiring up first).
        if (
            self.bot.holder.current.discord.mentions_enabled
            and intent in _MENTION_INTENTS
            and not may_mention
        ):
            await interaction.response.send_message(
                "Reporting requires the Pilot role (GDD §11.1).", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        resolution = resolve_typed_system(self.bot.gazetteer, system)
        if resolution is None:
            # Constraint 10 parity: a learned custom area (GDD §8.5a) resolves
            # for a typed report exactly as it does by voice — posts verbatim.
            # Key on the grammar-cleaned place so a typed "the branch" matches a
            # voice-learned "branch" (the voice path stores the stripped form).
            area = await asyncio.to_thread(
                areas.lookup_area,
                self.bot.conn,
                interaction.guild_id,
                grammar.clean_place(system),
            )
            if area is not None:
                resolution = Resolution(tier=Tier.HIGH, candidates=(), area_name=area)
            else:
                await interaction.followup.send(
                    f"Unknown system `{system}` — pick one from the autocomplete "
                    "(the gazetteer is scoped to our operational region).",
                    ephemeral=True,
                )
                return

        raw = f"/{command_name} {system}" + (f" {detail}" if detail else "")
        if code:
            raw += f" code:{code}"
        if audience:
            raw += f" audience:{audience}"
        parsed = ParsedCommand(
            intent=intent,
            system_text=system,
            group_alias=_AUDIENCE_ALIAS[audience] if audience else None,
            detail=detail,
            raw=raw,
            severity=_CODE_SEVERITY[code] if code else None,
        )
        outcome = await self.bot.engine.report(
            interaction.guild_id, member.id, parsed, resolution, caller_may_mention=may_mention
        )
        await interaction.followup.send(
            outcome_text(outcome.outcome, outcome.utterance), ephemeral=True
        )

    # ── commands ─────────────────────────────────────────────────────────────

    @app_commands.command(name="hostiles", description="Report hostiles in a system")
    @app_commands.describe(
        system="System name",
        detail="Free-text note, e.g. 'three battleships'",
        code="Threat colour override — twin of the spoken 'code red/orange/yellow'",
        audience="Who to ping — twin of 'miners only' / 'defense only' / 'all hands'",
    )
    @app_commands.autocomplete(system=system_autocomplete)
    async def hostiles(
        self,
        interaction: discord.Interaction,
        system: str,
        detail: str | None = None,
        code: _ThreatCode | None = None,
        audience: _Audience | None = None,
    ) -> None:
        await self._report(
            interaction,
            Intent.HOSTILE_SPOTTED,
            system,
            detail,
            "hostiles",
            code=code,
            audience=audience,
        )

    @app_commands.command(
        name="help-me", description="High-severity assist request — you are in trouble"
    )
    @app_commands.describe(
        system="System name",
        detail="Free-text note, e.g. 'tackled on gate'",
        code="Threat colour override — twin of the spoken 'code red/orange/yellow'",
        audience="Who to ping — twin of 'miners only' / 'defense only' / 'all hands'",
    )
    @app_commands.autocomplete(system=system_autocomplete)
    async def help_me(
        self,
        interaction: discord.Interaction,
        system: str,
        detail: str | None = None,
        code: _ThreatCode | None = None,
        audience: _Audience | None = None,
    ) -> None:
        await self._report(
            interaction,
            Intent.ASSIST_REQUEST,
            system,
            detail,
            "help-me",
            code=code,
            audience=audience,
        )

    @app_commands.command(
        name="under-attack", description="You are under attack — tackled or taking damage"
    )
    @app_commands.describe(
        system="System name",
        detail="Free-text note, e.g. 'pointed on gate'",
        code="Threat colour override — twin of the spoken 'code red/orange/yellow'",
        audience="Who to ping — twin of 'miners only' / 'defense only' / 'all hands'",
    )
    @app_commands.autocomplete(system=system_autocomplete)
    async def under_attack(
        self,
        interaction: discord.Interaction,
        system: str,
        detail: str | None = None,
        code: _ThreatCode | None = None,
        audience: _Audience | None = None,
    ) -> None:
        await self._report(
            interaction,
            Intent.UNDER_ATTACK,
            system,
            detail,
            "under-attack",
            code=code,
            audience=audience,
        )

    @app_commands.command(name="camp", description="Report a gate camp")
    @app_commands.describe(
        system="System name",
        detail="Free-text note, e.g. 'camping the gate'",
        code="Threat colour override — twin of the spoken 'code red/orange/yellow'",
        audience="Who to ping — twin of 'miners only' / 'defense only' / 'all hands'",
    )
    @app_commands.autocomplete(system=system_autocomplete)
    async def camp(
        self,
        interaction: discord.Interaction,
        system: str,
        detail: str | None = None,
        code: _ThreatCode | None = None,
        audience: _Audience | None = None,
    ) -> None:
        await self._report(
            interaction, Intent.GATE_CAMP, system, detail, "camp", code=code, audience=audience
        )

    @app_commands.command(
        name="relay",
        description="Relay freeform intel verbatim — twin of the voice relay (GDD §8.6)",
    )
    @app_commands.describe(
        message="Posted onto an intel-relay card exactly as typed",
        code="Threat colour for the relay card — twin of the spoken colour code",
        audience="Who to ping (requires the Pilot role); relays never @here",
    )
    async def relay(
        self,
        interaction: discord.Interaction,
        message: str,
        code: _ThreatCode | None = None,
        audience: _Audience | None = None,
    ) -> None:
        # Slash twin of the freeform voice relay (constraint 10): the exact
        # engine.broadcast path, with the same decide_mentions authority —
        # a relay can never @here, and mentions require the Pilot role
        # (threaded through as caller_may_mention, exactly like the voice
        # path gates its all-hands flag; the relay itself posts regardless).
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        member = interaction.user
        roles = getattr(member, "roles", ())
        await interaction.response.defer(ephemeral=True, thinking=True)
        outcome = await self.bot.engine.broadcast(
            interaction.guild_id,
            member.id,
            message,
            severity=_CODE_SEVERITY[code] if code else None,
            group_alias=_AUDIENCE_ALIAS[audience] if audience else None,
            caller_may_mention=self.bot.discipline.may_mention(r.id for r in roles),
        )
        await interaction.followup.send(
            outcome_text(outcome.outcome, outcome.utterance), ephemeral=True
        )

    @app_commands.command(name="clear", description="Resolve the incidents in a system")
    @app_commands.describe(system="System name")
    @app_commands.autocomplete(system=system_autocomplete)
    async def clear(self, interaction: discord.Interaction, system: str) -> None:
        await self._report(interaction, Intent.RESOLVE, system, None, "clear")

    @app_commands.command(
        name="chase", description="Chase mode: retarget your live incident as the target moves"
    )
    @app_commands.describe(system="System the target moved to (any name — no exact match needed)")
    @app_commands.autocomplete(system=system_autocomplete)
    async def chase(self, interaction: discord.Interaction, system: str) -> None:
        # Voice twin: "update chase <system>" (constraint 10). Deliberately
        # NOT routed through _report: chase is flexible — an unknown or
        # misheard system rides the card verbatim instead of rejecting.
        if interaction.guild_id is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        resolution = resolve_typed_system(self.bot.gazetteer, system)  # None is fine here
        parsed = ParsedCommand(
            intent=Intent.CHASE_UPDATE,
            system_text=system,
            group_alias=None,
            detail=None,
            raw=f"/chase {system}",
        )
        outcome = await self.bot.engine.report(
            interaction.guild_id, interaction.user.id, parsed, resolution
        )
        await interaction.followup.send(
            outcome_text(outcome.outcome, outcome.utterance), ephemeral=True
        )

    @app_commands.command(
        name="standdown",
        description="Stand down: resolve active incident cards — twin of the voice 'stand down'",
    )
    @app_commands.describe(
        scope="all = clear every active card (default); last = only the most recent"
    )
    async def standdown(
        self,
        interaction: discord.Interaction,
        scope: Literal["all", "last"] = "all",
    ) -> None:
        # Slash twin of the voice "stand down" / "clear all" / "cancel last
        # incident" (constraint 10): the same engine.report → stand_down path,
        # so both surfaces resolve cards identically and write one command_log
        # row. RESOLVE-class verb — never mentions, so it stays open to everyone
        # (like /clear and /cancel).
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        parsed = ParsedCommand(
            intent=Intent.STAND_DOWN,
            system_text=None,
            group_alias=None,
            detail=scope,
            raw=f"/standdown {scope}",
        )
        outcome = await self.bot.engine.report(
            interaction.guild_id, interaction.user.id, parsed, None
        )
        await interaction.followup.send(
            outcome_text(outcome.outcome, outcome.utterance), ephemeral=True
        )

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
