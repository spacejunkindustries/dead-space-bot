"""KillboardModule — the one class the kernel talks to (killboard GDD §3, §4).

This ties the killboard together and presents it to the ``dead/`` kernel as a
single :class:`~dead.module.BotModule`. It builds the store, the async API
client, the poller, the feed, the scheduler, the battles handle, the card
renderer, and the slash cog in :meth:`setup` (no network, no tasks), registers
the cog and the restart-proof pager in :meth:`cogs` / :meth:`dynamic_items`,
resolves the tracked guild and spawns the three supervised loops in
:meth:`start`, and closes the API session in :meth:`stop`.

Design boundaries this file honours:

* **OFF by default.** :meth:`enabled` is the whole gate (killboard GDD §12):
  the module runs only when ``killboard.enabled`` is set *and* a guild (id or
  name) *and* at least one feed channel are configured. A half-set config
  degrades to "disabled", never a crash of the voice bot.
* **Its own database.** The store opens ``cfg.killboard.storage.db_path`` with
  the killboard's own migrations (``brain/killboard/migrations``); CORTANA's
  connection and tables are never touched.
* **Contained failures.** The three loops run on ``ctx.supervisor`` (not the
  process-fatal ``App._spawn``), so a flapping poller can never take the voice
  bot down. Every Discord message the module's components send passes
  ``allowed_mentions=discord.AllowedMentions.none()`` (CLAUDE.md constraint 11).

The tracked guild id is resolved once at startup from ``guild_name`` via
``/search`` when it is not set directly (killboard GDD §2.2). A guild lives on
exactly one regional server, so an empty resolution almost always means the
wrong ``region`` — that is surfaced as a clear operator alarm and the loops are
not spawned (fail-fast, killboard GDD §13). The resolved id is injected into the
:class:`~cortana.config.KillboardConfig` every component reads live, so the
file-owned config (CLAUDE.md constraint 12) is never mutated in place.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from cortana.alarms import AlarmCode, AlarmSeverity
from dead.module import BotModule, ModuleContext, ModuleHealth, ModuleStatus
from killboard.battles import Battles
from killboard.cards import CardRenderer
from killboard.commands import KillboardCog
from killboard.feed import Feed
from killboard.poller import Poller
from killboard.schedule import Scheduler
from killboard.store import KbStore, open_store
from killboard.views import RankingPageButton

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from discord.ext import commands
    from discord.ui import DynamicItem
    from structlog.stdlib import BoundLogger

    from cortana.config import AuraConfig, ConfigHolder, KillboardConfig
    from killboard.api import KbApi
    from killboard.model import EventRow

log = structlog.get_logger(__name__)

#: Consecutive poll failures at which the module self-reports DEGRADED. A few
#: transient misses are normal (the API is flaky, killboard GDD §13); a run this
#: deep means the endpoint is genuinely down and the feed is serving stale data.
_FAILS_DEGRADED = 3


class KillboardModule(BotModule):
    """The killboard add-on, presented to the kernel (killboard GDD §3, §4).

    Non-critical: its failures are contained (logged, alarmed, retried), never
    fatal to the voice bot. Constructed with no arguments by the registry; all
    of its collaborators are built in :meth:`setup` from the context.
    """

    name = "killboard"
    critical = False

    def __init__(self) -> None:
        self._store: KbStore | None = None
        self._api: KbApi | None = None
        self._cards: CardRenderer | None = None
        self._battles: Battles | None = None
        self._poller: Poller | None = None
        self._feed: Feed | None = None
        self._scheduler: Scheduler | None = None
        self._cog: KillboardCog | None = None
        self._queue: asyncio.Queue[list[EventRow]] | None = None
        # Bound in setup() from the context; only read after setup completes.
        self._holder: ConfigHolder
        self._to_thread: Callable[..., Awaitable[Any]]
        self._log: BoundLogger
        #: Guild id resolved from ``guild_name`` at startup; injected into the
        #: config every component reads (the file-owned config is never mutated).
        self._resolved_guild_id: str | None = None
        #: Non-empty when startup could not resolve the tracked guild — drives a
        #: FAILED health report so ``/botstatus`` shows the region mismatch.
        self._start_error: str | None = None

    # ── the OFF-by-default gate (killboard GDD §12) ──────────────────────────

    def enabled(self, cfg: AuraConfig) -> bool:
        """Pure gate: enabled, a guild (id or name), and a feed channel are set.

        No network, no side effects — read straight from config. Any one of the
        three missing leaves the module disabled so a partially-configured
        killboard degrades gracefully instead of crashing the voice bot.
        """
        kb = cfg.killboard
        has_guild = bool((kb.guild_id or "").strip() or (kb.guild_name or "").strip())
        has_channel = bool(kb.feed.kills_channel or kb.feed.deaths_channel)
        return bool(kb.enabled and has_guild and has_channel)

    # ── build & wire (no network, no tasks) ──────────────────────────────────

    async def setup(self, ctx: ModuleContext) -> None:
        """Open the store and build every collaborator; spawn nothing (§3.1).

        Runs during ``App.setup`` (Discord not yet connected), so this touches no
        network and starts no tasks — it only constructs. The store is opened
        (and migrated) off the event loop via ``ctx.to_thread`` even though the
        open path is single-threaded, keeping the blocking sqlite work off-loop
        by convention (GDD §14).
        """
        # Import here (not at module top) so a construction path is obvious and
        # the KbApi type stays a type-only import above.
        from killboard.api import KbApi

        self._holder = ctx.holder
        self._to_thread = ctx.to_thread
        self._log = ctx.log

        db_path = ctx.holder.current.killboard.storage.db_path
        self._store = await ctx.to_thread(open_store, db_path)

        self._api = KbApi(self._kb_cfg)
        self._cards = CardRenderer(self._kb_cfg, ctx.to_thread, log=ctx.log)
        self._battles = Battles(self._api, self._kb_cfg, ctx.log)

        # One queue shared poller → feed: the poller pushes genuinely-new events
        # as a wake-up signal; the feed re-reads the store for exactly-once.
        self._queue = asyncio.Queue()

        self._poller = Poller(
            self._store,
            self._api,
            self._kb_cfg,
            self._queue,
            ctx.to_thread,
            ctx.log,
            shutdown=ctx.shutdown,
        )
        self._feed = Feed(
            ctx.bot,
            self._store,
            self._cards,
            self._root_cfg,
            ctx.to_thread,
            ctx.log,
            queue=self._queue,
            shutdown=ctx.shutdown,
        )
        self._scheduler = Scheduler(
            ctx.bot,
            self._store,
            self._kb_cfg,
            ctx.to_thread,
            ctx.log,
            shutdown=ctx.shutdown,
        )
        self._cog = KillboardCog(
            ctx.bot,
            self._store,
            self._battles,
            self._kb_cfg,
            ctx.to_thread,
        )
        ctx.log.info("kb_module.setup", db_path=db_path)

    def cogs(self) -> Iterable[commands.Cog]:
        """The single ``/killboard *`` command cog (killboard GDD §10)."""
        return [self._cog] if self._cog is not None else []

    def dynamic_items(self) -> Iterable[type[DynamicItem[Any]]]:
        """The restart-proof leaderboard pager button (killboard GDD §8, views)."""
        return [RankingPageButton]

    # ── post-login: resolve the guild, spawn the loops ───────────────────────

    async def start(self, ctx: ModuleContext) -> None:
        """Resolve the tracked guild, then spawn the poller/feed/scheduler (§5).

        When ``guild_id`` is set directly nothing is resolved. Otherwise the id
        is looked up from ``guild_name`` via ``/search`` (killboard GDD §2.2); an
        empty resolution is treated as a fail-fast region mismatch — a clear
        alarm is raised and the loops are NOT spawned, because a poller with no
        guild id can only no-op. Spawns pass *factories* (bound ``run`` methods),
        never coroutines, so the supervisor can restart each loop cleanly.
        """
        if None in (self._api, self._poller, self._feed, self._scheduler):
            # setup() did not complete — the manager would have dropped us, but
            # guard anyway so start is never a surprise AttributeError.
            ctx.log.warning("kb_module.start_without_setup")
            return

        cfg = ctx.holder.current.killboard
        if not await self._resolve_guild(ctx, cfg):
            return

        ctx.supervisor.spawn("killboard", "poller", self._poller.run)
        ctx.supervisor.spawn("killboard", "feed", self._feed.run)
        ctx.supervisor.spawn("killboard", "scheduler", self._scheduler.run)
        ctx.log.info(
            "kb_module.start",
            guild_id=(cfg.guild_id or self._resolved_guild_id),
            region=cfg.region,
        )

    async def _resolve_guild(self, ctx: ModuleContext, cfg: KillboardConfig) -> bool:
        """Ensure a tracked guild id exists, resolving from the name if needed.

        Returns True when a guild id is available (already configured or freshly
        resolved) and the loops may start; False when resolution came back empty,
        in which case a region-mismatch alarm has been raised and ``start`` must
        not spawn anything (fail-fast, killboard GDD §13).
        """
        self._start_error = None
        if (cfg.guild_id or "").strip():
            return True  # id set directly — no lookup, no network.

        name = (cfg.guild_name or "").strip()
        assert self._api is not None  # guarded by the caller
        record = await self._api.search_guild(name) if name else None
        resolved = _extract_guild_id(record)
        if not resolved:
            detail = (
                f"Killboard could not find guild {name!r} on the {cfg.region!r} server. "
                "A guild exists on exactly one regional server, so this is almost always "
                "the wrong region."
            )
            self._start_error = detail
            ctx.log.error("kb_module.guild_unresolved", guild_name=name, region=cfg.region)
            await self._raise_region_alarm(ctx, detail)
            return False

        self._resolved_guild_id = resolved
        ctx.log.info("kb_module.guild_resolved", guild_name=name, guild_id=resolved)
        return True

    async def _raise_region_alarm(self, ctx: ModuleContext, detail: str) -> None:
        """Surface the region mismatch as an operator alarm (best-effort)."""
        try:
            await ctx.alarms.raise_alarm(
                AlarmCode.MODULE_SETUP_FAILED,
                AlarmSeverity.WARNING,
                detail,
                "set killboard.region to the guild's server (west|europe|east), or "
                "killboard.guild_id directly, then restart",
                key="killboard:guild",
            )
        except Exception as exc:  # noqa: BLE001 — a broken alarm bus must not break start
            ctx.log.warning("kb_module.alarm_failed", error=str(exc))

    # ── shutdown ─────────────────────────────────────────────────────────────

    async def stop(self) -> None:
        """Close the API session; idempotent and bounded (<2s).

        The supervised loops are cancelled by the kernel's supervisor on
        shutdown; this only releases the module's own resource — the shared
        ``aiohttp`` session — which :meth:`KbApi.close` closes idempotently.
        """
        if self._api is not None:
            await self._api.close()

    # ── health (killboard GDD §10, §13) ──────────────────────────────────────

    def health(self) -> ModuleHealth:
        """Self-report from the poll state, merged by the manager with the
        supervisor's view (worse wins).

        FAILED when startup could not resolve the guild; STARTING before the
        first successful poll; DEGRADED when polling is stale (no success within
        ``staleness.warn_after_minutes``) or a deep failure streak is running;
        OK otherwise. Reads only the single ``poll_state`` row (a cheap indexed
        lookup) plus the feed's in-memory counters — safe to call on the loop.
        """
        if self._store is None:
            return ModuleHealth(ModuleStatus.STARTING, "killboard initialising")
        if self._start_error:
            return ModuleHealth(ModuleStatus.FAILED, self._start_error)

        try:
            state = self._store.poll_state()
        except Exception as exc:  # noqa: BLE001 — /botstatus must never raise
            return ModuleHealth(ModuleStatus.DEGRADED, f"poll state unreadable: {exc}")

        last_success = state["last_success_at"]
        last_event = state["last_advanced_at"]
        fails = int(state["consecutive_fails"])

        metrics: dict[str, object] = {
            "last_poll": last_success or "never",
            "last_event": last_event or "never",
            "high_water": state["last_event_id"],
            "consecutive_fails": fails,
        }
        if self._feed is not None:
            snap = self._feed.snapshot()
            metrics["posted"] = snap.get("posted_total", 0)

        now = datetime.now(UTC)
        warn_minutes = self._holder.current.killboard.staleness.warn_after_minutes

        if last_success is None:
            return ModuleHealth(ModuleStatus.STARTING, "awaiting first poll", metrics)
        if _older_than_minutes(last_success, warn_minutes, now):
            return ModuleHealth(
                ModuleStatus.DEGRADED,
                f"no successful poll in > {warn_minutes}m — feed is stale",
                metrics,
            )
        if fails >= _FAILS_DEGRADED:
            return ModuleHealth(
                ModuleStatus.DEGRADED,
                f"{fails} polls failing in a row — serving stored data",
                metrics,
            )
        return ModuleHealth(ModuleStatus.OK, "polling", metrics)

    # ── live config providers (read at point of use) ─────────────────────────

    def _kb_cfg(self) -> KillboardConfig:
        """The current :class:`KillboardConfig`, with the resolved guild id.

        Read live from the holder so hot reloads apply on the next call. When the
        guild id was resolved from a name at startup it is injected here (via a
        frozen-dataclass copy) rather than mutating the file-owned config, so
        every component — poller, battles, scheduler, cog — sees the real id.
        """
        kb = self._holder.current.killboard
        if self._resolved_guild_id and not (kb.guild_id or "").strip():
            return replace(kb, guild_id=self._resolved_guild_id)
        return kb

    def _root_cfg(self) -> AuraConfig:
        """The current root :class:`AuraConfig`, read live (the feed's provider)."""
        return self._holder.current


def _extract_guild_id(record: dict[str, Any] | None) -> str | None:
    """Pull a guild id from a ``/search`` guild record, tolerating shape drift.

    The gameinfo API returns the id under ``Id`` (occasionally ``id``); anything
    else — a missing record, an empty/whitespace id — yields ``None`` so the
    caller treats it as an unresolved (likely wrong-region) guild.
    """
    if not isinstance(record, dict):
        return None
    raw = record.get("Id") or record.get("id")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _older_than_minutes(value: str | None, minutes: int, now: datetime) -> bool:
    """Whether ISO-8601 ``value`` is missing or older than ``minutes`` before now.

    Tolerant: an unparseable stamp reads as "old" (a stale signal is the safe
    default). Accepts a trailing ``Z`` for UTC and assumes UTC for a naive value.
    """
    if not value:
        return True
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (now - dt).total_seconds() > minutes * 60


__all__ = ["KillboardModule"]
