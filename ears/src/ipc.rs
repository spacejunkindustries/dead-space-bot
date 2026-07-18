//! Framed Unix-domain-socket client to Brain (GDD §15, protocol v2).
//!
//! Brain binds the socket; Ears connects and reconnects with exponential
//! backoff forever. Outbound audio ALWAYS flows through a bounded ring
//! buffer (connected or not) so a wedged-but-connected Brain costs bounded,
//! observable audio loss instead of unbounded memory. The ring is only
//! flushed — and the backoff only reset — after Brain PROVES liveness by
//! sending its first control frame back; a crash-looping Brain that accepts
//! the socket but never reads cannot destroy the buffered audio.
//!
//! ```text
//! Frame: [4-byte BE length][1-byte type][body]
//!        length = 1 + len(body)  (every byte after the length field)
//! 0x01 CONTROL  UTF-8 JSON object
//! 0x02 AUDIO    Ears→Brain  [8B user_id LE][8B guild_id LE]
//!                           [8B captured_at ms-since-epoch LE]
//!                           [i16 LE PCM 16k mono]
//! 0x03 TTS      Brain→Ears  [8B guild_id LE][1B priority][WAV bytes]
//! ```
//!
//! Handshake: the first frame after connect is `hello` carrying
//! [`IPC_PROTOCOL_VERSION`]; a mismatched Brain refuses the connection and
//! Ears keeps retrying with backoff (loud on both sides, silent on neither).
//! `hello` is followed by a full state `snapshot` (per-guild connected state
//! plus the SSRC↔user roster) so Brain reconciles against truth instead of
//! replaying lossy event deltas.

use std::collections::{HashSet, VecDeque};
use std::path::Path;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::Result;
use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::unix::OwnedWriteHalf;
use tokio::net::UnixStream;
use tokio::sync::mpsc;
use tokio::time::{sleep_until, timeout, Instant};
use tracing::{debug, info, warn};

use crate::playback::TtsJob;
use crate::state::Shared;
use crate::voice::VoiceCmd;

/// IPC protocol version. Bump in LOCKSTEP with Brain's
/// `cortana.ipc.IPC_PROTOCOL_VERSION` (same commit — CLAUDE.md hard rule);
/// Brain refuses a client whose `hello` carries any other value.
pub const IPC_PROTOCOL_VERSION: u32 = 2;

/// Frame type: JSON control message.
pub const TYPE_CONTROL: u8 = 0x01;
/// Frame type: audio, Ears→Brain.
pub const TYPE_AUDIO: u8 = 0x02;
/// Frame type: TTS WAV, Brain→Ears.
pub const TYPE_TTS: u8 = 0x03;

/// Upper bound on a single frame's `length` field. A 3 s Piper WAV at
/// 22.05 kHz s16 is ~130 KiB; 8 MiB is generous headroom while still catching
/// stream desync quickly.
pub const MAX_FRAME_LEN: usize = 8 * 1024 * 1024;

/// Interval between heartbeat control messages while connected.
const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(5);
/// Reconnect backoff bounds.
const BACKOFF_MIN: Duration = Duration::from_millis(500);
const BACKOFF_MAX: Duration = Duration::from_secs(30);
/// Longest a single socket write may take. A UDS to a live local reader
/// drains in microseconds; missing this deadline means Brain has stopped
/// reading while keeping the socket open — treat the connection as Lost
/// instead of parking the pump inside `write_all` forever.
const WRITE_DEADLINE: Duration = Duration::from_secs(5);
/// Peer liveness: Brain acknowledges every Ears heartbeat with `hb_ack`
/// (GDD §15), so a healthy link carries inbound bytes at least every
/// [`HEARTBEAT_INTERVAL`]. This many seconds without ANY inbound bytes marks
/// the connection Lost and reconnects.
const INBOUND_LIVENESS: Duration = Duration::from_secs(20);
/// Cadence of the inbound-liveness check.
const LIVENESS_CHECK: Duration = Duration::from_secs(1);

