//! Framed Unix-domain-socket client to Brain (GDD §15).
//!
//! Brain binds the socket; Ears connects and reconnects with exponential
//! backoff forever. While disconnected, outbound audio is kept in a bounded
//! ring buffer (oldest frames dropped) so a Brain restart costs at most
//! `buffer_seconds` of speech, not the voice connection.
//!
//! ```text
//! Frame: [4-byte BE length][1-byte type][body]
//!        length = 1 + len(body)  (every byte after the length field)
//! 0x01 CONTROL  UTF-8 JSON object
//! 0x02 AUDIO    Ears→Brain  [8B user_id LE][8B guild_id LE][i16 LE PCM 16k mono]
//! 0x03 TTS      Brain→Ears  [8B guild_id LE][1B priority][WAV bytes]
//! ```

use std::collections::{HashSet, VecDeque};
use std::path::Path;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use tokio::sync::mpsc;
use tokio::time::{sleep_until, Instant};
use tracing::{debug, info, warn};

use crate::playback::TtsJob;
use crate::state::Shared;
use crate::voice::VoiceCmd;

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
                if body.len() < 16 {
                    return Err(CodecError::ShortAudio(body.len()));
                }
                let user_id = u64::from_le_bytes(
                    body[0..8].try_into().expect("slice length is 8"),
                );
                let guild_id = u64::from_le_bytes(
                    body[8..16].try_into().expect("slice length is 8"),
                );
                let pcm = body[16..]
                    .chunks_exact(2)
                    .map(|b| i16::from_le_bytes([b[0], b[1]]))
                    .collect();
                Ok(Frame::Audio {
                    user_id,
                    guild_id,
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

    /// Encode an audio frame (Ears→Brain).
    #[must_use]
    pub fn encode_audio(user_id: u64, guild_id: u64, pcm: &[i16]) -> Vec<u8> {
        let mut body = Vec::with_capacity(16 + pcm.len() * 2);
        body.extend_from_slice(&user_id.to_le_bytes());
        body.extend_from_slice(&guild_id.to_le_bytes());
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

/// An already-encoded outbound frame, tagged so the ring buffer knows what to
/// keep across a disconnect (audio is buffered; control is point-in-time and
/// dropped).
#[derive(Debug, Clone)]
pub enum Outbound {
    Audio(Vec<u8>),
    Control(Vec<u8>),
}

/// Bounded FIFO of encoded audio frames; evicts oldest when over capacity.
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

        info!(path = %socket_path.display(), "connected to brain");
        backoff = BACKOFF_MIN;

        let disconnect =
            pump(stream, &shared, &mut out_rx, &mut ring, &voice_cmd_tx, &tts_tx).await;
        match disconnect {
            Disconnect::Shutdown => return Ok(()),
            Disconnect::Lost(reason) => {
                warn!(
                    reason = %reason,
                    buffered_frames = ring.len(),
                    buffered_bytes = ring.bytes(),
                    "brain connection lost"
                );
            },
        }
    }
}

enum Disconnect {
    /// Outbound channel closed — the process is going down.
    Shutdown,
    /// Socket-level failure; reconnect.
    Lost(String),
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
                Some(Outbound::Control(_)) => {}, // point-in-time; stale on reconnect
                None => return true,
            },
        }
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

    // Hello, then whatever audio survived the outage.
    let hello = FrameCodec::encode_control(&json!({ "t": "hello", "version": "1.0" }));
    if let Err(e) = writer.write_all(&hello).await {
        return Disconnect::Lost(format!("hello write: {e}"));
    }
    if !ring.is_empty() {
        info!(frames = ring.len(), "flushing audio buffered during outage");
    }
    while let Some(frame) = ring.pop() {
        if let Err(e) = writer.write_all(&frame).await {
            ring.push_front(frame); // keep what we couldn't send, in order
            return Disconnect::Lost(format!("ring flush write: {e}"));
        }
    }

    let mut codec = FrameCodec::default();
    let mut read_buf = vec![0u8; 16 * 1024];
    let mut heartbeat = tokio::time::interval(HEARTBEAT_INTERVAL);
    heartbeat.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);

    loop {
        tokio::select! {
            read = reader.read(&mut read_buf) => match read {
                Ok(0) => return Disconnect::Lost("brain closed the socket".into()),
                Ok(n) => match codec.feed(&read_buf[..n]) {
                    Ok(frames) => {
                        for frame in frames {
                            dispatch_inbound(frame, shared, voice_cmd_tx, tts_tx);
                        }
                    },
                    Err(e) => return Disconnect::Lost(format!("frame decode: {e}")),
                },
                Err(e) => return Disconnect::Lost(format!("socket read: {e}")),
            },
            item = out_rx.recv() => match item {
                Some(Outbound::Audio(frame)) => {
                    if let Err(e) = writer.write_all(&frame).await {
                        ring.push(frame);
                        return Disconnect::Lost(format!("audio write: {e}"));
                    }
                },
                Some(Outbound::Control(frame)) => {
                    if let Err(e) = writer.write_all(&frame).await {
                        return Disconnect::Lost(format!("control write: {e}"));
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
                if let Err(e) = writer.write_all(&frame).await {
                    return Disconnect::Lost(format!("heartbeat write: {e}"));
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

/// Handle a Brain→Ears control message (GDD §15: join / leave / optouts).
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
        let msg = json!({ "t": "hello", "version": "1.0" });
        let encoded = FrameCodec::encode_control(&msg);
        let mut codec = FrameCodec::default();
        let frames = codec.feed(&encoded).unwrap();
        assert_eq!(frames, vec![Frame::Control(msg)]);
    }

    #[test]
    fn audio_round_trip() {
        let pcm: Vec<i16> = vec![0, 1, -1, i16::MAX, i16::MIN, 12345];
        let encoded = FrameCodec::encode_audio(42, 99, &pcm);
        // length = 1 (type) + 16 (ids) + 12 (pcm)
        assert_eq!(&encoded[..4], &29u32.to_be_bytes());
        assert_eq!(encoded[4], TYPE_AUDIO);
        let mut codec = FrameCodec::default();
        let frames = codec.feed(&encoded).unwrap();
        assert_eq!(
            frames,
            vec![Frame::Audio {
                user_id: 42,
                guild_id: 99,
                pcm
            }]
        );
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
        let encoded = FrameCodec::encode_audio(1, 2, &[100, -100]);
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
        data.extend(FrameCodec::encode_audio(1, 2, &[3]));
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
    fn parse_id_accepts_strings_and_numbers() {
        assert_eq!(parse_id(Some(&json!("123"))), Some(123));
        assert_eq!(parse_id(Some(&json!(456))), Some(456));
        assert_eq!(parse_id(Some(&json!("abc"))), None);
        assert_eq!(parse_id(None), None);
    }
}
