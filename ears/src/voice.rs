//! Songbird voice event handlers and join/leave command execution (GDD §4, §5).
//!
//! Per 20 ms `VoiceTick`, for every speaking SSRC: map SSRC → user, drop
//! opted-out users *before anything else leaves this process*, decimate
//! 48 kHz stereo → 16 kHz mono, and frame the PCM onto the IPC channel.
//! Ears applies zero judgement here — no thresholds, no matching, no meaning.

use std::collections::HashMap;
use std::num::NonZeroU64;
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex, RwLock};

use serde_json::json;
use serenity::async_trait;
use songbird::events::context_data::{ConnectData, DisconnectData};
use songbird::model::payload::{ClientDisconnect, Speaking};
use songbird::{CoreEvent, Event, EventContext, EventHandler, Songbird};
use tokio::sync::mpsc;
use tracing::{debug, error, info, warn};

use crate::dsp::{stereo48k_to_mono16k, Decimator};
use crate::ipc::{FrameCodec, Outbound};
use crate::state::Shared;

/// Voice commands parsed from Brain control messages (GDD §15).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VoiceCmd {
    Join { guild_id: u64, channel_id: u64 },
    Leave { guild_id: u64 },
}

/// Per-call receiver registered for Songbird core events.
#[derive(Clone)]
pub struct Receiver {
    inner: Arc<ReceiverInner>,
}

struct ReceiverInner {
    guild_id: u64,
    shared: Arc<Shared>,
    out_tx: mpsc::UnboundedSender<Outbound>,
    /// SSRC → Discord user id, learned from `SpeakingStateUpdate`.
    ssrc_users: RwLock<HashMap<u32, u64>>,
    /// Per-SSRC FIR/decimation state so streams stay continuous across ticks.
    decimators: Mutex<HashMap<u32, Decimator>>,
}

impl Receiver {
    #[must_use]
    pub fn new(
        guild_id: u64,
        shared: Arc<Shared>,
        out_tx: mpsc::UnboundedSender<Outbound>,
    ) -> Self {
        Self {
            inner: Arc::new(ReceiverInner {
                guild_id,
                shared,
                out_tx,
                ssrc_users: RwLock::new(HashMap::new()),
                decimators: Mutex::new(HashMap::new()),
            }),
        }
    }

    fn send_control(&self, msg: serde_json::Value) {
        let frame = FrameCodec::encode_control(&msg);
        if self.inner.out_tx.send(Outbound::Control(frame)).is_err() {
            debug!("ipc task gone; dropping control message");
        }
    }

    fn on_speaking_state(&self, update: &Speaking) {
        let Some(user) = update.user_id else {
            return;
        };
        let newly_mapped = match self.inner.ssrc_users.write() {
            Ok(mut map) => map.insert(update.ssrc, user.0) != Some(user.0),
            Err(_) => {
                warn!("ssrc map lock poisoned; skipping speaking update");
                return;
            },
        };
        if newly_mapped {
            debug!(ssrc = update.ssrc, user_id = user.0, "ssrc mapped");
            self.send_control(json!({
                "t": "speaking",
                "user_id": user.0.to_string(),
                "guild_id": self.inner.guild_id.to_string(),
                "state": "start",
            }));
        }
    }

    fn on_voice_tick(&self, tick: &songbird::events::context_data::VoiceTick) {
        let stats = &self.inner.shared.stats;
        stats.ticks.fetch_add(1, Ordering::Relaxed);
        stats
            .active_ssrcs
            .store(tick.speaking.len(), Ordering::Relaxed);
        if !tick.speaking.is_empty() {
            self.inner.shared.note_speech();
        }

        for (&ssrc, data) in &tick.speaking {
            let Some(pcm48k) = data.decoded_voice.as_deref() else {
                // Should not happen under DecodeMode::Decode; nothing to pump.
                continue;
            };

            // SSRC we cannot attribute to a user: drop. We cannot check the
            // opt-out set for audio we cannot attribute, so it never leaves.
            let user_id = {
                let Ok(map) = self.inner.ssrc_users.read() else {
                    continue;
                };
                match map.get(&ssrc) {
                    Some(&uid) => uid,
                    None => continue,
                }
            };

            // Opt-out drop, before DSP, before IPC (hard constraint).
            if self.inner.shared.is_opted_out(user_id) {
                continue;
            }

            let mono16k = {
                let Ok(mut decs) = self.inner.decimators.lock() else {
                    continue;
                };
                let dec = decs.entry(ssrc).or_default();
                stereo48k_to_mono16k(dec, pcm48k)
            };
            if mono16k.is_empty() {
                continue;
            }

            let frame = FrameCodec::encode_audio(user_id, self.inner.guild_id, &mono16k);
            if self.inner.out_tx.send(Outbound::Audio(frame)).is_err() {
                debug!("ipc task gone; dropping audio frame");
            }
        }
    }

    /// SSRCs are assigned per voice-server session; on (re)connect all
    /// pre-existing mappings are invalid. Drop them so audio we can no
    /// longer attribute is dropped (opt-out hard constraint) instead of
    /// being credited to a stale user id.
    fn reset_ssrc_state(&self) {
        if let Ok(mut map) = self.inner.ssrc_users.write() {
            map.clear();
        }
        if let Ok(mut decs) = self.inner.decimators.lock() {
            decs.clear();
        }
    }

