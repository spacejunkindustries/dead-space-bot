//! Songbird voice event handlers and join/leave command execution (GDD §4, §5).
//!
//! Per 20 ms `VoiceTick`, for every speaking SSRC: map SSRC → user, drop
//! opted-out users *before anything else leaves this process*, decimate
//! 48 kHz stereo → 16 kHz mono, stamp the capture time, and frame the PCM
//! onto the IPC channel. Ears applies zero judgement here — no thresholds,
//! no matching, no meaning.
//!
//! Lifecycle invariants (GDD §15):
//! - SSRC↔user attribution and decimator state live in per-guild
//!   [`GuildVoice`] shared state, NOT in the handler — a `join` control
//!   replay (every Brain restart) can never wipe them. Only a genuine
//!   driver (re)connect, where the SSRCs actually change, clears them.
//! - `join` is idempotent: already connected to the requested guild+channel
//!   means confirm (`join_ok`) and do nothing.
//! - Until Brain's first `optouts` frame of this process lifetime arrives,
//!   ALL audio is dropped — opt-out fails closed (§19).

use std::num::NonZeroU64;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use serde_json::json;
use serenity::async_trait;
use songbird::events::context_data::{ConnectData, DisconnectData, DisconnectKind};
use songbird::model::payload::{ClientDisconnect, Speaking};
use songbird::{CoreEvent, Event, EventContext, EventHandler, Songbird};
use tokio::sync::mpsc;
use tracing::{debug, error, info, warn};

use crate::dsp::stereo48k_to_mono16k;
use crate::ipc::{epoch_ms, FrameCodec, Outbound};
use crate::state::{GuildVoice, Shared};

/// Voice commands parsed from Brain control messages (GDD §15).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VoiceCmd {
    Join { guild_id: u64, channel_id: u64 },
    Leave { guild_id: u64 },
}

/// Per-call receiver registered for Songbird core events. Stateless apart
/// from log latches: all attribution state lives in [`GuildVoice`].
#[derive(Clone)]
pub struct Receiver {
    inner: Arc<ReceiverInner>,
}

struct ReceiverInner {
    guild: Arc<GuildVoice>,
    shared: Arc<Shared>,
    out_tx: mpsc::UnboundedSender<Outbound>,
    /// Log the fail-closed opt-out drop once per handler, not per 20 ms tick.
    unsynced_drop_logged: AtomicBool,
}

impl Receiver {
    #[must_use]
    pub fn new(
        guild: Arc<GuildVoice>,
        shared: Arc<Shared>,
        out_tx: mpsc::UnboundedSender<Outbound>,
    ) -> Self {
        Self {
            inner: Arc::new(ReceiverInner {
                guild,
                shared,
                out_tx,
                unsynced_drop_logged: AtomicBool::new(false),
            }),
        }
    }

    fn send_control(&self, msg: serde_json::Value) {
        send_control(&self.inner.out_tx, msg);
    }

