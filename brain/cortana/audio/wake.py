"""Wake-word detection — GDD §5.1/§5.2, §16 (wake block).

:class:`OpenWakeWordDetector` runs one openWakeWord model instance per active
speaker, because the model is a streaming detector with internal mel/embedding
buffers: interleaving two users' audio through one instance would smear their
streams together. Per-user state is created on first frame and purged via
:meth:`reset` when the user leaves or opts out.

openWakeWord's native input granularity is 80 ms (1280 samples); Ears delivers
20 ms frames, so the detector accumulates four frames per inference call and
holds the last score in between.

Multiple wake phrases (GDD §5.1): ``wake.model`` is the primary phrase;
``wake.extra_models`` (default empty) lists additional ONNX chains scored in
parallel — a corp mid-transition runs the old and new phrase side by side.
Each per-user unit is a **bank** of ``(path, model)`` pairs, one instance per
configured path; a chunk's score is the MAX across the bank and
``wake.threshold`` applies to that max. Hits are counted per model
(:meth:`counters`) so #bot-health shows which phrase pilots actually use.

Model lifecycle (the hardened pool):

- **Models are NEVER built on the audio hot path.** :meth:`score` runs on the
  event loop (IPC reader → CaptureManager.feed) and an ONNX session load
  stalls it for hundreds of ms. A spare bank is built at init (off-loop, via
  ``asyncio.to_thread`` in ``__main__``) and handed to the first speaker;
  whenever a spare is consumed a replacement is built in the background via
  ``asyncio.to_thread``. A NEW speaker who arrives before a spare is ready is
  simply not wake-scored until it lands — their frames still roll through the
  pre-roll ring upstream, so nothing else degrades.
- **Config-generation tagging.** Every bank (spare or live) is stamped with
  the ``(wake.model, wake.extra_models, wake.vad_threshold)`` it was built
  from; a SIGHUP that changes any of them — including any change to the
  extra-model list — drops stale banks lazily and rebuilds through the pool.
  ``wake.threshold`` needs no rebuild — it is read per frame.
- **Faulted state.** A PRIMARY model build failure (file removed by a config
  push, a broken openwakeword upgrade) latches the detector faulted for the
  current config generation: logged loudly ONCE, :meth:`score` returns 0.0,
  and no per-chunk retry storm grinds the loop. A config change (new
  generation) clears the fault and retries. A broken EXTRA model never
  faults the detector: it is logged once per config generation and skipped —
  the primary and the remaining extras keep running. Health reads
  :attr:`faulted`.
- **Stage counters.** :meth:`counters` exposes frames_seen → vad_speech →
  inferences → hits/near_misses (plus per-model hit attribution) so a silent
  wake death is a visible zero in ``#bot-health`` instead of a green status
  over a dead pipeline.

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
from pathlib import PurePath
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

#: The pool keeps this many pre-built model banks ready for new speakers.
_SPARE_TARGET = 1

#: (wake.model, wake.extra_models, wake.vad_threshold) — the config inputs
#: baked into a built model bank. ANY change to the model list (primary or
#: extras, including ordering) rebuilds. wake.threshold is deliberately NOT
#: part of the key: it is read per frame and needs no rebuild.
_CfgKey = tuple[str, tuple[str, ...], float]

#: One model instance per configured path, primary first: ((path, model), …).
_Bank = tuple[tuple[str, Any], ...]


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
    #: Path of the model behind ``last_score`` ("" until a chunk is scored);
    #: attributes hits/near-misses to a phrase in logs and counters.
    last_model: str = ""
    #: Highest near-miss already logged this episode; reset when the score
    #: falls back under the floor or a hit re-arms the detector.
    peak_logged: float = 0.0
    #: The per-user model bank ((path, openwakeword Model), …) from the pool.
    bank: _Bank | None = None
    #: Config generation this user's bank was built from; a mismatch with
    #: the live config drops the bank for a lazy rebuild (SIGHUP retune).
    cfg_key: _CfgKey | None = None


class OpenWakeWordDetector:
    """openWakeWord-backed :class:`WakeDetector` for the configured ONNX models.

    A hit (max score across the bank >= ``wake.threshold``) clears this
    user's streaming state — pending bytes, held score, and every bank
    model's prediction buffer — so the detector re-arms clean and the same
    utterance's tail cannot retrigger from the held score. Wake-hit
    *suppression* for ``wake.refractory_ms`` is the CaptureManager's job
    (GDD §5); no second refractory lives here.

    See the module docstring for the model-pool / fault / counter contract.
    """

    def __init__(self, holder: ConfigHolder) -> None:
        self._holder = holder
        self._states: dict[int, _UserWakeState] = {}
        self._spares: list[_Bank] = []
        self._spares_key: _CfgKey = self._cfg_key()
        self._build_inflight = False
        self._replenish_tasks: set[asyncio.Task[None]] = set()
        self._faulted = False
        self._fault_key: _CfgKey | None = None
        #: (config generation, path) pairs whose extra-model build failure was
        #: already logged — a broken extra is loud once, then silent until the
        #: wake config changes.
        self._extra_warned: set[tuple[_CfgKey, str]] = set()
        # Stage counters (cumulative; health snapshots them).
        self._c_frames_seen = 0
        self._c_vad_speech = 0
        self._c_inferences = 0
        self._c_hits = 0
        self._c_near_misses = 0
        #: Per-model hit attribution, keyed by configured model path.
        self._c_hits_by_model: dict[str, int] = {}
        # Eager first spare: __main__ constructs this detector inside
        # asyncio.to_thread precisely to keep the ONNX session load off the
        # event loop. Building one bank here hands it to the first speaker,
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
        """True while model builds are latched off after a PRIMARY build failure."""
        return self._faulted

    def counters(self) -> dict[str, int]:
        """Pipeline stage counters, cumulative since process start.

        - ``frames_seen`` — frames offered to the wake stage (CaptureManager
          only forwards VAD-speech frames while IDLE, so these already passed
          the VAD gate).
        - ``vad_speech`` — frames actually accepted into a live bank's
          stream. Diverges from ``frames_seen`` exactly when scoring is
          skipped (faulted, or a new speaker awaiting a spare) — the visible
          zero that turns a silent wake death into an alarm.
        - ``inferences`` — 80 ms chunks run through a bank (every model in
          the bank scores the chunk; the count is per chunk, not per model).
        - ``hits`` / ``near_misses`` — threshold crossings / logged
          near-misses (§16 tuning signal).
        - ``hits[<model stem>]`` — hits attributed to each configured model
          (multi-phrase mode, GDD §5.1); models sharing a file stem merge.
        """
        out = {
            "frames_seen": self._c_frames_seen,
            "vad_speech": self._c_vad_speech,
            "inferences": self._c_inferences,
            "hits": self._c_hits,
            "near_misses": self._c_near_misses,
        }
        for path, hits in self._c_hits_by_model.items():
            key = f"hits[{PurePath(path).stem}]"
            out[key] = out.get(key, 0) + hits
        return out

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
            # SIGHUP changed wake.model / wake.extra_models /
            # wake.vad_threshold: drop the stale bank and rebuild lazily
            # through the pool (never inline here).
            log.info("wake_model_stale_dropped", user_id=user_id)
            state.bank = None
            state.pending.clear()
            state.last_score = 0.0
            state.last_model = ""
            state.peak_logged = 0.0
        state.cfg_key = key

        if state.bank is None:
            state.bank = self._take_spare(key)
            # Top the pool back up for the NEXT speaker (with no running loop
            # — pure-sync tests — this may complete inline; retry the take so
            # the sync path loses no frames).
            self._request_spare(key)
            if state.bank is None:
                state.bank = self._take_spare(key)
            if state.bank is None:
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
            # Attribute the hit to the winning model (the primary when a
            # faked/legacy predict seam never set last_model).
            hit_model = state.last_model or key[0]
            self._rearm_after_hit(state)
            self._c_hits += 1
            self._c_hits_by_model[hit_model] = self._c_hits_by_model.get(hit_model, 0) + 1
            log.info(
                "wake_hit",
                user_id=user_id,
                score=round(hit_score, 3),
                model=PurePath(hit_model).stem,
            )
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
                    model=PurePath(state.last_model or key[0]).stem,
                )
        else:
            state.peak_logged = 0.0
        return state.last_score

    def reset(self, user_id: int) -> None:
        self._states.pop(user_id, None)

    # ── the model pool ───────────────────────────────────────────────────────

    def _cfg_key(self) -> _CfgKey:
        cfg = self._holder.current.wake
        return (cfg.model, cfg.extra_models, cfg.vad_threshold)

    def _take_spare(self, key: _CfgKey) -> _Bank | None:
        """Hand out a pre-built model bank matching ``key``; None when empty."""
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
            bank = await asyncio.to_thread(self._build_model)
        except Exception as exc:  # noqa: BLE001 — a bad build must latch, not raise
            self._enter_fault(key, exc)
            return
        self._store_spare(key, bank)

    def _build_spare_blocking(self, key: _CfgKey) -> None:
        try:
            bank = self._build_model()
        except Exception as exc:  # noqa: BLE001 — a bad build must latch, not raise
            self._enter_fault(key, exc)
            return
        self._store_spare(key, bank)

    def _store_spare(self, key: _CfgKey, bank: _Bank) -> None:
        self._build_inflight = False
        if key != self._cfg_key():
            return  # config changed mid-build; the next frame re-requests
        if self._spares_key != key:
            self._spares.clear()
            self._spares_key = key
        self._spares.append(bank)

    def _enter_fault(self, key: _CfgKey, exc: Exception) -> None:
        """Latch the detector faulted for this config generation. Loud, once.

        Only the PRIMARY model can land here — a broken extra is skipped
        inside :meth:`_build_model` and never reaches the latch.
        """
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

    def _build_model(self) -> _Bank:
        """Build one model bank: the primary plus every extra that loads.

        A primary failure raises (→ fault latch upstream). An extra failure
        is logged once per (config generation, path) and skipped — the
        primary and remaining extras keep the detector alive.
        """
        cfg = self._holder.current.wake
        key: _CfgKey = (cfg.model, cfg.extra_models, cfg.vad_threshold)
        bank: list[tuple[str, Any]] = [(cfg.model, self._build_one(cfg.model, cfg.vad_threshold))]
        for path in cfg.extra_models:
            try:
                bank.append((path, self._build_one(path, cfg.vad_threshold)))
            except Exception as exc:  # noqa: BLE001 — an extra must never fault the bank
                if (key, path) not in self._extra_warned:
                    self._extra_warned.add((key, path))
                    log.warning(
                        "wake_extra_model_build_failed",
                        model=path,
                        error=str(exc),
                        detail="extra wake model skipped; the primary model and "
                        "remaining extras keep running",
                    )
        return tuple(bank)

    @staticmethod
    def _build_one(model_path: str, vad_threshold: float) -> Any:
        from openwakeword.model import Model  # lazy

        kwargs: dict[str, Any] = {
            "wakeword_models": [model_path],
            "inference_framework": "onnx",
        }
        # Silero VAD gate (GDD §5.3): a wake trigger only counts when the VAD
        # simultaneously scores speech — music/game-audio/keyboard noise on
        # busy comms can no longer false-fire on its own. 0.0 disables.
        if vad_threshold > 0.0:
            kwargs["vad_threshold"] = vad_threshold
            try:
                return Model(**kwargs)
            except Exception as exc:  # noqa: BLE001 — degrade, never kill wake
                log.warning("wake_vad_gate_unavailable", model=model_path, error=str(exc))
                del kwargs["vad_threshold"]
        return Model(**kwargs)

    @staticmethod
    def _rearm_after_hit(state: _UserWakeState) -> None:
        """Clear the streaming state after a hit so the detector re-arms clean."""
        state.last_score = 0.0
        state.last_model = ""
        state.peak_logged = 0.0
        state.pending.clear()
        for _path, model in state.bank or ():
            if hasattr(model, "reset"):
                model.reset()

    def _predict_chunk(self, state: _UserWakeState, chunk: bytes) -> float:
        """Run one 80 ms chunk through this user's bank; return the max score."""
        import numpy as np  # lazy

        samples = np.frombuffer(chunk, dtype=np.int16)
        bank = state.bank or ()
        best = 0.0
        best_path = bank[0][0] if bank else ""
        for path, model in bank:
            predictions: dict[str, float] = model.predict(samples)
            if not predictions:
                continue
            model_score = float(max(predictions.values()))
            if model_score > best:
                best = model_score
                best_path = path
        state.last_model = best_path
        return best
