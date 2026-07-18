"""Voice-pipeline wiring tests for ``aura.__main__.App`` — GDD §5, §8.3, §11.1.

The App is assembled from stubs around the real ``_on_utterance`` flow: real
grammar parse, real phonetic resolve (against a fake gazetteer), real
Discipline, stubbed STT/engine/speaker/capture. Covers the @Pilot voice gate
(constraint 10 parity with the slash path), the LOW-tier "say again" retry
re-bind, the STT-failure reply, and the timer/reminder poll readiness guards.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

import aura.__main__ as app_main
from aura.__main__ import App
from aura.audio.stt import SttTimeoutError
from aura.core.discipline import Discipline
from aura.types import (
    IncidentOutcome,
    Intent,
    Outcome,
    ParsedCommand,
    PriorContext,
    Severity,
    SystemEntry,
    TranscriptResult,
)
from tests.test_incidents import PILOT_ROLE, FakeGazetteer, StubHolder, make_config
from tests.test_incidents import SYSTEMS as GAZ_SYSTEMS

GUILD = 1
USER = 42


# ── stubs ────────────────────────────────────────────────────────────────────


class _Gazetteer(FakeGazetteer):
    def prompt_bias_text(self) -> str:
        return ""


class _Transcriber:
    def __init__(
        self,
        texts: list[str] | None = None,
        error: Exception | None = None,
        avg_logprob: float = -0.1,
    ) -> None:
        self.texts = list(texts or [])
        self.error = error
        self.avg_logprob = avg_logprob

    def transcribe(self, pcm: bytes, bias: str) -> TranscriptResult:
        if self.error is not None:
            raise self.error
        return TranscriptResult(text=self.texts.pop(0), avg_logprob=self.avg_logprob)


class _Engine:
    def __init__(self, outcome: IncidentOutcome) -> None:
        self.outcome = outcome
        self.reports: list[tuple[int, int, Any, Any]] = []
        self.broadcasts: list[tuple[int, int, str, bool]] = []
        self.fired: list[datetime] = []

    async def report(self, guild_id: int, user_id: int, parsed: Any, resolution: Any) -> Any:
        self.reports.append((guild_id, user_id, parsed, resolution))
        return self.outcome

    async def broadcast(
        self,
        guild_id: int,
        user_id: int,
        text: str,
        *,
        here: bool = False,
        severity: Any = None,
        confidence: Any = None,
    ) -> Any:
        self.broadcasts.append((guild_id, user_id, text, here))
        self.broadcast_severities = getattr(self, "broadcast_severities", [])
        self.broadcast_severities.append(severity)
        return self.outcome

    def build_prior_context(self, guild_id: int, user_id: int) -> PriorContext:
        return PriorContext()

    async def fire_due_timers(self, now: datetime) -> list[Any]:
        self.fired.append(now)
        return []


class _Health:
    def __init__(self) -> None:
        self.wake_hits = 0
        self.rejected = 0
        self.stt: list[tuple[float, Any]] = []
        self.degraded = False

    def record_wake_hit(self) -> None:
        self.wake_hits += 1

    def record_rejected(self) -> None:
        self.rejected += 1

    def record_stt(self, confidence: float, tier: Any) -> None:
        self.stt.append((confidence, tier))

    def record_incident_posted(self) -> None:
        pass

    def record_incident_folded(self) -> None:
        pass


class _Speaker:
    def __init__(self) -> None:
        self.said: list[tuple[int, str]] = []
        self.chirped: list[int] = []

    async def say(self, guild_id: int, text: str, priority: int = 1, *, user_id=None) -> bool:
        self.said.append((guild_id, text))
        return True

    async def chirp(self, guild_id: int, *, user_id=None) -> bool:
        self.chirped.append(guild_id)
        return True


@dataclass
class _Capture:
    reopened: list[tuple[int, int]] = field(default_factory=list)
    dropped: list[int] = field(default_factory=list)

    def reopen(self, user_id: int, guild_id: int) -> None:
        self.reopened.append((user_id, guild_id))

    def drop_user(self, user_id: int) -> None:
        self.dropped.append(user_id)


class _Bot:
    def __init__(self, role_ids: list[int]) -> None:
        member = SimpleNamespace(roles=[SimpleNamespace(id=r) for r in role_ids])
        self._guild = SimpleNamespace(get_member=lambda uid: member)
        self.ready = False

    def get_guild(self, guild_id: int) -> Any:
        return self._guild

    def is_ready(self) -> bool:
        return self.ready


class _Reminders:
    def __init__(self) -> None:
        self.delivered: list[datetime] = []

    async def deliver_due(self, now: datetime) -> int:
        self.delivered.append(now)
        return 0


# ── assembly ─────────────────────────────────────────────────────────────────


def make_app(
    *,
    roles: list[int],
    transcriber: _Transcriber,
    outcome: IncidentOutcome | None = None,
    wake_ack: str = "beep",
    relay_mode: str = "framed",
) -> tuple[App, _Engine, _Health, _Speaker, _Capture]:
    holder = StubHolder(make_config(wake_ack=wake_ack, relay_mode=relay_mode))
    app = App(holder)  # type: ignore[arg-type]
    app.gazetteer = _Gazetteer(  # type: ignore[assignment]
        entries={
            sid: SystemEntry(
                id=sid, name=name, region=region, constellation=None, metaphone=name.upper()
            )
            for sid, name, region in GAZ_SYSTEMS
        }
    )
    app.transcriber = transcriber  # type: ignore[assignment]
    engine = _Engine(outcome or IncidentOutcome(Outcome.POSTED, None, None, 1))
    app.engine = engine  # type: ignore[assignment]
    health = _Health()
    app.health = health  # type: ignore[assignment]
    app.discipline = Discipline(holder)  # type: ignore[arg-type]
    speaker = _Speaker()
    app.speaker = speaker  # type: ignore[assignment]
    capture = _Capture()
    app.capture = capture  # type: ignore[assignment]
    app.bot = _Bot(roles)  # type: ignore[assignment]
    app.conn = None
    return app, engine, health, speaker, capture


# ── wake acknowledgement (wake.ack: voice | beep | none) ─────────────────────


async def _drain_voice_tasks(app: App) -> None:
    if app._voice_tasks:
        await asyncio.gather(*app._voice_tasks)


async def test_wake_ack_voice_speaks_go_ahead() -> None:
    app, _, _, speaker, _ = make_app(
        roles=[PILOT_ROLE], transcriber=_Transcriber([]), wake_ack="voice"
    )
    app._on_capture_start(USER, GUILD)
    await _drain_voice_tasks(app)
    assert speaker.said == [(GUILD, "Go ahead.")]
    assert speaker.chirped == []


async def test_wake_ack_beep_chirps() -> None:
    app, _, _, speaker, _ = make_app(
        roles=[PILOT_ROLE], transcriber=_Transcriber([]), wake_ack="beep"
    )
    app._on_capture_start(USER, GUILD)
    await _drain_voice_tasks(app)
    assert speaker.chirped == [GUILD]
    assert speaker.said == []


async def test_wake_ack_none_is_silent() -> None:
    app, _, _, speaker, _ = make_app(
        roles=[PILOT_ROLE], transcriber=_Transcriber([]), wake_ack="none"
    )
    app._on_capture_start(USER, GUILD)
    await _drain_voice_tasks(app)
    assert speaker.said == []
    assert speaker.chirped == []


# ── @Pilot voice gate (GDD §11.1 layer 4, constraint 10 parity) ──────────────


async def test_non_pilot_voice_report_is_rejected_before_the_engine() -> None:
    app, engine, health, speaker, _ = make_app(
        roles=[999], transcriber=_Transcriber(["aura command hostiles Otanuomi"])
    )
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert engine.reports == []  # never reaches the shared engine
    assert health.rejected == 1
    assert speaker.said == [(GUILD, "Reporting requires the Pilot role.")]


async def test_pilot_voice_report_reaches_the_engine() -> None:
    app, engine, _, _, _ = make_app(
        roles=[PILOT_ROLE], transcriber=_Transcriber(["aura command hostiles Otanuomi"])
    )
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert len(engine.reports) == 1
    assert engine.reports[0][2].intent is Intent.HOSTILE_SPOTTED


async def test_non_mention_intents_stay_open_to_non_pilots() -> None:
    app, engine, health, _, _ = make_app(
        roles=[], transcriber=_Transcriber(["aura command status"])
    )
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert len(engine.reports) == 1
    assert engine.reports[0][2].intent is Intent.QUERY
    assert health.rejected == 0


# ── STT failure path (GDD §20 watchdog → "say again") ────────────────────────


async def test_stt_timeout_reopens_capture_and_says_again() -> None:
    app, engine, health, speaker, capture = make_app(
        roles=[PILOT_ROLE], transcriber=_Transcriber(error=SttTimeoutError("watchdog"))
    )
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert engine.reports == []
    assert health.rejected == 1
    assert capture.reopened == [(USER, GUILD)]
    assert speaker.said == [(GUILD, "Say again the system.")]


# ── LOW-tier retry re-bind (GDD §8.3) ────────────────────────────────────────


async def test_low_tier_retry_rebinds_bare_system_name() -> None:
    rejected = IncidentOutcome(Outcome.REJECTED, None, None, None)
    app, engine, _, _, capture = make_app(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["aura command hostiles zzzz qqqq", "Kisogo"]),
        outcome=rejected,
    )
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert len(engine.reports) == 1
    assert engine.reports[0][3].tier.value == "LOW"
    assert capture.reopened == [(USER, GUILD)]  # window reopened
    assert USER in app._pending_retry

    # The bare name in the reopened window re-binds to the rejected intent.
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert len(engine.reports) == 2
    retried = engine.reports[1][2]
    assert retried.intent is Intent.HOSTILE_SPOTTED
    assert retried.system_text == "Kisogo"
    assert retried.raw == "Kisogo"
    assert USER not in app._pending_retry  # consumed


async def test_bare_no_intent_utterance_is_relayed_in_open_mode() -> None:
    # relay_mode: open — the old catch-all: anything unmatched relays.
    app, engine, _, _, _ = make_app(
        roles=[PILOT_ROLE], transcriber=_Transcriber(["Kisogo"]), relay_mode="open"
    )
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert engine.reports == []
    assert engine.broadcasts == [(GUILD, USER, "Kisogo", False)]


async def test_expired_pending_retry_falls_through_to_relay() -> None:
    # An expired retry no longer re-binds; the utterance is relayed instead.
    app, engine, _, _, _ = make_app(
        roles=[PILOT_ROLE], transcriber=_Transcriber(["Kisogo"]), relay_mode="open"
    )
    stale = ParsedCommand(
        intent=Intent.HOSTILE_SPOTTED, system_text="x", group_alias=None, detail=None, raw="x"
    )
    app._pending_retry[USER] = (stale, asyncio.get_running_loop().time() - 1.0)
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert engine.reports == []
    assert engine.broadcasts == [(GUILD, USER, "Kisogo", False)]
    assert USER not in app._pending_retry


# ── timer/reminder polls gated on Discord readiness ──────────────────────────


async def test_timer_and_reminder_polls_wait_for_bot_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A due timer/reminder is never consumed (fired=1) before login: the poll
    body is skipped entirely while bot.is_ready() is False."""
    monkeypatch.setattr(app_main, "_TIMER_POLL_INTERVAL_S", 0.01)
    app, engine, _, _, _ = make_app(roles=[PILOT_ROLE], transcriber=_Transcriber([]))
    reminders = _Reminders()
    app.reminders = reminders  # type: ignore[assignment]

    timer_task = asyncio.create_task(app._timer_loop())
    reminder_task = asyncio.create_task(app._reminder_loop())
    try:
        await asyncio.sleep(0.08)
        assert engine.fired == []  # nothing consumed pre-ready
        assert reminders.delivered == []

        app.bot.ready = True  # type: ignore[union-attr]
        await asyncio.sleep(0.08)
        assert engine.fired  # polls resume once Discord is usable
        assert reminders.delivered
    finally:
        timer_task.cancel()
        reminder_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await timer_task
        with contextlib.suppress(asyncio.CancelledError):
            await reminder_task


