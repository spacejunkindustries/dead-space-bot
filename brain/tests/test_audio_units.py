"""Unit tests for the audio front-end pieces: endpointing, wake scoring
mechanics, and the STT backends/watchdog — GDD §5, §16, §20.

Runs without the heavy audio deps (webrtcvad, openwakeword, faster_whisper,
numpy): native engines are faked at the seam each class exposes. All PCM is
synthetic bytes in RAM; nothing here touches disk (CLAUDE.md constraint 5).
"""

from __future__ import annotations

import io
import json
import struct
import sys
import threading
import types
import urllib.error
import wave
from typing import Any

import pytest

from cortana.audio.stt import (
    DEFAULT_WATCHDOG_S,
    FasterWhisperTranscriber,
    SttError,
    SttTimeoutError,
    TimeoutTranscriber,
    Transcriber,
    WhisperCppTranscriber,
    make_transcriber,
    pcm_to_wav_bytes,
)
from cortana.audio.vad import (
    FRAME_BYTES,
    FRAME_MS,
    SAMPLE_RATE_HZ,
    SAMPLES_PER_FRAME,
    EndpointTracker,
    VadGate,
)
from cortana.audio.wake import OWW_CHUNK_BYTES, OpenWakeWordDetector
from cortana.config import SttConfig, WakeConfig
from cortana.types import TranscriptResult

# ── shared fixtures ──────────────────────────────────────────────────────────

USER = 7


def frame_of(value: int) -> bytes:
    return struct.pack("<h", value) * SAMPLES_PER_FRAME


FRAME = frame_of(1000)


class _StubHolder:
    """Duck-typed ConfigHolder exposing just ``.current.wake``."""

    def __init__(self, wake: WakeConfig) -> None:
        self.current = types.SimpleNamespace(wake=wake)


def make_wake_config(threshold: float = 0.5, refractory_ms: int = 200) -> WakeConfig:
    return WakeConfig(
        model="/opt/cortana/models/wake/aura_command.onnx",
        threshold=threshold,
        refractory_ms=refractory_ms,
    )


def make_wake_holder(threshold: float = 0.5, refractory_ms: int = 200) -> _StubHolder:
    return _StubHolder(make_wake_config(threshold, refractory_ms))


def make_stt_config(backend: str = "faster-whisper", bias_with_gazetteer: bool = True) -> SttConfig:
    return SttConfig(
        backend=backend,
        model="small",
        compute_type="int8",
        cpu_threads=2,
        bias_with_gazetteer=bias_with_gazetteer,
        whisper_cpp_url="http://127.0.0.1:8080/inference",
    )


# ── frame constants (GDD §15 wire format) ────────────────────────────────────


def test_frame_constants_match_the_wire_format() -> None:
    assert SAMPLE_RATE_HZ == 16_000
    assert FRAME_MS == 20
    assert SAMPLES_PER_FRAME == 320
    assert FRAME_BYTES == 640  # 20ms of 16kHz mono s16le


# ── EndpointTracker ──────────────────────────────────────────────────────────


def test_endpoint_reached_after_consecutive_silence() -> None:
    tracker = EndpointTracker(silence_ms=400)
    tracker.update(True)  # speech must be seen before silence can endpoint
    for _ in range(19):
        assert not tracker.update(False)
    assert tracker.update(False)  # 20th silent frame = 400ms


def test_leading_silence_never_endpoints() -> None:
    # A pilot waiting for the "go ahead" cue opens the capture with silence;
    # that must not endpoint before a word is spoken.
    tracker = EndpointTracker(silence_ms=400)
    for _ in range(200):  # 4s of pure leading silence
        assert not tracker.update(False)
    tracker.update(True)  # now they speak
    for _ in range(19):
        assert not tracker.update(False)
    assert tracker.update(False)  # only now, after speech, does silence end it


def test_speech_resets_the_silence_run() -> None:
    tracker = EndpointTracker(silence_ms=400)
    for _ in range(19):
        tracker.update(False)
    assert not tracker.update(True)  # speech: run broken
    assert tracker.silence_ms == 0
    for _ in range(19):
        assert not tracker.update(False)
    assert tracker.update(False)


