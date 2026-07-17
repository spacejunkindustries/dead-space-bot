"""Tests for the Speaker: WAV wrapping, length cap, queues, §12.1 catalogue.

Everything runs on synthetic in-memory bytes — no real Piper binary, no audio
on disk (the ``wave`` module reads an ``io.BytesIO``, which is memory).
"""

from __future__ import annotations

import asyncio
import io
import json
import struct
import wave
from types import SimpleNamespace
from typing import Any

import pytest

from aura import tts
from aura.ipc import PRIORITY_ALERT
from aura.tts import (
    DEFAULT_SAMPLE_RATE,
    Speaker,
    SynthesisError,
    build_wav,
    read_voice_sample_rate,
)

# ── WAV header ────────────────────────────────────────────────────────────────


def test_build_wav_header_parses_with_stdlib_wave() -> None:
    pcm = struct.pack("<8h", 0, 1000, -1000, 32767, -32768, 42, -42, 0)
    wav_bytes = build_wav(pcm, 22050)
    with wave.open(io.BytesIO(wav_bytes)) as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 22050
        assert w.getnframes() == 8
        assert w.readframes(8) == pcm


def test_build_wav_exact_44_byte_header() -> None:
    pcm = b"\x00\x00" * 100
    wav_bytes = build_wav(pcm, 16000)
    assert len(wav_bytes) == 44 + len(pcm)
    assert wav_bytes[:4] == b"RIFF"
    assert struct.unpack_from("<I", wav_bytes, 4)[0] == 36 + len(pcm)
    assert wav_bytes[8:12] == b"WAVE"
    assert wav_bytes[12:16] == b"fmt "
    assert struct.unpack_from("<I", wav_bytes, 16)[0] == 16  # fmt size
    assert struct.unpack_from("<H", wav_bytes, 20)[0] == 1  # PCM
    assert struct.unpack_from("<H", wav_bytes, 22)[0] == 1  # mono
    assert struct.unpack_from("<I", wav_bytes, 24)[0] == 16000
    assert struct.unpack_from("<I", wav_bytes, 28)[0] == 32000  # byte rate
    assert struct.unpack_from("<H", wav_bytes, 32)[0] == 2  # block align
    assert struct.unpack_from("<H", wav_bytes, 34)[0] == 16  # bits
    assert wav_bytes[36:40] == b"data"
    assert struct.unpack_from("<I", wav_bytes, 40)[0] == len(pcm)


def test_build_wav_rejects_odd_pcm() -> None:
    with pytest.raises(ValueError):
        build_wav(b"\x00\x00\x00", 22050)


def test_build_wav_empty_pcm() -> None:
    with wave.open(io.BytesIO(build_wav(b"", 22050))) as w:
        assert w.getnframes() == 0


# ── voice config sample rate ──────────────────────────────────────────────────


def test_read_voice_sample_rate_from_config(tmp_path) -> None:
    voice = tmp_path / "voice.onnx"
    (tmp_path / "voice.onnx.json").write_text(
        json.dumps({"audio": {"sample_rate": 16000}}), encoding="utf-8"
    )
    assert read_voice_sample_rate(voice) == 16000


def test_read_voice_sample_rate_missing_file_defaults(tmp_path) -> None:
    assert read_voice_sample_rate(tmp_path / "absent.onnx") == DEFAULT_SAMPLE_RATE


def test_read_voice_sample_rate_bad_json_defaults(tmp_path) -> None:
    voice = tmp_path / "voice.onnx"
    (tmp_path / "voice.onnx.json").write_text("{nope", encoding="utf-8")
    assert read_voice_sample_rate(voice) == DEFAULT_SAMPLE_RATE


def test_read_voice_sample_rate_bad_rate_defaults(tmp_path) -> None:
    voice = tmp_path / "voice.onnx"
    (tmp_path / "voice.onnx.json").write_text(
        json.dumps({"audio": {"sample_rate": "fast"}}), encoding="utf-8"
    )
    assert read_voice_sample_rate(voice) == DEFAULT_SAMPLE_RATE


# ── §12.1 utterance catalogue: exact GDD strings ─────────────────────────────


