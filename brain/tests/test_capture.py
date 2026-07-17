"""Capture state-machine tests — GDD §5. Fully deterministic: fake VAD/wake,
synthetic PCM built in memory (stdlib only), time derived from frame count.
No audio ever touches disk (CLAUDE.md constraint 5): every buffer here is
``bytes`` in RAM.
"""

from __future__ import annotations

import asyncio
import struct

from aura.audio.capture import CaptureManager, Phase
from aura.audio.vad import FRAME_BYTES, FRAME_MS, SAMPLES_PER_FRAME
from aura.config import (
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

GUILD = 42
USER_A = 1001
USER_B = 1002

# Derived from the config used below: preroll 300ms, endpoint 400ms, cap 6s,
# refractory 2s, at 20ms per frame.
PREROLL_FRAMES = 300 // FRAME_MS  # 15
ENDPOINT_FRAMES = 400 // FRAME_MS  # 20
CAP_FRAMES = 6000 // FRAME_MS  # 300
REFRACTORY_FRAMES = 2000 // FRAME_MS  # 100


# ── synthetic PCM (in-memory, stdlib only) ───────────────────────────────────

#: FakeVad calls any frame whose first sample exceeds this "speech".
VAD_ENERGY_FLOOR = 500
#: Sample value that marks the wake-hit frame for FakeWake.
MARKER_VALUE = 31000


def frame_of(value: int) -> bytes:
    """One 20ms frame where every s16le sample equals ``value``."""
    return struct.pack("<h", value) * SAMPLES_PER_FRAME


SILENCE = frame_of(0)
MARKER = frame_of(MARKER_VALUE)


def speech(value: int) -> bytes:
    """A distinguishable 'speech' frame (loud enough for FakeVad)."""
    assert VAD_ENERGY_FLOOR < value < MARKER_VALUE
    return frame_of(value)


class FakeVad:
    """Energy-threshold VAD: speech iff the first sample is loud."""

    def is_speech(self, frame: bytes) -> bool:
        (sample,) = struct.unpack_from("<h", frame)
        return abs(sample) > VAD_ENERGY_FLOOR


class FakeWake:
    """Fires (score 1.0) exactly on the MARKER frame; records resets."""

    def __init__(self) -> None:
        self.resets: list[int] = []
        self.scored: int = 0

    def score(self, user_id: int, frame: bytes) -> float:
        self.scored += 1
        return 1.0 if frame == MARKER else 0.0

    def reset(self, user_id: int) -> None:
        self.resets.append(user_id)


class Recorder:
    """Async on_utterance sink."""

    def __init__(self) -> None:
        self.emitted: list[tuple[int, int, bytes]] = []

    async def __call__(self, user_id: int, guild_id: int, pcm: bytes) -> None:
        self.emitted.append((user_id, guild_id, pcm))


# ── config / manager plumbing ────────────────────────────────────────────────


def make_config() -> AuraConfig:
    return AuraConfig(
        discord=DiscordConfig(
            token_file="/dev/null",
            guild_id=GUILD,
            channels=ChannelsConfig(intel_alerts=10, intel_live=11, health=12),
            roles=RolesConfig(pilot=111, fc=222),
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
            user_cooldown_s=30,
            circuit_breaker=CircuitBreakerConfig(max_mentions=12, window_min=10),
        ),
        tts=TtsConfig(
            enabled=True,
            voice="voice.onnx",
            binary="/usr/local/bin/piper",
            max_utterance_s=3.0,
        ),
        gazetteer=GazetteerConfig(file="gazetteer.yaml", home_system="Otanuomi"),
        ipc=IpcConfig(socket="/run/aura/aura.sock"),
        health=HealthConfig(report_interval_min=60, voice_silence_alarm_s=60),
        database=DatabaseConfig(path=":memory:"),
    )


class StubHolder:
    """Duck-typed ConfigHolder: fixed AuraConfig, no YAML file needed."""

    def __init__(self, cfg: AuraConfig) -> None:
        self.current = cfg


def make_manager() -> tuple[CaptureManager, Recorder, FakeWake]:
    recorder = Recorder()
    wake = FakeWake()
    manager = CaptureManager(
        StubHolder(make_config()),  # type: ignore[arg-type]
        FakeVad(),  # type: ignore[arg-type]
        wake,
        recorder,
    )
    return manager, recorder, wake


async def settle() -> None:
    """Let dispatched on_utterance tasks run."""
    await asyncio.sleep(0)


def feed_all(manager: CaptureManager, user_id: int, frames: list[bytes]) -> None:
    for frame in frames:
        manager.feed(user_id, GUILD, frame)


def burn_refractory(manager: CaptureManager, user_id: int) -> None:
    """Feed enough silence for the post-emit refractory to fully elapse."""
    feed_all(manager, user_id, [SILENCE] * REFRACTORY_FRAMES)


# ── tests ────────────────────────────────────────────────────────────────────


async def test_preroll_included_in_emitted_utterance() -> None:
    manager, recorder, _ = make_manager()
    lead_in = [speech(1000 + i) for i in range(30)]
    feed_all(manager, USER_A, lead_in)
    manager.feed(USER_A, GUILD, MARKER)
    assert manager.is_capturing(USER_A)

    tail = [speech(2000 + i) for i in range(5)]
    feed_all(manager, USER_A, tail)
    feed_all(manager, USER_A, [SILENCE] * ENDPOINT_FRAMES)
    await settle()

    assert len(recorder.emitted) == 1
    user_id, guild_id, pcm = recorder.emitted[0]
    assert (user_id, guild_id) == (USER_A, GUILD)
    # Exactly the 300ms before the wake hit, then the hit frame, the speech,
    # and the trailing silence that endpointed it.
    expected = b"".join(lead_in[-PREROLL_FRAMES:] + [MARKER] + tail + [SILENCE] * ENDPOINT_FRAMES)
    assert pcm == expected


async def test_endpoint_fires_at_exactly_400ms_of_silence() -> None:
    manager, recorder, _ = make_manager()
    manager.feed(USER_A, GUILD, MARKER)
    feed_all(manager, USER_A, [speech(1500)] * 10)

    feed_all(manager, USER_A, [SILENCE] * (ENDPOINT_FRAMES - 1))
    await settle()
    assert recorder.emitted == []  # 380ms of silence: not yet
    assert manager.is_capturing(USER_A)

    manager.feed(USER_A, GUILD, SILENCE)  # 400ms: endpoint
    await settle()
    assert len(recorder.emitted) == 1


async def test_speech_resets_the_silence_endpoint() -> None:
    manager, recorder, _ = make_manager()
    manager.feed(USER_A, GUILD, MARKER)
    feed_all(manager, USER_A, [SILENCE] * (ENDPOINT_FRAMES - 1))
    manager.feed(USER_A, GUILD, speech(1500))  # run broken
    feed_all(manager, USER_A, [SILENCE] * (ENDPOINT_FRAMES - 1))
    await settle()
    assert recorder.emitted == []
    manager.feed(USER_A, GUILD, SILENCE)
    await settle()
    assert len(recorder.emitted) == 1


async def test_hard_cap_at_6s_of_continuous_speech() -> None:
    manager, recorder, _ = make_manager()
    manager.feed(USER_A, GUILD, MARKER)  # ring empty: no preroll, 1 frame captured
    for i in range(CAP_FRAMES - 2):
        manager.feed(USER_A, GUILD, speech(1000 + i % 100))
    await settle()
    assert recorder.emitted == []  # 299 frames captured: still under the cap

    manager.feed(USER_A, GUILD, speech(1000))  # frame 300: cap
    await settle()
    assert len(recorder.emitted) == 1
    assert len(recorder.emitted[0][2]) == CAP_FRAMES * FRAME_BYTES
    assert manager.phase_of(USER_A) is Phase.IDLE


async def test_refractory_blocks_immediate_retrigger() -> None:
    manager, recorder, _ = make_manager()
    manager.feed(USER_A, GUILD, MARKER)
    feed_all(manager, USER_A, [SILENCE] * ENDPOINT_FRAMES)
    await settle()
    assert len(recorder.emitted) == 1

    # An immediate wake hit is ignored...
    manager.feed(USER_A, GUILD, MARKER)
    assert not manager.is_capturing(USER_A)
    # ...and stays ignored until refractory_ms of frames have elapsed.
    feed_all(manager, USER_A, [SILENCE] * (REFRACTORY_FRAMES - 2))
    manager.feed(USER_A, GUILD, MARKER)
    assert not manager.is_capturing(USER_A)  # one frame early

    manager.feed(USER_A, GUILD, MARKER)  # refractory elapsed
    assert manager.is_capturing(USER_A)


async def test_reopen_captures_without_wake_word() -> None:
    manager, recorder, _ = make_manager()
    manager.reopen(USER_A, GUILD)
    frames = [speech(1200 + i) for i in range(8)]
    feed_all(manager, USER_A, frames)
    assert manager.is_capturing(USER_A)
    feed_all(manager, USER_A, [SILENCE] * ENDPOINT_FRAMES)
    await settle()

    assert len(recorder.emitted) == 1
    assert recorder.emitted[0] == (
        USER_A,
        GUILD,
        b"".join(frames + [SILENCE] * ENDPOINT_FRAMES),
    )


async def test_reopen_clears_refractory_for_say_again() -> None:
    manager, recorder, _ = make_manager()
    manager.feed(USER_A, GUILD, MARKER)
    feed_all(manager, USER_A, [SILENCE] * ENDPOINT_FRAMES)
    await settle()
    assert len(recorder.emitted) == 1

    # LOW tier → "say again": the reopened window must not be refractory-gated.
    manager.reopen(USER_A, GUILD)
    manager.feed(USER_A, GUILD, speech(1500))
    assert manager.is_capturing(USER_A)


async def test_reopen_window_expires_without_speech() -> None:
    manager, recorder, _ = make_manager()
    window_ms = 400
    manager.reopen(USER_A, GUILD, window_ms=window_ms)
    feed_all(manager, USER_A, [SILENCE] * (window_ms // FRAME_MS))
    assert manager.phase_of(USER_A) is Phase.IDLE

    # After expiry, plain speech (no wake) must NOT open a capture.
    feed_all(manager, USER_A, [speech(1500)] * 10)
    await settle()
    assert recorder.emitted == []
    assert not manager.is_capturing(USER_A)


async def test_per_user_isolation_with_interleaved_frames() -> None:
    manager, recorder, _ = make_manager()
    a_frames = [speech(1000 + i) for i in range(10)]
    b_frames = [speech(2000 + i) for i in range(10)]

    manager.feed(USER_A, GUILD, MARKER)
    manager.feed(USER_B, GUILD, MARKER)
    for fa, fb in zip(a_frames, b_frames, strict=True):
        manager.feed(USER_A, GUILD, fa)
        manager.feed(USER_B, GUILD, fb)
    for _ in range(ENDPOINT_FRAMES):
        manager.feed(USER_A, GUILD, SILENCE)
        manager.feed(USER_B, GUILD, SILENCE)
    await settle()

    assert len(recorder.emitted) == 2
    by_user = {user: pcm for user, _, pcm in recorder.emitted}
    assert by_user[USER_A] == b"".join([MARKER] + a_frames + [SILENCE] * ENDPOINT_FRAMES)
    assert by_user[USER_B] == b"".join([MARKER] + b_frames + [SILENCE] * ENDPOINT_FRAMES)


async def test_capture_buffer_freed_and_ring_keeps_rolling() -> None:
    manager, recorder, _ = make_manager()
    first = [speech(1000 + i) for i in range(20)]
    feed_all(manager, USER_A, first)
    manager.feed(USER_A, GUILD, MARKER)
    feed_all(manager, USER_A, [SILENCE] * ENDPOINT_FRAMES)
    await settle()
    assert len(recorder.emitted) == 1
    assert manager.phase_of(USER_A) is Phase.IDLE  # buffer freed, back to idle

    burn_refractory(manager, USER_A)
    second = [speech(3000 + i) for i in range(PREROLL_FRAMES)]
    feed_all(manager, USER_A, second)  # the ring kept rolling through all of it
    manager.feed(USER_A, GUILD, MARKER)
    feed_all(manager, USER_A, [SILENCE] * ENDPOINT_FRAMES)
    await settle()

    assert len(recorder.emitted) == 2
    pcm = recorder.emitted[1][2]
    # Pre-roll is exactly the fresh frames; nothing from the first utterance
    # (or the silence between) leaks in.
    assert pcm == b"".join(second + [MARKER] + [SILENCE] * ENDPOINT_FRAMES)
    for frame in first:
        assert frame not in pcm


async def test_drop_user_purges_state_mid_capture() -> None:
    manager, recorder, wake = make_manager()
    manager.feed(USER_A, GUILD, MARKER)
    feed_all(manager, USER_A, [speech(1500)] * 5)
    assert manager.is_capturing(USER_A)

    manager.drop_user(USER_A)
    assert manager.phase_of(USER_A) is Phase.IDLE
    assert manager.tracked_users == 0
    assert USER_A in wake.resets

    feed_all(manager, USER_A, [SILENCE] * ENDPOINT_FRAMES)
    await settle()
    assert recorder.emitted == []  # the aborted capture never emits


async def test_no_capture_without_wake_hit() -> None:
    manager, recorder, _ = make_manager()
    feed_all(manager, USER_A, [speech(1500)] * 100)
    feed_all(manager, USER_A, [SILENCE] * ENDPOINT_FRAMES)
    await settle()
    assert recorder.emitted == []
    assert not manager.is_capturing(USER_A)


async def test_wake_scoring_is_gated_behind_vad() -> None:
    manager, _, wake = make_manager()
    feed_all(manager, USER_A, [SILENCE] * 50)
    assert wake.scored == 0  # silence never reaches the wake model (GDD §3.3)
    manager.feed(USER_A, GUILD, speech(1500))
    assert wake.scored == 1


async def test_malformed_frame_is_dropped() -> None:
    manager, recorder, _ = make_manager()
    manager.feed(USER_A, GUILD, b"\x00" * 10)  # wrong size: dropped, no crash
    manager.feed(USER_A, GUILD, MARKER)
    assert manager.is_capturing(USER_A)
    feed_all(manager, USER_A, [SILENCE] * ENDPOINT_FRAMES)
    await settle()
    assert len(recorder.emitted) == 1
    assert recorder.emitted[0][2] == b"".join([MARKER] + [SILENCE] * ENDPOINT_FRAMES)


async def test_single_refractory_window_with_real_detector() -> None:
    """Composed regression (single refractory owner): with the real
    OpenWakeWordDetector plugged in, a wake attempt succeeds on the first
    speech frames after ``burn_refractory`` — the detector must not stack a
    second refractory window on top of the capture layer's."""
    from aura.audio.wake import OpenWakeWordDetector

    recorder = Recorder()
    holder = StubHolder(make_config())
    detector = OpenWakeWordDetector(holder)  # type: ignore[arg-type]
    detector._predict_chunk = lambda state, chunk: 0.9  # type: ignore[method-assign]
    manager = CaptureManager(
        holder,  # type: ignore[arg-type]
        FakeVad(),  # type: ignore[arg-type]
        detector,
        recorder,
    )

    # First utterance: four speech frames form a chunk, hit, capture, endpoint.
    feed_all(manager, USER_A, [speech(1500)] * 4)
    assert manager.is_capturing(USER_A)
    feed_all(manager, USER_A, [SILENCE] * ENDPOINT_FRAMES)
    await settle()
    assert len(recorder.emitted) == 1

    # Exactly one refractory window (capture.py's): the first speech frames
    # after it must be scored and hit again immediately.
    burn_refractory(manager, USER_A)
    feed_all(manager, USER_A, [speech(1500)] * 4)
    assert manager.is_capturing(USER_A)


def test_emit_works_without_a_running_event_loop() -> None:
    # feed() is normally called on the event loop; the sync fallback still
    # delivers the utterance (documented in CaptureManager._dispatch).
    manager, recorder, _ = make_manager()
    manager.feed(USER_A, GUILD, MARKER)
    feed_all(manager, USER_A, [SILENCE] * ENDPOINT_FRAMES)
    assert len(recorder.emitted) == 1
