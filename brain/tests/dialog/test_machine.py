"""Table tests for the pure dialog machine (GDD §5.4) plus regression
replays of the three live incident classes it exists to kill:

- incident 2: mid-question "Say again?" interruptions during override dialogs
  (context expiring mid-flight / windows racing STT latency),
- incident 5: the say-again infinite loop (every failure path must drain ONE
  budget and terminate audibly),
- incident 9: DTX-frozen wake-free windows staying open forever.
"""

from __future__ import annotations

import dataclasses

from cortana.dialog.machine import transition
from cortana.dialog.types import (
    ArmWindow,
    Classified,
    ConfirmPending,
    DialogEvent,
    DialogSession,
    DialogState,
    DisarmWindow,
    Ev,
    Line,
    NoteRejected,
    PendingConfirm,
    Relay,
    Report,
    RunOverride,
    RunStt,
    Speak,
)
from cortana.types import Intent, MatchCandidate, ParsedCommand, Severity

MAX_RETRIES = 2


def idle(**kw) -> DialogSession:
    return DialogSession(user_id=1, guild_id=9, **kw)


def step(s: DialogSession, ev: DialogEvent):
    res = transition(s, ev, MAX_RETRIES)
    return res.session, list(res.actions)


def wake(s: DialogSession):
    return step(s, DialogEvent(Ev.WAKE_HIT))


def classified(s: DialogSession, c: Classified):
    return step(s, DialogEvent(Ev.CLASSIFIED, gen=s.gen, classified=c))


def c(**kw) -> Classified:
    defaults = dict(text="x", confident=True, relay_mode="framed")
    defaults.update(kw)
    return Classified(**defaults)


def parsed_cmd(**kw) -> ParsedCommand:
    defaults = dict(
        intent=Intent.HOSTILE_SPOTTED,
        raw="hostiles umi",
        system_text="umi",
        group_alias=None,
        detail=None,
    )
    defaults.update(kw)
    return ParsedCommand(**defaults)


def kinds(actions) -> list[type]:
    return [type(a) for a in actions]


# ── the happy path ───────────────────────────────────────────────────────────


def test_wake_opens_fresh_dialog_with_full_budget() -> None:
    s, actions = wake(idle())
    assert s.state is DialogState.LISTENING
    assert s.retries_left == MAX_RETRIES
    assert s.gen == 1
    assert actions == [Speak(Line.ACK)]


def test_capture_emitted_moves_to_thinking_and_runs_stt() -> None:
    s, _ = wake(idle())
    s2, actions = step(s, DialogEvent(Ev.CAPTURE_EMITTED, gen=s.gen))
    assert s2.state is DialogState.THINKING
    assert actions == [RunStt(s.gen)]


def test_full_command_reports_and_returns_to_idle() -> None:
    s, _ = wake(idle())
    s, _ = step(s, DialogEvent(Ev.CAPTURE_EMITTED, gen=s.gen))
    p = parsed_cmd()
    s2, actions = classified(s, c(parsed=p))
    assert s2.state is DialogState.IDLE
    assert actions == [Report(p)]
    # Budget untouched by success:
    assert s2.retries_left == MAX_RETRIES


def test_abandoned_capture_is_silent_and_free() -> None:
    s, _ = wake(idle())
    s2, actions = step(s, DialogEvent(Ev.CAPTURE_ABANDONED, gen=s.gen))
    assert s2.state is DialogState.IDLE
    assert actions == []  # nothing spoken, no STT, no retry spent
    assert s2.retries_left == MAX_RETRIES


# ── the single failure door (incident 5) ─────────────────────────────────────


def _to_thinking(s: DialogSession) -> DialogSession:
    s, _ = step(s, DialogEvent(Ev.CAPTURE_EMITTED, gen=s.gen))
    return s


