"""Conversation mode on the dialog wheel (GDD §6.8).

Two layers:

- **Machine** (pure): the residue branch, command preemption, the non-draining
  CONVERSE_ARM re-arm, DEADLINE/WAKE_HIT behaviour, and the additivity
  regression (conversation off ⇒ byte-for-byte the old path).
- **Engine** (async, fakes only, transcript-only — no audio): ``_converse``
  speaks the reply and re-arms, and NEVER touches the incident engine or a
  mention (the hard wall).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from cortana.config import ConversationConfig, DialogConfig
from cortana.dialog.engine import DialogEngine
from cortana.dialog.machine import transition
from cortana.dialog.types import (
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
    Report,
    RunChat,
    Speak,
)
from cortana.types import Intent, ParsedCommand

MAX_RETRIES = 2


# ── machine helpers (mirrors test_machine.py) ────────────────────────────────


def idle(**kw) -> DialogSession:
    return DialogSession(user_id=1, guild_id=9, **kw)


def step(s: DialogSession, ev: DialogEvent):
    res = transition(s, ev, MAX_RETRIES)
    return res.session, list(res.actions)


def classified(s: DialogSession, c: Classified):
    return step(s, DialogEvent(Ev.CLASSIFIED, gen=s.gen, classified=c))


def cc(**kw) -> Classified:
    """A Classified defaulting to a confident conversation-available residue."""
    defaults = dict(
        text="how are you",
        confident=True,
        relay_mode="off",
        conversation_available=True,
    )
    defaults.update(kw)
    return Classified(**defaults)


def parsed_cmd(**kw) -> ParsedCommand:
    defaults = dict(
        intent=Intent.UNDER_ATTACK,
        raw="under attack umi",
        system_text="umi",
        group_alias=None,
        detail=None,
    )
    defaults.update(kw)
    return ParsedCommand(**defaults)


def thinking(pending: PendingKind = PendingKind.CONVERSATION, **kw) -> DialogSession:
    """A session parked in THINKING with a live conversation pending — the state
    the CLASSIFIED handler routes through ``_classified``."""
    return DialogSession(
        user_id=1,
        guild_id=9,
        state=DialogState.THINKING,
        gen=3,
        retries_left=MAX_RETRIES,
        pending=pending,
        **kw,
    )


def kinds(actions) -> list[type]:
    return [type(a) for a in actions]


# ── the residue branch ───────────────────────────────────────────────────────


def test_confident_residue_emits_runchat() -> None:
    s = thinking()
    s2, actions = classified(s, cc(text="tell me a joke"))
    assert s2.state is DialogState.IDLE
    assert actions == [RunChat("tell me a joke")]


def test_cold_start_when_relay_off_and_no_pending() -> None:
    # A fresh-wake residue (pending NONE) converses ONLY because relay is off.
    s = thinking(pending=PendingKind.NONE)
    _s2, actions = classified(s, cc(text="hey there", relay_mode="off"))
    assert actions == [RunChat("hey there")]


def test_no_cold_start_when_relay_not_off() -> None:
    # relay_mode != off with pending NONE → chat does NOT claim residue; it
    # falls through to the relay tail (here: unframed framed-mode → NOT_UNDERSTOOD).
    s = thinking(pending=PendingKind.NONE)
    _s2, actions = classified(s, cc(text="hey there", relay_mode="framed", framed=False))
    assert RunChat not in kinds(actions)


def test_live_conversation_converses_regardless_of_relay_mode() -> None:
    # A LIVE conversation (pending CONVERSATION) keeps going even with relay on.
    s = thinking(pending=PendingKind.CONVERSATION)
    _s2, actions = classified(s, cc(text="still chatting", relay_mode="framed"))
    assert actions == [RunChat("still chatting")]


def test_low_confidence_in_chat_window_says_again_no_runchat() -> None:
    s = thinking()
    s2, actions = classified(s, cc(text="mumble", confident=False))
    assert RunChat not in kinds(actions)
    assert Speak(Line.SAY_AGAIN) in actions
    assert s2.state is DialogState.AWAIT_REPEAT or s2.state is DialogState.AWAIT_CONVERSATION


def test_garbage_in_chat_window_closes_silently_no_runchat() -> None:
    s = thinking()
    s2, actions = classified(s, cc(text="xzzt", garbage=True))
    assert s2.state is DialogState.IDLE
    assert actions == [NoteRejected("chat_garbage")]


# ── command preemption (constraint 6) ────────────────────────────────────────


def test_command_preempts_live_conversation() -> None:
    # A real callout spoken mid-conversation hits the grammar-command dispatch
    # (step 4) ABOVE the residue branch: it reports and tears the chat down.
    s = thinking(pending=PendingKind.CONVERSATION)
    p = parsed_cmd()
    s2, actions = classified(s, cc(text="under attack in umi", parsed=p))
    assert s2.state is DialogState.IDLE
    assert actions == [Report(p)]
    assert RunChat not in kinds(actions)


def test_dismissal_closes_conversation_audibly() -> None:
    s = thinking(pending=PendingKind.CONVERSATION)
    s2, actions = classified(s, cc(text="never mind", dismissed=True))
    assert s2.state is DialogState.IDLE
    assert actions == [DisarmWindow(), Speak(Line.STANDING_DOWN)]


# ── the non-draining re-arm ──────────────────────────────────────────────────


def test_converse_arm_from_idle_does_not_drain_retry_budget() -> None:
    s = idle(state=DialogState.IDLE, gen=5, retries_left=2)
    s2, actions = step(s, DialogEvent(Ev.CONVERSE_ARM))
    assert s2.state is DialogState.AWAIT_CONVERSATION
    assert s2.pending is PendingKind.CONVERSATION
    assert s2.retries_left == 2  # the failure budget is UNTOUCHED
    assert s2.gen == 6
    assert actions == [ArmWindow(6)]


def test_converse_arm_from_non_idle_is_noop() -> None:
    s = idle(state=DialogState.LISTENING, gen=5)
    s2, actions = step(s, DialogEvent(Ev.CONVERSE_ARM))
    assert s2 == s
    assert actions == []


# ── inherited AWAIT_CONVERSATION behaviour (via AWAIT_STATES) ─────────────────


def test_deadline_in_await_conversation_closes_silently() -> None:
    s = idle(state=DialogState.AWAIT_CONVERSATION, gen=4, pending=PendingKind.CONVERSATION)
    s2, actions = step(s, DialogEvent(Ev.DEADLINE, gen=4))
    assert s2.state is DialogState.IDLE
    assert actions == [DisarmWindow()]


def test_wake_supersedes_conversation_window() -> None:
    s = idle(state=DialogState.AWAIT_CONVERSATION, gen=4, pending=PendingKind.CONVERSATION)
    s2, actions = step(s, DialogEvent(Ev.WAKE_HIT))
    assert s2.state is DialogState.LISTENING
    assert s2.retries_left == MAX_RETRIES  # budget refilled by the wake
    assert DisarmWindow() in actions
    assert Speak(Line.ACK) in actions


def test_window_opened_moves_conversation_window_to_listening() -> None:
    s = idle(state=DialogState.AWAIT_CONVERSATION, gen=4, pending=PendingKind.CONVERSATION)
    s2, actions = step(s, DialogEvent(Ev.WINDOW_OPENED, gen=4))
    assert s2.state is DialogState.LISTENING
    assert actions == []  # the spoken reply was the cue — no second chirp


# ── additivity regression: conversation OFF ⇒ old path, byte-for-byte ─────────


def test_conversation_unavailable_is_the_old_relay_path() -> None:
    # With conversation_available False, a confident unframed transcript in
    # framed relay-mode is NOT_UNDERSTOOD exactly as before the feature existed.
    s = thinking(pending=PendingKind.NONE)
    _s2, actions = classified(
        s,
        Classified(
            text="just chatter",
            confident=True,
            relay_mode="framed",
            framed=False,
            conversation_available=False,
        ),
    )
    assert RunChat not in kinds(actions)
    assert Speak(Line.NOT_UNDERSTOOD) in actions


def test_conversation_unavailable_with_relay_off_is_old_reject() -> None:
    s = thinking(pending=PendingKind.NONE)
    _s2, actions = classified(
        s,
        Classified(
            text="chatter",
            confident=True,
            relay_mode="off",
            conversation_available=False,
        ),
    )
    assert RunChat not in kinds(actions)
    assert Speak(Line.NOT_UNDERSTOOD) in actions


# ── engine: _converse (async, fakes, transcript-only) ────────────────────────


class _FakeBackend:
    def __init__(self, reply: str = "Doing great, pilot.") -> None:
        self.reply = reply
        self.calls: list[list[dict[str, str]]] = []

    async def converse(self, user_id: int, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        return self.reply

    async def ask(self, user_id, query):  # pragma: no cover
        return self.reply

    async def close(self):  # pragma: no cover
        return None


class _FakeSpeaker:
    def __init__(self, spoken: bool = True) -> None:
        self.spoken = spoken
        self.said: list[str] = []

    async def say(self, guild_id, text, priority, user_id=None, **kw) -> bool:
        self.said.append(text)
        return self.spoken


def _holder(conv: ConversationConfig) -> SimpleNamespace:
    return SimpleNamespace(
        current=SimpleNamespace(
            conversation=conv,
            dialog=DialogConfig(),
            discord=SimpleNamespace(channels=SimpleNamespace(intel_live=111)),
        )
    )


def _engine(
    conv: ConversationConfig,
    backend,
    speaker: _FakeSpeaker,
    incidents,
    sent: list,
):
    from cortana.conversation import ConversationManager

    holder = _holder(conv)
    manager = ConversationManager(
        holder,  # type: ignore[arg-type]
        conversation_provider=lambda: (backend, "ready" if backend else "disabled"),
        last_incident_at=lambda gid: None,
    )

    async def _send(channel_id, content, embed=None):
        sent.append((channel_id, content))
        return True

    engine = DialogEngine(
        holder,  # type: ignore[arg-type]
        capture=None,
        transcriber=Mock(),
        speaker=speaker,  # type: ignore[arg-type]
        incidents=incidents,
        discipline=Mock(),
        gazetteer=Mock(),
        conn=Mock(),
        health=Mock(),
        chat_provider=lambda: (None, "disabled"),
        member_role_ids=lambda uid: [],
        send_channel=_send,
        shutdown=asyncio.Event(),
        conversation=manager,
    )
    return engine, manager


def _session() -> DialogSession:
    return DialogSession(user_id=1, guild_id=9, state=DialogState.IDLE, gen=2)


def _conv(**kw) -> ConversationConfig:
    defaults = dict(
        enabled=True,
        backend="local",
        local_url="http://x",
        math_tool=False,
        user_cooldown_s=0.0,
        max_turns=5,
    )
    defaults.update(kw)
    return ConversationConfig(**defaults)


async def test_converse_speaks_reply_and_never_escalates() -> None:
    backend = _FakeBackend("Hanging in there.")
    speaker = _FakeSpeaker(spoken=True)
    incidents = Mock()
    sent: list = []
    engine, _mgr = _engine(_conv(), backend, speaker, incidents, sent)

    await engine._converse(_session(), "how are you")

    # The reply reached the speaker …
    assert speaker.said and speaker.said[0] == "Hanging in there."
    # … and NOTHING reached the incident engine — the hard wall (constraints 9+11).
    incidents.report.assert_not_called()
    incidents.broadcast.assert_not_called()
    incidents.confirm_system.assert_not_called()
    # No overflow post either (it was spoken).
    assert sent == []


async def test_converse_rearms_next_turn_while_budget_holds() -> None:
    backend = _FakeBackend()
    speaker = _FakeSpeaker(spoken=True)
    engine, mgr = _engine(_conv(max_turns=5), backend, speaker, Mock(), [])
    s = _session()

    await engine._converse(s, "hi")

    # A turn-taking window is armed for the pilot's next turn.
    live = engine._sessions.get(s.user_id)
    assert live is not None and live.state is DialogState.AWAIT_CONVERSATION
    assert mgr.active(s.user_id) is True


async def test_converse_no_rearm_when_budget_exhausted() -> None:
    backend = _FakeBackend()
    speaker = _FakeSpeaker(spoken=True)
    engine, mgr = _engine(_conv(max_turns=1), backend, speaker, Mock(), [])
    s = _session()

    await engine._converse(s, "last word")

    # Budget spent (max_turns=1) → active False → NO re-arm.
    assert mgr.active(s.user_id) is False
    live = engine._sessions.get(s.user_id)
    assert live is None or live.state is not DialogState.AWAIT_CONVERSATION


async def test_converse_none_reply_speaks_nothing_and_no_rearm() -> None:
    speaker = _FakeSpeaker(spoken=True)
    engine, mgr = _engine(_conv(), None, speaker, Mock(), [])  # no backend
    s = _session()

    await engine._converse(s, "anyone there")

    assert speaker.said == []
    assert mgr.active(s.user_id) is False


async def test_converse_overflow_posts_mention_free_when_unspoken() -> None:
    backend = _FakeBackend("A very long answer that cannot be spoken aloud.")
    speaker = _FakeSpeaker(spoken=False)  # say() fails (over the §12.2 cap)
    sent: list = []
    engine, _mgr = _engine(_conv(overflow_channel=222), backend, speaker, Mock(), sent)

    await engine._converse(_session(), "explain everything")

    assert sent == [(222, "💬 A very long answer that cannot be spoken aloud.")]


async def test_converse_unspoken_dropped_when_no_overflow_channel() -> None:
    backend = _FakeBackend("long")
    speaker = _FakeSpeaker(spoken=False)
    sent: list = []
    engine, _mgr = _engine(_conv(overflow_channel=0), backend, speaker, Mock(), sent)

    await engine._converse(_session(), "explain")

    assert sent == []  # dropped, never posted


async def test_converse_math_answered_without_backend() -> None:
    backend = _FakeBackend("WRONG")
    speaker = _FakeSpeaker(spoken=True)
    engine, _mgr = _engine(_conv(math_tool=True), backend, speaker, Mock(), [])

    await engine._converse(_session(), "what is 6 times 7")

    assert speaker.said == ["42"]
    assert backend.calls == []
