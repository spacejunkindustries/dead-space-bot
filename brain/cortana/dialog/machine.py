"""The pure dialog transition function — GDD §5.4.

``transition(session, event, max_retries) -> TransitionResult`` is the ONLY
place dialog state may change, and its ``_fail``/``_open_subdialog`` helpers
are the ONLY producers of :class:`~cortana.dialog.types.ArmWindow`. Every
failure class — STT error, unmatched speech, low confidence, override noise —
drains the same per-dialog retry budget and terminates audibly with
"standing down" when it runs out. A fresh wake is the only thing that refills
the budget, which is what makes a self-sustaining reopen loop structurally
impossible (the live incident this module exists to kill).

No I/O, no clocks, no imports beyond the types module: the function is
exhaustively table-tested in ``brain/tests/dialog/test_machine.py``.
"""

from __future__ import annotations

import dataclasses

from cortana.dialog.types import (
    AWAIT_STATES,
    Action,
    ArmWindow,
    Classified,
    DialogEvent,
    DialogSession,
    DialogState,
    DisarmWindow,
    Ev,
    Line,
    NoteRejected,
    PendingKind,
    Relay,
    Report,
    RunOverride,
    RunStt,
    Speak,
    TransitionResult,
)

__all__ = ["transition"]

#: AWAIT state for each pending kind (REPEAT is the ctx-free fallback).
_AWAIT_FOR: dict[PendingKind, DialogState] = {
    PendingKind.RETRY_SYSTEM: DialogState.AWAIT_RETRY_SYSTEM,
    PendingKind.SEVERITY: DialogState.AWAIT_SEVERITY_REPORT,
    PendingKind.OVERRIDE: DialogState.AWAIT_OVERRIDE_QUESTION,
    PendingKind.REPEAT: DialogState.AWAIT_REPEAT,
    PendingKind.NONE: DialogState.AWAIT_REPEAT,
}


def transition(s: DialogSession, ev: DialogEvent, max_retries: int) -> TransitionResult:
    """Apply one event. Unknown/stale combinations are ignored unchanged —
    the engine logs them; the machine never guesses."""
    if ev.kind is Ev.RESET:
        return TransitionResult(s.idle(), (DisarmWindow(),))

    if ev.kind is Ev.WAKE_HIT:
        # A wake always starts a FRESH dialog: context cleared, budget
        # refilled. If a window was armed it is superseded.
        fresh = s.fresh(max_retries=max_retries)
        actions: tuple[Action, ...] = (Speak(Line.ACK),)
        if s.state in AWAIT_STATES:
            actions = (DisarmWindow(), Speak(Line.ACK))
        return TransitionResult(fresh, actions)

    if ev.kind is Ev.WINDOW_OPENED:
        if s.state in AWAIT_STATES and ev.gen == s.gen:
            # The prompt was the ack — no second chirp (the double-chirp
            # complaint from the reopen era).
            return TransitionResult(dataclasses.replace(s, state=DialogState.LISTENING))
        return TransitionResult(s)  # stale window from a superseded dialog

    if ev.kind is Ev.DEADLINE:
        # The armed window (or the AWAIT state guarding it) expired unused.
        if s.state in AWAIT_STATES and ev.gen == s.gen:
            return TransitionResult(s.idle(), (DisarmWindow(),))
        return TransitionResult(s)

    if ev.kind is Ev.CAPTURE_EMITTED:
        if s.state is DialogState.LISTENING and ev.gen == s.gen:
            return TransitionResult(
                dataclasses.replace(s, state=DialogState.THINKING), (RunStt(s.gen),)
            )
        return TransitionResult(s)

    if ev.kind is Ev.CAPTURE_ABANDONED:
        # Zero speech after the capture opened (wake-tail only): discard
        # silently — nothing reaches STT, no retry is consumed.
        if s.state is DialogState.LISTENING and ev.gen == s.gen:
            return TransitionResult(s.idle())
        return TransitionResult(s)

    if ev.kind is Ev.STT_FAILED:
        if s.state is DialogState.THINKING and ev.gen == s.gen:
            return _fail(s, Line.SAY_AGAIN, keep_ctx=True)
        return TransitionResult(s)

    if ev.kind is Ev.ENGINE_REJECTED_LOW:
        # The incident engine bounced a report whose system matched at LOW
        # tier (GDD §8.3): offer one wake-free retry that re-binds a bare
        # system name to the rejected intent.
        if s.state is DialogState.IDLE and ev.parsed is not None:
            retry = dataclasses.replace(s, pending=PendingKind.RETRY_SYSTEM, ctx_retry=ev.parsed)
            return _fail(retry, None, keep_ctx=True)
        return TransitionResult(s)

    if ev.kind is Ev.CLASSIFIED:
        if s.state is DialogState.THINKING and ev.gen == s.gen and ev.classified is not None:
            return _classified(s, ev.classified)
        return TransitionResult(s)

    return TransitionResult(s)  # pragma: no cover — exhaustive above


# ── the routing table for one transcript ─────────────────────────────────────