/// Milliseconds since the Unix epoch "now" — the 0x02 capture timestamp.
/// Ears stamps audio at receipt; Brain age-gates on it (judgement lives in
/// Brain — Ears just stamps the truth).
#[must_use]
pub fn epoch_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Wire-format errors. Any of these tears down the connection: a framing
/// error means the stream is desynced and nothing after it can be trusted.
#[derive(Debug, thiserror::Error)]
pub enum CodecError {
    #[error("frame length {0} exceeds maximum {MAX_FRAME_LEN}")]
    FrameTooLarge(usize),
    #[error("frame length must be >= 1 (type byte)")]
    EmptyFrame,
    #[error("unknown frame type {0:#04x}")]
    UnknownType(u8),
    #[error("audio frame body too short: {0} bytes")]
    ShortAudio(usize),
    #[error("TTS frame body too short: {0} bytes")]
    ShortTts(usize),
    #[error("control frame is not valid JSON: {0}")]
    BadJson(#[from] serde_json::Error),
}

/// A decoded inbound frame.
#[derive(Debug, Clone, PartialEq)]
pub enum Frame {
    Control(Value),
    Audio {
        user_id: u64,
        guild_id: u64,
        captured_ms: u64,
        pcm: Vec<i16>,
    },
    Tts {
        guild_id: u64,
        priority: u8,
        wav: Vec<u8>,
    },
}

/// Streaming frame decoder, tolerant of partial reads.
#[derive(Debug, Default)]
pub struct FrameCodec {
    buf: Vec<u8>,
}

impl FrameCodec {
    /// Append raw bytes and decode every complete frame available.
    pub fn feed(&mut self, data: &[u8]) -> Result<Vec<Frame>, CodecError> {
        self.buf.extend_from_slice(data);
        let mut frames = Vec::new();
        loop {
            if self.buf.len() < 4 {
                break;
            }
            let len = u32::from_be_bytes([self.buf[0], self.buf[1], self.buf[2], self.buf[3]])
                as usize;
            if len == 0 {
                return Err(CodecError::EmptyFrame);
            }
            if len > MAX_FRAME_LEN {
                return Err(CodecError::FrameTooLarge(len));
            }
            if self.buf.len() < 4 + len {
                break;
            }
            let frame_type = self.buf[4];
            let body = self.buf[5..4 + len].to_vec();
            self.buf.drain(..4 + len);
            frames.push(Self::decode_body(frame_type, &body)?);
        }
        Ok(frames)
    }

    fn decode_body(frame_type: u8, body: &[u8]) -> Result<Frame, CodecError> {
        match frame_type {
            TYPE_CONTROL => Ok(Frame::Control(serde_json::from_slice(body)?)),
            TYPE_AUDIO => {
                if body.len() < 24 {
                    return Err(CodecError::ShortAudio(body.len()));
                }
                let user_id = u64::from_le_bytes(
                    body[0..8].try_into().expect("slice length is 8"),
                );
                let guild_id = u64::from_le_bytes(
                    body[8..16].try_into().expect("slice length is 8"),
                );
                let captured_ms = u64::from_le_bytes(
                    body[16..24].try_into().expect("slice length is 8"),
                );
                let pcm = body[24..]
                    .chunks_exact(2)
                    .map(|b| i16::from_le_bytes([b[0], b[1]]))
                    .collect();
                Ok(Frame::Audio {
                    user_id,
                    guild_id,
                    captured_ms,
                    pcm,
                })
            },
            TYPE_TTS => {
                if body.len() < 9 {
                    return Err(CodecError::ShortTts(body.len()));
                }
                let guild_id = u64::from_le_bytes(
                    body[0..8].try_into().expect("slice length is 8"),
                );
                let priority = body[8];
                Ok(Frame::Tts {
                    guild_id,
                    priority,
                    wav: body[9..].to_vec(),
                })
            },
            other => Err(CodecError::UnknownType(other)),
        }
    }

    /// Encode a JSON control frame.
    #[must_use]
    pub fn encode_control(msg: &Value) -> Vec<u8> {
        let body = msg.to_string().into_bytes();
        Self::encode_raw(TYPE_CONTROL, &body)
    }

