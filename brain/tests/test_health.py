"""Tests for the HealthReporter's audio-pipeline probes: the wake counters
snapshot, the wake fault latch, and the STT watchdog latch — GDD §20.

Everything is faked at the injected seams (probe callables, post_fn); no
discord, no audio deps.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from cortana.health import HealthReporter

WAKE_COUNTERS = {
    "frames_seen": 1200,
    "vad_speech": 0,
    "inferences": 0,
    "hits": 0,
    "near_misses": 0,
}


def make_reporter(posts: list[str]) -> HealthReporter:
    async def post(content: str, embed: dict[str, Any] | None) -> None:
        posts.append(content)

    holder = SimpleNamespace(
        current=SimpleNamespace(
            health=SimpleNamespace(report_interval_min=60, voice_silence_alarm_s=60)
        )
    )
    return HealthReporter(holder, post)  # type: ignore[arg-type]


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


async def test_stt_watchdog_latch_announces_once_and_recovers() -> None:
    posts: list[str] = []
    reporter = make_reporter(posts)
    latched = {"on": False}
    reporter.set_stt_probe(lambda: latched["on"])

    await reporter.check()
    assert posts == []

    latched["on"] = True
    await reporter.check()
    await reporter.check()  # second check must NOT repeat the alert
    assert sum("STT watchdog" in p for p in posts) == 1
    assert reporter.stt_watchdog_degraded is True

    latched["on"] = False
    await reporter.check()
    assert any("latch cleared" in p for p in posts)


async def test_wake_fault_announces_once_and_recovers() -> None:
    posts: list[str] = []
    reporter = make_reporter(posts)
    faulted = {"on": False}
    reporter.set_wake_probe(lambda: dict(WAKE_COUNTERS), lambda: faulted["on"])

    await reporter.check()
    assert posts == []

    faulted["on"] = True
    await reporter.check()
    await reporter.check()
    assert sum("Wake-word model failed" in p for p in posts) == 1
    assert reporter.wake_faulted is True

    faulted["on"] = False
    await reporter.check()
    assert any("Wake-word model restored" in p for p in posts)


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