def test_tracker_reset_and_silence_ms() -> None:
    tracker = EndpointTracker(silence_ms=400)
    tracker.update(False)
    tracker.update(False)
    assert tracker.silence_ms == 2 * FRAME_MS
    tracker.reset()
    assert tracker.silence_ms == 0


def test_tracker_rejects_nonpositive_configuration() -> None:
    with pytest.raises(ValueError):
        EndpointTracker(silence_ms=0)
    with pytest.raises(ValueError):
        EndpointTracker(silence_ms=400, frame_ms=0)


# ── VadGate (webrtcvad faked at the import seam) ─────────────────────────────


class _FakeNativeVad:
    def __init__(self, aggressiveness: int) -> None:
        self.aggressiveness = aggressiveness
        self.calls: list[tuple[bytes, int]] = []

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        self.calls.append((frame, sample_rate))
        (sample,) = struct.unpack_from("<h", frame)
        return abs(sample) > 500


@pytest.fixture
def fake_webrtcvad(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("webrtcvad")
    module.Vad = _FakeNativeVad  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "webrtcvad", module)
    return module


def test_vad_gate_rejects_bad_aggressiveness_before_importing_webrtcvad() -> None:
    # Validation precedes the lazy import: no webrtcvad needed to see the error.
    with pytest.raises(ValueError):
        VadGate(4)
    with pytest.raises(ValueError):
        VadGate(-1)


def test_vad_gate_classifies_frames(fake_webrtcvad: types.ModuleType) -> None:
    gate = VadGate(2)
    assert gate.aggressiveness == 2
    assert gate.is_speech(frame_of(1000)) is True
    assert gate.is_speech(frame_of(0)) is False


def test_vad_gate_passes_sample_rate_and_rejects_bad_frames(
    fake_webrtcvad: types.ModuleType,
) -> None:
    gate = VadGate(3)
    gate.is_speech(FRAME)
    native = gate._vad  # type: ignore[attr-defined]
    assert native.calls == [(FRAME, SAMPLE_RATE_HZ)]
    with pytest.raises(ValueError):
        gate.is_speech(FRAME + b"\x00\x00")


# ── OpenWakeWordDetector (model faked at the _predict_chunk seam) ────────────


class _ScriptedPredict:
    """Stands in for the per-user openWakeWord model call."""

    def __init__(self, scores: list[float]) -> None:
        self.scores = scores
        self.chunks: list[bytes] = []

    def __call__(self, state: Any, chunk: bytes) -> float:
        self.chunks.append(chunk)
        return self.scores.pop(0) if self.scores else 0.0


def make_detector(
    scores: list[float], threshold: float = 0.5, refractory_ms: int = 200
) -> tuple[OpenWakeWordDetector, _ScriptedPredict]:
    detector = OpenWakeWordDetector(make_wake_holder(threshold, refractory_ms))  # type: ignore[arg-type]
    predict = _ScriptedPredict(scores)
    detector._predict_chunk = predict  # type: ignore[method-assign]
    return detector, predict


def test_frames_accumulate_to_one_oww_chunk() -> None:
    detector, predict = make_detector([0.2])
    # openWakeWord's chunk is 80ms = four 20ms frames.
    for _ in range(3):
        assert detector.score(USER, FRAME) == 0.0
    assert predict.chunks == []
    assert detector.score(USER, FRAME) == pytest.approx(0.2)
    assert len(predict.chunks) == 1
    assert len(predict.chunks[0]) == OWW_CHUNK_BYTES