    fn on_client_disconnect(&self, evt: &ClientDisconnect) {
        let user_id = evt.user_id.0;
        if let Ok(mut map) = self.inner.ssrc_users.write() {
            let stale: Vec<u32> = map
                .iter()
                .filter(|&(_, &uid)| uid == user_id)
                .map(|(&ssrc, _)| ssrc)
                .collect();
            for ssrc in &stale {
                map.remove(ssrc);
                if let Ok(mut decs) = self.inner.decimators.lock() {
                    decs.remove(ssrc);
                }
            }
        }
        self.send_control(json!({
            "t": "left",
            "user_id": user_id.to_string(),
            "guild_id": self.inner.guild_id.to_string(),
        }));
    }
}

#[async_trait]
impl EventHandler for Receiver {
    async fn act(&self, ctx: &EventContext<'_>) -> Option<Event> {
        match ctx {
            EventContext::SpeakingStateUpdate(update) => self.on_speaking_state(update),
            EventContext::VoiceTick(tick) => self.on_voice_tick(tick),
            EventContext::ClientDisconnect(evt) => self.on_client_disconnect(evt),
            EventContext::DriverConnect(ConnectData { .. }) => {
                info!(guild_id = self.inner.guild_id, "voice driver connected");
                self.reset_ssrc_state();
                self.inner
                    .shared
                    .stats
                    .connected
                    .store(true, Ordering::Relaxed);
            },
            EventContext::DriverReconnect(ConnectData { .. }) => {
                info!(guild_id = self.inner.guild_id, "voice driver reconnected");
                self.reset_ssrc_state();
                self.inner
                    .shared
                    .stats
                    .connected
                    .store(true, Ordering::Relaxed);
            },
            EventContext::DriverDisconnect(DisconnectData { kind, reason, .. }) => {
                warn!(
                    guild_id = self.inner.guild_id,
                    kind = ?kind,
                    reason = ?reason,
                    "voice driver disconnected"
                );
                self.inner
                    .shared
                    .stats
                    .connected
                    .store(false, Ordering::Relaxed);
            },
            _ => {},
        }
        None
    }
}

/// Execute join/leave commands from Brain against the Songbird manager.
///
/// Runs until the command channel closes.
pub async fn run_voice_control(
    manager: Arc<Songbird>,
    shared: Arc<Shared>,
    out_tx: mpsc::UnboundedSender<Outbound>,
    mut rx: mpsc::UnboundedReceiver<VoiceCmd>,
) {
    while let Some(cmd) = rx.recv().await {
        match cmd {
            VoiceCmd::Join {
                guild_id,
                channel_id,
            } => join(&manager, &shared, &out_tx, guild_id, channel_id).await,
            VoiceCmd::Leave { guild_id } => leave(&manager, guild_id).await,
        }
    }
    debug!("voice control channel closed");
}

async fn join(
    manager: &Arc<Songbird>,
    shared: &Arc<Shared>,
    out_tx: &mpsc::UnboundedSender<Outbound>,
    guild_id: u64,
    channel_id: u64,
) {
    let (Some(gid), Some(cid)) = (NonZeroU64::new(guild_id), NonZeroU64::new(channel_id)) else {
        warn!(guild_id, channel_id, "join command with zero id; ignoring");
        return;
    };
    let gid = songbird::id::GuildId(gid);
    let cid = songbird::id::ChannelId(cid);

    // Events that matter for receive fire *while joining*: install handlers
    // before the join attempt (see vendored voice_receive example).
    {
        let call_lock = manager.get_or_insert(gid);
        let mut call = call_lock.lock().await;
        call.remove_all_global_events();

        let receiver = Receiver::new(guild_id, Arc::clone(shared), out_tx.clone());
        call.add_global_event(CoreEvent::SpeakingStateUpdate.into(), receiver.clone());
        call.add_global_event(CoreEvent::VoiceTick.into(), receiver.clone());
        call.add_global_event(CoreEvent::ClientDisconnect.into(), receiver.clone());
        call.add_global_event(CoreEvent::DriverConnect.into(), receiver.clone());
        call.add_global_event(CoreEvent::DriverReconnect.into(), receiver.clone());
        call.add_global_event(CoreEvent::DriverDisconnect.into(), receiver);

        // Hard constraint 4: a self-deafened bot receives no audio and raises
        // no error. Songbird defaults to self_deaf = false; enforce it anyway
        // so no future code path can silently flip it.
        if call.is_deaf() {
            if let Err(e) = call.deafen(false).await {
                warn!(error = %e, "could not clear self-deafen before join");
            }
        }
    }

    match manager.join(gid, cid).await {
        Ok(_call) => info!(guild_id, channel_id, "joined voice channel"),
        Err(e) => {
            error!(guild_id, channel_id, error = %e, "voice join failed");
            // A failed join still leaves a Call with our handlers registered;
            // clear it so the next attempt starts clean.
            if let Err(remove_err) = manager.remove(gid).await {
                debug!(error = %remove_err, "cleanup after failed join");
            }
        },
    }
}

async fn leave(manager: &Arc<Songbird>, guild_id: u64) {
    let Some(gid) = NonZeroU64::new(guild_id) else {
        warn!(guild_id, "leave command with zero guild id; ignoring");
        return;
    };
    let gid = songbird::id::GuildId(gid);
    match manager.remove(gid).await {
        Ok(()) => info!(guild_id, "left voice channel"),
        Err(e) => warn!(guild_id, error = %e, "voice leave failed"),
    }
}
