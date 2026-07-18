"""CORTANA Brain entrypoint: composition root, task supervision, shutdown.

``python -m cortana --config /etc/cortana/cortana.yaml``

Everything is built here, in the order INTERFACES.md prescribes:
ConfigHolder → db → Gazetteer → Discipline → IpcServer → Speaker →
Transcriber → CaptureManager → DialogEngine → IncidentEngine → AuraBot, then
the voice gateway and health reporter are wired around them. One asyncio
event loop, owned by this module.

Voice pipeline wiring (GDD §5): IPC audio frames → ``DialogEngine.on_audio``
→ ``CaptureManager.feed`` (sync hot path) → wake/VAD → the dialog state
machine (GDD §5.4) → STT → grammar → ``IncidentEngine`` → card posted by the
bot + spoken confirmation via the Speaker. All dialog state and timing lives
in :mod:`cortana.dialog` — this module only builds and connects.

Signals: SIGHUP reloads config in place (a failed reload keeps the old
config); SIGTERM/SIGINT run a graceful shutdown. Any crashed critical task
logs and triggers shutdown — systemd ``Restart=always`` brings us back.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sqlite3
import sys
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from cortana import tts as tts_mod
from cortana.alarms import AlarmBus, AlarmCode, AlarmSeverity
from cortana.audio.capture import CaptureManager, CaptureMeta, CaptureOrigin
from cortana.audio.stt import Transcriber, make_transcriber
from cortana.audio.vad import VadGate
from cortana.audio.wake import OpenWakeWordDetector
from cortana.chat import ChatClient, read_api_key
from cortana.config import ConfigError, ConfigHolder
from cortana.core import db
from cortana.core.discipline import Discipline
from cortana.core.incidents import IncidentEngine, Poster, TimerPing
from cortana.dialog import DialogEngine
from cortana.dsc.bot import AuraBot, read_token
from cortana.dsc.cogs.utility import ReminderService
from cortana.health import HealthReporter
from cortana.ipc import PRIORITY_NORMAL, IpcServer
from cortana.nlu.gazetteer import Gazetteer
from cortana.reload import ReloadResult
from cortana.tts import Speaker
from cortana.types import INTENT_SEVERITY, CardRender, Severity
from cortana.voice_gateway import VoiceGateway

log = structlog.get_logger(__name__)

_SWEEP_INTERVAL_S = 60.0
_TIMER_POLL_INTERVAL_S = 15.0
_HEALTH_CHECK_INTERVAL_S = 5.0

#: Upper bound on the startup STT model warm: enough for a cold model load on
#: the droplet's disk, small enough that a stalled network download of
#: ``stt.model`` cannot park setup() indefinitely behind a green unit status.
_STT_WARM_TIMEOUT_S = 120.0

#: Upper bound on graceful shutdown. Kept well under systemd's TimeoutStopSec
#: so a hung close never escalates to SIGKILL (which slows restarts and forces
#: Ears to re-negotiate DAVE on every rejoin).
_SHUTDOWN_TIMEOUT_S = 8.0


def configure_logging(level: int = logging.INFO) -> None:
    """structlog-style JSON lines on stdout (journald picks them up)."""
    logging.basicConfig(level=level, stream=sys.stdout, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )


class _LatePoster:
    """Poster proxy breaking the engine↔bot construction cycle.

    The engine needs a Poster at construction; the bot needs the engine.
    The engine gets this proxy, and :meth:`bind` attaches the real bot the
    moment it exists — before any task that could post is started.
    """

    def __init__(self) -> None:
        self._poster: Poster | None = None

    def bind(self, poster: Poster) -> None:
        self._poster = poster

    def _target(self) -> Poster:
        if self._poster is None:  # pragma: no cover — construction-order bug guard
            raise RuntimeError("Poster used before bind()")
        return self._poster

    async def post(
        self, guild_id: int, channel: Any, content: str, card: CardRender
    ) -> tuple[int, int]:
        return await self._target().post(guild_id, channel, content, card)

    async def edit(self, channel_id: int, message_id: int, content: str, card: CardRender) -> None:
        await self._target().edit(channel_id, message_id, content, card)


class App:
    """The composed application. Build with :func:`build_app`, then ``run()``."""

    def __init__(self, holder: ConfigHolder) -> None:
        self.holder = holder
        # Populated by setup(); typed for the wiring methods below.
        self.conn: sqlite3.Connection | None = None
        self.gazetteer: Gazetteer | None = None
        self.discipline: Discipline | None = None
        self.ipc: IpcServer | None = None
        self.speaker: Speaker | None = None
        self.transcriber: Transcriber | None = None
        self.capture: CaptureManager | None = None
        self.dialog: DialogEngine | None = None
        self.engine: IncidentEngine | None = None
        self.bot: AuraBot | None = None
        self.health: HealthReporter | None = None
        self.alarms: AlarmBus | None = None
        self.gateway: VoiceGateway | None = None
        self.reminders: ReminderService | None = None
        self.chat: ChatClient | None = None
        self._chat_status = "disabled"
        self._chat_key: str | None = None
        self._reload_task: asyncio.Task[None] | None = None
        self._shutdown = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    # ── construction ─────────────────────────────────────────────────────────

    async def setup(self) -> None:
        """Build every component in dependency order. Blocking work rides
        ``asyncio.to_thread``; nothing here touches the network yet."""
        cfg = self.holder.current

        self.conn = await asyncio.to_thread(db.connect, cfg.database.path)
        applied = await asyncio.to_thread(db.migrate, self.conn)
        log.info("db_ready", path=cfg.database.path, migrations_applied=applied)

        # The gazetteer takes the HOLDER (not a config snapshot) so reloads
        # see gazetteer.* edits — the holder-snapshot fix.
        self.gazetteer = Gazetteer(self.conn, self.holder)
        await asyncio.to_thread(self.gazetteer.load)
        log.info("gazetteer_loaded", systems=len(self.gazetteer.systems))

        # The operator alarm surface (GDD §11.3). Safe from here on even
        # though Discord isn't up yet: cards queue dirty and flush from the
        # health loop once the bot is ready.
        self.alarms = AlarmBus(self.conn, send=self._alarm_send, edit=self._alarm_edit)
        # This process IS the restart that applies any pending restart-class
        # edits — resolve the card the previous lifetime raised.
        await self.alarms.clear(AlarmCode.CONFIG_RESTART_PENDING)

        self.discipline = Discipline(self.holder)
        self.ipc = IpcServer(self.holder, self._on_audio, self._on_control)
        tts_mod.set_personality(cfg.tts.personality)
        self.speaker = Speaker(self.holder, self.ipc)
        await self.speaker.warm()  # prime Piper's model into cache off the hot path

        self._refresh_chat()

        self.transcriber = await asyncio.to_thread(make_transcriber, cfg.stt)
        # Load the Whisper weights now, off the request path: the first real
        # utterance must not pay the model load inside the STT watchdog (that
        # loop never produced a transcript on a 2-vCPU box). BOUNDED: stt.model
        # can be a Hugging Face name that downloads on first load — a stalled
        # download must not wedge setup forever (systemd would see a live
        # process and never restart it). On timeout, log and continue: the
        # lazy load plus the watchdog cover the cold path.
        warm = getattr(self.transcriber, "warm", None)
        if callable(warm):
            try:
                await asyncio.wait_for(asyncio.to_thread(warm), timeout=_STT_WARM_TIMEOUT_S)
            except TimeoutError:
                log.warning("stt_warm_timed_out", timeout_s=_STT_WARM_TIMEOUT_S)
            else:
                log.info("stt_model_warmed", model=cfg.stt.model)
        vad = VadGate(cfg.capture.vad_aggressiveness)
        # The detector reads holder.current at the point of use (config.py
        # contract) so SIGHUP retunes apply to it and CaptureManager alike.
        wake = await asyncio.to_thread(OpenWakeWordDetector, self.holder)
        # CaptureManager ↔ DialogEngine are mutually referential: the manager
        # reports into the engine, the engine arms windows on the manager.
        # The lambdas defer attribute lookup until the first frame flows.
        self.capture = CaptureManager(
            self.holder,
            vad,
            wake,
            self._dialog_on_utterance,
            self._dialog_on_capture_start,
        )

        # Health before the engine: the engine reports mentions into it.
        self.health = HealthReporter(self.holder, self._post_health)
        self.health.set_alarm_bus(self.alarms)
        # Audio-pipeline probes: the wake stage counters/fault latch and the
        # STT watchdog latch become #bot-health alerts + report lines instead
        # of silent deaths behind a green status.
        self.health.set_wake_probe(wake.counters, lambda: wake.faulted)
        transcriber = self.transcriber
        self.health.set_stt_probe(lambda: bool(getattr(transcriber, "degraded", False)))

        late_poster = _LatePoster()
        rules_path = self.holder.current.routing.resolve(self.holder.path)
        self.engine = IncidentEngine(
            self.conn,
            self.holder,
            self.gazetteer,
            self.discipline,
            late_poster,
            rules_path,
            on_mention=self.health.record_mention,
        )
        self.bot = AuraBot(
            self.holder, self.engine, self.gazetteer, self.discipline, self.speaker, self.conn
        )
        late_poster.bind(self.bot)
        self.bot.chat = self.chat  # /ask slash twin (GDD §6.6, constraint 10)
        self.bot.chat_status = self._chat_status
        self.bot.alarms = self.alarms
        # /reload is the slash twin of SIGHUP — the SAME transaction.
        self.bot.request_reload = self._reload_transaction
        self.bot.ipc_status = self._ipc_status
        self.bot.dialog_sessions = lambda: (
            self.dialog.sessions_active if self.dialog is not None else 0
        )
        self.reminders = ReminderService(self.conn, self.bot)

        # The voice dialog engine (GDD §5.4) — drives the same IncidentEngine
        # the slash cogs call (constraint 10).
        self.dialog = DialogEngine(
            self.holder,
            capture=self.capture,
            transcriber=self.transcriber,
            speaker=self.speaker,
            incidents=self.engine,
            discipline=self.discipline,
            gazetteer=self.gazetteer,
            conn=self.conn,
            health=self.health,
            chat_provider=lambda: (self.chat, self._chat_status),
            member_role_ids=self._member_role_ids,
            send_channel=self._send_channel,
            shutdown=self._shutdown,
        )
        # Authoritative dialog cleanup, redundant with the IPC "left" event —
        # survives Ears outages (GDD §5.4).
        self.bot.on_user_left_voice = self.dialog.reset_user

        self.gateway = VoiceGateway(self.holder, self.ipc, self.conn, self.bot.announce_join)
        self.gateway.set_census_listener(self.health.set_humans_present)
        # The bot forwards its voice census (on_voice_state_update + on_ready
        # seed) to the gateway, and the cogs reach both through the bot.
        self.bot.voice_gateway = self.gateway
        self.bot.health_reporter = self.health

        # Load the muted-voice set so /mute-voice survives restarts.
        rows = await asyncio.to_thread(db.query, self.conn, "SELECT user_id FROM voice_mutes", ())
        self.speaker.set_voice_mutes({row["user_id"] for row in rows})

        # Prime the callsign mirror so cards and /rollcall can name reporters.
        await self.engine.callsigns.load()
        # Prime the personal-ping mirror so routing sees subscriptions from
        # the first incident after a restart (GDD §10.3).
        await self.engine.personal_pings.load()

    # ── run / shutdown ───────────────────────────────────────────────────────

    def run(self) -> None:
        """Blocking entrypoint: owns the event loop from start to finish."""
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        await self.setup()
        assert self.ipc and self.bot and self.engine and self.health and self.dialog

        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGHUP, self._on_sighup)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._request_shutdown, sig.name)

        token = read_token(self.holder.current.discord)
        await self.ipc.start()

        self._spawn("discord-bot", self.bot.start(token))
        self._spawn("stale-sweep", self._sweep_loop())
        self._spawn("timer-poll", self._timer_loop())
        self._spawn("reminder-poll", self._reminder_loop())
        self._spawn("health-check", self._health_loop())
        self._spawn("dialog-wheel", self.dialog.run())

        # Pre-render the scripted acknowledgement lines once the app is fully
        # up (never during setup — see Speaker.start_priming).
        assert self.speaker is not None
        self.speaker.start_priming()

        log.info("aura_started")
        await self._shutdown.wait()
        await self._graceful_shutdown()

    def _spawn(self, name: str, coro: Coroutine[Any, Any, Any]) -> None:
        """Run a critical task; if it crashes or exits, shut the process down."""

        async def _supervised() -> None:
            try:
                await coro
                # A clean return during shutdown is expected (loops watch the
                # shutdown event and fall out); only an exit while the process
                # is meant to be running is a fault worth alarming on.
                if not self._shutdown.is_set():
                    log.error("critical_task_exited", task=name)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("critical_task_crashed", task=name)
            self._shutdown.set()

        self._tasks.append(asyncio.create_task(_supervised(), name=name))

    def _request_shutdown(self, reason: str) -> None:
        log.info("shutdown_requested", reason=reason)
        self._shutdown.set()

    def _on_sighup(self) -> None:
        # The transaction is async (engine reloads, health post); the signal
        # handler only schedules it. One at a time — a second SIGHUP during a
        # reload is dropped rather than interleaved.
        if self._reload_task is not None and not self._reload_task.done():
            log.warning("config_reload_already_running")
            return
        self._reload_task = asyncio.create_task(self._reload())

    async def _reload(self) -> None:
        await self._reload_transaction()

    async def _reload_transaction(self) -> ReloadResult:
        """The one reload transaction (GDD §16): validate everything, swap
        all-or-nothing, apply by reload class, post the receipt, and drive
        the CONFIG_RESTART_PENDING alarm. Backs BOTH doors — SIGHUP and the
        ``/reload`` slash command (which needs the returned receipt)."""
        from cortana.nlu.gazetteer import _load_scope
        from cortana.reload import reload_all

        def _validate_gazetteer(cfg: Any) -> None:
            _load_scope(Path(cfg.gazetteer.file))

        async def _reload_gazetteer(cfg: Any) -> None:
            if self.gazetteer is not None:
                await asyncio.to_thread(self.gazetteer.load)

        async def _reload_routing(cfg: Any) -> None:
            if self.bot is not None and self.bot.is_ready():
                await self.bot._load_routing_rules()

        result = await reload_all(
            self.holder,
            file_validators={"gazetteer.yaml": _validate_gazetteer},
            engine_reloaders={
                "gazetteer": _reload_gazetteer,
                "routing": _reload_routing,
            },
            appliers={
                "tts.personality": lambda cfg: tts_mod.set_personality(cfg.tts.personality),
            },
        )
        # Unconditional: catches a rotated on-disk API key even when no
        # chat.* KEY changed (appliers only fire on key changes).
        self._refresh_chat()
        # A reload is the operator's sanctioned "try again" for the STT
        # watchdog latch (stt.py: latched until /reload or restart).
        reset_degraded = getattr(self.transcriber, "reset_degraded", None)
        if callable(reset_degraded):
            reset_degraded()
        # Restart-bound edits are never silently absorbed: raise the alarm
        # while any are pending, resolve its card when a reload shows none.
        if self.alarms is not None:
            if result.restart_pending:
                await self.alarms.raise_alarm(
                    AlarmCode.CONFIG_RESTART_PENDING,
                    AlarmSeverity.WARNING,
                    "Restart-bound config keys were edited and are NOT live "
                    "yet: " + ", ".join(result.restart_pending),
                    "`systemctl restart cortana-brain` to apply them",
                )
            elif result.swapped:
                await self.alarms.clear(AlarmCode.CONFIG_RESTART_PENDING)
        log.info("config_reload_result", summary=result.summary(), ok=result.ok)
        # The receipt goes to #bot-health so the operator sees exactly what
        # applied and what still needs a restart — never silently absorbed.
        if self.bot is not None and self.bot.is_ready():
            with contextlib.suppress(Exception):
                await self._post_health(f"🔄 {result.summary()}", None)
        return result

    def _refresh_chat(self) -> None:
        """(Re)build the §6.6 ChatClient from the current config + key state.

        Called at setup and after every reload transaction. Rebuilds when the
        ON-DISK key differs from the live client's (a rotated key used to be
        ignored until restart). Keeps a status string alongside so /ask can
        tell an operator *why* the channel is down ("disabled" vs "no key").
        """
        cfg = self.holder.current
        if not cfg.chat.enabled:
            self.chat = None
            self._chat_status = "disabled"
        else:
            key = read_api_key(cfg.chat.api_key_file)
            if key:
                if self.chat is None or self._chat_key != key:
                    self.chat = ChatClient(self.holder, key)
                    self._chat_key = key
                self._chat_status = "ready"
            else:
                self.chat = None
                self._chat_key = None
                self._chat_status = "no_key"
        log.info("override_channel_status", status=self._chat_status, model=cfg.chat.model)
        if self.bot is not None:
            self.bot.chat = self.chat
            self.bot.chat_status = self._chat_status

    async def _graceful_shutdown(self) -> None:
        log.info("shutting_down")
        # Bound the whole sequence: a hung close (Discord gateway, Ears socket)
        # must not stall the process until systemd's SIGKILL — a 90s stop makes
        # every restart churn Ears through repeated voice rejoins, which resets
        # the DAVE handshake each time. Past the budget we drop straight to the
        # DB close and let the process exit.
        try:
            await asyncio.wait_for(self._shutdown_sequence(), timeout=_SHUTDOWN_TIMEOUT_S)
        except TimeoutError:
            log.warning("shutdown_timed_out", timeout_s=_SHUTDOWN_TIMEOUT_S)
        if self.conn is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.conn.close)
        log.info("shutdown_complete")

    async def _shutdown_sequence(self) -> None:
        if self.gateway is not None:
            with contextlib.suppress(Exception):
                await self.gateway.close()
        if self.ipc is not None:
            await self.ipc.stop()
        if self.speaker is not None:
            await self.speaker.close()
        if self.bot is not None and not self.bot.is_closed():
            with contextlib.suppress(Exception):
                await self.bot.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ── voice pipeline wiring (GDD §5) ───────────────────────────────────────

    def _on_audio(self, user_id: int, guild_id: int, pcm: bytes) -> None:
        """IPC audio hot path → dialog engine. Sync, never blocks."""
        if self.dialog is not None:
            self.dialog.on_audio(user_id, guild_id, pcm)

    def _dialog_on_utterance(
        self, user_id: int, guild_id: int, pcm: bytes, meta: CaptureMeta
    ) -> Coroutine[Any, Any, None]:
        assert self.dialog is not None
        return self.dialog.on_utterance(user_id, guild_id, pcm, meta)

    def _dialog_on_capture_start(
        self, user_id: int, guild_id: int, origin: CaptureOrigin, armed_gen: int | None
    ) -> int:
        assert self.dialog is not None
        return self.dialog.on_capture_start(user_id, guild_id, origin, armed_gen)

    def _purge_user(self, uid: int) -> None:
        """Drop every piece of per-user voice/dialog state Brain holds.

        Called from the IPC ``left`` event and from snapshot reconciliation
        (§19 posture). The dialog engine owns all per-user state (§5.4) —
        session, armed windows, capture, timing — so this is one call."""
        if self.dialog is not None:
            self.dialog.reset_user(uid)

    def _reconcile_snapshot(self, msg: dict[str, Any]) -> None:
        """Reconcile Brain's per-user view against Ears' state snapshot.

        Ears sends a full snapshot (per-guild connected state + SSRC↔user
        roster) right after every hello (GDD §15), replacing the lossy event
        deltas an IPC outage would have eaten. Users Brain is still tracking
        that Ears no longer sees (left during the outage) are purged exactly
        as if their ``left`` event had arrived. State is reset only where the
        views actually differ — the hello replay already handled the rest.
        """
        roster: set[int] = set()
        for guild in msg.get("guilds", []) or []:
            for entry in guild.get("users", []) or []:
                user_id = entry.get("user_id")
                if user_id is not None:
                    with contextlib.suppress(ValueError, TypeError):
                        roster.add(int(user_id))
        tracked: set[int] = self.dialog.tracked_users() if self.dialog is not None else set()
        stale = tracked - roster
        for uid in stale:
            self._purge_user(uid)
        if stale:
            log.info("ears_snapshot_reconciled", purged_users=len(stale))

    async def _on_control(self, msg: dict[str, Any]) -> None:
        t = msg.get("t")
        if t == "heartbeat":
            if self.health is not None:
                self.health.note_heartbeat(msg)
        elif t == "hello":
            log.info("ears_connected", version=msg.get("version"), proto=msg.get("proto"))
            # Every armed window / SSRC mapping the previous Ears process knew
            # is gone — dialog state must not outlive the audio path (§5.4).
            # The snapshot that follows re-purges anything Ears still sees
            # differently.
            if self.dialog is not None:
                self.dialog.reset_all()
            # A hello IS Ears liveness — resolve the card immediately rather
            # than waiting for the next heartbeat check cycle.
            if self.alarms is not None:
                await self.alarms.clear(AlarmCode.EARS_DOWN)
            if self.gateway is not None:
                await self.gateway.on_ears_hello()
        elif t == "snapshot":
            self._reconcile_snapshot(msg)
        elif t == "join_ok":
            if self.gateway is not None:
                await self.gateway.on_join_ok(int(msg.get("channel_id") or 0))
        elif t == "join_failed":
            if self.gateway is not None:
                await self.gateway.on_join_failed(
                    int(msg.get("channel_id") or 0), str(msg.get("reason", ""))
                )
        elif t == "driver_disconnected":
            # Voice session died under Ears (DAVE crash, 4014, channel gone).
            # Ears reports; the rejoin judgement stays here. Loud log + health
            # note so a silent voice absence becomes visible (GDD §20).
            log.warning(
                "ears_driver_disconnected",
                guild_id=msg.get("guild_id"),
                kind=msg.get("kind"),
                reason=msg.get("reason"),
            )
            if self.health is not None:
                self.health.note_driver_event(msg)
        elif t == "left":
            user_id = msg.get("user_id")
            if user_id is not None:
                # Purges capture state, armed windows, and the session (§19).
                self._purge_user(int(user_id))
        elif t == "speaking":
            pass  # informational; talk-over suppression happens in Ears
        else:
            log.warning("ipc_unknown_control", t=t)

    def _member_role_ids(self, user_id: int) -> list[int]:
        """This member's role ids from the guild cache (empty when unknown)."""
        assert self.bot
        guild = self.bot.get_guild(self.holder.current.discord.guild_id)
        member = guild.get_member(user_id) if guild is not None else None
        return [role.id for role in member.roles] if member is not None else []

    # ── periodic tasks ───────────────────────────────────────────────────────

    async def _sweep_loop(self) -> None:
        assert self.engine
        while True:
            await asyncio.sleep(_SWEEP_INTERVAL_S)
            # Per-iteration guard: a transient Discord/DB error in one sweep
            # must not kill the supervised task — _spawn treats that as fatal
            # and restarts the whole process (churning Ears through a DAVE
            # renegotiation) over what was a one-off REST failure.
            try:
                stale = await self.engine.sweep_stale()
            except Exception:
                log.exception("sweep_iteration_failed")
                continue
            if stale:
                log.info("incidents_marked_stale", ids=stale)

    async def _timer_loop(self) -> None:
        assert self.engine and self.bot
        while True:
            await asyncio.sleep(_TIMER_POLL_INTERVAL_S)
            # fire_due_timers commits fired=1 before delivery, so never
            # consume due rows while Discord is unusable (pre-login,
            # fetch_channel raises AttributeError and the ping is lost).
            # is_ready() is MISSING-safe pre-login; wait_until_ready() is not.
            if not self.bot.is_ready():
                continue
            try:
                pings = await self.engine.fire_due_timers(datetime.now(UTC))
                for ping in pings:
                    await self._announce_timer(ping)
            except Exception:
                log.exception("timer_iteration_failed")

    async def _reminder_loop(self) -> None:
        assert self.reminders and self.bot
        while True:
            await asyncio.sleep(_TIMER_POLL_INTERVAL_S)
            if not self.bot.is_ready():
                continue  # don't consume due reminders before Discord is usable
            try:
                await self.reminders.deliver_due(datetime.now(UTC))
            except Exception:
                log.exception("reminder_iteration_failed")

    async def _health_loop(self) -> None:
        assert self.health
        while True:
            await asyncio.sleep(_HEALTH_CHECK_INTERVAL_S)
            await self.health.check()
            # Retry any alarm card that couldn't reach Discord when raised
            # (pre-ready startup alarms land here once the bot is up).
            if self.alarms is not None:
                await self.alarms.flush()

    async def _announce_timer(self, ping: TimerPing) -> None:
        where = f" {ping.system_name}" if ping.system_name else ""
        note = f" — {ping.note}" if ping.note else ""
        delivered = await self._send_channel(
            self.holder.current.discord.channels.intel_alerts,
            f"⏰ **Timer{where}** is due{note} (set by <@{ping.created_by}>)",
        )
        # A structure timer eaten by a 403 is exactly the loss the corp set
        # the timer to avoid — degrade loudly (GDD §13 / §11.3).
        if self.alarms is not None:
            if not delivered:
                await self.alarms.raise_alarm(
                    AlarmCode.TIMER_UNDELIVERED,
                    AlarmSeverity.CRITICAL,
                    f"A timer ping{where or ''} fired but could not be posted to #intel-alerts.",
                    "check CORTANA's permissions on #intel-alerts; the timer "
                    "details are in the journal",
                )
            else:
                await self.alarms.clear(AlarmCode.TIMER_UNDELIVERED)
        if self.speaker is not None and ping.system_name:
            await self.speaker.say(ping.guild_id, f"Timer {ping.system_name} due.", PRIORITY_NORMAL)

    # ── discord-touching helpers (composition root only) ─────────────────────

    async def _send_channel(
        self, channel_id: int, content: str, embed: dict[str, Any] | None = None
    ) -> bool:
        """Send to a channel; True when the message landed. Failures raise
        the CHANNEL_UNWRITABLE alarm (keyed by channel id) and successes
        clear it — a broken channel id is visible without journal access."""
        assert self.bot
        import discord  # composition root: text-side discord.py only (constraint 2)

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.DiscordException as exc:
                log.error("channel_unavailable", channel_id=channel_id, error=str(exc))
                await self._channel_alarm(channel_id, str(exc))
                return False
        kwargs: dict[str, Any] = {}
        if embed is not None:
            kwargs["embed"] = discord.Embed.from_dict(embed)
        try:
            await channel.send(content or None, **kwargs)  # type: ignore[union-attr]
        except discord.DiscordException as exc:
            log.error("channel_send_failed", channel_id=channel_id, error=str(exc))
            await self._channel_alarm(channel_id, str(exc))
            return False
        if self.alarms is not None:
            await self.alarms.clear(AlarmCode.CHANNEL_UNWRITABLE, key=str(channel_id))
        return True

    async def _channel_alarm(self, channel_id: int, error: str) -> None:
        if self.alarms is None:
            return
        await self.alarms.raise_alarm(
            AlarmCode.CHANNEL_UNWRITABLE,
            AlarmSeverity.CRITICAL,
            f"Cannot post to channel {channel_id}: {error}",
            "check the channel id in cortana.yaml and CORTANA's permissions "
            "there (View Channel, Send Messages, Embed Links)",
            key=str(channel_id),
        )

    async def _post_health(self, content: str, embed: dict[str, Any] | None) -> None:
        await self._send_channel(self.holder.current.discord.channels.health, content, embed)

    # ── AlarmBus plumbing (GDD §11.3) ────────────────────────────────────────

    async def _alarm_send(
        self, content: str, embed: dict[str, Any] | None
    ) -> tuple[int, int] | None:
        """AlarmBus send: post a card to #bot-health, returning its ids.

        Deliberately NOT via ``_send_channel``: a failure here must only make
        the card retry later (dirty + flush), never raise further alarms."""
        bot = self.bot
        if bot is None or not bot.is_ready():
            return None
        import discord

        channel_id = self.holder.current.discord.channels.health
        try:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            message = await channel.send(  # type: ignore[union-attr]
                content or None,
                embed=discord.Embed.from_dict(embed) if embed is not None else None,
            )
        except Exception as exc:  # noqa: BLE001 — the bus retries; never crash it
            log.warning("alarm_card_post_failed", channel_id=channel_id, error=str(exc))
            return None
        return channel_id, message.id

    async def _alarm_edit(
        self, channel_id: int, message_id: int, content: str, embed: dict[str, Any] | None
    ) -> bool | None:
        """AlarmBus edit: True = landed, None = transient (retry the edit),
        False = message deleted (the bus re-posts a fresh card)."""
        bot = self.bot
        if bot is None or not bot.is_ready():
            return None
        import discord

        try:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            await channel.get_partial_message(message_id).edit(  # type: ignore[union-attr]
                content=content or None,
                embed=discord.Embed.from_dict(embed) if embed is not None else None,
            )
        except discord.NotFound:
            return False
        except Exception as exc:  # noqa: BLE001 — the bus retries; never crash it
            log.warning(
                "alarm_card_edit_failed",
                channel_id=channel_id,
                message_id=message_id,
                error=str(exc),
            )
            return None
        return True

    def _ipc_status(self) -> tuple[bool, float | None]:
        """Ears liveness for /botstatus: (alive, heartbeat age in seconds)."""
        import time

        from cortana.health import HEARTBEAT_TIMEOUT_S

        if self.ipc is None:
            return False, None
        last = self.ipc.last_heartbeat
        age = None if last is None else max(0.0, time.monotonic() - last)
        return self.ipc.is_alive(HEARTBEAT_TIMEOUT_S), age


def parsed_intent_severity(intent: Any) -> Severity:
    """Default severity for an intent (GDD §6.1); NONE when unmapped."""
    return INTENT_SEVERITY.get(intent, Severity.NONE)


def build_app(holder: ConfigHolder) -> App:
    """Create the (unstarted) application around a loaded config holder."""
    return App(holder)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cortana", description="CORTANA Brain")
    parser.add_argument("--config", required=True, metavar="PATH", help="path to cortana.yaml")
    args = parser.parse_args(argv)

    configure_logging()
    try:
        holder = ConfigHolder(Path(args.config))
    except ConfigError as exc:
        log.error("config_invalid", error=str(exc))
        # 78 = EX_CONFIG: paired with RestartPreventExitStatus=78 in the
        # unit so a bad config edit fails fast instead of crash-looping.
        return 78

    app = build_app(holder)
    # Signal handlers normally shut down first; a stray ^C must still exit 0.
    with contextlib.suppress(KeyboardInterrupt):
        app.run()
    return 0


__all__ = ["App", "build_app", "configure_logging", "main"]


if __name__ == "__main__":
    sys.exit(main())