def test_every_failure_path_arms_at_most_budget_windows_then_stands_down() -> None:
    """The incident-5 replay: no failure sequence, of ANY composition, can
    arm more windows than the per-dialog budget, and exhaustion is audible."""
    failure_events = [
        ("stt_failed", lambda s: step(s, DialogEvent(Ev.STT_FAILED, gen=s.gen))),
        ("unframed", lambda s: classified(s, c(framed=False))),
        ("low_conf", lambda s: classified(s, c(framed=True, confident=False))),
    ]
    for name, fail_once in failure_events:
        s, _ = wake(idle())
        arm_count = 0
        for _ in range(10):  # noise bursts forever — must terminate
            s = _to_thinking(s)
            s, actions = fail_once(s)
            arms = [a for a in actions if isinstance(a, ArmWindow)]
            arm_count += len(arms)
            if not arms:
                assert Speak(Line.STANDING_DOWN) in actions, name
                assert s.state is DialogState.IDLE, name
                break
            # window opened again (simulating more noise):
            s, _ = step(s, DialogEvent(Ev.WINDOW_OPENED, gen=s.gen))
        assert arm_count == MAX_RETRIES, name


def test_stt_failure_without_budget_stands_down_audibly() -> None:
    # The SttError path bypassed the loop guard in the live incident — it
    # must now flow through the same door.
    s, _ = wake(idle())
    s = dataclasses.replace(s, retries_left=0)
    s = _to_thinking(s)
    s2, actions = step(s, DialogEvent(Ev.STT_FAILED, gen=s.gen))
    assert s2.state is DialogState.IDLE
    assert Speak(Line.STANDING_DOWN) in actions
    assert ArmWindow not in kinds(actions)


def test_only_wake_refills_the_budget() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    s, _ = step(s, DialogEvent(Ev.STT_FAILED, gen=s.gen))  # spends one
    assert s.retries_left == MAX_RETRIES - 1
    # A successful window→command round does NOT refill:
    s, _ = step(s, DialogEvent(Ev.WINDOW_OPENED, gen=s.gen))
    s, _ = step(s, DialogEvent(Ev.CAPTURE_EMITTED, gen=s.gen))
    s, _ = classified(s, c(parsed=parsed_cmd()))
    assert s.retries_left == MAX_RETRIES - 1
    # A fresh wake does:
    s, _ = wake(s)
    assert s.retries_left == MAX_RETRIES


# ── windows expire on the wall clock (incident 9) ────────────────────────────


def test_deadline_closes_armed_window_silently() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    s, actions = step(s, DialogEvent(Ev.STT_FAILED, gen=s.gen))
    assert s.state is DialogState.AWAIT_REPEAT
    gen = s.gen
    s2, actions = step(s, DialogEvent(Ev.DEADLINE, gen=gen))
    assert s2.state is DialogState.IDLE
    assert DisarmWindow() in actions
    assert not any(isinstance(a, Speak) for a in actions)  # silent


def test_stale_deadline_from_superseded_dialog_is_ignored() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    s, _ = step(s, DialogEvent(Ev.STT_FAILED, gen=s.gen))
    old_gen = s.gen
    s, _ = wake(s)  # fresh dialog supersedes the window
    s2, actions = step(s, DialogEvent(Ev.DEADLINE, gen=old_gen))
    assert s2 == s
    assert actions == []


def test_stale_window_opened_is_ignored() -> None:
    s, _ = wake(idle())
    s2, actions = step(s, DialogEvent(Ev.WINDOW_OPENED, gen=99))
    assert s2 == s
    assert actions == []


# ── subdialogs: severity opener and override (incidents 2, 6-adjacent) ───────


def test_bare_code_opens_severity_subdialog() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    s2, actions = classified(s, c(bare_code=Severity.MEDIUM))
    assert s2.state is DialogState.AWAIT_SEVERITY_REPORT
    assert s2.ctx_severity is Severity.MEDIUM
    assert Speak(Line.CODE_ACK, Severity.MEDIUM) in actions
    assert ArmWindow(s2.gen) in actions


