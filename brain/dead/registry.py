"""The module roster — the one place that names which modules exist.

Order is start order: CORTANA (critical) first, add-ons after. Add-on imports
are guarded so a broken or absent add-on package can never stop the voice bot
from booting — a failed import is logged and the module is simply skipped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from dead.module import BotModule

log = structlog.get_logger(__name__)


def build_modules(app: Any) -> tuple[BotModule, ...]:
    """Instantiate every module for this process. ``app`` is the :class:`App`
    composition root, passed to the CORTANA facade so it can read the live
    voice components. Add-ons take no constructor args."""
    from cortana.cortana_module import CortanaModule

    modules: list[BotModule] = [CortanaModule(app)]

    try:
        from killboard.module import KillboardModule

        modules.append(KillboardModule())
    except Exception:
        # A broken add-on import must never keep the voice bot from booting.
        log.exception("addon_import_failed", module="killboard")

    return tuple(modules)


__all__ = ["build_modules"]