def test_hit_clears_streaming_state_but_not_scoring() -> None:
    # Refractory suppression is the CaptureManager's job (single owner);
    # a hit only clears pending bytes + held score + model buffers so the
    # same utterance's tail cannot retrigger from a held score.
    class _ResettableModel:
        def __init__(self) -> None:
            self.resets = 0

        def reset(self) -> None:
            self.resets += 1

    detector, predict = make_detector([0.9, 0.9])
    for _ in range(3):
        detector.score(USER, FRAME)  # accumulate a partial chunk
    state = detector._states[USER]
    model = _ResettableModel()
    state.model = model

    assert detector.score(USER, FRAME) == pytest.approx(0.9)  # chunk complete: hit
    assert state.last_score == 0.0  # held score cleared: the tail cannot retrigger
    assert state.pending == bytearray()  # partial-chunk residue discarded
    assert model.resets == 1  # model prediction buffer cleared

    # Scoring stays live immediately: the next full chunk can hit again.
    rearmed = [detector.score(USER, FRAME) for _ in range(4)]
    assert rearmed[-1] == pytest.approx(0.9)
    assert len(predict.chunks) == 2


def test_subthreshold_score_is_reported_but_does_not_arm_refractory() -> None:
    detector, _ = make_detector([0.3, 0.4], threshold=0.5)
    for _ in range(3):
        detector.score(USER, FRAME)
    assert detector.score(USER, FRAME) == pytest.approx(0.3)
    # Still live: the very next chunk scores again.
    for _ in range(3):
        assert detector.score(USER, FRAME) == pytest.approx(0.3)  # holds last score
    assert detector.score(USER, FRAME) == pytest.approx(0.4)


def test_wake_state_is_per_user_and_reset_purges_it() -> None:
    detector, predict = make_detector([0.9, 0.1])
    other_user = USER + 1
    for _ in range(3):
        detector.score(USER, FRAME)
        detector.score(other_user, FRAME)
    assert detector.score(USER, FRAME) == pytest.approx(0.9)  # USER hits...
    assert detector.score(other_user, FRAME) == pytest.approx(0.1)  # ...B unaffected
    assert detector.score(USER, FRAME) == 0.0  # USER's held score was cleared by the hit

    detector.reset(USER)  # purge (leave/opt-out)
    for _ in range(3):
        assert detector.score(USER, FRAME) == 0.0  # fresh accumulation from zero


def test_wake_detector_rejects_bad_frame_size() -> None:
    detector, _ = make_detector([])
    with pytest.raises(ValueError):
        detector.score(USER, b"\x00" * 10)


def _emit_one_chunk(detector: OpenWakeWordDetector) -> None:
    """Feed exactly four 20 ms frames so one 80 ms chunk is scored."""
    for _ in range(4):
        detector.score(USER, FRAME)


def test_near_miss_above_floor_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    import cortana.audio.wake as wake_mod

    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        wake_mod.log, "info", lambda event, **kw: events.append((event, kw)), raising=False
    )
    detector, _ = make_detector([0.42], threshold=0.55)
    _emit_one_chunk(detector)

    near = [kw for event, kw in events if event == "wake_near_miss"]
    assert near and near[0]["score"] == pytest.approx(0.42)
    assert near[0]["threshold"] == 0.55
    assert not any(event == "wake_hit" for event, _ in events)


def test_quiet_audio_below_floor_logs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import cortana.audio.wake as wake_mod

    events: list[str] = []
    monkeypatch.setattr(
        wake_mod.log, "info", lambda event, **kw: events.append(event), raising=False
    )
    detector, _ = make_detector([0.05], threshold=0.55)
    _emit_one_chunk(detector)

    assert "wake_near_miss" not in events


# ── pcm_to_wav_bytes ─────────────────────────────────────────────────────────


def test_wav_wrapping_roundtrips_in_memory() -> None:
    pcm = frame_of(1234) * 5
    wav_bytes = pcm_to_wav_bytes(pcm)
    assert wav_bytes.startswith(b"RIFF")
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == SAMPLE_RATE_HZ
        assert wav.readframes(wav.getnframes()) == pcm


