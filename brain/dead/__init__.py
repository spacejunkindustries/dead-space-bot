"""The Dead bot kernel — a small, game-agnostic module host.

CORTANA (EVE Echoes voice) is the first and only *critical* module; the Albion
killboard is the first *add-on*. The kernel's whole job is to let add-ons plug
into the one running discord.py process without ever being able to take the
voice bot down with them (crash isolation), and to give the operator one
health surface across every module.

The composition root stays :class:`cortana.__main__.App` — it still owns the
event loop, signals, the critical-task supervisor (``App._spawn``), the shared
shutdown event, and the graceful-shutdown budget. The kernel is purely
additive: :class:`~dead.manager.ModuleManager` fans lifecycle calls out to the
registered modules, and :class:`~dead.supervisor.ModuleSupervisor` runs add-on
background tasks with restart/backoff/quarantine that never touches the shared
shutdown event. See ``docs/dead-bot-architecture.md``.
"""

from __future__ import annotations

from dead.module import (
    Backoff,
    BotModule,
    ModuleContext,
    ModuleHealth,
    ModuleStatus,
)

__all__ = [
    "Backoff",
    "BotModule",
    "ModuleContext",
    "ModuleHealth",
    "ModuleStatus",
]
