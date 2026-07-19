"""Voice-pipeline tests — GDD §5, §5.4, §8.3, §11.1.

The DialogEngine is assembled from stubs around the real machine + grammar +
phonetic resolve (fake gazetteer, real Discipline); STT/incidents/speaker/
capture are stubbed. Covers the @Pilot voice gate (constraint 10 parity with
the slash path), the LOW-tier "say again" retry re-bind, the STT-failure
path, subdialogs (severity opener / override), relay framing, and the retry
budget. App-level tests (supervision, shutdown, polls, chat refresh, IPC
control routing) drive ``cortana.__main__.App`` directly.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

import cortana.__main__ as app_main
from cortana.__main__ import App
from cortana.audio.capture import CaptureMeta, CaptureOrigin
from cortana.audio.stt import SttTimeoutError
from cortana.core.discipline import Discipline
from cortana.dialog import DialogEngine, DialogState
from cortana.types import (
    IncidentOutcome,
    Intent,
    Outcome,
    PriorContext,
    Severity,
    SystemEntry,
    Tier,
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
        self.biases = getattr(self, "biases", [])
        self.biases.append(bias)
        return TranscriptResult(text=self.texts.pop(0), avg_logprob=self.avg_logprob)


class _Engine:
    def __init__(self, outcome: IncidentOutcome) -> None:
        self.outcome = outcome
        self.reports: list[tuple[int, int, Any, Any]] = []
        self.broadcasts: list[tuple[int, int, str, bool]] = []
        self.broadcast_severities: list[Any] = []
        self.confirms: list[tuple[int, int, int]] = []
        self.fired: list[datetime] = []

    async def report(
        self,
        guild_id: int,
        user_id: int,
        parsed: Any,
        resolution: Any,
        *,
        caller_may_mention: bool = True,
    ) -> Any:
        self.reports.append((guild_id, user_id, parsed, resolution))
        self.report_gates = getattr(self, "report_gates", [])
        self.report_gates.append(caller_may_mention)
        return self.outcome

    async def confirm_system(self, incident_id: int, user_id: int, system_id: int) -> Any:
        self.confirms.append((incident_id, user_id, system_id))
        return IncidentOutcome(Outcome.POSTED, "Confirmed Otanuomi.", None, incident_id)

    async def broadcast(
        self,
        guild_id: int,
        user_id: int,
        text: str,
        *,
        here: bool = False,
        group_alias: str | None = None,
        severity: Any = None,
        confidence: Any = None,
        caller_may_mention: bool = True,
    ) -> Any:
        self.broadcasts.append((guild_id, user_id, text, group_alias == "all_hands"))
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

    def note_audio(self) -> None:
        pass

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
        self.caps: list[float | None] = []  # the max_s override per say()

    async def say(
        self, guild_id: int, text: str, priority: int = 1, *, user_id=None, max_s=None
    ) -> bool:
        self.said.append((guild_id, text))
        self.caps.append(max_s)
        return True

    async def chirp(self, guild_id: int, *, user_id=None) -> bool:
        self.chirped.append(guild_id)
        return True


@dataclass
class _Capture:
    armed: list[tuple[int, int, int]] = field(default_factory=list)  # (user, guild, gen)
    disarmed: list[int] = field(default_factory=list)
    dropped: list[int] = field(default_factory=list)
    # Streaming (GDD §5.5): capturing_users + capture_progress feed the wheel's
    # incremental decoder; endpoints records force_endpoint calls.
    capturing: list[int] = field(default_factory=list)
    progress: dict[int, tuple[bytes, int, int]] = field(default_factory=dict)
    endpoints: list[tuple[int, str, int | None]] = field(default_factory=list)

    def arm_window(self, user_id: int, guild_id: int, gen: int) -> None:
        self.armed.append((user_id, guild_id, gen))

    def disarm(self, user_id: int) -> None:
        self.disarmed.append(user_id)

    def drop_user(self, user_id: int) -> None:
        self.dropped.append(user_id)

    def capturing_users(self) -> list[int]:
        return list(self.capturing)

    def capture_progress(self, user_id: int) -> tuple[bytes, int, int] | None:
        return self.progress.get(user_id)

    def force_endpoint(
        self, user_id: int, reason: str = "silence", *, expected_gen: int | None = None
    ) -> bool:
        self.endpoints.append((user_id, reason, expected_gen))
        if user_id in self.capturing:
            self.capturing.remove(user_id)
        self.progress.pop(user_id, None)
        return True

    def feed(self, user_id: int, guild_id: int, pcm: bytes) -> None:
        pass


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


# ── assembly ─────────────────────────────────────────────────────────────────


@dataclass
class Rig:
    dialog: DialogEngine
    engine: _Engine
    health: _Health
    speaker: _Speaker
    capture: _Capture
    sent: list[tuple[int, str]]
    chat: _Chat | None


def make_dialog(
    *,
    roles: list[int],
    transcriber: _Transcriber,
    outcome: IncidentOutcome | None = None,
    wake_ack: str = "beep",
    relay_mode: str = "framed",
    chat: _Chat | None = None,
    chat_enabled: bool = False,
    fun: Any | None = None,
    confirm_reports: str = "off",
    transcript_channel: int = 0,
    streaming: bool = False,
) -> Rig:
    import dataclasses as _dc

    from cortana.config import DialogConfig

    cfg = make_config(wake_ack=wake_ack, relay_mode=relay_mode, chat_enabled=chat_enabled)
    cfg = _dc.replace(cfg, dialog=DialogConfig(confirm_reports=confirm_reports))
    cfg = _dc.replace(cfg, capture=_dc.replace(cfg.capture, streaming=streaming))
    if transcript_channel:
        channels = _dc.replace(cfg.discord.channels, transcript=transcript_channel)
        cfg = _dc.replace(cfg, discord=_dc.replace(cfg.discord, channels=channels))
    holder = StubHolder(cfg)
    gaz = _Gazetteer(
        entries={
            sid: SystemEntry(
                id=sid, name=name, region=region, constellation=None, metaphone=name.upper()
            )
            for sid, name, region in GAZ_SYSTEMS
        }
    )
    engine = _Engine(outcome or IncidentOutcome(Outcome.POSTED, None, None, 1))
    health = _Health()
    speaker = _Speaker()
    capture = _Capture()
    sent: list[tuple[int, str]] = []

    async def send_channel(channel_id: int, content: str, embed: Any = None) -> None:
        sent.append((channel_id, content))

    dialog = DialogEngine(
        holder,  # type: ignore[arg-type]
        capture=capture,  # type: ignore[arg-type]
        transcriber=transcriber,
        speaker=speaker,  # type: ignore[arg-type]
        incidents=engine,  # type: ignore[arg-type]
        discipline=Discipline(holder),  # type: ignore[arg-type]
        gazetteer=gaz,  # type: ignore[arg-type]
        conn=None,  # type: ignore[arg-type]
        health=health,  # type: ignore[arg-type]
        chat_provider=lambda: (chat, "ready" if chat is not None else "disabled"),
        member_role_ids=lambda uid: roles,
        send_channel=send_channel,
        shutdown=asyncio.Event(),
        fun=fun,
    )
    return Rig(dialog, engine, health, speaker, capture, sent, chat)


async def drain(rig: Rig) -> None:
    """Let scheduled ack tasks run."""
    for _ in range(3):
        await asyncio.sleep(0)
    if rig.dialog._voice_tasks:
        await asyncio.gather(*rig.dialog._voice_tasks)


def wake_open(rig: Rig, user: int = USER) -> int:
    """Simulate CaptureManager opening a wake capture (returns the gen)."""
    return rig.dialog.on_capture_start(user, GUILD, CaptureOrigin.WAKE, None)


async def utter_wake(rig: Rig, user: int = USER, speech_frames: int = 5) -> None:
    """One full wake-origin utterance through the dialog engine."""
    gen = wake_open(rig, user)
    await drain(rig)
    meta = CaptureMeta(CaptureOrigin.WAKE, gen, speech_frames, "silence")
    await rig.dialog.on_utterance(user, GUILD, b"\x00\x00", meta)


async def utter_window(rig: Rig, user: int = USER) -> None:
    """Speech inside the most recently armed window (the continuation)."""
    assert rig.capture.armed, "no window armed"
    armed_gen = rig.capture.armed[-1][2]
    gen = rig.dialog.on_capture_start(user, GUILD, CaptureOrigin.WINDOW, armed_gen)
    await drain(rig)
    meta = CaptureMeta(CaptureOrigin.WINDOW, gen, 5, "silence")
    await rig.dialog.on_utterance(user, GUILD, b"\x00\x00", meta)


# ── wake acknowledgement (wake.ack: voice | beep | none) ─────────────────────


async def test_wake_ack_voice_speaks_go_ahead() -> None:
    rig = make_dialog(roles=[PILOT_ROLE], transcriber=_Transcriber([]), wake_ack="voice")
    wake_open(rig)
    await drain(rig)
    assert rig.speaker.said == [(GUILD, "Go ahead.")]
    assert rig.speaker.chirped == []


async def test_wake_ack_beep_chirps() -> None:
    rig = make_dialog(roles=[PILOT_ROLE], transcriber=_Transcriber([]), wake_ack="beep")
    wake_open(rig)
    await drain(rig)
    assert rig.speaker.chirped == [GUILD]
    assert rig.speaker.said == []


async def test_wake_ack_none_is_silent() -> None:
    rig = make_dialog(roles=[PILOT_ROLE], transcriber=_Transcriber([]), wake_ack="none")
    wake_open(rig)
    await drain(rig)
    assert rig.speaker.said == []
    assert rig.speaker.chirped == []


async def test_window_open_never_double_chirps() -> None:
    # The prompt WAS the ack: a capture opening via an armed window must not
    # chirp again (the double-chirp complaint from the reopen era).
    rig = make_dialog(roles=[PILOT_ROLE], transcriber=_Transcriber(["mumble static"]))
    await utter_wake(rig)  # unframed → say-again window armed
    chirps_before = list(rig.speaker.chirped)
    assert rig.capture.armed
    rig.dialog.on_capture_start(USER, GUILD, CaptureOrigin.WINDOW, rig.capture.armed[-1][2])
    await drain(rig)
    assert rig.speaker.chirped == chirps_before


# ── @Pilot voice gate (GDD §11.1 layer 4, constraint 10 parity) ──────────────


async def test_non_pilot_voice_report_is_rejected_before_the_engine() -> None:
    rig = make_dialog(roles=[999], transcriber=_Transcriber(["aura command hostiles Otanuomi"]))
    await utter_wake(rig)
    assert rig.engine.reports == []  # never reaches the shared engine
    assert rig.health.rejected == 1
    assert (GUILD, "Reporting requires the Pilot role.") in rig.speaker.said


async def test_pilot_voice_report_reaches_the_engine() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE], transcriber=_Transcriber(["aura command hostiles Otanuomi"])
    )
    await utter_wake(rig)
    assert len(rig.engine.reports) == 1
    assert rig.engine.reports[0][2].intent is Intent.HOSTILE_SPOTTED


async def test_non_mention_intents_stay_open_to_non_pilots() -> None:
    rig = make_dialog(roles=[], transcriber=_Transcriber(["aura command status"]))
    await utter_wake(rig)
    assert len(rig.engine.reports) == 1
    assert rig.engine.reports[0][2].intent is Intent.QUERY
    assert rig.health.rejected == 0


# ── abandoned captures (GDD §5.4) ────────────────────────────────────────────


async def test_zero_speech_capture_never_reaches_stt() -> None:
    calls: list[bytes] = []

    class _Counting(_Transcriber):
        def transcribe(self, pcm: bytes, bias: str) -> TranscriptResult:
            calls.append(pcm)
            raise AssertionError("abandoned capture must not be decoded")

    rig = make_dialog(roles=[PILOT_ROLE], transcriber=_Counting())
    gen = wake_open(rig)
    await drain(rig)
    meta = CaptureMeta(CaptureOrigin.WAKE, gen, 0, "abandoned")
    await rig.dialog.on_utterance(USER, GUILD, b"", meta)
    assert calls == []
    assert rig.dialog.session_state(USER) is DialogState.IDLE
    # Silent: no say-again, no retry spent.
    assert rig.speaker.said == []


# ── STT failure path (GDD §20 watchdog → "say again") ────────────────────────


async def test_stt_timeout_arms_retry_window_and_says_again() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE], transcriber=_Transcriber(error=SttTimeoutError("watchdog"))
    )
    await utter_wake(rig)
    assert rig.engine.reports == []
    assert rig.health.rejected == 1
    assert len(rig.capture.armed) == 1
    assert (GUILD, "Say again the system.") in rig.speaker.said


async def test_stt_failure_loop_terminates_via_the_budget() -> None:
    # The live incident: a wedged STT + ambient noise looped "say again"
    # forever (the SttError path bypassed the loop guard). Every failure now
    # drains ONE budget; exhaustion ends audibly with standing-down.
    rig = make_dialog(
        roles=[PILOT_ROLE], transcriber=_Transcriber(error=SttTimeoutError("watchdog"))
    )
    await utter_wake(rig)
    for _ in range(10):  # noise keeps opening the armed windows
        if not rig.capture.armed or rig.dialog.session_state(USER) is DialogState.IDLE:
            break
        await utter_window(rig)
    # Windows armed exactly max_retries times (2), then the audible close:
    assert len(rig.capture.armed) == 2
    assert (GUILD, "Standing down. Wake me to retry.") in rig.speaker.said
    assert rig.dialog.session_state(USER) is DialogState.IDLE


# ── LOW-tier retry re-bind (GDD §8.3) ────────────────────────────────────────


async def test_low_tier_retry_rebinds_bare_system_name() -> None:
    rejected = IncidentOutcome(Outcome.REJECTED, None, None, None)
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["aura command hostiles zzzz qqqq", "Kisogo"]),
        outcome=rejected,
    )
    await utter_wake(rig)
    assert len(rig.engine.reports) == 1
    assert rig.engine.reports[0][3].tier.value == "LOW"
    assert len(rig.capture.armed) == 1  # retry window armed
    assert rig.dialog.session_state(USER) is DialogState.AWAIT_RETRY_SYSTEM

    # The bare name in the window re-binds to the rejected intent.
    await utter_window(rig)
    assert len(rig.engine.reports) == 2
    retried = rig.engine.reports[1][2]
    assert retried.intent is Intent.HOSTILE_SPOTTED
    assert retried.system_text == "Kisogo"
    assert retried.raw == "Kisogo"
    assert rig.dialog.session_state(USER) is DialogState.IDLE


async def test_failed_rebind_does_not_rearm_forever() -> None:
    # A rebind that ALSO scores LOW must not arm another window from the
    # Report path (budget aside, the rebound_from guard breaks the cycle).
    rejected = IncidentOutcome(Outcome.REJECTED, None, None, None)
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["aura command hostiles zzzz qqqq", "qqqq"]),
        outcome=rejected,
    )
    await utter_wake(rig)
    armed_after_first = len(rig.capture.armed)
    await utter_window(rig)
    assert len(rig.capture.armed) == armed_after_first  # no re-arm from the rebind


# ── MEDIUM-tier voice confirm (GDD §8.3 AWAIT_CONFIRM) ───────────────────────

_ASKED_CLEAR = IncidentOutcome(Outcome.ASKED, "Heard Otanuomi — say again to confirm.", None, None)


async def test_asked_outcome_arms_a_confirm_window() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["aura command clear Otanuomi"]),
        outcome=_ASKED_CLEAR,
    )
    await utter_wake(rig)
    assert len(rig.engine.reports) == 1
    assert (GUILD, "Heard Otanuomi — say again to confirm.") in rig.speaker.said
    assert len(rig.capture.armed) == 1  # the confirm window
    assert rig.dialog.session_state(USER) is DialogState.AWAIT_CONFIRM


async def test_voice_yes_completes_a_destructive_confirm_as_high_tier() -> None:
    # "clear Otanuomi" → MEDIUM → ASKED; "yes" in the window re-reports the
    # STORED candidate as a HIGH-tier resolution — no re-resolution, same
    # engine entry point (constraint 10).
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["aura command clear Otanuomi", "yes"]),
        outcome=_ASKED_CLEAR,
    )
    await utter_wake(rig)
    first_resolution = rig.engine.reports[0][3]
    assert first_resolution.best is not None

    await utter_window(rig)
    assert len(rig.engine.reports) == 2
    _, _, parsed2, resolution2 = rig.engine.reports[1]
    assert parsed2.intent is Intent.RESOLVE
    assert resolution2.tier is Tier.HIGH
    assert resolution2.best.system_id == first_resolution.best.system_id
    assert rig.engine.confirms == []  # no card to pin — nothing was posted


async def test_voice_yes_confirms_an_uncertain_card_via_the_button_path() -> None:
    # An uncertain report posted a card (incident_id set): "yes" completes
    # through the SAME confirm_system path the card's pick button hits.
    asked = IncidentOutcome(Outcome.ASKED, "Hostiles Otanuomi — say again to confirm.", None, 7)
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["aura command hostiles Otanuomi", "yes"]),
        outcome=asked,
    )
    await utter_wake(rig)
    assert rig.dialog.session_state(USER) is DialogState.AWAIT_CONFIRM
    best = rig.engine.reports[0][3].best

    await utter_window(rig)
    assert rig.engine.confirms == [(7, USER, best.system_id)]
    assert len(rig.engine.reports) == 1  # the posted card is pinned, not re-reported
    assert (GUILD, "Confirmed Otanuomi.") in rig.speaker.said
    assert rig.dialog.session_state(USER) is DialogState.IDLE


async def test_exact_repeat_completes_the_confirm_by_voice() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["aura command clear Otanuomi", "clear Otanuomi"]),
        outcome=_ASKED_CLEAR,
    )
    await utter_wake(rig)
    await utter_window(rig)
    assert len(rig.engine.reports) == 2
    assert rig.engine.reports[1][3].tier is Tier.HIGH


async def test_voice_no_closes_the_confirm_silently() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["aura command clear Otanuomi", "no"]),
        outcome=_ASKED_CLEAR,
    )
    await utter_wake(rig)
    said_before = list(rig.speaker.said)
    armed_before = len(rig.capture.armed)

    await utter_window(rig)
    assert len(rig.engine.reports) == 1  # nothing confirmed, nothing re-reported
    assert rig.engine.confirms == []
    assert rig.speaker.said == said_before  # standing-down semantics, silent
    assert len(rig.capture.armed) == armed_before  # no re-arm
    assert rig.dialog.session_state(USER) is DialogState.IDLE


async def test_unrelated_speech_in_the_confirm_window_closes_silently() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["aura command clear Otanuomi", "How's everybody doing"]),
        outcome=_ASKED_CLEAR,
    )
    await utter_wake(rig)
    await utter_window(rig)
    assert len(rig.engine.reports) == 1
    assert rig.engine.broadcasts == []  # never leaks into the relay path
    assert rig.dialog.session_state(USER) is DialogState.IDLE


async def test_bare_no_intent_utterance_is_relayed_in_open_mode() -> None:
    # relay_mode: open — the old catch-all: anything unmatched relays.
    rig = make_dialog(roles=[PILOT_ROLE], transcriber=_Transcriber(["Kisogo"]), relay_mode="open")
    await utter_wake(rig)
    assert rig.engine.reports == []
    assert rig.engine.broadcasts == [(GUILD, USER, "Kisogo", False)]


# ── spoken colour codes: dialogue + inline (GDD §6.4) ────────────────────────


async def test_bare_code_opens_dialogue_and_next_report_inherits_severity() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey jarvis code orange", "hostiles Otanuomi"]),
    )
    await utter_wake(rig)
    # Step 1: acknowledged, window armed, nothing posted yet.
    assert (GUILD, "Code orange. Go ahead.") in rig.speaker.said
    assert len(rig.capture.armed) == 1
    assert rig.engine.reports == [] and rig.engine.broadcasts == []
    assert rig.dialog.session_state(USER) is DialogState.AWAIT_SEVERITY_REPORT

    await utter_window(rig)
    # Step 2: the report inherits the pending severity.
    assert len(rig.engine.reports) == 1
    parsed = rig.engine.reports[0][2]
    assert parsed.intent is Intent.HOSTILE_SPOTTED
    assert parsed.severity is Severity.MEDIUM


async def test_inline_code_red_rides_the_report() -> None:
    rig = make_dialog(roles=[PILOT_ROLE], transcriber=_Transcriber(["code red hostiles Otanuomi"]))
    await utter_wake(rig)
    assert len(rig.engine.reports) == 1
    assert rig.engine.reports[0][2].severity is Severity.HIGH


async def test_severity_colours_a_framed_relay_but_noise_still_fails() -> None:
    # NEW §6.4 semantics: inherited severity is severity-carrying but NOT
    # framing — hallucinated noise after "code red" must never become a RED
    # card (live channel-pollution incident). Framed speech still relays
    # with the inherited colour.
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(
            ["code red", "report blop fleet moving to Kisogo gate end report"]
        ),
    )
    await utter_wake(rig)
    await utter_window(rig)
    assert len(rig.engine.broadcasts) == 1
    assert rig.engine.broadcast_severities == [Severity.HIGH]

    # And the noise variant: unframed continuation fails instead of posting.
    rig2 = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["code red", "thank you thank you"]),
    )
    await utter_wake(rig2)
    await utter_window(rig2)
    assert rig2.engine.broadcasts == []
    # Context survives into the retry window so a framed repeat still works:
    assert rig2.dialog.session_state(USER) is DialogState.AWAIT_SEVERITY_REPORT


# ── freeform relay confidence gate (GDD §8.6) ────────────────────────────────


async def test_low_confidence_gibberish_is_not_relayed() -> None:
    # -2.4 is deep in garbage territory (below dialog.retry_min_logprob):
    # the gate closes SILENTLY — no card, and no "say again" prompt into an
    # open mic that would only recapture more chatter (field round 2).
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["Rens, Rens, Rens"], avg_logprob=-2.4),
        relay_mode="open",  # open mode so the confidence gate itself is what fires
    )
    await utter_wake(rig)
    assert rig.engine.broadcasts == []  # hallucinated noise never becomes a card
    assert rig.health.rejected == 1
    assert not any("Say again" in t for _, t in rig.speaker.said)
    assert rig.capture.armed == []  # and no retry window


async def test_marginal_confidence_gibberish_still_gets_say_again() -> None:
    # Between the relay gate (-0.9) and the garbage floor (-1.3): a quiet
    # mic, not chatter — the say-again retry is still worth offering.
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["mumbled something"], avg_logprob=-1.1),
        relay_mode="open",
    )
    await utter_wake(rig)
    assert rig.engine.broadcasts == []
    assert (GUILD, "Say again the system.") in rig.speaker.said
    assert rig.capture.armed  # retry window armed


async def test_confident_relay_posts_and_acks() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["blop fleet moving to Kisogo gate whatever"]),
        outcome=IncidentOutcome(Outcome.POSTED, "Relayed.", None, None),
        relay_mode="open",
    )
    await utter_wake(rig)
    assert len(rig.engine.broadcasts) == 1
    assert (GUILD, "Relayed.") in rig.speaker.said


# ── relay framing (GDD §8.6, relay_mode) ─────────────────────────────────────


async def test_unframed_speech_never_becomes_a_card_by_default() -> None:
    # relay_mode: framed (default) — crosstalk and mishearings get "Say
    # again?", never a card.
    rig = make_dialog(roles=[PILOT_ROLE], transcriber=_Transcriber(["How's everybody else doing"]))
    await utter_wake(rig)
    assert rig.engine.broadcasts == []
    assert rig.health.rejected == 1
    assert (GUILD, "Say again?") in rig.speaker.said


async def test_framed_report_relays_under_default_mode() -> None:
    # A "report … end report" envelope is explicit framing: it relays.
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey jarvis report blop fleet on the Kisogo gate end report"]),
        outcome=IncidentOutcome(Outcome.POSTED, "Relayed.", None, None),
    )
    await utter_wake(rig)
    assert len(rig.engine.broadcasts) == 1
    assert rig.engine.broadcasts[0][2] == "blop fleet on the Kisogo gate"
    assert (GUILD, "Relayed.") in rig.speaker.said


async def test_relay_off_drops_even_framed_speech() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["report blop fleet on the Kisogo gate end report"]),
        relay_mode="off",
    )
    await utter_wake(rig)
    assert rig.engine.broadcasts == []
    assert rig.health.rejected == 1
    assert (GUILD, "Say again?") in rig.speaker.said


# ── the retry budget (GDD §5.4 — the say-again loop killer) ──────────────────


async def test_say_again_budget_exhausts_audibly_and_wake_resets() -> None:
    # Consecutive noise drains the budget (2), closes audibly, and never
    # re-arms — then a fresh wake works normally again.
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(
            ["mumble static", "thank you thank you", "more noise", "clear Otanuomi"]
        ),
    )
    await utter_wake(rig)
    assert (GUILD, "Say again?") in rig.speaker.said
    assert len(rig.capture.armed) == 1

    await utter_window(rig)  # retry 2 of 2
    assert len(rig.capture.armed) == 2

    await utter_window(rig)  # budget gone: audible close, NO new window
    assert len(rig.capture.armed) == 2
    assert (GUILD, "Standing down. Wake me to retry.") in rig.speaker.said
    assert rig.engine.broadcasts == []
    assert rig.dialog.session_state(USER) is DialogState.IDLE

    # A fresh wake with a real command still works normally.
    await utter_wake(rig)
    assert len(rig.engine.reports) == 1


async def test_stale_gen_utterance_is_dropped() -> None:
    rig = make_dialog(roles=[PILOT_ROLE], transcriber=_Transcriber(["hostiles Otanuomi"]))
    gen = wake_open(rig)
    await drain(rig)
    stale = CaptureMeta(CaptureOrigin.WAKE, gen + 7, 5, "silence")
    await rig.dialog.on_utterance(USER, GUILD, b"\x00\x00", stale)
    assert rig.engine.reports == []  # never decoded, never reported


# ── command override wiring (GDD §6.6) ───────────────────────────────────────


async def test_override_routes_to_chat_and_speaks_reply() -> None:
    chat = _Chat()
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey jarvis command override what's the weather in Chicago"]),
        chat=chat,
        chat_enabled=True,
    )
    await utter_wake(rig)
    assert chat.asked == [(USER, "what's the weather in Chicago")]
    assert (GUILD, "Sunny, 21 degrees.") in rig.speaker.said
    assert rig.engine.reports == [] and rig.engine.broadcasts == []  # never touches intel


async def test_override_failure_speaks_fixed_line() -> None:
    from cortana.chat import ChatError

    chat = _Chat(error=ChatError("boom"))
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["command override tell me a story"]),
        chat=chat,
        chat_enabled=True,
    )
    await utter_wake(rig)
    assert (GUILD, "Override channel unavailable.") in rig.speaker.said
    assert rig.engine.broadcasts == []


async def test_override_while_disabled_speaks_unavailable() -> None:
    # chat disabled: an explicit override request gets the fixed unavailable
    # line — never a silent fall-through to the grammar.
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["command override what's the weather in Chicago"]),
        relay_mode="open",
        chat=None,
    )
    await utter_wake(rig)
    assert rig.engine.broadcasts == [] and rig.engine.reports == []
    assert (GUILD, "Override channel unavailable.") in rig.speaker.said


async def test_bare_override_opens_dialogue_then_takes_the_question() -> None:
    # "command override" alone (window closed on the pause) → ack + window;
    # the NEXT utterance is the question verbatim, no prefix needed.
    chat = _Chat()
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey jarvis command override", "what's the weather in Chicago"]),
        chat=chat,
        chat_enabled=True,
    )
    await utter_wake(rig)
    assert chat.asked == []
    assert (GUILD, "Go ahead.") in rig.speaker.said
    assert len(rig.capture.armed) == 1
    assert rig.dialog.session_state(USER) is DialogState.AWAIT_OVERRIDE_QUESTION

    await utter_window(rig)
    assert chat.asked == [(USER, "what's the weather in Chicago")]
    assert rig.engine.broadcasts == [] and rig.engine.reports == []


async def test_bare_override_while_disabled_speaks_unavailable() -> None:
    rig = make_dialog(roles=[PILOT_ROLE], transcriber=_Transcriber(["command override"]), chat=None)
    await utter_wake(rig)
    assert (GUILD, "Override channel unavailable.") in rig.speaker.said
    assert rig.capture.armed == []


async def test_override_noise_never_burns_an_api_call() -> None:
    chat = _Chat()
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["command override", "mmmm"], avg_logprob=-2.5),
        chat=chat,
        chat_enabled=True,
    )
    # Force only the SECOND utterance to be low-confidence:
    rig2 = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber([]),
        chat=chat,
        chat_enabled=True,
    )
    del rig2  # (single-rig flow below)

    class _TwoConf(_Transcriber):
        def transcribe(self, pcm: bytes, bias: str) -> TranscriptResult:
            text = self.texts.pop(0)
            logprob = -0.1 if text == "command override" else -2.5
            return TranscriptResult(text=text, avg_logprob=logprob)

    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_TwoConf(["command override", "mmmm"]),
        chat=chat,
        chat_enabled=True,
    )
    await utter_wake(rig)
    await utter_window(rig)
    assert chat.asked == []  # noise decoded in the window: no paid call


# ── dropped acks stay out of channels ────────────────────────────────────────


async def test_unspoken_ack_is_dropped_not_posted() -> None:
    # An ACK line that can't be spoken (muted/over-cap/synth fail) is logged
    # and dropped — retry prompts pasted into the intel channel are noise.
    rig = make_dialog(roles=[PILOT_ROLE], transcriber=_Transcriber(["mumble static"]))

    async def silent_say(guild_id, text, priority=1, *, user_id=None):
        rig.speaker.said.append((guild_id, text))
        return False  # speech failed

    rig.speaker.say = silent_say  # type: ignore[method-assign]
    await utter_wake(rig)
    assert rig.sent == []  # nothing posted to any channel


# ── cleanup: reset_user / reset_all ──────────────────────────────────────────


async def test_reset_user_purges_session_and_capture() -> None:
    rig = make_dialog(roles=[PILOT_ROLE], transcriber=_Transcriber(["mumble static"]))
    await utter_wake(rig)  # leaves an armed window + AWAIT state
    assert rig.dialog.session_state(USER) is not DialogState.IDLE
    rig.dialog.reset_user(USER)
    assert rig.dialog.session_state(USER) is DialogState.IDLE
    assert USER in rig.capture.dropped


async def test_reset_all_covers_every_tracked_user() -> None:
    rig = make_dialog(roles=[PILOT_ROLE], transcriber=_Transcriber(["mumble static"] * 2))
    await utter_wake(rig, user=USER)
    await utter_wake(rig, user=USER + 1)
    rig.dialog.reset_all()
    assert rig.dialog.session_state(USER) is DialogState.IDLE
    assert rig.dialog.session_state(USER + 1) is DialogState.IDLE
    assert {USER, USER + 1} <= set(rig.capture.dropped)


# ── App-level wiring (composition root) ──────────────────────────────────────


def make_app(*, roles: list[int]) -> tuple[App, Rig]:
    holder = StubHolder(make_config())
    app = App(holder)  # type: ignore[arg-type]
    rig = make_dialog(roles=roles, transcriber=_Transcriber([]))
    app.dialog = rig.dialog
    app.capture = rig.capture  # type: ignore[assignment]
    app.engine = rig.engine  # type: ignore[assignment]
    app.health = rig.health  # type: ignore[assignment]
    app.speaker = rig.speaker  # type: ignore[assignment]
    app.bot = _Bot(roles)  # type: ignore[assignment]
    app.conn = None
    return app, rig


async def test_left_control_purges_per_user_state() -> None:
    # §19 posture: when a pilot leaves voice, every per-user trace goes.
    app, rig = make_app(roles=[PILOT_ROLE])
    wake_open(rig)
    await drain(rig)
    await app._on_control({"t": "left", "user_id": str(USER)})
    assert USER in rig.capture.dropped
    assert rig.dialog.session_state(USER) is DialogState.IDLE


async def test_hello_control_resets_every_dialog() -> None:
    # Ears reconnect: every armed window it knew about is gone (GDD §5.4).
    app, rig = make_app(roles=[PILOT_ROLE])
    wake_open(rig)
    await drain(rig)
    await app._on_control({"t": "hello", "version": "test"})
    assert rig.dialog.session_state(USER) is DialogState.IDLE


class _StateRecordingGateway:
    def __init__(self) -> None:
        self.states: list[tuple[bool, int | None]] = []

    async def on_ears_state(self, connected: bool, channel_id: int | None) -> None:
        self.states.append((connected, channel_id))


async def test_snapshot_control_feeds_connect_state_to_the_gateway() -> None:
    """The snapshot's per-guild connected/channel fields must reach the
    voice gateway (which owns the rejoin/adopt/leave judgement) — after an
    unclean Brain death this is the only way a fresh Brain learns Ears is
    still parked in a voice channel."""
    app, _rig = make_app(roles=[PILOT_ROLE])
    gateway = _StateRecordingGateway()
    app.gateway = gateway  # type: ignore[assignment]

    await app._on_control(
        {
            "t": "snapshot",
            "guilds": [
                {"guild_id": "999", "channel_id": "5", "connected": True, "users": []},
                {"guild_id": str(GUILD), "channel_id": "9", "connected": True, "users": []},
            ],
        }
    )

    assert gateway.states == [(True, 9)]  # the CONFIGURED guild's state


async def test_snapshot_without_the_guild_reports_disconnected() -> None:
    app, _rig = make_app(roles=[PILOT_ROLE])
    gateway = _StateRecordingGateway()
    app.gateway = gateway  # type: ignore[assignment]

    await app._on_control({"t": "snapshot", "guilds": []})
    await app._on_control(
        {
            "t": "snapshot",
            "guilds": [{"guild_id": str(GUILD), "channel_id": None, "connected": False}],
        }
    )

    assert gateway.states == [(False, None), (False, None)]


async def test_timer_and_reminder_polls_wait_for_bot_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A due timer/reminder is never consumed (fired=1) before login: the poll
    body is skipped entirely while bot.is_ready() is False."""
    monkeypatch.setattr(app_main, "_TIMER_POLL_INTERVAL_S", 0.01)
    app, rig = make_app(roles=[PILOT_ROLE])
    reminders = _Reminders()
    app.reminders = reminders  # type: ignore[assignment]

    timer_task = asyncio.create_task(app._timer_loop())
    reminder_task = asyncio.create_task(app._reminder_loop())
    try:
        await asyncio.sleep(0.08)
        assert rig.engine.fired == []  # nothing consumed pre-ready
        assert reminders.delivered == []

        app.bot.ready = True  # type: ignore[union-attr]
        await asyncio.sleep(0.08)
        assert rig.engine.fired  # polls resume once Discord is usable
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
    app, _ = make_app(roles=[PILOT_ROLE])
    rec = _RecordingLog()
    monkeypatch.setattr(app_main, "log", rec)
    app._shutdown.set()  # process is already shutting down

    app._spawn("noop", _returns_immediately())
    await asyncio.gather(*app._tasks)

    assert ("error", "critical_task_exited") not in rec.calls


