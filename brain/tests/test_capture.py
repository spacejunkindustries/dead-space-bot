"""Capture state-machine tests — GDD §5. Fully deterministic: fake VAD/wake,
synthetic PCM built in memory (stdlib only), time derived from frame count.
No audio ever touches disk (CLAUDE.md constraint 5): every buffer here is
``bytes`` in RAM.

Endpointing is the dialog engine's wall-clock job (GDD §5.4) — these tests
drive :meth:`CaptureManager.force_endpoint` directly, exactly as the wheel
does. The only frame-counted duration left in the manager is the hard cap.
"""

from __future__ import annotations

import asyncio
import struct

from cortana.audio.capture import CaptureManager, CaptureMeta, CaptureOrigin, Phase
from cortana.audio.vad import FRAME_BYTES, FRAME_MS, SAMPLES_PER_FRAME
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

GUILD = 42
USER_A = 1001
USER_B = 1002

# Derived from the config used below: preroll 300ms, cap 6s, refractory 2s,
# at 20ms per frame.
PREROLL_FRAMES = 300 // FRAME_MS  # 15
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
    """Async on_utterance sink + sync on_capture_start, like DialogEngine."""

    def __init__(self) -> None:
        self.emitted: list[tuple[int, int, bytes, CaptureMeta]] = []
        self.starts: list[tuple[int, int, CaptureOrigin, int | None]] = []
        self._next_gen = 0

    async def __call__(self, user_id: int, guild_id: int, pcm: bytes, meta: CaptureMeta) -> None:
        self.emitted.append((user_id, guild_id, pcm, meta))

    def on_capture_start(
        self, user_id: int, guild_id: int, origin: CaptureOrigin, armed_gen: int | None
    ) -> int:
        self.starts.append((user_id, guild_id, origin, armed_gen))
        if armed_gen is not None:
            return armed_gen
        self._next_gen += 1
        return self._next_gen


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
        ipc=IpcConfig(socket="/run/cortana/cortana.sock"),
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
        recorder.on_capture_start,
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
    assert recorder.starts == [(USER_A, GUILD, CaptureOrigin.WAKE, None)]

    tail = [speech(2000 + i) for i in range(5)]
    feed_all(manager, USER_A, tail)
    manager.force_endpoint(USER_A)
    await settle()

    assert len(recorder.emitted) == 1
    user_id, guild_id, pcm, meta = recorder.emitted[0]
    assert (user_id, guild_id) == (USER_A, GUILD)
    # Exactly the 300ms before the wake hit, then the hit frame and the speech.
    assert pcm == b"".join(lead_in[-PREROLL_FRAMES:] + [MARKER] + tail)
    assert meta.origin is CaptureOrigin.WAKE
    assert meta.gen == 1
    assert meta.speech_frames == len(tail)  # the trigger frame is not counted
    assert meta.reason == "silence"


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
    assert recorder.emitted[0][3].reason == "hard_cap"
    assert manager.phase_of(USER_A) is Phase.IDLE


async def test_refractory_blocks_immediate_retrigger() -> None:
    manager, recorder, _ = make_manager()
    manager.feed(USER_A, GUILD, MARKER)
    manager.feed(USER_A, GUILD, speech(1500))  # the command
    manager.force_endpoint(USER_A)
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


async def test_armed_window_captures_without_wake_word() -> None:
    manager, recorder, _ = make_manager()
    manager.arm_window(USER_A, GUILD, gen=7)
    assert manager.phase_of(USER_A) is Phase.ARMED
    frames = [speech(1200 + i) for i in range(8)]
    feed_all(manager, USER_A, frames)
    assert manager.is_capturing(USER_A)
    assert recorder.starts == [(USER_A, GUILD, CaptureOrigin.WINDOW, 7)]
    manager.force_endpoint(USER_A)
    await settle()

    assert len(recorder.emitted) == 1
    user_id, guild_id, pcm, meta = recorder.emitted[0]
    assert pcm == b"".join(frames)
    assert meta.origin is CaptureOrigin.WINDOW
    assert meta.gen == 7  # the armed gen rides through to the emission


