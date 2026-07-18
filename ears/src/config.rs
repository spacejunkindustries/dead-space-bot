//! Ears' own (deliberately tiny) configuration.
//!
//! Ears is a dumb audio pump (GDD §3.2): its config carries plumbing only —
//! where the socket lives, how much audio to buffer through a Brain restart,
//! and a dev-only token fallback. Anything resembling a *meaning*-level knob
//! belongs in Brain's `cortana.yaml`, not here.

use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Deserialize;

/// Contents of `/etc/cortana/ears.yaml` (path overridable via `argv[1]`).
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EarsConfig {
    /// Dev-only fallback path for the bot token. In production the token
    /// arrives via systemd `LoadCredential=` as `$CREDENTIALS_DIRECTORY/token`
    /// and this file is never read.
    #[serde(default = "default_token_file")]
    pub token_file: PathBuf,

    /// Unix domain socket shared with Brain. Brain binds; Ears connects
    /// (GDD §15 — this ordering lets Ears buffer through a Brain restart).
    #[serde(default = "default_socket_path")]
    pub socket_path: PathBuf,

    /// Seconds of outbound audio to ring-buffer while Brain is unreachable
    /// (GDD §20: "Ears buffers 60s of frames").
    #[serde(default = "default_buffer_seconds")]
    pub buffer_seconds: u64,
}

fn default_token_file() -> PathBuf {
    PathBuf::from("/etc/cortana/token")
}

fn default_socket_path() -> PathBuf {
    PathBuf::from("/run/cortana/cortana.sock")
}

fn default_buffer_seconds() -> u64 {
    60
}

/// Default config path when none is given on the command line.
pub const DEFAULT_CONFIG_PATH: &str = "/etc/cortana/ears.yaml";

/// Load the config from a YAML file.
pub fn load(path: &Path) -> Result<EarsConfig> {
    let raw = fs::read_to_string(path)
        .with_context(|| format!("reading ears config {}", path.display()))?;
    let cfg: EarsConfig = serde_yaml::from_str(&raw)
        .with_context(|| format!("parsing ears config {}", path.display()))?;
    Ok(cfg)
}

/// Resolve the Discord bot token.
///
/// `$CREDENTIALS_DIRECTORY/token` (systemd `LoadCredential=`, hard constraint
/// 12) wins; `token_file` is the dev fallback. The token value itself is never
/// logged.
pub fn read_token(cfg: &EarsConfig) -> Result<String> {
    if let Ok(dir) = std::env::var("CREDENTIALS_DIRECTORY") {
        let cred = Path::new(&dir).join("token");
        if cred.is_file() {
            let token = fs::read_to_string(&cred)
                .with_context(|| format!("reading credential {}", cred.display()))?;
            return non_empty(token, &cred);
        }
    }
    let token = fs::read_to_string(&cfg.token_file)
        .with_context(|| format!("reading token file {}", cfg.token_file.display()))?;
    non_empty(token, &cfg.token_file)
}

fn non_empty(token: String, origin: &Path) -> Result<String> {
    let token = token.trim().to_owned();
    if token.is_empty() {
        anyhow::bail!("token at {} is empty", origin.display());
    }
    Ok(token)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_minimal_yaml_with_defaults() {
        let cfg: EarsConfig = serde_yaml::from_str("token_file: /tmp/tok\n").unwrap();
        assert_eq!(cfg.token_file, PathBuf::from("/tmp/tok"));
        assert_eq!(cfg.socket_path, PathBuf::from("/run/cortana/cortana.sock"));
        assert_eq!(cfg.buffer_seconds, 60);
    }

    #[test]
    fn parses_full_yaml() {
        let cfg: EarsConfig = serde_yaml::from_str(
            "token_file: /etc/cortana/token\nsocket_path: /tmp/cortana.sock\nbuffer_seconds: 30\n",
        )
        .unwrap();
        assert_eq!(cfg.socket_path, PathBuf::from("/tmp/cortana.sock"));
        assert_eq!(cfg.buffer_seconds, 30);
    }

    #[test]
    fn rejects_unknown_keys() {
        let err = serde_yaml::from_str::<EarsConfig>("wake_threshold: 0.5\n");
        assert!(err.is_err(), "meaning-level keys must not sneak into Ears");
    }
}
