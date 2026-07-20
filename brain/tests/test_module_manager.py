"""ModuleManager — lifecycle fan-out with per-phase isolation (dead/ kernel).

Pins the isolation guarantees: a disabled module is never built; a non-critical
module that fails setup is quarantined and boot continues; a critical module's
failure is fatal; add-on cogs are collected (CORTANA's are not); stop runs in
reverse order; a module whose reload throws doesn't break its siblings.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from cortana.alarms import AlarmCode
from dead.manager import ModuleManager
from dead.module import BotModule, ModuleContext, ModuleHealth, ModuleStatus
from dead.supervisor import ModuleSupervisor


class _FakeAlarms:
    def __init__(self) -> None:
        self.raised: list[tuple[AlarmCode, str | None]] = []

    async def raise_alarm(
        self, code: AlarmCode, severity: object, summary: str, hint: str, key: str | None = None
    ) -> None:
        self.raised.append((code, key))


class _Mod(BotModule):
    """A scriptable module that records its lifecycle calls into a shared sink."""

    def __init__(
        self,
        name: str,
        sink: list[tuple[str, str]],
        *,
        enabled: bool = True,
        critical: bool = False,
        setup_raises: bool = False,
        reload_raises: bool = False,
    ) -> None:
        self.name = name
        self.critical = critical
        self._sink = sink
        self._enabled = enabled
        self._setup_raises = setup_raises
        self._reload_raises = reload_raises

    def enabled(self, cfg: Any) -> bool:
        return self._enabled

    async def setup(self, ctx: ModuleContext) -> None:
        self._sink.append((self.name, "setup"))
        if self._setup_raises:
            raise RuntimeError("setup boom")

    async def start(self, ctx: ModuleContext) -> None:
        self._sink.append((self.name, "start"))

    async def stop(self) -> None:
        self._sink.append((self.name, "stop"))

    async def reload(self, old: Any, new: Any) -> None:
        self._sink.append((self.name, "reload"))
        if self._reload_raises:
            raise RuntimeError("reload boom")

    def cogs(self) -> tuple[Any, ...]:
        # CORTANA's cogs are hard-wired in setup_hook, so the facade returns
        # none; the fake mirrors that so all_cogs() excludes critical modules.
        return () if self.critical else (f"cog:{self.name}",)

    def health(self) -> ModuleHealth:
        return ModuleHealth(ModuleStatus.OK, "ok")


def _manager(*modules: BotModule) -> tuple[ModuleManager, _FakeAlarms]:
    holder = SimpleNamespace(current=SimpleNamespace())
    alarms = _FakeAlarms()
    shutdown = asyncio.Event()
    supervisor = ModuleSupervisor(shutdown, alarms)  # type: ignore[arg-type]
    mgr = ModuleManager(
        holder=holder,  # type: ignore[arg-type]
        bot=SimpleNamespace(),  # type: ignore[arg-type]
        alarms=alarms,  # type: ignore[arg-type]
        shutdown=shutdown,
        supervisor=supervisor,
        logger=structlog.get_logger("test"),
    )
    mgr.register(*modules)
    return mgr, alarms


async def test_disabled_module_is_never_built() -> None:
    sink: list[tuple[str, str]] = []
    m = _Mod("kb", sink, enabled=False)
    mgr, _ = _manager(m)
    await mgr.setup_enabled()
    assert sink == []  # enabled()->False → no setup, no cog
    assert mgr.all_cogs() == []


async def test_setup_failure_is_quarantined_and_boot_continues() -> None:
    sink: list[tuple[str, str]] = []
    bad = _Mod("bad", sink, setup_raises=True)
    good = _Mod("good", sink)
    mgr, alarms = _manager(bad, good)
    await mgr.setup_enabled()
    assert (good, "start") not in sink  # not started yet, but...
    assert ("good", "setup") in sink  # the good module still came up
    assert mgr.all_cogs() == ["cog:good"]  # only the survivor's cog
    snap = mgr.health_snapshot()
    assert snap["bad"].status is ModuleStatus.FAILED
    assert snap["good"].status is ModuleStatus.OK
    assert any(code is AlarmCode.MODULE_SETUP_FAILED for code, _ in alarms.raised)


async def test_health_snapshot_explains_a_quarantined_task_not_a_stale_ok() -> None:
    """When the supervisor's status (a quarantined background task = FAILED) is
    worse than the module's own OK self-report, the snapshot must surface FAILED
    *and* replace the reassuring 'ok' detail with why — otherwise /botstatus reads
    'FAILED — ok' and masks the reason."""
    sink: list[tuple[str, str]] = []
    m = _Mod("kb", sink)  # health() → (OK, "ok")
    mgr, _ = _manager(m)
    await mgr.setup_enabled()  # kb is active, healthy, not failed
    # Simulate one of kb's supervised tasks having crash-looped into quarantine.
    mgr._supervisor.status = lambda name: ModuleStatus.FAILED  # type: ignore[assignment,method-assign]

    snap = mgr.health_snapshot()

    assert snap["kb"].status is ModuleStatus.FAILED
    assert snap["kb"].detail != "ok"  # not the stale reassuring self-report
    assert "supervised task" in snap["kb"].detail


async def test_critical_setup_failure_is_fatal() -> None:
    sink: list[tuple[str, str]] = []
    crit = _Mod("cortana", sink, critical=True, setup_raises=True)
    mgr, _ = _manager(crit)
    with pytest.raises(RuntimeError):
        await mgr.setup_enabled()  # a critical failure propagates (process dies)


async def test_all_cogs_excludes_critical_modules() -> None:
    sink: list[tuple[str, str]] = []
    cortana = _Mod("cortana", sink, critical=True)
    kb = _Mod("kb", sink)
    mgr, _ = _manager(cortana, kb)
    await mgr.setup_enabled()
    assert mgr.all_cogs() == ["cog:kb"]  # cortana facade contributes none


async def test_stop_all_runs_in_reverse_order() -> None:
    sink: list[tuple[str, str]] = []
    a = _Mod("a", sink)
    b = _Mod("b", sink)
    c = _Mod("c", sink)
    mgr, _ = _manager(a, b, c)
    await mgr.setup_enabled()
    sink.clear()
    await mgr.stop_all()
    assert sink == [("c", "stop"), ("b", "stop"), ("a", "stop")]


async def test_reload_isolates_a_throwing_module() -> None:
    sink: list[tuple[str, str]] = []
    bad = _Mod("bad", sink, reload_raises=True)
    good = _Mod("good", sink)
    mgr, _ = _manager(bad, good)
    await mgr.setup_enabled()
    sink.clear()
    await mgr.reload_all(SimpleNamespace(), SimpleNamespace())
    # bad's reload threw but was contained; good's still ran.
    assert ("bad", "reload") in sink
    assert ("good", "reload") in sink