def test_catalogue_matches_gdd_12_1_exactly() -> None:
    assert tts.ping_sent("Otanuomi") == "Hostiles Otanuomi, pinged."
    assert tts.ping_sent("Otanuomi", "home defense") == "Hostiles Otanuomi, pinged home defense."
    assert tts.ambiguous("Hostiles", "Otanuomi") == "Hostiles Otanuomi — say again to confirm."
    assert tts.say_again() == "Say again the system."
    assert tts.responders(2, "Otanuomi") == "Two responding to Otanuomi."
    assert tts.resolved("Otanuomi") == "Otanuomi clear."
    assert tts.timer_set("Kisogo", "four hours") == "Timer Kisogo, four hours."
    assert tts.flood_control() == "Flood control active."
    assert tts.degraded() == "Voice offline, use slash commands."


def test_number_word() -> None:
    assert tts.number_word(0) == "zero"
    assert tts.number_word(10) == "ten"
    assert tts.number_word(11) == "11"
    assert tts.number_word(-3) == "-3"


# ── Speaker with a fake piper subprocess ──────────────────────────────────────


class _FakeProc:
    def __init__(self, stdout: bytes, returncode: int = 0, stderr: bytes = b"") -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.stdin_received: bytes | None = None

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin_received = input
        return self._stdout, self._stderr

    def kill(self) -> None:  # pragma: no cover — timeout path not exercised here
        pass

    async def wait(self) -> int:  # pragma: no cover
        return self.returncode


class _FakeIpc:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int, bytes]] = []

    async def send_tts(self, guild_id: int, priority: int, wav_bytes: bytes) -> None:
        self.sent.append((guild_id, priority, wav_bytes))


def _holder(tmp_path, *, enabled: bool = True, max_utterance_s: float = 3.0) -> Any:
    """A ConfigHolder stand-in exposing only what Speaker reads."""
    voice = tmp_path / "voice.onnx"
    (tmp_path / "voice.onnx.json").write_text(
        json.dumps({"audio": {"sample_rate": 22050}}), encoding="utf-8"
    )
    cfg = SimpleNamespace(
        tts=SimpleNamespace(
            enabled=enabled,
            voice=str(voice),
            binary="/nonexistent/piper",
            max_utterance_s=max_utterance_s,
            duck_to=0.6,
            suppress_while_speech=True,
        )
    )
    return SimpleNamespace(current=cfg)


def _patch_piper(monkeypatch: pytest.MonkeyPatch, stdout: bytes, **proc_kwargs: Any) -> list:
    calls: list[tuple] = []

    async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProc:
        calls.append(argv)
        return _FakeProc(stdout, **proc_kwargs)

    monkeypatch.setattr(tts.asyncio, "create_subprocess_exec", fake_exec)
    return calls


async def test_say_under_cap_sends_wav(tmp_path, monkeypatch) -> None:
    # 1 second of audio at 22050 Hz — well under the 3s cap.
    pcm = b"\x01\x00" * 22050
    calls = _patch_piper(monkeypatch, pcm)
    ipc = _FakeIpc()
    speaker = Speaker(_holder(tmp_path), ipc)  # type: ignore[arg-type]

    assert await speaker.say(42, "Hostiles Otanuomi, pinged.", PRIORITY_ALERT) is True
    assert len(ipc.sent) == 1
    guild_id, priority, wav_bytes = ipc.sent[0]
    assert (guild_id, priority) == (42, PRIORITY_ALERT)
    with wave.open(io.BytesIO(wav_bytes)) as w:
        assert w.getframerate() == 22050
        assert w.getnframes() == 22050
        assert w.readframes(w.getnframes()) == pcm
    # Piper was invoked with the documented CLI shape.
    assert calls[0][0] == "/nonexistent/piper"
    assert "--model" in calls[0] and "--output-raw" in calls[0]
    await speaker.close()


async def test_say_over_cap_drops_and_returns_false(tmp_path, monkeypatch) -> None:
    # 4 seconds of audio at 22050 Hz — over the 3s cap: dropped, not truncated.
    pcm = b"\x01\x00" * (22050 * 4)
    _patch_piper(monkeypatch, pcm)
    ipc = _FakeIpc()
    speaker = Speaker(_holder(tmp_path, max_utterance_s=3.0), ipc)  # type: ignore[arg-type]

    assert await speaker.say(42, "a very long utterance") is False
    assert ipc.sent == []
    await speaker.close()


