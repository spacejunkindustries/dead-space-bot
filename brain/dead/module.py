"""The plug-in contract every Dead module implements.

A module owns a slice of the bot: its own config namespace, its own database
file (add-ons never share CORTANA's connection), its own cogs, and its own
background tasks. The kernel only ever talks to a module through this contract,
so adding game #3 is "write a :class:`BotModule`, register it" — nothing in the
core changes.

Lifecycle, in order, driven by :class:`dead.manager.ModuleManager`:

1. :meth:`enabled` — a *pure* gate read from config. False → the module is
   never built (the OFF-by-default switch lives here, not in validation).
2. :meth:`setup` — build and wire everything; open+migrate the module's own DB.
   **No network, no login, no tasks yet** (Discord isn't connected during
   ``App.setup``). A non-critical module that raises here is quarantined and
   dropped; boot continues.
3. :meth:`cogs` / :meth:`dynamic_items` — collected and added to the bot after
   the core cogs, before command sync.
4. :meth:`start` — post-login. Spawn background work **only** via
   ``ctx.supervisor.spawn(...)`` (never the process-fatal ``App._spawn``).
5. :meth:`on_ready` — after the gateway is ready and the census is seeded.
6. :meth:`reload` — a config swap happened; hot-apply what you can.
7. :meth:`stop` — bounded (<2s) and idempotent; runs in reverse registration
   order inside the shared 8s shutdown budget.

:meth:`health` is polled any time for ``/botstatus``.
"""

from __future__ import annotations

import abc
import asyncio
import enum
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from discord.ext import commands
    from discord.ui import DynamicItem
    from structlog.stdlib import BoundLogger

    from cortana.alarms import AlarmBus
    from cortana.config import AuraConfig, ConfigHolder
    from dead.supervisor import ModuleSupervisor


class ModuleStatus(enum.Enum):
    """A module's coarse operational state, surfaced on ``/botstatus``."""

    DISABLED = "disabled"
    STARTING = "starting"
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ModuleHealth:
    """One module's self-report. ``metrics`` is free-form (last poll, events
    stored, …) rendered as a small key/value block under the module's section."""

    status: ModuleStatus
    detail: str
    metrics: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Backoff:
    """Restart policy for a supervised module task.

    ``base * factor**(streak-1)`` capped at ``cap`` seconds between restarts; a
    task that survives ``reset_after`` seconds resets its streak (so a task that
    fails once an hour is never quarantined); after ``max_restarts``
    back-to-back failures the task is quarantined (``FAILED``) so a hard-broken
    loop cannot busy-spin a vCPU forever.
    """

    base: float = 5.0
    factor: float = 2.0
    cap: float = 60.0
    reset_after: float = 300.0
    max_restarts: int = 8


@dataclass(frozen=True, slots=True)
class ModuleContext:
    """Everything a module is handed at setup/start — and nothing more.

    Deliberately withholds two things add-ons must never touch: the shared
    sqlite connection (add-ons own their own DB file) and ``App._spawn`` (a
    process-fatal supervisor; add-ons get ``supervisor`` instead, whose crashes
    are contained). ``to_thread`` is ``asyncio.to_thread`` — all blocking work
    (Pillow, sqlite, HTTP-with-a-sync-client) rides it so the voice event loop
    is never stalled by an add-on.
    """

    name: str
    holder: ConfigHolder
    bot: commands.Bot
    alarms: AlarmBus
    log: BoundLogger
    shutdown: asyncio.Event
    supervisor: ModuleSupervisor
    to_thread: Callable[..., Awaitable[Any]]
    credentials_dir: str | None = None


class BotModule(abc.ABC):
    """Base class for a Dead module. See the module docstring for the lifecycle."""

    #: Stable identifier; equals ``ctx.name`` and the config namespace. Set on
    #: the subclass.
    name: str = ""
    #: True only for CORTANA. A critical module's setup/start failure is fatal
    #: (the process exits and systemd restarts it — correct for the voice/DAVE
    #: re-handshake); a non-critical module's failure is contained.
    critical: bool = False

    @abc.abstractmethod
    def enabled(self, cfg: AuraConfig) -> bool:
        """Pure, side-effect-free: should this module run under ``cfg``? Called
        BEFORE :meth:`setup`. The OFF-by-default switch lives here."""

    @abc.abstractmethod
    async def setup(self, ctx: ModuleContext) -> None:
        """Build and wire; open+migrate the module's own DB. No network/tasks."""

    def cogs(self) -> Iterable[commands.Cog]:
        """discord.py cogs to register after the core cogs. Default: none."""
        return ()

    def dynamic_items(self) -> Iterable[type[DynamicItem[Any]]]:
        """Restart-proof component handlers to register. Default: none."""
        return ()

    @abc.abstractmethod
    async def start(self, ctx: ModuleContext) -> None:
        """Post-login. Spawn background tasks via ``ctx.supervisor.spawn``."""

    async def on_ready(self, bot: commands.Bot) -> None:
        """Called after the gateway is ready and the census is seeded."""
        return None

    async def reload(self, old: AuraConfig, new: AuraConfig) -> None:
        """A config swap happened; hot-apply what this module can."""
        return None

    @abc.abstractmethod
    async def stop(self) -> None:
        """Release resources. MUST be bounded (<2s) and idempotent — it runs
        inside the shared shutdown budget and may be called more than once."""

    def health(self) -> ModuleHealth:
        """Current self-report. Override; the default is a bland OK."""
        return ModuleHealth(ModuleStatus.OK, "")


__all__ = [
    "Backoff",
    "BotModule",
    "ModuleContext",
    "ModuleHealth",
    "ModuleStatus",
]