    fn on_speaking_state(&self, update: &Speaking) {
        let Some(user) = update.user_id else {
            return;
        };
        let newly_mapped = match self.inner.guild.ssrc_users.write() {
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
                "guild_id": self.inner.guild.guild_id.to_string(),
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
            self.inner.guild.note_speech();
        }

        // Opt-out fails closed (§19): until Brain's first `optouts` frame of
        // THIS process lifetime has been applied, we cannot know who opted
        // out — so nothing crosses the IPC boundary.
        if !self.inner.shared.optouts_synced() {
            if !self.inner.unsynced_drop_logged.swap(true, Ordering::Relaxed) {
                info!(
                    guild_id = self.inner.guild.guild_id,
                    "opt-out set not yet received from brain; dropping all audio (fail closed)"
                );
            }
            return;
        }

        let captured_ms = epoch_ms();
        for (&ssrc, data) in &tick.speaking {
            let Some(pcm48k) = data.decoded_voice.as_deref() else {
                // Should not happen under DecodeMode::Decode; nothing to pump.
                continue;
            };

            // SSRC we cannot attribute to a user: drop. We cannot check the
            // opt-out set for audio we cannot attribute, so it never leaves.
            let user_id = {
                let Ok(map) = self.inner.guild.ssrc_users.read() else {
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
                let Ok(mut decs) = self.inner.guild.decimators.lock() else {
                    continue;
                };
                let dec = decs.entry(ssrc).or_default();
                stereo48k_to_mono16k(dec, pcm48k)
            };
            if mono16k.is_empty() {
                continue;
            }

            let frame = FrameCodec::encode_audio(
                user_id,
                self.inner.guild.guild_id,
                captured_ms,
                &mono16k,
            );
            if self.inner.out_tx.send(Outbound::Audio(frame)).is_err() {
                debug!("ipc task gone; dropping audio frame");
            }
        }
    }

    fn on_client_disconnect(&self, evt: &ClientDisconnect) {
        let user_id = evt.user_id.0;
        if let Ok(mut map) = self.inner.guild.ssrc_users.write() {
            let stale: Vec<u32> = map
                .iter()
                .filter(|&(_, &uid)| uid == user_id)
                .map(|(&ssrc, _)| ssrc)
                .collect();
            for ssrc in &stale {
                map.remove(ssrc);
                if let Ok(mut decs) = self.inner.guild.decimators.lock() {
                    decs.remove(ssrc);
                }
            }
        }
        self.send_control(json!({
            "t": "left",
            "user_id": user_id.to_string(),
            "guild_id": self.inner.guild.guild_id.to_string(),
        }));
    }

    fn on_driver_connect(&self, reconnect: bool) {
        info!(
            guild_id = self.inner.guild.guild_id,
            reconnect, "voice driver connected"
        );
        // A genuine (re)connect is the ONLY event allowed to clear SSRC
        // attribution: the voice session changed, so the SSRCs really are new.
        self.inner.guild.reset_ssrc_state();
        self.inner.guild.set_connected(true);
        self.inner
            .shared
            .stats
            .connected
            .store(true, Ordering::Relaxed);
    }

    fn on_driver_disconnect(&self, data: &DisconnectData<'_>) {
        let kind = match data.kind {
            DisconnectKind::Connect => "connect",
            DisconnectKind::Reconnect => "reconnect",
            DisconnectKind::Runtime => "runtime",
            _ => "unknown",
        };
        let reason = match data.reason {
            None => "requested".to_owned(),
            Some(r) => format!("{r:?}"),
        };
        warn!(
            guild_id = self.inner.guild.guild_id,
            kind, reason = %reason, "voice driver disconnected"
        );
        self.inner.guild.set_connected(false);
        self.inner
            .shared
            .stats
            .connected
            .store(self.inner.shared.any_connected(), Ordering::Relaxed);
        // Surface it to Brain (GDD §15) — rejoin policy is judgement and
        // therefore lives in Brain, but Brain needs the event to exercise it.
        self.send_control(json!({
            "t": "driver_disconnected",
            "guild_id": self.inner.guild.guild_id.to_string(),
            "kind": kind,
            "reason": reason,
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
            EventContext::DriverConnect(ConnectData { .. }) => self.on_driver_connect(false),
            EventContext::DriverReconnect(ConnectData { .. }) => self.on_driver_connect(true),
            EventContext::DriverDisconnect(data) => self.on_driver_disconnect(data),
            _ => {},
        }
        None
    }
}

fn send_control(out_tx: &mpsc::UnboundedSender<Outbound>, msg: serde_json::Value) {
    let frame = FrameCodec::encode_control(&msg);
    if out_tx.send(Outbound::Control(frame)).is_err() {
        debug!("ipc task gone; dropping control message");
    }
}

/// Execute join/leave commands from Brain against the Songbird manager.
///
/// Runs until the command channel closes.
///
/// Commands are held until `ready_rx` reports the gateway READY: Brain
/// replays the current join the instant the IPC socket connects, which on a
/// fresh Ears process is ~half a second *before* serenity initialises the
/// Songbird manager — and `Songbird::get_or_insert` panics until then,
/// killing this task and silently dropping every subsequent join. The mpsc
/// channel buffers whatever arrives while we wait, so nothing is lost.
pub async fn run_voice_control(
    manager: Arc<Songbird>,
    shared: Arc<Shared>,
    out_tx: mpsc::UnboundedSender<Outbound>,
    mut rx: mpsc::UnboundedReceiver<VoiceCmd>,
    mut ready_rx: tokio::sync::watch::Receiver<bool>,
) {
    while let Some(cmd) = rx.recv().await {
        if !*ready_rx.borrow() {
            info!(?cmd, "gateway not ready; holding voice command");
            if ready_rx.wait_for(|ready| *ready).await.is_err() {
                // Sender dropped => the serenity client is gone; we are
                // shutting down and the command can never be executed.
                warn!("gateway ready signal closed; stopping voice control");
                return;
            }
        }
        match cmd {
            VoiceCmd::Join {
                guild_id,
                channel_id,
            } => join(&manager, &shared, &out_tx, guild_id, channel_id).await,
            VoiceCmd::Leave { guild_id } => leave(&manager, &shared, guild_id).await,
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
    let guild = shared.guild(guild_id);

    // Idempotent replay: Brain re-sends `join` on every IPC reconnect
    // (i.e. every Brain restart). If we are already connected to the
    // requested channel with a live Call, do NOTHING — re-registering the
    // receiver used to wipe the SSRC↔user map, and SpeakingStateUpdate only
    // fires once per user per session, so that replay made the bot deaf.
    if guild.is_connected()
        && guild.channel_id() == channel_id
        && manager.get(gid).is_some()
    {
        info!(guild_id, channel_id, "join replay for current channel; already connected");
        send_control(
            out_tx,
            json!({
                "t": "join_ok",
                "guild_id": guild_id.to_string(),
                "channel_id": channel_id.to_string(),
            }),
        );
        return;
    }

    // Events that matter for receive fire *while joining*: install handlers
    // before the join attempt (see vendored voice_receive example). Install
    // them exactly once per Call lifetime — attribution state lives in the
    // per-guild shared state, so handler identity does not matter, but
    // stacking duplicates would double-send every frame.
    {
        let call_lock = manager.get_or_insert(gid);
        let mut call = call_lock.lock().await;
        if !guild.mark_handlers_installed() {
            call.remove_all_global_events();
            let receiver = Receiver::new(Arc::clone(&guild), Arc::clone(shared), out_tx.clone());
            call.add_global_event(CoreEvent::SpeakingStateUpdate.into(), receiver.clone());
            call.add_global_event(CoreEvent::VoiceTick.into(), receiver.clone());
            call.add_global_event(CoreEvent::ClientDisconnect.into(), receiver.clone());
            call.add_global_event(CoreEvent::DriverConnect.into(), receiver.clone());
            call.add_global_event(CoreEvent::DriverReconnect.into(), receiver.clone());
            call.add_global_event(CoreEvent::DriverDisconnect.into(), receiver);
        }

        // Hard constraint 4: a self-deafened bot receives no audio and raises
        // no error. Songbird defaults to self_deaf = false; enforce it anyway
        // so no future code path can silently flip it.
        if call.is_deaf() {
            if let Err(e) = call.deafen(false).await {
                warn!(error = %e, "could not clear self-deafen before join");
            }
        }
    }

    guild.set_channel_id(channel_id);
    match manager.join(gid, cid).await {
        Ok(_call) => {
            info!(guild_id, channel_id, "joined voice channel");
            send_control(
                out_tx,
                json!({
                    "t": "join_ok",
                    "guild_id": guild_id.to_string(),
                    "channel_id": channel_id.to_string(),
                }),
            );
        },
        Err(e) => {
            error!(guild_id, channel_id, error = %e, "voice join failed");
            // A failed join still leaves a Call with our handlers registered;
            // clear it so the next attempt starts clean.
            if let Err(remove_err) = manager.remove(gid).await {
                debug!(error = %remove_err, "cleanup after failed join");
            }
            guild.set_connected(false);
            guild.set_channel_id(0);
            guild.clear_handlers_installed();
            guild.reset_ssrc_state();
            send_control(
                out_tx,
                json!({
                    "t": "join_failed",
                    "guild_id": guild_id.to_string(),
                    "channel_id": channel_id.to_string(),
                    "reason": e.to_string(),
                }),
            );
        },
    }
}

async fn leave(manager: &Arc<Songbird>, shared: &Arc<Shared>, guild_id: u64) {
    let Some(gid) = NonZeroU64::new(guild_id) else {
        warn!(guild_id, "leave command with zero guild id; ignoring");
        return;
    };
    let gid = songbird::id::GuildId(gid);
    match manager.remove(gid).await {
        Ok(()) => info!(guild_id, "left voice channel"),
        Err(e) => warn!(guild_id, error = %e, "voice leave failed"),
    }
    // The Call is gone: attribution state is meaningless and the handlers
    // died with the Call. A later join starts clean.
    let guild = shared.guild(guild_id);
    guild.set_connected(false);
    guild.set_channel_id(0);
    guild.clear_handlers_installed();
    guild.reset_ssrc_state();
}