def test_severity_continuation_inherits_but_must_still_be_framed() -> None:
    """Inherited severity is NOT framing: hallucinated noise after a code
    opener must never become an ORANGE card (the live pollution incident)."""
    s, _ = wake(idle())
    s = _to_thinking(s)
    s, _ = classified(s, c(bare_code=Severity.MEDIUM))
    s, _ = step(s, DialogEvent(Ev.WINDOW_OPENED, gen=s.gen))
    s = _to_thinking(s)
    # Unframed continuation → fail, severity ctx retained for the retry:
    s2, actions = classified(s, c(framed=False))
    assert s2.state is DialogState.AWAIT_SEVERITY_REPORT
    assert s2.ctx_severity is Severity.MEDIUM
    # Framed continuation relays WITH the severity:
    s3, _ = step(s2, DialogEvent(Ev.WINDOW_OPENED, gen=s2.gen))
    s3 = _to_thinking(s3)
    s4, actions = classified(s3, c(framed=True, relay_text="camp at the gate"))
    assert s4.state is DialogState.IDLE
    assert actions == [Relay("camp at the gate", severity=Severity.MEDIUM, framed=True)]


def test_severity_attaches_to_a_parsed_report() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    s, _ = classified(s, c(bare_code=Severity.HIGH))
    s, _ = step(s, DialogEvent(Ev.WINDOW_OPENED, gen=s.gen))
    s = _to_thinking(s)
    p = parsed_cmd()
    s2, actions = classified(s, c(parsed=p))
    assert actions == [Report(p, inherited=Severity.HIGH)]


def test_bare_override_opens_question_subdialog_when_chat_up() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    s2, actions = classified(s, c(bare_override=True, chat_available=True))
    assert s2.state is DialogState.AWAIT_OVERRIDE_QUESTION
    assert Speak(Line.GO_AHEAD) in actions
    assert ArmWindow(s2.gen) in actions


def test_bare_override_with_chat_down_says_unavailable() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    s2, actions = classified(s, c(bare_override=True, chat_available=False))
    assert s2.state is DialogState.IDLE
    assert actions == [Speak(Line.OVERRIDE_UNAVAILABLE)]


def test_override_continuation_takes_whole_utterance_as_question() -> None:
    """Incident-2 replay: the question arrives as the window continuation and
    is consumed by ITS dialog — no TTL can expire it mid-flight because the
    context is bound to the session, not a wall-clock dict."""
    s, _ = wake(idle())
    s = _to_thinking(s)
    s, _ = classified(s, c(bare_override=True, chat_available=True))
    s, _ = step(s, DialogEvent(Ev.WINDOW_OPENED, gen=s.gen))
    s = _to_thinking(s)
    s2, actions = classified(s, c(relay_text="what is the weather in chicago", chat_available=True))
    assert s2.state is DialogState.IDLE
    assert actions == [RunOverride("what is the weather in chicago")]


def test_override_continuation_noise_never_burns_an_api_call() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    s, _ = classified(s, c(bare_override=True, chat_available=True))
    s, _ = step(s, DialogEvent(Ev.WINDOW_OPENED, gen=s.gen))
    s = _to_thinking(s)
    s2, actions = classified(s, c(confident=False, chat_available=True))
    assert not any(isinstance(a, RunOverride) for a in actions)
    # Budget path: one retry left after the subdialog spent one.
    assert s2.state in (DialogState.AWAIT_OVERRIDE_QUESTION, DialogState.IDLE)


def test_explicit_override_prefix_runs_directly() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    s2, actions = classified(s, c(override_query="status of the war", chat_available=True))
    assert s2.state is DialogState.IDLE
    assert actions == [RunOverride("status of the war")]


# ── LOW-tier retry rebind (GDD §8.3) ─────────────────────────────────────────


def test_engine_rejected_low_arms_retry_window() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    p = parsed_cmd()
    s, _ = classified(s, c(parsed=p))  # → IDLE + Report
    s2, actions = step(s, DialogEvent(Ev.ENGINE_REJECTED_LOW, parsed=p))
    assert s2.state is DialogState.AWAIT_RETRY_SYSTEM
    assert s2.ctx_retry == p
    assert any(isinstance(a, ArmWindow) for a in actions)
    # The engine's outcome utterance was already spoken — no extra line here.
    assert not any(isinstance(a, Speak) for a in actions)


