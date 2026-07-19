//! State shared between the voice handlers, the IPC task, and playback.
//!
//! Voice attribution state (SSRC↔user, decimators) lives HERE, per guild,
//! not inside the Songbird event handler. Handlers are re-registered on every
//! `join` control replay (every Brain restart); attribution state must
//! survive that, because Discord only announces an SSRC→user mapping once
//! per voice session (`SpeakingStateUpdate` fires on first speech). Only a
//! genuine driver (re)connect — where the SSRCs actually change — may clear
//! it (see `voice.rs`).

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

use crate::dsp::Decimator;

/// Counters surfaced in the 5-second IPC heartbeat (GDD §15).
#[derive(Debug, Default)]
pub struct VoiceStats {
    /// Cumulative `VoiceTick` count across all calls.
    pub ticks: AtomicU64,
    /// SSRCs with audio in the most recent tick.
    pub active_ssrcs: AtomicUsize,
    /// Whether at least one voice driver is currently connected.
    pub connected: AtomicBool,
}

/// Sustained decode-failure detector — the DAVE-wedge watchdog (GDD §20).
///
/// Live incident: the DAVE E2EE session stuck at `status: PENDING` after a
/// voice-server transition. Packets kept arriving but NONE decoded — every
/// 20 ms tick carried speakers whose `decoded_voice` was `None`, and outgoing
/// TTS was keyed against a session nobody had agreed on: she could hear
/// everyone and nobody could hear her, indefinitely, until a manual Ears
/// restart forced a fresh join + DAVE handshake.
///
/// The signature is "packets present, zero successful decodes, sustained":
/// one healthy decode from ANY speaker resets the run; a silent tick (no
/// packets) is no evidence either way — it neither extends nor resets, so a
/// quiet channel can never trip the watchdog and a mid-wedge pause cannot
/// hide the wedge. All-`None` runs also occur benignly for a few hundred ms
/// around session churn, which is why the trigger threshold lives in the
/// caller and is measured in tens of seconds.
#[derive(Debug, Default)]
pub struct DecodeWatchdog {
    /// Epoch-ms when the current all-bad run started; 0 = no run.
    bad_since_ms: AtomicU64,
}

impl DecodeWatchdog {
    /// Observe one tick; returns the current all-bad run length in ms
    /// (0 when healthy, silent, or the run just started).
    pub fn observe(&self, now_ms: u64, any_packets: bool, any_decoded: bool) -> u64 {
        if any_decoded {
            self.bad_since_ms.store(0, Ordering::Relaxed);
            return 0;
        }
        if !any_packets {
            return 0;
        }
        let now = now_ms.max(1); // never store the "no run" sentinel
        let start = self.bad_since_ms.load(Ordering::Relaxed);
        if start == 0 {
            self.bad_since_ms.store(now, Ordering::Relaxed);
            return 0;
        }
        now.saturating_sub(start)
    }

    /// Forget any in-progress run (a fresh driver session gets fresh runway
    /// to negotiate DAVE before the clock starts).
    pub fn reset(&self) {
        self.bad_since_ms.store(0, Ordering::Relaxed);
    }
}

/// Per-guild voice state. Survives Songbird handler re-registration; only a
/// real driver (re)connect clears the SSRC attribution maps.
#[derive(Debug)]
pub struct GuildVoice {
    pub guild_id: u64,
    /// Channel Ears was last told to join (0 = none).
    channel_id: AtomicU64,
    /// Whether this guild's voice driver is currently connected.
    connected: AtomicBool,
    /// Whether our Receiver's global events are installed on this Call.
    /// Guards against stacking duplicate handlers on join replays.
    handlers_installed: AtomicBool,
    /// SSRC → Discord user id, learned from `SpeakingStateUpdate`.
    pub ssrc_users: RwLock<HashMap<u32, u64>>,
    /// Per-SSRC FIR/decimation state so streams stay continuous across ticks.
    pub decimators: Mutex<HashMap<u32, Decimator>>,
    /// Millisecond offset (from `epoch`) of the most recent tick that carried
    /// human audio in this guild. `0` = never. Drives talk-over suppression
    /// and ducking — per guild, so speech in one call never gates another.
    last_speech_ms: AtomicU64,
    /// The DAVE-wedge detector (GDD §20): packets flowing, nothing decoding.
    pub decode_watchdog: DecodeWatchdog,
    epoch: Instant,
}

impl GuildVoice {
    #[must_use]
    fn new(guild_id: u64) -> Self {
        Self {
            guild_id,
            channel_id: AtomicU64::new(0),
            connected: AtomicBool::new(false),
            handlers_installed: AtomicBool::new(false),
            ssrc_users: RwLock::new(HashMap::new()),
            decimators: Mutex::new(HashMap::new()),
            last_speech_ms: AtomicU64::new(0),
            decode_watchdog: DecodeWatchdog::default(),
            epoch: Instant::now(),
        }
    }

    #[must_use]
    pub fn channel_id(&self) -> u64 {
        self.channel_id.load(Ordering::Relaxed)
    }

