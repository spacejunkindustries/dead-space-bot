"""Framed Unix-domain-socket server linking Brain to Ears — GDD §15 (v2).

Brain BINDS the socket; Ears connects and reconnects with backoff. This
ordering is a hard invariant (CLAUDE.md): it means Ears buffers audio through
a Brain restart rather than the reverse. Never flip it.

Wire format (spans both languages — change both sides in the same commit):

    Frame: [4-byte BE u32 length][1-byte type][body]

    LENGTH CONVENTION: length = 1 + len(body). The length field counts every
    byte AFTER itself — the type byte plus the body. A frame with an empty
    body therefore has length 1, never 0. The Rust side (ears/src/ipc.rs)
    MUST use the same convention.

    type 0x01  CONTROL  body = UTF-8 JSON object
    type 0x02  AUDIO    Ears→Brain
               body = [8B user_id u64 LE][8B guild_id u64 LE]
                      [8B captured_at ms-since-epoch u64 LE]
                      [s16le PCM 16kHz mono]
    type 0x03  TTS      Brain→Ears
               body = [8B guild_id u64 LE][1B priority u8][WAV bytes]

Version handshake: the first frame on every connection must be Ears' ``hello``
carrying ``proto == IPC_PROTOCOL_VERSION``. On mismatch Brain logs loudly and
drops the client; Ears keeps retrying with backoff, so a desynced deploy is
noisy on both sides instead of silently half-working. Frames arriving before
a valid ``hello`` are dropped.

Age gate: the 0x02 capture timestamp (stamped by Ears at receipt) lets Brain
drop audio older than :data:`MAX_AUDIO_AGE_S` before it reaches wake/capture.
Ears buffers up to 60 s of audio through a Brain outage and flushes it on
reconnect; without the gate that flush replays stale wake words and feeds the
wall-clock endpointer a minute of audio arriving in milliseconds.

Control message shapes are exactly GDD §15: ``hello``, ``snapshot``,
``speaking``, ``left``, ``join_ok``, ``join_failed``, ``driver_disconnected``,
``heartbeat`` inbound; ``join``, ``leave``, ``optouts``, ``hb_ack`` outbound
(ids as strings). Ears liveness is tracked from ``heartbeat`` control
messages; Brain answers each heartbeat with ``hb_ack`` so Ears can detect a
wedged-but-connected Brain by inbound-byte silence.

Plane separation: the read loop does ONLY ``readexactly`` plus the sync audio
feed. Control frames are pushed onto an :class:`asyncio.Queue` consumed by an
independent task, so a slow control handler (gateway lock, Discord REST) can
never stall the 20 ms audio stream.

Only one Ears process exists, so the server holds a single client: a new
connection replaces (closes) the old one, which makes Ears restarts seamless.
On any decode error the connection is dropped — Ears reconnects with backoff
and the stream restarts cleanly framed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import structlog

from cortana.config import ConfigHolder

__all__ = [
    "IPC_PROTOCOL_VERSION",
    "MAX_AUDIO_AGE_S",
    "MAX_FRAME_BYTES",
    "PRIORITY_ALERT",
    "PRIORITY_LOW",
    "PRIORITY_NORMAL",
    "TYPE_AUDIO",
    "TYPE_CONTROL",
    "TYPE_TTS",
    "AudioFrame",
    "ControlFrame",
    "FrameCodec",
    "FrameDecodeError",
    "IpcServer",
    "TtsFrame",
]

log = structlog.get_logger(__name__)

#: IPC protocol version. Bump in LOCKSTEP with Ears'
#: ``ears/src/ipc.rs::IPC_PROTOCOL_VERSION`` (same commit — CLAUDE.md hard
#: rule). A ``hello`` carrying any other value is refused.
IPC_PROTOCOL_VERSION = 2

TYPE_CONTROL = 0x01
TYPE_AUDIO = 0x02
TYPE_TTS = 0x03

#: TTS playback priorities (GDD §15 type 0x03). Higher preempts queue order in
#: Ears' playback module.
PRIORITY_LOW = 0
PRIORITY_NORMAL = 1
PRIORITY_ALERT = 2

#: Upper bound on ``length`` (= 1 + len(body)). The largest legitimate frame
#: is a TTS WAV a few hundred KB long; 8 MB means a corrupt or hostile length
#: prefix cannot make us buffer unbounded garbage.
MAX_FRAME_BYTES = 8 * 1024 * 1024

#: Audio older than this (per the 0x02 capture timestamp) is dropped before it
#: reaches wake/capture. Generous against clock jitter and scheduling delay on
#: a live link (frames normally arrive within tens of milliseconds), but far
#: below Ears' 60 s outage ring — a post-reconnect flush can never replay
#: stale wake words or confuse the wall-clock endpointer. Would be
#: ``ipc.max_audio_age_s`` in cortana.yaml; module constant until the config
#: schema workstream lands (see scratchpad handoff note).
MAX_AUDIO_AGE_S = 3.0

#: Bound on one outbound socket drain. A UDS to a live local process drains
#: in microseconds; hitting this means Ears has stopped reading — see _send.
_SEND_STALL_S = 5.0

_LEN_STRUCT = struct.Struct(">I")  # 4-byte big-endian length prefix
_AUDIO_HEADER = struct.Struct("<QQQ")  # user_id, guild_id, captured_at_ms (u64 LE)
_TTS_HEADER = struct.Struct("<QB")  # guild_id u64 LE, priority u8


class FrameDecodeError(Exception):
    """A frame violated the wire format. The connection must be dropped."""


@dataclass(frozen=True, slots=True)
class ControlFrame:
    """Decoded type 0x01 frame: one JSON control object."""

    msg: dict


@dataclass(frozen=True, slots=True)
class AudioFrame:
    """Decoded type 0x02 frame: one user's PCM chunk (16 kHz mono s16le).

    ``captured_ms`` is milliseconds since the Unix epoch, stamped by Ears at
    receipt — the age-gate input.
    """

    user_id: int
    guild_id: int
    captured_ms: int
    pcm: bytes


@dataclass(frozen=True, slots=True)
class TtsFrame:
    """Decoded type 0x03 frame: WAV bytes for playback in a guild."""

    guild_id: int
    priority: int
    wav: bytes


Frame = ControlFrame | AudioFrame | TtsFrame


class FrameCodec:
    """Streaming encoder/decoder for the GDD §15 frame format.

    LENGTH CONVENTION: the 4-byte big-endian length prefix equals
    ``1 + len(body)`` — it counts the type byte plus the body, i.e. every
    byte after the length field itself. An empty-body frame has length 1.
    The Rust peer (ears/src/ipc.rs) must match this exactly.

    :meth:`feed` is tolerant of arbitrary partial reads: bytes are buffered
    internally and complete frames are returned as they materialise, so the
    caller may feed data one byte at a time or a megabyte at a time.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    # ── encoding ─────────────────────────────────────────────────────────────

    @staticmethod
    def _frame(frame_type: int, body: bytes) -> bytes:
        # length = 1 (type byte) + len(body); see the class docstring.
        return _LEN_STRUCT.pack(1 + len(body)) + bytes([frame_type]) + body

    @staticmethod
    def encode_control(msg: dict) -> bytes:
        """Encode a type 0x01 frame from a JSON-serialisable control object."""
        return FrameCodec._frame(TYPE_CONTROL, json.dumps(msg, separators=(",", ":")).encode())

    @staticmethod
    def encode_audio(user_id: int, guild_id: int, captured_ms: int, pcm: bytes) -> bytes:
        """Encode a type 0x02 frame (used by tests and any Python Ears stand-in)."""
        return FrameCodec._frame(
            TYPE_AUDIO, _AUDIO_HEADER.pack(user_id, guild_id, captured_ms) + pcm
        )

    @staticmethod
    def encode_tts(guild_id: int, priority: int, wav: bytes) -> bytes:
        """Encode a type 0x03 frame carrying in-memory WAV bytes."""
        if not 0 <= priority <= 0xFF:
            raise ValueError(f"priority must fit in one byte, got {priority}")
        return FrameCodec._frame(TYPE_TTS, _TTS_HEADER.pack(guild_id, priority) + wav)

    # ── decoding ─────────────────────────────────────────────────────────────

    @staticmethod
    def decode_body(frame_type: int, body: bytes) -> Frame:
        """Decode one frame's payload (everything after the length + type bytes).

        Raises :class:`FrameDecodeError` on any wire-format violation.
        """
        if frame_type == TYPE_CONTROL:
            try:
                msg = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FrameDecodeError(f"control frame is not UTF-8 JSON: {exc}") from exc
            if not isinstance(msg, dict):
                raise FrameDecodeError(
                    f"control frame must be a JSON object, got {type(msg).__name__}"
                )
            return ControlFrame(msg=msg)

        if frame_type == TYPE_AUDIO:
            if len(body) < _AUDIO_HEADER.size:
                raise FrameDecodeError(f"audio frame body too short: {len(body)} bytes")
            user_id, guild_id, captured_ms = _AUDIO_HEADER.unpack_from(body)
            pcm = body[_AUDIO_HEADER.size :]
            if len(pcm) % 2 != 0:
                raise FrameDecodeError("audio frame PCM has an odd byte count (not s16le)")
            return AudioFrame(user_id=user_id, guild_id=guild_id, captured_ms=captured_ms, pcm=pcm)

        if frame_type == TYPE_TTS:
            if len(body) < _TTS_HEADER.size:
                raise FrameDecodeError(f"tts frame body too short: {len(body)} bytes")
            guild_id, priority = _TTS_HEADER.unpack_from(body)
            return TtsFrame(guild_id=guild_id, priority=priority, wav=body[_TTS_HEADER.size :])

        raise FrameDecodeError(f"unknown frame type 0x{frame_type:02x}")

    def feed(self, data: bytes) -> list[Frame]:
        """Buffer ``data`` and return every complete frame now available.

        Handles arbitrary fragmentation. Raises :class:`FrameDecodeError` on a
        malformed frame; the codec is then poisoned and the caller must drop
        the connection (there is no way to resynchronise a byte stream).
        """
        self._buf.extend(data)
        frames: list[Frame] = []
        while True:
            if len(self._buf) < _LEN_STRUCT.size:
                return frames
            (length,) = _LEN_STRUCT.unpack_from(self._buf)
            if length < 1:
                raise FrameDecodeError("frame length 0 (must be >= 1: the type byte)")
            if length > MAX_FRAME_BYTES:
                raise FrameDecodeError(f"frame length {length} exceeds {MAX_FRAME_BYTES}")
            if len(self._buf) < _LEN_STRUCT.size + length:
                return frames
            frame_type = self._buf[_LEN_STRUCT.size]
            body = bytes(self._buf[_LEN_STRUCT.size + 1 : _LEN_STRUCT.size + length])
            del self._buf[: _LEN_STRUCT.size + length]
            frames.append(self.decode_body(frame_type, body))