async def test_task_exit_while_running_alarms_and_triggers_shutdown(monkeypatch) -> None:
    app, _ = make_app(roles=[PILOT_ROLE])
    rec = _RecordingLog()
    monkeypatch.setattr(app_main, "log", rec)
    assert not app._shutdown.is_set()

    app._spawn("noop", _returns_immediately())
    await asyncio.gather(*app._tasks)

    assert ("error", "critical_task_exited") in rec.calls
    assert app._shutdown.is_set()  # a task dying mid-run brings the process down


async def test_graceful_shutdown_is_bounded(monkeypatch) -> None:
    app, _ = make_app(roles=[PILOT_ROLE])

    async def _hang() -> None:
        await asyncio.sleep(100)

    monkeypatch.setattr(app, "_shutdown_sequence", _hang)
    monkeypatch.setattr(app_main, "_SHUTDOWN_TIMEOUT_S", 0.05)

    # A wedged close must not hang the process: the wait_for bound returns
    # control promptly instead of blocking until systemd's SIGKILL.
    await asyncio.wait_for(app._graceful_shutdown(), timeout=2)


async def test_refresh_chat_follows_config_and_key_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SIGHUP path: flipping chat.enabled (or a key appearing) takes effect on
    # reload — it used to require a full restart, silently.
    app, _ = make_app(roles=[PILOT_ROLE])
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


