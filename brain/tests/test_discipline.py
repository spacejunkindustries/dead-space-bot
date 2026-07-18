"""Notification-discipline tests — GDD §11.1. All time is injected; no wall clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cortana.config import (
    AuraConfig,
    CaptureConfig,
    ChannelsConfig,
    CircuitBreakerConfig,
    DatabaseConfig,
    DisciplineConfig,
    DiscordConfig,
    GazetteerConfig,
    HealthConfig,
    IncidentsConfig,
    IpcConfig,
    MatchingConfig,
    PriorsConfig,
    RolesConfig,
    SttConfig,
    TiersConfig,
    TtsConfig,
    WakeConfig,
)
from cortana.core.discipline import Discipline

PILOT_ROLE = 111
FC_ROLE = 222

T0 = datetime(2026, 7, 17, 12, 0, 0, tzinfo=UTC)


def make_config(
    *,
    user_cooldown_s: int = 30,
    max_mentions: int = 3,
    window_min: int = 10,
) -> AuraConfig:
    return AuraConfig(
        discord=DiscordConfig(
            token_file="/dev/null",
            guild_id=1,
            channels=ChannelsConfig(intel_alerts=10, intel_live=11, health=12),
            roles=RolesConfig(pilot=PILOT_ROLE, fc=FC_ROLE),
            watch_voice_channels=(9,),
            auto_join=True,
        ),
        wake=WakeConfig(model="wake.onnx", threshold=0.55, refractory_ms=2000),
        capture=CaptureConfig(
            preroll_ms=300, endpoint_silence_ms=400, max_utterance_ms=6000, vad_aggressiveness=2
        ),
        stt=SttConfig(
            backend="faster-whisper",
            model="small",
            compute_type="int8",
            cpu_threads=2,
            bias_with_gazetteer=True,
            whisper_cpp_url="http://127.0.0.1:8080/inference",
        ),
        matching=MatchingConfig(
            phonetic_weight=0.6,
            text_weight=0.4,
            tiers=TiersConfig(high_min=0.80, high_margin=0.12, medium_min=0.55),
            priors=PriorsConfig(
                recency_weight=0.35,
                recency_window_min=10,
                proximity_weight=0.25,
                proximity_max_jumps=5,
                reporter_history_weight=0.15,
                home_weight=0.10,
            ),
        ),
        incidents=IncidentsConfig(dedupe_window_s=90, stale_after_min=20, cancel_window_s=30),
        discipline=DisciplineConfig(
            user_cooldown_s=user_cooldown_s,
            circuit_breaker=CircuitBreakerConfig(max_mentions=max_mentions, window_min=window_min),
        ),
        tts=TtsConfig(
            enabled=True,
            voice="voice.onnx",
            binary="/usr/local/bin/piper",
            max_utterance_s=3.0,
        ),
        gazetteer=GazetteerConfig(file="gazetteer.yaml", home_system="Otanuomi"),
        ipc=IpcConfig(socket="/run/cortana/cortana.sock"),
        health=HealthConfig(report_interval_min=60, voice_silence_alarm_s=60),
        database=DatabaseConfig(path=":memory:"),
    )


class StubHolder:
    """Duck-typed ConfigHolder: a fixed AuraConfig, no YAML file needed."""

    def __init__(self, cfg: AuraConfig) -> None:
        self.current = cfg


def make_discipline(**cfg_overrides: int) -> Discipline:
    return Discipline(StubHolder(make_config(**cfg_overrides)))  # type: ignore[arg-type]


# ── per-user cooldown ────────────────────────────────────────────────────────


def test_cooldown_blocks_within_window_and_releases_at_boundary() -> None:
    d = make_discipline(user_cooldown_s=30)
    assert d.allow_mention(1, T0)
    d.record_mention(1, T0)
    assert not d.allow_mention(1, T0 + timedelta(seconds=29))
    assert d.allow_mention(1, T0 + timedelta(seconds=30))


def test_cooldown_is_per_user() -> None:
    d = make_discipline(user_cooldown_s=30)
    d.record_mention(1, T0)
    assert not d.allow_mention(1, T0 + timedelta(seconds=5))
    assert d.allow_mention(2, T0 + timedelta(seconds=5))


# ── global circuit breaker ───────────────────────────────────────────────────


def test_breaker_opens_only_above_threshold() -> None:
    d = make_discipline(max_mentions=3, window_min=10)
    for i in range(3):
        d.record_mention(100 + i, T0 + timedelta(seconds=i))
    assert not d.breaker_open(T0 + timedelta(seconds=10))  # exactly N is fine
    d.record_mention(200, T0 + timedelta(seconds=11))
    now = T0 + timedelta(seconds=12)
    assert d.breaker_open(now)  # >N inside the window
    # Breaker open suppresses everyone, even users with no cooldown running.
    assert not d.allow_mention(999, now)


def test_breaker_closes_when_window_slides() -> None:
    d = make_discipline(max_mentions=2, window_min=10, user_cooldown_s=1)
    for i in range(3):
        d.record_mention(i, T0 + timedelta(seconds=i))
    assert d.breaker_open(T0 + timedelta(minutes=1))
    later = T0 + timedelta(minutes=11)
    assert not d.breaker_open(later)
    assert d.allow_mention(50, later)


def test_flood_announcement_fires_once_per_episode() -> None:
    d = make_discipline(max_mentions=1, window_min=10)
    assert not d.should_announce_flood(T0)  # closed: nothing to announce
    d.record_mention(1, T0)
    d.record_mention(2, T0 + timedelta(seconds=1))
    now = T0 + timedelta(seconds=2)
    assert d.breaker_open(now)
    assert d.should_announce_flood(now)  # first notice
    assert not d.should_announce_flood(now + timedelta(seconds=1))  # only once
    # Window slides → breaker closes → announcement re-arms.
    closed = T0 + timedelta(minutes=11)
    assert not d.breaker_open(closed)
    d.record_mention(3, closed)
    d.record_mention(4, closed + timedelta(seconds=1))
    reopened = closed + timedelta(seconds=2)
    assert d.should_announce_flood(reopened)


# ── pilot-role gate ──────────────────────────────────────────────────────────


def test_may_mention_requires_pilot_role() -> None:
    d = make_discipline()
    assert d.may_mention([PILOT_ROLE, 555])
    assert not d.may_mention([555, 666])
    assert not d.may_mention([])


# ── fleet-ops mode ───────────────────────────────────────────────────────────


def test_fleetmode_gates_voice_to_fc_only() -> None:
    d = make_discipline()
    assert not d.fleetmode
    assert d.check([PILOT_ROLE], "voice")  # off: anyone may voice-trigger
    d.set_fleetmode(True)
    assert d.fleetmode
    assert not d.check([PILOT_ROLE], "voice")
    assert d.check([PILOT_ROLE, FC_ROLE], "voice")
    assert d.may_voice_trigger([FC_ROLE])
    assert not d.may_voice_trigger([PILOT_ROLE])


def test_fleetmode_never_gates_slash() -> None:
    d = make_discipline()
    d.set_fleetmode(True)
    assert d.check([], "slash")
    assert d.check([PILOT_ROLE], "slash")
    d.set_fleetmode(False)
    assert d.check([PILOT_ROLE], "voice")


# ── unconfigured role gates (roles: section is optional) ─────────────────────


def _ungated() -> Discipline:
    import dataclasses

    cfg = make_config()
    cfg = dataclasses.replace(cfg, discord=dataclasses.replace(cfg.discord, roles=RolesConfig()))
    return Discipline(StubHolder(cfg))  # type: ignore[arg-type]


def test_unconfigured_pilot_role_lifts_mention_gate() -> None:
    d = _ungated()
    assert d.may_mention([])
    assert d.may_mention([555])


def test_unconfigured_fc_role_means_fleetmode_restricts_nobody() -> None:
    d = _ungated()
    d.set_fleetmode(True)
    assert d.may_voice_trigger([])
    assert d.check([555], "voice")
