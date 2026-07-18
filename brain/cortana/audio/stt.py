"""Speech-to-text backends — GDD §5.3, §16 (stt block), §20 (watchdog).

Three :class:`Transcriber` implementations behind one protocol:

- :class:`FasterWhisperTranscriber` — the default. CTranslate2 releases the
  GIL during inference, so a blocking ``transcribe`` call in a worker thread
  does not stall the event loop.
- :class:`WhisperCppTranscriber` — posts a WAV built entirely in memory to a
  whisper.cpp HTTP server. No temp files, ever.
- :class:`TimeoutTranscriber` — the §20 "STT worker hang" watchdog. Wraps
  either backend; a call that exceeds the deadline is abandoned and the inner
  transcriber is rebuilt so the next utterance gets a fresh worker.

Every class here is synchronous by design: callers run ``transcribe`` via
``asyncio.to_thread`` (docs/INTERFACES.md threading rules). Heavy dependencies
(faster_whisper, numpy) are imported lazily on first use, never at module
import, so unit tests run without them installed.

Audio is bytes in, text out — nothing in this module can touch disk
(CLAUDE.md constraint 5).
"""

from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
import uuid
import wave
from collections.abc import Callable
from typing import Any, Protocol

import structlog

from cortana.audio.vad import SAMPLE_RATE_HZ
from cortana.config import SttConfig
from cortana.types import TranscriptResult

log = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_WATCHDOG_S",
    "FasterWhisperTranscriber",
    "SttError",
    "SttTimeoutError",
    "TimeoutTranscriber",
    "Transcriber",
    "WhisperCppTranscriber",
    "make_transcriber",
    "pcm_to_wav_bytes",
]

#: STT watchdog deadline — GDD §20 "STT worker hang". This is a *hang*
#: backstop, not a latency target: the model is preloaded at startup (see
#: ``warm``), so a real transcription of a few-second clip finishes in well
#: under this even on a 2-vCPU box. Set generously so slow-but-progressing
#: decoding is never mistaken for a hang and killed mid-flight (which, before
#: preloading, spun a reload→timeout→reload loop that never produced a word).
DEFAULT_WATCHDOG_S = 15.0

#: int16 full-scale divisor for the float32 conversion Whisper expects.
_INT16_FULL_SCALE = 32768.0

#: avg_logprob reported when a backend yields no segments / no confidence
#: signal. Deliberately the floor: downstream "sustained low confidence"
#: detection (GDD §20) must count an empty decode as low, never high.
_NO_CONFIDENCE_LOGPROB = -10.0


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
        if self._cfg.bias_with_gazetteer and bias:
            kwargs["initial_prompt"] = bias  # gazetteer biasing, GDD §5.3
            # hotwords bias the decoder toward these tokens on EVERY window
            # (initial_prompt only prefixes the first) — helps short system
            # names survive noisy audio.
        # Feature-probe the installed faster-whisper ONCE instead of
        # try/except TypeError around the call: vad_filter runs Silero
        # eagerly inside transcribe(), so a real TypeError from that path
        # would be indistinguishable from an unsupported-kwarg signature
        # error — and a blanket retry would silently disable the chatter
        # defence while hiding the actual bug.
        supported = self._transcribe_params(model)
        for optional in ("vad_filter", "hotwords"):
            if optional in kwargs and optional not in supported:
                log.warning("stt_kwarg_unsupported", kwarg=optional)
                del kwargs[optional]
        if self._cfg.bias_with_gazetteer and bias and "hotwords" in supported:
            kwargs["hotwords"] = bias
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
            fields["prompt"] = bias
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
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
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


class TimeoutTranscriber:
    """Watchdog wrapper — GDD §20 "STT worker hang: 5s watchdog, kill, respawn".

    Each call runs the inner transcriber on a fresh *daemon* worker thread and
    waits at most ``timeout_s``. On timeout the worker is abandoned (Python
    cannot forcibly kill a thread) and the inner transcriber is rebuilt via
    the injected factory, so the next utterance gets a clean worker and — for
    faster-whisper — a freshly loaded model. :class:`SttTimeoutError`
    propagates to the caller, which answers the pilot with "say again".

    Daemon threads are deliberate: ``concurrent.futures`` workers are
    non-daemon and joined at interpreter exit, so one truly hung C-level call
    would wedge process shutdown. A daemon thread cannot — systemd's restart
    (GDD §17.2) stays clean.
    """

    def __init__(
        self, factory: Callable[[], Transcriber], timeout_s: float = DEFAULT_WATCHDOG_S
    ) -> None:
        self._factory = factory
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._generation = 0
        self._inner = factory()

    @property
    def inner(self) -> Transcriber:
        """The currently live backend (rebuilt after each watchdog fire)."""
        with self._lock:
            return self._inner

    def warm(self) -> None:
        """Preload the backend model, outside the per-call watchdog window."""
        inner_warm = getattr(self.inner, "warm", None)
        if callable(inner_warm):
            inner_warm()

    def transcribe(self, pcm16k: bytes, bias: str) -> TranscriptResult:
        with self._lock:
            inner = self._inner
            generation = self._generation
        done = threading.Event()
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["result"] = inner.transcribe(pcm16k, bias)
            except BaseException as exc:  # noqa: BLE001 — re-raised on the caller thread
                box["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(target=run, name="aura-stt-worker", daemon=True)
        worker.start()
        if not done.wait(self._timeout_s):
            self._respawn(generation)
            raise SttTimeoutError(
                f"transcription exceeded the {self._timeout_s}s watchdog; worker respawned"
            )
        error = box.get("error")
        if error is not None:
            raise error
        result: TranscriptResult = box["result"]
        return result

    def _respawn(self, stale_generation: int) -> None:
        with self._lock:
            if self._generation != stale_generation:
                return  # a concurrent timeout already respawned
            log.warning("stt_watchdog_fired", timeout_s=self._timeout_s)
            old = self._inner
            self._inner = self._factory()
            self._generation += 1
            fresh = self._inner
        # Close the wedged backend OUTSIDE self._lock and on its own thread:
        # FasterWhisperTranscriber.close() takes the model lock, which the
        # hung worker may hold for the duration of a stalled load — closing
        # inline under self._lock would deadlock every future transcribe()
        # at its lock acquisition, wedging all voice commands permanently.
        close = getattr(old, "close", None)
        if callable(close):
            threading.Thread(target=close, name="aura-stt-close", daemon=True).start()
        # Re-warm the respawned backend OFF the request path: without this,
        # the very next utterance pays the multi-second model reload inside
        # its own watchdog window — the reload→timeout→reload spiral the
        # startup warm() exists to prevent (GDD §20).
        warm = getattr(fresh, "warm", None)
        if callable(warm):
            threading.Thread(target=warm, name="aura-stt-rewarm", daemon=True).start()


def make_transcriber(cfg: SttConfig) -> Transcriber:
    """Build the configured backend (GDD §16 ``stt.backend``) behind the watchdog."""

    def factory() -> Transcriber:
        if cfg.backend == "whisper-cpp":
            return WhisperCppTranscriber(cfg)
        return FasterWhisperTranscriber(cfg)

    return TimeoutTranscriber(factory, timeout_s=DEFAULT_WATCHDOG_S)
