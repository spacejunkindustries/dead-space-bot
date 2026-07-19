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
    ConfirmPending,
    DialogEvent,
    DialogSession,
    DialogState,
    DisarmWindow,
    Ev,
    LearnArea,
    Line,
    NoteRejected,
    PendingConfirm,
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
    PendingKind.CONFIRM: DialogState.AWAIT_CONFIRM,
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
            # A confirm-first report commits on silence (GDD §8.6: a
            # distress call is never lost to an unanswered question) —
            # only an explicit decline/dismissal retracts it.
            if (
                s.state is DialogState.AWAIT_CONFIRM
                and s.ctx_confirm is not None
                and s.ctx_confirm.commit_on_timeout
            ):
                return TransitionResult(s.idle(), (DisarmWindow(), ConfirmPending(s.ctx_confirm)))
            return TransitionResult(s.idle(), (DisarmWindow(),))
        return TransitionResult(s)

    if ev.kind is Ev.CAPTURE_EMITTED:
        if s.state is DialogState.LISTENING and ev.gen == s.gen:
            return TransitionResult(
                dataclasses.replace(s, state=DialogState.THINKING), (RunStt(s.gen),)
            )
        return TransitionResult(s)

    if ev.kind is Ev.CAPTURE_ABANDONED:
        # Zero speech after the capture opened: discard silently — nothing
        # reaches STT. For a WAKE capture (no pending context) that ends the
        # dialog free of charge. Inside a subdialog, a VAD noise blip that
        # opened-then-abandoned the window must NOT silently strand the
        # pilot's pending context (review finding): re-arm through the one
        # budgeted door instead, silent (no prompt — nothing was heard).
        if s.state is DialogState.LISTENING and ev.gen == s.gen:
            if s.pending is not PendingKind.NONE:
                return _fail(s, None, keep_ctx=True)
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

    if ev.kind is Ev.ENGINE_ASKED:
        # The engine asked to confirm (MEDIUM-tier match, or a confirm-first
        # report): arm one wake-free window carrying the pending command +
        # candidate, so an affirmative or an exact repeat can complete the
        # confirm by voice instead of dead-ending. Same budgeted door as
        # every other window; no prompt — the readback was it.
        if s.state is DialogState.IDLE and ev.confirm is not None:
            pend = dataclasses.replace(s, pending=PendingKind.CONFIRM, ctx_confirm=ev.confirm)
            res = _fail(pend, None, keep_ctx=True)
            if ev.confirm.commit_on_timeout and res.session.state is DialogState.IDLE:
                # No budget left for a confirm window: commit outright —
                # "standing down" must never eat an unposted distress call.
                return TransitionResult(res.session, (ConfirmPending(ev.confirm),))
            return res
        return TransitionResult(s)

    if ev.kind is Ev.CLASSIFIED:
        if s.state is DialogState.THINKING and ev.gen == s.gen and ev.classified is not None:
            return _classified(s, ev.classified)
        return TransitionResult(s)

    return TransitionResult(s)  # pragma: no cover — exhaustive above


# ── the routing table for one transcript ─────────────────────────────────────