async def test_stale_arm_window_action_is_dropped() -> None:
    # Regression (confirmed review finding): ArmWindow executes after the
    # spoken prompt resolves; if the pilot re-woke in that gap the session
    # gen has advanced and the stale arm must be a no-op — executing it
    # would destroy the in-flight capture or strand an unowned window.
    rig = make_dialog(roles=[PILOT_ROLE], transcriber=_Transcriber(["mumble static"]))
    await utter_wake(rig)  # fails -> AWAIT with an armed window (gen N+1)
    stale_session = rig.dialog._sessions[USER]
    armed_before = len(rig.capture.armed)

    # Pilot re-wakes: fresh dialog supersedes the AWAIT gen.
    wake_open(rig)
    await drain(rig)

    # The stale ArmWindow from the superseded dialog fires late:
    rig.dialog._arm_window(stale_session, stale_session.gen)
    assert len(rig.capture.armed) == armed_before  # dropped, not armed
    assert USER not in rig.dialog._deadlines  # no unowned deadline created


async def test_capped_confirmation_still_speaks_a_short_posted() -> None:
    """Field report: a long verbatim system name pushed the confirmation
    line over the §12.2 cap — the speaker refused it, the fallback went to
    channel TEXT, and the pilot heard silence and assumed the report was
    swallowed. The reply path now speaks the minimal 'Posted.' instead."""
    long_line = "Under attack M-TAC-O requiring, posted."
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["under attack hostiles Otanuomi"]),
        outcome=IncidentOutcome(Outcome.POSTED, long_line, None, 1),
    )

    async def capped_say(guild_id, text, priority=1, *, user_id=None):
        rig.speaker.said.append((guild_id, text))
        return len(text) <= 12  # only short lines fit the cap

    rig.speaker.say = capped_say  # type: ignore[method-assign]
    await utter_wake(rig)
    assert (GUILD, long_line) in rig.speaker.said  # tried the full line
    assert (GUILD, "Posted.") in rig.speaker.said  # then the audible short ack
    assert rig.sent == []  # no channel-text dump needed


