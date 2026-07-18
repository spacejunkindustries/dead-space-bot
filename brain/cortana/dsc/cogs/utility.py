"""Utility slash commands: /evetime /route /history /remindme /poll (GDD §7).

Corp quality-of-life surfaces, slash-only by design — none of them posts an
alert or mentions anyone, so the voice-twin invariant (CLAUDE.md constraint
10) does not apply; it constrains voice commands, and these have none.

The cog stays a thin adapter: everything that can be pure lives in the
module-level helpers (route/poll/reminder logic, all unit tested in
``tests/test_utility.py``). Poll buttons follow the views.py pattern — plain
buttons carrying stable custom_ids, dispatched by a persistent
``DynamicItem`` handler so votes survive Brain restarts, and the poll card is
edited in place (constraint 9 spirit: one poll, one message).

custom_id scheme (extends the views.py registry):

    aura:poll:{poll_id}:{option_idx}    poll vote button
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import discord
import structlog
from discord import app_commands
from discord.ext import commands

from cortana.core import db
from cortana.core.incidents import parse_duration
from cortana.dsc.cogs.intel import system_autocomplete
from cortana.types import Intent

if TYPE_CHECKING:  # pragma: no cover
    from cortana.dsc.bot import AuraBot

__all__ = [
    "HISTORY_DEFAULT_HOURS",
    "HISTORY_MAX_HOURS",
    "POLL_MAX_OPTIONS",
    "REMINDER_MAX_DURATION",
    "REMINDER_MAX_PENDING",
    "PollVoteButton",
    "ReminderService",
    "UtilityCog",
    "add_reminder",
    "close_poll",
    "create_poll",
    "evetime_text",
    "fire_due_reminders",
    "fold_poll_votes",
    "format_route",
    "history_line",
    "parse_poll_custom_id",
    "pending_reminder_count",
    "poll_custom_id",
    "poll_lines",
    "poll_row",
    "poll_vote_indices",
    "recent_incidents",
    "record_vote",
    "reminder_fires_at",
    "render_poll_embed",
    "set_poll_message",
]

log = structlog.get_logger(__name__)

#: /remindme guard rails (task spec): at most this many unfired reminders per
#: pilot, and nothing scheduled further out than a week.
REMINDER_MAX_PENDING = 10
REMINDER_MAX_DURATION = timedelta(days=7)

#: /history look-back window bounds, in hours.
HISTORY_DEFAULT_HOURS = 24
HISTORY_MAX_HOURS = 72

#: /poll option count (two required + two optional command parameters).
POLL_MAX_OPTIONS = 4

#: A Discord message body caps at 2000 chars; long routes degrade gracefully.
_ROUTE_TEXT_MAX = 1900


def _iso(dt: datetime) -> str:
    """Canonical timestamp format: fixed-width so string comparison == time order."""
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


# ── /evetime (pure) ──────────────────────────────────────────────────────────


def evetime_text(now: datetime) -> str:
    """EVE time is UTC; the ``<t:…:t>`` token renders in the viewer's locale,
    which is the "local offset hint" without CORTANA ever knowing a timezone."""
    utc = now.astimezone(UTC)
    unix = int(utc.timestamp())
    return (
        f"🕐 EVE time: **{utc:%H:%M:%S}** — {utc:%Y-%m-%d} (UTC)\n"
        f"Your local time: <t:{unix}:t> (<t:{unix}:d>)"
    )


# ── /route (pure) ────────────────────────────────────────────────────────────


def format_route(names: Sequence[str]) -> str:
    """Render a jump path: ``**A** → **B** → **C** (2 jumps)``."""
    jumps = len(names) - 1
    plural = "jump" if jumps == 1 else "jumps"
    return " → ".join(f"**{n}**" for n in names) + f" ({jumps} {plural})"


# ── /history (sync db helpers — always called via asyncio.to_thread) ─────────


def recent_incidents(
    conn: sqlite3.Connection, guild_id: int, system_id: int, since_iso: str
) -> list[sqlite3.Row]:
    """Incidents for one system since ``since_iso``, newest first, with the
    distinct-reporter count (opener + everyone folded in, GDD §9.1)."""
    return db.query(
        conn,
        "SELECT i.id, i.type, i.opened_at, i.status,"
        " 1 + (SELECT COUNT(DISTINCT u.user_id) FROM incident_updates u"
        "      WHERE u.incident_id = i.id AND u.user_id != i.reporter_id) AS reporters"
        " FROM incidents i"
        " WHERE i.guild_id = ? AND i.system_id = ? AND i.opened_at >= ?"
        " ORDER BY i.opened_at DESC LIMIT 15",
        (guild_id, system_id, since_iso),
    )


_HISTORY_TYPE_BADGES: dict[str, str] = {
    str(Intent.HOSTILE_SPOTTED): "🟠 Hostiles",
    str(Intent.UNDER_ATTACK): "🔴 Under attack",
    str(Intent.ASSIST_REQUEST): "🔴 Assist request",
    str(Intent.GATE_CAMP): "🟠 Gate camp",
    str(Intent.TIMER): "⏰ Timer",
    str(Intent.FORMUP): "🔵 Form-up",
}

_HISTORY_STATUS_BADGES: dict[str, str] = {
    "ACTIVE": "🔴 active",
    "STALE": "🟡 stale",
    "RESOLVED": "✅ resolved",
}


def history_line(incident_type: str, opened_at: str, reporters: int, status: str) -> str:
    """One compact /history embed line: type, relative time, reporters, status."""
    badge = _HISTORY_TYPE_BADGES.get(incident_type, incident_type)
    ts = int(datetime.fromisoformat(opened_at).timestamp())
    status_badge = _HISTORY_STATUS_BADGES.get(status, status.lower())
    return f"{badge} — <t:{ts}:R> · reported by {reporters} · {status_badge}"


# ── /remindme (pure + sync db helpers) ───────────────────────────────────────


def reminder_fires_at(now: datetime, duration_text: str) -> datetime | None:
    """Fire time for a reminder, reusing the shared grammar duration parser.

    None when no duration parses or it exceeds :data:`REMINDER_MAX_DURATION`
    — the caller rejects with a helpful message instead of guessing.
    """
    delta = parse_duration(duration_text)
    if delta is None or delta > REMINDER_MAX_DURATION:
        return None
    return now + delta


def add_reminder(
    conn: sqlite3.Connection, guild_id: int, user_id: int, fires_at: datetime, message: str
) -> int:
    return db.execute(
        conn,
        "INSERT INTO reminders (guild_id, user_id, fires_at, message) VALUES (?, ?, ?, ?)",
        (guild_id, user_id, _iso(fires_at), message),
    )


def pending_reminder_count(conn: sqlite3.Connection, user_id: int) -> int:
    return int(
        db.query_value(
            conn, "SELECT COUNT(*) FROM reminders WHERE user_id = ? AND fired = 0", (user_id,)
        )
    )


def fire_due_reminders(conn: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    """Mark due reminders fired and return their rows (fire_due_timers pattern)."""
    rows = db.query(
        conn,
        "SELECT * FROM reminders WHERE fired = 0 AND fires_at <= ? ORDER BY fires_at",
        (_iso(now),),
    )
    for row in rows:
        db.execute(conn, "UPDATE reminders SET fired = 1 WHERE id = ?", (row["id"],))
    return rows


class ReminderService:
    """Delivers due /remindme reminders: DM first, #intel-live mention fallback.

    Owned by ``__main__`` and driven by its reminder poll loop, exactly like
    the engine's ``fire_due_timers`` + timer loop pair.
    """

    def __init__(self, conn: sqlite3.Connection, bot: AuraBot) -> None:
        self._conn = conn
        self._bot = bot

    async def deliver_due(self, now: datetime) -> int:
        rows = await asyncio.to_thread(fire_due_reminders, self._conn, now)
        for row in rows:
            await self._deliver(row["user_id"], row["message"])
        if rows:
            log.info("reminders_fired", ids=[row["id"] for row in rows])
        return len(rows)

    async def _deliver(self, user_id: int, message: str) -> None:
        text = f"⏰ Reminder: {message}"
        try:
            user = self._bot.get_user(user_id) or await self._bot.fetch_user(user_id)
            await user.send(text)
            return
        except discord.DiscordException:
            log.info("reminder_dm_failed_falling_back", user_id=user_id)
        channel_id = self._bot.holder.current.discord.channels.intel_live
        try:
            channel = self._bot.get_channel(channel_id) or await self._bot.fetch_channel(channel_id)
            if isinstance(channel, discord.TextChannel | discord.Thread):
                await channel.send(
                    f"<@{user_id}> {text} *(your DMs are closed)*",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
        except discord.DiscordException:
            log.error("reminder_delivery_failed", user_id=user_id)


# ── /poll (pure helpers) ─────────────────────────────────────────────────────

_POLL_TEMPLATE = r"aura:poll:(?P<poll>[0-9]+):(?P<idx>[0-9]+)"
_POLL_RE = re.compile(rf"^{_POLL_TEMPLATE}$")

_POLL_OPTION_BADGES = ("1️⃣", "2️⃣", "3️⃣", "4️⃣")
_POLL_BAR_WIDTH = 10


def poll_custom_id(poll_id: int, option_idx: int) -> str:
    """Build a poll vote-button ``custom_id`` (module docstring scheme)."""
    return f"aura:poll:{poll_id}:{option_idx}"


def parse_poll_custom_id(custom_id: str) -> tuple[int, int] | None:
    """Parse ``aura:poll:{id}:{idx}``; None for foreign/invalid ids."""
    m = _POLL_RE.match(custom_id)
    if m is None:
        return None
    return int(m.group("poll")), int(m.group("idx"))


def fold_poll_votes(option_indices: Iterable[int], n_options: int) -> tuple[int, ...]:
    """Fold per-user vote rows into per-option counts.

    Out-of-range indices (an option count shrunk by a bad edit) are dropped
    rather than crashing the card render.
    """
    counts = [0] * n_options
    for idx in option_indices:
        if 0 <= idx < n_options:
            counts[idx] += 1
    return tuple(counts)


def poll_lines(options: Sequence[str], counts: Sequence[int]) -> list[str]:
    """One embed line per option: badge, label, proportional bar, count."""
    total = sum(counts)
    lines: list[str] = []
    for badge, option, count in zip(_POLL_OPTION_BADGES, options, counts, strict=False):
        filled = round(_POLL_BAR_WIDTH * count / total) if total else 0
        bar = "▰" * filled + "▱" * (_POLL_BAR_WIDTH - filled)
        lines.append(f"{badge} **{option}** {bar} {count}")
    return lines


def render_poll_embed(
    question: str,
    options: Sequence[str],
    counts: Sequence[int],
    *,
    poll_id: int,
    author_id: int,
    closed: bool,
) -> dict[str, object]:
    """Embed dict (``discord.Embed.from_dict`` shape) for the poll card."""
    total = sum(counts)
    votes = f"{total} vote{'s' if total != 1 else ''}"
    footer = (
        f"Poll #{poll_id} · closed"
        if closed
        else f"Poll #{poll_id} · one vote per pilot — press again to switch · {votes}"
    )
    return {
        "title": f"📊 {question}",
        "description": "\n".join(poll_lines(options, counts)),
        "color": 0x95A5A6 if closed else 0x3498DB,
        "fields": [{"name": "Opened by", "value": f"<@{author_id}>", "inline": True}],
        "footer": {"text": footer},
    }


# ── /poll (sync db helpers — always called via asyncio.to_thread) ────────────


def create_poll(
    conn: sqlite3.Connection,
    guild_id: int,
    author_id: int,
    question: str,
    options: Sequence[str],
    opened_at: datetime,
) -> int:
    return db.execute(
        conn,
        "INSERT INTO polls (guild_id, author_id, question, options_json, opened_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (guild_id, author_id, question, json.dumps(list(options)), _iso(opened_at)),
    )


def set_poll_message(
    conn: sqlite3.Connection, poll_id: int, channel_id: int, message_id: int
) -> None:
    db.execute(
        conn,
        "UPDATE polls SET channel_id = ?, message_id = ? WHERE id = ?",
        (channel_id, message_id, poll_id),
    )


def poll_row(conn: sqlite3.Connection, poll_id: int) -> sqlite3.Row | None:
    return db.query_one(conn, "SELECT * FROM polls WHERE id = ?", (poll_id,))


def record_vote(
    conn: sqlite3.Connection, poll_id: int, user_id: int, option_idx: int, at: datetime
) -> None:
    """Upsert: one vote per pilot, switchable by pressing another option."""
    db.execute(
        conn,
        "INSERT INTO poll_votes (poll_id, user_id, option_idx, at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(poll_id, user_id)"
        " DO UPDATE SET option_idx = excluded.option_idx, at = excluded.at",
        (poll_id, user_id, option_idx, _iso(at)),
    )


def poll_vote_indices(conn: sqlite3.Connection, poll_id: int) -> list[int]:
    rows = db.query(conn, "SELECT option_idx FROM poll_votes WHERE poll_id = ?", (poll_id,))
    return [row["option_idx"] for row in rows]


def close_poll(conn: sqlite3.Connection, poll_id: int, closed_at: datetime) -> None:
    db.execute(conn, "UPDATE polls SET closed_at = ? WHERE id = ?", (_iso(closed_at), poll_id))


# ── poll discord surface (thin wrappers over the pure layer) ─────────────────


def poll_view(poll_id: int, options: Sequence[str]) -> discord.ui.View:
    """Persistent view of plain vote buttons; dispatch runs through
    :class:`PollVoteButton`, the same live and after a restart (views.py
    pattern)."""
    view = discord.ui.View(timeout=None)
    for idx, option in enumerate(options):
        view.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label=option[:80],
                custom_id=poll_custom_id(poll_id, idx),
            )
        )
    return view


async def dispatch_poll_vote(
    interaction: discord.Interaction, poll_id: int, option_idx: int
) -> None:
    """Handle one vote press: upsert the vote, re-render the card in place."""
    bot = cast("AuraBot", interaction.client)
    row = await asyncio.to_thread(poll_row, bot.conn, poll_id)
    if row is None:
        await interaction.response.send_message("That poll no longer exists.", ephemeral=True)
        return
    if row["closed_at"] is not None:
        await interaction.response.send_message("That poll is closed.", ephemeral=True)
        return
    options: list[str] = json.loads(row["options_json"])
    if option_idx >= len(options):
        await interaction.response.send_message("That option no longer exists.", ephemeral=True)
        return

    user_id = interaction.user.id

    def _vote_and_count() -> tuple[int, ...]:
        record_vote(bot.conn, poll_id, user_id, option_idx, datetime.now(UTC))
        return fold_poll_votes(poll_vote_indices(bot.conn, poll_id), len(options))

    counts = await asyncio.to_thread(_vote_and_count)
    embed = render_poll_embed(
        row["question"],
        options,
        counts,
        poll_id=poll_id,
        author_id=row["author_id"],
        closed=False,
    )
    await interaction.response.edit_message(embed=discord.Embed.from_dict(embed))
    log.info("poll_vote_recorded", poll_id=poll_id, user_id=user_id, option_idx=option_idx)


class PollVoteButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_POLL_TEMPLATE,
):
    """Dynamic handler for every ``aura:poll:*`` vote button, any message age."""

    def __init__(self, custom_id: str) -> None:
        super().__init__(discord.ui.Button(custom_id=custom_id, label="​"))

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[discord.ui.View],
        match: re.Match[str],
        /,
    ) -> PollVoteButton:
        return cls(match.string)

    async def callback(self, interaction: discord.Interaction) -> None:
        parsed = parse_poll_custom_id(self.item.custom_id or "")
        if parsed is None:  # pragma: no cover — template guarantees a parse
            return
        await dispatch_poll_vote(interaction, parsed[0], parsed[1])


# ── the cog ──────────────────────────────────────────────────────────────────


class UtilityCog(commands.Cog):
    """Quality-of-life utilities — thin adapters over the pure helpers above."""

    def __init__(self, bot: AuraBot) -> None:
        self.bot = bot

    # ── /evetime ─────────────────────────────────────────────────────────────

    @app_commands.command(name="evetime", description="Current EVE time (UTC)")
    async def evetime(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(evetime_text(datetime.now(UTC)), ephemeral=True)

    # ── /ask — slash twin of "command override" (GDD §6.6, constraint 10) ────

    @app_commands.command(name="ask", description="Ask CORTANA a question (out-of-band assistant)")
    @app_commands.describe(question="What do you want to know?")
    async def ask(self, interaction: discord.Interaction, question: str) -> None:
        from cortana.chat import ChatCooldownError, ChatError  # lazy: optional feature

        chat = self.bot.chat
        if chat is None:
            # Distinguish "operator chose off" from "enabled but no key
            # loaded" — the latter used to claim the feature was disabled,
            # sending an admin to the wrong config knob mid-outage.
            if getattr(self.bot, "chat_status", "disabled") == "no_key":
                msg = (
                    "The override channel is enabled but no API key is loaded — "
                    "check `/etc/cortana/anthropic` and the service credential, "
                    "then `systemctl reload cortana-brain`."
                )
            else:
                msg = "The override channel is not enabled on this server."
            await interaction.response.send_message(msg, ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        timeout_s = self.bot.holder.current.chat.timeout_s
        try:
            reply = await asyncio.wait_for(
                chat.ask(interaction.user.id, question), timeout=timeout_s
            )
        except ChatCooldownError:
            await interaction.followup.send("Override cooling down — try again shortly.")
            return
        except (ChatError, TimeoutError):
            await interaction.followup.send("Override channel unavailable.")
            return
        await interaction.followup.send(f"💬 {reply}")

    # ── /route ───────────────────────────────────────────────────────────────

    @app_commands.command(name="route", description="Full jump route between two systems")
    @app_commands.describe(from_system="Start system", to_system="Destination system")
    @app_commands.rename(from_system="from", to_system="to")
    @app_commands.autocomplete(from_system=system_autocomplete, to_system=system_autocomplete)
    async def route(
        self, interaction: discord.Interaction, from_system: str, to_system: str
    ) -> None:
        gaz = self.bot.gazetteer
        origin = gaz.by_name(from_system.strip())
        dest = gaz.by_name(to_system.strip())
        missing = [
            name for name, entry in ((from_system, origin), (to_system, dest)) if entry is None
        ]
        if origin is None or dest is None:
            await interaction.response.send_message(
                "Unknown system: " + ", ".join(f"`{n}`" for n in missing),
                ephemeral=True,
            )
            return
        ids = gaz.path(origin.id, dest.id)
        if ids is None:
            text = f"**{origin.name}** and **{dest.name}** are not connected in the gazetteer."
        else:
            names = [gaz.system_name(i) or str(i) for i in ids]
            text = format_route(names)
            if len(text) > _ROUTE_TEXT_MAX:
                jumps = len(ids) - 1
                text = f"**{origin.name}** → … → **{dest.name}** ({jumps} jumps — too long to list)"
        await interaction.response.send_message(text, ephemeral=True)

    # ── /history ─────────────────────────────────────────────────────────────

    @app_commands.command(name="history", description="Recent incidents in a system")
    @app_commands.describe(
        system="System name",
        hours=f"Look-back window in hours (default {HISTORY_DEFAULT_HOURS})",
    )
    @app_commands.autocomplete(system=system_autocomplete)
    async def history(
        self,
        interaction: discord.Interaction,
        system: str,
        hours: app_commands.Range[int, 1, HISTORY_MAX_HOURS] = HISTORY_DEFAULT_HOURS,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        entry = self.bot.gazetteer.by_name(system.strip())
        if entry is None:
            await interaction.response.send_message(
                f"Unknown system `{system}` — pick one from the autocomplete.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        since = datetime.now(UTC) - timedelta(hours=int(hours))
        rows = await asyncio.to_thread(
            recent_incidents, self.bot.conn, interaction.guild_id, entry.id, _iso(since)
        )
        if not rows:
            await interaction.followup.send(
                f"No incidents in **{entry.name}** in the last {hours}h.", ephemeral=True
            )
            return
        lines = [
            history_line(row["type"], row["opened_at"], row["reporters"], row["status"])
            for row in rows
        ]
        embed = discord.Embed(
            title=f"{entry.name} — last {hours}h",
            description="\n".join(lines),
            color=0x3498DB,
            timestamp=datetime.now(UTC),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /remindme ────────────────────────────────────────────────────────────

    @app_commands.command(name="remindme", description="Personal reminder, DMed when due")
    @app_commands.describe(
        duration="How far out, e.g. '45 minutes', '2h' — max 7 days",
        message="What to remind you about",
    )
    async def remindme(
        self,
        interaction: discord.Interaction,
        duration: str,
        message: app_commands.Range[str, 1, 200],
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        now = datetime.now(UTC)
        fires_at = reminder_fires_at(now, duration)
        if fires_at is None:
            await interaction.response.send_message(
                f"Couldn't read a duration up to 7 days from `{duration}` — "
                "try `45 minutes`, `2 hours`, `1h30m`.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = interaction.user.id
        pending = await asyncio.to_thread(pending_reminder_count, self.bot.conn, user_id)
        if pending >= REMINDER_MAX_PENDING:
            await interaction.followup.send(
                f"You already have {pending} pending reminders (cap {REMINDER_MAX_PENDING}) — "
                "wait for some to fire.",
                ephemeral=True,
            )
            return
        reminder_id = await asyncio.to_thread(
            add_reminder, self.bot.conn, interaction.guild_id, user_id, fires_at, str(message)
        )
        log.info("reminder_set", reminder_id=reminder_id, user_id=user_id)
        await interaction.followup.send(
            f"⏰ I'll DM you <t:{int(fires_at.timestamp())}:R>: {message}\n"
            "-# If your DMs are closed the reminder mentions you in the intel channel instead.",
            ephemeral=True,
        )

    # ── /poll ────────────────────────────────────────────────────────────────

    poll = app_commands.Group(name="poll", description="Quick votes with buttons")

    @poll.command(name="create", description="Post a quick vote with up to four options")
    @app_commands.describe(
        question="What are we voting on?",
        option1="First option",
        option2="Second option",
        option3="Third option (optional)",
        option4="Fourth option (optional)",
    )
    async def poll_create(
        self,
        interaction: discord.Interaction,
        question: app_commands.Range[str, 1, 200],
        option1: app_commands.Range[str, 1, 80],
        option2: app_commands.Range[str, 1, 80],
        option3: app_commands.Range[str, 1, 80] | None = None,
        option4: app_commands.Range[str, 1, 80] | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        options = [str(o).strip() for o in (option1, option2, option3, option4) if o is not None]
        options = [o for o in options if o]
        if len(options) < 2:
            await interaction.response.send_message(
                "A poll needs at least two non-empty options.", ephemeral=True
            )
            return
        poll_id = await asyncio.to_thread(
            create_poll,
            self.bot.conn,
            interaction.guild_id,
            interaction.user.id,
            str(question),
            options,
            datetime.now(UTC),
        )
        embed = render_poll_embed(
            str(question),
            options,
            [0] * len(options),
            poll_id=poll_id,
            author_id=interaction.user.id,
            closed=False,
        )
        # The poll card is the interaction response itself — public, in the
        # channel the command was run in.
        await interaction.response.send_message(
            embed=discord.Embed.from_dict(embed), view=poll_view(poll_id, options)
        )
        posted = await interaction.original_response()
        await asyncio.to_thread(
            set_poll_message, self.bot.conn, poll_id, posted.channel.id, posted.id
        )
        log.info("poll_created", poll_id=poll_id, author_id=interaction.user.id)

    @poll.command(name="close", description="Close a poll (author or admin)")
    @app_commands.describe(poll_id="The poll number shown in the card footer")
    async def poll_close(self, interaction: discord.Interaction, poll_id: int) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        row = await asyncio.to_thread(poll_row, self.bot.conn, poll_id)
        if row is None or row["guild_id"] != interaction.guild_id:
            await interaction.response.send_message(f"No poll #{poll_id} here.", ephemeral=True)
            return
        if not self._may_close(interaction, row["author_id"]):
            await interaction.response.send_message(
                "Only the poll author or an admin (Manage Guild / FC role) can close it.",
                ephemeral=True,
            )
            return
        if row["closed_at"] is not None:
            await interaction.response.send_message(
                f"Poll #{poll_id} is already closed.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        def _close_and_count() -> tuple[int, ...]:
            close_poll(self.bot.conn, poll_id, datetime.now(UTC))
            options: list[str] = json.loads(row["options_json"])
            return fold_poll_votes(poll_vote_indices(self.bot.conn, poll_id), len(options))

        counts = await asyncio.to_thread(_close_and_count)
        options = json.loads(row["options_json"])
        embed = render_poll_embed(
            row["question"],
            options,
            counts,
            poll_id=poll_id,
            author_id=row["author_id"],
            closed=True,
        )
        await self._edit_poll_message(row, embed)
        log.info("poll_closed", poll_id=poll_id, user_id=interaction.user.id)
        await interaction.followup.send(f"✅ Poll #{poll_id} closed.", ephemeral=True)

    def _may_close(self, interaction: discord.Interaction, author_id: int) -> bool:
        """Poll author, Manage Guild, or the FC role (admin.py gate)."""
        member = interaction.user
        if member.id == author_id:
            return True
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.manage_guild:
            return True
        fc_role = self.bot.holder.current.discord.roles.fc
        return any(r.id == fc_role for r in member.roles)

    async def _edit_poll_message(self, row: sqlite3.Row, embed: dict[str, object]) -> None:
        """Final in-place edit of the poll card: closed embed, buttons stripped."""
        if row["channel_id"] is None or row["message_id"] is None:
            return
        try:
            channel = self.bot.get_channel(row["channel_id"]) or await self.bot.fetch_channel(
                row["channel_id"]
            )
            if isinstance(channel, discord.TextChannel | discord.Thread):
                await channel.get_partial_message(row["message_id"]).edit(
                    embed=discord.Embed.from_dict(embed), view=None
                )
        except discord.DiscordException:
            log.warning("poll_message_edit_failed", poll_id=row["id"])