    pub fn set_channel_id(&self, channel_id: u64) {
        self.channel_id.store(channel_id, Ordering::Relaxed);
    }

    #[must_use]
    pub fn is_connected(&self) -> bool {
        self.connected.load(Ordering::Relaxed)
    }

    pub fn set_connected(&self, connected: bool) {
        self.connected.store(connected, Ordering::Relaxed);
    }

    /// Atomically mark the handlers installed; returns the previous value,
    /// so exactly one caller sees `false` per Call lifetime.
    pub fn mark_handlers_installed(&self) -> bool {
        self.handlers_installed.swap(true, Ordering::SeqCst)
    }

    pub fn clear_handlers_installed(&self) {
        self.handlers_installed.store(false, Ordering::SeqCst);
    }

    /// SSRCs are assigned per voice-server session; on a genuine driver
    /// (re)connect all pre-existing mappings are invalid. Drop them so audio
    /// we can no longer attribute is dropped (opt-out hard constraint)
    /// instead of being credited to a stale user id. This is the ONLY thing
    /// allowed to clear attribution state — control-plane replays are not.
    pub fn reset_ssrc_state(&self) {
        if let Ok(mut map) = self.ssrc_users.write() {
            map.clear();
        }
        if let Ok(mut decs) = self.decimators.lock() {
            decs.clear();
        }
    }

    /// Current SSRC→user roster, for the reconnect snapshot (GDD §15).
    #[must_use]
    pub fn roster(&self) -> Vec<(u32, u64)> {
        match self.ssrc_users.read() {
            Ok(map) => {
                let mut pairs: Vec<(u32, u64)> = map.iter().map(|(&s, &u)| (s, u)).collect();
                pairs.sort_unstable();
                pairs
            },
            Err(_) => Vec::new(),
        }
    }

    /// Record that human speech was heard "now" in this guild.
    pub fn note_speech(&self) {
        let ms = self.epoch.elapsed().as_millis() as u64;
        // Never store 0 ("never"); speech in the first millisecond still counts.
        self.last_speech_ms.store(ms.max(1), Ordering::Relaxed);
    }

    /// Whether human speech was heard in this guild within the given window.
    #[must_use]
    pub fn speech_within(&self, window: Duration) -> bool {
        let last = self.last_speech_ms.load(Ordering::Relaxed);
        if last == 0 {
            return false;
        }
        let now = self.epoch.elapsed().as_millis() as u64;
        now.saturating_sub(last) <= window.as_millis() as u64
    }
}

/// Process-wide shared state.
#[derive(Debug)]
pub struct Shared {
    /// Users whose audio must be dropped *before* it crosses the IPC boundary
    /// (hard constraint: opt-out is enforced in Ears). Replaced wholesale on
    /// every `optouts` control message from Brain.
    pub optouts: RwLock<HashSet<u64>>,
    /// False until the first `optouts` control message of THIS process
    /// lifetime has been applied. Until then `voice.rs` drops ALL audio —
    /// a fresh Ears process must fail closed, never open (GDD §19).
    optouts_synced: AtomicBool,
    /// Heartbeat counters.
    pub stats: VoiceStats,
    /// Per-guild voice state, created on first use and kept for the process
    /// lifetime (guild count is 1–2 in practice).
    guilds: RwLock<HashMap<u64, Arc<GuildVoice>>>,
}

impl Default for Shared {
    fn default() -> Self {
        Self::new()
    }
}

impl Shared {
    #[must_use]
    pub fn new() -> Self {
        Self {
            optouts: RwLock::new(HashSet::new()),
            optouts_synced: AtomicBool::new(false),
            stats: VoiceStats::default(),
            guilds: RwLock::new(HashMap::new()),
        }
    }

    /// Get-or-create the per-guild voice state.
    #[must_use]
    pub fn guild(&self, guild_id: u64) -> Arc<GuildVoice> {
        if let Ok(map) = self.guilds.read() {
            if let Some(g) = map.get(&guild_id) {
                return Arc::clone(g);
            }
        }
        match self.guilds.write() {
            Ok(mut map) => Arc::clone(
                map.entry(guild_id)
                    .or_insert_with(|| Arc::new(GuildVoice::new(guild_id))),
            ),
            // Lock poison means a panic elsewhere; a detached (unregistered)
            // state object keeps the caller alive without corrupting the map.
            Err(_) => Arc::new(GuildVoice::new(guild_id)),
        }
    }

    /// Every known guild state, sorted by guild id (stable snapshots).
    #[must_use]
    pub fn guilds_snapshot(&self) -> Vec<Arc<GuildVoice>> {
        match self.guilds.read() {
            Ok(map) => {
                let mut all: Vec<Arc<GuildVoice>> = map.values().cloned().collect();
                all.sort_unstable_by_key(|g| g.guild_id);
                all
            },
            Err(_) => Vec::new(),
        }
    }

    /// Whether any guild's voice driver is currently connected.
    #[must_use]
    pub fn any_connected(&self) -> bool {
        match self.guilds.read() {
            Ok(map) => map.values().any(|g| g.is_connected()),
            Err(_) => false,
        }
    }