# ── fun commands (GDD §13.2): voice in, voice out — never a channel post ─────


def _fun_engine(tmp_path, *, fact_cooldown_s: int = 0, insult_cooldown_s: int = 0):
    import dataclasses
    import json

    from cortana.config import FunConfig
    from cortana.core.fun import FunEngine, load_library

    base = tmp_path / "facts"
    base.mkdir(exist_ok=True)
    (base / "astronomy.json").write_text(
        json.dumps(["Saturn is so light it would float in a big enough bathtub."])
    )
    (base / "insults_test.json").write_text(
        json.dumps([{"text": "You fly like a shuttle with no capacitor.", "spicy": False}])
    )
    holder = StubHolder(
        dataclasses.replace(
            make_config(),
            fun=FunConfig(fact_cooldown_s=fact_cooldown_s, insult_cooldown_s=insult_cooldown_s),
        )
    )
    return FunEngine(holder, load_library(base))  # type: ignore[arg-type]


async def test_voice_fact_speaks_and_posts_nothing(tmp_path) -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey cortana, tell me a fact"]),
        fun=_fun_engine(tmp_path),
    )
    await utter_wake(rig)
    texts = [t for _, t in rig.speaker.said]
    assert "Saturn is so light it would float in a big enough bathtub." in texts
    assert rig.engine.reports == []  # never reaches the incident engine
    assert rig.sent == []  # voice in, voice out — no channel post
    assert 20.0 in rig.speaker.caps  # spoken under the fun cap, not §12.2's


