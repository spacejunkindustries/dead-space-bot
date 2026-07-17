"""Wake-word detection — GDD §5.1/§5.2, §16 (wake block).

:class:`OpenWakeWordDetector` runs one openWakeWord model instance per active
speaker, because the model is a streaming detector with internal mel/embedding
buffers: interleaving two users' audio through one instance would smear their
streams together. Per-user state is created on first frame and purged via
:meth:`reset` when the user leaves or opts out.

openWakeWord's native input granularity is 80 ms (1280 samples); Ears delivers
20 ms frames, so the detector accumulates four frames per inference call and
holds the last score in between. All timing is derived from frame count
(20 ms/frame) — no wall clock — which keeps the refractory period fully
deterministic and testable.

Heavy dependencies (openwakeword, numpy) are imported lazily on the first
inference, never at module import, so pure-logic tests run without them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from aura.audio.vad import FRAME_BYTES, FRAME_MS
from aura.config import WakeConfig

log = structlog.get_logger(__name__)

__all__ = [
    "OWW_CHUNK_BYTES",
    "OWW_CHUNK_SAMPLES",
    "OpenWakeWordDetector",
    "WakeDetector",
]

#: openWakeWord's designed inference chunk: 80 ms at 16 kHz (upstream docs).
OWW_CHUNK_SAMPLES = 1280
OWW_CHUNK_BYTES = OWW_CHUNK_SAMPLES * 2  # s16le


class WakeDetector(Protocol):
    """Per-user streaming wake-word scorer (docs/INTERFACES.md)."""

    def score(self, user_id: int, frame: bytes) -> float:
        """Feed one 20 ms frame for this user; return the current score in 0..1."""
        ...

    def reset(self, user_id: int) -> None:
        """Drop all detector state for this user (left channel / opted out)."""
        ...


@dataclass(slots=True)
class _UserWakeState:
    """Streaming state for one speaker."""

    pending: bytearray = field(default_factory=bytearray)
    last_score: float = 0.0
    refractory_frames_left: int = 0
    model: Any = None  # openwakeword.model.Model, created lazily


class OpenWakeWordDetector:
    """openWakeWord-backed :class:`WakeDetector` for the configured ONNX model.

    A hit (score >= ``wake.threshold``) starts a per-user refractory period of
    ``wake.refractory_ms`` during which :meth:`score` reports 0.0, so the same
    utterance cannot retrigger (GDD §5). Audio fed during refractory is
    discarded, and the model's prediction buffer is cleared on the hit, so the
    detector re-arms clean.
    """

    def __init__(self, cfg: WakeConfig) -> None:
        self._cfg = cfg
        self._refractory_frames = max(1, cfg.refractory_ms // FRAME_MS)
        self._states: dict[int, _UserWakeState] = {}

    def score(self, user_id: int, frame: bytes) -> float:
        if len(frame) != FRAME_BYTES:
            raise ValueError(f"expected one {FRAME_BYTES}-byte 20ms frame, got {len(frame)} bytes")
        state = self._states.get(user_id)
        if state is None:
            state = self._states[user_id] = _UserWakeState()

        if state.refractory_frames_left > 0:
            state.refractory_frames_left -= 1
            return 0.0

        state.pending += frame
        while len(state.pending) >= OWW_CHUNK_BYTES:
            chunk = bytes(state.pending[:OWW_CHUNK_BYTES])
            del state.pending[:OWW_CHUNK_BYTES]
            state.last_score = self._predict_chunk(state, chunk)

        if state.last_score >= self._cfg.threshold:
            hit_score = state.last_score
            self._arm_refractory(user_id, state)
            log.info("wake_hit", user_id=user_id, score=round(hit_score, 3))
            return hit_score
        return state.last_score

    def reset(self, user_id: int) -> None:
        self._states.pop(user_id, None)

    # ── internals ────────────────────────────────────────────────────────────

    def _arm_refractory(self, user_id: int, state: _UserWakeState) -> None:
        state.refractory_frames_left = self._refractory_frames
        state.last_score = 0.0
        state.pending.clear()
        if state.model is not None and hasattr(state.model, "reset"):
            state.model.reset()

    def _predict_chunk(self, state: _UserWakeState, chunk: bytes) -> float:
        """Run one 80 ms chunk through this user's model; return the top score."""
        import numpy as np  # lazy

        if state.model is None:
            from openwakeword.model import Model  # lazy

            state.model = Model(
                wakeword_models=[self._cfg.model],
                inference_framework="onnx",
            )
        samples = np.frombuffer(chunk, dtype=np.int16)
        predictions: dict[str, float] = state.model.predict(samples)
        if not predictions:
            return 0.0
        return float(max(predictions.values()))