def test_system_reply_rebinds_to_rejected_intent() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    p = parsed_cmd(system_text="otanumi")
    s, _ = classified(s, c(parsed=p))
    s, _ = step(s, DialogEvent(Ev.ENGINE_REJECTED_LOW, parsed=p))
    s, _ = step(s, DialogEvent(Ev.WINDOW_OPENED, gen=s.gen))
    s = _to_thinking(s)
    s2, actions = classified(s, c(text="otanuomi", system_reply="otanuomi"))
    assert len(actions) == 1 and isinstance(actions[0], Report)
    assert actions[0].parsed.system_text == "otanuomi"
    assert actions[0].parsed.intent is p.intent
    assert actions[0].rebound_from == p


# ── MEDIUM-tier confirm flow (GDD §8.3 AWAIT_CONFIRM) ────────────────────────


def pending_confirm(**kw) -> PendingConfirm:
    defaults = dict(
        parsed=parsed_cmd(intent=Intent.RESOLVE, raw="clear otanumi", system_text="otanumi"),
        candidate=MatchCandidate(1, "Otanuomi", 0.62),
        incident_id=None,
    )
    defaults.update(kw)
    return PendingConfirm(**defaults)


def to_await_confirm(conf: PendingConfirm):
    """Wake → command → engine ASKED: session parked in AWAIT_CONFIRM."""
    s, _ = wake(idle())
    s = _to_thinking(s)
    s, _ = classified(s, c(parsed=conf.parsed))  # → IDLE + Report
    s, actions = step(s, DialogEvent(Ev.ENGINE_ASKED, confirm=conf))
    return s, actions


def confirm_window_open(s: DialogSession) -> DialogSession:
    s, _ = step(s, DialogEvent(Ev.WINDOW_OPENED, gen=s.gen))
    return _to_thinking(s)


def test_engine_asked_arms_confirm_window_carrying_the_candidate() -> None:
    conf = pending_confirm()
    s, actions = to_await_confirm(conf)
    assert s.state is DialogState.AWAIT_CONFIRM
    assert s.ctx_confirm == conf
    assert ArmWindow(s.gen) in actions
    # The engine's "Heard X — say again to confirm." was the prompt.
    assert not any(isinstance(a, Speak) for a in actions)
    assert s.retries_left == MAX_RETRIES - 1  # through the one budgeted door


def test_engine_asked_without_budget_stands_down_audibly() -> None:
    s, _ = wake(idle())
    s = dataclasses.replace(s, retries_left=0)
    s = _to_thinking(s)
    s, _ = classified(s, c(parsed=parsed_cmd(intent=Intent.RESOLVE)))
    s2, actions = step(s, DialogEvent(Ev.ENGINE_ASKED, confirm=pending_confirm()))
    assert s2.state is DialogState.IDLE
    assert Speak(Line.STANDING_DOWN) in actions
    assert ArmWindow not in kinds(actions)


def test_engine_asked_ignored_outside_idle_or_without_payload() -> None:
    s, _ = wake(idle())  # LISTENING, not IDLE
    s2, actions = step(s, DialogEvent(Ev.ENGINE_ASKED, confirm=pending_confirm()))
    assert s2 == s and actions == []
    s3, _ = classified(_to_thinking(s), c(parsed=parsed_cmd()))  # back to IDLE
    s4, actions = step(s3, DialogEvent(Ev.ENGINE_ASKED))  # no payload
    assert s4 == s3 and actions == []


def test_affirmative_completes_the_confirm() -> None:
    conf = pending_confirm()
    s, _ = to_await_confirm(conf)
    s = confirm_window_open(s)
    s2, actions = classified(s, c(text="yes", confirm_reply="yes"))
    assert actions == [ConfirmPending(conf)]
    assert s2.state is DialogState.IDLE
    assert s2.ctx_confirm is None


def test_unconfident_affirmative_never_confirms() -> None:
    # A hallucinated "yes" must not resolve/schedule against a guessed
    # system: destructive confirms fail closed, silently.
    conf = pending_confirm()
    s, _ = to_await_confirm(conf)
    s = confirm_window_open(s)
    s2, actions = classified(s, c(text="yes", confirm_reply="yes", confident=False))
    assert not any(isinstance(a, ConfirmPending) for a in actions)
    assert kinds(actions) == [NoteRejected]
    assert s2.state is DialogState.IDLE


