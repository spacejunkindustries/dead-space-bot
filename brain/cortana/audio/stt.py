"""Speech-to-text backends — GDD §5.3, §16 (stt block), §20 (watchdog).

Three :class:`Transcriber` implementations behind one protocol:

- :class:`FasterWhisperTranscriber` — the default. CTranslate2 releases the
  GIL during inference, so a blocking ``transcribe`` call in a worker thread
  does not stall the event loop.
- :class:`WhisperCppTranscriber` — posts a WAV built entirely in memory to a
  whisper.cpp HTTP server. No temp files, ever.
- :class:`TimeoutTranscriber` — the §20 "STT worker hang" watchdog *and* the
  admission control in front of it: ONE decode at a time through a bounded
  queue (five pilots reporting one gate camp produce queued decodes, never
  five concurrent Whisper runs on 2 vCPUs). When the queue is full the OLDEST
  queued job is dropped with :class:`SttError` — its pilot hears "say again"
  through the dialog machine's normal failure door. The watchdog deadline
  applies to **queue-head service time** only, so overload shows up as queue
  time and can never masquerade as a hang and trigger a respawn cascade.
  Consecutive respawns are capped; past the cap the wrapper latches
  ``degraded`` and refuses work instead of burning CPU on a broken backend.

Every class here is synchronous by design: callers run ``transcribe`` via
``asyncio.to_thread`` (docs/INTERFACES.md threading rules). Heavy dependencies
(faster_whisper, numpy) are imported lazily on first use, never at module
import, so unit tests run without them installed.

Audio is bytes in, text out — nothing in this module can touch disk
(CLAUDE.md constraint 5).
"""

from __future__ import annotations

import http.client
import io
import json
import threading
import urllib.error
import urllib.request
import uuid
import wave
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

import structlog

from cortana.audio.vad import SAMPLE_RATE_HZ
from cortana.config import SttConfig
from cortana.types import TranscriptResult

log = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_QUEUE_DEPTH",
    "DEFAULT_WATCHDOG_S",
    "MAX_CONSECUTIVE_RESPAWNS",
    "FasterWhisperTranscriber",
    "SttError",
    "SttTimeoutError",
    "TimeoutTranscriber",
    "Transcriber",
    "WhisperCppTranscriber",
    "make_transcriber",
    "pcm_to_wav_bytes",
]

#: STT watchdog deadline — GDD §20 "STT worker hang"; the default for
#: ``stt.watchdog_s`` (config.py). This is a *hang* backstop, not a latency
#: target: the model is preloaded at startup (see ``warm``), so a real
#: transcription of a few-second clip finishes in well under this even on a
#: 2-vCPU box. Set generously so slow-but-progressing decoding is never
#: mistaken for a hang and killed mid-flight (which, before preloading, spun
#: a reload→timeout→reload loop that never produced a word). The deadline
#: measures queue-head SERVICE time only — time spent waiting in the queue
#: under load never counts against it.
DEFAULT_WATCHDOG_S = 15.0

#: Jobs allowed to WAIT behind the in-service decode. Depth 3 covers the
#: headline five-pilots-one-gate-camp burst (one decoding + three queued);
#: past that the oldest queued job is dropped with :class:`SttError` so its
#: pilot is re-prompted instead of the whole fleet backing up.
DEFAULT_QUEUE_DEPTH = 3

#: Consecutive watchdog respawns before the wrapper latches ``degraded`` and
#: stops feeding a provably broken backend. A completed decode (success or a
#: clean backend error) resets the count — only back-to-back hangs latch.
MAX_CONSECUTIVE_RESPAWNS = 2

#: int16 full-scale divisor for the float32 conversion Whisper expects.
_INT16_FULL_SCALE = 32768.0

#: avg_logprob reported when a backend yields no segments / no confidence
#: signal. Deliberately the floor: downstream "sustained low confidence"
#: detection (GDD §20) must count an empty decode as low, never high.
_NO_CONFIDENCE_LOGPROB = -10.0

