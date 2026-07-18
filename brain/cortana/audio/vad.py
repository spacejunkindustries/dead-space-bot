"""Voice-activity detection — GDD §5, §16 (capture).

:class:`VadGate` wraps webrtcvad for the 20 ms / 16 kHz / mono / s16le frames
Ears emits (GDD §15 type 0x02).

Utterance endpointing is NOT here: a frame-counted silence tracker can never
fire under Discord DTX (no packets flow while a pilot is silent), so all
endpoint timing lives in the dialog engine's wall-clock wheel (GDD §5.4).

webrtcvad is imported lazily inside :class:`VadGate` so that pure-logic unit
tests (and any module that only needs the frame constants) run without the
native dependency installed.
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
