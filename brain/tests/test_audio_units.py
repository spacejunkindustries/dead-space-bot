"""Unit tests for the audio front-end pieces: endpointing, wake scoring
mechanics, and the STT backends/watchdog — GDD §5, §16, §20.

Runs without the heavy audio deps (webrtcvad, openwakeword, faster_whisper,
numpy): native engines are faked at the seam each class exposes. All PCM is
synthetic bytes in RAM; nothing here touches disk (CLAUDE.md constraint 5).
"""

from __future__ import annotations

import asyncio
import io
import json
import struct
import sys
import threading
import time
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


def make_stt_config(
    backend: str = "faster-whisper",
    bias_with_gazetteer: bool = True,
    watchdog_s: float = DEFAULT_WATCHDOG_S,
) -> SttConfig:
    return SttConfig(
        backend=backend,
        model="small",
        compute_type="int8",
        cpu_threads=2,
        bias_with_gazetteer=bias_with_gazetteer,
        whisper_cpp_url="http://127.0.0.1:8080/inference",
        watchdog_s=watchdog_s,
    )


# ── frame constants (GDD §15 wire format) ────────────────────────────────────


def test_frame_constants_match_the_wire_format() -> None:
    assert SAMPLE_RATE_HZ == 16_000
    assert FRAME_MS == 20
    assert SAMPLES_PER_FRAME == 320
    assert FRAME_BYTES == 640  # 20ms of 16kHz mono s16le


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


class _FakeModel:
    """Stands in for a built openWakeWord model instance in the spare pool."""

    def __init__(self) -> None:
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1


def make_detector(
    scores: list[float], threshold: float = 0.5, refractory_ms: int = 200
) -> tuple[OpenWakeWordDetector, _ScriptedPredict]:
    detector = OpenWakeWordDetector(make_wake_holder(threshold, refractory_ms))  # type: ignore[arg-type]
    # openwakeword isn't installed in the test env (__init__ logged the lazy
    # fallback); give the pool a build seam so models "build" instantly.
    detector._build_model = _FakeModel  # type: ignore[method-assign]
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


# ── wake model pool (spares, fault latch, config generations, counters) ──────


def test_no_spare_ready_skips_scoring_until_the_build_lands() -> None:
    # A NEW speaker while the pool is empty and a background build is in
    # flight: never build on the hot path — skip scoring, frames keep ringing
    # upstream, and the moment the spare lands scoring resumes.
    detector, predict = make_detector([0.9])
    assert detector._spares == []  # openwakeword absent: no init spare
    detector._build_inflight = True  # a background asyncio.to_thread build

    for _ in range(8):
        assert detector.score(USER, FRAME) == 0.0
    assert predict.chunks == []  # no model, no inference, no sync build
    c = detector.counters()
    assert c["frames_seen"] == 8
    assert c["vad_speech"] == 0  # the visible zero: frames flow, none scored

    # The background build completes (what _build_spare_off_loop does):
    detector._store_spare(detector._cfg_key(), _FakeModel())
    for _ in range(4):
        detector.score(USER, FRAME)
    assert len(predict.chunks) == 1  # scoring resumed with the delivered model
    assert detector.counters()["hits"] == 1


async def test_consumed_spare_is_replenished_off_the_event_loop() -> None:
    detector, predict = make_detector([0.1, 0.1])
    build_threads: list[threading.Thread] = []

    def build() -> _FakeModel:
        build_threads.append(threading.current_thread())
        return _FakeModel()

    detector._build_model = build  # type: ignore[method-assign]
    detector._spares.append(_FakeModel())  # the spare __init__ would have built

    _emit_one_chunk(detector)  # first speaker consumes the spare
    assert detector._spares == []
    await asyncio.gather(*detector._replenish_tasks)  # the to_thread build
    assert len(detector._spares) == 1
    # The replacement was built OFF the loop thread (asyncio.to_thread).
    assert build_threads and all(t is not threading.main_thread() for t in build_threads)

    # A second speaker picks the replenished spare up immediately.
    other = USER + 1
    for _ in range(4):
        detector.score(other, FRAME)
    assert len(predict.chunks) == 2
    await asyncio.gather(*detector._replenish_tasks)  # drain the next top-up