def test_negative_closes_silently_with_standing_down_semantics() -> None:
    conf = pending_confirm()
    s, _ = to_await_confirm(conf)
    s = confirm_window_open(s)
    s2, actions = classified(s, c(text="no", confirm_reply="no"))
    assert kinds(actions) == [NoteRejected]  # no Speak, no ArmWindow, no confirm
    assert s2.state is DialogState.IDLE
    assert s2.ctx_confirm is None


def test_exact_repeat_of_the_command_completes_the_confirm() -> None:
    conf = pending_confirm()
    s, _ = to_await_confirm(conf)
    s = confirm_window_open(s)
    repeat = parsed_cmd(intent=Intent.RESOLVE, raw="clear otanumi", system_text="otanumi")
    s2, actions = classified(s, c(text="clear otanumi", parsed=repeat))
    assert actions == [ConfirmPending(conf)]
    assert s2.state is DialogState.IDLE


def test_repeat_naming_the_candidate_completes_the_confirm() -> None:
    # The pilot repeats with the CANDIDATE's name ("clear Otanuomi") after
    # hearing the readback — same intent, confirmed system.
    conf = pending_confirm()
    s, _ = to_await_confirm(conf)
    s = confirm_window_open(s)
    repeat = parsed_cmd(intent=Intent.RESOLVE, raw="clear Otanuomi", system_text="Otanuomi")
    s2, actions = classified(s, c(text="clear Otanuomi", parsed=repeat))
    assert actions == [ConfirmPending(conf)]


def test_bare_candidate_name_reply_completes_the_confirm() -> None:
    conf = pending_confirm()
    s, _ = to_await_confirm(conf)
    s = confirm_window_open(s)
    s2, actions = classified(s, c(text="Otanuomi", system_reply="Otanuomi"))
    assert actions == [ConfirmPending(conf)]


def test_different_command_or_noise_closes_silently() -> None:
    conf = pending_confirm()
    for reply in (
        c(text="timer kisogo four hours", parsed=parsed_cmd(intent=Intent.TIMER)),
        c(text="clear kisogo", parsed=parsed_cmd(intent=Intent.RESOLVE, system_text="kisogo")),
        c(text="mumble static"),
    ):
        s, _ = to_await_confirm(conf)
        s = confirm_window_open(s)
        s2, actions = classified(s, reply)
        assert kinds(actions) == [NoteRejected], reply.text
        assert s2.state is DialogState.IDLE, reply.text


def test_confirm_window_deadline_expires_silently() -> None:
    s, _ = to_await_confirm(pending_confirm())
    s2, actions = step(s, DialogEvent(Ev.DEADLINE, gen=s.gen))
    assert s2.state is DialogState.IDLE
    assert DisarmWindow() in actions
    assert not any(isinstance(a, Speak) for a in actions)


def test_wake_during_confirm_supersedes_and_clears_context() -> None:
    s, _ = to_await_confirm(pending_confirm())
    s2, actions = wake(s)
    assert s2.state is DialogState.LISTENING
    assert s2.ctx_confirm is None
    assert s2.retries_left == MAX_RETRIES
    assert DisarmWindow() in actions


def test_abandoned_confirm_capture_rearms_keeping_context() -> None:
    # A VAD blip that opened-then-abandoned the window must not strand the
    # pending confirm: re-arm through the budget, silently.
    conf = pending_confirm()
    s, _ = to_await_confirm(conf)
    s, _ = step(s, DialogEvent(Ev.WINDOW_OPENED, gen=s.gen))
    s2, actions = step(s, DialogEvent(Ev.CAPTURE_ABANDONED, gen=s.gen))
    assert s2.state is DialogState.AWAIT_CONFIRM
    assert s2.ctx_confirm == conf
    assert ArmWindow(s2.gen) in actions
    assert s2.retries_left == MAX_RETRIES - 2


