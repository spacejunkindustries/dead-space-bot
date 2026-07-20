"""ModuleSupervisor — the crash-isolation half of the kernel.

Every add-on background task (the killboard poller, feed, scheduler, …) runs
here instead of on ``App._spawn``. The difference is the whole point of the
platform:

- ``App._spawn`` is **fatal**: a crash sets the shared shutdown event and the
  process exits (systemd restarts it). Correct for CORTANA's six voice tasks —
  a dead voice pipeline should re-handshake DAVE from a clean process.
- ``ModuleSupervisor`` is **contained**: a crash is logged, alarmed, and the
  task is restarted with exponential backoff; after a storm the task is
  quarantined. The shared shutdown event is **never** set here, so a flapping
  killboard poller can never take the voice bot (or Ears' DAVE session) down.

The supervisor watches the same shutdown event only to *stop* — backoff sleeps
are cancellable through it, and shutdown ends every runner promptly so
``stop_all`` returns inside the graceful-shutdown budget.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import structlog

from cortana.alarms import AlarmCode, AlarmSeverity
from dead.module import Backoff, ModuleStatus

if TYPE_CHECKING:
    from cortana.alarms import AlarmBus

log = structlog.get_logger(__name__)

#: A task factory: called to (re)start one supervised coroutine. It is a
#: *factory*, not a coroutine, so a restart builds a fresh awaitable — reusing
#: a spent coroutine would raise ``RuntimeError: cannot reuse already awaited``.
TaskFactory = Callable[[], Awaitable[None]]

#: Shared default policy — safe to share because :class:`Backoff` is frozen.
_DEFAULT_BACKOFF = Backoff()


class ModuleSupervisor:
    """Runs add-on tasks with restart/backoff/quarantine, isolated from the
    process-fatal path. One instance is shared by every module."""

    def __init__(self, shutdown: asyncio.Event, alarms: AlarmBus | None, logger: Any = log) -> None:
        self._shutdown = shutdown
        self._alarms = alarms
        self._log = logger
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._status: dict[str, ModuleStatus] = {}

    def spawn(
        self,
        module: str,
        task: str,
        factory: TaskFactory,
        *,
        backoff: Backoff = _DEFAULT_BACKOFF,
    ) -> None:
        """Start (or ignore, if already running) a supervised task named
        ``(module, task)``. Returns immediately; the runner lives on the loop."""
        key = (module, task)
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            self._log.warning("module_task_already_running", module=module, task=task)
            return
        self._status.setdefault(module, ModuleStatus.OK)
        runner = asyncio.create_task(
            self._runner(module, task, factory, backoff), name=f"mod:{module}:{task}"
        )
        self._tasks[key] = runner

    async def _runner(self, module: str, task: str, factory: TaskFactory, backoff: Backoff) -> None:
        """Restart loop for one task. Clean return ends it; a crash backs off
        and retries; a storm quarantines. Never sets the shutdown event."""
        loop = asyncio.get_running_loop()
        streak = 0
        last_fail = 0.0
        while not self._shutdown.is_set():
            # Optimistic on (re)start: a task that crashed and is now retrying
            # has recovered as far as we know, so clear a stale DEGRADED/FAILED
            # instead of showing degraded for the rest of the process life. It
            # flips back to DEGRADED below the instant it crashes again. (Status
            # is per-module, so with several tasks the last writer wins — good
            # enough for a health hint; the module's own health() is the detail.)
            self._status[module] = ModuleStatus.OK
            try:
                await factory()
                return  # a task that returns cleanly is DONE, not restarted
            except asyncio.CancelledError:
                raise  # shutdown / stop() — propagate so the task ends fast
            except Exception:
                self._log.exception("module_task_crashed", module=module, task=task)
                now = loop.time()
                if now - last_fail > backoff.reset_after:
                    streak = 0  # survived long enough — a fresh incident
                streak += 1
                last_fail = now
                self._status[module] = ModuleStatus.DEGRADED
                await self._alarm(
                    AlarmCode.MODULE_TASK_DEGRADED,
                    AlarmSeverity.WARNING,
                    f"`{module}` task `{task}` crashed (restart {streak}) — the module "
                    "is degraded; the rest of the bot is unaffected.",
                    "check the journal for the traceback; it will keep retrying",
                    key=f"{module}:{task}",
                )
                if streak >= backoff.max_restarts:
                    self._status[module] = ModuleStatus.FAILED
                    await self._alarm(
                        AlarmCode.MODULE_QUARANTINED,
                        AlarmSeverity.CRITICAL,
                        f"`{module}` task `{task}` failed {streak} times in a row and is "
                        "quarantined (stopped). The rest of the bot keeps running.",
                        "fix the cause, then `/reload` or restart to bring it back",
                        key=f"{module}:{task}",
                    )
                    return
                delay = min(backoff.base * backoff.factor ** (streak - 1), backoff.cap)
                if await self._sleep_or_shutdown(delay):
                    return  # shutdown fired during backoff

    async def _sleep_or_shutdown(self, delay: float) -> bool:
        """Sleep ``delay`` seconds, or wake early if shutdown fires. Returns
        True iff shutdown fired (so the caller should stop)."""
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=delay)
        except TimeoutError:
            return False
        return True

    async def _alarm(
        self, code: AlarmCode, severity: AlarmSeverity, summary: str, hint: str, *, key: str
    ) -> None:
        """Raise an operator alarm, best-effort — a broken alarm bus must never
        break the supervisor (which exists precisely to contain failures)."""
        if self._alarms is None:
            return
        with contextlib.suppress(Exception):
            await self._alarms.raise_alarm(code, severity, summary, hint, key=key)

    def status(self, module: str) -> ModuleStatus:
        """The supervisor's view of a module (OK until a task crashes)."""
        return self._status.get(module, ModuleStatus.OK)

    async def stop(self, module: str) -> None:
        """Cancel and await every task for one module. Idempotent."""
        keys = [k for k in self._tasks if k[0] == module]
        await self._cancel(keys)

    async def stop_all(self) -> None:
        """Cancel and await every supervised task. Idempotent; fast — tasks
        propagate CancelledError so this returns well inside the shutdown budget."""
        await self._cancel(list(self._tasks))

    async def _cancel(self, keys: list[tuple[str, str]]) -> None:
        tasks = [self._tasks.pop(k) for k in keys if k in self._tasks]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["ModuleSupervisor", "TaskFactory"]
