//! TTS playback: WAV bytes → Songbird input, with a priority queue,
//! talk-over suppression, and volume ducking (GDD §4 Ears table, §12).
//!
//! Priorities mirror Brain's `aura/ipc.py` constants: 0 = low, 1 = normal,
//! 2 = alert. An alert preempts whatever is playing; anything below alert
//! waits (up to a cap) while a human is speaking. These are playback
//! *mechanics*, fixed here on purpose — Ears carries no meaning-level config.

use std::num::NonZeroU64;
use std::sync::Arc;
use std::time::Duration;

use songbird::input::Input;
use songbird::tracks::TrackHandle;
use songbird::Songbird;
use tokio::sync::mpsc;
use tokio::time::Instant;
use tracing::{debug, info, warn};

use crate::state::Shared;

/// Priority values on the wire (GDD §15 type 0x03).
pub const PRIORITY_LOW: u8 = 0;
pub const PRIORITY_NORMAL: u8 = 1;
pub const PRIORITY_ALERT: u8 = 2;

/// Volume while a human is talking over playback.
const DUCK_TO: f32 = 0.6;
/// How recently a tick must have carried human audio to count as
/// "someone is speaking right now" (a handful of 20 ms ticks).
const SPEECH_ACTIVE_WINDOW: Duration = Duration::from_millis(250);
/// Longest a non-alert utterance is held back for human speech.
const HOLD_MAX: Duration = Duration::from_secs(3);
/// Queue/track maintenance cadence.
const TICK: Duration = Duration::from_millis(100);

/// One TTS utterance from Brain.
#[derive(Debug, Clone)]
pub struct TtsJob {
    pub guild_id: u64,
    pub priority: u8,
    pub wav: Vec<u8>,
}

/// Priority queue: higher priority first, FIFO within a priority class.
#[derive(Debug, Default)]
pub struct TtsQueue {
    items: Vec<Queued>,
    next_seq: i64,
}

#[derive(Debug)]
struct Queued {
    job: TtsJob,
    /// FIFO ordering key within a priority class; requeued (preempted) jobs
    /// get a key below the current minimum so they replay first.
    seq: i64,
}

impl TtsQueue {
    pub fn push(&mut self, job: TtsJob) {
        self.items.push(Queued {
            job,
            seq: self.next_seq,
        });
        self.next_seq += 1;
    }

    /// Re-insert a preempted job at the head of its priority class so it
    /// replays before anything queued behind it.
    pub fn push_front(&mut self, job: TtsJob) {
        let seq = self
            .items
            .iter()
            .map(|q| q.seq)
            .min()
            .unwrap_or(self.next_seq)
            - 1;
        self.items.push(Queued { job, seq });
    }

    /// Highest priority, then oldest.
    pub fn pop(&mut self) -> Option<TtsJob> {
        let idx = self
            .items
            .iter()
            .enumerate()
            .max_by(|(_, a), (_, b)| {
                (a.job.priority, std::cmp::Reverse(a.seq))
                    .cmp(&(b.job.priority, std::cmp::Reverse(b.seq)))
            })
            .map(|(i, _)| i)?;
        Some(self.items.swap_remove(idx).job)
    }

    #[must_use]
    pub fn peek_priority(&self) -> Option<u8> {
        self.items
            .iter()
            .max_by(|a, b| {
                (a.job.priority, std::cmp::Reverse(a.seq))
                    .cmp(&(b.job.priority, std::cmp::Reverse(b.seq)))
            })
            .map(|q| q.job.priority)
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.items.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }
}

/// Human-readable priority label for logs.
#[must_use]
pub fn priority_name(priority: u8) -> &'static str {
    match priority {
        PRIORITY_LOW => "low",
        PRIORITY_NORMAL => "normal",
        PRIORITY_ALERT => "alert",
        _ => "above-alert",
    }
}

/// Whether an incoming utterance should cut off the one playing now.
/// Only alerts preempt; a normal message never interrupts speech in flight.
#[must_use]
pub fn should_preempt(current_priority: u8, incoming_priority: u8) -> bool {
    incoming_priority >= PRIORITY_ALERT && incoming_priority > current_priority
}

