"""AURA Brain entrypoint: composition root, task supervision, shutdown.

``python -m aura --config /etc/aura/aura.yaml``

Everything is built here, in the order INTERFACES.md prescribes:
ConfigHolder → db → Gazetteer → Discipline → IpcServer → Speaker →
Transcriber → CaptureManager → IncidentEngine → AuraBot, then the voice
gateway and health reporter are wired around them. One asyncio event loop,
owned by this module.

Voice pipeline wiring (GDD §5): IPC audio frames → ``CaptureManager.feed``
(sync hot path) → wake/VAD/endpoint → ``on_utterance`` → STT in a thread →
grammar parse → phonetic resolve → ``IncidentEngine.report`` → card posted by
the bot + spoken confirmation via the Speaker (falling back to channel text
when speech is suppressed or over the cap).

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

from aura import tts as tts_mod
from aura.audio.capture import CaptureManager
from aura.audio.stt import Transcriber, make_transcriber
from aura.audio.vad import VadGate
from aura.audio.wake import OpenWakeWordDetector
from aura.config import ConfigError, ConfigHolder
from aura.core import db
from aura.core.discipline import Discipline
from aura.core.incidents import IncidentEngine, Poster, TimerPing
from aura.dsc.bot import AuraBot, read_token
from aura.health import HealthReporter
from aura.ipc import PRIORITY_ALERT, PRIORITY_NORMAL, IpcServer
from aura.nlu import grammar, phonetics
from aura.nlu.gazetteer import Gazetteer
from aura.tts import Speaker
from aura.types import INTENT_SEVERITY, CardRender, Outcome, Severity, Tier
from aura.voice_gateway import VoiceGateway

log = structlog.get_logger(__name__)

_SWEEP_INTERVAL_S = 60.0
_TIMER_POLL_INTERVAL_S = 15.0
_HEALTH_CHECK_INTERVAL_S = 5.0


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
        self.engine: IncidentEngine | None = None
        self.bot: AuraBot | None = None
        self.health: HealthReporter | None = None
        self.gateway: VoiceGateway | None = None
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

        self.gazetteer = Gazetteer(self.conn, cfg.gazetteer)
        await asyncio.to_thread(self.gazetteer.load)
        log.info("gazetteer_loaded", systems=len(self.gazetteer.systems))

        self.discipline = Discipline(self.holder)
        self.ipc = IpcServer(self.holder, self._on_audio, self._on_control)
        self.speaker = Speaker(self.holder, self.ipc)

        self.transcriber = await asyncio.to_thread(make_transcriber, cfg.stt)
        vad = VadGate(cfg.capture.vad_aggressiveness)
        wake = await asyncio.to_thread(OpenWakeWordDetector, cfg.wake)
        self.capture = CaptureManager(self.holder, vad, wake, self._on_utterance)

        late_poster = _LatePoster()
        rules_path = self.holder.path.parent / "routing.yaml"
        self.engine = IncidentEngine(
            self.conn, self.holder, self.gazetteer, self.discipline, late_poster, rules_path
        )
        self.bot = AuraBot(
            self.holder, self.engine, self.gazetteer, self.discipline, self.speaker, self.conn
        )
        late_poster.bind(self.bot)

        self.health = HealthReporter(self.holder, self._post_health)
        self.gateway = VoiceGateway(self.holder, self.ipc, self.conn, self._announce_join)
        self.gateway.set_census_listener(self.health.set_humans_present)
        self.bot.add_listener(self._on_voice_state_update, "on_voice_state_update")

        # Load the muted-voice set so /mute-voice survives restarts.
        rows = await asyncio.to_thread(db.query, self.conn, "SELECT user_id FROM voice_mutes", ())
        self.speaker.set_voice_mutes({row["user_id"] for row in rows})

    # ── run / shutdown ───────────────────────────────────────────────────────

    def run(self) -> None:
        """Blocking entrypoint: owns the event loop from start to finish."""
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        await self.setup()
        assert self.ipc and self.bot and self.engine and self.health  # narrow for typing

        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGHUP, self._on_sighup)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._request_shutdown, sig.name)

        token = read_token(self.holder.current.discord)
        await self.ipc.start()

        self._spawn("discord-bot", self.bot.start(token))
        self._spawn("stale-sweep", self._sweep_loop())
        self._spawn("timer-poll", self._timer_loop())
        self._spawn("health-check", self._health_loop())

        log.info("aura_started")
        await self._shutdown.wait()
        await self._graceful_shutdown()

    def _spawn(self, name: str, coro: Coroutine[Any, Any, Any]) -> None:
        """Run a critical task; if it crashes or exits, shut the process down."""

        async def _supervised() -> None:
            try:
                await coro
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
        try:
            self.holder.reload()
        except ConfigError as exc:
            log.error("config_reload_failed", error=str(exc))

    async def _graceful_shutdown(self) -> None:
        log.info("shutting_down")
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
        if self.conn is not None:
            await asyncio.to_thread(self.conn.close)
        log.info("shutdown_complete")

    # ── voice pipeline wiring (GDD §5) ───────────────────────────────────────

    def _on_audio(self, user_id: int, guild_id: int, pcm: bytes) -> None:
        """IPC audio hot path — sync, never blocks (constraint: thin + RAM only)."""
        if self.health is not None:
            self.health.note_audio()
        if self.capture is not None:
            self.capture.feed(user_id, guild_id, pcm)

    async def _on_control(self, msg: dict[str, Any]) -> None:
        t = msg.get("t")
        if t == "heartbeat":
            if self.health is not None:
                self.health.note_heartbeat(msg)
        elif t == "hello":
            log.info("ears_connected", version=msg.get("version"))
            if self.gateway is not None:
                await self.gateway.on_ears_hello()
        elif t == "left":
            user_id = msg.get("user_id")
            if user_id is not None and self.capture is not None:
                self.capture.drop_user(int(user_id))
        elif t == "speaking":
            pass  # informational; talk-over suppression happens in Ears
        else:
            log.warning("ipc_unknown_control", t=t)

    async def _on_utterance(self, user_id: int, guild_id: int, pcm: bytes) -> None:
        """One wake-gated utterance: STT → grammar → resolve → engine → reply.

        The pcm buffer is dropped the moment ``transcribe`` returns; only the
        transcript is retained (constraint 5, GDD §19).
        """
        assert self.gazetteer and self.transcriber and self.engine and self.health
        cfg = self.holder.current
        if self.health is not None:
            self.health.record_wake_hit()

        if not self._may_voice_trigger(user_id):
            log.info("voice_trigger_denied", user_id=user_id)
            return

        bias = self.gazetteer.prompt_bias_text() if cfg.stt.bias_with_gazetteer else ""
        result = await asyncio.to_thread(self.transcriber.transcribe, pcm, bias)
        del pcm  # transcript only from here on
        log.info(
            "utterance_transcribed",
            user_id=user_id,
            text=result.text,
            avg_logprob=round(result.avg_logprob, 3),
        )

        parsed = grammar.parse(result.text)
        if parsed is None:
            self.health.record_rejected()
            log.info("utterance_no_intent", user_id=user_id, text=result.text)
            return

        resolution = None
        if parsed.system_text:
            priors = await asyncio.to_thread(self.engine.build_prior_context, guild_id, user_id)
            resolution = await asyncio.to_thread(
                phonetics.resolve,
                parsed.system_text,
                self.gazetteer,
                priors,
                cfg.matching,
                self.conn,
            )
            self.health.record_stt(
                resolution.best.score if resolution.best else 0.0, resolution.tier
            )

        outcome = await self.engine.report(guild_id, user_id, parsed, resolution)
        self._count_outcome(outcome.outcome)

        # LOW tier: "say again" — reopen capture so the retry needs no wake word.
        if (
            outcome.outcome is Outcome.REJECTED
            and (resolution is None or resolution.tier is Tier.LOW)
            and self.capture is not None
            and parsed.system_text
        ):
            self.capture.reopen(user_id, guild_id)

        await self._reply(guild_id, user_id, parsed_intent_severity(parsed.intent), outcome)

    def _count_outcome(self, outcome: Outcome) -> None:
        assert self.health
        if outcome is Outcome.POSTED:
            self.health.record_incident_posted()
        elif outcome is Outcome.FOLDED:
            self.health.record_incident_folded()
        elif outcome is Outcome.REJECTED:
            self.health.record_rejected()

    async def _reply(self, guild_id: int, user_id: int, severity: Severity, outcome: Any) -> None:
        """Speak the outcome utterance; fall back to channel text when speech
        is disabled, muted, or over the §12.2 length cap."""
        assert self.speaker and self.health
        utterance = outcome.utterance
        if not utterance:
            return
        if self.health.degraded:
            utterance = f"{utterance} {tts_mod.degraded()}"
        priority = PRIORITY_ALERT if severity is Severity.HIGH else PRIORITY_NORMAL
        spoken = await self.speaker.say(guild_id, utterance, priority, user_id=user_id)
        if not spoken:
            await self._send_channel(
                self.holder.current.discord.channels.intel_live, f"🔊 {utterance}"
            )

    def _may_voice_trigger(self, user_id: int) -> bool:
        """Fleetmode gate (GDD §11.1): voice triggers may be FC-only."""
        assert self.bot and self.discipline
        guild = self.bot.get_guild(self.holder.current.discord.guild_id)
        member = guild.get_member(user_id) if guild is not None else None
        role_ids = [role.id for role in member.roles] if member is not None else []
        return self.discipline.may_voice_trigger(role_ids)

    # ── periodic tasks ───────────────────────────────────────────────────────

    async def _sweep_loop(self) -> None:
        assert self.engine
        while True:
            await asyncio.sleep(_SWEEP_INTERVAL_S)
            stale = await self.engine.sweep_stale()
            if stale:
                log.info("incidents_marked_stale", ids=stale)

    async def _timer_loop(self) -> None:
        assert self.engine
        while True:
            await asyncio.sleep(_TIMER_POLL_INTERVAL_S)
            pings = await self.engine.fire_due_timers(datetime.now(UTC))
            for ping in pings:
                await self._announce_timer(ping)

    async def _health_loop(self) -> None:
        assert self.health
        while True:
            await asyncio.sleep(_HEALTH_CHECK_INTERVAL_S)
            await self.health.check()

    async def _announce_timer(self, ping: TimerPing) -> None:
        where = f" {ping.system_name}" if ping.system_name else ""
        note = f" — {ping.note}" if ping.note else ""
        await self._send_channel(
            self.holder.current.discord.channels.intel_alerts,
            f"⏰ **Timer{where}** is due{note} (set by <@{ping.created_by}>)",
        )
        if self.speaker is not None and ping.system_name:
            await self.speaker.say(ping.guild_id, f"Timer {ping.system_name} due.", PRIORITY_NORMAL)

    # ── discord-touching helpers (composition root only) ─────────────────────

    async def _send_channel(
        self, channel_id: int, content: str, embed: dict[str, Any] | None = None
    ) -> None:
        assert self.bot
        import discord  # composition root: text-side discord.py only (constraint 2)

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.DiscordException as exc:
                log.error("channel_unavailable", channel_id=channel_id, error=str(exc))
                return
        kwargs: dict[str, Any] = {}
        if embed is not None:
            kwargs["embed"] = discord.Embed.from_dict(embed)
        try:
            await channel.send(content or None, **kwargs)  # type: ignore[union-attr]
        except discord.DiscordException as exc:
            log.error("channel_send_failed", channel_id=channel_id, error=str(exc))

    async def _post_health(self, content: str, embed: dict[str, Any] | None) -> None:
        await self._send_channel(self.holder.current.discord.channels.health, content, embed)

    async def _announce_join(self, channel_id: int) -> None:
        """§19 consent announcement, into the voice channel's text chat."""
        from aura.voice_gateway import ANNOUNCEMENT

        await self._send_channel(channel_id, ANNOUNCEMENT)

    async def _on_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        """discord.py listener → watched-channel census → voice gateway."""
        assert self.gateway
        if member.bot:
            return
        watched = set(self.holder.current.discord.watch_voice_channels)
        for state in (before, after):
            channel = state.channel
            if channel is None or channel.id not in watched:
                continue
            humans = sum(1 for m in channel.members if not m.bot)
            await self.gateway.on_voice_update(channel.id, humans)


def parsed_intent_severity(intent: Any) -> Severity:
    """Default severity for an intent (GDD §6.1); NONE when unmapped."""
    return INTENT_SEVERITY.get(intent, Severity.NONE)


def build_app(holder: ConfigHolder) -> App:
    """Create the (unstarted) application around a loaded config holder."""
    return App(holder)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aura", description="AURA Brain")
    parser.add_argument("--config", required=True, metavar="PATH", help="path to aura.yaml")
    args = parser.parse_args(argv)

    configure_logging()
    try:
        holder = ConfigHolder(Path(args.config))
    except ConfigError as exc:
        log.error("config_invalid", error=str(exc))
        return 2

    app = build_app(holder)
    # Signal handlers normally shut down first; a stray ^C must still exit 0.
    with contextlib.suppress(KeyboardInterrupt):
        app.run()
    return 0


__all__ = ["App", "build_app", "configure_logging", "main"]


if __name__ == "__main__":
    sys.exit(main())
