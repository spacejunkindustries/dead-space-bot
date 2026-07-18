//! TTS playback: WAV bytes → Songbird input, with a priority queue,
//! talk-over suppression, and volume ducking (GDD §4 Ears table, §12).
//!
//! Priorities mirror Brain's `cortana/ipc.py` constants: 0 = low, 1 = normal,
//! 2 = alert. An alert preempts whatever is playing; anything below alert
//! waits while a human is speaking, and a non-alert utterance still blocked
//! at [`HOLD_MAX`] is DROPPED, not played late — a 3-seconds-late "Go
//! ahead." spoken over an FC mid-report is worse than none (GDD §12.2).
//! These are playback *mechanics*, fixed here on purpose — Ears carries no
//! meaning-level config.
//!
//! All playback state (queue, playing slot, hold timer) is **per guild**,
//! and the speech clock that drives suppression/ducking is the per-guild
//! clock in [`GuildVoice`] — speech in one voice call never gates another.

use std::collections::HashMap;
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
/// Longest a non-alert utterance is held back for human speech before being
/// dropped as stale (GDD §12.2).
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
    /// When this utterance was first held back for human speech while at the
    /// head of the queue. Carried by the job itself — not the engine — so an
    /// interleaving alert cannot reset the hold age of a blocked utterance
    /// and defeat the [`HOLD_MAX`] drop-stale guarantee (GDD §12.2).
    blocked_since: Option<Instant>,
}

impl TtsQueue {
    pub fn push(&mut self, job: TtsJob) {
        self.items.push(Queued {
            job,
            seq: self.next_seq,
            blocked_since: None,
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
        self.items.push(Queued {
            job,
            seq,
            blocked_since: None,
        });
    }

    /// Index of the head: highest priority, then oldest.
    fn head_idx(&self) -> Option<usize> {
        self.items
            .iter()
            .enumerate()
            .max_by(|(_, a), (_, b)| {
                (a.job.priority, std::cmp::Reverse(a.seq))
                    .cmp(&(b.job.priority, std::cmp::Reverse(b.seq)))
            })
            .map(|(i, _)| i)
    }

    /// Highest priority, then oldest.
    pub fn pop(&mut self) -> Option<TtsJob> {
        let idx = self.head_idx()?;
        Some(self.items.swap_remove(idx).job)
    }

    #[must_use]
    pub fn peek_priority(&self) -> Option<u8> {
        self.head_idx().map(|i| self.items[i].job.priority)
    }

    /// How long the head utterance has been held for human speech, as of
    /// `now`. Zero if the queue is empty or the head has never been held.
    #[must_use]
    pub fn head_blocked_for(&self, now: Instant) -> Duration {
        self.head_idx()
            .and_then(|i| self.items[i].blocked_since)
            .map(|since| now.saturating_duration_since(since))
            .unwrap_or(Duration::ZERO)
    }

    /// Stamp the head utterance as blocked-from-`now` unless it already
    /// carries a hold stamp. Returns `true` when the stamp was newly set
    /// (i.e. this is the first tick the utterance is held).
    pub fn mark_head_blocked(&mut self, now: Instant) -> bool {
        let Some(idx) = self.head_idx() else {
            return false;
        };
        let queued = &mut self.items[idx];
        if queued.blocked_since.is_some() {
            return false;
        }
        queued.blocked_since = Some(now);
        true
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

/// What to do with the head-of-queue utterance right now (GDD §12.2).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HoldOutcome {
    /// Play it now.
    Start,
    /// A human is speaking; keep holding.
    Hold,
    /// Held past [`HOLD_MAX`] with a human still speaking: the utterance is
    /// stale — drop it rather than talk over an FC mid-report.
    Drop,
}

/// Talk-over suppression: alerts always start; anything else waits for
/// silence, and a non-alert utterance still blocked at [`HOLD_MAX`] is
/// dropped as stale.
#[must_use]
pub fn hold_outcome(priority: u8, human_speaking: bool, blocked_for: Duration) -> HoldOutcome {
    if priority >= PRIORITY_ALERT || !human_speaking {
        HoldOutcome::Start
    } else if blocked_for >= HOLD_MAX {
        HoldOutcome::Drop
    } else {
        HoldOutcome::Hold
    }
}

struct Playing {
    handle: TrackHandle,
    job: TtsJob,
    ducked: bool,
}

/// Per-guild playback engine: queue and playing slot. Hold timers ride on
/// the queued utterances themselves (see [`Queued::blocked_since`]); the
/// speech clock lives in the per-guild [`crate::state::GuildVoice`].
#[derive(Default)]
struct GuildPlayback {
    queue: TtsQueue,
    playing: Option<Playing>,
}

/// Run the playback task until the TTS channel closes.
pub async fn run_playback(
    manager: Arc<Songbird>,
    shared: Arc<Shared>,
    mut rx: mpsc::UnboundedReceiver<TtsJob>,
) {
    let mut engines: HashMap<u64, GuildPlayback> = HashMap::new();
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
                    "tts job received"
                );
                let engine = engines.entry(job.guild_id).or_default();
                debug!(
                    guild_id = job.guild_id,
                    queued = engine.queue.len(),
                    "guild playback engine selected"
                );
                if let Some(current) = engine.playing.take() {
                    if should_preempt(current.job.priority, job.priority) {
                        info!(
                            guild_id = job.guild_id,
                            interrupted_priority = current.job.priority,
                            alert_priority = job.priority,
                            "alert preempts current utterance"
                        );
                        if let Err(e) = current.handle.stop() {
                            debug!(error = %e, "stopping preempted track");
                        }
                        // Replay the interrupted utterance after the alert.
                        engine.queue.push_front(current.job);
                    } else {
                        engine.playing = Some(current);
                    }
                }
                engine.queue.push(job);
            },
            _ = tick.tick() => {},
        }