async def test_voice_insult_addresses_the_spoken_target(tmp_path) -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey cortana, roast dave"]),
        fun=_fun_engine(tmp_path),
    )
    await utter_wake(rig)
    texts = [t for _, t in rig.speaker.said]
    assert "Dave. You fly like a shuttle with no capacitor." in texts
    assert rig.engine.reports == [] and rig.sent == []


async def test_voice_fact_without_engine_says_off() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey cortana, tell me a fact"]),
        fun=None,
    )
    await utter_wake(rig)
    assert (GUILD, "Fun commands are off.") in rig.speaker.said
    assert rig.sent == []


async def test_voice_fact_cooldown_speaks_the_throttle_line(tmp_path) -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey cortana, tell me a fact"] * 2),
        fun=_fun_engine(tmp_path, fact_cooldown_s=60),
    )
    await utter_wake(rig)
    await utter_wake(rig)
    assert (GUILD, "Cooling down.") in rig.speaker.said
    assert rig.sent == []


async def test_stt_bias_carries_the_command_vocabulary(tmp_path) -> None:
    # Live incident: the gazetteer-only bias prompt dragged casual command
    # words toward system names ("roast" -> "Woust"). The grammar's trigger
    # vocabulary now rides at the tail of the bias text.
    transcriber = _Transcriber(["hey cortana, tell me a fact"])
    rig = make_dialog(roles=[PILOT_ROLE], transcriber=transcriber, fun=_fun_engine(tmp_path))
    await utter_wake(rig)
    assert transcriber.biases, "transcribe was never called"
    assert "insult, roast" in transcriber.biases[0]
    assert "tackled" in transcriber.biases[0]


