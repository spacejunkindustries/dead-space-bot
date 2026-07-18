"""Wake-word detection — GDD §5.1/§5.2, §16 (wake block).

:class:`OpenWakeWordDetector` runs one openWakeWord model instance per active
speaker, because the model is a streaming detector with internal mel/embedding
buffers: interleaving two users' audio through one instance would smear their
streams together. Per-user state is created on first frame and purged via
:meth:`reset` when the user leaves or opts out.

openWakeWord's native input granularity is 80 ms (1280 samples); Ears delivers
20 ms frames, so the detector accumulates four frames per inference call and
holds the last score in between.

Model lifecycle (the hardened pool):

- **Models are NEVER built on the audio hot path.** :meth:`score` runs on the
  event loop (IPC reader → CaptureManager.feed) and an ONNX session load
  stalls it for hundreds of ms. A spare model is built at init (off-loop, via
  ``asyncio.to_thread`` in ``__main__``) and handed to the first speaker;
  whenever a spare is consumed a replacement is built in the background via
  ``asyncio.to_thread``. A NEW speaker who arrives before a spare is ready is
  simply not wake-scored until it lands — their frames still roll through the
  pre-roll ring upstream, so nothing else degrades.
- **Config-generation tagging.** Every model (spare or live) is stamped with
  the ``(wake.model, wake.vad_threshold)`` it was built from; a SIGHUP that
  changes either drops stale models lazily and rebuilds through the pool.
  ``wake.threshold`` needs no rebuild — it is read per frame.
- **Faulted state.** A model BUILD failure (file removed by a config push, a
  broken openwakeword upgrade) latches the detector faulted for the current
  config generation: logged loudly ONCE, :meth:`score` returns 0.0, and no
  per-chunk retry storm grinds the loop. A config change (new generation)
  clears the fault and retries. Health reads :attr:`faulted`.
- **Stage counters.** :meth:`counters` exposes frames_seen → vad_speech →
  inferences → hits/near_misses so a silent wake death is a visible zero in
  ``#bot-health`` instead of a green status over a dead pipeline.

The wake **refractory period is owned by the capture layer**
(``audio/capture.py``): after an emitted utterance the CaptureManager stops
feeding this detector for ``wake.refractory_ms``. On a hit the detector only
clears its own streaming state (pending bytes, held score, model buffers) so
the tail of the same utterance cannot retrigger from a held score.

Heavy dependencies (openwakeword, numpy) are imported lazily on the first
inference, never at module import, so pure-logic tests run without them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from cortana.audio.vad import FRAME_BYTES
from cortana.config import ConfigHolder

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

#: The pool keeps this many pre-built models ready for new speakers.
_SPARE_TARGET = 1

#: (wake.model, wake.vad_threshold) — the config inputs baked into a built
#: model instance. wake.threshold is deliberately NOT part of the key: it is
#: read per frame and needs no rebuild.
_CfgKey = tuple[str, float]


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
    model: Any = None  # openwakeword.model.Model, from the spare pool
    #: Config generation this user's model was built from; a mismatch with
    #: the live config drops the model for a lazy rebuild (SIGHUP retune).
    cfg_key: _CfgKey | None = None


class OpenWakeWordDetector:
    """openWakeWord-backed :class:`WakeDetector` for the configured ONNX model.

    A hit (score >= ``wake.threshold``) clears this user's streaming state —
    pending bytes, held score, and the model's prediction buffer — so the
    detector re-arms clean and the same utterance's tail cannot retrigger
    from the held score. Wake-hit *suppression* for ``wake.refractory_ms`` is
    the CaptureManager's job (GDD §5); no second refractory lives here.

    See the module docstring for the model-pool / fault / counter contract.
    """

    def __init__(self, holder: ConfigHolder) -> None:
        self._holder = holder
        self._states: dict[int, _UserWakeState] = {}
        self._spares: list[Any] = []
        self._spares_key: _CfgKey = self._cfg_key()
        self._build_inflight = False
        self._replenish_tasks: set[asyncio.Task[None]] = set()
        self._faulted = False
        self._fault_key: _CfgKey | None = None
        # Stage counters (cumulative; health snapshots them).
        self._c_frames_seen = 0
        self._c_vad_speech = 0
        self._c_inferences = 0
        self._c_hits = 0
        self._c_near_misses = 0
        # Eager first spare: __main__ constructs this detector inside
        # asyncio.to_thread precisely to keep the ONNX session load off the
        # event loop. Building one model here hands it to the first speaker,
        # warms the OS file cache for later builds, and fails fast at boot
        # when wake.model is missing instead of killing the audio path at
        # 02:00. An absent openwakeword package (unit tests fake the
        # _predict_chunk seam and never install it) falls back to the pool's
        # lazy path.
        try:
            self._spares.append(self._build_model())
        except ImportError:  # pragma: no cover — test envs without openwakeword
            log.warning("openwakeword_unavailable_lazy_load")

    # ── state the rest of the system reads ───────────────────────────────────

    @property
    def faulted(self) -> bool:
        """True while model builds are latched off after a build failure."""
        return self._faulted

    def counters(self) -> dict[str, int]:
        """Pipeline stage counters, cumulative since process start.

        - ``frames_seen`` — frames offered to the wake stage (CaptureManager
          only forwards VAD-speech frames while IDLE, so these already passed
          the VAD gate).
        - ``vad_speech`` — frames actually accepted into a live model's
          stream. Diverges from ``frames_seen`` exactly when scoring is
          skipped (faulted, or a new speaker awaiting a spare) — the visible
          zero that turns a silent wake death into an alarm.
        - ``inferences`` — 80 ms chunks run through a model.
        - ``hits`` / ``near_misses`` — threshold crossings / logged
          near-misses (§16 tuning signal).
        """
        return {
            "frames_seen": self._c_frames_seen,
            "vad_speech": self._c_vad_speech,
            "inferences": self._c_inferences,
            "hits": self._c_hits,
            "near_misses": self._c_near_misses,
        }

    # ── hot path ─────────────────────────────────────────────────────────────

    def score(self, user_id: int, frame: bytes) -> float:
        if len(frame) != FRAME_BYTES:
            raise ValueError(f"expected one {FRAME_BYTES}-byte 20ms frame, got {len(frame)} bytes")
        self._c_frames_seen += 1
        key = self._cfg_key()
        if self._faulted:
            if key == self._fault_key:
                return 0.0  # latched: no per-chunk retry storm (logged once)
            # The config changed since the fault: clear it and try again.
            self._faulted = False
            self._fault_key = None
            log.info("wake_fault_cleared", reason="config_changed")

        state = self._states.get(user_id)
        if state is None:
            state = self._states[user_id] = _UserWakeState()
        if state.cfg_key is not None and state.cfg_key != key:
            # SIGHUP changed wake.model / wake.vad_threshold: drop the stale
            # model and rebuild lazily through the pool (never inline here).
            log.info("wake_model_stale_dropped", user_id=user_id)
            state.model = None
            state.pending.clear()
            state.last_score = 0.0
            state.peak_logged = 0.0
        state.cfg_key = key

        if state.model is None:
            state.model = self._take_spare(key)
            # Top the pool back up for the NEXT speaker (with no running loop
            # — pure-sync tests — this may complete inline; retry the take so
            # the sync path loses no frames).
            self._request_spare(key)
            if state.model is None:
                state.model = self._take_spare(key)
            if state.model is None:
                # No spare ready: skip wake scoring for this user until the
                # background build lands. Never build on the hot path.
                return 0.0

        self._c_vad_speech += 1
        state.pending += frame
        while len(state.pending) >= OWW_CHUNK_BYTES:
            chunk = bytes(state.pending[:OWW_CHUNK_BYTES])
            del state.pending[:OWW_CHUNK_BYTES]
            self._c_inferences += 1
            state.last_score = self._predict_chunk(state, chunk)

        threshold = self._holder.current.wake.threshold
        if state.last_score >= threshold:
            hit_score = state.last_score
            self._rearm_after_hit(state)
            self._c_hits += 1
            log.info("wake_hit", user_id=user_id, score=round(hit_score, 3))
            return hit_score
        # Near-miss visibility: without this, a phrase that scores just under
        # the threshold is indistinguishable from silence in the log — the exact
        # ambiguity that makes "the wake word doesn't work" impossible to triage.
        if state.last_score >= _NEAR_MISS_FLOOR:
            if state.last_score >= state.peak_logged + _NEAR_MISS_STEP:
                state.peak_logged = state.last_score
                self._c_near_misses += 1
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

    # ── the model pool ───────────────────────────────────────────────────────

    def _cfg_key(self) -> _CfgKey:
        cfg = self._holder.current.wake
        return (cfg.model, cfg.vad_threshold)

    def _take_spare(self, key: _CfgKey) -> Any:
        """Hand out a pre-built model matching ``key``; None when empty."""
        if self._spares_key != key:
            # Spares built for a superseded config are worthless — drop them.
            self._spares.clear()
            self._spares_key = key
        return self._spares.pop() if self._spares else None

    def _request_spare(self, key: _CfgKey) -> None:
        """Top the pool back up in the background (never on the hot path)."""
        if self._faulted or self._build_inflight:
            return
        if self._spares_key == key and len(self._spares) >= _SPARE_TARGET:
            return
        self._build_inflight = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (pure-sync tests): build inline — mirrors the
            # CaptureManager._dispatch convention for the sync test path.
            self._build_spare_blocking(key)
            return
        task = loop.create_task(self._build_spare_off_loop(key))
        self._replenish_tasks.add(task)
        task.add_done_callback(self._replenish_tasks.discard)

    async def _build_spare_off_loop(self, key: _CfgKey) -> None:
        try:
            model = await asyncio.to_thread(self._build_model)
        except Exception as exc:  # noqa: BLE001 — a bad build must latch, not raise
            self._enter_fault(key, exc)
            return
        self._store_spare(key, model)

    def _build_spare_blocking(self, key: _CfgKey) -> None:
        try:
            model = self._build_model()
        except Exception as exc:  # noqa: BLE001 — a bad build must latch, not raise
            self._enter_fault(key, exc)
            return
        self._store_spare(key, model)

    def _store_spare(self, key: _CfgKey, model: Any) -> None:
        self._build_inflight = False
        if key != self._cfg_key():
            return  # config changed mid-build; the next frame re-requests
        if self._spares_key != key:
            self._spares.clear()
            self._spares_key = key
        self._spares.append(model)

    def _enter_fault(self, key: _CfgKey, exc: Exception) -> None:
        """Latch the detector faulted for this config generation. Loud, once."""
        self._build_inflight = False
        self._faulted = True
        self._fault_key = key
        log.error(
            "wake_model_build_failed",
            model=key[0],
            error=str(exc),
            detail="wake detection DISABLED until the wake config changes or a restart; "
            "audio keeps flowing and slash commands are unaffected",
        )

    # ── internals ────────────────────────────────────────────────────────────

    def _build_model(self) -> Any:
        from openwakeword.model import Model  # lazy

        cfg = self._holder.current.wake
        kwargs: dict[str, Any] = {
            "wakeword_models": [cfg.model],
            "inference_framework": "onnx",
        }
        # Silero VAD gate (GDD §5.3): a wake trigger only counts when the VAD
        # simultaneously scores speech — music/game-audio/keyboard noise on
        # busy comms can no longer false-fire on its own. 0.0 disables.
        if cfg.vad_threshold > 0.0:
            kwargs["vad_threshold"] = cfg.vad_threshold
            try:
                return Model(**kwargs)
            except Exception as exc:  # noqa: BLE001 — degrade, never kill wake
                log.warning("wake_vad_gate_unavailable", error=str(exc))
                del kwargs["vad_threshold"]
        return Model(**kwargs)

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

        samples = np.frombuffer(chunk, dtype=np.int16)
        predictions: dict[str, float] = state.model.predict(samples)
        if not predictions:
            return 0.0
        return float(max(predictions.values()))