def _classified(s: DialogSession, c: Classified) -> TransitionResult:
    # -1. Dismissal is absolute: "end transmission" / "disregard" / "never
    #     mind" closes the dialog from ANY point — including a pending
    #     confirm-first report, which it retracts (an explicit abort fails
    #     closed; only silence commits). The audible ack tells the pilot the
    #     door actually shut (live complaint: no spoken way out of the
    #     say-again loop in heavy chatter).
    if c.dismissed:
        return TransitionResult(s.idle(), (DisarmWindow(), Speak(Line.STANDING_DOWN)))

    # 0. Confirm continuation (GDD §8.3): the window carries a pending
    #    command + candidate — complete it, decline it, or close. It never
    #    re-asks, so the ask-loop the old flow dead-ended in cannot form.
    if s.pending is PendingKind.CONFIRM and s.ctx_confirm is not None:
        if s.ctx_confirm.learn_word is not None:
            return _learn_confirm_continuation(s, s.ctx_confirm, c)
        return _confirm_continuation(s, s.ctx_confirm, c)

    # 1. Override continuation: the whole utterance IS the question.
    if s.pending is PendingKind.OVERRIDE:
        query = c.override_query or c.relay_text or c.text
        if c.garbage:
            # Chatter-quality noise in the question window: close silently
            # instead of re-prompting into an open mic (the retry loop).
            return TransitionResult(s.idle(), (NoteRejected("garbage_dropped"),))
        if not c.confident:
            # Noise decoded inside the window must not become a paid API
            # call (live complaint: hallucinated questions burned cooldown).
            return _fail(s, Line.SAY_AGAIN, keep_ctx=True)
        return TransitionResult(s.idle(), (RunOverride(query),))

    # 2. Explicit "command override <question>" prefix. Confidence-gated
    #    exactly like the continuation path: a hallucinated decode that
    #    happens to start with the prefix must not burn a paid API call.
    if c.override_query is not None:
        if not c.confident:
            return _fail(s, Line.SAY_AGAIN, keep_ctx=True)
        if not c.chat_available:
            return TransitionResult(s.idle(), (Speak(Line.OVERRIDE_UNAVAILABLE),))
        return TransitionResult(s.idle(), (RunOverride(c.override_query),))

    # 3. Bare "command override" — open the question subdialog.
    if c.bare_override:
        if not c.chat_available:
            return TransitionResult(s.idle(), (Speak(Line.OVERRIDE_UNAVAILABLE),))
        return _open_subdialog(s, PendingKind.OVERRIDE, Speak(Line.GO_AHEAD))

    # 4. A full grammar command. The garbage-gate verdict rides along so the
    #    engine's learn-a-word gate (GDD §8.5a) never offers to remember a
    #    place from a chatter-quality transcript.
    if c.parsed is not None:
        inherited = s.ctx_severity if s.pending is PendingKind.SEVERITY else None
        return TransitionResult(
            s.idle(),
            (Report(c.parsed, inherited=inherited, source_confident=not c.garbage),),
        )

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
    #    Chatter-quality garbage (below dialog.retry_min_logprob) that
    #    matched NOTHING above closes silently first: re-prompting "say
    #    again" into an open mic full of conversation just recaptures more
    #    conversation (the stuck-open loop, live complaint). Recognised
    #    commands and framed relays were already handled — this gates only
    #    the retry-prompt path.
    if c.garbage:
        return TransitionResult(s.idle(), (NoteRejected("garbage_dropped"),))
    inherited = s.ctx_severity if s.pending is PendingKind.SEVERITY else None
    if c.relay_mode == "off":
        return _rejected_fail(s, "relay_off", Line.NOT_UNDERSTOOD, keep_ctx=False)
    # ctx worth keeping through a retry: an inherited severity OR a LOW-tier
    # rebind — one garbled decode must not permanently kill either (review
    # finding: ctx_retry used to be dropped here).
    keep = s.pending in (PendingKind.SEVERITY, PendingKind.RETRY_SYSTEM)
    if c.relay_mode == "framed" and not c.framed:
        return _rejected_fail(s, "relay_unframed", Line.NOT_UNDERSTOOD, keep_ctx=keep)
    if not c.confident:
        return _rejected_fail(s, "relay_low_confidence", Line.SAY_AGAIN, keep_ctx=keep)
    if len(c.relay_text) < 3:
        return TransitionResult(s.idle(), (NoteRejected("relay_too_short"),))
    return TransitionResult(s.idle(), (Relay(c.relay_text, severity=inherited, framed=c.framed),))


