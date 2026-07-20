"""ModuleSupervisor — crash isolation for add-on tasks (dead/ kernel).

The whole point of the supervisor is that an add-on task can crash, flap, or
hard-fail without ever touching the shared shutdown event that would take the
voice bot down. These tests pin that: restart-with-backoff, clean-return-ends,
quarantine after a storm, cancellable backoff, and — the load-bearing one — a
module crash NEVER sets shutdown.
"""

from __future__ import annotations

import asyncio

from cortana.alarms import AlarmCode
from dead.module import Backoff, ModuleStatus
from dead.supervisor import ModuleSupervisor

#: Tiny backoff so restart tests don't wait real seconds.
_FAST = Backoff(base=0.001, factor=1.0, cap=0.001, reset_after=300.0, max_restarts=3)


class _FakeAlarms:
    """Records raises; the supervisor only needs an async raise_alarm."""

    def __init__(self) -> None:
        self.raised: list[tuple[AlarmCode, str | None]] = []

    async def raise_alarm(
        self, code: AlarmCode, severity: object, summary: str, hint: str, key: str | None = None
    ) -> None:
        self.raised.append((code, key))


def _sup(
    shutdown: asyncio.Event | None = None,
) -> tuple[ModuleSupervisor, asyncio.Event, _FakeAlarms]:
    shutdown = shutdown if shutdown is not None else asyncio.Event()
    alarms = _FakeAlarms()
    return ModuleSupervisor(shutdown, alarms), shutdown, alarms  # type: ignore[arg-type]


async def _await_task(sup: ModuleSupervisor, module: str, task: str) -> None:
    await sup._tasks[(module, task)]


async def test_clean_return_does_not_restart() -> None:
    sup, shutdown, _ = _sup()
    calls = 0

    async def factory() -> None:
        nonlocal calls
        calls += 1

    sup.spawn("m", "t", factory, backoff=_FAST)
    await _await_task(sup, "m", "t")
    assert calls == 1  # a task that returns cleanly is DONE, not restarted
    assert sup.status("m") is ModuleStatus.OK
    assert not shutdown.is_set()


async def test_crash_then_success_restarts_and_never_shuts_down() -> None:
    sup, shutdown, alarms = _sup()
    calls = 0

    async def factory() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")  # crash once, then succeed

    sup.spawn("m", "t", factory, backoff=_FAST)
    await _await_task(sup, "m", "t")
    assert calls == 2  # restarted exactly once
    assert not shutdown.is_set()  # a module crash must NEVER set shutdown
    assert any(code is AlarmCode.MODULE_TASK_DEGRADED for code, _ in alarms.raised)


async def test_quarantine_after_max_restarts() -> None:
    sup, shutdown, alarms = _sup()
    calls = 0

    async def factory() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("always")

    sup.spawn("m", "t", factory, backoff=_FAST)
    await _await_task(sup, "m", "t")
    assert calls == _FAST.max_restarts  # stops trying after the storm
    assert sup.status("m") is ModuleStatus.FAILED
    assert any(code is AlarmCode.MODULE_QUARANTINED for code, _ in alarms.raised)
    assert not shutdown.is_set()  # even a hard-failed module never shuts down


async def test_shutdown_cuts_backoff_short() -> None:
    shutdown = asyncio.Event()
    sup, _, _ = _sup(shutdown)

    async def factory() -> None:
        raise RuntimeError("boom")  # forces a (long) backoff sleep

    # A 10s backoff must not delay shutdown: setting the event ends the runner.
    sup.spawn("m", "t", factory, backoff=Backoff(base=10.0, factor=1.0, cap=10.0, max_restarts=99))
    await asyncio.sleep(0.05)  # let it crash once and enter the backoff sleep
    shutdown.set()
    await asyncio.wait_for(_await_task(sup, "m", "t"), timeout=1.0)


async def test_stop_all_cancels_running_task() -> None:
    sup, _, _ = _sup()
    started = asyncio.Event()

    async def factory() -> None:
        started.set()
        await asyncio.sleep(3600)

    sup.spawn("m", "t", factory)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await sup.stop_all()
    assert not sup._tasks  # cancelled, awaited, and cleared


async def test_one_task_restart_does_not_mask_a_quarantined_sibling() -> None:
    """A module runs several tasks under one name; a healthy/flapping task's
    optimistic OK-reset must NOT erase a sibling task that quarantined — status
    is the WORST across the module's tasks."""
    sup, _, _ = _sup()

    async def dead() -> None:
        raise RuntimeError("always")  # quarantines fast

    started = asyncio.Event()

    async def alive() -> None:
        started.set()
        await asyncio.sleep(3600)  # healthy sibling, keeps running

    sup.spawn("kb", "feed", dead, backoff=_FAST)
    await _await_task(sup, "kb", "feed")  # feed quarantines → FAILED
    assert sup.status("kb") is ModuleStatus.FAILED

    sup.spawn("kb", "poller", alive)  # a healthy sibling starts (sets its own OK)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    # The module is still FAILED — the sibling's OK didn't erase the dead feed.
    assert sup.status("kb") is ModuleStatus.FAILED
    await sup.stop_all()


async def test_duplicate_spawn_is_ignored() -> None:
    sup, _, _ = _sup()
    started = asyncio.Event()

    async def factory() -> None:
        started.set()
        await asyncio.sleep(3600)

    sup.spawn("m", "t", factory)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    first = sup._tasks[("m", "t")]
    sup.spawn("m", "t", factory)  # already running → ignored
    assert sup._tasks[("m", "t")] is first
    await sup.stop_all()
