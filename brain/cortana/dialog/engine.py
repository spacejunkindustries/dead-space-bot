"""DialogEngine — executes the pure machine against the real world. GDD §5.4.

The engine owns:

- one :class:`~cortana.dialog.types.DialogSession` per speaking user,
- the single 100 ms timeout wheel (``run()``) enforcing, on ONE monotonic
  clock: DTX-tolerant utterance endpointing, armed-window expiry, and the
  AWAIT-state TTLs — the sole successor to the old silence sweep AND the old
  frame-counted window,
- the executors that turn machine :class:`Action`s into STT calls, incident
  reports, relays, override questions, and spoken lines.

Dependencies arrive as narrow callables/objects so the engine is testable
with fakes; it never imports discord. The IncidentEngine it drives is the
same one the slash cogs call — constraint 10 (voice/slash twins share the
engine) is preserved by construction.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import structlog

from cortana import tts as tts_mod
from cortana.audio.capture import CaptureManager, CaptureMeta, CaptureOrigin
from cortana.audio.stt import SttError, SttTimeoutError
from cortana.chat import ChatClient, ChatCooldownError
from cortana.dialog.machine import transition
from cortana.dialog.types import (
    Action,
    ArmWindow,
    Classified,
    ConfirmPending,
    DialogEvent,
    DialogSession,
    DialogState,
    DisarmWindow,
    Ev,
    Line,
    NoteRejected,
    PendingConfirm,
    Relay,
    Report,
    RunOverride,
    RunStt,
    Speak,
)
from cortana.ipc import PRIORITY_ALERT, PRIORITY_NORMAL
from cortana.nlu import grammar, phonetics
from cortana.types import (
    INTENT_SEVERITY,
    MENTION_INTENTS,
    Intent,
    Outcome,
    Resolution,
    Severity,
    Tier,
)

if TYPE_CHECKING:
    import sqlite3

    from cortana.config import ConfigHolder
    from cortana.core.discipline import Discipline
    from cortana.core.fun import FunEngine
    from cortana.core.incidents import IncidentEngine
    from cortana.health import HealthReporter
    from cortana.nlu.gazetteer import Gazetteer
    from cortana.tts import Speaker

log = structlog.get_logger(__name__)

__all__ = ["DialogEngine"]

_WHEEL_TICK_S = 0.1

#: Spoken/posted when a non-@Pilot member voice-triggers a mention-bearing
#: intent — mirrors the slash twin's rejection (GDD §11.1 layer 4).
_PILOT_REQUIRED_UTTERANCE = "Reporting requires the Pilot role."

SendChannel = Callable[..., Awaitable[None]]


class DialogEngine:
    """One instance serves all guilds/users. See module docstring."""

    def __init__(
        self,
        holder: ConfigHolder,
        *,
        capture: CaptureManager | None,
        transcriber: Any,
        speaker: Speaker,
        incidents: IncidentEngine,
        discipline: Discipline,
        gazetteer: Gazetteer,
        conn: sqlite3.Connection,
        health: HealthReporter,
        chat_provider: Callable[[], tuple[ChatClient | None, str]],
        member_role_ids: Callable[[int], list[int]],
        send_channel: SendChannel,
        shutdown: asyncio.Event,
        fun: FunEngine | None = None,
    ) -> None:
        self._holder = holder
        self._capture = capture
        self._transcriber = transcriber
        self._speaker = speaker
        self._incidents = incidents
        self._discipline = discipline
        self._gazetteer = gazetteer
        self._conn = conn
        self._health = health
        self._chat_provider = chat_provider
        self._member_role_ids = member_role_ids
        self._send_channel = send_channel
        self._shutdown = shutdown
        self._fun = fun

        self._sessions: dict[int, DialogSession] = {}
        self._feed_errors = 0
        #: Wall-clock deadline (loop.time) per user for the armed-window /
        #: AWAIT TTL; validated against session.gen before firing.
        self._deadlines: dict[int, tuple[float, int]] = {}
        #: Endpoint grace per user — set at capture open and after window
        #: prompts, so cue playback + reaction time can't endpoint a capture
        #: before the first word.
        self._grace_until: dict[int, float] = {}
        #: Last audio frame per user (loop.time) — DTX endpointing authority.
        self._last_audio_at: dict[int, float] = {}
        # Fire-and-forget spoken cues; referenced so they aren't GC'd mid-flight.
        self._voice_tasks: set[asyncio.Task[None]] = set()

    # ── session plumbing ─────────────────────────────────────────────────────

    def _session(self, user_id: int, guild_id: int) -> DialogSession:
        s = self._sessions.get(user_id)
        if s is None or s.guild_id != guild_id:
            s = DialogSession(user_id=user_id, guild_id=guild_id)
            self._sessions[user_id] = s
        return s

    def session_state(self, user_id: int) -> DialogState:
        s = self._sessions.get(user_id)
        return s.state if s is not None else DialogState.IDLE

    @property
    def sessions_active(self) -> int:
        """Dialog sessions currently in flight (non-IDLE) — for /botstatus."""
        return sum(1 for s in self._sessions.values() if s.state is not DialogState.IDLE)

    def reset_user(self, user_id: int) -> None:
        """Purge one user's dialog + capture state. Safe from any caller —
        IPC 'left', discord voice_state, or tests — and idempotent."""
        self._sessions.pop(user_id, None)
        self._deadlines.pop(user_id, None)
        self._grace_until.pop(user_id, None)
        self._last_audio_at.pop(user_id, None)
        if self._capture is not None:
            self._capture.drop_user(user_id)

    def reset_all(self) -> None:
        """Ears reconnected: every armed window/SSRC mapping it knew is gone."""
        for user_id in list(self._sessions):
            self.reset_user(user_id)

    def tracked_users(self) -> set[int]:
        """Every user the engine holds ANY state for — sessions or timing.

        Snapshot reconciliation (GDD §15) purges members of this set that
        Ears no longer sees."""
        return set(self._sessions) | set(self._last_audio_at)

    # ── hot path ─────────────────────────────────────────────────────────────

    def on_audio(self, user_id: int, guild_id: int, pcm: bytes) -> None:
        """IPC audio hot path — sync, never blocks (constraint: thin + RAM only)."""
        self._health.note_audio()
        self._last_audio_at[user_id] = self._now()
        if self._capture is not None:
            # An exception here would kill the IPC read loop — one bad frame
            # (or a wake-model failure for one user) must never take down the
            # whole audio path. Log sparsely: this fires every 20 ms.
            try:
                self._capture.feed(user_id, guild_id, pcm)
            except Exception:
                self._feed_errors += 1
                if self._feed_errors == 1 or self._feed_errors % 500 == 0:
                    log.exception("audio_feed_failed", user_id=user_id, count=self._feed_errors)

    def on_capture_start(
        self, user_id: int, guild_id: int, origin: CaptureOrigin, armed_gen: int | None
    ) -> int:
        """Sync callback from CaptureManager the instant a capture opens.

        Transitions the session (WAKE_HIT / WINDOW_OPENED) and returns the
        generation token the capture is stamped with. Must not block — spoken
        acks are scheduled, never awaited."""
        s = self._session(user_id, guild_id)
        if origin is CaptureOrigin.WAKE:
            self._health.record_wake_hit()
            ev = DialogEvent(Ev.WAKE_HIT)
        else:
            ev = DialogEvent(Ev.WINDOW_OPENED, gen=armed_gen)
        self._grace_until[user_id] = self._now() + self._dcfg().ack_grace_ms / 1000
        self._deadlines.pop(user_id, None)  # the window did its job
        return self._apply_sync(s, ev)

    # ── utterance path ───────────────────────────────────────────────────────

    async def on_utterance(
        self, user_id: int, guild_id: int, pcm: bytes, meta: CaptureMeta
    ) -> None:
        """One emitted capture: machine-routed STT → grammar → engine."""
        s = self._session(user_id, guild_id)
        if meta.gen != s.gen:
            log.info("utterance_stale_gen", user_id=user_id, gen=meta.gen, session_gen=s.gen)
            return
        if meta.speech_frames == 0 or not pcm:
            await self._apply(s, DialogEvent(Ev.CAPTURE_ABANDONED, gen=meta.gen))
            return
        # Fleetmode gate (GDD §11.1): voice triggers may be FC-only.
        if not self._discipline.may_voice_trigger(self._member_role_ids(user_id)):
            log.info("voice_trigger_denied", user_id=user_id)
            await self._apply(s, DialogEvent(Ev.RESET))
            return
        await self._apply(s, DialogEvent(Ev.CAPTURE_EMITTED, gen=meta.gen), pcm=pcm)

    async def _run_stt(self, s: DialogSession, gen: int, pcm: bytes) -> None:
        cfg = self._holder.current
        bias = self._gazetteer.prompt_bias_text() if cfg.stt.bias_with_gazetteer else ""
        try:
            result = await asyncio.to_thread(self._transcriber.transcribe, pcm, bias)
        except Exception as exc:
            # ANY backend failure — SttError, but also raw ctranslate2 /
            # model-load errors the watchdog re-raises verbatim — must flow
            # through the machine's fail() door: swallowing it here left the
            # session stuck THINKING with the pilot hearing dead air.
            del pcm  # constraint 5: audio dropped even on failure
            self._health.record_rejected()
            log.warning(
                "stt_failed",
                user_id=s.user_id,
                timed_out=isinstance(exc, SttTimeoutError),
                stt_error=isinstance(exc, SttError),
                error=str(exc),
            )
            await self._apply(
                self._session(s.user_id, s.guild_id), DialogEvent(Ev.STT_FAILED, gen=gen)
            )
            return
        del pcm  # transcript only from here on (constraint 5)
        log.info(
            "utterance_transcribed",
            user_id=s.user_id,
            text=result.text,
            avg_logprob=round(result.avg_logprob, 3),
        )
        classified = self._classify(result.text, result.avg_logprob)
        await self._apply(
            self._session(s.user_id, s.guild_id),
            DialogEvent(Ev.CLASSIFIED, gen=gen, classified=classified),
        )

    def _classify(self, text: str, avg_logprob: float) -> Classified:
        """All grammar/config lookups for one transcript, in one place —
        the machine receives facts, never functions."""
        cfg = self._holder.current
        chat, _status = self._chat_provider()
        return Classified(
            text=text,
            confident=avg_logprob >= cfg.stt.relay_min_logprob,
            override_query=grammar.override_query(text),
            bare_override=grammar.bare_override(text),
            bare_code=grammar.bare_code(text),
            parsed=grammar.parse(text),
            system_reply=grammar.system_reply(text),
            framed=grammar.relay_framed(text),
            relay_text=grammar.broadcast_text(text),
            relay_mode=cfg.stt.relay_mode,
            chat_available=chat is not None and cfg.chat.enabled,
            confirm_reply=grammar.confirm_reply(text),
        )

    # ── machine application ──────────────────────────────────────────────────

    def _apply_sync(self, s: DialogSession, ev: DialogEvent) -> int:
        """Hot-path variant: commit the transition, schedule the actions."""
        res = transition(s, ev, self._dcfg().max_retries)
        self._sessions[s.user_id] = res.session
        if res.actions:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # pure-sync tests: run inline
                asyncio.run(self._execute(res.session, res.actions))
                return res.session.gen
            task = loop.create_task(self._execute(res.session, res.actions))
            self._voice_tasks.add(task)
            task.add_done_callback(self._voice_tasks.discard)
        return res.session.gen

    async def _apply(self, s: DialogSession, ev: DialogEvent, *, pcm: bytes = b"") -> None:
        res = transition(s, ev, self._dcfg().max_retries)
        self._sessions[s.user_id] = res.session
        await self._execute(res.session, res.actions, pcm=pcm)

    async def _execute(
        self, s: DialogSession, actions: tuple[Action, ...], *, pcm: bytes = b""
    ) -> None:
        for action in actions:
            try:
                await self._execute_one(s, action, pcm)
            except Exception:
                # One failed executor (Discord hiccup, Piper error) must not
                # abort the remaining actions or crash the utterance task.
                log.exception(
                    "dialog_action_failed", user_id=s.user_id, action=type(action).__name__
                )

    async def _execute_one(self, s: DialogSession, action: Action, pcm: bytes) -> None:
        if isinstance(action, Speak):
            await self._speak(s, action)
        elif isinstance(action, ArmWindow):
            self._arm_window(s, action.gen)
        elif isinstance(action, DisarmWindow):
            if self._capture is not None:
                self._capture.disarm(s.user_id)
            self._deadlines.pop(s.user_id, None)
        elif isinstance(action, RunStt):
            await self._run_stt(s, action.gen, pcm)
        elif isinstance(action, Report):
            await self._report(s, action)
        elif isinstance(action, Relay):
            await self._relay(s, action)
        elif isinstance(action, RunOverride):
            await self._override(s, action.query)
        elif isinstance(action, ConfirmPending):
            await self._confirm(s, action.confirm)
        elif isinstance(action, NoteRejected):
            self._health.record_rejected()
            log.info("utterance_no_intent", user_id=s.user_id, reason=action.reason)

    def _arm_window(self, s: DialogSession, gen: int) -> None:
        """Arm the wake-free window and start its WALL-CLOCK lifetime.

        Executed AFTER the spoken prompt resolves, which can be seconds under
        TTS-queue backlog — long enough for the pilot to have re-woken. A
        fresh wake supersedes the dialog (new gen), so an ArmWindow whose gen
        no longer matches the live session is stale and must be dropped, not
        executed: arming it would destroy the in-flight capture (confirmed
        live-class defect) or strand a window no deadline owns.
        """
        live = self._sessions.get(s.user_id)
        if live is None or live.gen != gen:
            log.info(
                "window_arm_dropped_stale",
                user_id=s.user_id,
                gen=gen,
                live_gen=live.gen if live is not None else None,
            )
            return
        dcfg = self._dcfg()
        if self._capture is not None:
            self._capture.arm_window(s.user_id, s.guild_id, gen)
        now = self._now()
        self._deadlines[s.user_id] = (now + dcfg.window_ms / 1000, gen)
        # The prompt spoken just before this counts into the pilot's reaction
        # time — extend the endpoint grace to cover it.
        self._grace_until[s.user_id] = now + dcfg.ack_grace_ms / 1000

    # ── the wheel ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """The single dialog clock: endpoints, window expiry, AWAIT TTLs."""
        while not self._shutdown.is_set():
            await asyncio.sleep(_WHEEL_TICK_S)
            try:
                await self._tick()
            except Exception:
                log.exception("dialog_wheel_tick_failed")

    async def _tick(self) -> None:
        now = self._now()
        cfg = self._holder.current
        dcfg = cfg.dialog
        # 1. Armed windows / AWAIT states that expired unused → IDLE, silent.
        for user_id, (deadline, gen) in list(self._deadlines.items()):
            if now < deadline:
                continue
            self._deadlines.pop(user_id, None)
            s = self._sessions.get(user_id)
            if s is not None:
                await self._apply(s, DialogEvent(Ev.DEADLINE, gen=gen))
        # 2. DTX-tolerant endpointing: a capturing pilot who has sent no
        #    packets for the gap (Discord stops the stream on silence) has
        #    their utterance emitted — unless still inside the ack grace.
        if self._capture is None:
            return
        gap = max(cfg.capture.endpoint_silence_ms, dcfg.endpoint_gap_floor_ms) / 1000
        for user_id in self._capture.capturing_users():
            if now < self._grace_until.get(user_id, 0.0):
                continue
            last = self._last_audio_at.get(user_id)
            if last is not None and now - last >= gap:
                self._capture.force_endpoint(user_id)

    # ── executors: the ported §5 pipeline tail ───────────────────────────────

    async def _report(self, s: DialogSession, action: Report) -> None:
        """Full command path: pilot gate → resolve → IncidentEngine → reply."""
        cfg = self._holder.current
        parsed = action.parsed
        # Fun commands (GDD §13.2) never reach the incident engine: the reply
        # is voice-ONLY by explicit design — no card, no channel post, no
        # command_log row. The slash twins answer in their invoking channel.
        if parsed.intent in (Intent.FACT, Intent.INSULT):
            await self._fun_reply(s, parsed)
            return
        if action.inherited is not None and parsed.severity is None:
            # A severity spoken in the opener ("code orange" → "go ahead" →
            # the report) attaches to the report; an inline code wins.
            parsed = dataclasses.replace(parsed, severity=action.inherited)

        # GDD §11.1 layer 4: only @Pilot may trigger mentions — reject the
        # command outright, exactly like the slash twin (constraint 10).
        # Lifted in silent mode: with pings off there is no mention to protect.
        if (
            cfg.discord.mentions_enabled
            and parsed.intent in MENTION_INTENTS
            and not self._discipline.may_mention(self._member_role_ids(s.user_id))
        ):
            self._health.record_rejected()
            log.info("voice_pilot_denied", user_id=s.user_id)
            await self._speak_or_post(s, _PILOT_REQUIRED_UTTERANCE)
            return

        # A confirmed §8.3 candidate arrives pre-resolved at HIGH tier — the
        # pilot vouched for exactly that system; re-resolving could demote it.
        resolution = action.forced_resolution
        if resolution is None and parsed.system_text:
            priors = await asyncio.to_thread(
                self._incidents.build_prior_context, s.guild_id, s.user_id
            )
            resolution = await asyncio.to_thread(
                phonetics.resolve,
                parsed.system_text,
                self._gazetteer,
                priors,
                cfg.matching,
                self._conn,
            )
            self._health.record_stt(
                resolution.best.score if resolution.best else 0.0, resolution.tier
            )

        # Defence in depth: the gate also rides inside decide_mentions(), so
        # no engine path can mint a mention for a caller the gate refuses.
        outcome = await self._incidents.report(
            s.guild_id,
            s.user_id,
            parsed,
            resolution,
            caller_may_mention=self._discipline.may_mention(self._member_role_ids(s.user_id)),
        )
        self._count_outcome(outcome.outcome)

        # Voice "help" (GDD §6.1): the spoken line stays under the §12.2 cap,
        # so the actual command list — the /help front page — is posted to the
        # intel channel alongside it.
        if parsed.intent is Intent.HELP and outcome.outcome is Outcome.POSTED:
            from cortana.dsc.cogs.help import main_embed  # text-side import, lazy

            await self._send_channel(cfg.discord.channels.intel_live, "", embed=main_embed())

        effective = (
            parsed.severity
            if parsed.severity is not None
            else INTENT_SEVERITY.get(parsed.intent, Severity.NONE)
        )
        # Speak the outcome FIRST, then arm the retry window: the window's
        # wall-clock lifetime must not start ticking while the rejection
        # line is still queued behind other synthesis (review finding — the
        # pilot could lose 1-2s of a 4s window before hearing the prompt).
        await self._reply(s, effective, outcome)

        # LOW tier (GDD §8.3): feed the rejection back — the machine decides
        # whether the retry budget still covers a wake-free rebind window.
        if (
            outcome.outcome is Outcome.REJECTED
            and (resolution is None or resolution.tier is Tier.LOW)
            and parsed.system_text
            and action.rebound_from is None  # a failed rebind doesn't re-arm
        ):
            await self._apply(
                self._session(s.user_id, s.guild_id),
                DialogEvent(Ev.ENGINE_REJECTED_LOW, parsed=parsed),
            )

        # MEDIUM tier (GDD §8.3): the engine answered "say again to confirm".
        # Arm a wake-free confirm window carrying the command + candidate so
        # "yes" — or an exact repeat — completes it by voice; the machine's
        # budget bounds how often this can recur.
        if (
            outcome.outcome is Outcome.ASKED
            and resolution is not None
            and resolution.best is not None
        ):
            await self._apply(
                self._session(s.user_id, s.guild_id),
                DialogEvent(
                    Ev.ENGINE_ASKED,
                    confirm=PendingConfirm(
                        parsed=parsed,
                        candidate=resolution.best,
                        incident_id=outcome.incident_id,
                    ),
                ),
            )

    async def _fun_reply(self, s: DialogSession, parsed: Any) -> None:
        """Speak one fact or insult (GDD §13.2). Voice in, voice out — a
        failed synthesis is logged and dropped, never posted to a channel."""
        from cortana.core.fun import FunCooldown, FunUnavailable

        cfg = self._holder.current
        if self._fun is None or not cfg.fun.enabled:
            await self._speak_or_post(s, tts_mod.fun_disabled())
            return
        try:
            if parsed.intent is Intent.FACT:
                line = self._fun.next_fact(s.guild_id, parsed.detail).text
            else:
                line = self._fun.next_insult(s.guild_id, parsed.detail)
        except FunCooldown:
            await self._speak_or_post(s, tts_mod.fun_cooldown())
            return
        except FunUnavailable:
            log.warning("fun_library_empty", intent=str(parsed.intent))
            await self._speak_or_post(s, tts_mod.fun_disabled())
            return
        log.info("fun_served", user_id=s.user_id, intent=str(parsed.intent), text=line)
        spoken = await self._speaker.say(
            s.guild_id,
            line,
            PRIORITY_NORMAL,
            user_id=s.user_id,
            max_s=cfg.fun.max_speak_s,
        )
        if not spoken:
            log.info("fun_line_unspoken_dropped", user_id=s.user_id)

    async def _confirm(self, s: DialogSession, confirm: PendingConfirm) -> None:
        """Complete a pending §8.3 confirm — the voice twin of the card's
        pick/confirm button (constraint 10: same engine path, both ways in).

        An uncertain report already posted its card: confirming pins the
        heard candidate through the same ``confirm_system`` path the pick
        button uses — alias learning (§8.5) included. A destructive or
        scheduling command (clear/timer/form up) posted nothing: the stored
        candidate re-reports as a HIGH-tier resolution, exactly as if the
        repeat had been heard cleanly.
        """
        if confirm.incident_id is not None:
            outcome = await self._incidents.confirm_system(
                confirm.incident_id, s.user_id, confirm.candidate.system_id
            )
            self._count_outcome(outcome.outcome)
            parsed = confirm.parsed
            effective = (
                parsed.severity
                if parsed.severity is not None
                else INTENT_SEVERITY.get(parsed.intent, Severity.NONE)
            )
            await self._reply(s, effective, outcome)
            return
        forced = Resolution(tier=Tier.HIGH, candidates=(confirm.candidate,))
        await self._report(s, Report(confirm.parsed, forced_resolution=forced))

    async def _relay(self, s: DialogSession, action: Relay) -> None:
        """Freeform intel relay (GDD §8.6) through the broadcast path.

        The pilot gate rides inside decide_mentions() — for non-Pilots the
        relay still posts, mention-free, exactly like the slash twin."""
        text = action.text
        severity = action.severity or grammar.broadcast_severity(text)
        outcome = await self._incidents.broadcast(
            s.guild_id,
            s.user_id,
            text,
            group_alias="all_hands" if grammar.wants_all_hands(text) else None,
            severity=severity,
            confidence=0.0,
            caller_may_mention=self._discipline.may_mention(self._member_role_ids(s.user_id)),
        )
        self._count_outcome(outcome.outcome)
        # "Relayed." — without an audible ack pilots repeat themselves, and
        # every repeat is another card and another STT decode.
        await self._reply(s, severity or Severity.NONE, outcome)

    async def _override(self, s: DialogSession, query: str) -> None:
        """One §6.6 override question: throttle → ask → speak or post.

        Failures never surface raw errors on comms — a fixed line only."""
        chat, status = self._chat_provider()
        cfg = self._holder.current
        if chat is None or not cfg.chat.enabled:
            log.info("override_requested_unavailable", user_id=s.user_id, status=status)
            await self._speak_or_post(s, tts_mod.override_unavailable())
            return
        log.info("override_query", user_id=s.user_id, query=query)
        try:
            reply = await asyncio.wait_for(chat.ask(s.user_id, query), timeout=cfg.chat.timeout_s)
        except ChatCooldownError:
            await self._speak_or_post(s, tts_mod.override_cooldown())
            return
        except Exception as exc:  # ChatError, TimeoutError — comms stay clean
            log.warning("override_failed", user_id=s.user_id, error=str(exc))
            await self._speak_or_post(s, tts_mod.override_unavailable())
            return
        spoken = await self._speaker.say(s.guild_id, reply, PRIORITY_NORMAL, user_id=s.user_id)
        if not spoken:
            # Long answers post to chat.answer_channel when set — chit-chat
            # in the intel channel annoys fast (live complaint).
            target = cfg.chat.answer_channel or cfg.discord.channels.intel_live
            await self._send_channel(target, f"💬 **Override** · {reply}")
            await self._speak_or_post(s, tts_mod.override_posted())

    # ── spoken output ────────────────────────────────────────────────────────

    async def _speak(self, s: DialogSession, action: Speak) -> None:
        line = action.line
        if line is Line.ACK:
            # Wake acknowledgement: instant chirp by default, spoken line or
            # silence per wake.ack. Never awaited from the hot path caller.
            ack = self._holder.current.wake.ack
            if ack == "none" or self._speaker is None:
                return
            if ack == "voice":
                await self._speaker.say(
                    s.guild_id, tts_mod.go_ahead(), PRIORITY_ALERT, user_id=s.user_id
                )
            else:
                await self._speaker.chirp(s.guild_id, user_id=s.user_id)
            return
        text = {
            Line.GO_AHEAD: tts_mod.go_ahead,
            Line.SAY_AGAIN: tts_mod.say_again,
            Line.NOT_UNDERSTOOD: tts_mod.not_understood,
            Line.STANDING_DOWN: tts_mod.standing_down,
            Line.OVERRIDE_UNAVAILABLE: tts_mod.override_unavailable,
        }
        if line is Line.CODE_ACK:
            utterance = tts_mod.code_ack(action.severity or Severity.NONE)
        else:
            utterance = text[line]()
        await self._speak_or_post(s, utterance)

    async def _reply(self, s: DialogSession, severity: Severity, outcome: Any) -> None:
        """Speak the outcome utterance; fall back to channel text when speech
        is disabled, muted, or over the §12.2 length cap."""
        utterance = outcome.utterance
        if not utterance:
            return
        if self._health.degraded:
            utterance = f"{utterance} {tts_mod.degraded()}"
        priority = PRIORITY_ALERT if severity is Severity.HIGH else PRIORITY_NORMAL
        spoken = await self._speaker.say(s.guild_id, utterance, priority, user_id=s.user_id)
        if not spoken:
            # A long verbatim system name can push the confirmation over the
            # §12.2 cap — the pilot still needs to HEAR that the report
            # landed (a silent post read as a swallowed report, live
            # complaint). Speak the minimal form; the card is the record.
            short = None
            if outcome.outcome is Outcome.POSTED:
                short = tts_mod.posted_short()
            elif outcome.outcome is Outcome.FOLDED:
                short = tts_mod.updated_short()
            if short is not None:
                spoken = await self._speaker.say(s.guild_id, short, priority, user_id=s.user_id)
        if not spoken:
            await self._send_channel(
                self._holder.current.discord.channels.intel_live, f"🔊 {utterance}"
            )

    async def _speak_or_post(self, s: DialogSession, utterance: str) -> None:
        """Speak a short acknowledgement/rejection, best-effort.

        ACK-class lines ("Say again?", "Go ahead.", "Standing down…") are
        ephemeral feedback for the pilot's ears ONLY — when speech fails
        (muted, over the §12.2 cap, synth error) they are logged and DROPPED,
        never posted: a retry prompt pasted into the intel channel is pure
        noise (live complaint). Command OUTCOMES still fall back to channel
        text via ``_reply`` — those carry real information.
        """
        spoken = await self._speaker.say(s.guild_id, utterance, PRIORITY_NORMAL, user_id=s.user_id)
        if not spoken:
            log.info("ack_unspoken_dropped", user_id=s.user_id, utterance=utterance)

    # ── small helpers ────────────────────────────────────────────────────────

    def _count_outcome(self, outcome: Outcome) -> None:
        if outcome is Outcome.POSTED:
            self._health.record_incident_posted()
        elif outcome is Outcome.FOLDED:
            self._health.record_incident_folded()
        elif outcome is Outcome.REJECTED:
            self._health.record_rejected()

    def _dcfg(self) -> Any:
        return self._holder.current.dialog

    @staticmethod
    def _now() -> float:
        try:
            return asyncio.get_running_loop().time()
        except RuntimeError:  # pragma: no cover — only outside the loop (tests)
            return 0.0