async def test_cap_boundary_exactly_at_limit_is_sent(tmp_path, monkeypatch) -> None:
    pcm = b"\x01\x00" * (22050 * 3)  # exactly 3.0s
    _patch_piper(monkeypatch, pcm)
    ipc = _FakeIpc()
    speaker = Speaker(_holder(tmp_path, max_utterance_s=3.0), ipc)  # type: ignore[arg-type]
    assert await speaker.say(1, "exactly at cap") is True
    assert len(ipc.sent) == 1
    await speaker.close()


async def test_say_disabled_returns_false_without_synthesis(tmp_path, monkeypatch) -> None:
    calls = _patch_piper(monkeypatch, b"\x00\x00")
    ipc = _FakeIpc()
    speaker = Speaker(_holder(tmp_path, enabled=False), ipc)  # type: ignore[arg-type]
    assert await speaker.say(1, "anything") is False
    assert calls == []
    assert ipc.sent == []
    await speaker.close()


async def test_say_muted_user_suppressed(tmp_path, monkeypatch) -> None:
    _patch_piper(monkeypatch, b"\x01\x00" * 1000)
    ipc = _FakeIpc()
    speaker = Speaker(_holder(tmp_path), ipc)  # type: ignore[arg-type]
    speaker.set_muted(777, True)
    assert await speaker.say(1, "hi", user_id=777) is False
    assert await speaker.say(1, "hi", user_id=778) is True
    speaker.set_muted(777, False)
    assert await speaker.say(1, "hi", user_id=777) is True
    await speaker.close()


async def test_piper_failure_returns_false(tmp_path, monkeypatch) -> None:
    _patch_piper(monkeypatch, b"", returncode=1, stderr=b"boom")
    ipc = _FakeIpc()
    speaker = Speaker(_holder(tmp_path), ipc)  # type: ignore[arg-type]
    assert await speaker.say(1, "hi") is False
    assert ipc.sent == []
    await speaker.close()


async def test_missing_binary_returns_false(tmp_path) -> None:
    # No monkeypatch: /nonexistent/piper genuinely cannot exec.
    ipc = _FakeIpc()
    speaker = Speaker(_holder(tmp_path), ipc)  # type: ignore[arg-type]
    assert await speaker.say(1, "hi") is False
    assert ipc.sent == []
    await speaker.close()


async def test_synthesize_raises_on_failure(tmp_path, monkeypatch) -> None:
    _patch_piper(monkeypatch, b"", returncode=2, stderr=b"model not found")
    speaker = Speaker(_holder(tmp_path), _FakeIpc())  # type: ignore[arg-type]
    with pytest.raises(SynthesisError, match="exited 2"):
        await speaker.synthesize("hi")
    await speaker.close()


async def test_per_guild_ordering(tmp_path, monkeypatch) -> None:
    """Utterances for one guild are synthesised and sent strictly in order."""
    order: list[str] = []

    async def fake_exec(*argv: str, **kwargs: Any) -> Any:
        class _P(_FakeProc):
            async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
                order.append((input or b"").decode().strip())
                await asyncio.sleep(0)  # yield, allowing interleave if unserialised
                return self._stdout, self._stderr

        return _P(b"\x01\x00" * 100)

    monkeypatch.setattr(tts.asyncio, "create_subprocess_exec", fake_exec)
    ipc = _FakeIpc()
    speaker = Speaker(_holder(tmp_path), ipc)  # type: ignore[arg-type]

    results = await asyncio.gather(
        speaker.say(1, "first"), speaker.say(1, "second"), speaker.say(1, "third")
    )
    assert results == [True, True, True]
    assert order == ["first", "second", "third"]
    assert [len(s) for s in ipc.sent] == [3, 3, 3]
    await speaker.close()


async def test_close_flushes_pending_jobs_false(tmp_path, monkeypatch) -> None:
    _patch_piper(monkeypatch, b"\x01\x00" * 100)
    speaker = Speaker(_holder(tmp_path), _FakeIpc())  # type: ignore[arg-type]
    assert await speaker.say(1, "warm up") is True  # spin up the worker
    await speaker.close()
    assert await speaker.say(1, "after close") is False