async def test_armed_window_clears_refractory_for_say_again() -> None:
    manager, recorder, _ = make_manager()
    manager.feed(USER_A, GUILD, MARKER)
    manager.feed(USER_A, GUILD, speech(1500))
    manager.force_endpoint(USER_A)
    await settle()
    assert len(recorder.emitted) == 1

    # LOW tier → "say again": the armed window must not be refractory-gated.
    manager.arm_window(USER_A, GUILD, gen=2)
    manager.feed(USER_A, GUILD, speech(1500))
    assert manager.is_capturing(USER_A)


async def test_disarm_closes_window_without_capturing() -> None:
    manager, recorder, _ = make_manager()
    manager.arm_window(USER_A, GUILD, gen=3)
    # DTX: NO frames arrive while the pilot is silent — the window must be
    # closable from outside regardless (the stuck-open incident).
    manager.disarm(USER_A)
    assert manager.phase_of(USER_A) is Phase.IDLE

    # After disarm, plain speech (no wake) must NOT open a capture.
    feed_all(manager, USER_A, [speech(1500)] * 10)
    await settle()
    assert recorder.emitted == []
    assert not manager.is_capturing(USER_A)
    # disarm is a no-op outside ARMED:
    manager.disarm(USER_A)
    manager.disarm(USER_B)


async def test_abandoned_capture_emits_empty_pcm() -> None:
    # A wake hit whose capture never sees another speech frame (the pilot
    # said only the wake tail) is emitted as abandoned with NO audio: the
    # buffer is dropped before dispatch (constraint 5).
    manager, recorder, _ = make_manager()
    manager.feed(USER_A, GUILD, MARKER)
    assert manager.is_capturing(USER_A)
    manager.force_endpoint(USER_A)
    await settle()
    assert len(recorder.emitted) == 1
    _, _, pcm, meta = recorder.emitted[0]
    assert pcm == b""
    assert meta.reason == "abandoned"
    assert meta.speech_frames == 0


async def test_per_user_isolation_with_interleaved_frames() -> None:
    manager, recorder, _ = make_manager()
    a_frames = [speech(1000 + i) for i in range(10)]
    b_frames = [speech(2000 + i) for i in range(10)]

    manager.feed(USER_A, GUILD, MARKER)
    manager.feed(USER_B, GUILD, MARKER)
    for fa, fb in zip(a_frames, b_frames, strict=True):
        manager.feed(USER_A, GUILD, fa)
        manager.feed(USER_B, GUILD, fb)
    manager.force_endpoint(USER_A)
    manager.force_endpoint(USER_B)
    await settle()

    assert len(recorder.emitted) == 2
    by_user = {user: pcm for user, _, pcm, _ in recorder.emitted}
    assert by_user[USER_A] == b"".join([MARKER] + a_frames)
    assert by_user[USER_B] == b"".join([MARKER] + b_frames)
    # Distinct gens per capture:
    gens = {meta.gen for _, _, _, meta in recorder.emitted}
    assert len(gens) == 2


async def test_capture_buffer_freed_and_ring_keeps_rolling() -> None:
    manager, recorder, _ = make_manager()
    first = [speech(1000 + i) for i in range(20)]
    feed_all(manager, USER_A, first)
    manager.feed(USER_A, GUILD, MARKER)
    manager.feed(USER_A, GUILD, speech(1500))
    manager.force_endpoint(USER_A)
    await settle()
    assert len(recorder.emitted) == 1
    assert manager.phase_of(USER_A) is Phase.IDLE  # buffer freed, back to idle

    burn_refractory(manager, USER_A)
    second = [speech(3000 + i) for i in range(PREROLL_FRAMES)]
    feed_all(manager, USER_A, second)  # the ring kept rolling through all of it
    manager.feed(USER_A, GUILD, MARKER)
    cmd2 = speech(3500)
    manager.feed(USER_A, GUILD, cmd2)
    manager.force_endpoint(USER_A)
    await settle()

    assert len(recorder.emitted) == 2
    pcm = recorder.emitted[1][2]
    # Pre-roll is exactly the fresh frames; nothing from the first utterance
    # (or the silence between) leaks in.
    assert pcm == b"".join(second + [MARKER, cmd2])
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

    assert manager.force_endpoint(USER_A) is False
    await settle()
    assert recorder.emitted == []  # the aborted capture never emits