# ── field round 2: dismissal, garbage gate, confirm-first (GDD §5.4/§8.3) ────


async def test_dismissal_stands_down_end_to_end() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey cortana, end transmission"]),
    )
    await utter_wake(rig)
    assert any("Standing down" in t for _, t in rig.speaker.said)
    assert rig.engine.reports == [] and rig.engine.broadcasts == []
    assert rig.capture.armed == []  # no retry window


async def test_confirm_first_low_report_asks_then_yes_commits() -> None:
    # "zanzibar" is nowhere near the gazetteer → LOW/no-match → confirm-first.
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey cortana, under attack in zanzibar", "yes"]),
        confirm_reports="low",
    )
    await utter_wake(rig)
    # Natural readback: the situation, not just the system ("Under attack in
    # zanzibar. Confirm?") so a wrong intent is as catchable as a wrong system.
    assert any(
        "Under attack" in t and "zanzibar" in t and "Confirm" in t for _, t in rig.speaker.said
    )
    assert rig.engine.reports == []  # nothing committed yet
    assert rig.capture.armed  # confirm window armed
    await utter_window(rig)  # "yes"
    assert len(rig.engine.reports) == 1
    _, _, parsed, _resolution = rig.engine.reports[0]
    assert parsed.intent is Intent.UNDER_ATTACK
    assert parsed.system_text == "zanzibar"


