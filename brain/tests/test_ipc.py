"""Tests for the GDD §15 frame codec and the Brain-side UDS server.

The wire format spans both languages — these tests pin the exact byte layout
(including the length = 1 + len(body) convention) so a Rust-side change that
desyncs the framing fails loudly here.
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

# ── codec: byte-layout pinning ────────────────────────────────────────────────


def test_length_convention_is_one_plus_body() -> None:
    """length counts the type byte plus the body — never includes itself."""
    for frame in (
        FrameCodec.encode_control({"t": "hello", "version": "1.0"}),
        FrameCodec.encode_audio(1, 2, b"\x00\x01" * 320),
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
    pcm = bytes(range(64)) * 10  # 640 bytes = one 20ms frame
    user_id = 0xDEADBEEFCAFE
    guild_id = 0x1122334455667788
    frame = FrameCodec.encode_audio(user_id, guild_id, pcm)
    assert frame[4] == TYPE_AUDIO
    body = frame[5:]
    assert struct.unpack_from("<Q", body, 0)[0] == user_id  # u64 LE
    assert struct.unpack_from("<Q", body, 8)[0] == guild_id  # u64 LE
    assert body[16:] == pcm


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
        + FrameCodec.encode_audio(7, 8, pcm)
        + FrameCodec.encode_tts(9, PRIORITY_NORMAL, wav)
    )
    assert frames == [
        ControlFrame(msg=msg),
        AudioFrame(user_id=7, guild_id=8, pcm=pcm),
        TtsFrame(guild_id=9, priority=PRIORITY_NORMAL, wav=wav),
    ]


def test_round_trip_big_audio_frame() -> None:
    """A multi-megabyte PCM payload survives intact (under the 8MB guard)."""
    rng = random.Random(1701)
    pcm = bytes(rng.getrandbits(8) for _ in range(2 * 1024 * 1024))  # 2 MB, even
    codec = FrameCodec()
    frames = codec.feed(FrameCodec.encode_audio(2**63, 2**64 - 1, pcm))
    assert len(frames) == 1
    frame = frames[0]
    assert isinstance(frame, AudioFrame)
    assert frame.user_id == 2**63
    assert frame.guild_id == 2**64 - 1
    assert frame.pcm == pcm


def test_round_trip_empty_pcm_and_empty_wav() -> None:
    codec = FrameCodec()
    frames = codec.feed(FrameCodec.encode_audio(1, 2, b"") + FrameCodec.encode_tts(3, 0, b""))
    assert frames == [
        AudioFrame(user_id=1, guild_id=2, pcm=b""),
        TtsFrame(guild_id=3, priority=0, wav=b""),
    ]


# ── codec: partial-feed fuzzing ───────────────────────────────────────────────


def _sample_stream() -> tuple[bytes, list[Any]]:
    expected = [
        ControlFrame(msg={"t": "hello", "version": "1.0"}),
        AudioFrame(user_id=11, guild_id=22, pcm=b"\xff\x7f" * 480),
        ControlFrame(msg={"t": "speaking", "user_id": "11", "guild_id": "22", "state": "start"}),
        TtsFrame(guild_id=22, priority=PRIORITY_ALERT, wav=b"RIFF" + bytes(1234)),
        AudioFrame(user_id=11, guild_id=22, pcm=b""),
    ]
    stream = (
        FrameCodec.encode_control(expected[0].msg)
        + FrameCodec.encode_audio(11, 22, expected[1].pcm)
        + FrameCodec.encode_control(expected[2].msg)
        + FrameCodec.encode_tts(22, PRIORITY_ALERT, expected[3].wav)
        + FrameCodec.encode_audio(11, 22, b"")
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
    body = b"\x00" * 15  # needs at least 16
    frame = struct.pack(">I", 1 + len(body)) + bytes([TYPE_AUDIO]) + body
    with pytest.raises(FrameDecodeError, match="too short"):
        FrameCodec().feed(frame)


def test_audio_frame_odd_pcm_rejected() -> None:
    body = b"\x00" * 17  # 16B header + 1 stray byte
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
    """IpcServer plus captured callbacks and a controllable clock."""

    def __init__(self, socket_path: str) -> None:
        self.path = socket_path
        self.audio: list[tuple[int, int, bytes]] = []
        self.control: list[dict] = []
        self.now = 100.0
        holder = SimpleNamespace(current=SimpleNamespace(ipc=SimpleNamespace(socket=socket_path)))
        self.server = IpcServer(
            holder,  # type: ignore[arg-type]
            on_audio=lambda u, g, p: self.audio.append((u, g, p)),
            on_control=self._on_control,
            clock=lambda: self.now,
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
    writer.write(FrameCodec.encode_control({"t": "hello", "version": "1.0"}))
    writer.write(FrameCodec.encode_audio(5, 6, pcm))
    await writer.drain()

    await _eventually(lambda: harness.audio and harness.control)
    assert harness.control == [{"t": "hello", "version": "1.0"}]
    assert harness.audio == [(5, 6, pcm)]
    writer.close()


async def test_heartbeat_tracks_liveness(harness: _Harness) -> None:
    assert harness.server.last_heartbeat is None
    assert not harness.server.is_alive(timeout=60.0)

    _, writer = await asyncio.open_unix_connection(harness.path)
    hb = {"t": "heartbeat", "ticks": 1, "active_ssrcs": 0, "connected": True}
    writer.write(FrameCodec.encode_control(hb))
    await writer.drain()

    await _eventually(lambda: harness.server.last_heartbeat is not None)
    assert harness.server.last_heartbeat == harness.now
    assert harness.server.last_heartbeat_msg == hb
    assert harness.server.is_alive(timeout=10.0)
    harness.now += 30.0
    assert not harness.server.is_alive(timeout=10.0)
    writer.close()


async def test_send_tts_and_control_reach_client(harness: _Harness) -> None:
    reader, writer = await asyncio.open_unix_connection(harness.path)
    await _eventually(lambda: harness.server.connected)

    wav = b"RIFF" + bytes(64)
    await harness.server.send_tts(77, PRIORITY_ALERT, wav)
    await harness.server.send_control({"t": "leave", "guild_id": "77"})

    codec = FrameCodec()
    frames: list[Any] = []
    while len(frames) < 2:
        frames.extend(codec.feed(await reader.read(4096)))
    assert frames[0] == TtsFrame(guild_id=77, priority=PRIORITY_ALERT, wav=wav)
    assert frames[1] == ControlFrame(msg={"t": "leave", "guild_id": "77"})
    writer.close()


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


async def test_send_without_client_is_dropped_not_raised(harness: _Harness) -> None:
    await harness.server.send_tts(1, PRIORITY_NORMAL, b"RIFF")
    await harness.server.send_control({"t": "leave", "guild_id": "1"})


async def test_socket_file_created_and_removed(tmp_path) -> None:
    path = tmp_path / "sub" / "dir" / "cortana.sock"
    h = _Harness(str(path))
    await h.server.start()
    assert path.exists()  # parent dirs were created too
    await h.server.stop()
    assert not path.exists()