class IpcServer:
    """Asyncio UDS server for the Ears link. Brain binds; Ears connects.

    ``on_audio(user_id, guild_id, pcm)`` is a SYNC callback on the hot path —
    it must never block (it feeds :class:`~cortana.audio.capture.CaptureManager`).
    ``on_control(msg)`` is awaited per message by an independent consumer
    task fed from a queue — the read loop itself never awaits a handler, so
    a slow control handler cannot stall the 20 ms audio stream.

    Outbound traffic goes through :meth:`send_control` / :meth:`send_tts`;
    both return ``True`` when the frame was handed to a connected client and
    ``False`` when there is no client or the write failed (Ears is the
    buffering side, per the bind/connect ordering above) — callers use the
    bool to engage their text fallbacks instead of failing silent.

    Handshake: the first frame of every connection must be a ``hello`` with
    ``proto == IPC_PROTOCOL_VERSION``; a mismatch is logged loudly and the
    client is refused (dropped — Ears retries with backoff). Audio frames
    are additionally age-gated on their capture timestamp
    (:data:`MAX_AUDIO_AGE_S`) so a post-outage ring flush cannot replay
    stale speech into wake/capture.

    Liveness: every ``{"t": "heartbeat", ...}`` control message stamps
    :attr:`last_heartbeat` (monotonic seconds); :meth:`is_alive` compares it
    against a timeout, and the control consumer answers with ``hb_ack`` so
    Ears can detect a wedged Brain. The clocks are injectable for tests.
    """

    def __init__(
        self,
        holder: ConfigHolder,
        on_audio: Callable[[int, int, bytes], None],
        on_control: Callable[[dict], Awaitable[None]],
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._holder = holder
        self._on_audio = on_audio
        self._on_control = on_control
        self._clock = clock
        self._wall_clock = wall_clock
        self._server: asyncio.AbstractServer | None = None
        self._socket_path: Path | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._send_lock = asyncio.Lock()
        self._last_heartbeat: float | None = None
        self._last_heartbeat_msg: dict | None = None
        self._conn_seq = 0
        self._handshaken = False
        self._protocol_mismatch = False
        self._stale_audio_dropped = 0
        self._prehello_dropped = 0
        self._control_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._control_task: asyncio.Task[None] | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Unlink any stale socket, bind, chmod 0660, and start accepting."""
        path = Path(self._holder.current.ipc.socket)
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        self._server = await asyncio.start_unix_server(self._on_connect, path=str(path))
        # Group-writable so the ears user (same aura group) can connect.
        os.chmod(path, 0o660)
        self._socket_path = path
        self._control_task = asyncio.create_task(
            self._control_consumer(), name="ipc-control-consumer"
        )
        log.info("ipc_listening", socket=str(path))

    async def stop(self) -> None:
        """Stop accepting, drop the client, and remove the socket file."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._control_task is not None:
            self._control_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._control_task
            self._control_task = None
        await self._drop_client("server stopping")
        if self._socket_path is not None:
            with contextlib.suppress(OSError):
                self._socket_path.unlink()
            self._socket_path = None
        log.info("ipc_stopped")

    @property
    def connected(self) -> bool:
        """True while an Ears connection is attached."""
        return self._writer is not None and not self._writer.is_closing()

    @property
    def last_heartbeat(self) -> float | None:
        """Monotonic timestamp of the last Ears heartbeat, or None if never seen."""
        return self._last_heartbeat

    @property
    def last_heartbeat_msg(self) -> dict | None:
        """The most recent heartbeat control message verbatim (GDD §15)."""
        return self._last_heartbeat_msg

    @property
    def protocol_mismatch(self) -> bool:
        """True once a client was refused for a protocol-version mismatch.

        A deploy hook for the ops surface: this staying True means the
        Ears/Brain pair on disk is desynced and needs a lockstep deploy.
        """
        return self._protocol_mismatch

    def is_alive(self, timeout: float) -> bool:
        """True if a heartbeat arrived within ``timeout`` seconds (monotonic)."""
        if self._last_heartbeat is None:
            return False
        return (self._clock() - self._last_heartbeat) <= timeout

    # ── outbound ─────────────────────────────────────────────────────────────

    async def send_control(self, msg: dict) -> bool:
        """Send a type 0x01 JSON control frame (``join``/``leave``/``optouts``).

        Returns ``True`` when handed to a connected Ears, else ``False``.
        """
        return await self._send(FrameCodec.encode_control(msg), kind="control", t=msg.get("t"))

    async def send_tts(self, guild_id: int, priority: int, wav_bytes: bytes) -> bool:
        """Send a type 0x03 TTS frame with in-memory WAV bytes.

        Returns ``True`` when handed to a connected Ears, ``False`` when no
        client is attached or the write failed — the caller must treat the
        utterance as unspoken and fall back to channel text.
        """
        return await self._send(
            FrameCodec.encode_tts(guild_id, priority, wav_bytes),
            kind="tts",
            guild_id=guild_id,
            priority=priority,
            wav_bytes=len(wav_bytes),
        )

    async def _send(self, frame: bytes, **log_fields: object) -> bool:
        async with self._send_lock:
            writer = self._writer
            if writer is None or writer.is_closing():
                log.warning("ipc_send_dropped_no_client", **log_fields)
                return False
            try:
                writer.write(frame)
                # Bounded drain: an Ears that stops reading but keeps the
                # socket open would otherwise park this await forever WITH
                # the send lock held — every later join/leave/TTS frame then
                # queues behind it and voice control dies silently. Past the
                # bound we drop the connection; Ears reconnects with backoff.
                await asyncio.wait_for(writer.drain(), timeout=_SEND_STALL_S)
            except TimeoutError:
                log.warning("ipc_send_stalled", stall_s=_SEND_STALL_S, **log_fields)
                await self._drop_client("send stalled")
                return False
            except (ConnectionError, OSError) as exc:
                log.warning("ipc_send_failed", error=str(exc), **log_fields)
                await self._drop_client("write failed")
                return False
            return True

    # ── inbound ──────────────────────────────────────────────────────────────

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._conn_seq += 1
        seq = self._conn_seq
        # Single client: a new Ears connection replaces the old one.
        if self._writer is not None:
            log.info("ipc_client_replaced", conn=seq)
            await self._drop_client("replaced by new connection")
        self._writer = writer
        self._handshaken = False
        self._stale_audio_dropped = 0
        self._prehello_dropped = 0
        log.info("ipc_client_connected", conn=seq)
        try:
            await self._read_loop(reader)
        except asyncio.IncompleteReadError:
            log.info("ipc_client_disconnected", conn=seq)
        except FrameDecodeError as exc:
            # Constraint: on decode error, log + drop; Ears reconnects cleanly.
            log.error("ipc_decode_error", conn=seq, error=str(exc))
        except (ConnectionError, OSError) as exc:
            log.warning("ipc_client_io_error", conn=seq, error=str(exc))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ipc_reader_crashed", conn=seq)
        finally:
            if self._writer is writer:
                self._writer = None
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        # Hot path: readexactly + decode + sync dispatch only. Control frames
        # go to the queue; nothing here awaits a handler.
        while True:
            header = await reader.readexactly(_LEN_STRUCT.size)
            (length,) = _LEN_STRUCT.unpack(header)
            if length < 1:
                raise FrameDecodeError("frame length 0 (must be >= 1: the type byte)")
            if length > MAX_FRAME_BYTES:
                raise FrameDecodeError(f"frame length {length} exceeds {MAX_FRAME_BYTES}")
            payload = await reader.readexactly(length)
            frame = FrameCodec.decode_body(payload[0], payload[1:])
            self._dispatch(frame)

    def _dispatch(self, frame: Frame) -> None:
        if isinstance(frame, AudioFrame):
            if not self._handshaken:
                self._prehello_dropped += 1
                if self._prehello_dropped == 1:
                    log.warning("ipc_audio_before_hello_dropped")
                return
            # Age gate: stale audio (a ring flush after an outage, or a
            # peer with a broken clock) must never reach wake/capture.
            age_s = self._wall_clock() - frame.captured_ms / 1000.0
            if age_s > MAX_AUDIO_AGE_S:
                self._stale_audio_dropped += 1
                if self._stale_audio_dropped == 1 or self._stale_audio_dropped % 500 == 0:
                    log.warning(
                        "ipc_stale_audio_dropped",
                        age_s=round(age_s, 1),
                        max_age_s=MAX_AUDIO_AGE_S,
                        dropped=self._stale_audio_dropped,
                    )
                return
            # Hot path: sync callback, no awaits, no logging.
            self._on_audio(frame.user_id, frame.guild_id, frame.pcm)
            return
        if isinstance(frame, ControlFrame):
            self._dispatch_control(frame.msg)
            return
        # TTS frames are Brain→Ears only; receiving one means the peer is confused.
        raise FrameDecodeError("received a type 0x03 TTS frame from Ears (wrong direction)")

    def _dispatch_control(self, msg: dict) -> None:
        t = msg.get("t")
        if not self._handshaken:
            if t != "hello":
                self._prehello_dropped += 1
                log.warning("ipc_control_before_hello_dropped", t=t)
                return
            proto = msg.get("proto")
            if proto != IPC_PROTOCOL_VERSION:
                # LOUD: a desynced Ears/Brain pair on disk. Refuse the client;
                # Ears keeps retrying with backoff, so this line repeats until
                # the pair is deployed in lockstep.
                self._protocol_mismatch = True
                log.error(
                    "ipc_protocol_mismatch",
                    brain_proto=IPC_PROTOCOL_VERSION,
                    ears_proto=proto,
                    ears_version=msg.get("version"),
                    fix="deploy ears and brain from the same commit (GDD §15)",
                )
                raise FrameDecodeError(
                    f"protocol mismatch: ears speaks {proto!r}, brain speaks "
                    f"{IPC_PROTOCOL_VERSION}; refusing client"
                )
            self._handshaken = True
            self._protocol_mismatch = False
        if t == "heartbeat":
            self._last_heartbeat = self._clock()
            self._last_heartbeat_msg = msg
        self._control_queue.put_nowait(msg)

    async def _control_consumer(self) -> None:
        """Drain the control queue independently of the audio read loop.

        Answers heartbeats with ``hb_ack`` (Ears' inbound-liveness signal),
        then hands the message to the app's ``on_control``. A crashing
        handler is logged and skipped — it must never kill this task, or
        every later join/leave/hello would silently queue forever.
        """
        while True:
            msg = await self._control_queue.get()
            if msg.get("t") == "heartbeat":
                await self.send_control({"t": "hb_ack"})
            try:
                await self._on_control(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ipc_control_handler_crashed", t=msg.get("t"))

    async def _drop_client(self, reason: str) -> None:
        writer, self._writer = self._writer, None
        if writer is None:
            return
        self._handshaken = False
        log.info("ipc_client_dropped", reason=reason)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
