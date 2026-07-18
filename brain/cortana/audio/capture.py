"""Per-user capture state machine — GDD §5, §19; docs/INTERFACES.md.

For every speaker, continuously:

    IDLE ──(VAD speech + wake hit)──► CAPTURING ──(force_endpoint | cap)──► emit
      ▲                                                                     │
      └────────────────── refractory (wake.refractory_ms) ◄─────────────────┘

plus the wake-free window (GDD §5.4/§8.3): :meth:`CaptureManager.arm_window`
arms ARMED — the next speech inside it is captured as if a wake hit had
occurred. Windows are armed and disarmed ONLY by the dialog engine; their
lifetime is wall-clock, owned by the engine's wheel, never by frame count
(Discord DTX sends no frames during silence — a frame-counted window can
never expire, which is exactly the stuck-open incident this design removes).

Privacy (CLAUDE.md constraint 5, GDD §19): audio exists only in two places,
both RAM — the per-user pre-roll ring (a fixed-size deque holding the last
``RING_MS`` of frames, overwritten as it rolls) and the active capture buffer,
which is released the moment the utterance is handed off. Nothing here writes,
or can write, to disk.

Determinism: the manager takes VAD and wake as injected dependencies; the
only frame-counted duration left is the hard cap (frames DO flow while the
pilot is talking). All silence timing lives in the dialog engine's wheel.

:meth:`feed` is the sync hot path, called by the IPC reader on the event loop;
it must never block. The async ``on_utterance`` callback is scheduled as a
task on the running loop (or, when no loop is running — pure-sync tests — run
to completion inline).
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum

import structlog

from cortana.audio.vad import FRAME_BYTES, FRAME_MS, VadGate
from cortana.audio.wake import WakeDetector
from cortana.config import AuraConfig, ConfigHolder

log = structlog.get_logger(__name__)

__all__ = ["RING_MS", "CaptureManager", "CaptureMeta", "CaptureOrigin", "Phase"]

#: Pre-roll ring depth — "last 1.5s, RAM only, overwritten" (GDD §5, §19).
RING_MS = 1500
_RING_FRAMES = RING_MS // FRAME_MS


class Phase(Enum):
    """Where a user's state machine currently sits."""

    IDLE = "idle"
    CAPTURING = "capturing"
    #: A wake-free window is armed (dialog engine authority — GDD §5.4).
    ARMED = "armed"


class CaptureOrigin(Enum):
    """How a capture opened: the wake word, or an armed dialog window."""

    WAKE = "wake"
    WINDOW = "window"


@dataclass(frozen=True, slots=True)
class CaptureMeta:
    """Provenance of one emitted utterance, handed to the dialog engine."""

    origin: CaptureOrigin
    #: Dialog generation token the capture belongs to (gen-stale emissions
    #: are dropped by the engine instead of being misattributed).
    gen: int
    #: VAD-speech frames observed AFTER the capture opened (pre-roll and the
    #: trigger frame excluded). Zero means the pilot never actually spoke —
    #: the engine discards the buffer without an STT decode.
    speech_frames: int
    reason: str


#: ``on_utterance(user_id, guild_id, pcm, meta)`` — pcm is empty for
#: zero-speech (abandoned) captures; the buffer is dropped before dispatch.
OnUtterance = Callable[[int, int, bytes, CaptureMeta], Awaitable[None]]

#: ``on_capture_start(user_id, guild_id, origin, armed_gen) -> gen`` — sync,
#: on the hot path. The dialog engine transitions its session and returns the
#: generation token to stamp on this capture (for WINDOW origins that is the
#: armed gen echoed back; for WAKE origins the engine mints a fresh one).
OnCaptureStart = Callable[[int, int, CaptureOrigin, int | None], int]


@dataclass(slots=True)
class _UserState:
    """All capture state for one speaker. Audio lives only in ``ring``/``capture``."""

    ring: deque[bytes] = field(default_factory=lambda: deque(maxlen=_RING_FRAMES))
    phase: Phase = Phase.IDLE
    guild_id: int = 0
    capture: list[bytes] | None = None
    capture_gen: int = 0
    capture_origin: CaptureOrigin = CaptureOrigin.WAKE
    speech_frames: int = 0
    refractory_frames_left: int = 0
    armed_gen: int | None = None