# ── WhisperCppTranscriber (HTTP faked; WAV stays in memory) ──────────────────


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_whisper_cpp_posts_in_memory_wav_and_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        captured["url"] = request.full_url
        captured["content_type"] = request.get_header("Content-type")
        captured["body"] = request.data
        captured["timeout"] = timeout
        return _FakeResponse({"text": "  hostiles otanuomi  "})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    transcriber = WhisperCppTranscriber(make_stt_config(backend="whisper-cpp"))
    pcm = frame_of(2000) * 10
    result = transcriber.transcribe(pcm, "Otanuomi Kisogo Jita")

    assert result.text == "hostiles otanuomi"
    assert result.avg_logprob < 0  # no-confidence floor, never "high confidence"
    assert captured["url"] == "http://127.0.0.1:8080/inference"
    assert captured["content_type"].startswith("multipart/form-data; boundary=")
    body: bytes = captured["body"]
    assert pcm_to_wav_bytes(pcm) in body  # the WAV bytes, straight from memory
    assert b'name="prompt"' in body and b"Otanuomi Kisogo Jita" in body
    assert captured["timeout"] == DEFAULT_WATCHDOG_S


def test_whisper_cpp_omits_prompt_when_biasing_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        captured["body"] = request.data
        return _FakeResponse({"text": "ok"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    cfg = make_stt_config(backend="whisper-cpp", bias_with_gazetteer=False)
    WhisperCppTranscriber(cfg).transcribe(FRAME, "Otanuomi")
    assert b'name="prompt"' not in captured["body"]


def test_whisper_cpp_wraps_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    transcriber = WhisperCppTranscriber(make_stt_config(backend="whisper-cpp"))
    with pytest.raises(SttError):
        transcriber.transcribe(FRAME, "")


# ── TimeoutTranscriber watchdog (GDD §20) ────────────────────────────────────


class _FakeBackend:
    """Controllable Transcriber: hangs on demand, counts close() calls."""

    def __init__(self, text: str, hang: threading.Event | None = None) -> None:
        self.text = text
        self.hang = hang
        self.closed = 0

    def transcribe(self, pcm16k: bytes, bias: str) -> TranscriptResult:
        if self.hang is not None:
            self.hang.wait()
        return TranscriptResult(text=self.text, avg_logprob=-0.1)

    def close(self) -> None:
        self.closed += 1


def test_watchdog_passes_results_through() -> None:
    wrapped = TimeoutTranscriber(lambda: _FakeBackend("hostiles kisogo"), timeout_s=1.0)
    result = wrapped.transcribe(FRAME, "bias")
    assert result.text == "hostiles kisogo"


def test_watchdog_propagates_backend_errors() -> None:
    class Exploding:
        def transcribe(self, pcm16k: bytes, bias: str) -> TranscriptResult:
            raise SttError("model exploded")

    wrapped = TimeoutTranscriber(lambda: Exploding(), timeout_s=1.0)
    with pytest.raises(SttError, match="model exploded"):
        wrapped.transcribe(FRAME, "")


def test_watchdog_respawns_inner_on_hang() -> None:
    release = threading.Event()
    built: list[_FakeBackend] = []

    def factory() -> Transcriber:
        # First instance hangs forever; the respawned one answers instantly.
        backend = _FakeBackend("recovered", hang=release if not built else None)
        built.append(backend)
        return backend

    wrapped = TimeoutTranscriber(factory, timeout_s=0.05)
    try:
        with pytest.raises(SttTimeoutError):
            wrapped.transcribe(FRAME, "")
        assert len(built) == 2  # inner was rebuilt
        assert built[0].closed == 1  # stale backend was told to release its model
        assert wrapped.inner is built[1]
        # The respawned worker serves the next utterance normally.
        assert wrapped.transcribe(FRAME, "").text == "recovered"
    finally:
        release.set()  # unblock the abandoned daemon worker


# ── make_transcriber ─────────────────────────────────────────────────────────


def test_make_transcriber_selects_backend_behind_watchdog() -> None:
    fw = make_transcriber(make_stt_config(backend="faster-whisper"))
    assert isinstance(fw, TimeoutTranscriber)
    assert isinstance(fw.inner, FasterWhisperTranscriber)

    wc = make_transcriber(make_stt_config(backend="whisper-cpp"))
    assert isinstance(wc, TimeoutTranscriber)
    assert isinstance(wc.inner, WhisperCppTranscriber)