    /// Encode an audio frame (Ears→Brain). `captured_ms` is milliseconds
    /// since the Unix epoch, stamped at receipt (see [`epoch_ms`]).
    #[must_use]
    pub fn encode_audio(user_id: u64, guild_id: u64, captured_ms: u64, pcm: &[i16]) -> Vec<u8> {
        let mut body = Vec::with_capacity(24 + pcm.len() * 2);
        body.extend_from_slice(&user_id.to_le_bytes());
        body.extend_from_slice(&guild_id.to_le_bytes());
        body.extend_from_slice(&captured_ms.to_le_bytes());
        for sample in pcm {
            body.extend_from_slice(&sample.to_le_bytes());
        }
        Self::encode_raw(TYPE_AUDIO, &body)
    }

    /// Encode a TTS frame. TTS flows Brain→Ears, so production Ears code
    /// never sends one; it exists to round-trip-test the wire format.
    #[cfg(test)]
    #[must_use]
    pub fn encode_tts(guild_id: u64, priority: u8, wav: &[u8]) -> Vec<u8> {
        let mut body = Vec::with_capacity(9 + wav.len());
        body.extend_from_slice(&guild_id.to_le_bytes());
        body.push(priority);
        body.extend_from_slice(wav);
        Self::encode_raw(TYPE_TTS, &body)
    }

    fn encode_raw(frame_type: u8, body: &[u8]) -> Vec<u8> {
        let len = (1 + body.len()) as u32;
        let mut out = Vec::with_capacity(4 + 1 + body.len());
        out.extend_from_slice(&len.to_be_bytes());
        out.push(frame_type);
        out.extend_from_slice(body);
        out
    }
}

/// The `hello` control message opening every connection (GDD §15).
#[must_use]
pub fn hello_msg() -> Value {
    json!({
        "t": "hello",
        "proto": IPC_PROTOCOL_VERSION,
        "version": env!("CARGO_PKG_VERSION"),
    })
}

/// The full-state `snapshot` control message sent right after `hello`:
/// per-guild connected state plus the SSRC↔user roster, replacing lossy
/// event deltas so Brain reconciles instead of guessing (GDD §15).
#[must_use]
pub fn snapshot_msg(shared: &Shared) -> Value {
    let guilds: Vec<Value> = shared
        .guilds_snapshot()
        .iter()
        .map(|g| {
            let channel = match g.channel_id() {
                0 => Value::Null,
                c => Value::String(c.to_string()),
            };
            let users: Vec<Value> = g
                .roster()
                .into_iter()
                .map(|(ssrc, user_id)| {
                    json!({ "ssrc": ssrc, "user_id": user_id.to_string() })
                })
                .collect();
            json!({
                "guild_id": g.guild_id.to_string(),
                "channel_id": channel,
                "connected": g.is_connected(),
                "users": users,
            })
        })
        .collect();
    json!({ "t": "snapshot", "guilds": guilds })
}

/// Whether the ring may be flushed this iteration: only once Brain has
/// proven liveness, and only while there is something to send.
#[must_use]
pub fn should_flush(brain_live: bool, ring_empty: bool) -> bool {
    brain_live && !ring_empty
}

/// An already-encoded outbound frame, tagged so the ring buffer knows what to
/// keep across a disconnect (audio is buffered; control is point-in-time and
/// superseded by the snapshot on reconnect).
#[derive(Debug, Clone)]
pub enum Outbound {
    Audio(Vec<u8>),
    Control(Vec<u8>),
}

/// Bounded FIFO of encoded audio frames; evicts oldest when over capacity.
/// This is the SINGLE outbound audio buffer, connected or not — the pump
/// never lets audio bypass it, so backpressure is always bounded.
#[derive(Debug)]
pub struct AudioRing {
    frames: VecDeque<Vec<u8>>,
    bytes: usize,
    capacity_bytes: usize,
}

impl AudioRing {
    /// `seconds` of a single 16 kHz mono s16 stream (32 kB/s). Multiple
    /// simultaneous speakers share the budget — oldest frames go first.
    #[must_use]
    pub fn with_seconds(seconds: u64) -> Self {
        Self::with_capacity_bytes((seconds as usize).saturating_mul(32_000).max(32_000))
    }

    #[must_use]
    pub fn with_capacity_bytes(capacity_bytes: usize) -> Self {
        Self {
            frames: VecDeque::new(),
            bytes: 0,
            capacity_bytes,
        }
    }

    pub fn push(&mut self, frame: Vec<u8>) {
        self.bytes += frame.len();
        self.frames.push_back(frame);
        while self.bytes > self.capacity_bytes {
            match self.frames.pop_front() {
                Some(evicted) => self.bytes -= evicted.len(),
                None => {
                    self.bytes = 0;
                    break;
                },
            }
        }
    }