/// Talk-over suppression (GDD §12.2): may the head-of-queue utterance start
/// now? Alerts always start; anything else waits for silence, but never more
/// than [`HOLD_MAX`].
#[must_use]
pub fn may_start(priority: u8, human_speaking: bool, blocked_for: Duration) -> bool {
    priority >= PRIORITY_ALERT || !human_speaking || blocked_for >= HOLD_MAX
}

struct Playing {
    handle: TrackHandle,
    job: TtsJob,
    ducked: bool,
}

/// Run the playback task until the TTS channel closes.
pub async fn run_playback(
    manager: Arc<Songbird>,
    shared: Arc<Shared>,
    mut rx: mpsc::UnboundedReceiver<TtsJob>,
) {
    let mut queue = TtsQueue::default();
    let mut playing: Option<Playing> = None;
    let mut blocked_since: Option<Instant> = None;
    let mut tick = tokio::time::interval(TICK);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

    loop {
        tokio::select! {
            job = rx.recv() => {
                let Some(job) = job else { break };
                debug!(
                    guild_id = job.guild_id,
                    priority = priority_name(job.priority),
                    wav_bytes = job.wav.len(),
                    queued = queue.len(),
                    "tts job received"
                );
                if let Some(current) = playing.take() {
                    if should_preempt(current.job.priority, job.priority) {
                        info!(
                            interrupted_priority = current.job.priority,
                            alert_priority = job.priority,
                            "alert preempts current utterance"
                        );
                        if let Err(e) = current.handle.stop() {
                            debug!(error = %e, "stopping preempted track");
                        }
                        // Replay the interrupted utterance after the alert.
                        queue.push_front(current.job);
                    } else {
                        playing = Some(current);
                    }
                }
                queue.push(job);
            },
            _ = tick.tick() => {},
        }

        // Reap the current track if it finished (or its handle died).
        if let Some(current) = playing.take() {
            match current.handle.get_info().await {
                Ok(state) if state.playing.is_done() => {
                    debug!("utterance finished");
                },
                Ok(_state) => {
                    playing = Some(duck(current, &shared));
                },
                Err(_) => {
                    debug!("track handle gone; treating utterance as finished");
                },
            }
        }

        // Start the next utterance if we are idle and allowed to speak.
        if playing.is_none() {
            if queue.is_empty() {
                blocked_since = None;
            } else if let Some(priority) = queue.peek_priority() {
                let human = shared.speech_within(SPEECH_ACTIVE_WINDOW);
                let blocked_for = blocked_since
                    .map(|t| t.elapsed())
                    .unwrap_or(Duration::ZERO);
                if may_start(priority, human, blocked_for) {
                    blocked_since = None;
                    if let Some(job) = queue.pop() {
                        playing = start(&manager, &shared, job).await;
                    }
                } else if blocked_since.is_none() {
                    debug!(priority, "holding utterance for human speech");
                    blocked_since = Some(Instant::now());
                }
            } else {
                blocked_since = None;
            }
        }
    }
    debug!("tts channel closed");
}

/// Apply/release ducking on the playing track when human speech starts/stops.
fn duck(mut current: Playing, shared: &Arc<Shared>) -> Playing {
    let human = shared.speech_within(SPEECH_ACTIVE_WINDOW);
    if human != current.ducked {
        let target = if human { DUCK_TO } else { 1.0 };
        match current.handle.set_volume(target) {
            Ok(()) => current.ducked = human,
            Err(e) => debug!(error = %e, "set_volume on finished track"),
        }
    }
    current
}

