"""CortanaModule — the voice bot, presented to the kernel as a module.

This is a **delegating facade**, not a relocation. CORTANA's voice/dialog/IPC
wiring stays exactly where it is: :class:`cortana.__main__.App` still builds
every component, binds the IPC socket before login, spawns the six voice tasks
on the process-fatal ``App._spawn``, and runs the no-leave shutdown. The facade
does nothing but let the kernel *see* CORTANA uniformly alongside add-ons — one
entry in ``/botstatus``, one entry in the lifecycle fan-out — without moving a
line of the code the owner stabilised.

Because ``App`` already does the real work, every lifecycle method here is a
no-op. The one method with substance is :meth:`health`, which reads the live
:class:`~cortana.health.HealthReporter`. CORTANA is ``critical=True``: its
failures are fatal by design (the process exits and systemd restarts it, which
is the correct recovery for a dead voice pipeline / a stale DAVE session), so it
never routes through the supervisor's contain-and-retry path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from dead.module import BotModule, ModuleContext, ModuleHealth, ModuleStatus

if TYPE_CHECKING:
    from cortana.config import AuraConfig


class CortanaModule(BotModule):
    """Facade over :class:`cortana.__main__.App`. Lifecycle is a no-op; only
    :meth:`health` delegates to the live health reporter."""

    name = "cortana"
    critical = True

    def __init__(self, app: Any) -> None:
        self._app = app

    def enabled(self, cfg: AuraConfig) -> bool:
        # CORTANA is the reason the process exists — always on.
        return True

    async def setup(self, ctx: ModuleContext) -> None:
        # App.setup() already built and wired every voice component.
        return None

    async def start(self, ctx: ModuleContext) -> None:
        # App._run_async already _spawn-ed the six voice tasks (fatal tier).
        return None

    async def stop(self) -> None:
        # App._shutdown_sequence owns the (unchanged) no-leave voice teardown.
        return None

    def health(self) -> ModuleHealth:
        """Read the live HealthReporter. Absent (pre-setup) → STARTING; a down
        Ears / offline voice / degraded pipeline → DEGRADED; otherwise OK."""
        reporter = getattr(self._app, "health", None)
        if reporter is None:
            return ModuleHealth(ModuleStatus.STARTING, "voice pipeline coming up")
        try:
            ears_down = bool(reporter.ears_down())
            voice_offline = bool(reporter.voice_offline())
            degraded = bool(reporter.degraded())
        except Exception:
            return ModuleHealth(ModuleStatus.OK, "voice up")
        metrics: dict[str, object] = {
            "ears": "down" if ears_down else "up",
            "voice": "offline" if voice_offline else "online",
        }
        if ears_down:
            return ModuleHealth(ModuleStatus.DEGRADED, "Ears is not connected", metrics)
        if voice_offline or degraded:
            return ModuleHealth(ModuleStatus.DEGRADED, "voice pipeline degraded", metrics)
        return ModuleHealth(ModuleStatus.OK, "voice up", metrics)


__all__ = ["CortanaModule"]