    /// Whether the opt-out set has been received from Brain this process
    /// lifetime. Until it has, ALL audio is dropped (fail closed).
    #[must_use]
    pub fn optouts_synced(&self) -> bool {
        self.optouts_synced.load(Ordering::Acquire)
    }

    /// Whether the given user has opted out of voice capture.
    #[must_use]
    pub fn is_opted_out(&self, user_id: u64) -> bool {
        match self.optouts.read() {
            Ok(set) => set.contains(&user_id),
            // A poisoned lock means a panic elsewhere; fail closed — drop audio.
            Err(_) => true,
        }
    }

    /// Replace the opt-out set (from Brain's `optouts` control message) and
    /// mark the process opt-out-synced.
    pub fn set_optouts(&self, users: HashSet<u64>) {
        if let Ok(mut set) = self.optouts.write() {
            *set = users;
            self.optouts_synced.store(true, Ordering::Release);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn speech_window_starts_empty() {
        let s = Shared::new();
        let g = s.guild(1);
        assert!(!g.speech_within(Duration::from_secs(3600)));
    }

    #[test]
    fn speech_window_registers_per_guild() {
        let s = Shared::new();
        let a = s.guild(1);
        let b = s.guild(2);
        a.note_speech();
        assert!(a.speech_within(Duration::from_secs(5)));
        assert!(!b.speech_within(Duration::from_secs(5)), "speech clocks are per guild");
    }

    #[test]
    fn optouts_start_unsynced_and_sync_on_first_set() {
        let s = Shared::new();
        assert!(!s.optouts_synced(), "a fresh process must fail closed");
        s.set_optouts(HashSet::new());
        assert!(s.optouts_synced(), "even an empty set counts as synced");
    }

    #[test]
    fn optouts_replace_wholesale() {
        let s = Shared::new();
        s.set_optouts([1u64, 2u64].into_iter().collect());
        assert!(s.is_opted_out(1));
        assert!(!s.is_opted_out(3));
        s.set_optouts(HashSet::new());
        assert!(!s.is_opted_out(1));
    }

    #[test]
    fn guild_state_is_shared_and_stable() {
        let s = Shared::new();
        let a = s.guild(7);
        a.set_channel_id(42);
        let b = s.guild(7);
        assert_eq!(b.channel_id(), 42, "same Arc across lookups");
        assert_eq!(s.guilds_snapshot().len(), 1);
    }

    #[test]
    fn roster_sorted_and_survives_nothing_special() {
        let s = Shared::new();
        let g = s.guild(1);
        if let Ok(mut map) = g.ssrc_users.write() {
            map.insert(30, 300);
            map.insert(10, 100);
        }
        assert_eq!(g.roster(), vec![(10, 100), (30, 300)]);
        g.reset_ssrc_state();
        assert!(g.roster().is_empty());
    }

    #[test]
    fn decode_watchdog_tracks_sustained_all_bad_runs() {
        let w = DecodeWatchdog::default();
        assert_eq!(w.observe(1_000, true, false), 0, "run starts on first bad tick");
        assert_eq!(w.observe(11_000, true, false), 10_000, "run length measured");
        assert_eq!(w.observe(41_000, true, false), 40_000);
    }

    #[test]
    fn decode_watchdog_one_good_decode_resets() {
        let w = DecodeWatchdog::default();
        w.observe(1_000, true, false);
        assert_eq!(w.observe(20_000, true, true), 0, "any healthy decode heals");
        assert_eq!(w.observe(21_000, true, false), 0, "next bad run starts fresh");
        assert_eq!(w.observe(31_000, true, false), 10_000);
    }

    #[test]
    fn decode_watchdog_silence_is_no_evidence() {
        let w = DecodeWatchdog::default();
        w.observe(1_000, true, false);
        assert_eq!(w.observe(5_000, false, false), 0, "silent tick reports nothing");
        // …but the run survives the pause: still wedged when packets resume.
        assert_eq!(w.observe(31_000, true, false), 30_000);
        // A quiet channel alone can never start a run.
        let idle = DecodeWatchdog::default();
        assert_eq!(idle.observe(9_999_999, false, false), 0);
    }

    #[test]
    fn decode_watchdog_reset_clears_the_run() {
        let w = DecodeWatchdog::default();
        w.observe(1_000, true, false);
        w.reset();
        assert_eq!(w.observe(50_000, true, false), 0, "fresh session, fresh runway");
    }

    #[test]
    fn handlers_installed_latch() {
        let s = Shared::new();
        let g = s.guild(1);
        assert!(!g.mark_handlers_installed(), "first caller installs");
        assert!(g.mark_handlers_installed(), "second caller must not");
        g.clear_handlers_installed();
        assert!(!g.mark_handlers_installed());
    }

    #[test]
    fn any_connected_tracks_guilds() {
        let s = Shared::new();
        assert!(!s.any_connected());
        let g = s.guild(1);
        g.set_connected(true);
        assert!(s.any_connected());
        g.set_connected(false);
        assert!(!s.any_connected());
    }
}
