"""Per-user capture state machine — GDD §5, §19; docs/INTERFACES.md.

For every speaker, continuously:

    IDLE ──(VAD speech + wake hit)──► CAPTURING ──(endpoint | hard cap)──► emit
      ▲                                                                    │
      └───────────────── refractory (wake.refractory_ms) ◄─────────────────┘

plus the LOW-tier "say again" path (GDD §8.3): :meth:`CaptureManager.reopen`
arms a wake-word-free window — the next speech inside it is captured as if a
wake hit had occurred.

Privacy (CLAUDE.md constraint 5, GDD §19): audio exists only in two places,
both RAM — the per-user pre-roll ring (a fixed-size deque holding the last
``RING_MS`` of frames, overwritten as it rolls) and the active capture buffer,
which is released the moment the utterance is handed off. Nothing here writes,
or can write, to disk.

Determinism: the manager takes VAD and wake as injected dependencies and
derives all time from frame count (20 ms per fed frame). No wall clock exists
anywhere in this module.

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

from aura.audio.vad import FRAME_BYTES, FRAME_MS, EndpointTracker, VadGate
from aura.audio.wake import WakeDetector
from aura.config import AuraConfig, ConfigHolder

log = structlog.get_logger(__name__)

__all__ = ["DEFAULT_REOPEN_MS", "RING_MS", "CaptureManager", "Phase"]

#: Pre-roll ring depth — "last 1.5s, RAM only, overwritten" (GDD §5, §19).
RING_MS = 1500
_RING_FRAMES = RING_MS // FRAME_MS

#: Default "say again" window — GDD §8.3: reopen the capture window for 4s.
DEFAULT_REOPEN_MS = 4000

OnUtterance = Callable[[int, int, bytes], Awaitable[None]]


class Phase(Enum):
    """Where a user's state machine currently sits."""

    IDLE = "idle"
    CAPTURING = "capturing"
    REOPENED = "reopened"


@dataclass(slots=True)
class _UserState:
    """All capture state for one speaker. Audio lives only in ``ring``/``capture``."""

    ring: deque[bytes] = field(default_factory=lambda: deque(maxlen=_RING_FRAMES))
    phase: Phase = Phase.IDLE
    guild_id: int = 0
    capture: list[bytes] | None = None
    endpoint: EndpointTracker | None = None
    refractory_frames_left: int = 0
    reopen_frames_left: int = 0


class CaptureManager:
    """Owns the per-user pre-roll rings and capture state machines (GDD §5)."""

    def __init__(
        self,
        holder: ConfigHolder,
        vad: VadGate,
        wake: WakeDetector,
        on_utterance: OnUtterance,
        on_capture_start: Callable[[int, int], None] | None = None,
    ) -> None:
        self._holder = holder
        self._vad = vad
        self._wake = wake
        self._on_utterance = on_utterance
        # Fired (sync, on the hot path) the instant a capture opens — the
        # composition root uses it to speak the "go ahead" cue so the pilot
        # knows AURA is listening (GDD §5). Must not block; it schedules.
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
        elif state.phase is Phase.REOPENED:
            self._feed_reopened(user_id, state, cfg, frame, is_speech)
        else:
            self._feed_idle(user_id, state, cfg, frame, is_speech, in_refractory)

        # The ring rolls in every phase: the privacy window stays 1.5s and the
        # next capture's pre-roll is always current.
        state.ring.append(frame)

    # ── control ──────────────────────────────────────────────────────────────

    def reopen(self, user_id: int, guild_id: int, window_ms: int = DEFAULT_REOPEN_MS) -> None:
        """Arm a wake-free capture window (LOW-tier "say again", GDD §8.3).

        For the next ``window_ms`` of this user's audio, speech starts a
        capture with no wake hit required. Clears any refractory left over
        from the utterance that scored LOW. The window is measured in fed
        frames; it cannot expire while the user is not transmitting.
        """
        state = self._states.get(user_id)
        if state is None:
            state = self._states[user_id] = _UserState()
        state.guild_id = guild_id
        state.phase = Phase.REOPENED
        state.reopen_frames_left = max(1, window_ms // FRAME_MS)
        state.refractory_frames_left = 0
        state.capture = None
        state.endpoint = None
        log.info("capture_reopened", user_id=user_id, window_ms=window_ms)

    def drop_user(self, user_id: int) -> None:
        """Purge every trace of this user: ring, capture, wake state (GDD §19)."""
        self._states.pop(user_id, None)
        self._wake.reset(user_id)
        log.info("user_dropped", user_id=user_id)

    # ── introspection (health reporting / tests) ─────────────────────────────

    def phase_of(self, user_id: int) -> Phase:
        state = self._states.get(user_id)
        return state.phase if state is not None else Phase.IDLE

    def is_capturing(self, user_id: int) -> bool:
        return self.phase_of(user_id) is Phase.CAPTURING

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
            self._begin_capture(user_id, state, cfg, frame)

    def _feed_reopened(
        self, user_id: int, state: _UserState, cfg: AuraConfig, frame: bytes, is_speech: bool
    ) -> None:
        state.reopen_frames_left -= 1
        if is_speech:
            self._begin_capture(user_id, state, cfg, frame)
        elif state.reopen_frames_left <= 0:
            state.phase = Phase.IDLE
            log.info("reopen_expired", user_id=user_id)

    def _feed_capturing(
        self, user_id: int, state: _UserState, cfg: AuraConfig, frame: bytes, is_speech: bool
    ) -> None:
        assert state.capture is not None and state.endpoint is not None
        state.capture.append(frame)
        max_frames = max(1, cfg.capture.max_utterance_ms // FRAME_MS)
        endpointed = state.endpoint.update(is_speech)
        if endpointed or len(state.capture) >= max_frames:
            self._emit(user_id, state, cfg, reason="endpoint" if endpointed else "hard_cap")

    # ── transitions ──────────────────────────────────────────────────────────

    def _begin_capture(
        self, user_id: int, state: _UserState, cfg: AuraConfig, frame: bytes
    ) -> None:
        """Open the capture buffer, seeded with pre-roll from the ring.

        The ring has not yet received the current frame, so the seed is exactly
        the ``preroll_ms`` of audio *preceding* the trigger, then the trigger
        frame itself.
        """
        preroll_frames = max(0, cfg.capture.preroll_ms // FRAME_MS)
        preroll = list(state.ring)[-preroll_frames:] if preroll_frames else []
        state.capture = [*preroll, frame]
        state.endpoint = EndpointTracker(cfg.capture.endpoint_silence_ms)
        state.phase = Phase.CAPTURING
        state.reopen_frames_left = 0
        log.info("capture_started", user_id=user_id, preroll_frames=len(preroll))
        if self._on_capture_start is not None:
            self._on_capture_start(user_id, state.guild_id)

    def _emit(self, user_id: int, state: _UserState, cfg: AuraConfig, reason: str) -> None:
        """Hand the utterance off and free the capture buffer immediately (GDD §19)."""
        assert state.capture is not None
        pcm = b"".join(state.capture)
        guild_id = state.guild_id
        state.capture = None
        state.endpoint = None
        state.phase = Phase.IDLE
        state.refractory_frames_left = max(1, cfg.wake.refractory_ms // FRAME_MS)
        log.info(
            "utterance_emitted",
            user_id=user_id,
            guild_id=guild_id,
            ms=len(pcm) // FRAME_BYTES * FRAME_MS,
            reason=reason,
        )
        self._dispatch(self._on_utterance(user_id, guild_id, pcm))

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
