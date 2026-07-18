"""Tests for the Speaker: WAV wrapping, length cap, queues, §12.1 catalogue.

Everything runs on synthetic in-memory bytes — no real Piper binary, no audio
on disk (the ``wave`` module reads an ``io.BytesIO``, which is memory).
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import struct
import wave
from types import SimpleNamespace
from typing import Any

import pytest

from cortana import tts
from cortana.ipc import PRIORITY_ALERT
from cortana.tts import (
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
    assert tts.not_understood() == "Say again?"
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


def test_personal_ping_catalogue_strings() -> None:
    assert tts.pinging_you("gate camps", "Otanuomi") == "Pinging you for gate camps in Otanuomi."
    assert tts.pinging_you("everything", None) == "Pinging you for everything everywhere."
    assert tts.ping_cleared() == "No longer pinging you."
    assert tts.no_pings() == "You have no pings set."
    assert tts.ping_limit() == "Ping limit reached."


def test_ping_types_phrase_pluralizes_naturally() -> None:
    from cortana.types import Intent

    assert tts.ping_types_phrase(frozenset({Intent.HOSTILE_SPOTTED})) == "hostiles"
    assert tts.ping_types_phrase(frozenset({Intent.UNDER_ATTACK})) == "attacks"
    assert tts.ping_types_phrase(frozenset({Intent.ASSIST_REQUEST})) == "assist requests"
    assert tts.ping_types_phrase(frozenset({Intent.GATE_CAMP})) == "gate camps"
    assert (
        tts.ping_types_phrase(frozenset({Intent.HOSTILE_SPOTTED, Intent.GATE_CAMP}))
        == "hostiles and gate camps"
    )
    assert (
        tts.ping_types_phrase(
            frozenset(
                {
                    Intent.HOSTILE_SPOTTED,
                    Intent.UNDER_ATTACK,
                    Intent.ASSIST_REQUEST,
                    Intent.GATE_CAMP,
                }
            )
        )
        == "everything"
    )


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
    """send_tts mirrors IpcServer's v2 contract: True while a client is
    attached, False when Ears is disconnected (Speaker must then report the
    utterance as unspoken so the channel-text fallback engages)."""

    def __init__(self, *, connected: bool = True) -> None:
        self.sent: list[tuple[int, int, bytes]] = []
        self.connected = connected

    async def send_tts(self, guild_id: int, priority: int, wav_bytes: bytes) -> bool:
        if not self.connected:
            return False
        self.sent.append((guild_id, priority, wav_bytes))
        return True


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
            effect="none",
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


async def test_short_line_synthesised_once_then_cached(tmp_path, monkeypatch) -> None:
    # Piper reloads its model every spawn; scripted lines render once and
    # replay from the line cache.
    pcm = b"\x01\x00" * 22050
    calls = _patch_piper(monkeypatch, pcm)
    ipc = _FakeIpc()
    speaker = Speaker(_holder(tmp_path), ipc)  # type: ignore[arg-type]

    assert await speaker.say(1, "Go ahead.") is True
    assert await speaker.say(1, "Go ahead.") is True
    assert len(calls) == 1  # second play served from cache
    assert len(ipc.sent) == 2
    assert ipc.sent[0][2] == ipc.sent[1][2]  # identical WAV bytes
    await speaker.close()


async def test_long_text_never_cached(tmp_path, monkeypatch) -> None:
    pcm = b"\x01\x00" * 22050
    calls = _patch_piper(monkeypatch, pcm)
    ipc = _FakeIpc()
    speaker = Speaker(_holder(tmp_path), ipc)  # type: ignore[arg-type]

    long_text = "the situation in the north is developing and " * 3  # > 80 chars
    assert await speaker.say(1, long_text) is True
    assert await speaker.say(1, long_text) is True
    assert len(calls) == 2  # variable text synthesises fresh every time
    await speaker.close()


async def test_start_priming_renders_every_hot_line_once(tmp_path, monkeypatch) -> None:
    tts.set_personality("standard")
    pcm = b"\x01\x00" * 2205
    calls = _patch_piper(monkeypatch, pcm)
    ipc = _FakeIpc()
    speaker = Speaker(_holder(tmp_path), ipc)  # type: ignore[arg-type]

    speaker.start_priming()
    assert speaker._prime_task is not None
    await speaker._prime_task
    assert len(calls) == len(tts.hot_lines())
    # A primed line replays from cache — no further synthesis.
    assert await speaker.say(1, "Go ahead.") is True
    assert len(calls) == len(tts.hot_lines())
    assert ipc.sent  # and it actually played
    await speaker.close()


def test_hot_lines_cover_the_ack_pool() -> None:
    tts.set_personality("standard")
    try:
        lines = tts.hot_lines()
        assert "Go ahead." in lines
        assert "Say again?" in lines
        assert "Relayed." in lines
        assert "Code orange. Go ahead." in lines
        assert "Listening." not in lines  # cortana variants only under cortana
        tts.set_personality("cortana")
        cortana_lines = tts.hot_lines()
        assert "Listening." in cortana_lines
        assert set(lines) <= set(cortana_lines)
    finally:
        tts.set_personality("standard")


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


async def test_alert_jumps_ahead_of_queued_normals(tmp_path, monkeypatch) -> None:
    """The per-guild queue is priority-ordered: an ALERT enqueued behind
    NORMAL jobs plays before them (the in-flight synthesis is never
    preempted), so a voice ack can't sit behind an uncached synthesis past
    the dialog grace window. Equal priorities keep FIFO order, and every
    job's completion future still resolves True."""
    order: list[str] = []
    first_started = asyncio.Event()
    gate = asyncio.Event()

    async def fake_exec(*argv: str, **kwargs: Any) -> Any:
        class _P(_FakeProc):
            async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
                text = (input or b"").decode().strip()
                if text == "first":
                    first_started.set()
                    await gate.wait()  # hold the worker: the rest must queue
                order.append(text)
                return self._stdout, self._stderr

        return _P(b"\x01\x00" * 100)

    monkeypatch.setattr(tts.asyncio, "create_subprocess_exec", fake_exec)
    ipc = _FakeIpc()
    speaker = Speaker(_holder(tmp_path), ipc)  # type: ignore[arg-type]

    t_first = asyncio.create_task(speaker.say(1, "first"))
    await first_started.wait()  # in-flight NORMAL holds the worker
    t_normal_a = asyncio.create_task(speaker.say(1, "normal a"))
    t_normal_b = asyncio.create_task(speaker.say(1, "normal b"))
    await asyncio.sleep(0)  # both NORMALs are queued...
    t_alert = asyncio.create_task(speaker.say(1, "alert", PRIORITY_ALERT))
    await asyncio.sleep(0)  # ...before the ALERT even arrives
    gate.set()

    results = await asyncio.gather(t_first, t_normal_a, t_normal_b, t_alert)
    assert results == [True, True, True, True]
    assert order == ["first", "alert", "normal a", "normal b"]
    assert len(ipc.sent) == 4
    await speaker.close()


