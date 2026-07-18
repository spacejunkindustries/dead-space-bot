"""Tests for the GDD §15 frame codec (v2) and the Brain-side UDS server.

The wire format spans both languages — these tests pin the exact byte layout
(including the length = 1 + len(body) convention and the v2 audio capture
timestamp) so a Rust-side change that desyncs the framing fails loudly here.
Server tests additionally cover the v2 handshake (protocol-version refusal),
the audio age gate, the heartbeat → hb_ack liveness answer, and the
send-returns-bool contract.
"""

from __future__ import annotations

import asyncio
import json
import random
import struct
from types import SimpleNamespace
from typing import Any

import pytest

from cortana.ipc import (
    IPC_PROTOCOL_VERSION,
    MAX_AUDIO_AGE_S,
    MAX_FRAME_BYTES,
    PRIORITY_ALERT,
    PRIORITY_NORMAL,
    TYPE_AUDIO,
    TYPE_CONTROL,
    TYPE_TTS,
    AudioFrame,
    ControlFrame,
    FrameCodec,
    FrameDecodeError,
    IpcServer,
    TtsFrame,
)

#: Wall clock used by the server harness, seconds since epoch (fixed).
_WALL_S = 1_000_000.0


def _hello(proto: int | None = IPC_PROTOCOL_VERSION) -> dict:
    msg: dict[str, Any] = {"t": "hello", "version": "1.0.0"}
    if proto is not None:
        msg["proto"] = proto
    return msg


def _fresh_ms() -> int:
    """A capture timestamp the harness age gate considers live."""
    return int(_WALL_S * 1000)


# ── codec: byte-layout pinning ────────────────────────────────────────────────


def test_length_convention_is_one_plus_body() -> None:
    """length counts the type byte plus the body — never includes itself."""
    for frame in (
        FrameCodec.encode_control(_hello()),
        FrameCodec.encode_audio(1, 2, 3, b"\x00\x01" * 320),
        FrameCodec.encode_tts(3, PRIORITY_NORMAL, b"RIFFdata"),
    ):
        (length,) = struct.unpack(">I", frame[:4])
        assert length == len(frame) - 4  # type byte + body
        assert length == 1 + len(frame[5:])


def test_control_frame_exact_bytes() -> None:
    frame = FrameCodec.encode_control({"t": "leave", "guild_id": "42"})
    (length,) = struct.unpack(">I", frame[:4])
    assert frame[4] == TYPE_CONTROL
    body = frame[5:]
    assert length == 1 + len(body)
    assert json.loads(body.decode("utf-8")) == {"t": "leave", "guild_id": "42"}


def test_audio_frame_exact_bytes() -> None:
    """v2 layout: user_id, guild_id, capture timestamp (all u64 LE), PCM."""
    pcm = bytes(range(64)) * 10  # 640 bytes = one 20ms frame
    user_id = 0xDEADBEEFCAFE
    guild_id = 0x1122334455667788
    captured_ms = 1_700_000_000_123
    frame = FrameCodec.encode_audio(user_id, guild_id, captured_ms, pcm)
    assert frame[4] == TYPE_AUDIO
    body = frame[5:]
    assert struct.unpack_from("<Q", body, 0)[0] == user_id  # u64 LE
    assert struct.unpack_from("<Q", body, 8)[0] == guild_id  # u64 LE
    assert struct.unpack_from("<Q", body, 16)[0] == captured_ms  # u64 LE
    assert body[24:] == pcm


def test_tts_frame_exact_bytes() -> None:
    wav = b"RIFF\x00\x00\x00\x00WAVE"
    frame = FrameCodec.encode_tts(99, PRIORITY_ALERT, wav)
    assert frame[4] == TYPE_TTS
    body = frame[5:]
    assert struct.unpack_from("<Q", body, 0)[0] == 99
    assert body[8] == PRIORITY_ALERT
    assert body[9:] == wav


# ── codec: round-trips ────────────────────────────────────────────────────────


