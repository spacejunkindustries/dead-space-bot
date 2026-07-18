"""Wake-word detection — GDD §5.1/§5.2, §16 (wake block).

:class:`OpenWakeWordDetector` runs one openWakeWord model instance per active
speaker, because the model is a streaming detector with internal mel/embedding
buffers: interleaving two users' audio through one instance would smear their
streams together. Per-user state is created on first frame and purged via
:meth:`reset` when the user leaves or opts out.

openWakeWord's native input granularity is 80 ms (1280 samples); Ears delivers
20 ms frames, so the detector accumulates four frames per inference call and
holds the last score in between.

Configuration is read from ``holder.current.wake`` at the point of use (the
``config.py`` contract), so a SIGHUP retune of ``wake.threshold`` or
``wake.model`` applies immediately and the detector can never disagree with
:class:`~aura.audio.capture.CaptureManager` about the live threshold.

The wake **refractory period is owned by the capture layer**
(``audio/capture.py``): after an emitted utterance the CaptureManager stops
feeding this detector for ``wake.refractory_ms``. On a hit the detector only
clears its own streaming state (pending bytes, held score, model buffers) so
the tail of the same utterance cannot retrigger from a held score.

Heavy dependencies (openwakeword, numpy) are imported lazily on the first
inference, never at module import, so pure-logic tests run without them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from aura.audio.vad import FRAME_BYTES
from aura.config import ConfigHolder

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

#: A below-threshold peak this high means the phrase was clearly heard but
#: didn't clear the bar — the one signal that tells an operator to lower
#: ``wake.threshold`` rather than chase a phantom audio fault. Logged as
#: ``wake_near_miss``; below this floor the channel stays quiet in the log.
_NEAR_MISS_FLOOR = 0.3
#: Only re-log a near-miss once it climbs this much higher, so one utterance
#: emits a couple of lines, not fifty.
_NEAR_MISS_STEP = 0.05


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
    #: Highest near-miss already logged this episode; reset when the score
    #: falls back under the floor or a hit re-arms the detector.
    peak_logged: float = 0.0
    model: Any = None  # openwakeword.model.Model, created lazily


class OpenWakeWordDetector:
    """openWakeWord-backed :class:`WakeDetector` for the configured ONNX model.

    A hit (score >= ``wake.threshold``) clears this user's streaming state —
    pending bytes, held score, and the model's prediction buffer — so the
    detector re-arms clean and the same utterance's tail cannot retrigger
    from the held score. Wake-hit *suppression* for ``wake.refractory_ms`` is
    the CaptureManager's job (GDD §5); no second refractory lives here.
    """

    def __init__(self, holder: ConfigHolder) -> None:
        self._holder = holder
        self._states: dict[int, _UserWakeState] = {}

    def score(self, user_id: int, frame: bytes) -> float:
        if len(frame) != FRAME_BYTES:
            raise ValueError(f"expected one {FRAME_BYTES}-byte 20ms frame, got {len(frame)} bytes")
        state = self._states.get(user_id)
        if state is None:
            state = self._states[user_id] = _UserWakeState()

        state.pending += frame
        while len(state.pending) >= OWW_CHUNK_BYTES:
            chunk = bytes(state.pending[:OWW_CHUNK_BYTES])
            del state.pending[:OWW_CHUNK_BYTES]
            state.last_score = self._predict_chunk(state, chunk)

        threshold = self._holder.current.wake.threshold
        if state.last_score >= threshold:
            hit_score = state.last_score
            self._rearm_after_hit(state)
            log.info("wake_hit", user_id=user_id, score=round(hit_score, 3))
            return hit_score
        # Near-miss visibility: without this, a phrase that scores just under
        # the threshold is indistinguishable from silence in the log — the exact
        # ambiguity that makes "the wake word doesn't work" impossible to triage.
        if state.last_score >= _NEAR_MISS_FLOOR:
            if state.last_score >= state.peak_logged + _NEAR_MISS_STEP:
                state.peak_logged = state.last_score
                log.info(
                    "wake_near_miss",
                    user_id=user_id,
                    score=round(state.last_score, 3),
                    threshold=threshold,
                )
        else:
            state.peak_logged = 0.0
        return state.last_score

    def reset(self, user_id: int) -> None:
        self._states.pop(user_id, None)

    # ── internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _rearm_after_hit(state: _UserWakeState) -> None:
        """Clear the streaming state after a hit so the detector re-arms clean."""
        state.last_score = 0.0
        state.peak_logged = 0.0
        state.pending.clear()
        if state.model is not None and hasattr(state.model, "reset"):
            state.model.reset()

    def _predict_chunk(self, state: _UserWakeState, chunk: bytes) -> float:
        """Run one 80 ms chunk through this user's model; return the top score."""
        import numpy as np  # lazy

        if state.model is None:
            from openwakeword.model import Model  # lazy

            state.model = Model(
                wakeword_models=[self._holder.current.wake.model],
                inference_framework="onnx",
            )
        samples = np.frombuffer(chunk, dtype=np.int16)
        predictions: dict[str, float] = state.model.predict(samples)
        if not predictions:
            return 0.0
        return float(max(predictions.values()))