async def test_voice_swap_on_reload_refreshes_sample_rate(tmp_path, monkeypatch) -> None:
    """A SIGHUP that swaps tts.voice must rebuild WAV headers (and the
    duration check) with the NEW voice's native rate, not the boot-time one."""
    holder = _holder(tmp_path)  # voice A: 22050 Hz
    voice_b = tmp_path / "voice_b.onnx"
    (tmp_path / "voice_b.onnx.json").write_text(
        json.dumps({"audio": {"sample_rate": 16000}}), encoding="utf-8"
    )
    pcm = b"\x01\x00" * 16000  # 1s at 16 kHz
    _patch_piper(monkeypatch, pcm)
    ipc = _FakeIpc()
    speaker = Speaker(holder, ipc)  # type: ignore[arg-type]
    assert speaker.sample_rate == 22050

    holder.current.tts.voice = str(voice_b)  # simulate the config reload
    assert await speaker.say(1, "after reload") is True
    assert speaker.sample_rate == 16000
    with wave.open(io.BytesIO(ipc.sent[0][2])) as w:
        assert w.getframerate() == 16000  # header built with the new rate
    await speaker.close()


async def test_render_uses_one_config_snapshot_for_key_synthesis_and_rate(
    tmp_path, monkeypatch
) -> None:
    """A SIGHUP voice swap landing between _render's config read and the
    Piper invocation must not synthesise with the NEW voice while caching
    under the OLD voice's key and wrapping in the OLD rate's header: one
    snapshot flows through the whole render."""
    voice_b = tmp_path / "voice_b.onnx"
    (tmp_path / "voice_b.onnx.json").write_text(
        json.dumps({"audio": {"sample_rate": 16000}}), encoding="utf-8"
    )

    class _SwapMidRender:
        """holder.current returns voice A for a limited number of reads, then
        voice B — simulating the reload swap landing mid-render."""

        def __init__(self, cfg_a: Any, cfg_b: Any) -> None:
            self.cfg_a, self.cfg_b = cfg_a, cfg_b
            self.reads_of_a = 10**9

        @property
        def current(self) -> Any:
            if self.reads_of_a > 0:
                self.reads_of_a -= 1
                return self.cfg_a
            return self.cfg_b

    cfg_a = _holder(tmp_path).current  # voice A: 22050 Hz
    cfg_b = SimpleNamespace(
        tts=SimpleNamespace(
            enabled=True,
            voice=str(voice_b),
            binary="/nonexistent/piper",
            max_utterance_s=3.0,
            effect="none",
        )
    )
    holder = _SwapMidRender(cfg_a, cfg_b)
    pcm = b"\x01\x00" * 22050
    calls = _patch_piper(monkeypatch, pcm)
    speaker = Speaker(holder, _FakeIpc())  # type: ignore[arg-type]

    # The NEXT holder read (_render's snapshot) sees voice A; every read
    # after that — including any synthesize() re-read — would see voice B.
    holder.reads_of_a = 1
    rendered_pcm, rate = await speaker._render("Go ahead.")

    model = calls[0][calls[0].index("--model") + 1]
    assert model == cfg_a.tts.voice  # synthesised with the snapshot's voice…
    assert rate == 22050  # …and reported at that voice's rate
    assert speaker._line_cache[("Go ahead.", cfg_a.tts.voice, "none")] == rendered_pcm
    await speaker.close()


