"""The config reload transaction — one door for SIGHUP and ``/reload``.

``systemctl reload cortana-brain`` used to re-read one file, give the
operator zero feedback, and silently absorb restart-bound edits. This module
makes reload a transaction over ALL config inputs:

1. **validate** — ``cortana.yaml`` through :func:`cortana.config.load_config`,
   plus any registered extra file validators (gazetteer.yaml, routing.yaml)
   against the candidate config;
2. **swap all-or-nothing** — any validation failure keeps every old config
   in force and reports the rejection; nothing is half-applied;
3. **apply by reload class** — hot keys are live the moment the holder swaps
   (consumers read ``holder.current`` at the point of use); ``sighup``-class
   keys run their registered applier (``set_personality()``, ChatClient
   rebuild); ``engine``-class inputs run their registered engine reloaders
   (gazetteer / routing rebuilds — always, because those FILES may have
   changed even when no ``cortana.yaml`` key did);
4. **report** — a :class:`ReloadResult` receipt listing applied /
   engine-reloaded / restart-pending / rejected, rendered phone-readable for
   the health channel. Restart-bound edits are never silently absorbed.

Wiring (who passes which validators/reloaders/appliers) belongs to the
composition root; this module stays mechanism-only so the transaction is
testable with fakes.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

import structlog

from cortana.config import AuraConfig, ConfigError, ConfigHolder, diff_configs
from cortana.config_schema import Reload

__all__ = ["EngineReloader", "KeyApplier", "ReloadResult", "reload_all"]

log = structlog.get_logger(__name__)

#: Rebuilds an engine (gazetteer, routing) against the new config. May be
#: sync or async; raising rejects that engine and is reported, but the
#: config swap has already happened (the files were validated up front, so a
#: reloader failure is an engine bug, not an operator typo).
EngineReloader = Callable[[AuraConfig], Awaitable[None] | None]

#: Applies a sighup-class key change (e.g. ``set_personality``). Keyed by a
#: key-path prefix; run once when any changed key matches. May be sync or
#: async.
KeyApplier = Callable[[AuraConfig], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class ReloadResult:
    """The operator-facing receipt of one reload transaction.

    ``ok`` is False when the transaction rejected the new config (nothing
    swapped) or an engine reloader failed after the swap; ``rejected``
    carries the reasons either way.
    """

    ok: bool
    #: hot + sighup key paths whose values changed and are now live.
    applied: tuple[str, ...] = ()
    #: Engines rebuilt (by name), plus any engine-class keys that changed.
    engine_reload: tuple[str, ...] = ()
    #: restart-class key paths that changed — recorded, NOT applied.
    restart_pending: tuple[str, ...] = ()
    #: Validation errors (config kept) or engine-reload failures (config
    #: swapped, engine stale).
    rejected: tuple[str, ...] = ()
    #: True when the swap happened (even if an engine reloader then failed).
    swapped: bool = field(default=False)

    def lines(self) -> list[str]:
        """Phone-readable receipt lines for the health channel."""
        out: list[str] = []
        if not self.swapped:
            out.append("Reload REJECTED — old config stays in force.")
        elif self.ok:
            out.append("Reload applied.")
        else:
            out.append("Reload applied WITH ERRORS.")
        if self.applied:
            out.append("Applied: " + ", ".join(self.applied))
        if self.engine_reload:
            out.append("Engines reloaded: " + ", ".join(self.engine_reload))
        if self.restart_pending:
            out.append(
                "Restart pending (edit recorded, takes effect on restart): "
                + ", ".join(self.restart_pending)
            )
        if self.rejected:
            out.extend("Rejected: " + reason for reason in self.rejected)
        if len(out) == 1 and self.swapped and self.ok:
            out.append("No changes detected.")
        return out

    def summary(self) -> str:
        return "\n".join(self.lines())


async def _call(fn: Callable[[AuraConfig], Awaitable[None] | None], cfg: AuraConfig) -> None:
    result = fn(cfg)
    if inspect.isawaitable(result):
        await result


async def reload_all(
    holder: ConfigHolder,
    *,
    file_validators: Mapping[str, Callable[[AuraConfig], None]] | None = None,
    engine_reloaders: Mapping[str, EngineReloader] | None = None,
    appliers: Mapping[str, KeyApplier] | None = None,
) -> ReloadResult:
    """Run one reload transaction against every config input.

    ``file_validators`` — name → blocking callable validating an extra
    config file (gazetteer.yaml, routing.yaml) against the CANDIDATE config;
    raising rejects the whole transaction (run via ``asyncio.to_thread``).

    ``engine_reloaders`` — engine name → callable rebuilding that engine
    against the new config. Always run after a successful swap: the engine's
    backing FILE may have changed even when no ``cortana.yaml`` key did.

    ``appliers`` — key-path prefix → callable applying a ``sighup``-class
    change (e.g. ``"tts.personality"`` → ``set_personality``). Run only when
    a changed key matches the prefix.

    Never raises for operator-caused problems; the receipt carries them.
    """
    validators = dict(file_validators or {})
    reloaders = dict(engine_reloaders or {})
    key_appliers = dict(appliers or {})

    old = holder.current

    # 1. Validate everything against the candidate config. Any failure keeps
    #    every old config in force — all-or-nothing.
    try:
        new = await asyncio.to_thread(_load, holder)
    except ConfigError as exc:
        log.warning("reload_rejected", file="cortana.yaml", error=str(exc))
        return ReloadResult(ok=False, rejected=(f"cortana.yaml: {exc}",))

    rejected: list[str] = []
    for name, validate in validators.items():
        try:
            await asyncio.to_thread(validate, new)
        except Exception as exc:  # noqa: BLE001 — every validator failure is a rejection
            log.warning("reload_rejected", file=name, error=str(exc))
            rejected.append(f"{name}: {exc}")
    if rejected:
        return ReloadResult(ok=False, rejected=tuple(rejected))

    # 2. Swap all-or-nothing. Hot keys are live from here on.
    holder.replace(new)
    buckets = diff_configs(old, new)
    applied = buckets[Reload.HOT] + buckets[Reload.SIGHUP]
    restart_pending = buckets[Reload.RESTART]
    for path in restart_pending:
        log.warning("config_restart_pending", key=path)

    # 3. Run sighup-class appliers for changed keys.
    failures: list[str] = []
    changed = set(applied)
    for prefix, applier in key_appliers.items():
        if any(path == prefix or path.startswith(prefix + ".") for path in changed):
            try:
                await _call(applier, new)
            except Exception as exc:  # noqa: BLE001 — report, don't crash the reload
                log.error("reload_applier_failed", prefix=prefix, error=str(exc))
                failures.append(f"applier {prefix}: {exc}")

    # 4. Rebuild engines — unconditionally, because their files may have
    #    changed without any cortana.yaml key changing.
    reloaded: list[str] = []
    for name, reloader in reloaders.items():
        try:
            await _call(reloader, new)
            reloaded.append(name)
        except Exception as exc:  # noqa: BLE001 — engine stays stale; say so
            log.error("reload_engine_failed", engine=name, error=str(exc))
            failures.append(f"engine {name}: {exc}")
    engine_keys = [p for p in buckets[Reload.ENGINE] if p not in reloaded]

    result = ReloadResult(
        ok=not failures,
        applied=applied,
        engine_reload=tuple(reloaded + engine_keys),
        restart_pending=restart_pending,
        rejected=tuple(failures),
        swapped=True,
    )
    log.info(
        "reload_complete",
        ok=result.ok,
        applied=list(result.applied),
        engines=list(result.engine_reload),
        restart_pending=list(result.restart_pending),
        rejected=list(result.rejected),
    )
    return result


def _load(holder: ConfigHolder) -> AuraConfig:
    from cortana.config import load_config

    return load_config(holder.path)
