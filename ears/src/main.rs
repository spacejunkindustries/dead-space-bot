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

/// `sysexits.h` EX_CONFIG: configuration error. The deploy units set
/// `RestartPreventExitStatus=78` so a bad config edit stops the service
/// cleanly instead of crash-looping it.
const EXIT_CONFIG: i32 = 78;

/// Parsed command line: `cortana-ears [--version|--check] [CONFIG]`.
struct Cli {
    version: bool,
    check: bool,
    config_path: String,
}

fn parse_cli(args: impl Iterator<Item = String>) -> Cli {
    let mut cli = Cli {
        version: false,
        check: false,
        config_path: config::DEFAULT_CONFIG_PATH.to_owned(),
    };
    for arg in args {
        match arg.as_str() {
            "--version" => cli.version = true,
            "--check" => cli.check = true,
            other => cli.config_path = other.to_owned(),
        }
    }
    cli
}

/// Offline preflight for `ExecStartPre=` and operators: parse the config
/// (deny_unknown_fields stays on), confirm the credential is readable, the
/// socket directory is usable, and the statically linked Opus decoder
/// actually constructs. Returns the process exit code and prints one human
/// line per problem — this is an interactive CLI mode, deliberately stdout,
/// not tracing.
fn run_check(config_path: &str) -> i32 {
    let cfg = match config::load(std::path::Path::new(config_path)) {
        Ok(cfg) => cfg,
        Err(e) => {
            println!("cortana-ears --check: FAIL: config: {e:#}");
            return EXIT_CONFIG;
        },
    };
    if let Err(e) = config::read_token(&cfg) {
        println!("cortana-ears --check: FAIL: token: {e:#}");
        return EXIT_CONFIG;
    }
    let socket_dir = cfg
        .socket_path
        .parent()
        .map(std::path::Path::to_path_buf)
        .unwrap_or_else(|| std::path::PathBuf::from("/"));
    if !socket_dir.is_dir() {
        println!(
            "cortana-ears --check: FAIL: socket directory {} does not exist \
             (tmpfiles.d should create it)",
            socket_dir.display()
        );
        return EXIT_CONFIG;
    }
    // Opus decode is the whole job (DecodeMode::Decode); prove the statically
    // linked decoder constructs at 48 kHz stereo, Songbird's receive shape.
    if let Err(e) = songbird::driver::opus::Decoder::new(48_000, songbird::driver::opus::Channels::Stereo)
    {
        println!("cortana-ears --check: FAIL: opus decoder: {e}");
        return 1;
    }
    println!(
        "cortana-ears --check: OK (config {}, socket {}, ipc protocol v{})",
        config_path,
        cfg.socket_path.display(),
        ipc::IPC_PROTOCOL_VERSION
    );
    0
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = parse_cli(std::env::args().skip(1));
    if cli.version {
        // Human/CLI output on purpose (matches --check): the protocol version
        // is how an operator confirms a matched Ears/Brain pair (GDD §15).
        println!(
            "cortana-ears {} (ipc protocol v{})",
            env!("CARGO_PKG_VERSION"),
            ipc::IPC_PROTOCOL_VERSION
        );
        return Ok(());
    }
    if cli.check {
        std::process::exit(run_check(&cli.config_path));
    }

    init_tracing();

    let cfg_path = cli.config_path;
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

#[cfg(test)]
mod tests {
    use super::*;

    fn cli(args: &[&str]) -> Cli {
        parse_cli(args.iter().map(|s| (*s).to_owned()))
    }

    #[test]
    fn cli_defaults() {
        let c = cli(&[]);
        assert!(!c.version);
        assert!(!c.check);
        assert_eq!(c.config_path, config::DEFAULT_CONFIG_PATH);
    }

    #[test]
    fn cli_flags_and_config_path_in_any_order() {
        let c = cli(&["--check", "/tmp/ears.yaml"]);
        assert!(c.check);
        assert_eq!(c.config_path, "/tmp/ears.yaml");
        let c = cli(&["/tmp/ears.yaml", "--version"]);
        assert!(c.version);
        assert_eq!(c.config_path, "/tmp/ears.yaml");
    }

    #[test]
    fn check_fails_config_exit_code_on_missing_file() {
        assert_eq!(run_check("/nonexistent/ears.yaml"), EXIT_CONFIG);
    }
}
