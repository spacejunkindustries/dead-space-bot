"""Voice-activity detection and utterance endpointing — GDD §5, §16 (capture).

:class:`VadGate` wraps webrtcvad for the 20 ms / 16 kHz / mono / s16le frames
Ears emits (GDD §15 type 0x02). :class:`EndpointTracker` turns a per-frame
speech/silence stream into an endpoint decision by counting consecutive
silent frames — pure logic, no wall clock, no audio retained.

webrtcvad is imported lazily inside :class:`VadGate` so that pure-logic unit
tests (and any module that only needs the frame constants or the tracker) run
without the native dependency installed.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)

__all__ = [
    "BYTES_PER_SAMPLE",
    "FRAME_BYTES",
    "FRAME_MS",
    "SAMPLES_PER_FRAME",
    "SAMPLE_RATE_HZ",
    "EndpointTracker",
    "VadGate",
]

#: Wire format of one audio frame from Ears (GDD §15): 20 ms of 16 kHz mono s16le.
SAMPLE_RATE_HZ = 16_000
FRAME_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE_HZ * FRAME_MS // 1000  # 320
BYTES_PER_SAMPLE = 2  # s16le
FRAME_BYTES = SAMPLES_PER_FRAME * BYTES_PER_SAMPLE  # 640


class VadGate:
    """Per-frame speech/silence classifier backed by webrtcvad.

    One instance is shared across users — webrtcvad keeps no cross-frame
    state, so a single detector safely classifies interleaved user streams.
    """

    def __init__(self, aggressiveness: int) -> None:
        """``aggressiveness`` is webrtcvad's 0–3 knob (``capture.vad_aggressiveness``)."""
        if not 0 <= aggressiveness <= 3:
            raise ValueError(f"vad aggressiveness must be 0-3, got {aggressiveness}")
        import webrtcvad  # lazy: native wheel, absent in pure-logic test envs

        self._vad = webrtcvad.Vad(aggressiveness)
        self._aggressiveness = aggressiveness

    @property
    def aggressiveness(self) -> int:
        return self._aggressiveness

    def is_speech(self, frame: bytes) -> bool:
        """Classify one 20 ms frame. ``frame`` must be exactly ``FRAME_BYTES`` long."""
        if len(frame) != FRAME_BYTES:
            raise ValueError(f"expected one {FRAME_BYTES}-byte 20ms frame, got {len(frame)} bytes")
        return bool(self._vad.is_speech(frame, SAMPLE_RATE_HZ))


class EndpointTracker:
    """Tracks consecutive silence duration for utterance endpointing (GDD §5).

    Feed one VAD verdict per 20 ms frame via :meth:`update`; it returns True
    once ``silence_ms`` of *uninterrupted* silence has accumulated. Any speech
    frame resets the run. Time is derived purely from frame count — 20 ms per
    update — so the tracker is fully deterministic.
    """

    def __init__(self, silence_ms: int, frame_ms: int = FRAME_MS) -> None:
        if silence_ms <= 0:
            raise ValueError(f"silence_ms must be > 0, got {silence_ms}")
        if frame_ms <= 0:
            raise ValueError(f"frame_ms must be > 0, got {frame_ms}")
        self._limit_frames = max(1, silence_ms // frame_ms)
        self._frame_ms = frame_ms
        self._silent_frames = 0

    @property
    def silence_ms(self) -> int:
        """Current consecutive-silence duration, in milliseconds."""
        return self._silent_frames * self._frame_ms

    def update(self, is_speech: bool) -> bool:
        """Record one frame's verdict; True when the silence endpoint is reached."""
        if is_speech:
            self._silent_frames = 0
        else:
            self._silent_frames += 1
        return self._silent_frames >= self._limit_frames

    def reset(self) -> None:
        self._silent_frames = 0