def _classified(s: DialogSession, c: Classified) -> TransitionResult:
    # 1. Override continuation: the whole utterance IS the question.
    if s.pending is PendingKind.OVERRIDE:
        query = c.override_query or c.relay_text or c.text
        if not c.confident:
            # Noise decoded inside the window must not become a paid API
            # call (live complaint: hallucinated questions burned cooldown).
            return _fail(s, Line.SAY_AGAIN, keep_ctx=True)
        return TransitionResult(s.idle(), (RunOverride(query),))

    # 2. Explicit "command override <question>" prefix.
    if c.override_query is not None:
        if not c.chat_available:
            return TransitionResult(s.idle(), (Speak(Line.OVERRIDE_UNAVAILABLE),))
        return TransitionResult(s.idle(), (RunOverride(c.override_query),))

    # 3. Bare "command override" — open the question subdialog.
    if c.bare_override:
        if not c.chat_available:
            return TransitionResult(s.idle(), (Speak(Line.OVERRIDE_UNAVAILABLE),))
        return _open_subdialog(s, PendingKind.OVERRIDE, Speak(Line.GO_AHEAD))

    # 4. A full grammar command.
    if c.parsed is not None:
        inherited = s.ctx_severity if s.pending is PendingKind.SEVERITY else None
        return TransitionResult(s.idle(), (Report(c.parsed, inherited=inherited),))

    # 5. LOW-retry continuation: a bare system name re-binds to the
    #    rejected command's intent (GDD §8.3).
    if s.pending is PendingKind.RETRY_SYSTEM and s.ctx_retry is not None and c.system_reply:
        rebound = dataclasses.replace(s.ctx_retry, system_text=c.system_reply, raw=c.text)
        return TransitionResult(s.idle(), (Report(rebound, rebound_from=s.ctx_retry),))

    # 6. Bare "code <colour>" — open the two-step report subdialog.
    if c.bare_code is not None:
        sub = dataclasses.replace(s, ctx_severity=c.bare_code)
        return _open_subdialog(sub, PendingKind.SEVERITY, Speak(Line.CODE_ACK, c.bare_code))

    # 7. Freeform relay (GDD §8.6). An inherited severity makes the relay
    #    severity-carrying but does NOT frame it: the continuation must still
    #    be framed speech, or hallucinated noise becomes an ORANGE card.
    inherited = s.ctx_severity if s.pending is PendingKind.SEVERITY else None
    if c.relay_mode == "off":
        return _rejected_fail(s, "relay_off", Line.NOT_UNDERSTOOD, keep_ctx=False)
    if c.relay_mode == "framed" and not c.framed:
        keep = s.pending is PendingKind.SEVERITY
        return _rejected_fail(s, "relay_unframed", Line.NOT_UNDERSTOOD, keep_ctx=keep)
    if not c.confident:
        keep = s.pending is PendingKind.SEVERITY
        return _rejected_fail(s, "relay_low_confidence", Line.SAY_AGAIN, keep_ctx=keep)
    if len(c.relay_text) < 3:
        return TransitionResult(s.idle(), (NoteRejected("relay_too_short"),))
    return TransitionResult(s.idle(), (Relay(c.relay_text, severity=inherited, framed=c.framed),))


def _rejected_fail(
    s: DialogSession, reason: str, line: Line, *, keep_ctx: bool
) -> TransitionResult:
    """A relay-gate rejection: counted in health, then through the one door."""
    res = _fail(s, line, keep_ctx=keep_ctx)
    return TransitionResult(res.session, (NoteRejected(reason), *res.actions))


# ── the single window-arming door ────────────────────────────────────────────


def _fail(s: DialogSession, line: Line | None, *, keep_ctx: bool) -> TransitionResult:
    """One failure: spend a retry and arm a window, or stand down audibly.

    THE only failure door. ``line`` is the prompt spoken with the window
    (None = the engine already spoke — ENGINE_REJECTED_LOW's outcome
    utterance covers it).
    """
    if s.retries_left <= 0:
        return TransitionResult(s.idle(), (Speak(Line.STANDING_DOWN),))
    pending = s.pending if (keep_ctx and s.pending is not PendingKind.NONE) else PendingKind.REPEAT
    nxt = dataclasses.replace(
        s,
        state=_AWAIT_FOR[pending],
        gen=s.gen + 1,
        retries_left=s.retries_left - 1,
        pending=pending,
        ctx_severity=s.ctx_severity if keep_ctx else None,
        ctx_retry=s.ctx_retry if keep_ctx else None,
    )
    actions: list[Action] = []
    if line is not None:
        actions.append(Speak(line))
    actions.append(ArmWindow(nxt.gen))
    return TransitionResult(nxt, tuple(actions))


def _open_subdialog(s: DialogSession, kind: PendingKind, prompt: Speak) -> TransitionResult:
    """Open a deliberate two-step dialog (severity opener / bare override).

    Draws on the same retry budget as failures: a dialog gets
    ``max_retries`` wake-free windows TOTAL, however they are spent, so
    chaining subdialogs can never extend a session indefinitely.
    """
    if s.retries_left <= 0:
        # Budget exhausted — close audibly rather than arming another window.
        return TransitionResult(s.idle(), (Speak(Line.STANDING_DOWN),))
    nxt = dataclasses.replace(
        s,
        state=_AWAIT_FOR[kind],
        gen=s.gen + 1,
        retries_left=s.retries_left - 1,
        pending=kind,
    )
    return TransitionResult(nxt, (prompt, ArmWindow(nxt.gen)))