def _confirm_continuation(s: DialogSession, ctx: PendingConfirm, c: Classified) -> TransitionResult:
    """One utterance into an AWAIT_CONFIRM window (GDD §8.3).

    Completes on a confident standalone affirmative, on a repeat that parses
    to the same intent + system, or on a confident bare reply naming the
    candidate.

    On everything else the two confirm flavours diverge:

    - **Card/destructive confirm** (``commit_on_timeout=False``): a negative
      or unmatched speech closes silently — a destructive confirm fails
      closed; it is never guessed from an unmatched utterance.
    - **Confirm-first report** (``commit_on_timeout=True``): the report is
      not posted yet and must not be LOST — only an explicit decline (or a
      dismissal, handled above) retracts it; a decline opens a say-again
      window re-bound to the intent so the pilot can re-say the system.
      Unmatched speech or chatter commits, exactly like the timeout.
    """
    if c.confirm_reply == "no":
        if ctx.commit_on_timeout:
            # Retract the unposted report, then offer the system retry —
            # the pilot said "no" because the name was wrong.
            retry = dataclasses.replace(
                s, pending=PendingKind.RETRY_SYSTEM, ctx_retry=ctx.parsed, ctx_confirm=None
            )
            return _fail(retry, Line.SAY_AGAIN, keep_ctx=True)
        return TransitionResult(s.idle(), (NoteRejected("confirm_declined"),))
    if c.confirm_reply == "yes" and c.confident:
        # Short affirmatives are exactly what Whisper hallucinates from
        # noise, so "yes" is confidence-gated like the override path.
        return TransitionResult(s.idle(), (ConfirmPending(ctx),))
    heard = {(ctx.parsed.system_text or "").casefold()} - {""}
    if ctx.candidate is not None:
        heard.add(ctx.candidate.name.casefold())
    if (
        c.parsed is not None
        and c.parsed.intent is ctx.parsed.intent
        and (c.parsed.system_text or "").casefold() in heard
    ):
        # "Say again to confirm" taken literally: the repeated command names
        # the same system — that IS the confirmation.
        return TransitionResult(s.idle(), (ConfirmPending(ctx),))
    if c.confident and c.system_reply is not None and c.system_reply.casefold() in heard:
        # A bare repeat of just the system name confirms too.
        return TransitionResult(s.idle(), (ConfirmPending(ctx),))
    if ctx.commit_on_timeout:
        return TransitionResult(s.idle(), (ConfirmPending(ctx),))
    return TransitionResult(s.idle(), (NoteRejected("confirm_unmatched"),))


def _learn_confirm_continuation(
    s: DialogSession, ctx: PendingConfirm, c: Classified
) -> TransitionResult:
    """One utterance into a "Did you say <word>?" learn confirm (GDD §8.5a).

    Learning happens ONLY on a confident explicit yes (via ``LearnArea``);
    every other exit posts the report verbatim and remembers nothing, so a
    distress call is never lost and a mishearing never becomes a saved place:

    - **"yes"** (confident): learn the word, then post under it.
    - **"no, it's X"** (confident, non-garbage correction): rebind the report
      to X — a real system is used and the misheard word discarded; an unknown
      X re-enters the learn flow. The rebind carries no ``rebound_from`` so X
      can itself be learned (unlike a §8.3 LOW rebind, which must not teach).
    - **bare "no"**: open the say-again system retry, learning nothing.
    - **timeout / unmatched / low-confidence "yes"**: commit the report
      verbatim (``ConfirmPending``) — never learn (an unconfirmed word is not a
      place). ``commit_on_timeout`` is always True here.
    """
    if c.confirm_reply == "yes" and c.confident:
        return TransitionResult(s.idle(), (LearnArea(ctx.parsed, ctx.learn_word or ""),))
    if c.confirm_reply == "no":
        if c.correction_text and c.confident and not c.garbage:
            rebound = dataclasses.replace(ctx.parsed, system_text=c.correction_text, raw=c.text)
            return TransitionResult(s.idle(), (Report(rebound),))
        retry = dataclasses.replace(
            s, pending=PendingKind.RETRY_SYSTEM, ctx_retry=ctx.parsed, ctx_confirm=None
        )
        return _fail(retry, Line.SAY_AGAIN, keep_ctx=True)
    if ctx.commit_on_timeout:
        return TransitionResult(s.idle(), (ConfirmPending(ctx),))
    return TransitionResult(s.idle(), (NoteRejected("learn_unmatched"),))


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
        ctx_confirm=s.ctx_confirm if keep_ctx else None,
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
