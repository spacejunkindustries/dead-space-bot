"""AuraBot: the Discord client, Poster implementation, and token loading.

GDD §7 / §9.1 / §11.2 / §17.4 / §19. This module is the only place discord.py
meets the rest of the system:

- **Intents** (GDD §17.4): guilds, members (role gating), voice_states
  (census for auto-join). ``message_content`` stays off — CORTANA reads no chat.
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

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord
import structlog
from discord import app_commands
from discord.ext import commands

from cortana.alarms import AlarmBus, AlarmCode, AlarmSeverity
from cortana.config import ConfigHolder, DiscordConfig
from cortana.core import db
from cortana.core.discipline import Discipline
from cortana.core.incidents import IncidentEngine
from cortana.core.routing import MentionDecision
from cortana.types import (
    AlertChannel,
    CardRender,
    EditNotFound,
    MatchCandidate,
    PostError,
    Resolution,
    Tier,
)
from cortana.voice_gateway import ANNOUNCEMENT

if TYPE_CHECKING:  # pragma: no cover — wiring types only
    from cortana.health import HealthReporter
    from cortana.nlu.gazetteer import Gazetteer
    from cortana.reload import ReloadResult
    from cortana.tts import Speaker
    from cortana.voice_gateway import VoiceGateway

__all__ = [
    "AuraBot",
    "TokenError",
    "read_token",
    "resolve_typed_system",
    "sync_command_tree",
    "tree_payload_hash",
]

log = structlog.get_logger(__name__)

#: The cogs of GDD §7, loaded in setup_hook.
_COG_MODULES = (
    "cortana.dsc.cogs.intel",
    "cortana.dsc.cogs.subs",
    "cortana.dsc.cogs.ops",
    "cortana.dsc.cogs.utility",
    "cortana.dsc.cogs.fun",
    "cortana.dsc.cogs.admin",
    "cortana.dsc.cogs.help",
    "cortana.dsc.cogs.status",
)

#: app_state key for the last successfully synced command-tree payload hash.
_TREE_HASH_KEY = "tree_payload_hash"


def tree_payload_hash(payloads: Sequence[Mapping[str, Any]]) -> str:
    """Stable digest of the app-command payloads about to be synced.

    Order-insensitive (commands sorted by name) so cog load order can never
    force a re-sync; any actual payload change (new command, changed option,
    changed description) changes the digest."""
    canonical = json.dumps(
        sorted(payloads, key=lambda p: str(p.get("name", ""))),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def sync_command_tree(
    conn: sqlite3.Connection,
    tree: Any,
    guild: discord.abc.Snowflake,
    alarms: AlarmBus | None,
) -> str:
    """Payload-hash-gated ``tree.sync`` — the startup-critical-path fix.

    ``tree.sync`` used to run unguarded inside ``setup_hook`` on EVERY start:
    a 429/403/5xx killed the whole process, and systemd's restart re-hit the
    rate-limited endpoint in a loop. Now: skip entirely when the payload hash
    (persisted in ``app_state``) is unchanged; on failure raise the
    TREE_SYNC_STALE alarm and keep serving the previously synced commands —
    never kill startup. Returns ``"synced" | "skipped" | "failed"``.
    """
    payloads = [command.to_dict(tree) for command in tree.get_commands(guild=guild)]
    # The digest is guild-scoped: a guild_id change (or kick/re-invite, which
    # deletes guild commands server-side) must never be masked by a hash
    # computed for a different guild (review finding).
    digest = f"{guild.id}:{tree_payload_hash(payloads)}"
    stored = await asyncio.to_thread(
        db.query_value, conn, "SELECT value FROM app_state WHERE key = ?", (_TREE_HASH_KEY,)
    )
    if stored == digest:
        log.info("app_commands_sync_skipped", guild_id=guild.id, count=len(payloads))
        if alarms is not None:
            await alarms.clear(AlarmCode.TREE_SYNC_STALE)
        return "skipped"
    try:
        synced = await tree.sync(guild=guild)
    except discord.HTTPException as exc:
        log.error("app_commands_sync_failed", guild_id=guild.id, error=str(exc))
        if alarms is not None:
            await alarms.raise_alarm(
                AlarmCode.TREE_SYNC_STALE,
                AlarmSeverity.WARNING,
                f"Slash-command sync failed: {exc}. The previously synced "
                "commands keep serving — new/changed commands are not visible.",
                "retried on the next restart; check discord.guild_id and the "
                "bot's `applications.commands` scope if this persists",
            )
        return "failed"
    await asyncio.to_thread(
        db.execute,
        conn,
        "INSERT INTO app_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_TREE_HASH_KEY, digest),
    )
    if alarms is not None:
        await alarms.clear(AlarmCode.TREE_SYNC_STALE)
    log.info("app_commands_synced", guild_id=guild.id, count=len(synced))
    return "synced"


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
    is for speech, not typos. Resolves over the FULL seeded map (by_name_any),
    not just the scoped set: a typed report may name any real k-space system
    (GDD §8.1), and the autocomplete now offers all of them.
    """
    entry = gazetteer.by_name_any(name.strip())
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
        # No message_content: CORTANA never reads chat, and the prefix path is
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
        #: Dialog-engine cleanup hook (GDD §5.4) — set by the composition root.
        self.on_user_left_voice: Callable[[int], None] | None = None
        #: The fact library / insult maker (GDD §13.2) — set by the
        #: composition root; the FunCog reads it (same engine as voice).
        self.fun: Any | None = None
        # The §6.6 out-of-band assistant; None = override channel disabled.
        self.chat: Any | None = None
        self.chat_status: str = "disabled"  # "ready" | "no_key" | "disabled" (§6.6)
        #: The operator alarm surface (GDD §11.3) — set by the composition root.
        self.alarms: AlarmBus | None = None
        #: The one reload transaction (SIGHUP == /reload) — set by __main__.
        self.request_reload: Callable[[], Awaitable[ReloadResult]] | None = None
        #: Graceful process restart (/restart → shutdown → systemd
        #: Restart=always) — set by __main__.
        self.request_restart: Callable[[], Awaitable[None]] | None = None
        #: Ears IPC liveness probe: ``() -> (alive, heartbeat_age_s | None)``.
        self.ipc_status: Callable[[], tuple[bool, float | None]] | None = None
        #: Voice dialog sessions currently in flight (``/botstatus``).
        self.dialog_sessions: Callable[[], int] | None = None
        self.started_at_monotonic = time.monotonic()
        self._tree_sync_task: asyncio.Task[None] | None = None
        # One interaction error boundary (GDD §11.3): every slash-command
        # failure answers the pilot and counts into the AlarmBus instead of
        # dying as a silent eternal spinner.
        self.tree.error(self._on_app_command_error)

    # ── startup ──────────────────────────────────────────────────────────────

    async def setup_hook(self) -> None:
        """Load cogs, register restart-proof component handlers, sync commands."""
        # Imported here, not at module top: the cogs import this module.
        from cortana.dsc import views
        from cortana.dsc.cogs.admin import AdminCog
        from cortana.dsc.cogs.fun import FunCog
        from cortana.dsc.cogs.help import HelpCog, HelpTopicSelect
        from cortana.dsc.cogs.intel import IntelCog
        from cortana.dsc.cogs.ops import OpsCog
        from cortana.dsc.cogs.status import StatusCog
        from cortana.dsc.cogs.subs import SubsCog
        from cortana.dsc.cogs.utility import PollVoteButton, UtilityCog

        self.add_dynamic_items(
            views.IncidentButton, views.SubscriptionButton, PollVoteButton, HelpTopicSelect
        )

        for cog in (
            IntelCog(self),
            SubsCog(self),
            OpsCog(self),
            UtilityCog(self),
            FunCog(self),
            AdminCog(self),
            HelpCog(self),
            StatusCog(self),
        ):
            await self.add_cog(cog)

        # tree.sync is OUT of the startup critical path: it is hash-gated,
        # background, and failure-tolerant (TREE_SYNC_STALE alarm) — a 429 or
        # a wrong guild_id can no longer crash-loop the whole Brain against a
        # rate-limited endpoint.
        guild = discord.Object(id=self.holder.current.discord.guild_id)
        self.tree.copy_global_to(guild=guild)
        self._tree_sync_task = asyncio.create_task(
            self._background_tree_sync(guild), name="tree-sync"
        )
        log.info("cogs_loaded", cogs=[m.rsplit(".", 1)[-1] for m in _COG_MODULES])

    async def _background_tree_sync(self, guild: discord.abc.Snowflake) -> None:
        try:
            await sync_command_tree(self.conn, self.tree, guild, self.alarms)
        except Exception:  # noqa: BLE001 — a sync failure must never kill startup
            log.exception("app_commands_sync_crashed")

    # ── the interaction error boundary (GDD §11.3) ───────────────────────────

    async def _on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Every unhandled slash-command error lands here: answer the pilot
        (no eternal 'thinking…' spinner mid-fight) and count it into the
        AlarmBus so repeats surface in #bot-health."""
        if isinstance(error, app_commands.CheckFailure):
            # Gate refusals are user-facing, not faults. The owning cog's
            # cog_app_command_error usually answered already — a second
            # tree-level reply doubled every refusal (review finding).
            if interaction.response.is_done():
                return
            from cortana.dsc.views import answer_interaction_error

            await answer_interaction_error(
                interaction, "Not allowed — this command is gated.", ephemeral=True
            )
            return
        command = interaction.command
        name = command.qualified_name if command is not None else "unknown"
        original = getattr(error, "original", error)
        log.error("app_command_failed", command=name, error=repr(original))
        from cortana.dsc.views import answer_interaction_error

        await answer_interaction_error(interaction)
        if self.alarms is not None:
            await self.alarms.record_interaction_error(name)

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
        """Load routing.yaml through the engine (same path as /routing reload
        and /reload). Every silent-degradation branch — guild missing, file
        rejected, zero rules, unresolved role names — raises its alarm so a
        phone admin sees "nobody will get pinged" before pilots do."""
        from cortana.core.routing import RoutingConfigError

        guild = self.get_guild(self.holder.current.discord.guild_id)
        if guild is None:
            log.error(
                "routing_rules_not_loaded_guild_missing",
                guild_id=self.holder.current.discord.guild_id,
            )
            await self._raise_alarm(
                AlarmCode.ROUTING_ZERO_RULES,
                AlarmSeverity.CRITICAL,
                "Routing rules not loaded — the configured guild is not in the "
                "bot's cache. Cards post but NOBODY gets mentioned.",
                "check `discord.guild_id` and that the bot is in that guild",
            )
            return
        roles_by_name = {r.name: r.id for r in guild.roles}
        unresolved: set[str] = set()

        def resolve_role(name: str) -> int | None:
            role_id = roles_by_name.get(name.lstrip("@"))
            if role_id is None:
                unresolved.add(name)
            return role_id

        try:
            count = await asyncio.to_thread(self.engine.load_routing_rules, resolve_role)
        except RoutingConfigError as exc:
            log.error("routing_rules_rejected", error=str(exc))
            await self._raise_alarm(
                AlarmCode.ROUTING_ZERO_RULES,
                AlarmSeverity.CRITICAL,
                f"routing.yaml rejected: {exc}. The previous rules stay in "
                "force (zero on a fresh start).",
                "fix routing.yaml, then `/routing reload` (or `/reload`)",
            )
            return
        log.info("routing_rules_loaded", count=count)
        if unresolved:
            await self._raise_alarm(
                AlarmCode.ROLE_UNRESOLVED,
                AlarmSeverity.WARNING,
                "routing.yaml names roles that don't exist in the guild — "
                "their rules are SKIPPED: " + ", ".join(sorted(unresolved)),
                "create the roles or fix the names, then `/routing reload`",
            )
        else:
            await self._clear_alarm(AlarmCode.ROLE_UNRESOLVED)
        if count == 0:
            await self._raise_alarm(
                AlarmCode.ROUTING_ZERO_RULES,
                AlarmSeverity.CRITICAL,
                "ZERO routing rules are active — cards post but nobody gets mentioned.",
                "write routing.yaml (or fix its role names), then `/routing reload`",
            )
        else:
            await self._clear_alarm(AlarmCode.ROUTING_ZERO_RULES)

    async def _raise_alarm(
        self, code: AlarmCode, severity: AlarmSeverity, summary: str, fix_hint: str
    ) -> None:
        if self.alarms is not None:
            await self.alarms.raise_alarm(code, severity, summary, fix_hint)

    async def _clear_alarm(self, code: AlarmCode) -> None:
        if self.alarms is not None:
            await self.alarms.clear(code)

    # ── voice census → gateway (GDD §19 / voice_gateway) ─────────────────────

    @staticmethod
    def _human_census(channel: discord.abc.Connectable) -> tuple[int, int]:
        """Census of a voice channel as ``(present, unmuted)``.

        ``present`` counts every non-bot member physically in the channel,
        regardless of mute state — this drives auto-join/leave, because a pilot
        sitting muted until they need to shout a report is still present and
        CORTANA must stay with them (GDD §1.2, the lonely ratter).

        ``unmuted`` additionally excludes self/server-muted members — this
        feeds the §20 "voice receive is dead" alarm, which must not fire when
        the channel is simply quiet because everyone is muted.
        """
        members = getattr(channel, "members", [])
        present = 0
        unmuted = 0
        for member in members:
            if member.bot:
                continue
            present += 1
            voice = member.voice
            if voice is not None and not (voice.self_mute or voice.mute):
                unmuted += 1
        return present, unmuted

    async def _seed_voice_census(self) -> None:
        if self.voice_gateway is None:
            return
        for channel_id in self.holder.current.discord.watch_voice_channels:
            channel = self.get_channel(channel_id)
            if channel is None:
                continue
            present, unmuted = self._human_census(channel)
            await self.voice_gateway.on_voice_update(channel_id, present, unmuted)

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
        # Authoritative dialog cleanup (GDD §5.4): a pilot leaving/moving out
        # of a watched channel purges their session + armed windows even when
        # Ears is down and its IPC "left" event can never arrive.
        if self.on_user_left_voice is not None:
            left_watched = (
                before.channel is not None
                and before.channel.id in self.holder.current.discord.watch_voice_channels
                and (after.channel is None or after.channel.id != before.channel.id)
            )
            if left_watched:
                self.on_user_left_voice(member.id)
        if self.voice_gateway is None:
            return
        watched = set(self.holder.current.discord.watch_voice_channels)
        touched: dict[int, discord.abc.Connectable] = {}
        for state in (before, after):
            channel = state.channel
            if channel is not None and channel.id in watched:
                touched[channel.id] = channel
        for channel_id, channel in touched.items():
            present, unmuted = self._human_census(channel)
            await self.voice_gateway.on_voice_update(channel_id, present, unmuted)

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

    def _record_post_failure(self) -> None:
        reporter = getattr(self, "health_reporter", None)
        if reporter is not None:
            reporter.record_post_failure()

    async def post(
        self,
        guild_id: int,
        channel: AlertChannel,
        content: str,
        card: CardRender,
        *,
        mentions: MentionDecision | None = None,
        channel_id: int | None = None,
    ) -> tuple[int, int]:
        """Post an incident card; returns ``(channel_id, message_id)``.

        ``mentions`` is the ``decide_mentions`` grant (the single escalation
        authority, constraint 11): ``AllowedMentions`` is built from it as an
        explicit allowlist — the listed user ids (never ``users=True``, so
        verbatim detail text can't ping arbitrary users), the listed role
        ids, and ``everyone`` only when ``decision.here``. ``mentions=None``
        means nothing in the content may notify anyone.

        ``channel_id`` overrides the :class:`AlertChannel` lookup with an
        exact channel — the engine's re-post-on-lost-message path (§9.1) uses
        it so a replacement card lands in the same channel the original
        lived in, never migrating between #intel-alerts and #intel-live.

        Raises :class:`PostError` on any Discord failure (403 on the channel,
        deleted channel, REST error) so the engine rolls the incident back —
        a raw discord exception here used to orphan an invisible ACTIVE
        incident and, on the sweep path, crash-loop the whole process.
        """
        from cortana.dsc.views import view_from_card

        try:
            if channel_id is not None:
                target = await self._messageable(channel_id)
            else:
                target = await self._alert_channel(channel)
            # Silent mode hard-stop (belt for decide_mentions' braces): even
            # if a stray "@here" reached the content, Discord suppresses the
            # actual notification when mentions are disabled.
            if mentions is None or not self.holder.current.discord.mentions_enabled:
                allowed = discord.AllowedMentions.none()
            else:
                allowed = discord.AllowedMentions(
                    everyone=mentions.here,
                    roles=[discord.Object(id=r) for r in mentions.role_ids],
                    users=[discord.Object(id=u) for u in mentions.user_ids],
                )
            message = await target.send(
                content=content or None,
                embed=discord.Embed.from_dict(card.embed),
                view=view_from_card(card),
                allowed_mentions=allowed,
            )
        except (discord.DiscordException, TypeError) as exc:
            log.warning("card_post_failed", channel=str(channel), error=str(exc))
            self._record_post_failure()
            await self._raise_alarm(
                AlarmCode.POST_FAILURE,
                AlarmSeverity.CRITICAL,
                f"An incident card could not be posted to {channel}: {exc}",
                "check CORTANA's channel permissions (View Channel, Send Messages, Embed Links)",
            )
            raise PostError(f"post to {channel} failed: {exc}") from exc
        await self._clear_alarm(AlarmCode.POST_FAILURE)
        return target.id, message.id

    async def edit(self, channel_id: int, message_id: int, content: str, card: CardRender) -> None:
        """Edit the card in place — the only mutation an incident ever gets.

        Raises :class:`EditNotFound` when the message was deleted, so the
        engine's deliverer can re-post the card and store the new ids (§9.1:
        one LIVE message per incident). Every OTHER Discord failure stays
        best-effort (the card is a view; the DB row is the state) — a 403/5xx
        propagating out of here used to kill the stale sweep and take the
        whole process down.
        """
        from cortana.dsc.views import view_from_card

        kwargs: dict[str, Any] = {
            "embed": discord.Embed.from_dict(card.embed),
            "view": view_from_card(card),  # None strips buttons off resolved cards
        }
        if content:
            kwargs["content"] = content
        try:
            target = await self._messageable(channel_id)
            await target.get_partial_message(message_id).edit(**kwargs)
        except discord.NotFound as exc:
            log.warning("card_message_deleted", channel_id=channel_id, message_id=message_id)
            raise EditNotFound(f"card message {message_id} is gone: {exc}") from exc
        except (discord.DiscordException, TypeError) as exc:
            log.warning(
                "card_edit_failed",
                channel_id=channel_id,
                message_id=message_id,
                error=str(exc),
            )
            self._record_post_failure()
            await self._raise_alarm(
                AlarmCode.POST_FAILURE,
                AlarmSeverity.CRITICAL,
                f"An incident card edit failed in channel {channel_id}: {exc}",
                "check CORTANA's channel permissions (View Channel, Send Messages, Embed Links)",
            )

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
