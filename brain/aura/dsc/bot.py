"""AuraBot: the Discord client, Poster implementation, and token loading.

GDD §7 / §9.1 / §11.2 / §17.4 / §19. This module is the only place discord.py
meets the rest of the system:

- **Intents** (GDD §17.4): guilds, members (role gating), voice_states
  (census for auto-join). ``message_content`` stays off — AURA reads no chat.
- **Token** (constraint 12): ``$CREDENTIALS_DIRECTORY/token`` (systemd
  ``LoadCredential=``) first, ``discord.token_file`` as the dev fallback.
  Never an env var value, never a config value, never logged.
- **Poster** (GDD §9.1): posts and edits incident cards. One incident is one
  message, edited in place, forever (constraint 9) — ``post`` is called once
  per incident by the engine and everything after is ``edit``.
- **Voice census** (GDD §19): ``on_voice_state_update`` forwards the unmuted
  human count of watched channels to the voice gateway, which owns join/leave
  judgement and the §19 consent announcement — posted verbatim on every join.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord
import structlog
from discord.ext import commands

from aura.config import ConfigHolder, DiscordConfig
from aura.core.discipline import Discipline
from aura.core.incidents import IncidentEngine
from aura.types import AlertChannel, CardRender, MatchCandidate, Resolution, Tier
from aura.voice_gateway import ANNOUNCEMENT

if TYPE_CHECKING:  # pragma: no cover — wiring types only
    from aura.health import HealthReporter
    from aura.nlu.gazetteer import Gazetteer
    from aura.tts import Speaker
    from aura.voice_gateway import VoiceGateway

__all__ = ["AuraBot", "TokenError", "read_token", "resolve_typed_system"]

log = structlog.get_logger(__name__)

#: The cogs of GDD §7, loaded in setup_hook.
_COG_MODULES = (
    "aura.dsc.cogs.intel",
    "aura.dsc.cogs.subs",
    "aura.dsc.cogs.ops",
    "aura.dsc.cogs.utility",
    "aura.dsc.cogs.admin",
    "aura.dsc.cogs.help",
)


class TokenError(Exception):
    """No readable Discord token was found (LoadCredential or dev fallback)."""


def read_token(cfg: DiscordConfig) -> str:
    """Read the bot token — constraint 12.

    ``$CREDENTIALS_DIRECTORY/token`` (systemd ``LoadCredential=``) wins;
    ``cfg.token_file`` is the development fallback. The token value is never
    logged and never passes through config or environment values.
    """
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    candidates: list[Path] = []
    if cred_dir:
        candidates.append(Path(cred_dir) / "token")
    if cfg.token_file:
        candidates.append(Path(cfg.token_file))
    for path in candidates:
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if token:
            log.info("token_loaded", source=str(path))
            return token
    raise TokenError(
        "no Discord token found: set LoadCredential=token:… in the unit file "
        "or point discord.token_file at a readable file for development"
    )


def resolve_typed_system(gazetteer: Gazetteer, name: str) -> Resolution | None:
    """HIGH-tier resolution for a *typed* system name (slash path, GDD §7).

    Slash input is autocompleted from the gazetteer, so an exact
    (case-insensitive) match resolves at full confidence; anything else is
    the caller's cue to reject with a helpful message — the phonetic matcher
    is for speech, not typos.
    """
    entry = gazetteer.by_name(name.strip())
    if entry is None:
        return None
    return Resolution(
        tier=Tier.HIGH,
        candidates=(MatchCandidate(system_id=entry.id, name=entry.name, score=1.0),),
    )


class AuraBot(commands.Bot):
    """discord.py client + Poster. Cogs stay thin; judgement lives in the engine."""

    def __init__(
        self,
        holder: ConfigHolder,
        engine: IncidentEngine,
        gazetteer: Gazetteer,
        discipline: Discipline,
        speaker: Speaker,
        conn: sqlite3.Connection,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True  # role gating (GDD §17.4)
        intents.voice_states = True  # census + auto-join (GDD §17.4)
        # No message_content: AURA never reads chat, and the prefix path is
        # inert — every command is a slash command on the app-command tree.
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.holder = holder
        self.engine = engine
        self.gazetteer = gazetteer
        self.discipline = discipline
        self.speaker = speaker
        self.conn = conn
        # Wired by __main__ after construction (VoiceGateway needs this bot's
        # announce helper, so it cannot exist before the bot does).
        self.voice_gateway: VoiceGateway | None = None
        self.health_reporter: HealthReporter | None = None

    # ── startup ──────────────────────────────────────────────────────────────

    async def setup_hook(self) -> None:
        """Load cogs, register restart-proof component handlers, sync commands."""
        # Imported here, not at module top: the cogs import this module.
        from aura.dsc import views
        from aura.dsc.cogs.admin import AdminCog
        from aura.dsc.cogs.help import HelpCog, HelpTopicSelect
        from aura.dsc.cogs.intel import IntelCog
        from aura.dsc.cogs.ops import OpsCog
        from aura.dsc.cogs.subs import SubsCog
        from aura.dsc.cogs.utility import PollVoteButton, UtilityCog

        self.add_dynamic_items(
            views.IncidentButton, views.SubscriptionButton, PollVoteButton, HelpTopicSelect
        )

        for cog in (
            IntelCog(self),
            SubsCog(self),
            OpsCog(self),
            UtilityCog(self),
            AdminCog(self),
            HelpCog(self),
        ):
            await self.add_cog(cog)

        guild = discord.Object(id=self.holder.current.discord.guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info(
            "app_commands_synced",
            guild_id=guild.id,
            count=len(synced),
            cogs=[m.rsplit(".", 1)[-1] for m in _COG_MODULES],
        )

    async def on_ready(self) -> None:
        log.info(
            "bot_ready",
            user=str(self.user),
            guilds=len(self.guilds),
            fleetmode=self.discipline.fleetmode,
        )
        # Routing rules name roles ("@Home-Defense"); resolving them needs the
        # guild role cache, which only exists now. Until this succeeds the
        # engine runs with zero rules — cards post, nobody gets mentioned.
        await self._load_routing_rules()
        # Pilots may already be in voice when Brain (re)starts — no voice
        # event will fire for them, so seed the census once.
        await self._seed_voice_census()

    async def _load_routing_rules(self) -> None:
        """Load routing.yaml through the engine (same path as /routing reload)."""
        import asyncio

        from aura.core.routing import RoutingConfigError

        guild = self.get_guild(self.holder.current.discord.guild_id)
        if guild is None:
            log.error(
                "routing_rules_not_loaded_guild_missing",
                guild_id=self.holder.current.discord.guild_id,
            )
            return
        roles_by_name = {r.name: r.id for r in guild.roles}

        def resolve_role(name: str) -> int | None:
            return roles_by_name.get(name.lstrip("@"))

        try:
            count = await asyncio.to_thread(self.engine.load_routing_rules, resolve_role)
        except RoutingConfigError as exc:
            log.error("routing_rules_rejected", error=str(exc))
            return
        log.info("routing_rules_loaded", count=count)

    # ── voice census → gateway (GDD §19 / voice_gateway) ─────────────────────

    @staticmethod
    def _human_census(channel: discord.abc.Connectable) -> int:
        """Unmuted, non-bot member count of a voice channel."""
        members = getattr(channel, "members", [])
        count = 0
        for member in members:
            voice = member.voice
            if member.bot or voice is None:
                continue
            if not (voice.self_mute or voice.mute):
                count += 1
        return count

    async def _seed_voice_census(self) -> None:
        if self.voice_gateway is None:
            return
        for channel_id in self.holder.current.discord.watch_voice_channels:
            channel = self.get_channel(channel_id)
            if channel is None:
                continue
            await self.voice_gateway.on_voice_update(channel_id, self._human_census(channel))

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Forward watched-channel census changes to the voice gateway.

        Ears owns the actual voice connection; this is purely the census feed
        the gateway's join/leave judgement runs on.
        """
        if self.voice_gateway is None:
            return
        watched = set(self.holder.current.discord.watch_voice_channels)
        touched: dict[int, discord.abc.Connectable] = {}
        for state in (before, after):
            channel = state.channel
            if channel is not None and channel.id in watched:
                touched[channel.id] = channel
        for channel_id, channel in touched.items():
            await self.voice_gateway.on_voice_update(channel_id, self._human_census(channel))

    # ── §19 consent announcement — every join, verbatim ──────────────────────

    async def announce_join(self, channel_id: int) -> None:
        """Post the §19 announcement into the joined voice channel's chat.

        Passed to the VoiceGateway as its ``announce_fn``; the gateway calls
        it on every single join. Falls back to #intel-live if the voice
        channel's text chat cannot be posted to.
        """
        channel = self.get_channel(channel_id)
        if isinstance(channel, discord.VoiceChannel):
            try:
                await channel.send(ANNOUNCEMENT)
                return
            except discord.HTTPException:
                log.warning("join_announcement_voice_chat_failed", channel_id=channel_id)
        fallback = await self._alert_channel(AlertChannel.LIVE)
        await fallback.send(ANNOUNCEMENT)

    # ── Poster (GDD §9.1 — the card is a view, not a log) ────────────────────

    async def _alert_channel(self, channel: AlertChannel) -> discord.TextChannel:
        channels = self.holder.current.discord.channels
        channel_id = (
            channels.intel_alerts if channel is AlertChannel.ALERTS else channels.intel_live
        )
        return await self._messageable(channel_id)

    async def _messageable(self, channel_id: int) -> discord.TextChannel:
        found = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if not isinstance(found, discord.TextChannel):
            raise TypeError(f"channel {channel_id} is not a text channel")
        return found

    async def post(
        self, guild_id: int, channel: AlertChannel, content: str, card: CardRender
    ) -> tuple[int, int]:
        """Post an incident card; returns ``(channel_id, message_id)``.

        ``content`` carries the mentions the routing/discipline stack already
        approved — ``@here`` can only reach this point for UNDER_ATTACK /
        ASSIST_REQUEST (constraint 11 is enforced upstream in routing).
        """
        from aura.dsc.views import view_from_card

        target = await self._alert_channel(channel)
        message = await target.send(
            content=content or None,
            embed=discord.Embed.from_dict(card.embed),
            view=view_from_card(card),
            allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=False),
        )
        return target.id, message.id

    async def edit(self, channel_id: int, message_id: int, content: str, card: CardRender) -> None:
        """Edit the card in place — the only mutation an incident ever gets."""
        from aura.dsc.views import view_from_card

        target = await self._messageable(channel_id)
        kwargs: dict[str, Any] = {
            "embed": discord.Embed.from_dict(card.embed),
            "view": view_from_card(card),  # None strips buttons off resolved cards
        }
        if content:
            kwargs["content"] = content
        try:
            await target.get_partial_message(message_id).edit(**kwargs)
        except discord.NotFound:
            log.warning("card_message_deleted", channel_id=channel_id, message_id=message_id)

    # ── routing-role helpers for the subscription surface (GDD §10.2) ────────

    def routing_role_ids(self) -> tuple[int, ...]:
        """Role ids of the loaded routing rules — the subscribable set.

        The single sanctioned read of the engine's private rule list; the
        engine owns rule loading and this stays a view of it.
        """
        rules = getattr(self.engine, "_rules", [])
        seen: list[int] = []
        for rule in rules:
            if rule.role_id not in seen:
                seen.append(rule.role_id)
        return tuple(seen)

    def subscription_role_pairs(self, guild: discord.Guild) -> list[tuple[int, str]]:
        """(role_id, name) pairs for routing roles that exist on the guild."""
        pairs: list[tuple[int, str]] = []
        for role_id in self.routing_role_ids():
            role = guild.get_role(role_id)
            if role is not None:
                pairs.append((role.id, role.name))
        return pairs