#: Hard character cap on the gazetteer bias prefix fed to Whisper. The base
#: model's decoder has 448 absolute-position encodings, and the bias is
#: prepended to every window; a wide `include_all` gazetteer prompt can run
#: into the thousands of characters and overflow that limit ("No position
#: encodings ... got position 449"), failing every decode. ~4 chars/token puts
#: 600 chars near 150 tokens — a comfortable slice of the budget with plenty of
#: room left for the audio tokens, whatever the configured prompt width.
_MAX_BIAS_CHARS = 600


class SttError(Exception):
    """A transcription attempt failed (backend error, bad response, ...)."""


class SttTimeoutError(SttError):
    """The watchdog deadline elapsed; the worker was abandoned and respawned."""


class Transcriber(Protocol):
    """Blocking STT backend (docs/INTERFACES.md). Call via ``asyncio.to_thread``."""

    def transcribe(self, pcm16k: bytes, bias: str) -> TranscriptResult:
        """Transcribe 16 kHz mono s16le PCM. ``bias`` is the gazetteer prompt text."""
        ...


def pcm_to_wav_bytes(pcm: bytes, sample_rate: int = SAMPLE_RATE_HZ) -> bytes:
    """Wrap raw s16le mono PCM in a WAV container, entirely in memory."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


class FasterWhisperTranscriber:
    """Default backend — faster-whisper (CTranslate2) on CPU, GDD §5.3.

    Model weights load lazily on the first call (and after a watchdog
    respawn), guarded by a lock so concurrent first calls cannot double-load.
    CTranslate2 releases the GIL while decoding, so this class parks cleanly
    in a thread pool.
    """

    def __init__(self, cfg: SttConfig) -> None:
        self._cfg = cfg
        self._model: Any = None
        self._model_lock = threading.Lock()
        self._transcribe_params_cache: frozenset[str] | None = None

    def _transcribe_params(self, model: Any) -> frozenset[str]:
        """The installed faster-whisper's transcribe() parameter names,
        probed once — drives graceful degradation of optional kwargs."""
        if self._transcribe_params_cache is None:
            import inspect

            try:
                self._transcribe_params_cache = frozenset(
                    inspect.signature(model.transcribe).parameters
                )
            except (TypeError, ValueError):  # pragma: no cover — exotic backends
                self._transcribe_params_cache = frozenset()
        return self._transcribe_params_cache

    def transcribe(self, pcm16k: bytes, bias: str) -> TranscriptResult:
        import numpy as np  # lazy

        model = self._get_model()
        audio = np.frombuffer(pcm16k, dtype=np.int16).astype(np.float32) / _INT16_FULL_SCALE
        kwargs: dict[str, Any] = {
            "language": "en",
            # Latency-critical decode settings for a 2-vCPU box (GDD §5.3):
            # the library defaults are beam_size=5 plus a six-step temperature
            # fallback ladder that re-decodes the whole clip whenever quality
            # thresholds fail — on noisy fleet audio that is 5-10x the wall
            # clock of one greedy pass, and it was the bulk of a measured
            # 20-30s voice→card latency. Greedy + no fallback keeps a 6s
            # utterance in low single-digit seconds; greedy decoding also
            # curbs the repeated-token hallucinations that fallback amplifies.
            "beam_size": 1,
            "temperature": 0.0,
            # Each utterance is independent — carrying decoder context across
            # segments only propagates a hallucination into the next one.
            "condition_on_previous_text": False,
            "without_timestamps": True,
            # Chatter defence (GDD §5.3): Silero VAD inside faster-whisper
            # trims non-speech from the clip before decoding. Fleet comms are
            # full of keyboard noise, breath, and game audio bleed — decoding
            # those spans is where "Rens, Rens, Rens" hallucinations grow.
            "vad_filter": True,
        }
        # Feature-probe the installed faster-whisper ONCE instead of
        # try/except TypeError around the call: vad_filter runs Silero
        # eagerly inside transcribe(), so a real TypeError from that path
        # would be indistinguishable from an unsupported-kwarg signature
        # error — and a blanket retry would silently disable the chatter
        # defence while hiding the actual bug.
        supported = self._transcribe_params(model)
        if "vad_filter" not in supported:
            log.warning("stt_kwarg_unsupported", kwarg="vad_filter")
            kwargs.pop("vad_filter", None)
        if self._cfg.bias_with_gazetteer and bias:
            # Gazetteer biasing (GDD §5.3) through EXACTLY ONE channel — never
            # both. faster-whisper prepends BOTH initial_prompt AND hotwords to
            # the decoder; passing the (long) bias in both doubled the prefix
            # past the model's 448-token position limit ("No position encodings
            # ... got position 449"), which failed EVERY decode (live incident,
            # base model). hotwords is the better mechanism — it biases every
            # window, not just the first — so prefer it and fall back to
            # initial_prompt only where the backend lacks it. The hard char cap
            # keeps even one channel comfortably inside the token budget
            # regardless of how wide the gazetteer prompt is configured.
            clipped = bias[:_MAX_BIAS_CHARS]
            if "hotwords" in supported:
                kwargs["hotwords"] = clipped
            else:
                kwargs["initial_prompt"] = clipped
        segments, _info = model.transcribe(audio, **kwargs)

        texts: list[str] = []
        logprobs: list[float] = []
        for segment in segments:  # generator: decoding happens here
            stripped = segment.text.strip()
            if stripped:
                texts.append(stripped)
            logprobs.append(float(segment.avg_logprob))
        avg = sum(logprobs) / len(logprobs) if logprobs else _NO_CONFIDENCE_LOGPROB
        return TranscriptResult(text=" ".join(texts), avg_logprob=avg)

    def _get_model(self) -> Any:
        with self._model_lock:
            if self._model is None:
                from faster_whisper import WhisperModel  # lazy

                log.info(
                    "stt_model_loading",
                    model=self._cfg.model,
                    compute_type=self._cfg.compute_type,
                    cpu_threads=self._cfg.cpu_threads,
                )
                self._model = WhisperModel(
                    self._cfg.model,
                    device="cpu",
                    compute_type=self._cfg.compute_type,
                    cpu_threads=self._cfg.cpu_threads,
                )
            return self._model

    def warm(self) -> None:
        """Load the model now, off the request path.

        Called once at startup (via a thread) so the first real utterance does
        not pay the multi-second model-load *inside* the watchdog window."""
        self._get_model()

    def close(self) -> None:
        """Drop the loaded model so a respawned instance starts clean."""
        with self._model_lock:
            self._model = None


class WhisperCppTranscriber:
    """Alternative backend — whisper.cpp's HTTP server (GDD §5.3).

    Builds the WAV in memory and POSTs it as multipart/form-data to
    ``stt.whisper_cpp_url`` with urllib (stdlib; the class is sync and runs in
    a worker thread, so no async HTTP client is needed). The server's JSON
    response carries text only — it exposes no decoder log-probabilities — so
    ``avg_logprob`` is the no-confidence floor and downstream confidence logic
    leans on the resolution score instead.
    """

    def __init__(self, cfg: SttConfig, timeout_s: float = DEFAULT_WATCHDOG_S) -> None:
        self._cfg = cfg
        self._timeout_s = timeout_s

    def transcribe(self, pcm16k: bytes, bias: str) -> TranscriptResult:
        fields: dict[str, str] = {"response_format": "json"}
        if self._cfg.bias_with_gazetteer and bias:
            # Same 448-position budget as the faster-whisper path: a wide
            # `include_all` prompt overflows the base model here too. One
            # channel, hard-capped (see _MAX_BIAS_CHARS).
            fields["prompt"] = bias[:_MAX_BIAS_CHARS]
        body, content_type = _encode_multipart(
            fields,
            file_field="file",
            file_name="utterance.wav",
            file_bytes=pcm_to_wav_bytes(pcm16k),
        )
        request = urllib.request.Request(
            self._cfg.whisper_cpp_url,
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # OSError covers URLError, TimeoutError, and ConnectionResetError;
            # HTTPException covers what urllib does NOT wrap — a server drop
            # mid-request (deploy/OOM restart of whisper.cpp) escapes
            # getresponse()/read() as raw RemoteDisconnected, IncompleteRead,
            # or BadStatusLine. ValueError covers JSONDecodeError. Everything
            # must surface as the clean SttError the caller contract promises
            # (dialog "say again"), never a raw transport exception.
            raise SttError(f"whisper.cpp request failed: {exc}") from exc
        text = str(payload.get("text", "")).strip()
        return TranscriptResult(text=text, avg_logprob=_NO_CONFIDENCE_LOGPROB)


def _encode_multipart(
    fields: dict[str, str], *, file_field: str, file_name: str, file_bytes: bytes
) -> tuple[bytes, str]:
    """Encode a multipart/form-data body in memory. ``file_name`` is a form
    label only — no file exists anywhere."""
    boundary = uuid.uuid4().hex
    buf = io.BytesIO()
    for name, value in fields.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        buf.write(f"{value}\r\n".encode())
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'.encode()
    )
    buf.write(b"Content-Type: audio/wav\r\n\r\n")
    buf.write(file_bytes)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


class _Job:
    """One queued transcription request; the caller blocks on ``done``."""

    __slots__ = ("bias", "done", "error", "pcm", "result", "started")

    def __init__(self, pcm: bytes, bias: str) -> None:
        self.pcm = pcm
        self.bias = bias
        self.started = threading.Event()  # set when the worker begins THIS job
        self.done = threading.Event()
        self.result: TranscriptResult | None = None
        self.error: BaseException | None = None

    def fail(self, error: BaseException) -> None:
        self.error = error
        self.pcm = b""  # constraint 5: drop the audio the moment it is dead
        self.done.set()
        self.started.set()  # wake a caller still queued behind the head job


class TimeoutTranscriber:
    """Bounded serialized STT worker with the §20 hang watchdog.

    All calls funnel through ONE decode at a time (Whisper is the heaviest
    workload on the droplet; Piper is already serialised in ``tts.py``).
    Callers — each an ``asyncio.to_thread`` worker — enqueue a job and block
    on its completion:

    - **Bounded queue (drop-oldest).** At most ``queue_depth`` jobs wait
      behind the in-service decode. Overflow drops the OLDEST queued job with
      ``SttError("stt queue overflow")`` so its caller re-prompts through the
      dialog machine's normal fail path; the freshest audio keeps its place.
    - **Watchdog on queue-head service time.** The ``timeout_s`` deadline
      starts when the worker picks the job up, never at enqueue — a decode
      that waited out three predecessors is not a hang. On a genuine hang the
      worker thread is abandoned (Python cannot kill a thread), the inner
      backend is rebuilt via the factory, and :class:`SttTimeoutError`
      propagates so the pilot hears "say again".
    - **Respawn cap → degraded latch.** More than
      :data:`MAX_CONSECUTIVE_RESPAWNS` back-to-back watchdog fires latch
      ``degraded``: further calls raise :class:`SttError` immediately instead
      of burning CPU on a provably broken backend, until
      :meth:`reset_degraded` (a future ``/reload``) or a restart. The latch
      is logged loudly exactly once.

    Worker threads are daemons on purpose: a truly hung C-level call must not
    wedge process shutdown — systemd's restart (GDD §17.2) stays clean.
    """

    def __init__(
        self,
        factory: Callable[[], Transcriber],
        timeout_s: float = DEFAULT_WATCHDOG_S,
        queue_depth: int = DEFAULT_QUEUE_DEPTH,
    ) -> None:
        self._factory = factory
        self._timeout_s = timeout_s
        self._queue_depth = queue_depth
        self._lock = threading.Lock()
        self._work_ready = threading.Condition(self._lock)
        self._queue: deque[_Job] = deque()
        self._inner: Transcriber = factory()
        self._generation = 0
        self._worker: threading.Thread | None = None
        self._consecutive_timeouts = 0
        self._degraded = False
        self._closed = False

    # ── state the rest of the system reads ───────────────────────────────────

    @property
    def inner(self) -> Transcriber:
        """The currently live backend (rebuilt after each watchdog fire)."""
        with self._lock:
            return self._inner

    @property
    def degraded(self) -> bool:
        """True once the respawn cap latched; cleared only by
        :meth:`reset_degraded` or a process restart."""
        with self._lock:
            return self._degraded

    def reset_degraded(self) -> None:
        """Clear the degraded latch and stand up a fresh backend (``/reload``)."""
        with self._work_ready:
            if not self._degraded:
                return
            self._degraded = False
            self._consecutive_timeouts = 0
            self._generation += 1
            self._worker = None
            self._inner = self._factory()
            fresh = self._inner
        log.info("stt_degraded_cleared")
        self._warm_off_path(fresh)

    # ── public API (Transcriber protocol) ────────────────────────────────────

    def warm(self) -> None:
        """Preload the backend model, outside the per-call watchdog window."""
        inner_warm = getattr(self.inner, "warm", None)
        if callable(inner_warm):
            inner_warm()

    def close(self) -> None:
        """Stop accepting work, fail everything queued, release the backend."""
        with self._work_ready:
            self._closed = True
            while self._queue:
                self._queue.popleft().fail(SttError("transcriber closed"))
            self._work_ready.notify_all()
            old = self._inner
        # Off-thread for the same reason as _watchdog_fired: close() may block
        # on the model lock a hung decode still holds.
        close = getattr(old, "close", None)
        if callable(close):
            threading.Thread(target=close, name="aura-stt-close", daemon=True).start()

    def transcribe(self, pcm16k: bytes, bias: str) -> TranscriptResult:
        job = _Job(pcm16k, bias)
        with self._work_ready:
            if self._degraded:
                raise SttError(
                    "stt degraded: watchdog respawn cap reached; reload or restart to recover"
                )
            if self._closed:
                raise SttError("transcriber closed")
            if len(self._queue) >= self._queue_depth:
                # Drop-oldest: the stalest audio is the least likely to still
                # matter mid-fight, and its pilot gets re-prompted normally.
                self._queue.popleft().fail(SttError("stt queue overflow"))
                log.warning("stt_queue_overflow", depth=self._queue_depth)
            self._queue.append(job)
            self._ensure_worker_locked()
            self._work_ready.notify_all()
        # Queue wait — bounded by the PREDECESSORS' watchdogs (every in-service
        # job has its own caller enforcing timeout_s on it), so overload shows
        # up here, in queue time, and is never charged against this job's
        # deadline. The failsafe below can only fire on an internal bug.
        if not job.started.wait(self._timeout_s * (self._queue_depth + 1) + 5.0):
            with self._work_ready:
                if job in self._queue:
                    self._queue.remove(job)
            if not job.done.is_set():  # pragma: no cover — worker-loss failsafe
                raise SttError("stt worker never picked the job up")
        elif not job.done.wait(self._timeout_s) and self._watchdog_fired(job):
            raise SttTimeoutError(
                f"transcription exceeded the {self._timeout_s}s watchdog; worker respawned"
            )
        if job.error is not None:
            raise job.error
        result = job.result
        assert result is not None  # done + no error ⇒ result was set
        return result

    # ── internals ────────────────────────────────────────────────────────────

    def _ensure_worker_locked(self) -> None:
        """Start the (single) worker thread if none is serving. Lock held."""
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._worker_loop,
                args=(self._generation, self._inner),
                name="aura-stt-worker",
                daemon=True,
            )
            self._worker.start()

    def _worker_loop(self, generation: int, inner: Transcriber) -> None:
        """Serve jobs one at a time until closed or abandoned by a respawn."""
        # Load the model BEFORE picking up the first job. The caller's
        # watchdog clock starts at job.started, so a lazy model load paid
        # inside the first service window reads as a hang — after a watchdog
        # respawn with jobs still queued, that false timeout cascades
        # (respawn → reload inside the next window → respawn …) straight
        # into the degraded latch. Warming here keeps load time out of every
        # service-time measurement; a warm failure is swallowed because the
        # decode itself surfaces the real error to the caller.
        warm = getattr(inner, "warm", None)
        if callable(warm):
            try:
                warm()
            except Exception:  # noqa: BLE001 — the decode reports the real error
                log.exception("stt_worker_warm_failed")
        while True:
            with self._work_ready:
                while generation == self._generation and not self._closed and not self._queue:
                    self._work_ready.wait()
                if generation != self._generation or self._closed:
                    return
                job = self._queue.popleft()
            job.started.set()  # the caller's watchdog clock starts HERE
            try:
                job.result = inner.transcribe(job.pcm, job.bias)
            except BaseException as exc:  # noqa: BLE001 — re-raised on the caller thread
                job.error = exc
            job.pcm = b""  # constraint 5: release the audio buffer immediately
            job.done.set()
            with self._lock:
                if generation != self._generation:
                    return  # abandoned mid-decode by a watchdog respawn
                # A completed service — even a backend error — proves the
                # worker is alive: only back-to-back HANGS may latch.
                self._consecutive_timeouts = 0

    def _watchdog_fired(self, job: _Job) -> bool:
        """The caller's service deadline elapsed. Returns False when the
        decode actually finished in the race window (result is used)."""
        with self._work_ready:
            if job.done.is_set():
                return False
            old = self._inner
            self._generation += 1  # abandon the wedged worker thread
            self._worker = None
            self._consecutive_timeouts += 1
            log.warning(
                "stt_watchdog_fired",
                timeout_s=self._timeout_s,
                consecutive=self._consecutive_timeouts,
            )
            if self._consecutive_timeouts > MAX_CONSECUTIVE_RESPAWNS:
                # The backend hangs faster than we can respawn it: latch and
                # refuse work instead of reloading a ~GB model every deadline.
                self._degraded = True
                while self._queue:
                    self._queue.popleft().fail(
                        SttError("stt degraded: watchdog respawn cap reached")
                    )
                log.error(
                    "stt_degraded_latched",
                    respawns=MAX_CONSECUTIVE_RESPAWNS,
                    detail="STT disabled until /reload or restart; "
                    "voice commands will be rejected, slash commands unaffected",
                )
            else:
                # The fresh worker warms the new backend itself, before it
                # picks up the first queued job (_worker_loop) — queued jobs
                # never pay the model reload inside their watchdog window.
                self._inner = self._factory()
                self._ensure_worker_locked()
                self._work_ready.notify_all()
        # Close the wedged backend OUTSIDE the lock and on its own thread:
        # FasterWhisperTranscriber.close() takes the model lock, which the
        # hung worker may hold for the duration of a stalled load — closing
        # inline under the lock would deadlock every future transcribe().
        close = getattr(old, "close", None)
        if callable(close):
            threading.Thread(target=close, name="aura-stt-close", daemon=True).start()
        return True

    @staticmethod
    def _warm_off_path(backend: Transcriber) -> None:
        """Re-warm a fresh backend OFF the request path (used by
        :meth:`reset_degraded`, which stands up a backend without starting a
        worker): without this, the next utterance pays the multi-second model
        reload before its decode — the reload→timeout→reload spiral the
        startup ``warm()`` exists to prevent (GDD §20)."""
        warm = getattr(backend, "warm", None)
        if callable(warm):
            threading.Thread(target=warm, name="aura-stt-rewarm", daemon=True).start()


#: The whisper-cpp HTTP timeout runs at this fraction of ``stt.watchdog_s``
#: so the socket gives up (raising a clean :class:`SttError`) slightly BEFORE
#: the outer watchdog abandons the worker thread — the two deadlines used to
#: race at exactly the same instant.
_HTTP_TIMEOUT_FRACTION = 0.9


def make_transcriber(cfg: SttConfig) -> Transcriber:
    """Build the configured backend (GDD §16 ``stt.backend``) behind the
    bounded queue + watchdog, with ``stt.watchdog_s`` threaded through."""

    def factory() -> Transcriber:
        if cfg.backend == "whisper-cpp":
            # The HTTP socket gives up slightly BEFORE the watchdog: a slow
            # server surfaces as a clean SttError (no respawn burned) instead
            # of racing the hang detector at exactly the same deadline.
            return WhisperCppTranscriber(cfg, timeout_s=max(1.0, cfg.watchdog_s - 1.0))
        return FasterWhisperTranscriber(cfg)

    return TimeoutTranscriber(factory, timeout_s=cfg.watchdog_s)
