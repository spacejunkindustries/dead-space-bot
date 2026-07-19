"""Operator surface: /botstatus, /doctor, /reload (GDD §18).

The phone-admin trio, all gated on Manage Guild or the FC role:

- **/botstatus** — ONE phone-sized embed: voice/Ears state, STT and wake
  health, dialog sessions in flight, incidents in the last hour, active
  alarms, uptime. (Named ``botstatus`` because ``/status`` is the voice
  QUERY intent's slash twin — the pilots' active-incident summary,
  constraint 10 — and cannot be repurposed.)
- **/doctor** — runs the OFFLINE preflight checks (:mod:`cortana.doctor`)
  in a thread and posts the PASS/WARN/FAIL table ephemerally. Online checks
  stay CLI-only — a Discord problem is what this command diagnoses, so it
  must not depend on more Discord than one followup message.
- **/reload** — the slash twin of SIGHUP: calls the SAME reload transaction
  the signal handler runs (injected as ``bot.request_reload`` by the
  composition root) and replies with the :class:`ReloadResult` receipt.

Slash-only is deliberate — constraint 10 governs the voice→slash direction
only; these are operator levers, not pilot commands.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord
import structlog
from discord import app_commands
from discord.ext import commands

from cortana.core import db
from cortana.doctor import DoctorContext, Status, run_offline_checks
from cortana.doctor import render as render_doctor
from cortana.dsc.cogs.admin import _is_admin

if TYPE_CHECKING:  # pragma: no cover
    from cortana.dsc.bot import AuraBot

__all__ = ["StatusCog", "format_uptime"]

log = structlog.get_logger(__name__)

#: Discord embed descriptions cap at 4096; leave room for the code fence.
_DOCTOR_CHUNK = 3900

_COLOR_OK = 0x2ECC71
_COLOR_WARN = 0xF1C40F
_COLOR_FAIL = 0xE74C3C


def format_uptime(seconds: float) -> str:
    """``93784.0`` → ``"1d 2h 3m"`` (phone-sized, coarse on purpose)."""
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


class StatusCog(commands.Cog):
    """The operator's phone-sized window into the running process."""

    def __init__(self, bot: AuraBot) -> None:
        self.bot = bot

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = "Admin only — needs Manage Guild or the FC role."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

    # ── /botstatus ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="botstatus", description="(admin) One-screen operational status of CORTANA"
    )
    @app_commands.check(_is_admin)
    async def botstatus(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await self._status_embed(interaction.guild_id)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _status_embed(self, guild_id: int) -> discord.Embed:
        bot = self.bot
        health = bot.health_reporter
        alarms = bot.alarms

        # Ears / voice.
        if bot.ipc_status is not None:
            alive, age = bot.ipc_status()
            if alive:
                ears = f"connected (heartbeat {age:.0f}s ago)" if age is not None else "connected"
            elif age is not None:
                ears = f"DOWN (last heartbeat {age:.0f}s ago)"
            else:
                ears = "never connected"
        else:
            ears = "unknown"
        gateway = bot.voice_gateway
        joined = gateway.joined_channel_id if gateway is not None else None
        voice = f"in <#{joined}>" if joined is not None else "not in voice"

        # STT.
        if health is not None and health.stt_watchdog_degraded:
            stt = "LATCHED — watchdog respawn cap (run /reload)"
        elif health is not None and health.stt_degraded:
            stt = f"degraded — {health.low_streak} consecutive low-confidence results"
        else:
            stt = "ok"

        # Wake.
        if health is not None:
            counters = health.wake_counters
            if health.wake_faulted:
                wake = "FAULTED — model failed to build"
            elif counters:
                wake = (
                    f"hits {counters.get('hits', 0)} · "
                    f"inferences {counters.get('inferences', 0)} · "
                    f"frames {counters.get('frames_seen', 0)}"
                )
            else:
                wake = "no counters yet"
        else:
            wake = "unknown"

        sessions = bot.dialog_sessions() if bot.dialog_sessions is not None else 0
        since = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        incidents_1h = await asyncio.to_thread(
            db.query_value,
            bot.conn,
            "SELECT COUNT(*) FROM incidents WHERE guild_id = ? AND opened_at >= ?",
            (guild_id, since),
        )

        if alarms is not None:
            active = alarms.active()
            alarm_line = (
                "none"
                if not active
                else f"{len(active)} — " + ", ".join(a.code.value for a in active[:6])
            )
            degraded = bool(active)
        else:
            alarm_line = "unknown"
            degraded = False

        uptime = format_uptime(time.monotonic() - bot.started_at_monotonic)

        embed = discord.Embed(
            title="CORTANA status",
            color=_COLOR_FAIL if degraded else _COLOR_OK,
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Ears", value=ears, inline=True)
        embed.add_field(name="Voice", value=voice, inline=True)
        embed.add_field(name="Uptime", value=uptime, inline=True)
        embed.add_field(name="STT", value=stt, inline=True)
        embed.add_field(name="Wake", value=wake, inline=True)
        embed.add_field(name="Dialogs in flight", value=str(sessions), inline=True)
        embed.add_field(name="Incidents (1h)", value=str(incidents_1h or 0), inline=True)
        embed.add_field(
            name="Fleet-ops mode",
            value="on" if bot.discipline.fleetmode else "off",
            inline=True,
        )
        embed.add_field(name="Active alarms", value=alarm_line, inline=False)
        return embed

    # ── /doctor ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="doctor", description="(admin) Run the offline preflight checks (PASS/WARN/FAIL)"
    )
    @app_commands.check(_is_admin)
    async def doctor(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ctx = DoctorContext(config_path=self.bot.holder.path)
        results = await asyncio.to_thread(run_offline_checks, ctx)
        table = render_doctor(results, ctx.config_path)
        if any(r.status is Status.FAIL for r in results):
            color = _COLOR_FAIL
        elif any(r.status is Status.WARN for r in results):
            color = _COLOR_WARN
        else:
            color = _COLOR_OK
        embeds = [
            discord.Embed(
                title="CORTANA doctor — offline checks" if i == 0 else None,
                description=f"```\n{chunk}\n```",
                color=color,
            )
            for i, chunk in enumerate(_chunk(table, _DOCTOR_CHUNK))
        ]
        log.info(
            "doctor_via_slash",
            user_id=interaction.user.id,
            failed=sum(1 for r in results if r.status is Status.FAIL),
            warned=sum(1 for r in results if r.status is Status.WARN),
        )
        await interaction.followup.send(embeds=embeds[:10], ephemeral=True)

    # ── /reload ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="reload",
        description="(admin) Reload cortana.yaml + gazetteer.yaml + routing.yaml (same as SIGHUP)",
    )
    @app_commands.check(_is_admin)
    async def reload(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        request_reload = self.bot.request_reload
        if request_reload is None:
            await interaction.followup.send(
                "Reload isn't wired in this process — restart the service instead.",
                ephemeral=True,
            )
            return
        try:
            result = await request_reload()
        except Exception:
            # reload_all never raises for operator-caused problems, so this
            # is an internal bug — say so instead of an eternal spinner.
            log.exception("reload_via_slash_failed")
            await interaction.followup.send(
                "❌ Reload failed internally — logged. The old config stays in force.",
                ephemeral=True,
            )
            return
        log.info("reload_via_slash", user_id=interaction.user.id, ok=result.ok)
        await interaction.followup.send(f"🔄 {result.summary()}", ephemeral=True)

    # ── /restart ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="restart",
        description="(admin) Restart the CORTANA brain process (systemd brings it back)",
    )
    @app_commands.check(_is_admin)
    async def restart(self, interaction: discord.Interaction) -> None:
        """The remote kick for a wedged brain (GDD §18): graceful shutdown,
        `Restart=always` brings the process back in seconds. Ears is NOT
        restarted — it stays connected and buffers audio through the gap, so
        no DAVE renegotiation. Wake models, STT, and the IPC handshake all
        rebuild fresh, which clears most "she went deaf" states without SSH.
        """
        request_restart = self.bot.request_restart
        if request_restart is None:
            await interaction.response.send_message(
                "Restart isn't wired in this process — `systemctl restart "
                "cortana-brain` from the droplet instead.",
                ephemeral=True,
            )
            return
        # Answer FIRST — after the shutdown starts, this interaction dies.
        await interaction.response.send_message(
            "🔄 Restarting the brain — back in ~15s. Ears stays connected and "
            "buffers audio through the gap. `/botstatus` to check on her after.",
            ephemeral=True,
        )
        log.info("restart_via_slash", user_id=interaction.user.id)
        await request_restart()


def _chunk(text: str, size: int) -> list[str]:
    """Split on line boundaries into <= size chunks (never mid-row)."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.splitlines():
        if length + len(line) + 1 > size and current:
            chunks.append("\n".join(current))
            current = []
            length = 0
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or [text]