def test_round_trip_all_three_types() -> None:
    msg = {"t": "heartbeat", "ticks": 15021, "active_ssrcs": 4, "connected": True}
    pcm = b"\x01\x02" * 320
    wav = b"RIFF" + bytes(100)

    codec = FrameCodec()
    frames = codec.feed(
        FrameCodec.encode_control(msg)
        + FrameCodec.encode_audio(7, 8, 9, pcm)
        + FrameCodec.encode_tts(9, PRIORITY_NORMAL, wav)
    )
    assert frames == [
        ControlFrame(msg=msg),
        AudioFrame(user_id=7, guild_id=8, captured_ms=9, pcm=pcm),
        TtsFrame(guild_id=9, priority=PRIORITY_NORMAL, wav=wav),
    ]


def test_round_trip_big_audio_frame() -> None:
    """A multi-megabyte PCM payload survives intact (under the 8MB guard)."""
    rng = random.Random(1701)
    pcm = bytes(rng.getrandbits(8) for _ in range(2 * 1024 * 1024))  # 2 MB, even
    codec = FrameCodec()
    frames = codec.feed(FrameCodec.encode_audio(2**63, 2**64 - 1, 2**64 - 2, pcm))
    assert len(frames) == 1
    frame = frames[0]
    assert isinstance(frame, AudioFrame)
    assert frame.user_id == 2**63
    assert frame.guild_id == 2**64 - 1
    assert frame.captured_ms == 2**64 - 2
    assert frame.pcm == pcm


def test_round_trip_empty_pcm_and_empty_wav() -> None:
    codec = FrameCodec()
    frames = codec.feed(FrameCodec.encode_audio(1, 2, 3, b"") + FrameCodec.encode_tts(3, 0, b""))
    assert frames == [
        AudioFrame(user_id=1, guild_id=2, captured_ms=3, pcm=b""),
        TtsFrame(guild_id=3, priority=0, wav=b""),
    ]


# ── codec: partial-feed fuzzing ───────────────────────────────────────────────


def _sample_stream() -> tuple[bytes, list[Any]]:
    expected = [
        ControlFrame(msg=_hello()),
        AudioFrame(user_id=11, guild_id=22, captured_ms=33, pcm=b"\xff\x7f" * 480),
        ControlFrame(msg={"t": "speaking", "user_id": "11", "guild_id": "22", "state": "start"}),
        TtsFrame(guild_id=22, priority=PRIORITY_ALERT, wav=b"RIFF" + bytes(1234)),
        AudioFrame(user_id=11, guild_id=22, captured_ms=44, pcm=b""),
    ]
    stream = (
        FrameCodec.encode_control(expected[0].msg)
        + FrameCodec.encode_audio(11, 22, 33, expected[1].pcm)
        + FrameCodec.encode_control(expected[2].msg)
        + FrameCodec.encode_tts(22, PRIORITY_ALERT, expected[3].wav)
        + FrameCodec.encode_audio(11, 22, 44, b"")
    )
    return stream, expected


def test_feed_byte_by_byte() -> None:
    stream, expected = _sample_stream()
    codec = FrameCodec()
    got: list[Any] = []
    for i in range(len(stream)):
        got.extend(codec.feed(stream[i : i + 1]))
    assert got == expected


def test_feed_random_chunks() -> None:
    stream, expected = _sample_stream()
    for seed in range(20):
        rng = random.Random(seed)
        codec = FrameCodec()
        got: list[Any] = []
        pos = 0
        while pos < len(stream):
            step = rng.randint(1, 97)
            got.extend(codec.feed(stream[pos : pos + step]))
            pos += step
        assert got == expected, f"seed {seed}"


def test_feed_all_at_once_then_nothing_pending() -> None:
    stream, expected = _sample_stream()
    codec = FrameCodec()
    assert codec.feed(stream) == expected
    assert codec.feed(b"") == []


# ── codec: malformed input ────────────────────────────────────────────────────