async def test_close_flushes_pending_jobs_false(tmp_path, monkeypatch) -> None:
    _patch_piper(monkeypatch, b"\x01\x00" * 100)
    speaker = Speaker(_holder(tmp_path), _FakeIpc())  # type: ignore[arg-type]
    assert await speaker.say(1, "warm up") is True  # spin up the worker
    await speaker.close()
    assert await speaker.say(1, "after close") is False


def test_build_chirp_is_valid_wav() -> None:
    import io
    import wave

    from cortana.tts import build_chirp

    data = build_chirp(22050)
    with wave.open(io.BytesIO(data)) as r:
        assert r.getnchannels() == 1
        assert r.getframerate() == 22050
        assert r.getnframes() > 0  # non-empty tone


def test_holographic_effect_preserves_format() -> None:
    pytest.importorskip("numpy")  # lazy audio dep; skip in the light CI env
    import struct

    from cortana.tts import holographic

    # 200ms of a 220Hz tone at 22050Hz, s16le.
    rate = 22050
    n = rate // 5
    pcm = b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * 220 * i / rate))) for i in range(n)
    )
    out = holographic(pcm, rate)
    assert isinstance(out, bytes)
    assert len(out) == len(pcm)  # same length, same s16le format
    assert len(out) % 2 == 0
    assert holographic(b"", rate) == b""  # empty passes through


# ── personality pack (GDD §12.4) ─────────────────────────────────────────────


def test_standard_personality_keeps_exact_catalogue() -> None:
    tts.set_personality("standard")
    assert tts.go_ahead() == "Go ahead."
    assert tts.relayed() == "Relayed."


def test_cortana_personality_rotates_ack_lines_only() -> None:
    tts.set_personality("cortana")
    try:
        seen = {tts.go_ahead() for _ in range(50)}
        assert len(seen) > 1  # it actually varies
        assert all(len(line) < 40 for line in seen)  # stays under the spoken cap
        # Info-carrying lines never vary.
        assert tts.ping_sent("Otanuomi") == "Hostiles Otanuomi, pinged."
        assert tts.responders(2, "Otanuomi") == "Two responding to Otanuomi."
    finally:
        tts.set_personality("standard")


def test_bratty_personality_rotates_with_attitude() -> None:
    from cortana.types import Severity

    tts.set_personality("bratty")
    try:
        seen = {tts.go_ahead() for _ in range(60)}
        assert len(seen) > 3  # rotates
        assert seen <= set(tts._GO_AHEAD_BRATTY)
        # Info-carrying lines never vary, whatever the personality.
        assert tts.chase_updated("Kisogo") == "Chase updated, Kisogo."
        assert tts.ping_sent("Otanuomi") == "Hostiles Otanuomi, pinged."
        # code_ack keeps the colour word intact in every variant.
        for _ in range(20):
            assert "red" in tts.code_ack(Severity.HIGH)
        # hot_lines covers the bratty pool so acks stay cached/instant.
        lines = tts.hot_lines()
        assert set(tts._GO_AHEAD_BRATTY) <= set(lines)
    finally:
        tts.set_personality("standard")


async def test_say_returns_false_when_ears_disconnected(tmp_path, monkeypatch) -> None:
    """IPC v2: send_tts is False with no Ears attached — the utterance was
    never played, so say() must report unspoken (channel-text fallback)."""
    pcm = b"\x01\x00" * 22050
    _patch_piper(monkeypatch, pcm)
    ipc = _FakeIpc(connected=False)
    speaker = Speaker(_holder(tmp_path), ipc)  # type: ignore[arg-type]

    assert await speaker.say(42, "Hostiles Otanuomi, pinged.", PRIORITY_ALERT) is False
    assert ipc.sent == []
    await speaker.close()


async def test_chirp_returns_false_when_ears_disconnected(tmp_path) -> None:
    ipc = _FakeIpc(connected=False)
    speaker = Speaker(_holder(tmp_path), ipc)  # type: ignore[arg-type]
    assert await speaker.chirp(42) is False
    ipc.connected = True
    assert await speaker.chirp(42) is True
    assert len(ipc.sent) == 1
    await speaker.close()
