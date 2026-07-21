"""Tests for the HealthReporter's audio-pipeline probes: the wake counters
snapshot, the wake fault latch, and the STT watchdog latch — GDD §20.

Everything is faked at the injected seams (probe callables, post_fn, the
AlarmBus); no discord, no audio deps. Degradation transitions raise/clear
alarm codes through the bus (GDD §11.3) — the fake records them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from cortana.alarms import AlarmCode
from cortana.health import HealthReporter

WAKE_COUNTERS = {
    "frames_seen": 1200,
    "vad_speech": 0,
    "inferences": 0,
    "hits": 0,
    "near_misses": 0,
}


class FakeAlarmBus:
    """Records raise/clear calls; stands in for cortana.alarms.AlarmBus."""

    def __init__(self) -> None:
        self.raised: list[tuple[AlarmCode, str | None]] = []
        self.cleared: list[tuple[AlarmCode, str | None]] = []

    async def raise_alarm(
        self,
        code: AlarmCode,
        severity: Any,
        summary: str,
        fix_hint: str,
        key: str | None = None,
    ) -> None:
        self.raised.append((code, key))

    async def clear(self, code: AlarmCode, key: str | None = None) -> None:
        self.cleared.append((code, key))


def make_reporter(posts: list[str], bus: FakeAlarmBus | None = None) -> HealthReporter:
    async def post(content: str, embed: dict[str, Any] | None) -> None:
        posts.append(content)

    holder = SimpleNamespace(
        current=SimpleNamespace(
            health=SimpleNamespace(report_interval_min=60, voice_silence_alarm_s=60)
        )
    )
    reporter = HealthReporter(holder, post)  # type: ignore[arg-type]
    if bus is not None:
        reporter.set_alarm_bus(bus)  # type: ignore[arg-type]
    return reporter


def test_unwired_probes_never_alarm_and_report_na() -> None:
    reporter = make_reporter([])
    assert reporter.wake_counters == {}
    assert reporter.wake_faulted is False
    assert reporter.stt_watchdog_degraded is False
    embed = reporter.build_report_embed(datetime.now(UTC))
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Wake pipeline"] == "n/a"
    assert fields["STT watchdog"] == "ok"
    assert fields["Status"].startswith("nominal")


async def test_stt_watchdog_latch_raises_alarm_once_and_clears() -> None:
    bus = FakeAlarmBus()
    reporter = make_reporter([], bus)
    latched = {"on": False}
    reporter.set_stt_probe(lambda: latched["on"])

    await reporter.check()
    assert bus.raised == []

    latched["on"] = True
    await reporter.check()
    await reporter.check()  # second check must NOT re-raise (the bus dedupes
    # anyway, but the transition itself fires once)
    assert bus.raised == [(AlarmCode.STT_DEGRADED, "watchdog")]
    assert reporter.stt_watchdog_degraded is True

    latched["on"] = False
    await reporter.check()
    assert (AlarmCode.STT_DEGRADED, "watchdog") in bus.cleared


async def test_wake_fault_raises_alarm_once_and_clears() -> None:
    bus = FakeAlarmBus()
    reporter = make_reporter([], bus)
    faulted = {"on": False}
    reporter.set_wake_probe(lambda: dict(WAKE_COUNTERS), lambda: faulted["on"])

    await reporter.check()
    assert bus.raised == []

    faulted["on"] = True
    await reporter.check()
    await reporter.check()
    assert bus.raised == [(AlarmCode.WAKE_FAULTED, None)]
    assert reporter.wake_faulted is True

    faulted["on"] = False
    await reporter.check()
    assert (AlarmCode.WAKE_FAULTED, None) in bus.cleared


async def test_ears_never_connected_raises_ears_down() -> None:
    bus = FakeAlarmBus()
    clock = {"now": 0.0}
    posts: list[str] = []

    async def post(content: str, embed: dict[str, Any] | None) -> None:
        posts.append(content)

    holder = SimpleNamespace(
        current=SimpleNamespace(
            health=SimpleNamespace(report_interval_min=60, voice_silence_alarm_s=60)
        )
    )
    reporter = HealthReporter(holder, post, clock=lambda: clock["now"])  # type: ignore[arg-type]
    reporter.set_alarm_bus(bus)  # type: ignore[arg-type]

    await reporter.check()
    assert bus.raised == []  # startup grace window

    clock["now"] = 20.0  # past HEARTBEAT_TIMEOUT_S with no heartbeat ever
    await reporter.check()
    assert (AlarmCode.EARS_DOWN, None) in bus.raised

    reporter.note_heartbeat({"connected": True})
    clock["now"] = 21.0
    await reporter.check()
    assert (AlarmCode.EARS_DOWN, None) in bus.cleared
    assert reporter.ears_down is False


def _clocked_reporter(bus: FakeAlarmBus, clock: dict[str, float]) -> HealthReporter:
    """A reporter with an injected clock and a large heartbeat timeout, so the
    voice-latch tests can advance time without tripping the EARS_DOWN detector."""

    async def post(content: str, embed: dict[str, Any] | None) -> None:
        return None

    holder = SimpleNamespace(
        current=SimpleNamespace(
            health=SimpleNamespace(report_interval_min=60, voice_silence_alarm_s=60)
        )
    )
    reporter = HealthReporter(
        holder,  # type: ignore[arg-type]
        post,
        clock=lambda: clock["now"],
        heartbeat_timeout_s=10_000.0,
    )
    reporter.set_alarm_bus(bus)  # type: ignore[arg-type]
    return reporter


async def _raise_voice_absent(
    reporter: HealthReporter, bus: FakeAlarmBus, clock: dict[str, float]
) -> None:
    """Drive the reporter into the latched VOICE_ABSENT state: two humans, Ears
    connected, and no audio for the whole alarm window."""
    reporter.set_humans_present(2)
    reporter.note_heartbeat({"connected": True})
    clock["now"] = 61.0  # past voice_silence_alarm_s (60) with no note_audio
    await reporter.check()
    assert reporter.voice_offline is True
    assert reporter.degraded is True
    assert (AlarmCode.VOICE_ABSENT, None) in bus.raised


async def test_voice_offline_latch_clears_when_audience_leaves() -> None:
    """Once the channel empties below two humans, the very next tick lifts the
    VOICE_ABSENT latch and its alarm — the alarm is only meaningful while there
    are pilots to receive — even though no audio ever flowed again."""
    bus = FakeAlarmBus()
    clock = {"now": 0.0}
    reporter = _clocked_reporter(bus, clock)
    await _raise_voice_absent(reporter, bus, clock)

    reporter.set_humans_present(0)  # audience precondition now false
    clock["now"] = 62.0
    await reporter.check()  # no note_audio()

    assert reporter.voice_offline is False
    assert reporter.degraded is False
    assert (AlarmCode.VOICE_ABSENT, None) in bus.cleared


async def test_voice_offline_latch_clears_when_ears_disconnects() -> None:
    """The other arm of the precondition: Ears reporting disconnected also drops
    the audience, so the latch lifts on the next tick without fresh audio."""
    bus = FakeAlarmBus()
    clock = {"now": 0.0}
    reporter = _clocked_reporter(bus, clock)
    await _raise_voice_absent(reporter, bus, clock)

    reporter.note_heartbeat({"connected": False})  # Ears disconnected
    clock["now"] = 62.0
    await reporter.check()  # no note_audio()

    assert reporter.voice_offline is False
    assert (AlarmCode.VOICE_ABSENT, None) in bus.cleared


async def test_voice_offline_latch_clears_when_audio_flows_again() -> None:
    """The original recovery path still works: pilots stay in channel and audio
    starts flowing again, so the latch clears via the audio-recovered branch."""
    bus = FakeAlarmBus()
    clock = {"now": 0.0}
    reporter = _clocked_reporter(bus, clock)
    await _raise_voice_absent(reporter, bus, clock)

    reporter.note_heartbeat({"connected": True})  # audience stays (2 humans)
    reporter.note_audio()  # audio flows again at t=61
    clock["now"] = 62.0
    await reporter.check()

    assert reporter.voice_offline is False
    assert (AlarmCode.VOICE_ABSENT, None) in bus.cleared


def test_report_embed_carries_wake_counters_and_latch_state() -> None:
    reporter = make_reporter([])
    reporter.set_wake_probe(lambda: dict(WAKE_COUNTERS), lambda: True)
    reporter.set_stt_probe(lambda: True)

    embed = reporter.build_report_embed(datetime.now(UTC))
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    # The silent-wake-death signature: frames flowing, nothing scored.
    assert "frames 1200" in fields["Wake pipeline"]
    assert "scored 0" in fields["Wake pipeline"]
    assert fields["Wake pipeline"].startswith("FAULTED")
    assert fields["STT watchdog"] == "latched (degraded)"
    assert fields["Status"].startswith("degraded")
    assert "STT watchdog latched" in fields["Status"]
    assert "wake model faulted" in fields["Status"]
    # The engine-facing degraded flag is untouched by the new latches — they
    # speak through their own #bot-health alerts (additive contract).
    assert reporter.degraded is False
