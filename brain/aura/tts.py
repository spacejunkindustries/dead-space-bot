"""Piper TTS: subprocess synthesis, in-memory WAV wrapping, per-guild queues.

GDD §12. The audio path (§12.3) is deliberately minimal:

- Piper runs as a **separate binary** over a subprocess boundary
  (``tts.binary --model tts.voice --output-raw``), text on stdin, raw s16le
  PCM at the model's native rate on stdout. No temp files, ever.
- Brain wraps the raw samples in a WAV header built with :mod:`struct`,
  entirely in memory, and ships the bytes to Ears over IPC. **Brain does not
  resample** — Songbird's Symphonia layer parses the header and resamples to
  48 kHz inside Ears.
- Hard cap ``tts.max_utterance_s`` (§12.2): an utterance that synthesises
  longer than the cap is dropped and :meth:`Speaker.say` returns ``False`` so
  the caller falls back to posting the text in the channel instead.

The voice's native sample rate is read from the Piper voice config JSON
(``<voice>.json`` next to the ``.onnx``) once at init; 22050 Hz is the Piper
default when the file is missing or unreadable.

This module also carries the §12.1 utterance catalogue as pure functions so
every module that speaks uses the exact scripted strings.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path

import structlog

from aura.config import ConfigHolder
from aura.ipc import PRIORITY_NORMAL, IpcServer

__all__ = [
    "DEFAULT_SAMPLE_RATE",
    "Speaker",
    "SynthesisError",
    "ambiguous",
    "build_wav",
    "degraded",
    "flood_control",
    "not_registered",
    "number_word",
    "ping_sent",
    "read_voice_sample_rate",
    "registered",
    "resolved",
    "responders",
    "say_again",
    "say_again_callsign",
    "timer_set",
    "unregistered",
    "whoami",
]

log = structlog.get_logger(__name__)

#: Piper's default output rate when the voice config is unavailable.
DEFAULT_SAMPLE_RATE = 22_050

#: Watchdog for one Piper invocation. Piper is ~10× real time on this class of
#: CPU (GDD §12), so a capped 3 s utterance synthesises in well under a
#: second; anything near this bound means the process is wedged and gets
#: killed rather than stalling the per-guild speech queue.
SYNTHESIS_TIMEOUT_S = 10.0

_CHANNELS = 1
_BYTES_PER_SAMPLE = 2  # s16le


class SynthesisError(Exception):
    """Piper failed: missing binary, non-zero exit, timeout, or empty output."""


# ── §12.1 utterance catalogue ─────────────────────────────────────────────────
# Short. Always short. AURA is talking over a fight. These are the exact
# scripted strings from the GDD table; do not improvise variants elsewhere.

_NUMBER_WORDS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
)


def number_word(n: int) -> str:
    """Spell small counts (``2`` → ``"two"``); larger numbers stay digits."""
    return _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)


def ping_sent(system: str, group: str | None = None, *, type_word: str = "Hostiles") -> str:
    """*"Hostiles Otanuomi, pinged."* / *"Hostiles Otanuomi, pinged home defense."*"""
    if group:
        return f"{type_word} {system}, pinged {group}."
    return f"{type_word} {system}, pinged."


def ambiguous(type_word: str, system: str) -> str:
    """*"Hostiles Otanuomi — say again to confirm."*"""
    return f"{type_word} {system} — say again to confirm."


def say_again() -> str:
    """*"Say again the system."* — unresolved system name (LOW tier)."""
    return "Say again the system."


def responders(n: int, system: str) -> str:
    """*"Two responding to Otanuomi."*"""
    return f"{number_word(n).capitalize()} responding to {system}."


def resolved(system: str) -> str:
    """*"Otanuomi clear."*"""
    return f"{system} clear."


def timer_set(system: str, duration_words: str) -> str:
    """*"Timer Kisogo, four hours."*"""
    return f"Timer {system}, {duration_words}."


def flood_control() -> str:
    """*"Flood control active."*"""
    return "Flood control active."


def degraded() -> str:
    """*"Voice offline, use slash commands."*"""
    return "Voice offline, use slash commands."


def registered(callsign: str) -> str:
    """*"Registered you as Space Junkie."*"""
    return f"Registered you as {callsign}."


def unregistered() -> str:
    """*"Unregistered."*"""
    return "Unregistered."


def not_registered() -> str:
    """*"You are not registered."*"""
    return "You are not registered."


def whoami(callsign: str) -> str:
    """*"You are Space Junkie."*"""
    return f"You are {callsign}."


def say_again_callsign() -> str:
    """*"Say again the callsign."* — REGISTER heard with no usable name."""
    return "Say again the callsign."


# ── WAV wrapping (in memory — constraint 5 adjacent: nothing touches disk) ───


def build_wav(pcm_s16le: bytes, sample_rate: int) -> bytes:
    """Prepend a canonical 44-byte RIFF/WAVE header to raw mono s16le samples.

    Pure bytes-in/bytes-out; no file objects, no disk. Symphonia on the Ears
    side parses this header and resamples to 48 kHz internally (GDD §12.3).
    """
    if len(pcm_s16le) % _BYTES_PER_SAMPLE != 0:
        raise ValueError(f"PCM byte count {len(pcm_s16le)} is not s16le-aligned")
    byte_rate = sample_rate * _CHANNELS * _BYTES_PER_SAMPLE
    block_align = _CHANNELS * _BYTES_PER_SAMPLE
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm_s16le),  # RIFF chunk size: rest of header + data
        b"WAVE",
        b"fmt ",
        16,  # fmt chunk size (PCM)
        1,  # audio format: PCM
        _CHANNELS,
        sample_rate,
        byte_rate,
        block_align,
        _BYTES_PER_SAMPLE * 8,  # bits per sample
        b"data",
        len(pcm_s16le),
    )
    return header + pcm_s16le


def read_voice_sample_rate(voice_path: str | Path) -> int:
    """Read the native sample rate from the Piper voice config JSON.

    Piper ships ``<name>.onnx`` + ``<name>.onnx.json``; the config carries
    ``audio.sample_rate``. Missing/invalid config falls back to
    :data:`DEFAULT_SAMPLE_RATE` with a warning — a wrong rate only makes the
    voice sound off-pitch, it must not stop the bot.
    """
    config_path = Path(f"{voice_path}.json")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        rate = data["audio"]["sample_rate"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning(
            "piper_voice_config_unreadable",
            path=str(config_path),
            error=str(exc),
            fallback=DEFAULT_SAMPLE_RATE,
        )
        return DEFAULT_SAMPLE_RATE
    if not isinstance(rate, int) or rate <= 0:
        log.warning(
            "piper_voice_config_bad_rate",
            path=str(config_path),
            rate=rate,
            fallback=DEFAULT_SAMPLE_RATE,
        )
        return DEFAULT_SAMPLE_RATE
    return rate


# ── Speaker ──────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _SayJob:
    text: str
    priority: int
    done: asyncio.Future[bool]


class Speaker:
    """Synthesises §12.1 utterances and ships them to Ears for playback.

    One :class:`Speaker` serves all guilds. Each guild gets its own FIFO queue
    and worker task so utterances within a guild play in order; the Piper
    subprocess itself is serialised globally (one synthesis at a time — the
    droplet's cores belong to Whisper).

    :meth:`say` returns ``True`` when the utterance was synthesised and handed
    to IPC, ``False`` when it was suppressed (TTS disabled, muted trigger
    user, synthesis failure, or over the length cap) — the caller then falls
    back to channel text.
    """

    def __init__(self, holder: ConfigHolder, ipc: IpcServer) -> None:
        self._holder = holder
        self._ipc = ipc
        # Cache the voice path with its rate so a SIGHUP voice swap refreshes
        # the rate before the next WAV header is built (see _speak).
        self._voice_path = holder.current.tts.voice
        self._sample_rate = read_voice_sample_rate(self._voice_path)
        self._synth_lock = asyncio.Lock()
        self._queues: dict[int, asyncio.Queue[_SayJob]] = {}
        self._workers: dict[int, asyncio.Task[None]] = {}
        self._closed = False
        self._voice_mutes: set[int] = set()

    @property
    def sample_rate(self) -> int:
        """The native output rate of the last-used voice (from ``.onnx.json``)."""
        return self._sample_rate

    # ── /mute-voice (GDD §12.2) ──────────────────────────────────────────────

    def set_voice_mutes(self, user_ids: set[int]) -> None:
        """Replace the muted-user set (loaded from the ``voice_mutes`` table)."""
        self._voice_mutes = set(user_ids)

    def set_muted(self, user_id: int, muted: bool) -> None:
        """Flip one user's ``/mute-voice`` state."""
        if muted:
            self._voice_mutes.add(user_id)
        else:
            self._voice_mutes.discard(user_id)

    def is_muted(self, user_id: int) -> bool:
        return user_id in self._voice_mutes

    # ── public API ───────────────────────────────────────────────────────────

    async def say(
        self,
        guild_id: int,
        text: str,
        priority: int = PRIORITY_NORMAL,
        *,
        user_id: int | None = None,
    ) -> bool:
        """Queue ``text`` for spoken playback in ``guild_id``.

        ``user_id`` is the pilot whose command triggered the reply; if they
        ran ``/mute-voice`` the utterance is suppressed for them. Returns
        ``True`` once the WAV has been sent to Ears, ``False`` when the speech
        was dropped for any reason — callers fall back to channel text.
        """
        if self._closed or not self._holder.current.tts.enabled:
            return False
        if user_id is not None and user_id in self._voice_mutes:
            log.debug("tts_suppressed_muted_user", user_id=user_id, guild_id=guild_id)
            return False
        job = _SayJob(text=text, priority=priority, done=asyncio.get_running_loop().create_future())
        self._queue_for(guild_id).put_nowait(job)
        return await job.done

    async def synthesize(self, text: str) -> bytes:
        """Run Piper once and return raw s16le PCM at the voice's native rate.

        Blocking process I/O rides asyncio's subprocess transport — nothing
        runs on the event loop thread itself. One synthesis at a time.
        Raises :class:`SynthesisError` on failure.
        """
        cfg = self._holder.current.tts
        async with self._synth_lock:
            try:
                proc = await asyncio.create_subprocess_exec(
                    cfg.binary,
                    "--model",
                    cfg.voice,
                    "--output-raw",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                raise SynthesisError(f"cannot exec piper binary {cfg.binary!r}: {exc}") from exc
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=text.encode("utf-8") + b"\n"),
                    timeout=SYNTHESIS_TIMEOUT_S,
                )
            except TimeoutError as exc:
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
                raise SynthesisError(f"piper timed out after {SYNTHESIS_TIMEOUT_S}s") from exc
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[:400]
            raise SynthesisError(f"piper exited {proc.returncode}: {detail}")
        if not stdout:
            raise SynthesisError("piper produced no audio")
        if len(stdout) % _BYTES_PER_SAMPLE != 0:
            stdout = stdout[: len(stdout) - (len(stdout) % _BYTES_PER_SAMPLE)]
        return stdout

    async def close(self) -> None:
        """Stop accepting work, drain nothing, cancel all guild workers."""
        self._closed = True
        workers = list(self._workers.values())
        for task in workers:
            task.cancel()
        for task in workers:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._workers.clear()
        for queue in self._queues.values():
            while not queue.empty():
                job = queue.get_nowait()
                if not job.done.done():
                    job.done.set_result(False)
        self._queues.clear()

    # ── internals ────────────────────────────────────────────────────────────

    def _queue_for(self, guild_id: int) -> asyncio.Queue[_SayJob]:
        queue = self._queues.get(guild_id)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[guild_id] = queue
            self._workers[guild_id] = asyncio.create_task(
                self._worker(guild_id, queue), name=f"tts-worker-{guild_id}"
            )
        return queue

    async def _worker(self, guild_id: int, queue: asyncio.Queue[_SayJob]) -> None:
        while True:
            job = await queue.get()
            try:
                spoken = await self._speak(guild_id, job)
            except asyncio.CancelledError:
                if not job.done.done():
                    job.done.set_result(False)
                raise
            except Exception:
                log.exception("tts_worker_error", guild_id=guild_id, text=job.text)
                spoken = False
            if not job.done.done():
                job.done.set_result(spoken)

    async def _speak(self, guild_id: int, job: _SayJob) -> bool:
        cfg = self._holder.current.tts
        if cfg.voice != self._voice_path:
            # SIGHUP swapped the voice model: refresh the cached rate so the
            # WAV header and duration check match the new voice. Safe to
            # mutate here — _speak runs only inside per-guild workers and
            # synthesis is serialised by _synth_lock.
            self._sample_rate = await asyncio.to_thread(read_voice_sample_rate, cfg.voice)
            self._voice_path = cfg.voice
        try:
            pcm = await self.synthesize(job.text)
        except SynthesisError as exc:
            log.warning("tts_synthesis_failed", guild_id=guild_id, text=job.text, error=str(exc))
            return False
        duration_s = len(pcm) / (self._sample_rate * _BYTES_PER_SAMPLE)
        if duration_s > cfg.max_utterance_s:
            # §12.2: hard cap — if it does not fit, it goes to the channel instead.
            log.info(
                "tts_over_cap_dropped",
                guild_id=guild_id,
                text=job.text,
                duration_s=round(duration_s, 2),
                cap_s=cfg.max_utterance_s,
            )
            return False
        await self._ipc.send_tts(guild_id, job.priority, build_wav(pcm, self._sample_rate))
        log.debug(
            "tts_sent",
            guild_id=guild_id,
            priority=job.priority,
            duration_s=round(duration_s, 2),
        )
        return True