async def test_no_capture_without_wake_hit() -> None:
    manager, recorder, _ = make_manager()
    feed_all(manager, USER_A, [speech(1500)] * 100)
    await settle()
    assert recorder.emitted == []
    assert not manager.is_capturing(USER_A)


async def test_wake_scoring_is_gated_behind_vad() -> None:
    manager, _, wake = make_manager()
    feed_all(manager, USER_A, [SILENCE] * 50)
    assert wake.scored == 0  # silence never reaches the wake model (GDD §3.3)
    manager.feed(USER_A, GUILD, speech(1500))
    assert wake.scored == 1


async def test_armed_window_never_scores_wake() -> None:
    manager, _, wake = make_manager()
    manager.arm_window(USER_A, GUILD, gen=1)
    manager.feed(USER_A, GUILD, SILENCE)
    assert wake.scored == 0
    manager.feed(USER_A, GUILD, MARKER)  # speech: opens the window capture
    assert wake.scored == 0
    assert manager.is_capturing(USER_A)


async def test_malformed_frame_is_dropped() -> None:
    manager, recorder, _ = make_manager()
    manager.feed(USER_A, GUILD, b"\x00" * 10)  # wrong size: dropped, no crash
    manager.feed(USER_A, GUILD, MARKER)
    assert manager.is_capturing(USER_A)
    cmd = speech(1500)
    manager.feed(USER_A, GUILD, cmd)
    manager.force_endpoint(USER_A)
    await settle()
    assert len(recorder.emitted) == 1
    assert recorder.emitted[0][2] == b"".join([MARKER, cmd])


async def test_single_refractory_window_with_real_detector() -> None:
    """Composed regression (single refractory owner): with the real
    OpenWakeWordDetector plugged in, a wake attempt succeeds on the first
    speech frames after ``burn_refractory`` — the detector must not stack a
    second refractory window on top of the capture layer's."""
    from cortana.audio.wake import OpenWakeWordDetector

    recorder = Recorder()
    holder = StubHolder(make_config())
    detector = OpenWakeWordDetector(holder)  # type: ignore[arg-type]
    # openwakeword isn't installed here: give the model pool a build seam and
    # the spare __init__ would have built, so the first speaker scores
    # immediately (the pool replenishes in the background off-loop).
    detector._build_model = object  # type: ignore[method-assign]
    detector._spares.append(object())
    detector._predict_chunk = lambda state, chunk: 0.9  # type: ignore[method-assign]
    manager = CaptureManager(
        holder,  # type: ignore[arg-type]
        FakeVad(),  # type: ignore[arg-type]
        detector,
        recorder,
        recorder.on_capture_start,
    )

    # First utterance: four speech frames form a chunk and hit; the two after
    # the hit are the captured command.
    feed_all(manager, USER_A, [speech(1500)] * 6)
    assert manager.is_capturing(USER_A)
    manager.force_endpoint(USER_A)
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
    manager.feed(USER_A, GUILD, speech(1500))
    manager.force_endpoint(USER_A)
    assert len(recorder.emitted) == 1


async def test_force_endpoint_emits_open_capture() -> None:
    # The dialog wheel ends a capture when Discord stops sending packets
    # (no silence frames ever arrive to endpoint on).
    manager, recorder, _ = make_manager()
    manager.feed(USER_A, GUILD, MARKER)
    cmd = speech(1500)
    manager.feed(USER_A, GUILD, cmd)
    assert manager.is_capturing(USER_A)
    assert manager.capturing_users() == [USER_A]

    assert manager.force_endpoint(USER_A) is True
    await settle()
    assert len(recorder.emitted) == 1
    assert recorder.emitted[0][2] == b"".join([MARKER, cmd])
    assert manager.phase_of(USER_A) is Phase.IDLE
    # Idempotent: nothing to end now.
    assert manager.force_endpoint(USER_A) is False