def test_zero_length_frame_rejected() -> None:
    with pytest.raises(FrameDecodeError, match="length 0"):
        FrameCodec().feed(struct.pack(">I", 0))


def test_oversize_frame_rejected_before_buffering() -> None:
    header = struct.pack(">I", MAX_FRAME_BYTES + 1)
    with pytest.raises(FrameDecodeError, match="exceeds"):
        FrameCodec().feed(header)


def test_unknown_frame_type_rejected() -> None:
    with pytest.raises(FrameDecodeError, match="unknown frame type"):
        FrameCodec().feed(struct.pack(">I", 1) + b"\x7f")


def test_control_frame_bad_json_rejected() -> None:
    body = b"not json"
    frame = struct.pack(">I", 1 + len(body)) + bytes([TYPE_CONTROL]) + body
    with pytest.raises(FrameDecodeError, match="not UTF-8 JSON"):
        FrameCodec().feed(frame)


def test_control_frame_non_object_rejected() -> None:
    body = b"[1, 2, 3]"
    frame = struct.pack(">I", 1 + len(body)) + bytes([TYPE_CONTROL]) + body
    with pytest.raises(FrameDecodeError, match="JSON object"):
        FrameCodec().feed(frame)


def test_audio_frame_truncated_header_rejected() -> None:
    body = b"\x00" * 23  # v2 needs at least 24 (user + guild + timestamp)
    frame = struct.pack(">I", 1 + len(body)) + bytes([TYPE_AUDIO]) + body
    with pytest.raises(FrameDecodeError, match="too short"):
        FrameCodec().feed(frame)


def test_audio_frame_odd_pcm_rejected() -> None:
    body = b"\x00" * 25  # 24B header + 1 stray byte
    frame = struct.pack(">I", 1 + len(body)) + bytes([TYPE_AUDIO]) + body
    with pytest.raises(FrameDecodeError, match="odd byte count"):
        FrameCodec().feed(frame)


def test_encode_tts_priority_range() -> None:
    with pytest.raises(ValueError):
        FrameCodec.encode_tts(1, 256, b"")
    with pytest.raises(ValueError):
        FrameCodec.encode_tts(1, -1, b"")


# ── server ────────────────────────────────────────────────────────────────────


class _Harness:
    """IpcServer plus captured callbacks and controllable clocks."""

    def __init__(self, socket_path: str) -> None:
        self.path = socket_path
        self.audio: list[tuple[int, int, bytes]] = []
        self.control: list[dict] = []
        self.now = 100.0  # monotonic (heartbeat liveness)
        self.wall = _WALL_S  # wall clock (audio age gate)
        holder = SimpleNamespace(current=SimpleNamespace(ipc=SimpleNamespace(socket=socket_path)))
        self.server = IpcServer(
            holder,  # type: ignore[arg-type]
            on_audio=lambda u, g, p: self.audio.append((u, g, p)),
            on_control=self._on_control,
            clock=lambda: self.now,
            wall_clock=lambda: self.wall,
        )

    async def _on_control(self, msg: dict) -> None:
        self.control.append(msg)