def test_build_failure_latches_faulted_without_a_retry_storm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cortana.audio.wake as wake_mod

    errors: list[str] = []
    monkeypatch.setattr(
        wake_mod.log, "error", lambda event, **kw: errors.append(event), raising=False
    )
    detector, predict = make_detector([])
    build_calls: list[int] = []

    def bad_build() -> Any:
        build_calls.append(1)
        raise RuntimeError("onnx file missing")

    detector._build_model = bad_build  # type: ignore[method-assign]
    for _ in range(20):
        assert detector.score(USER, FRAME) == 0.0
    assert detector.faulted is True
    assert len(build_calls) == 1  # latched: no per-chunk retry storm
    assert errors == ["wake_model_build_failed"]  # loud, exactly once
    assert predict.chunks == []
    c = detector.counters()
    assert c["frames_seen"] == 20
    assert c["vad_speech"] == 0

    # A wake config change (new generation) clears the fault and retries.
    detector._build_model = _FakeModel  # type: ignore[method-assign]
    detector._holder.current.wake = WakeConfig(
        model="/opt/cortana/models/wake/retrained.onnx", threshold=0.5, refractory_ms=200
    )
    for _ in range(4):
        detector.score(USER, FRAME)
    assert detector.faulted is False
    assert len(predict.chunks) == 1  # scoring resumed under the new config


def test_config_change_drops_and_rebuilds_live_models_lazily() -> None:
    # SIGHUP swapping wake.model must reach ACTIVE speakers, not only new
    # ones — per-user models are generation-tagged and rebuilt via the pool.
    detector, predict = make_detector([0.1, 0.1])
    _emit_one_chunk(detector)
    model_a = detector._states[USER].model
    assert model_a is not None

    new_cfg = WakeConfig(
        model="/opt/cortana/models/wake/retrained.onnx", threshold=0.5, refractory_ms=200
    )
    detector._holder.current.wake = new_cfg
    _emit_one_chunk(detector)  # stale model dropped, fresh one from the pool
    state = detector._states[USER]
    assert state.model is not None
    assert state.model is not model_a
    assert state.cfg_key == (new_cfg.model, new_cfg.vad_threshold)
    assert len(predict.chunks) == 2


def test_counters_track_every_pipeline_stage() -> None:
    detector, _ = make_detector([0.9, 0.4, 0.1], threshold=0.5)
    for _ in range(3):
        _emit_one_chunk(detector)  # hit, near-miss, quiet
    assert detector.counters() == {
        "frames_seen": 12,
        "vad_speech": 12,
        "inferences": 3,
        "hits": 1,
        "near_misses": 1,
    }


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


def test_whisper_cpp_wraps_mid_request_server_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    """urllib only wraps errors from sending the request; a whisper.cpp
    restart mid-decode (deploy, OOM) surfaces raw from getresponse()/read()
    as RemoteDisconnected / IncompleteRead / BadStatusLine /
    ConnectionResetError. Every one must become the clean SttError the caller
    contract promises (the dialog "say again" path), never escape raw."""
    import http.client

    drops: tuple[BaseException, ...] = (
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        http.client.IncompleteRead(b"partial"),
        http.client.BadStatusLine("garbage"),
        ConnectionResetError(104, "Connection reset by peer"),
    )
    for drop in drops:

        def fake_urlopen(request: Any, timeout: float, _drop: BaseException = drop) -> None:
            raise _drop

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


# ── bounded serialized STT queue (GDD §20 hardening) ─────────────────────────