def test_stt_failure_in_confirm_window_keeps_context() -> None:
    conf = pending_confirm()
    s, _ = to_await_confirm(conf)
    s = confirm_window_open(s)
    s2, actions = step(s, DialogEvent(Ev.STT_FAILED, gen=s.gen))
    assert s2.state is DialogState.AWAIT_CONFIRM
    assert s2.ctx_confirm == conf
    assert Speak(Line.SAY_AGAIN) in actions


# ── relay gating ─────────────────────────────────────────────────────────────


def test_relay_mode_off_fails_instead_of_posting() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    s2, actions = classified(s, c(relay_mode="off", framed=True))
    assert not any(isinstance(a, Relay) for a in actions)


def test_open_mode_relays_unframed_speech() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    s2, actions = classified(s, c(relay_mode="open", framed=False, relay_text="moving to umi now"))
    assert actions == [Relay("moving to umi now", severity=None, framed=False)]


def test_short_relay_is_rejected_silently() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    s2, actions = classified(s, c(framed=True, relay_text="ok"))
    assert kinds(actions) == [NoteRejected]
    assert s2.state is DialogState.IDLE


# ── cleanup semantics ────────────────────────────────────────────────────────


def test_reset_from_any_state_lands_idle_and_disarms() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    s, _ = classified(s, c(bare_code=Severity.MEDIUM))
    s2, actions = step(s, DialogEvent(Ev.RESET))
    assert s2.state is DialogState.IDLE
    assert s2.ctx_severity is None
    assert DisarmWindow() in actions


def test_wake_during_await_supersedes_the_window() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    s, _ = classified(s, c(bare_code=Severity.MEDIUM))
    assert s.state is DialogState.AWAIT_SEVERITY_REPORT
    s2, actions = wake(s)
    assert s2.state is DialogState.LISTENING
    assert s2.ctx_severity is None
    assert s2.retries_left == MAX_RETRIES
    assert DisarmWindow() in actions


def test_stale_gen_capture_events_are_dropped() -> None:
    s, _ = wake(idle())
    s2, actions = step(s, DialogEvent(Ev.CAPTURE_EMITTED, gen=s.gen + 5))
    assert s2 == s
    assert actions == []


# ── dismissal: "end transmission" is an absolute exit ────────────────────────


def test_dismissal_from_thinking_stands_down() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    s2, actions = classified(s, c(text="end transmission", dismissed=True))
    assert s2.state is DialogState.IDLE
    assert Speak(Line.STANDING_DOWN) in actions
    assert DisarmWindow in kinds(actions)
    assert Report not in kinds(actions) and ArmWindow not in kinds(actions)


def test_dismissal_kills_pending_subdialog_context() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    s, _ = classified(s, c(bare_code=Severity.MEDIUM))  # severity subdialog armed
    s = confirm_window_open(s)
    s2, actions = classified(s, c(text="never mind", dismissed=True))
    assert s2.state is DialogState.IDLE
    assert s2.ctx_severity is None
    assert Speak(Line.STANDING_DOWN) in actions


def test_dismissal_retracts_a_pending_confirm_first_report() -> None:
    # An explicit abort fails closed — the unposted report dies with it.
    conf = pending_confirm(commit_on_timeout=True, candidate=None)
    s, _ = to_await_confirm(conf)
    s = confirm_window_open(s)
    s2, actions = classified(s, c(text="disregard", dismissed=True))
    assert s2.state is DialogState.IDLE
    assert not any(isinstance(a, ConfirmPending) for a in actions)
    assert Speak(Line.STANDING_DOWN) in actions


# ── the garbage gate: chatter never earns a retry prompt ─────────────────────


def test_garbage_unmatched_closes_silently_without_a_window() -> None:
    # The stuck-open loop: say-again into an open mic recaptures chatter.
    s, _ = wake(idle())
    s = _to_thinking(s)
    s2, actions = classified(s, c(text="mumble chatter", confident=False, garbage=True))
    assert s2.state is DialogState.IDLE
    assert kinds(actions) == [NoteRejected]  # silent: no Speak, no ArmWindow