async def _eventually(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


@pytest.fixture
async def harness(tmp_path):
    h = _Harness(str(tmp_path / "cortana.sock"))
    await h.server.start()
    yield h
    await h.server.stop()


async def test_server_dispatches_audio_and_control(harness: _Harness) -> None:
    reader, writer = await asyncio.open_unix_connection(harness.path)
    pcm = b"\x00\x01" * 320
    writer.write(FrameCodec.encode_control(_hello()))
    writer.write(FrameCodec.encode_audio(5, 6, _fresh_ms(), pcm))
    await writer.drain()

    await _eventually(lambda: harness.audio and harness.control)
    assert harness.control == [_hello()]
    assert harness.audio == [(5, 6, pcm)]
    writer.close()


# ── server: v2 handshake ─────────────────────────────────────────────────────


async def test_hello_version_mismatch_refused(harness: _Harness) -> None:
    """A hello carrying the wrong protocol version drops the client loudly."""
    reader, writer = await asyncio.open_unix_connection(harness.path)
    writer.write(FrameCodec.encode_control(_hello(proto=IPC_PROTOCOL_VERSION + 1)))
    await writer.drain()

    assert await reader.read() == b""  # EOF: refused
    await _eventually(lambda: not harness.server.connected)
    assert harness.server.protocol_mismatch
    assert harness.control == []  # the bad hello never reached the app
    writer.close()


async def test_hello_without_proto_refused(harness: _Harness) -> None:
    """A v1-shaped hello (no proto field) is a mismatch, not a pass."""
    reader, writer = await asyncio.open_unix_connection(harness.path)
    writer.write(FrameCodec.encode_control(_hello(proto=None)))
    await writer.drain()

    assert await reader.read() == b""
    assert harness.control == []
    writer.close()


async def test_matching_hello_clears_mismatch_flag(harness: _Harness) -> None:
    reader1, writer1 = await asyncio.open_unix_connection(harness.path)
    writer1.write(FrameCodec.encode_control(_hello(proto=1)))
    await writer1.drain()
    assert await reader1.read() == b""
    assert harness.server.protocol_mismatch
    writer1.close()

    reader2, writer2 = await asyncio.open_unix_connection(harness.path)
    writer2.write(FrameCodec.encode_control(_hello()))
    await writer2.drain()
    await _eventually(lambda: harness.control == [_hello()])
    assert not harness.server.protocol_mismatch
    writer2.close()


async def test_frames_before_hello_are_dropped(harness: _Harness) -> None:
    """Audio and non-hello control arriving before the handshake never reach
    the app; frames after a valid hello do."""
    reader, writer = await asyncio.open_unix_connection(harness.path)
    writer.write(FrameCodec.encode_audio(5, 6, _fresh_ms(), b"\x00\x01"))
    writer.write(FrameCodec.encode_control({"t": "left", "user_id": "5", "guild_id": "6"}))
    writer.write(FrameCodec.encode_control(_hello()))
    writer.write(FrameCodec.encode_audio(7, 8, _fresh_ms(), b"\x02\x03"))
    await writer.drain()

    await _eventually(lambda: harness.audio)
    assert harness.audio == [(7, 8, b"\x02\x03")]
    assert harness.control == [_hello()]
    writer.close()


# ── server: audio age gate ───────────────────────────────────────────────────


async def test_stale_audio_dropped_fresh_audio_passes(harness: _Harness) -> None:
    """Frames older than MAX_AUDIO_AGE_S (a post-outage ring flush) are
    dropped before wake/capture; live frames pass."""
    reader, writer = await asyncio.open_unix_connection(harness.path)
    stale_ms = int((_WALL_S - MAX_AUDIO_AGE_S - 7.0) * 1000)
    edge_ms = int((_WALL_S - MAX_AUDIO_AGE_S + 0.5) * 1000)  # inside the window
    writer.write(FrameCodec.encode_control(_hello()))
    writer.write(FrameCodec.encode_audio(1, 2, stale_ms, b"\x00\x01"))
    writer.write(FrameCodec.encode_audio(3, 4, edge_ms, b"\x02\x03"))
    writer.write(FrameCodec.encode_audio(5, 6, _fresh_ms(), b"\x04\x05"))
    await writer.drain()

    await _eventually(lambda: len(harness.audio) >= 2)
    await asyncio.sleep(0.05)  # the stale frame must never arrive late
    assert harness.audio == [(3, 4, b"\x02\x03"), (5, 6, b"\x04\x05")]
    writer.close()


# ── server: liveness ─────────────────────────────────────────────────────────


async def test_heartbeat_tracks_liveness_and_answers_hb_ack(harness: _Harness) -> None:
    assert harness.server.last_heartbeat is None
    assert not harness.server.is_alive(timeout=60.0)

    reader, writer = await asyncio.open_unix_connection(harness.path)
    hb = {"t": "heartbeat", "ticks": 1, "active_ssrcs": 0, "connected": True}
    writer.write(FrameCodec.encode_control(_hello()))
    writer.write(FrameCodec.encode_control(hb))
    await writer.drain()

    await _eventually(lambda: harness.server.last_heartbeat is not None)
    assert harness.server.last_heartbeat == harness.now
    assert harness.server.last_heartbeat_msg == hb
    assert harness.server.is_alive(timeout=10.0)
    harness.now += 30.0
    assert not harness.server.is_alive(timeout=10.0)

    # Brain answers every heartbeat with hb_ack — Ears' inbound-liveness
    # signal (a wedged Brain goes byte-silent and Ears reconnects).
    codec = FrameCodec()
    frames: list[Any] = []
    while not frames:
        frames.extend(codec.feed(await reader.read(4096)))
    assert ControlFrame(msg={"t": "hb_ack"}) in frames
    writer.close()


# ── server: outbound ─────────────────────────────────────────────────────────


async def test_send_tts_and_control_reach_client_and_return_true(harness: _Harness) -> None:
    reader, writer = await asyncio.open_unix_connection(harness.path)
    await _eventually(lambda: harness.server.connected)

    wav = b"RIFF" + bytes(64)
    assert await harness.server.send_tts(77, PRIORITY_ALERT, wav) is True
    assert await harness.server.send_control({"t": "leave", "guild_id": "77"}) is True

    codec = FrameCodec()
    frames: list[Any] = []
    while len(frames) < 2:
        frames.extend(codec.feed(await reader.read(4096)))
    assert frames[0] == TtsFrame(guild_id=77, priority=PRIORITY_ALERT, wav=wav)
    assert frames[1] == ControlFrame(msg={"t": "leave", "guild_id": "77"})
    writer.close()


async def test_send_without_client_returns_false(harness: _Harness) -> None:
    """No Ears attached: sends are dropped (never raise) and report False so
    callers engage the channel-text fallback."""
    assert await harness.server.send_tts(1, PRIORITY_NORMAL, b"RIFF") is False
    assert await harness.server.send_control({"t": "leave", "guild_id": "1"}) is False


async def test_new_connection_replaces_old(harness: _Harness) -> None:
    r1, w1 = await asyncio.open_unix_connection(harness.path)
    await _eventually(lambda: harness.server.connected)
    r2, w2 = await asyncio.open_unix_connection(harness.path)

    # The first connection is closed by the server (EOF on its reader)...
    assert await r1.read() == b""
    # ...and outbound traffic goes to the new one.
    await harness.server.send_control({"t": "leave", "guild_id": "1"})
    frames = FrameCodec().feed(await r2.read(4096))
    assert frames == [ControlFrame(msg={"t": "leave", "guild_id": "1"})]
    w1.close()
    w2.close()


async def test_decode_error_drops_connection(harness: _Harness) -> None:
    reader, writer = await asyncio.open_unix_connection(harness.path)
    await _eventually(lambda: harness.server.connected)
    # A length prefix over the 8MB guard: the server must log + drop us.
    writer.write(struct.pack(">I", MAX_FRAME_BYTES + 1))
    await writer.drain()
    assert await reader.read() == b""  # EOF: connection dropped
    await _eventually(lambda: not harness.server.connected)
    writer.close()


async def test_tts_frame_from_ears_is_a_protocol_error(harness: _Harness) -> None:
    reader, writer = await asyncio.open_unix_connection(harness.path)
    writer.write(FrameCodec.encode_tts(1, PRIORITY_NORMAL, b"RIFF"))
    await writer.drain()
    assert await reader.read() == b""  # wrong-direction frame → dropped
    writer.close()


async def test_socket_file_created_and_removed(tmp_path) -> None:
    path = tmp_path / "sub" / "dir" / "cortana.sock"
    h = _Harness(str(path))
    await h.server.start()
    assert path.exists()  # parent dirs were created too
    await h.server.stop()
    assert not path.exists()