class _BlockingBackend:
    """Transcriber that parks inside transcribe() until released."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def transcribe(self, pcm16k: bytes, bias: str) -> TranscriptResult:
        self.entered.set()
        assert self.release.wait(10.0), "test forgot to release the backend"
        return TranscriptResult(text=bias, avg_logprob=-0.1)


def _wait_for_queue_depth(wrapped: TimeoutTranscriber, depth: int) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with wrapped._lock:
            if len(wrapped._queue) == depth:
                return
        time.sleep(0.005)
    raise AssertionError(f"queue never reached depth {depth}")


def test_queue_overflow_drops_the_oldest_queued_job_with_stt_error() -> None:
    backend = _BlockingBackend()
    wrapped = TimeoutTranscriber(lambda: backend, timeout_s=10.0, queue_depth=2)
    results: dict[str, Any] = {}

    def call(tag: str) -> None:
        try:
            results[tag] = wrapped.transcribe(FRAME, tag)
        except SttError as exc:
            results[tag] = exc

    threads = {"head": threading.Thread(target=call, args=("head",), daemon=True)}
    threads["head"].start()
    assert backend.entered.wait(2.0)  # head is IN SERVICE; the queue is empty
    for depth, tag in enumerate(("q1", "q2"), start=1):
        threads[tag] = threading.Thread(target=call, args=(tag,), daemon=True)
        threads[tag].start()
        _wait_for_queue_depth(wrapped, depth)

    # The queue is full: the next job evicts the OLDEST queued one (q1),
    # whose caller gets SttError promptly — routed by the dialog machine
    # through its normal say-again fail path.
    threads["q3"] = threading.Thread(target=call, args=("q3",), daemon=True)
    threads["q3"].start()
    threads["q1"].join(2.0)
    assert isinstance(results["q1"], SttError)
    assert "overflow" in str(results["q1"])

    backend.release.set()
    for thread in threads.values():
        thread.join(3.0)
    # Everything that kept its place was served, strictly one at a time.
    assert results["head"].text == "head"
    assert results["q2"].text == "q2"
    assert results["q3"].text == "q3"
    assert wrapped.degraded is False


def test_watchdog_measures_service_time_not_queue_wait() -> None:
    # Four concurrent decodes at 0.15s each: the LAST one finishes ~0.6s
    # after enqueue — far past the 0.4s watchdog — but every queue-head
    # SERVICE takes 0.15s, so overload must produce zero respawns.
    built: list[Any] = []

    class _Sleepy:
        def transcribe(self, pcm16k: bytes, bias: str) -> TranscriptResult:
            time.sleep(0.15)
            return TranscriptResult(text=bias, avg_logprob=-0.1)

    def factory() -> Transcriber:
        backend = _Sleepy()
        built.append(backend)
        return backend

    wrapped = TimeoutTranscriber(factory, timeout_s=0.4, queue_depth=4)
    results: list[Any] = []

    def call(tag: str) -> None:
        try:
            results.append(wrapped.transcribe(FRAME, tag).text)
        except SttError as exc:  # pragma: no cover — the failure this test guards
            results.append(exc)

    threads = [threading.Thread(target=call, args=(f"job{i}",), daemon=True) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)

    assert sorted(str(r) for r in results) == ["job0", "job1", "job2", "job3"]
    assert len(built) == 1  # overload never masqueraded as a hang: no respawn
    assert wrapped.degraded is False


def test_respawn_cap_latches_degraded_and_reset_recovers() -> None:
    release = threading.Event()
    built: list[Any] = []
    hang = {"on": True}

    class _MaybeHang:
        def transcribe(self, pcm16k: bytes, bias: str) -> TranscriptResult:
            if hang["on"]:
                release.wait(10.0)
                raise SttError("released by test teardown")
            return TranscriptResult(text="recovered", avg_logprob=-0.1)

        def close(self) -> None:
            pass

    def factory() -> Transcriber:
        backend = _MaybeHang()
        built.append(backend)
        return backend

    wrapped = TimeoutTranscriber(factory, timeout_s=0.05)
    try:
        # Hangs 1 and 2: watchdog fires, backend respawned each time.
        for expected_builds in (2, 3):
            with pytest.raises(SttTimeoutError):
                wrapped.transcribe(FRAME, "")
            assert len(built) == expected_builds
            assert wrapped.degraded is False
        # Hang 3 exceeds the cap: latch DEGRADED, refuse to respawn again.
        with pytest.raises(SttTimeoutError):
            wrapped.transcribe(FRAME, "")
        assert wrapped.degraded is True
        assert len(built) == 3  # no third rebuild burning CPU on a broken backend

        # While latched: immediate SttError, never another decode attempt.
        with pytest.raises(SttError) as excinfo:
            wrapped.transcribe(FRAME, "")
        assert not isinstance(excinfo.value, SttTimeoutError)
        assert len(built) == 3

        # The /reload path: reset_degraded stands a fresh backend up.
        hang["on"] = False
        wrapped.reset_degraded()
        assert wrapped.degraded is False
        assert wrapped.transcribe(FRAME, "").text == "recovered"
    finally:
        release.set()  # unblock the abandoned daemon workers


def test_completed_decode_resets_the_consecutive_respawn_count() -> None:
    release = threading.Event()
    hang = {"on": True}

    class _MaybeHang:
        def transcribe(self, pcm16k: bytes, bias: str) -> TranscriptResult:
            if hang["on"]:
                release.wait(10.0)
                raise SttError("released by test teardown")
            return TranscriptResult(text="ok", avg_logprob=-0.1)

        def close(self) -> None:
            pass

    wrapped = TimeoutTranscriber(lambda: _MaybeHang(), timeout_s=0.05)
    try:
        # timeout → success, three times over: more total timeouts than the
        # cap, but never CONSECUTIVE ones — the latch must not engage.
        for _ in range(3):
            hang["on"] = True
            with pytest.raises(SttTimeoutError):
                wrapped.transcribe(FRAME, "")
            hang["on"] = False
            assert wrapped.transcribe(FRAME, "").text == "ok"
        assert wrapped.degraded is False
    finally:
        release.set()


def test_respawned_backend_load_is_not_charged_to_queued_jobs() -> None:
    """After a watchdog respawn with a job still queued, the fresh backend's
    model load must run BEFORE the worker picks that job up (warm at worker
    start), never inside its service window. Without this, one genuine hang
    during a burst turns the reload into a false consecutive timeout and the
    cascade latches degraded — the exact spiral _warm_off_path/GDD §20 exists
    to prevent."""
    release = threading.Event()

    class _HangingFirst:
        def __init__(self) -> None:
            self.entered = threading.Event()

        def transcribe(self, pcm16k: bytes, bias: str) -> TranscriptResult:
            self.entered.set()
            release.wait(10.0)
            raise SttError("released by test teardown")

        def close(self) -> None:
            pass

    class _SlowLoad:
        """Simulates faster-whisper: lazy model load on first use, warm()
        preloads. The load takes 3x the watchdog deadline."""

        def __init__(self) -> None:
            self.loaded = threading.Event()

        def warm(self) -> None:
            time.sleep(0.3)
            self.loaded.set()

        def transcribe(self, pcm16k: bytes, bias: str) -> TranscriptResult:
            if not self.loaded.is_set():
                self.warm()  # lazy load INSIDE the service window
            return TranscriptResult(text=bias, avg_logprob=-0.1)

    hanging = _HangingFirst()
    backends: list[Any] = [hanging, _SlowLoad()]
    wrapped = TimeoutTranscriber(lambda: backends.pop(0), timeout_s=0.1, queue_depth=3)
    results: dict[str, Any] = {}

    def call(tag: str) -> None:
        try:
            results[tag] = wrapped.transcribe(FRAME, tag)
        except SttError as exc:
            results[tag] = exc

    try:
        head = threading.Thread(target=call, args=("head",), daemon=True)
        head.start()
        assert hanging.entered.wait(2.0)  # head is in service (and hung)
        queued = threading.Thread(target=call, args=("queued",), daemon=True)
        queued.start()
        _wait_for_queue_depth(wrapped, 1)

        head.join(5.0)
        queued.join(5.0)
        assert isinstance(results["head"], SttTimeoutError)  # the genuine hang
        # The queued job survived the respawn: its watchdog clock started
        # AFTER the fresh backend finished loading.
        assert results["queued"].text == "queued"
        assert wrapped.degraded is False
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


def test_make_transcriber_threads_watchdog_config_through() -> None:
    wrapped = make_transcriber(make_stt_config(backend="whisper-cpp", watchdog_s=10.0))
    assert isinstance(wrapped, TimeoutTranscriber)
    assert wrapped._timeout_s == 10.0
    # The whisper.cpp HTTP socket gives up slightly BEFORE the watchdog so a
    # slow server is a clean SttError, not a respawn.
    assert wrapped.inner._timeout_s == pytest.approx(9.0)  # type: ignore[union-attr]