async def test_confirm_first_no_reopens_say_again() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey cortana, under attack in zanzibar", "no"]),
        confirm_reports="low",
    )
    await utter_wake(rig)
    await utter_window(rig)  # "no" → decline, say-again retry armed
    assert rig.engine.reports == []
    assert any("Say again" in t for _, t in rig.speaker.said)
    assert rig.dialog.session_state(USER) is DialogState.AWAIT_RETRY_SYSTEM


async def test_confirm_first_off_commits_immediately() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey cortana, under attack in zanzibar"]),
        confirm_reports="off",
    )
    await utter_wake(rig)
    assert len(rig.engine.reports) == 1  # legacy behavior preserved


async def test_confirm_first_never_gates_high_tier_in_low_mode() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey cortana, hostiles Otanuomi"]),
        confirm_reports="low",
    )
    await utter_wake(rig)
    assert len(rig.engine.reports) == 1  # exact match commits straight away
    assert not any("Confirm" in t for _, t in rig.speaker.said)


async def test_confirm_first_always_gates_high_tier_and_yes_forces_candidate() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey cortana, hostiles Otanuomi", "affirmative"]),
        confirm_reports="always",
    )
    await utter_wake(rig)
    assert rig.engine.reports == []
    # "Hostiles in Otanuomi. Confirm?" — intent-aware readback (GDD §8.3).
    assert any("Hostiles in Otanuomi" in t and "Confirm" in t for _, t in rig.speaker.said)
    await utter_window(rig)
    assert len(rig.engine.reports) == 1
    _, _, _parsed, resolution = rig.engine.reports[0]
    assert resolution is not None and resolution.tier is Tier.HIGH  # forced candidate


