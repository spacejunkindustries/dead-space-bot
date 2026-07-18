//! CORTANA Ears — the DAVE-capable Discord audio pump (GDD §3, §4).
//!
//! Ears is deliberately thin: it maintains the gateway and voice connections,
//! decodes per-user PCM, decimates it, pushes frames to Brain over a Unix
//! socket, and plays back whatever WAV bytes Brain sends. Everything that
//! requires judgement lives in Brain.

mod config;
mod dsp;
mod ipc;
mod playback;
mod state;
mod voice;

use std::sync::Arc;

use anyhow::{Context as AnyhowContext, Result};
use serenity::async_trait;
use serenity::client::{Client, Context, EventHandler};
use serenity::model::gateway::{GatewayIntents, Ready};
use songbird::driver::{DecodeConfig, DecodeMode};
use songbird::{SerenityInit, Songbird};
use tokio::sync::{mpsc, watch};
use tracing::{error, info, warn};

struct GatewayHandler {
    /// Flipped true on the first READY. Songbird's manager panics on
    /// `get_or_insert` until serenity has initialised it with shard/user
    /// data, which happens at READY — voice.rs holds Brain's join/leave
    /// commands behind this flag so an early IPC replay cannot race it.
    ready_tx: watch::Sender<bool>,
}

#[async_trait]
impl EventHandler for GatewayHandler {
    async fn ready(&self, _ctx: Context, ready: Ready) {
        info!(user = %ready.user.name, guilds = ready.guilds.len(), "discord gateway ready");
        let _ = self.ready_tx.send(true);
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    init_tracing();

    let cfg_path = std::env::args()
        .nth(1)
        .unwrap_or_else(|| config::DEFAULT_CONFIG_PATH.to_owned());
    let cfg = config::load(std::path::Path::new(&cfg_path))?;
    let token = config::read_token(&cfg)?;
    info!(
        config = %cfg_path,
        socket = %cfg.socket_path.display(),
        buffer_seconds = cfg.buffer_seconds,
        "ears starting"
    );

    let shared = Arc::new(state::Shared::new());
    let (out_tx, out_rx) = mpsc::unbounded_channel::<ipc::Outbound>();
    let (voice_cmd_tx, voice_cmd_rx) = mpsc::unbounded_channel::<voice::VoiceCmd>();
    let (tts_tx, tts_rx) = mpsc::unbounded_channel::<playback::TtsJob>();

    // Receive requires full decode; DecodeConfig::default() is interleaved
    // stereo at 48 kHz — dsp.rs owns the fold + 3:1 decimation to 16 kHz.
    let songbird_config = songbird::Config::default()
        .decode_mode(DecodeMode::Decode(DecodeConfig::default()));
    let manager = Songbird::serenity_from_config(songbird_config);

    let (ready_tx, ready_rx) = watch::channel(false);

    let intents = GatewayIntents::GUILDS | GatewayIntents::GUILD_VOICE_STATES;
    let mut client = Client::builder(&token, intents)
        .event_handler(GatewayHandler { ready_tx })
        .register_songbird_with(Arc::clone(&manager))
        .await
        .context("building serenity client")?;

    let ipc_task = tokio::spawn(ipc::run_ipc(
        cfg.socket_path.clone(),
        cfg.buffer_seconds,
        Arc::clone(&shared),
        out_rx,
        voice_cmd_tx,
        tts_tx,
    ));
    let voice_task = tokio::spawn(voice::run_voice_control(
        Arc::clone(&manager),
        Arc::clone(&shared),
        out_tx.clone(),
        voice_cmd_rx,
        ready_rx,
    ));
    let playback_task = tokio::spawn(playback::run_playback(
        Arc::clone(&manager),
        Arc::clone(&shared),
        tts_rx,
    ));

    // Graceful shutdown on SIGINT/SIGTERM (systemd sends SIGTERM).
    let shard_manager = client.shard_manager.clone();
    tokio::spawn(async move {
        let signal = wait_for_shutdown_signal().await;
        info!(signal, "shutdown signal received");
        shard_manager.shutdown_all().await;
    });

    if let Err(e) = client.start().await {
        error!(error = %e, "serenity client exited with error");
    }

    // Gateway is down; stop the worker tasks.
    ipc_task.abort();
    voice_task.abort();
    playback_task.abort();
    info!("ears stopped");
    Ok(())
}

/// Wait for SIGINT or SIGTERM; returns the signal name for logging.
async fn wait_for_shutdown_signal() -> &'static str {
    use tokio::signal::unix::{signal, SignalKind};
    let mut sigterm = match signal(SignalKind::terminate()) {
        Ok(s) => s,
        Err(e) => {
            warn!(error = %e, "cannot install SIGTERM handler; ctrl-c only");
            match tokio::signal::ctrl_c().await {
                Ok(()) => return "SIGINT",
                Err(err) => {
                    error!(error = %err, "cannot install ctrl-c handler");
                    std::future::pending::<()>().await;
                    unreachable!();
                },
            }
        },
    };
    tokio::select! {
        _ = sigterm.recv() => "SIGTERM",
        _ = tokio::signal::ctrl_c() => "SIGINT",
    }
}

fn init_tracing() {
    use tracing_subscriber::EnvFilter;
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("info,serenity=warn,songbird=info"));
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(filter)
        .with_current_span(false)
        .init();
}
