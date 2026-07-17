//! State shared between the voice handlers, the IPC task, and playback.

use std::collections::HashSet;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::RwLock;
use std::time::{Duration, Instant};

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

/// Process-wide shared state.
#[derive(Debug)]
pub struct Shared {
    /// Users whose audio must be dropped *before* it crosses the IPC boundary
    /// (hard constraint: opt-out is enforced in Ears). Replaced wholesale on
    /// every `optouts` control message from Brain.
    pub optouts: RwLock<HashSet<u64>>,
    /// Heartbeat counters.
    pub stats: VoiceStats,
    /// Millisecond offset (from `epoch`) of the most recent tick that carried
    /// human audio. `0` = never. Drives talk-over suppression and ducking.
    last_speech_ms: AtomicU64,
    epoch: Instant,
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
            stats: VoiceStats::default(),
            last_speech_ms: AtomicU64::new(0),
            epoch: Instant::now(),
        }
    }

    /// Record that human speech was heard "now".
    pub fn note_speech(&self) {
        let ms = self.epoch.elapsed().as_millis() as u64;
        // Never store 0 ("never"); speech in the first millisecond still counts.
        self.last_speech_ms.store(ms.max(1), Ordering::Relaxed);
    }

    /// Whether human speech was heard within the given window.
    #[must_use]
    pub fn speech_within(&self, window: Duration) -> bool {
        let last = self.last_speech_ms.load(Ordering::Relaxed);
        if last == 0 {
            return false;
        }
        let now = self.epoch.elapsed().as_millis() as u64;
        now.saturating_sub(last) <= window.as_millis() as u64
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

    /// Replace the opt-out set (from Brain's `optouts` control message).
    pub fn set_optouts(&self, users: HashSet<u64>) {
        if let Ok(mut set) = self.optouts.write() {
            *set = users;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn speech_window_starts_empty() {
        let s = Shared::new();
        assert!(!s.speech_within(Duration::from_secs(3600)));
    }

    #[test]
    fn speech_window_registers() {
        let s = Shared::new();
        s.note_speech();
        assert!(s.speech_within(Duration::from_secs(5)));
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
}
