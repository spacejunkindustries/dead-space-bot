"""Voice dialog engine — GDD §5.4 (state machine), §8.3, §6.4, §6.6.

One per-user :class:`~cortana.dialog.types.DialogSession` driven by the pure
:func:`~cortana.dialog.machine.transition` function, executed against the
world by :class:`~cortana.dialog.engine.DialogEngine`. Every deadline lives on
one monotonic clock in the engine's wheel; a wake-free capture window can only
be armed through the machine's single budgeted ``fail()``/subdialog door.

This package replaced five independent wall-clock dicts plus a frame-counted
window whose halves expired independently — the structural cause of the
say-again loops, stuck-open captures, and mid-question interruptions the
deployment suffered (GDD §5.4 history note).
"""

from cortana.dialog.engine import DialogEngine
from cortana.dialog.types import DialogState

__all__ = ["DialogEngine", "DialogState"]