/// Hand the WAV bytes to Songbird on the guild's call.
async fn start(
    manager: &Arc<Songbird>,
    shared: &Arc<Shared>,
    job: TtsJob,
) -> Option<Playing> {
    let Some(gid) = NonZeroU64::new(job.guild_id) else {
        warn!(guild_id = job.guild_id, "tts for zero guild id; dropping");
        return None;
    };
    let Some(call_lock) = manager.get(songbird::id::GuildId(gid)) else {
        warn!(
            guild_id = job.guild_id,
            "tts for a guild with no voice call; dropping"
        );
        return None;
    };

    // In-memory bytes become an Input via Songbird's blanket
    // `impl<T: AsRef<[u8]>> From<T> for Input`; Symphonia's WAV reader
    // (enabled via our symphonia features) parses them on the mixer thread.
    // The clone keeps a copy for replay if an alert preempts this utterance.
    let input: Input = job.wav.clone().into();

    let handle = {
        let mut call = call_lock.lock().await;
        call.play_input(input)
    };

    // If someone is already talking, start ducked.
    let mut ducked = false;
    if shared.speech_within(SPEECH_ACTIVE_WINDOW) && handle.set_volume(DUCK_TO).is_ok() {
        ducked = true;
    }

    debug!(
        guild_id = job.guild_id,
        priority = job.priority,
        ducked,
        "utterance started"
    );
    Some(Playing {
        handle,
        job,
        ducked,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn job(priority: u8, tag: u8) -> TtsJob {
        TtsJob {
            guild_id: 1,
            priority,
            wav: vec![tag],
        }
    }

    #[test]
    fn queue_orders_by_priority_then_fifo() {
        let mut q = TtsQueue::default();
        q.push(job(PRIORITY_NORMAL, 1));
        q.push(job(PRIORITY_LOW, 2));
        q.push(job(PRIORITY_ALERT, 3));
        q.push(job(PRIORITY_NORMAL, 4));
        let order: Vec<u8> = std::iter::from_fn(|| q.pop().map(|j| j.wav[0])).collect();
        assert_eq!(order, vec![3, 1, 4, 2]);
    }

    #[test]
    fn queue_peek_matches_pop() {
        let mut q = TtsQueue::default();
        assert_eq!(q.peek_priority(), None);
        q.push(job(PRIORITY_LOW, 1));
        q.push(job(PRIORITY_ALERT, 2));
        assert_eq!(q.peek_priority(), Some(PRIORITY_ALERT));
        assert_eq!(q.pop().map(|j| j.wav[0]), Some(2));
        assert_eq!(q.peek_priority(), Some(PRIORITY_LOW));
    }

    #[test]
    fn push_front_replays_before_same_class() {
        let mut q = TtsQueue::default();
        q.push(job(PRIORITY_NORMAL, 1));
        q.push(job(PRIORITY_NORMAL, 2));
        q.push_front(job(PRIORITY_NORMAL, 9)); // interrupted job comes back
        let order: Vec<u8> = std::iter::from_fn(|| q.pop().map(|j| j.wav[0])).collect();
        assert_eq!(order, vec![9, 1, 2]);
    }

    #[test]
    fn queue_len_tracks() {
        let mut q = TtsQueue::default();
        assert!(q.is_empty());
        q.push(job(PRIORITY_LOW, 1));
        assert_eq!(q.len(), 1);
        let _ = q.pop();
        assert!(q.is_empty());
    }

    #[test]
    fn only_alerts_preempt() {
        assert!(should_preempt(PRIORITY_LOW, PRIORITY_ALERT));
        assert!(should_preempt(PRIORITY_NORMAL, PRIORITY_ALERT));
        assert!(!should_preempt(PRIORITY_ALERT, PRIORITY_ALERT));
        assert!(!should_preempt(PRIORITY_LOW, PRIORITY_NORMAL));
        assert!(!should_preempt(PRIORITY_NORMAL, PRIORITY_LOW));
    }

    #[test]
    fn talk_over_suppression_rules() {
        // Alerts never wait.
        assert!(may_start(PRIORITY_ALERT, true, Duration::ZERO));
        // Non-urgent waits while a human is speaking...
        assert!(!may_start(PRIORITY_NORMAL, true, Duration::from_secs(1)));
        assert!(!may_start(PRIORITY_LOW, true, Duration::from_secs(2)));
        // ...but not past the cap.
        assert!(may_start(PRIORITY_NORMAL, true, HOLD_MAX));
        // Silence releases immediately.
        assert!(may_start(PRIORITY_LOW, false, Duration::ZERO));
    }
}