    /// Re-insert a frame at the front (oldest position). Used only to
    /// restore a just-popped frame after a failed flush write so temporal
    /// order is preserved across reconnects. Evicts from the back if over
    /// capacity (evicting from the front would discard the frame being
    /// restored); in practice the caller re-inserts a frame popped a moment
    /// earlier, so eviction cannot fire.
    pub fn push_front(&mut self, frame: Vec<u8>) {
        self.bytes += frame.len();
        self.frames.push_front(frame);
        while self.bytes > self.capacity_bytes {
            match self.frames.pop_back() {
                Some(evicted) => self.bytes -= evicted.len(),
                None => {
                    self.bytes = 0;
                    break;
                },
            }
        }
    }

    pub fn pop(&mut self) -> Option<Vec<u8>> {
        let frame = self.frames.pop_front()?;
        self.bytes -= frame.len();
        Some(frame)
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.frames.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.frames.is_empty()
    }

    #[must_use]
    pub fn bytes(&self) -> usize {
        self.bytes
    }
}

/// Run the IPC client forever: connect, pump, reconnect with backoff.
///
/// The backoff is reset only after a connection on which Brain proved
/// liveness (sent at least one control frame back). A Brain that accepts
/// the socket and dies before speaking — the crash-loop class — escalates
/// backoff exactly like a refused connection, and the ring is preserved.
///
/// Returns only when the outbound channel closes (process shutdown).
pub async fn run_ipc(
    socket_path: impl AsRef<Path>,
    buffer_seconds: u64,
    shared: Arc<Shared>,
    mut out_rx: mpsc::UnboundedReceiver<Outbound>,
    voice_cmd_tx: mpsc::UnboundedSender<VoiceCmd>,
    tts_tx: mpsc::UnboundedSender<TtsJob>,
) -> Result<()> {
    let socket_path = socket_path.as_ref();
    let mut ring = AudioRing::with_seconds(buffer_seconds);
    let mut backoff = BACKOFF_MIN;

    loop {
        let stream = match UnixStream::connect(socket_path).await {
            Ok(s) => s,
            Err(e) => {
                debug!(
                    error = %e,
                    path = %socket_path.display(),
                    backoff_ms = backoff.as_millis() as u64,
                    "brain socket unavailable; backing off"
                );
                // Keep draining outbound audio into the ring during backoff
                // so nothing blocks the voice path.
                if drain_while_waiting(&mut out_rx, &mut ring, backoff).await {
                    return Ok(()); // channel closed: shutting down
                }
                backoff = (backoff * 2).min(BACKOFF_MAX);
                continue;
            },
        };

        info!(path = %socket_path.display(), "connected to brain; awaiting liveness");

        let disconnect =
            pump(stream, &shared, &mut out_rx, &mut ring, &voice_cmd_tx, &tts_tx).await;
        match disconnect {
            Disconnect::Shutdown => return Ok(()),
            Disconnect::Lost { reason, brain_live } => {
                warn!(
                    reason = %reason,
                    brain_live,
                    buffered_frames = ring.len(),
                    buffered_bytes = ring.bytes(),
                    "brain connection lost"
                );
                if brain_live {
                    backoff = BACKOFF_MIN;
                } else {
                    // Brain accepted the socket but never spoke: crash-loop
                    // or wedged startup. Escalate like a refused connect and
                    // wait it out (ring keeps buffering) so we don't hammer
                    // a dying peer.
                    if drain_while_waiting(&mut out_rx, &mut ring, backoff).await {
                        return Ok(());
                    }
                    backoff = (backoff * 2).min(BACKOFF_MAX);
                }
            },
        }
    }
}

enum Disconnect {
    /// Outbound channel closed — the process is going down.
    Shutdown,
    /// Connection-level failure; reconnect. `brain_live` records whether
    /// Brain ever proved liveness on this connection (controls backoff and
    /// preserves the ring across silent crash-loops).
    Lost { reason: String, brain_live: bool },
}

/// Sleep for `backoff`, buffering any outbound audio that arrives meanwhile.
/// Returns `true` if the outbound channel closed.
async fn drain_while_waiting(
    out_rx: &mut mpsc::UnboundedReceiver<Outbound>,
    ring: &mut AudioRing,
    backoff: Duration,
) -> bool {
    let deadline = Instant::now() + backoff;
    loop {
        tokio::select! {
            () = sleep_until(deadline) => return false,
            item = out_rx.recv() => match item {
                Some(Outbound::Audio(frame)) => ring.push(frame),
                Some(Outbound::Control(_)) => {}, // superseded by the reconnect snapshot
                None => return true,
            },
        }
    }
}

/// One socket write with the [`WRITE_DEADLINE`]; an error string on failure.
async fn write_frame(writer: &mut OwnedWriteHalf, frame: &[u8]) -> Result<(), String> {
    match timeout(WRITE_DEADLINE, writer.write_all(frame)).await {
        Ok(Ok(())) => Ok(()),
        Ok(Err(e)) => Err(format!("write: {e}")),
        Err(_) => Err(format!("write deadline ({}s) missed", WRITE_DEADLINE.as_secs())),
    }
}

/// Pump one live connection until it drops.
async fn pump(
    stream: UnixStream,
    shared: &Arc<Shared>,
    out_rx: &mut mpsc::UnboundedReceiver<Outbound>,
    ring: &mut AudioRing,
    voice_cmd_tx: &mpsc::UnboundedSender<VoiceCmd>,
    tts_tx: &mpsc::UnboundedSender<TtsJob>,
) -> Disconnect {
    let (mut reader, mut writer) = stream.into_split();
    let mut brain_live = false;
    let lost = |reason: String, brain_live: bool| Disconnect::Lost { reason, brain_live };

    // Handshake: hello (protocol version) then the full state snapshot.
    // The ring stays HELD until Brain sends a control frame back.
    let hello = FrameCodec::encode_control(&hello_msg());
    if let Err(e) = write_frame(&mut writer, &hello).await {
        return lost(format!("hello {e}"), false);
    }
    let snapshot = FrameCodec::encode_control(&snapshot_msg(shared));
    if let Err(e) = write_frame(&mut writer, &snapshot).await {
        return lost(format!("snapshot {e}"), false);
    }

    let mut codec = FrameCodec::default();
    let mut read_buf = vec![0u8; 16 * 1024];
    let mut heartbeat = tokio::time::interval(HEARTBEAT_INTERVAL);
    heartbeat.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut liveness = tokio::time::interval(LIVENESS_CHECK);
    liveness.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut last_inbound = Instant::now();

    loop {
        let flush_ready = should_flush(brain_live, ring.is_empty());
        tokio::select! {
            read = reader.read(&mut read_buf) => match read {
                Ok(0) => return lost("brain closed the socket".into(), brain_live),
                Ok(n) => {
                    last_inbound = Instant::now();
                    match codec.feed(&read_buf[..n]) {
                        Ok(frames) => {
                            for frame in frames {
                                if !brain_live && matches!(frame, Frame::Control(_)) {
                                    brain_live = true;
                                    info!(
                                        buffered_frames = ring.len(),
                                        buffered_bytes = ring.bytes(),
                                        "brain proved liveness; ring flush enabled"
                                    );
                                }
                                dispatch_inbound(frame, shared, voice_cmd_tx, tts_tx);
                            }
                        },
                        Err(e) => return lost(format!("frame decode: {e}"), brain_live),
                    }
                },
                Err(e) => return lost(format!("socket read: {e}"), brain_live),
            },
            item = out_rx.recv() => match item {
                // Audio ALWAYS goes through the ring — the single bounded
                // buffer — and leaves via the flush branch below.
                Some(Outbound::Audio(frame)) => ring.push(frame),
                Some(Outbound::Control(frame)) => {
                    if let Err(e) = write_frame(&mut writer, &frame).await {
                        return lost(format!("control {e}"), brain_live);
                    }
                },
                None => return Disconnect::Shutdown,
            },
            _ = heartbeat.tick() => {
                let msg = json!({
                    "t": "heartbeat",
                    "ticks": shared.stats.ticks.load(Ordering::Relaxed),
                    "active_ssrcs": shared.stats.active_ssrcs.load(Ordering::Relaxed),
                    "connected": shared.stats.connected.load(Ordering::Relaxed),
                });
                let frame = FrameCodec::encode_control(&msg);
                if let Err(e) = write_frame(&mut writer, &frame).await {
                    return lost(format!("heartbeat {e}"), brain_live);
                }
            },
            _ = liveness.tick() => {
                if last_inbound.elapsed() >= INBOUND_LIVENESS {
                    return lost(
                        format!(
                            "no inbound bytes from brain in {}s",
                            INBOUND_LIVENESS.as_secs()
                        ),
                        brain_live,
                    );
                }
            },
            () = std::future::ready(()), if flush_ready => {
                if let Some(frame) = ring.pop() {
                    if let Err(e) = write_frame(&mut writer, &frame).await {
                        ring.push_front(frame); // keep what we couldn't send, in order
                        return lost(format!("audio {e}"), brain_live);
                    }
                }
            },
        }
    }
}

/// Route one decoded inbound frame.
fn dispatch_inbound(
    frame: Frame,
    shared: &Arc<Shared>,
    voice_cmd_tx: &mpsc::UnboundedSender<VoiceCmd>,
    tts_tx: &mpsc::UnboundedSender<TtsJob>,
) {
    match frame {
        Frame::Control(msg) => handle_control(&msg, shared, voice_cmd_tx),
        Frame::Tts {
            guild_id,
            priority,
            wav,
        } => {
            if tts_tx
                .send(TtsJob {
                    guild_id,
                    priority,
                    wav,
                })
                .is_err()
            {
                warn!("playback task gone; dropping TTS frame");
            }
        },
        Frame::Audio { .. } => {
            // Audio flows Ears→Brain only.
            warn!("protocol violation: inbound audio frame from brain; ignoring");
        },
    }
}

/// Handle a Brain→Ears control message (GDD §15: join / leave / optouts /
/// hb_ack).
fn handle_control(
    msg: &Value,
    shared: &Arc<Shared>,
    voice_cmd_tx: &mpsc::UnboundedSender<VoiceCmd>,
) {
    let t = msg.get("t").and_then(Value::as_str).unwrap_or("");
    match t {
        "join" => {
            let (Some(guild_id), Some(channel_id)) =
                (parse_id(msg.get("guild_id")), parse_id(msg.get("channel_id")))
            else {
                warn!(%msg, "join control message missing/invalid ids");
                return;
            };
            let _ = voice_cmd_tx.send(VoiceCmd::Join {
                guild_id,
                channel_id,
            });
        },
        "leave" => {
            let Some(guild_id) = parse_id(msg.get("guild_id")) else {
                warn!(%msg, "leave control message missing/invalid guild_id");
                return;
            };
            let _ = voice_cmd_tx.send(VoiceCmd::Leave { guild_id });
        },
        "optouts" => {
            let users: HashSet<u64> = msg
                .get("user_ids")
                .and_then(Value::as_array)
                .map(|arr| arr.iter().filter_map(|v| parse_id(Some(v))).collect())
                .unwrap_or_default();
            info!(count = users.len(), "opt-out set replaced");
            shared.set_optouts(users);
        },
        // Brain's answer to our heartbeat; its arrival already refreshed the
        // inbound-liveness clock in the pump. Nothing else to do.
        "hb_ack" => {},
        other => debug!(t = other, "unhandled control message"),
    }
}

/// Snowflakes cross the control channel as strings (GDD §15); accept raw
/// numbers too for robustness.
fn parse_id(value: Option<&Value>) -> Option<u64> {
    let value = value?;
    if let Some(s) = value.as_str() {
        return s.parse().ok();
    }
    value.as_u64()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn control_round_trip() {
        let msg = hello_msg();
        let encoded = FrameCodec::encode_control(&msg);
        let mut codec = FrameCodec::default();
        let frames = codec.feed(&encoded).unwrap();
        assert_eq!(frames, vec![Frame::Control(msg)]);
    }

    #[test]
    fn hello_carries_protocol_version() {
        let msg = hello_msg();
        assert_eq!(msg["t"], "hello");
        assert_eq!(msg["proto"], IPC_PROTOCOL_VERSION);
        assert_eq!(msg["version"], env!("CARGO_PKG_VERSION"));
    }

    #[test]
    fn audio_round_trip() {
        let pcm: Vec<i16> = vec![0, 1, -1, i16::MAX, i16::MIN, 12345];
        let encoded = FrameCodec::encode_audio(42, 99, 1_700_000_000_123, &pcm);
        // length = 1 (type) + 24 (ids + timestamp) + 12 (pcm)
        assert_eq!(&encoded[..4], &37u32.to_be_bytes());
        assert_eq!(encoded[4], TYPE_AUDIO);
        let mut codec = FrameCodec::default();
        let frames = codec.feed(&encoded).unwrap();
        assert_eq!(
            frames,
            vec![Frame::Audio {
                user_id: 42,
                guild_id: 99,
                captured_ms: 1_700_000_000_123,
                pcm
            }]
        );
    }

    #[test]
    fn audio_frame_exact_byte_layout() {
        let encoded = FrameCodec::encode_audio(1, 2, 3, &[0x0405]);
        let body = &encoded[5..];
        assert_eq!(&body[0..8], &1u64.to_le_bytes());
        assert_eq!(&body[8..16], &2u64.to_le_bytes());
        assert_eq!(&body[16..24], &3u64.to_le_bytes());
        assert_eq!(&body[24..], &0x0405i16.to_le_bytes());
    }

    #[test]
    fn short_audio_frame_rejected() {
        // 23 body bytes: one short of the v2 header (user + guild + timestamp).
        let mut raw = Vec::new();
        raw.extend_from_slice(&24u32.to_be_bytes());
        raw.push(TYPE_AUDIO);
        raw.extend_from_slice(&[0u8; 23]);
        let mut codec = FrameCodec::default();
        assert!(matches!(codec.feed(&raw), Err(CodecError::ShortAudio(23))));
    }

    #[test]
    fn tts_round_trip() {
        let wav = vec![0x52, 0x49, 0x46, 0x46, 0xAA];
        let encoded = FrameCodec::encode_tts(7, 2, &wav);
        let mut codec = FrameCodec::default();
        let frames = codec.feed(&encoded).unwrap();
        assert_eq!(
            frames,
            vec![Frame::Tts {
                guild_id: 7,
                priority: 2,
                wav
            }]
        );
    }

    #[test]
    fn partial_reads_reassemble() {
        let encoded = FrameCodec::encode_audio(1, 2, 3, &[100, -100]);
        let mut codec = FrameCodec::default();
        for byte in &encoded[..encoded.len() - 1] {
            assert!(codec.feed(std::slice::from_ref(byte)).unwrap().is_empty());
        }
        let frames = codec
            .feed(std::slice::from_ref(&encoded[encoded.len() - 1]))
            .unwrap();
        assert_eq!(frames.len(), 1);
    }

    #[test]
    fn multiple_frames_in_one_read() {
        let mut data = FrameCodec::encode_control(&json!({ "t": "a" }));
        data.extend(FrameCodec::encode_control(&json!({ "t": "b" })));
        data.extend(FrameCodec::encode_audio(1, 2, 3, &[4]));
        let mut codec = FrameCodec::default();
        let frames = codec.feed(&data).unwrap();
        assert_eq!(frames.len(), 3);
    }

    #[test]
    fn empty_body_control_has_length_one_rule() {
        // A frame with an empty body has length 1 (the type byte alone).
        // An empty JSON body is invalid JSON, so check the framing directly.
        let raw = [0u8, 0, 0, 1, TYPE_TTS];
        let mut codec = FrameCodec::default();
        let err = codec.feed(&raw).unwrap_err();
        assert!(matches!(err, CodecError::ShortTts(0)));
    }

    #[test]
    fn oversized_frame_rejected() {
        let mut raw = Vec::new();
        raw.extend_from_slice(&((MAX_FRAME_LEN + 1) as u32).to_be_bytes());
        raw.push(TYPE_CONTROL);
        let mut codec = FrameCodec::default();
        assert!(matches!(
            codec.feed(&raw),
            Err(CodecError::FrameTooLarge(_))
        ));
    }

    #[test]
    fn unknown_type_rejected() {
        let raw = [0u8, 0, 0, 2, 0x7F, 0x00];
        let mut codec = FrameCodec::default();
        assert!(matches!(codec.feed(&raw), Err(CodecError::UnknownType(0x7F))));
    }

    #[test]
    fn ring_evicts_oldest_when_full() {
        let mut ring = AudioRing::with_capacity_bytes(10);
        ring.push(vec![1; 4]);
        ring.push(vec![2; 4]);
        ring.push(vec![3; 4]); // 12 bytes > 10: evicts [1;4]
        assert_eq!(ring.len(), 2);
        assert_eq!(ring.bytes(), 8);
        assert_eq!(ring.pop(), Some(vec![2; 4]));
        assert_eq!(ring.pop(), Some(vec![3; 4]));
        assert_eq!(ring.pop(), None);
        assert!(ring.is_empty());
    }

    #[test]
    fn ring_seconds_capacity() {
        let ring = AudioRing::with_seconds(60);
        assert_eq!(ring.capacity_bytes, 60 * 32_000);
    }

    #[test]
    fn ring_push_front_preserves_order_after_failed_flush() {
        let mut ring = AudioRing::with_capacity_bytes(1024);
        for i in 0..3u8 {
            ring.push(vec![i; 8]);
        }
        let popped = ring.pop().unwrap(); // simulate failed write of oldest
        ring.push_front(popped);
        for i in 0..3u8 {
            assert_eq!(ring.pop(), Some(vec![i; 8]));
        }
        assert!(ring.is_empty());
        assert_eq!(ring.bytes(), 0);
    }

    #[test]
    fn ring_keeps_fifo_order() {
        let mut ring = AudioRing::with_capacity_bytes(1024);
        for i in 0..5u8 {
            ring.push(vec![i; 8]);
        }
        for i in 0..5u8 {
            assert_eq!(ring.pop(), Some(vec![i; 8]));
        }
    }

    #[test]
    fn ring_flush_gated_on_brain_liveness() {
        // A connected-but-silent Brain must never receive the ring.
        assert!(!should_flush(false, false), "liveness unproven: hold the ring");
        assert!(!should_flush(false, true));
        assert!(!should_flush(true, true), "nothing to flush");
        assert!(should_flush(true, false), "live brain + buffered audio: flush");
    }

    #[test]
    fn snapshot_encodes_guild_state_and_roster() {
        let shared = Shared::new();
        let g = shared.guild(42);
        g.set_channel_id(9);
        g.set_connected(true);
        if let Ok(mut map) = g.ssrc_users.write() {
            map.insert(555, 1001);
            map.insert(111, 1002);
        }
        let empty = shared.guild(43); // never joined: null channel, no users

        let msg = snapshot_msg(&shared);
        assert_eq!(msg["t"], "snapshot");
        let guilds = msg["guilds"].as_array().unwrap();
        assert_eq!(guilds.len(), 2);
        assert_eq!(guilds[0]["guild_id"], "42");
        assert_eq!(guilds[0]["channel_id"], "9");
        assert_eq!(guilds[0]["connected"], true);
        assert_eq!(
            guilds[0]["users"],
            json!([
                { "ssrc": 111, "user_id": "1002" },
                { "ssrc": 555, "user_id": "1001" },
            ])
        );
        assert_eq!(guilds[1]["guild_id"], "43");
        assert_eq!(guilds[1]["channel_id"], Value::Null);
        assert_eq!(guilds[1]["connected"], false);
        assert_eq!(empty.roster(), vec![]);
    }

    #[test]
    fn snapshot_round_trips_through_the_codec() {
        let shared = Shared::new();
        let g = shared.guild(1);
        g.set_channel_id(2);
        let msg = snapshot_msg(&shared);
        let mut codec = FrameCodec::default();
        let frames = codec.feed(&FrameCodec::encode_control(&msg)).unwrap();
        assert_eq!(frames, vec![Frame::Control(msg)]);
    }

    #[test]
    fn parse_id_accepts_strings_and_numbers() {
        assert_eq!(parse_id(Some(&json!("123"))), Some(123));
        assert_eq!(parse_id(Some(&json!(456))), Some(456));
        assert_eq!(parse_id(Some(&json!("abc"))), None);
        assert_eq!(parse_id(None), None);
    }

    #[test]
    fn epoch_ms_is_sane() {
        // 2020-01-01 in ms — any healthy clock is past this.
        assert!(epoch_ms() > 1_577_836_800_000);
    }
}