        for (&guild_id, engine) in &mut engines {
            maintain_guild(&manager, &shared, guild_id, engine).await;
        }
    }
    debug!("tts channel closed");
}

/// One maintenance pass for one guild: reap the finished track, apply
/// ducking, and start (or drop) the next utterance.
async fn maintain_guild(
    manager: &Arc<Songbird>,
    shared: &Arc<Shared>,
    guild_id: u64,
    engine: &mut GuildPlayback,
) {
    let human = shared.guild(guild_id).speech_within(SPEECH_ACTIVE_WINDOW);

    // Reap the current track if it finished (or its handle died).
    if let Some(current) = engine.playing.take() {
        match current.handle.get_info().await {
            Ok(state) if state.playing.is_done() => {
                debug!(guild_id, "utterance finished");
            },
            Ok(_state) => {
                engine.playing = Some(duck(current, human));
            },
            Err(_) => {
                debug!(guild_id, "track handle gone; treating utterance as finished");
            },
        }
    }

    // Start the next utterance if we are idle and allowed to speak.
    if engine.playing.is_some() || engine.queue.is_empty() {
        return;
    }
    let Some(priority) = engine.queue.peek_priority() else {
        return;
    };
    // Hold age is per queued utterance, not per engine: an alert starting
    // in between must not reset a blocked lower-priority utterance's clock
    // (that would let it play far past HOLD_MAX, or never drop at all).
    let now = Instant::now();
    let blocked_for = engine.queue.head_blocked_for(now);
    match hold_outcome(priority, human, blocked_for) {
        HoldOutcome::Start => {
            if let Some(job) = engine.queue.pop() {
                engine.playing = start(manager, human, job).await;
            }
        },
        HoldOutcome::Hold => {
            if engine.queue.mark_head_blocked(now) {
                debug!(guild_id, priority, "holding utterance for human speech");
            }
        },
        HoldOutcome::Drop => {
            if let Some(job) = engine.queue.pop() {
                info!(
                    guild_id,
                    priority = priority_name(job.priority),
                    held_ms = blocked_for.as_millis() as u64,
                    "dropping stale utterance held past HOLD_MAX (human still speaking)"
                );
            }
        },
    }
}