def test_garbage_in_override_window_closes_silently() -> None:
    s, _ = wake(idle())
    s = _to_thinking(s)
    s, _ = classified(s, c(bare_override=True, chat_available=True))
    s = confirm_window_open(s)
    s2, actions = classified(s, c(text="crowd noise", confident=False, garbage=True))
    assert s2.state is DialogState.IDLE
    assert kinds(actions) == [NoteRejected]
    assert not any(isinstance(a, RunOverride) for a in actions)


def test_garbage_never_gates_a_recognised_command() -> None:
    # A quiet mic's distress call still posts (GDD §8.6).
    s, _ = wake(idle())
    s = _to_thinking(s)
    p = parsed_cmd(intent=Intent.UNDER_ATTACK, system_text="Otanuomi")
    s2, actions = classified(s, c(parsed=p, confident=False, garbage=True))
    assert any(isinstance(a, Report) for a in actions)


# ── confirm-first reports (dialog.confirm_reports) ───────────────────────────


def _report_confirm(**kw) -> PendingConfirm:
    defaults = dict(
        parsed=parsed_cmd(intent=Intent.UNDER_ATTACK, system_text="moee 8", raw="tackled moee 8"),
        candidate=None,
        incident_id=None,
        commit_on_timeout=True,
    )
    defaults.update(kw)
    return PendingConfirm(**defaults)


def test_confirm_first_yes_commits() -> None:
    conf = _report_confirm()
    s, _ = to_await_confirm(conf)
    s = confirm_window_open(s)
    s2, actions = classified(s, c(text="yes", confirm_reply="yes"))
    assert actions == [ConfirmPending(conf)]
    assert s2.state is DialogState.IDLE


def test_confirm_first_timeout_commits_the_report() -> None:
    # Silence must never eat an unposted distress call (GDD §8.6).
    conf = _report_confirm()
    s, _ = to_await_confirm(conf)
    s2, actions = step(s, DialogEvent(Ev.DEADLINE, gen=s.gen))
    assert ConfirmPending(conf) in actions
    assert s2.state is DialogState.IDLE


def test_confirm_first_unmatched_speech_commits() -> None:
    conf = _report_confirm()
    s, _ = to_await_confirm(conf)
    s = confirm_window_open(s)
    s2, actions = classified(s, c(text="unrelated chatter", confident=False, garbage=True))
    assert ConfirmPending(conf) in actions
    assert s2.state is DialogState.IDLE


def test_confirm_first_no_opens_system_retry() -> None:
    conf = _report_confirm()
    s, _ = to_await_confirm(conf)
    s = confirm_window_open(s)
    s2, actions = classified(s, c(text="no", confirm_reply="no"))
    assert s2.state is DialogState.AWAIT_RETRY_SYSTEM
    assert s2.ctx_retry == conf.parsed
    assert Speak(Line.SAY_AGAIN) in actions and ArmWindow(s2.gen) in actions
    assert not any(isinstance(a, ConfirmPending) for a in actions)


def test_confirm_first_without_budget_commits_instead_of_standing_down() -> None:
    s, _ = wake(idle())
    s = dataclasses.replace(s, retries_left=0)
    s = _to_thinking(s)
    s, _ = classified(s, c(parsed=parsed_cmd(intent=Intent.UNDER_ATTACK)))
    conf = _report_confirm()
    s2, actions = step(s, DialogEvent(Ev.ENGINE_ASKED, confirm=conf))
    assert s2.state is DialogState.IDLE
    assert ConfirmPending(conf) in actions
    assert Speak(Line.STANDING_DOWN) not in actions


def test_card_confirm_timeout_still_closes_silently() -> None:
    # The pre-existing flavour (card already posted / destructive) is
    # unchanged: timeout closes, commits nothing.
    conf = pending_confirm()  # commit_on_timeout=False
    s, _ = to_await_confirm(conf)
    s2, actions = step(s, DialogEvent(Ev.DEADLINE, gen=s.gen))
    assert not any(isinstance(a, ConfirmPending) for a in actions)
    assert s2.state is DialogState.IDLE