class CaptureManager:
    """Owns the per-user pre-roll rings and capture state machines (GDD §5)."""

    def __init__(
        self,
        holder: ConfigHolder,
        vad: VadGate,
        wake: WakeDetector,
        on_utterance: OnUtterance,
        on_capture_start: OnCaptureStart,
    ) -> None:
        self._holder = holder
        self._vad = vad
        self._wake = wake
        self._on_utterance = on_utterance
        self._on_capture_start = on_capture_start
        self._states: dict[int, _UserState] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # ── hot path ─────────────────────────────────────────────────────────────

    def feed(self, user_id: int, guild_id: int, frame: bytes) -> None:
        """Consume one 20 ms frame for this user. Sync; never blocks.

        Frames of the wrong size are dropped (a malformed IPC payload must not
        poison the ring), logged once per offending frame.
        """
        if len(frame) != FRAME_BYTES:
            log.warning("frame_dropped_bad_size", user_id=user_id, size=len(frame))
            return

        cfg = self._holder.current
        state = self._states.get(user_id)
        if state is None:
            state = self._states[user_id] = _UserState()
        state.guild_id = guild_id

        is_speech = self._vad.is_speech(frame)
        # A frame is refractory-gated iff the counter was still positive when it
        # arrived, so the period covers exactly refractory_ms of frames.
        in_refractory = state.refractory_frames_left > 0
        if in_refractory:
            state.refractory_frames_left -= 1

        if state.phase is Phase.CAPTURING:
            self._feed_capturing(user_id, state, cfg, frame, is_speech)
        elif state.phase is Phase.ARMED:
            if is_speech:
                self._begin_capture(user_id, state, cfg, frame, CaptureOrigin.WINDOW)
        else:
            self._feed_idle(user_id, state, cfg, frame, is_speech, in_refractory)

        # The ring rolls in every phase: the privacy window stays 1.5s and the
        # next capture's pre-roll is always current.
        state.ring.append(frame)

    # ── dialog-engine control (GDD §5.4) ─────────────────────────────────────

    def arm_window(self, user_id: int, guild_id: int, gen: int) -> None:
        """Arm a wake-free capture window for dialog generation ``gen``.

        Called only by the dialog engine's ArmWindow executor. The window has
        NO lifetime here — the engine's wall-clock wheel calls :meth:`disarm`
        when it expires, so DTX silence cannot freeze it open.

        Refused while a capture is OPEN: the executor runs after awaiting the
        spoken prompt, and a pilot who re-woke during that gap has a live
        capture buffer that a stale window must never destroy (the engine
        also gen-gates the action; this is the capture-side backstop).
        """
        state = self._states.get(user_id)
        if state is None:
            state = self._states[user_id] = _UserState()
        if state.phase is Phase.CAPTURING:
            log.warning("window_arm_refused_capturing", user_id=user_id, gen=gen)
            return
        state.guild_id = guild_id
        state.phase = Phase.ARMED
        state.armed_gen = gen
        state.refractory_frames_left = 0
        state.capture = None
        log.info("window_armed", user_id=user_id, gen=gen)

    def disarm(self, user_id: int) -> None:
        """Close an armed window without capturing. No-op in other phases."""
        state = self._states.get(user_id)
        if state is None or state.phase is not Phase.ARMED:
            return
        state.phase = Phase.IDLE
        state.armed_gen = None
        log.info("window_disarmed", user_id=user_id)

    def drop_user(self, user_id: int) -> None:
        """Purge every trace of this user: ring, capture, wake state (GDD §19)."""
        self._states.pop(user_id, None)
        self._wake.reset(user_id)
        log.info("user_dropped", user_id=user_id)

    # ── introspection (dialog wheel / health / tests) ────────────────────────

    def phase_of(self, user_id: int) -> Phase:
        state = self._states.get(user_id)
        return state.phase if state is not None else Phase.IDLE

    def is_capturing(self, user_id: int) -> bool:
        return self.phase_of(user_id) is Phase.CAPTURING

    def capturing_users(self) -> list[int]:
        """User ids with an open capture — for the dialog engine's wheel."""
        return [uid for uid, st in self._states.items() if st.phase is Phase.CAPTURING]

    def force_endpoint(self, user_id: int, reason: str = "silence") -> bool:
        """End an open capture now and emit it (GDD §5).

        Discord sends no packets while a pilot is silent — it stops the stream
        rather than sending silence frames — so a frame-counted endpoint can
        never fire on a real pause. The dialog engine's wall-clock wheel
        drives this instead. No-op unless the user is actually capturing."""
        state = self._states.get(user_id)
        if state is None or state.phase is not Phase.CAPTURING or state.capture is None:
            return False
        self._emit(user_id, state, self._holder.current, reason=reason)
        return True

    @property
    def tracked_users(self) -> int:
        return len(self._states)

    # ── phase handlers ───────────────────────────────────────────────────────

    def _feed_idle(
        self,
        user_id: int,
        state: _UserState,
        cfg: AuraConfig,
        frame: bytes,
        is_speech: bool,
        in_refractory: bool,
    ) -> None:
        if not is_speech or in_refractory:
            # Wake inference is gated behind VAD (GDD §3.3) and the per-user
            # refractory period (GDD §5).
            return
        score = self._wake.score(user_id, frame)
        if score >= cfg.wake.threshold:
            self._begin_capture(user_id, state, cfg, frame, CaptureOrigin.WAKE)

    def _feed_capturing(
        self, user_id: int, state: _UserState, cfg: AuraConfig, frame: bytes, is_speech: bool
    ) -> None:
        assert state.capture is not None
        state.capture.append(frame)
        if is_speech:
            state.speech_frames += 1
        # The hard cap is the only frame-counted duration left: frames DO
        # flow while the pilot talks, so a runaway capture ends here even if
        # the wall-clock wheel is starved.
        max_frames = max(1, cfg.capture.max_utterance_ms // FRAME_MS)
        if len(state.capture) >= max_frames:
            self._emit(user_id, state, cfg, reason="hard_cap")

    # ── transitions ──────────────────────────────────────────────────────────

    def _begin_capture(
        self,
        user_id: int,
        state: _UserState,
        cfg: AuraConfig,
        frame: bytes,
        origin: CaptureOrigin,
    ) -> None:
        """Open the capture buffer, seeded with pre-roll from the ring.

        The ring has not yet received the current frame, so the seed is exactly
        the ``preroll_ms`` of audio *preceding* the trigger, then the trigger
        frame itself.
        """
        preroll_frames = max(0, cfg.capture.preroll_ms // FRAME_MS)
        preroll = list(state.ring)[-preroll_frames:] if preroll_frames else []
        armed_gen = state.armed_gen
        state.capture = [*preroll, frame]
        state.phase = Phase.CAPTURING
        state.armed_gen = None
        state.speech_frames = 0
        state.capture_origin = origin
        # The dialog engine transitions its session NOW and tells us which
        # generation this capture belongs to (sync, must not block).
        state.capture_gen = self._on_capture_start(user_id, state.guild_id, origin, armed_gen)
        log.info(
            "capture_started",
            user_id=user_id,
            origin=origin.value,
            gen=state.capture_gen,
            preroll_frames=len(preroll),
        )

    def _emit(self, user_id: int, state: _UserState, cfg: AuraConfig, reason: str) -> None:
        """Hand the utterance off and free the capture buffer immediately (GDD §19)."""
        assert state.capture is not None
        meta = CaptureMeta(
            origin=state.capture_origin,
            gen=state.capture_gen,
            speech_frames=state.speech_frames,
            reason=reason if state.speech_frames else "abandoned",
        )
        # Constraint 5: a capture with no actual speech is dropped HERE — the
        # buffer never crosses into the STT path at all.
        pcm = b"".join(state.capture) if state.speech_frames else b""
        guild_id = state.guild_id
        state.capture = None
        state.phase = Phase.IDLE
        state.speech_frames = 0
        state.refractory_frames_left = max(1, cfg.wake.refractory_ms // FRAME_MS)
        log.info(
            "utterance_emitted",
            user_id=user_id,
            guild_id=guild_id,
            ms=len(pcm) // FRAME_BYTES * FRAME_MS,
            reason=meta.reason,
            gen=meta.gen,
            speech_frames=meta.speech_frames,
        )
        self._dispatch(self._on_utterance(user_id, guild_id, pcm, meta))

    def _dispatch(self, coro: Awaitable[None]) -> None:
        """Schedule the async handler without blocking the hot path.

        On the live system :meth:`feed` runs on the event loop (IPC reader),
        so the coroutine becomes a task; a reference is kept until done so it
        cannot be garbage-collected mid-flight. With no running loop (sync
        tests) the coroutine is run to completion inline.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_await(coro))
            return
        task = loop.create_task(_await(coro))
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        """Reap a finished utterance task; a swallowed exception here would
        silently drop the pilot's command, so it is always logged."""
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("utterance_task_failed", exc_info=task.exception())


async def _await(coro: Awaitable[None]) -> None:
    await coro