async def test_transcript_channel_off_posts_nothing() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey cortana, hostiles Otanuomi"]),
    )
    await utter_wake(rig)
    await drain(rig)
    assert rig.sent == []  # no transcript channel configured → no review post


async def test_transcript_channel_logs_one_line_per_utterance() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey cortana, hostiles Otanuomi"]),
        transcript_channel=777,
    )
    await utter_wake(rig)
    await drain(rig)
    posts = [(cid, text) for cid, text in rig.sent if cid == 777]
    assert len(posts) == 1
    _, line = posts[0]
    assert "hostiles Otanuomi" in line
    assert Intent.HOSTILE_SPOTTED.value in line


async def test_transcript_channel_logs_unmatched_utterances_too() -> None:
    # The whole point: see the phrasings that DON'T match yet (live request).
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey cortana, some gibberish here"]),
        transcript_channel=777,
        relay_mode="framed",
    )
    await utter_wake(rig)
    await drain(rig)
    posts = [text for cid, text in rig.sent if cid == 777]
    assert len(posts) == 1
    assert "no command" in posts[0]


# ── live recognition / streaming early-commit (GDD §5.5) ─────────────────────


def test_early_committable_predicate() -> None:
    from cortana.dialog.engine import _early_committable
    from cortana.nlu.grammar import parse

    assert _early_committable(parse("hostiles Otanuomi")) is True  # complete report
    assert _early_committable(parse("status")) is True  # systemless command
    assert _early_committable(parse("under attack")) is False  # system not spoken yet
    assert _early_committable(parse("uh what was that")) is False  # no intent
    assert _early_committable(None) is False


async def test_partial_decode_commits_complete_command() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey cortana hostiles Otanuomi"]),
        streaming=True,
    )
    await rig.dialog._partial_decode(USER, b"\x00\x00", 7)
    # Ended the capture early, gen-guarded — the final decode does the routing.
    assert rig.capture.endpoints == [(USER, "early_command", 7)]


async def test_partial_decode_waits_for_the_system_name() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey cortana under attack"]),  # system still coming
        streaming=True,
    )
    await rig.dialog._partial_decode(USER, b"\x00\x00", 7)
    assert rig.capture.endpoints == []  # don't clip the pilot mid-command


async def test_partial_decode_ignores_low_confidence() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hostiles Otanuomi"], avg_logprob=-2.5),
        streaming=True,
    )
    await rig.dialog._partial_decode(USER, b"\x00\x00", 7)
    assert rig.capture.endpoints == []  # too uncertain to commit early


async def test_partial_decode_failure_is_a_non_event() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(error=RuntimeError("queue overflow")),
        streaming=True,
    )
    rig.dialog._partial_inflight.add(USER)
    await rig.dialog._partial_decode(USER, b"\x00\x00", 7)
    assert USER not in rig.dialog._partial_inflight  # cleared even on failure
    assert rig.capture.endpoints == []  # normal endpoint still catches it


async def test_maybe_partial_decode_rate_limits_by_speech() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hostiles Otanuomi"]),
        streaming=True,
    )
    # 10 frames = 200ms < partial_min_speech_ms (900): too short to decode.
    rig.capture.progress[USER] = (b"\x00\x00", 10, 3)
    rig.dialog._maybe_partial_decode(USER)
    assert USER not in rig.dialog._partial_inflight
    # 60 frames = 1200ms: past the minimum and the cadence → one decode fires.
    rig.capture.progress[USER] = (b"\x00\x00", 60, 3)
    rig.dialog._maybe_partial_decode(USER)
    assert USER in rig.dialog._partial_inflight
    assert rig.dialog._partial_at_frames[USER] == 60
    await drain(rig)
    assert USER not in rig.dialog._partial_inflight  # cleared when the decode finishes


async def test_tick_streaming_early_commits_a_live_command() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey cortana hostiles Otanuomi"]),
        streaming=True,
    )
    gen = wake_open(rig)
    await drain(rig)
    now = rig.dialog._now()
    rig.dialog._grace_until[USER] = 0.0  # ack grace passed
    rig.dialog._last_audio_at[USER] = now  # recent audio: no silence endpoint
    rig.capture.capturing = [USER]
    rig.capture.progress[USER] = (b"\x00\x00", 60, gen)
    await rig.dialog._tick()
    await drain(rig)
    assert (USER, "early_command", gen) in rig.capture.endpoints


async def test_tick_streaming_off_never_partials() -> None:
    rig = make_dialog(
        roles=[PILOT_ROLE],
        transcriber=_Transcriber(["hey cortana hostiles Otanuomi"]),
        streaming=False,
    )
    now = rig.dialog._now()
    rig.dialog._grace_until[USER] = 0.0
    rig.dialog._last_audio_at[USER] = now
    rig.capture.capturing = [USER]
    rig.capture.progress[USER] = (b"\x00\x00", 60, 1)
    await rig.dialog._tick()
    await drain(rig)
    assert rig.capture.endpoints == []


async def test_restart_via_slash_trips_the_shutdown_event() -> None:
    app = App.__new__(App)  # only the shutdown event is touched
    app._shutdown = asyncio.Event()
    await app._request_restart()
    assert app._shutdown.is_set()