# ── task supervision & bounded shutdown ──────────────────────────────────────


class _RecordingLog:
    """Captures log calls so a test can assert what was (not) emitted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def _record(self, level: str):
        def _log(event: str, **_: object) -> None:
            self.calls.append((level, event))

        return _log

    def __getattr__(self, name: str):
        return self._record(name)


async def _returns_immediately() -> None:
    return None


async def test_clean_task_exit_during_shutdown_does_not_alarm(monkeypatch) -> None:
    app, *_ = make_app(roles=[PILOT_ROLE], transcriber=_Transcriber([]))
    rec = _RecordingLog()
    monkeypatch.setattr(app_main, "log", rec)
    app._shutdown.set()  # process is already shutting down

    app._spawn("noop", _returns_immediately())
    await asyncio.gather(*app._tasks)

    assert ("error", "critical_task_exited") not in rec.calls


async def test_task_exit_while_running_alarms_and_triggers_shutdown(monkeypatch) -> None:
    app, *_ = make_app(roles=[PILOT_ROLE], transcriber=_Transcriber([]))
    rec = _RecordingLog()
    monkeypatch.setattr(app_main, "log", rec)
    assert not app._shutdown.is_set()

    app._spawn("noop", _returns_immediately())
    await asyncio.gather(*app._tasks)

    assert ("error", "critical_task_exited") in rec.calls
    assert app._shutdown.is_set()  # a task dying mid-run brings the process down


async def test_graceful_shutdown_is_bounded(monkeypatch) -> None:
    app, *_ = make_app(roles=[PILOT_ROLE], transcriber=_Transcriber([]))

    async def _hang() -> None:
        await asyncio.sleep(100)

    monkeypatch.setattr(app, "_shutdown_sequence", _hang)
    monkeypatch.setattr(app_main, "_SHUTDOWN_TIMEOUT_S", 0.05)

    # A wedged close must not hang the process: the wait_for bound returns
    # control promptly instead of blocking until systemd's SIGKILL.
    await asyncio.wait_for(app._graceful_shutdown(), timeout=2)


# ── spoken colour codes: dialogue + inline (GDD §6.4) ────────────────────────


async def test_bare_code_opens_dialogue_and_next_report_inherits_severity() -> None:
    app, engine, _, speaker, capture = make_app(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey jarvis code orange", "hostiles Otanuomi"]),
    )
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    # Step 1: acknowledged, window reopened, nothing posted yet.
    assert speaker.said == [(GUILD, "Code orange. Go ahead.")]
    assert capture.reopened == [(USER, GUILD)]
    assert engine.reports == [] and engine.broadcasts == []

    await app._on_utterance(USER, GUILD, b"\x00\x00")
    # Step 2: the report inherits the pending severity.
    assert len(engine.reports) == 1
    parsed = engine.reports[0][2]
    assert parsed.intent is Intent.HOSTILE_SPOTTED
    assert parsed.severity is Severity.MEDIUM


async def test_inline_code_red_rides_the_report() -> None:
    app, engine, _, _, _ = make_app(
        roles=[PILOT_ROLE], transcriber=_Transcriber(["code red hostiles Otanuomi"])
    )
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert len(engine.reports) == 1
    assert engine.reports[0][2].severity is Severity.HIGH


async def test_pending_severity_colours_a_freeform_relay() -> None:
    app, engine, _, _, _ = make_app(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["code red", "blop fleet moving to Kisogo gate whatever"]),
    )
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert len(engine.broadcasts) == 1
    assert engine.broadcast_severities == [Severity.HIGH]


# ── freeform relay confidence gate (GDD §8.6) ────────────────────────────────


async def test_low_confidence_gibberish_is_not_relayed() -> None:
    app, engine, health, speaker, _ = make_app(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["Rens, Rens, Rens"], avg_logprob=-2.4),
        relay_mode="open",  # open mode so the confidence gate itself is what fires
    )
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert engine.broadcasts == []  # hallucinated noise never becomes a card
    assert health.rejected == 1
    assert speaker.said == [(GUILD, "Say again the system.")]


async def test_confident_relay_posts_and_acks() -> None:
    app, engine, _, speaker, _ = make_app(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["blop fleet moving to Kisogo gate whatever"]),
        outcome=IncidentOutcome(Outcome.POSTED, "Relayed.", None, None),
        relay_mode="open",
    )
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert len(engine.broadcasts) == 1
    assert (GUILD, "Relayed.") in speaker.said


# ── relay framing (GDD §8.6, relay_mode) ─────────────────────────────────────


async def test_unframed_speech_never_becomes_a_card_by_default() -> None:
    # relay_mode: framed (default) — crosstalk and mishearings get "Say
    # again?", never a card. This is the fix for junk relays like a lone
    # system name decoded from noise.
    app, engine, health, speaker, _ = make_app(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["How's everybody else doing"]),
    )
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert engine.broadcasts == []
    assert health.rejected == 1
    assert speaker.said == [(GUILD, "Say again?")]


async def test_framed_report_relays_under_default_mode() -> None:
    # A "report … end report" envelope is explicit framing: it relays.
    app, engine, _, speaker, _ = make_app(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey jarvis report blop fleet on the Kisogo gate end report"]),
        outcome=IncidentOutcome(Outcome.POSTED, "Relayed.", None, None),
    )
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert len(engine.broadcasts) == 1
    assert engine.broadcasts[0][2] == "blop fleet on the Kisogo gate"
    assert (GUILD, "Relayed.") in speaker.said


async def test_relay_off_drops_even_framed_speech() -> None:
    app, engine, health, speaker, _ = make_app(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["report blop fleet on the Kisogo gate end report"]),
        relay_mode="off",
    )
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert engine.broadcasts == []
    assert health.rejected == 1
    assert speaker.said == [(GUILD, "Say again?")]


# ── command override wiring (GDD §6.6) ───────────────────────────────────────


class _Chat:
    def __init__(self, reply: str | None = "Sunny, 21 degrees.", error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.asked: list[tuple[int, str]] = []

    async def ask(self, user_id: int, query: str) -> str:
        self.asked.append((user_id, query))
        if self.error is not None:
            raise self.error
        assert self.reply is not None
        return self.reply


async def test_override_routes_to_chat_and_speaks_reply() -> None:
    app, engine, _, speaker, _ = make_app(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey jarvis command override what's the weather in Chicago"]),
    )
    app.holder = StubHolder(make_config(chat_enabled=True))  # type: ignore[assignment]
    app.discipline = Discipline(app.holder)  # type: ignore[arg-type]
    chat = _Chat()
    app.chat = chat  # type: ignore[assignment]
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert chat.asked == [(USER, "what's the weather in Chicago")]
    assert speaker.said == [(GUILD, "Sunny, 21 degrees.")]
    assert engine.reports == [] and engine.broadcasts == []  # never touches intel


async def test_override_failure_speaks_fixed_line() -> None:
    from aura.chat import ChatError

    app, engine, _, speaker, _ = make_app(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["command override tell me a story"]),
    )
    app.holder = StubHolder(make_config(chat_enabled=True))  # type: ignore[assignment]
    app.discipline = Discipline(app.holder)  # type: ignore[arg-type]
    app.chat = _Chat(error=ChatError("boom"))  # type: ignore[assignment]
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert speaker.said == [(GUILD, "Override channel unavailable.")]
    assert engine.broadcasts == []


async def test_refresh_chat_follows_config_and_key_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SIGHUP path: flipping chat.enabled (or a key appearing) takes effect on
    # reload — it used to require a full restart, silently.
    app, *_ = make_app(roles=[PILOT_ROLE], transcriber=_Transcriber([]))
    app.holder = StubHolder(make_config(chat_enabled=True))  # type: ignore[assignment]
    monkeypatch.setattr(app_main, "read_api_key", lambda path: "sk-test")
    app._refresh_chat()
    assert app.chat is not None
    assert app._chat_status == "ready"

    monkeypatch.setattr(app_main, "read_api_key", lambda path: None)
    app._refresh_chat()
    assert app.chat is None
    assert app._chat_status == "no_key"

    app.holder = StubHolder(make_config(chat_enabled=False))  # type: ignore[assignment]
    app._refresh_chat()
    assert app.chat is None
    assert app._chat_status == "disabled"


async def test_left_control_purges_per_user_state() -> None:
    # §19 posture: when a pilot leaves voice, every per-user trace goes.
    app, _, _, _, capture = make_app(roles=[PILOT_ROLE], transcriber=_Transcriber([]))
    stale = ParsedCommand(
        intent=Intent.HOSTILE_SPOTTED, system_text="x", group_alias=None, detail=None, raw="x"
    )
    app._pending_retry[USER] = (stale, 999.0)
    app._pending_severity[USER] = (Severity.HIGH, 999.0)
    app._last_audio_at[USER] = 1.0
    await app._on_control({"t": "left", "user_id": str(USER)})
    assert capture.dropped == [USER]
    assert USER not in app._pending_retry
    assert USER not in app._pending_severity
    assert USER not in app._last_audio_at


async def test_override_while_disabled_speaks_unavailable() -> None:
    # chat.enabled false: an explicit override request gets the fixed
    # unavailable line — never a silent fall-through to the grammar, which
    # used to disguise a down channel as a mishearing/relay.
    app, engine, _, speaker, _ = make_app(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["command override what's the weather in Chicago"]),
        relay_mode="open",
    )
    app.chat = None  # type: ignore[assignment]
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert engine.broadcasts == [] and engine.reports == []
    assert speaker.said == [(GUILD, "Override channel unavailable.")]


async def test_bare_override_opens_dialogue_then_takes_the_question() -> None:
    # "command override" alone (window closed on the pause) → ack + reopen;
    # the NEXT utterance is the question verbatim, no prefix needed.
    app, engine, _, speaker, capture = make_app(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey jarvis command override", "what's the weather in Chicago"]),
    )
    app.holder = StubHolder(make_config(chat_enabled=True))  # type: ignore[assignment]
    app.discipline = Discipline(app.holder)  # type: ignore[arg-type]
    chat = _Chat()
    app.chat = chat  # type: ignore[assignment]

    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert chat.asked == []
    assert speaker.said == [(GUILD, "Go ahead.")]
    assert capture.reopened == [(USER, GUILD)]

    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert chat.asked == [(USER, "what's the weather in Chicago")]
    assert engine.broadcasts == [] and engine.reports == []


async def test_bare_override_while_disabled_speaks_unavailable() -> None:
    app, _, _, speaker, capture = make_app(
        roles=[PILOT_ROLE], transcriber=_Transcriber(["command override"])
    )
    app.chat = None  # type: ignore[assignment]
    await app._on_utterance(USER, GUILD, b"\x00\x00")
    assert speaker.said == [(GUILD, "Override channel unavailable.")]
    assert capture.reopened == []