/// Apply/release ducking on the playing track when human speech starts/stops.
fn duck(mut current: Playing, human: bool) -> Playing {
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
async fn start(manager: &Arc<Songbird>, human_speaking: bool, job: TtsJob) -> Option<Playing> {
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
    if human_speaking && handle.set_volume(DUCK_TO).is_ok() {
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
    fn hold_age_survives_alert_interleaving() {
        // The GDD §12.2 regression this guards: a NORMAL utterance is held
        // for human speech, an ALERT interleaves and plays, and the NORMAL
        // utterance must still be droppable at its original HOLD_MAX
        // deadline — the alert must not reset its hold clock.
        let mut q = TtsQueue::default();
        q.push(job(PRIORITY_NORMAL, 1));
        let t0 = Instant::now();
        assert!(q.mark_head_blocked(t0));

        // Alert arrives and becomes head; it carries no hold stamp itself.
        q.push(job(PRIORITY_ALERT, 2));
        assert_eq!(q.peek_priority(), Some(PRIORITY_ALERT));
        assert_eq!(q.head_blocked_for(t0 + HOLD_MAX), Duration::ZERO);
        assert_eq!(q.pop().map(|j| j.wav[0]), Some(2));

        // Back at the NORMAL head: its hold age spans the alert playback.
        let after_alert = t0 + HOLD_MAX;
        assert!(q.head_blocked_for(after_alert) >= HOLD_MAX);
        assert_eq!(
            hold_outcome(PRIORITY_NORMAL, true, q.head_blocked_for(after_alert)),
            HoldOutcome::Drop
        );
    }

    #[test]
    fn mark_head_blocked_stamps_once() {
        let mut q = TtsQueue::default();
        assert!(!q.mark_head_blocked(Instant::now())); // empty queue: no-op
        q.push(job(PRIORITY_NORMAL, 1));
        let t0 = Instant::now();
        assert!(q.mark_head_blocked(t0));
        // A later re-mark must not restart the clock.
        assert!(!q.mark_head_blocked(t0 + Duration::from_secs(1)));
        assert_eq!(q.head_blocked_for(t0 + Duration::from_secs(2)), Duration::from_secs(2));
    }

    #[test]
    fn head_blocked_for_defaults_to_zero() {
        let mut q = TtsQueue::default();
        assert_eq!(q.head_blocked_for(Instant::now()), Duration::ZERO);
        q.push(job(PRIORITY_NORMAL, 1));
        // Never held: zero, and a stamp earlier than `now` never underflows.
        let now = Instant::now();
        assert_eq!(q.head_blocked_for(now), Duration::ZERO);
        assert!(q.mark_head_blocked(now));
        assert_eq!(q.head_blocked_for(now), Duration::ZERO);
    }

    #[test]
    fn pop_discards_hold_stamp_with_the_job() {
        // Once an utterance leaves the queue its hold history goes with it;
        // the next head starts a fresh clock.
        let mut q = TtsQueue::default();
        q.push(job(PRIORITY_NORMAL, 1));
        q.push(job(PRIORITY_NORMAL, 2));
        let t0 = Instant::now();
        assert!(q.mark_head_blocked(t0));
        assert_eq!(q.pop().map(|j| j.wav[0]), Some(1));
        assert_eq!(q.head_blocked_for(t0 + HOLD_MAX), Duration::ZERO);
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
        assert_eq!(
            hold_outcome(PRIORITY_ALERT, true, Duration::ZERO),
            HoldOutcome::Start
        );
        // Non-urgent waits while a human is speaking...
        assert_eq!(
            hold_outcome(PRIORITY_NORMAL, true, Duration::from_secs(1)),
            HoldOutcome::Hold
        );
        assert_eq!(
            hold_outcome(PRIORITY_LOW, true, Duration::from_secs(2)),
            HoldOutcome::Hold
        );
        // ...and past the cap the stale utterance is DROPPED, never played
        // late over the ongoing report (GDD §12.2).
        assert_eq!(
            hold_outcome(PRIORITY_NORMAL, true, HOLD_MAX),
            HoldOutcome::Drop
        );
        assert_eq!(hold_outcome(PRIORITY_LOW, true, HOLD_MAX), HoldOutcome::Drop);
        // Silence releases immediately.
        assert_eq!(
            hold_outcome(PRIORITY_LOW, false, Duration::ZERO),
            HoldOutcome::Start
        );
        // Silence after a long hold still plays (the pilot stopped talking
        // before the cap): drop only fires while a human is speaking.
        assert_eq!(
            hold_outcome(PRIORITY_NORMAL, false, HOLD_MAX),
            HoldOutcome::Start
        );
    }
}
