"""ModuleManager — registry-driven lifecycle fan-out with per-phase isolation.

The manager is the only thing ``App`` talks to. It walks the registered
modules through each lifecycle phase, building a :class:`ModuleContext` per
module, and it isolates non-critical failures at every phase so one broken
add-on can never abort boot, shutdown, or a reload:

- **setup**: a non-critical module that raises is alarmed and *dropped*; boot
  continues with the rest. A *critical* module (CORTANA) is never caught —
  its failure stays fatal (exit → systemd restart).
- **start / on_ready / reload**: a non-critical throw is logged and contained;
  the module is marked degraded but the bot stays up.
- **stop**: reverse order, each ``stop()`` under a 2s cap and suppressed, so a
  wedged add-on can't blow the shared 8s graceful-shutdown budget.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import TYPE_CHECKING, Any

import structlog

from cortana.alarms import AlarmCode, AlarmSeverity
from dead.module import BotModule, ModuleContext, ModuleHealth, ModuleStatus

if TYPE_CHECKING:
    from discord.ext import commands
    from discord.ui import DynamicItem

    from cortana.alarms import AlarmBus
    from cortana.config import AuraConfig, ConfigHolder
    from dead.supervisor import ModuleSupervisor

log = structlog.get_logger(__name__)

#: Per-module stop() budget. The whole graceful shutdown is bounded (~8s); no
#: single add-on may consume it, so each stop() is capped well under it.
_STOP_TIMEOUT_S = 2.0

#: Severity ordering used to merge a module's self-report with the supervisor's
#: view — the worse of the two wins on ``/botstatus``.
_RANK = {
    ModuleStatus.DISABLED: 0,
    ModuleStatus.OK: 1,
    ModuleStatus.STARTING: 2,
    ModuleStatus.DEGRADED: 3,
    ModuleStatus.FAILED: 4,
}


class ModuleManager:
    """Owns the registered modules and drives their lifecycle for ``App``."""

    def __init__(
        self,
        *,
        holder: ConfigHolder,
        bot: commands.Bot,
        alarms: AlarmBus,
        shutdown: asyncio.Event,
        supervisor: ModuleSupervisor,
        logger: Any = log,
    ) -> None:
        self._holder = holder
        self._bot = bot
        self._alarms = alarms
        self._shutdown = shutdown
        self._supervisor = supervisor
        self._log = logger
        self._modules: list[BotModule] = []
        self._active: list[BotModule] = []
        self._contexts: dict[str, ModuleContext] = {}
        #: setup-failed modules kept only so ``/botstatus`` can show them FAILED.
        self._failed: dict[str, str] = {}

    def register(self, *modules: BotModule) -> None:
        """Record modules in start order (CORTANA first)."""
        self._modules.extend(modules)

    def _context(self, module: BotModule) -> ModuleContext:
        return ModuleContext(
            name=module.name,
            holder=self._holder,
            bot=self._bot,
            alarms=self._alarms,
            log=self._log.bind(module=module.name),
            shutdown=self._shutdown,
            supervisor=self._supervisor,
            to_thread=asyncio.to_thread,
            credentials_dir=os.environ.get("CREDENTIALS_DIRECTORY"),
        )

    async def setup_enabled(self) -> None:
        """Build every enabled module. A non-critical setup failure quarantines
        that module and boot continues; a critical failure propagates (fatal)."""
        cfg = self._holder.current
        for module in self._modules:
            try:
                on = module.enabled(cfg)
            except Exception:
                self._log.exception("module_enabled_check_failed", module=module.name)
                on = False
            if not on:
                self._log.info("module_disabled", module=module.name)
                continue
            ctx = self._context(module)
            if module.critical:
                # CORTANA: App already built the real thing; setup() is a no-op.
                # A failure here is a genuine voice-bootstrap fault — let it be
                # fatal, exactly as before the kernel existed.
                await module.setup(ctx)
                self._contexts[module.name] = ctx
                self._active.append(module)
                continue
            try:
                await module.setup(ctx)
            except Exception:
                self._log.exception("module_setup_failed", module=module.name)
                self._failed[module.name] = "setup failed — see the journal"
                await self._alarm(
                    AlarmCode.MODULE_SETUP_FAILED,
                    f"`{module.name}` failed to start up and was disabled for this run. "
                    "The rest of the bot is unaffected.",
                    "check the journal for the traceback, fix it, then restart",
                    key=module.name,
                )
                continue
            self._contexts[module.name] = ctx
            self._active.append(module)
            self._log.info("module_setup_ok", module=module.name)

    def all_cogs(self) -> list[commands.Cog]:
        """Cogs from every active module (CORTANA's stay hard-wired → none here).
        A module whose cogs() raises is contained (dropped + FAILED), never
        allowed to abort boot."""
        cogs: list[commands.Cog] = []
        for module in self._active:
            try:
                cogs.extend(module.cogs())
            except Exception:
                self._log.exception("module_cogs_failed", module=module.name)
                self._failed[module.name] = "cogs() failed — see the journal"
        return cogs

    def all_dynamic_items(self) -> list[type[DynamicItem[Any]]]:
        items: list[type[DynamicItem[Any]]] = []
        for module in self._active:
            try:
                items.extend(module.dynamic_items())
            except Exception:
                self._log.exception("module_dynamic_items_failed", module=module.name)
                self._failed[module.name] = "dynamic_items() failed — see the journal"
        return items

    async def start_enabled(self) -> None:
        """Post-login start. Non-critical failures are contained."""
        for module in list(self._active):
            ctx = self._contexts[module.name]
            if module.critical:
                await module.start(ctx)
                continue
            try:
                await module.start(ctx)
            except Exception:
                self._log.exception("module_start_failed", module=module.name)
                self._failed[module.name] = "start failed — see the journal"
                await self._alarm(
                    AlarmCode.MODULE_SETUP_FAILED,
                    f"`{module.name}` failed to start its background work and is degraded.",
                    "check the journal; `/reload` or restart to retry",
                    key=module.name,
                )

    async def on_ready(self) -> None:
        for module in list(self._active):
            try:
                await module.on_ready(self._bot)
            except Exception:
                self._log.exception("module_on_ready_failed", module=module.name)

    async def reload_all(self, old: AuraConfig, new: AuraConfig) -> None:
        for module in list(self._active):
            try:
                await module.reload(old, new)
            except Exception:
                self._log.exception("module_reload_failed", module=module.name)

    async def stop_all(self) -> None:
        """Stop active modules in reverse order, each bounded and suppressed."""
        for module in reversed(self._active):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(module.stop(), timeout=_STOP_TIMEOUT_S)

    def health_snapshot(self) -> dict[str, ModuleHealth]:
        """One :class:`ModuleHealth` per module for ``/botstatus``. Merges the
        module's self-report with the supervisor's view (worse wins) and shows
        setup/start-failed modules as FAILED."""
        snapshot: dict[str, ModuleHealth] = {}
        for module in self._active:
            if module.name in self._failed:
                continue  # start/cog-failed → authoritative FAILED, applied below
            try:
                own = module.health()
            except Exception:
                self._log.exception("module_health_failed", module=module.name)
                snapshot[module.name] = ModuleHealth(ModuleStatus.FAILED, "health() raised")
                continue
            sup = self._supervisor.status(module.name)
            status = own.status if _RANK[own.status] >= _RANK[sup] else sup
            snapshot[module.name] = ModuleHealth(status, own.detail, own.metrics)
        # _failed is authoritative — a module that failed setup/start/cog wiring
        # reads FAILED even if it lingers in _active for stop() cleanup.
        for name, detail in self._failed.items():
            snapshot[name] = ModuleHealth(ModuleStatus.FAILED, detail)
        return snapshot

    async def _alarm(self, code: AlarmCode, summary: str, hint: str, *, key: str) -> None:
        with contextlib.suppress(Exception):
            await self._alarms.raise_alarm(code, AlarmSeverity.WARNING, summary, hint, key=key)


__all__ = ["ModuleManager"]
