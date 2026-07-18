"""Degradation detection and #bot-health reporting — GDD §20 / §16 (health).

CORTANA is engineered to survive the loss of its own headline feature: voice
receive is undocumented and can break without notice. This module watches
for exactly the failure signatures the §20 table names and raises the
``degraded`` flag the rest of the system keys off (the engine speaks/posts
"Voice offline, use slash commands" while it is set):

- **Voice receive dead** — no audio frame for ``health.voice_silence_alarm_s``
  while Ears' heartbeat says it is connected AND ≥2 unmuted humans are in the
  channel. Silence alone proves nothing; silence *with an audience* does.
- **Ears down** — heartbeat miss. The text path is unaffected and says so.
- **STT rot** — 10 consecutive LOW-tier resolutions means something upstream
  (audio path, model, mic mix) is broken, not that ten pilots mumbled.
- **STT watchdog latch** — the transcriber's respawn cap fired
  (``TimeoutTranscriber.degraded``): decodes are refused until a reload.
- **Wake fault** — the wake model pool latched after a build failure
  (``OpenWakeWordDetector.faulted``): frames flow but nothing is scored.

The wake/STT probes arrive via :meth:`set_wake_probe` / :meth:`set_stt_probe`
(injected callables — this module imports neither audio component). The wake
stage counters (frames_seen → vad_speech → inferences → hits/near_misses)
ride the periodic report and are exposed for the ``/botstatus`` cog, so a
silent wake death is a visible zero instead of a green status.

This module DETECTS; the :class:`cortana.alarms.AlarmBus` (GDD §11.3)
ANNOUNCES. Every degradation transition raises/clears its alarm code
(EARS_DOWN, VOICE_ABSENT, WAKE_FAULTED, STT_DEGRADED) through the bus wired
by :meth:`set_alarm_bus` — one edited-in-place #bot-health card per episode
instead of the old one-shot posts. Without a bus (tests, partial wiring) the
transition is journald-only.

The hourly report still goes through an injected async
``post_fn(content, embed)`` — this module never imports discord; the embed is
a plain ``discord.Embed.from_dict`` payload dict assembled here. All time is
injected: a monotonic ``clock`` callable for intervals, and ``datetime``
timestamps enter only through :meth:`build_report_embed`'s caller-supplied
``now``.

Hot-path discipline: :meth:`note_audio` runs for every 20 ms frame and does
one attribute store — no allocation, no logging, no locks.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from cortana.alarms import AlarmBus, AlarmCode, AlarmSeverity
from cortana.config import ConfigHolder
from cortana.types import Tier

__all__ = ["HEARTBEAT_TIMEOUT_S", "LOW_TIER_ALERT_STREAK", "HealthReporter"]

log = structlog.get_logger(__name__)

#: Ears heartbeats ride the control channel every few seconds; missing this
#: many seconds of them means the process (or the socket) is gone.
HEARTBEAT_TIMEOUT_S = 15.0

#: §20: "10 consecutive low-tier results" → degrade and alert.
LOW_TIER_ALERT_STREAK = 10

#: STT confidence ring size for the hourly report.
_CONFIDENCE_RING = 100

PostFn = Callable[[str, dict[str, Any] | None], Awaitable[None]]


class HealthReporter:
    """Counters, degradation detection, and the hourly #bot-health embed.

    Wire-up (all optional signals default to "unknown", which never alarms):

    - ``note_audio()`` — from the IPC audio hot path.
    - ``note_heartbeat(msg)`` — from the control dispatcher (``t=heartbeat``).
    - ``set_humans_present(n)`` — from the voice gateway's channel census.
    - ``record_*()`` — from the engine/wake/STT paths.
    - ``await check()`` — from a supervised task every few seconds; performs
      all detection and posting.
    """

    def __init__(
        self,
        holder: ConfigHolder,
        post_fn: PostFn,
        *,
        clock: Callable[[], float] = time.monotonic,
        heartbeat_timeout_s: float = HEARTBEAT_TIMEOUT_S,
    ) -> None:
        self._holder = holder
        self._post = post_fn
        self._clock = clock
        self._heartbeat_timeout_s = heartbeat_timeout_s

        started = clock()
        self._started_at = started
        self._last_report_at = started

        # Signals (monotonic timestamps; None = never seen).
        self._last_audio_at: float | None = None
        self._last_heartbeat_at: float | None = None
        self._ears_reports_connected = False
        self._humans_present = 0

        # Degradation state (each flag posts once per episode).
        self._voice_offline = False
        self._ears_down = False
        self._stt_degraded = False

        # Counters since the last hourly report.
        self._incidents_posted = 0
        self._incidents_folded = 0
        self._mentions_sent = 0
        self._wake_hits = 0
        self._commands_rejected = 0
        # Discord post/edit failures (channel 403s etc.): counted per report
        # window. The per-failure POST_FAILURE alarm is raised by the Poster
        # itself (bot.py) — this is just the report-window tally.
        self._post_failures = 0

        # STT quality: rolling confidence ring + consecutive-LOW streak.
        self._confidences: deque[float] = deque(maxlen=_CONFIDENCE_RING)
        self._low_streak = 0

        # Injected audio-pipeline probes (None until wired — never alarm).
        self._wake_counters_fn: Callable[[], dict[str, int]] | None = None
        self._wake_faulted_fn: Callable[[], bool] | None = None
        self._stt_watchdog_fn: Callable[[], bool] | None = None
        self._wake_fault_announced = False
        self._stt_watchdog_announced = False
        # Most recent Ears driver_disconnected control event (IPC v2), kept
        # for the ops surface: a voice session dying under Ears must never be
        # invisible to whoever reads the health report.
        self._last_driver_event: dict[str, Any] | None = None
        self._stt_conf_alarmed = False

        # The operator alarm surface (GDD §11.3); None = journald only.
        self._alarms: AlarmBus | None = None

    # ── signals ──────────────────────────────────────────────────────────────

    def note_audio(self) -> None:
        """An audio frame arrived from Ears. HOT PATH — one store, nothing else."""
        self._last_audio_at = self._clock()

    def note_heartbeat(self, msg: dict[str, Any]) -> None:
        """An Ears heartbeat control message arrived (GDD §15)."""
        self._last_heartbeat_at = self._clock()
        self._ears_reports_connected = bool(msg.get("connected", False))

    def set_humans_present(self, count: int) -> None:
        """Unmuted human count in the watched voice channel (voice gateway)."""
        self._humans_present = count

    def set_wake_probe(
        self, counters: Callable[[], dict[str, int]], faulted: Callable[[], bool]
    ) -> None:
        """Wire the wake detector's stage counters + fault flag (``__main__``)."""
        self._wake_counters_fn = counters
        self._wake_faulted_fn = faulted

    def set_stt_probe(self, watchdog_degraded: Callable[[], bool]) -> None:
        """Wire the transcriber's watchdog-latch flag (``__main__``)."""
        self._stt_watchdog_fn = watchdog_degraded

    def note_driver_event(self, msg: dict[str, Any]) -> None:
        """An Ears ``driver_disconnected`` control event arrived (GDD §15)."""
        self._last_driver_event = dict(msg)

    @property
    def last_driver_event(self) -> dict[str, Any] | None:
        """The most recent Ears driver_disconnected event, verbatim."""
        return self._last_driver_event

    def set_alarm_bus(self, alarms: AlarmBus) -> None:
        """Wire the AlarmBus every degradation transition reports through."""
        self._alarms = alarms

    # ── counters ─────────────────────────────────────────────────────────────

    def record_incident_posted(self) -> None:
        self._incidents_posted += 1

    def record_incident_folded(self) -> None:
        self._incidents_folded += 1

    def record_mention(self) -> None:
        self._mentions_sent += 1

    def record_wake_hit(self) -> None:
        self._wake_hits += 1

    def record_rejected(self) -> None:
        self._commands_rejected += 1

    def record_post_failure(self) -> None:
        """A Discord card post/edit failed (403, deleted channel, REST error).

        Counted into the hourly report; the POST_FAILURE alarm card is
        raised by the Poster itself (bot.py), which has the context."""
        self._post_failures += 1

    def record_stt(self, confidence: float, tier: Tier) -> None:
        """One STT+resolution result: confidence ring + LOW-tier streak (§20)."""
        self._confidences.append(confidence)
        if tier is Tier.LOW:
            self._low_streak += 1
        else:
            self._low_streak = 0
            if self._stt_degraded:
                self._stt_degraded = False
                log.info("stt_degradation_cleared")

    # ── state the rest of the system reads ───────────────────────────────────

    @property
    def degraded(self) -> bool:
        """True while any §20 degradation is active — the engine's cue to
        steer pilots to slash commands."""
        return self._voice_offline or self._ears_down or self._stt_degraded

    @property
    def voice_offline(self) -> bool:
        return self._voice_offline

    @property
    def ears_down(self) -> bool:
        return self._ears_down

    @property
    def stt_degraded(self) -> bool:
        return self._stt_degraded

    @property
    def low_streak(self) -> int:
        return self._low_streak

    @property
    def wake_counters(self) -> dict[str, int]:
        """Snapshot of the wake pipeline stage counters ({} until wired) —
        for the periodic report and the future ``/status`` cog."""
        return dict(self._wake_counters_fn()) if self._wake_counters_fn is not None else {}

    @property
    def wake_faulted(self) -> bool:
        """True while the wake model pool is latched faulted (build failure)."""
        return bool(self._wake_faulted_fn()) if self._wake_faulted_fn is not None else False

    @property
    def stt_watchdog_degraded(self) -> bool:
        """True while the STT watchdog respawn cap is latched."""
        return bool(self._stt_watchdog_fn()) if self._stt_watchdog_fn is not None else False

    # ── detection + reporting (call every few seconds) ───────────────────────

    async def check(self) -> None:
        """Run all §20 detections and the hourly report cadence."""
        now = self._clock()
        await self._check_ears(now)
        await self._check_voice(now)
        await self._check_stt()
        await self._check_stt_watchdog()
        await self._check_wake_fault()
        await self._maybe_report(now)

    async def _raise(
        self,
        code: AlarmCode,
        severity: AlarmSeverity,
        summary: str,
        fix_hint: str,
        key: str | None = None,
    ) -> None:
        if self._alarms is None:
            log.warning("alarm_bus_unwired", code=code.value, summary=summary)
            return
        await self._alarms.raise_alarm(code, severity, summary, fix_hint, key=key)

    async def _clear(self, code: AlarmCode, key: str | None = None) -> None:
        if self._alarms is not None:
            await self._alarms.clear(code, key=key)

    async def _check_stt_watchdog(self) -> None:
        """The transcriber latched degraded (respawn cap) — one alarm card."""
        latched = self.stt_watchdog_degraded
        if latched and not self._stt_watchdog_announced:
            self._stt_watchdog_announced = True
            log.warning("stt_watchdog_latch_announced")
            await self._raise(
                AlarmCode.STT_DEGRADED,
                AlarmSeverity.CRITICAL,
                "STT watchdog respawn cap reached — transcription is latched off. "
                "Voice commands are rejected; slash commands and the incident "
                "engine are unaffected.",
                "run `/reload` (or restart cortana-brain) to clear the latch",
                key="watchdog",
            )
        elif not latched and self._stt_watchdog_announced:
            self._stt_watchdog_announced = False
            log.info("stt_watchdog_latch_cleared")
            await self._clear(AlarmCode.STT_DEGRADED, key="watchdog")

    async def _check_wake_fault(self) -> None:
        """The wake model pool latched faulted (build failure) — one card."""
        faulted = self.wake_faulted
        if faulted and not self._wake_fault_announced:
            self._wake_fault_announced = True
            log.warning("wake_fault_announced")
            await self._raise(
                AlarmCode.WAKE_FAULTED,
                AlarmSeverity.CRITICAL,
                "Wake-word model failed to build — wake detection is offline "
                "(audio still flows, nothing is scored). Slash commands are "
                "unaffected.",
                "check `wake.model` and the openwakeword install, then `/reload` or restart",
            )
        elif not faulted and self._wake_fault_announced:
            self._wake_fault_announced = False
            log.info("wake_fault_recovered")
            await self._clear(AlarmCode.WAKE_FAULTED)

    async def _check_ears(self, now: float) -> None:
        seen = self._last_heartbeat_at
        alive = seen is not None and (now - seen) <= self._heartbeat_timeout_s
        # Never-connected counts as down too: an Ears that fails at boot sends
        # no heartbeat at all, and gating the alarm on "saw one once" left
        # CORTANA silently deaf with a green systemd status. Give startup one
        # timeout window of grace before alarming.
        never_connected = seen is None and (now - self._started_at) > self._heartbeat_timeout_s
        if never_connected and not self._ears_down:
            self._ears_down = True
            log.warning("ears_never_connected", waited_s=round(now - self._started_at, 1))
            await self._raise(
                AlarmCode.EARS_DOWN,
                AlarmSeverity.CRITICAL,
                "Ears has not connected since startup — voice is down. Slash "
                "commands and the incident engine are unaffected.",
                "check `systemctl status cortana-ears` and its journal",
            )
            return
        if not alive and seen is not None and not self._ears_down:
            self._ears_down = True
            log.warning("ears_heartbeat_missed", last_seen_s_ago=round(now - seen, 1))
            await self._raise(
                AlarmCode.EARS_DOWN,
                AlarmSeverity.CRITICAL,
                "Ears heartbeat missed — voice path degraded. Slash commands "
                "and the incident engine are unaffected.",
                "check `systemctl status cortana-ears`; Brain re-adopts it on "
                "reconnect automatically",
            )
        elif alive and self._ears_down:
            self._ears_down = False
            log.info("ears_heartbeat_recovered")
            await self._clear(AlarmCode.EARS_DOWN)

    async def _check_voice(self, now: float) -> None:
        """§20 row 1: no audio for the alarm window while Ears is connected
        and at least two unmuted humans are in channel."""
        alarm_s = self._holder.current.health.voice_silence_alarm_s
        audience = (
            self._ears_reports_connected and not self._ears_down and self._humans_present >= 2
        )
        last = self._last_audio_at
        silent = audience and (
            (last is None and (now - self._started_at) >= alarm_s)
            or (last is not None and (now - last) >= alarm_s)
        )
        if silent and not self._voice_offline:
            self._voice_offline = True
            log.warning(
                "voice_receive_offline",
                silence_s=round(now - (last if last is not None else self._started_at), 1),
                humans_present=self._humans_present,
            )
            await self._raise(
                AlarmCode.VOICE_ABSENT,
                AlarmSeverity.CRITICAL,
                f"No audio received for {alarm_s}s with pilots in channel — "
                "voice receive is dead. Pilots should use `/under-attack`, "
                "`/help-me` and `/hostiles`; every slash command keeps working.",
                "check cortana-ears and the Discord voice connection; this "
                "clears itself when audio flows again",
            )
        elif self._voice_offline and last is not None and (now - last) < alarm_s:
            self._voice_offline = False
            log.info("voice_receive_recovered")
            await self._clear(AlarmCode.VOICE_ABSENT)

    async def _check_stt(self) -> None:
        if self._low_streak >= LOW_TIER_ALERT_STREAK and not self._stt_degraded:
            self._stt_degraded = True
            log.warning("stt_sustained_low_confidence", streak=self._low_streak)
        if self._stt_degraded and not self._stt_conf_alarmed:
            self._stt_conf_alarmed = True
            await self._raise(
                AlarmCode.STT_DEGRADED,
                AlarmSeverity.WARNING,
                f"{max(self._low_streak, LOW_TIER_ALERT_STREAK)} consecutive "
                "low-confidence resolutions — something is wrong with the audio "
                "path (mic mix, model, or capture). Voice commands are being "
                "rejected; slash commands are unaffected.",
                "listen to comms quality and check the journal's confidence "
                "scores; clears itself on the next confident resolution",
                key="confidence",
            )
        elif not self._stt_degraded and self._stt_conf_alarmed:
            self._stt_conf_alarmed = False
            await self._clear(AlarmCode.STT_DEGRADED, key="confidence")

    async def _maybe_report(self, now: float) -> None:
        interval_s = self._holder.current.health.report_interval_min * 60
        if (now - self._last_report_at) < interval_s:
            return
        self._last_report_at = now
        embed = self.build_report_embed(datetime.now(UTC))
        await self._post("", embed)
        self._reset_window_counters()

    # ── report rendering ─────────────────────────────────────────────────────

    def build_report_embed(self, now: datetime) -> dict[str, Any]:
        """The hourly report as a ``discord.Embed.from_dict`` payload."""
        avg_conf = (
            round(sum(self._confidences) / len(self._confidences), 3) if self._confidences else None
        )
        # The report's status word also covers the audio-pipeline latches; the
        # `degraded` property itself (what the engine keys spoken fallbacks
        # off) is unchanged — the latches speak through their own alerts.
        report_degraded = self.degraded or self.stt_watchdog_degraded or self.wake_faulted
        status = "degraded" if report_degraded else "nominal"
        parts = []
        if self._voice_offline:
            parts.append("voice receive offline")
        if self._ears_down:
            parts.append("Ears heartbeat missed")
        if self._stt_degraded:
            parts.append("sustained low STT confidence")
        if self.stt_watchdog_degraded:
            parts.append("STT watchdog latched")
        if self.wake_faulted:
            parts.append("wake model faulted")
        wake = self.wake_counters
        wake_value = (
            "n/a"
            if not wake
            else (
                f"frames {wake.get('frames_seen', 0)} · "
                f"scored {wake.get('vad_speech', 0)} · "
                f"inferences {wake.get('inferences', 0)} · "
                f"hits {wake.get('hits', 0)} · "
                f"near {wake.get('near_misses', 0)}"
            )
        )
        fields = [
            {
                "name": "Status",
                "value": status + (f" — {', '.join(parts)}" if parts else ""),
                "inline": False,
            },
            {"name": "Incidents posted", "value": str(self._incidents_posted), "inline": True},
            {"name": "Folded", "value": str(self._incidents_folded), "inline": True},
            {"name": "Mentions", "value": str(self._mentions_sent), "inline": True},
            {"name": "Wake hits", "value": str(self._wake_hits), "inline": True},
            {"name": "Rejected", "value": str(self._commands_rejected), "inline": True},
            {"name": "Post failures", "value": str(self._post_failures), "inline": True},
            {
                "name": f"STT confidence (last {len(self._confidences)})",
                "value": "n/a" if avg_conf is None else str(avg_conf),
                "inline": True,
            },
            {
                "name": "STT watchdog",
                "value": "latched (degraded)" if self.stt_watchdog_degraded else "ok",
                "inline": True,
            },
            {
                "name": "Wake pipeline",
                "value": ("FAULTED · " if self.wake_faulted else "") + wake_value,
                "inline": False,
            },
            {
                "name": "Ears",
                "value": "connected"
                if self._ears_reports_connected and not self._ears_down
                else "not connected",
                "inline": True,
            },
            {"name": "Pilots in voice", "value": str(self._humans_present), "inline": True},
        ]
        return {
            "title": "CORTANA health report",
            "color": 0xE74C3C if report_degraded else 0x2ECC71,
            "timestamp": now.isoformat(),
            "fields": fields,
        }

    def _reset_window_counters(self) -> None:
        self._incidents_posted = 0
        self._incidents_folded = 0
        self._mentions_sent = 0
        self._wake_hits = 0
        self._commands_rejected = 0
        self._post_failures = 0
