"""Interactive /help: what CORTANA is, every command, and the admin runbook.

The slash twin of the voice ``HELP`` intent (GDD §6.1) — voice gets the spoken
hint *"Check help in Discord."* through the same ``IncidentEngine.report``
entry point (constraint 10); the actual manual lives here, in Discord, where
there is room for it.

All content lives in :data:`HELP_TOPICS`, a plain topic → :class:`HelpTopic`
table, so ``tests/test_help_cog.py`` can assert that every registered app
command in the other cogs appears somewhere in the help text — the mechanism
that keeps this page honest as commands are added.

The topic picker follows the views.py pattern: a plain select carrying a
stable ``custom_id``, dispatched by a persistent ``DynamicItem`` handler so
the live path and the post-restart path are the same code.

custom_id scheme (extends the views.py registry):

    aura:help:{topic}    help select menu ("menu" for the picker itself;
                         topic keys ride in the option values)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
import structlog
from discord import app_commands
from discord.ext import commands

from cortana import tts
from cortana.dsc.cogs.admin import _is_admin
from cortana.types import Intent, ParsedCommand

if TYPE_CHECKING:  # pragma: no cover
    from cortana.dsc.bot import AuraBot

__all__ = [
    "HELP_TOPICS",
    "HelpCog",
    "HelpTopic",
    "HelpTopicSelect",
    "help_custom_id",
    "main_embed",
    "parse_help_custom_id",
    "topic_embed",
    "visible_topics",
]

log = structlog.get_logger(__name__)

# ── content (data, not handler strings — the coverage test reads this) ───────


@dataclass(frozen=True, slots=True)
class HelpTopic:
    """One help page: select-option label + an embed's worth of content."""

    key: str
    label: str
    emoji: str
    title: str
    description: str
    fields: tuple[tuple[str, str], ...]
    admin_only: bool = False


HELP_TOPICS: dict[str, HelpTopic] = {
    "voice": HelpTopic(
        key="voice",
        label="Voice commands (quick list)",
        emoji="🎙️",
        title="🎙️ Voice commands — the quick list",
        description=(
            "Say the wake phrase, wait for the chirp, then speak. One page, "
            "every phrase that works. Colour codes and system names can ride "
            "anywhere in the sentence."
        ),
        fields=(
            (
                "Report",
                "• *“hostiles / reds / neuts <system>”* — sighting 🟠\n"
                "• *“under attack / tackled / point on me <system>”* — 🔴\n"
                "• *“need help / need backup <system>”* — 🔴\n"
                "• *“gate camp <system>”* — 🟠\n"
                "• *“report … end report”* — freeform intel relay\n"
                "• *“code red / orange / yellow”* — alone opens a two-step "
                "report; inline it colours the report it rides in",
            ),
            (
                "Manage",
                "• *“clear <system>”* — resolve the card\n"
                "• *“cancel”* — retract your last report (30s)\n"
                "• *“update chase <system>”* — chase mode retarget; "
                "*“chase clear”* ends it\n"
                "• *“status”* — active incidents readback\n"
                "• *“timer <system> <duration>”* — structure timer\n"
                "• *“form up …”* — rally card",
            ),
            (
                "Personal",
                "• *“ping me for <type> in <system>”* — personal ping\n"
                "• *“stop pinging me”* — clear personal pings\n"
                "• *“this is <callsign>”* — set your callsign\n"
                "• *“command override <question>”* — ask the assistant "
                "(needs the override channel enabled)\n"
                "• *“what can you do”* / `/capabilities` — CORTANA speaks a "
                "quick summary of her features (for new pilots)\n"
                "• *“help”* — speaks a hint and posts this guide",
            ),
            (
                "Fun",
                "• *“tell me a fact”* — a random true fact, spoken; name a "
                "topic: *“space fact”*, *“fact about animals”*\n"
                "• *“insult this guy”* / *“roast Dave”* — a roast, spoken\n"
                "Voice fun stays in voice — nothing posts to a channel.",
            ),
            (
                "How she behaves",
                "Misheard? She says *“Say again?”* and listens without a new "
                "wake word — twice max, then *“Standing down”* means wake her "
                "again. When she's not sure of the system she asks *“Heard X — "
                "confirm?”*: say **yes** to post, **no** to re-say the system — "
                "or say nothing and it posts anyway (a distress call is never "
                "dropped). Say *“end transmission”*, *“disregard”*, *“never "
                "mind”* or *“stand down”* at any point to close her out "
                "immediately.",
            ),
        ),
    ),
    "reporting": HelpTopic(
        key="reporting",
        label="Reporting intel",
        emoji="📡",
        title="📡 Reporting intel",
        description=(
            "Speak after the wake phrase, or use the slash twin — both hit the "
            "same engine, so everything below works even if voice is down."
        ),
        fields=(
            (
                "Voice → slash twins",
                "• *“hostiles / reds / neuts <system>”* → `/hostiles` — 🟠 medium, "
                "pings subscribed roles\n"
                "• *“under attack / tackled / point on me <system>”* → `/under-attack` — "
                "🔴 high, roles + `@here`\n"
                "• *“need help / need backup <system>”* → `/help-me` — 🔴 high, "
                "roles + `@here`\n"
                "• *“gate camp <system>”* → `/camp` — 🟠 medium, pings subscribed roles\n"
                "• *“clear <system>”* → `/clear` — resolves the card, no mention\n"
                "• *“update chase <system>”* → `/chase` — chase mode: retargets "
                "your live card as the target moves, no new post\n"
                "• *“status”* → `/status` — active incidents, spoken/ephemeral reply\n"
                "• *“cancel”* → `/cancel` — retracts your last report (30s window)\n"
                "• anything else you say (freeform intel) → `/relay` — posts it "
                "verbatim as an intel-relay card; relays never `@here`",
            ),
            (
                "Colour codes stack inline",
                "Say the threat colour anywhere in the report and it rides along: "
                "*“I'm tackled, **code red**, in system UMI, over”* → one 🔴 CODE RED "
                "card for UMI, and CORTANA reads it back: *“Tackled UMI, code red, "
                "posted.”* Codes: **code red** 🔴 / **code orange** 🟠 / "
                "**code yellow** 🟡. A standalone *“code orange”* opens a "
                "wake-free window for the report that follows.",
            ),
            (
                "Who gets pinged",
                "Roles subscribed to that type + system (see Subscriptions), plus "
                "personal `/pingme` subscribers. `@here` fires **only** for "
                "under-attack and help-me. Triggering mentions needs the @Pilot role. "
                "Anything after the system is kept verbatim on the card "
                "(*“three battleships”*); *“miners only”*, *“defense only”* or "
                "*“all hands”* narrows or widens the audience.",
            ),
            (
                "When CORTANA mishears",
                "**A distress call always posts** — an unknown or misheard system "
                "goes on the card verbatim instead of being dropped, and CORTANA "
                "reads the report back so you hear exactly what landed.\n"
                "• Fairly sure: the card posts flagged ❓ *unconfirmed* with candidate "
                "buttons and **[Wrong — fix]** — tap the right system; corrections "
                "are learned.\n"
                "• Wrong on the card? Say *“cancel”* (30s) and re-report, or press "
                "**[Wrong — fix]**.",
            ),
        ),
    ),
    "responding": HelpTopic(
        key="responding",
        label="Responding to alerts",
        emoji="🚀",
        title="🚀 Responding to alerts",
        description=(
            "Every incident card is live — one message per incident, edited in "
            "place. Repeat reports fold into it (*“reported by 5”*), never re-ping."
        ),
        fields=(
            (
                "Card buttons",
                "🚀 **On my way** — you're burning there\n"
                "👀 **Watching** — eyes on, not committing\n"
                "❌ **Can't respond** — counted out\n"
                "You can switch by pressing another button.",
            ),
            (
                "The spoken count",
                "On the first **On my way** (and each new responder) CORTANA speaks the "
                "running count into voice — *“Two responding to Otanuomi.”* — so the "
                "pilot in trouble hears help coming without touching Discord.",
            ),
            (
                "Roll call",
                "`/rollcall` — who's in the watched voice channels (callsigns first), "
                "per-role subscriber counts, and who's responding to what.",
            ),
        ),
    ),
    "subscriptions": HelpTopic(
        key="subscriptions",
        label="Subscriptions & pings",
        emoji="🔔",
        title="🔔 Subscriptions & personal pings",
        description=(
            "Two layers: **role subscriptions** (shared audiences an admin defines "
            "in routing rules) and **personal pings** (just you, self-service)."
        ),
        fields=(
            (
                "Role subscriptions — standing duty",
                "`/subscribe` — toggle yourself into alert roles (e.g. @Home-Defense). "
                "Use these for the alerts your role in the corp means you should see. "
                "Roles can carry **quiet hours** — no mentions during the window the "
                "admin set. `/mysubs` shows your roles and personal pings.",
            ),
            (
                "Personal pings — just you",
                "*“Hey Cortana, ping me for gate camps in Otanuomi”* or "
                "`/pingme type [system]` — a direct @mention when a matching incident "
                "posts. Use it for a specific interest (your mining hole, your route) "
                "without joining a role. No system = everywhere; max 10.\n"
                "`/mypings` lists them; `/pingme-clear [index]` removes one or all — "
                "by voice: *“stop pinging me”*. Personal pings never cause `@here`.",
            ),
        ),
    ),
    "identity": HelpTopic(
        key="identity",
        label="Callsigns",
        emoji="🪪",
        title="🪪 Callsigns",
        description=(
            "A display name CORTANA uses for you, keyed on your **Discord account** — "
            "identity comes from Discord's user id on each utterance, never from a "
            "voiceprint or anything derived from audio."
        ),
        fields=(
            (
                "Commands",
                "• *“register <callsign>”* / *“call me <callsign>”* → "
                "`/register callsign` — the typed form stores it exactly as typed, "
                "which is also how you fix a misheard spelling\n"
                "• *“who am I”* → `/whoami` — CORTANA answers *“You are Space Junkie.”*\n"
                "• *“unregister”* / *“forget me”* → `/unregister` — deletes it",
            ),
            (
                "Where it shows up",
                "On incident cards (**Reported by** names a sole registered reporter) "
                "and in `/rollcall`'s voice listing. Max 32 characters; markdown and "
                "@mentions are stripped so a callsign can never smuggle a ping.",
            ),
        ),
    ),
    "ops": HelpTopic(
        key="ops",
        label="Fleet ops & utilities",
        emoji="🛠️",
        title="🛠️ Fleet ops & utilities",
        description="Timers and form-ups also work by voice; the rest are slash-only.",
        fields=(
            (
                "Scheduling",
                "• `/timer Kisogo 4 hours armor timer` — mention ahead of a structure "
                "timer (*“Hey Cortana, timer Kisogo four hours”*)\n"
                "• `/formup Otanuomi 15 minutes kitchen sink` — op card with RSVP "
                "buttons (*“Hey Cortana, form up Otanuomi fifteen minutes”*)\n"
                "• `/remindme 45 minutes check the pos` — personal DM reminder",
            ),
            (
                "Navigation & lookups",
                "• `/jumps from:Otanuomi to:Jita` — jump distance\n"
                "• `/route from:Otanuomi to:Jita` — the full shortest path\n"
                "• `/history system:Kisogo hours:24` — recent incidents there\n"
                "• `/evetime` — EVE (UTC) time with your local equivalent",
            ),
            (
                "Quick votes",
                "• `/poll create question option1 option2 …` — up to 4 options, live "
                "counts edited in place\n"
                "• `/poll close id` — close it (author or admin)",
            ),
            (
                "Ask CORTANA (out-of-band)",
                "• `/ask question` — the assistant channel; by voice it's "
                '*"command override, …"* (e.g. *"command override, what\'s the '
                'weather in Chicago?"*). Separate from intel — CORTANA never uses '
                "it to interpret reports.\n"
                "• `/chat message` — freestyle back-and-forth when the fleet is "
                "idle (opt-in); by voice she just talks back, no wake word between "
                "turns. Speech-only — never posts a card, never pings.",
            ),
        ),
    ),
    "fun": HelpTopic(
        key="fun",
        label="Facts & roasts",
        emoji="🎲",
        title="🎲 Facts & roasts",
        description=(
            "**1,692 true facts** across 16 categories and a **293-line roast "
            "generator** — bundled, offline, entertainment kept strictly off "
            "the intel path: own cooldowns, and alert speech always talks "
            "over it."
        ),
        fields=(
            (
                "Facts",
                "• *“hey cortana, tell me a fact”* — speaks one into voice; add "
                "a topic: *“space fact”*, *“fact about the ocean”*, *“eve fact”*\n"
                "• `/fact [category]` — posts one in the channel you ran it in\n"
                "Categories: space, physics, history, military, tech, animals, "
                "the human body, the ocean, gaming, math, geography, "
                "engineering, language, food, science, New Eden lore. Every "
                "fact was written and accuracy-checked before shipping — no "
                "myths, no internet lookups mid-fight. No repeats until the "
                "whole deck has been dealt.",
            ),
            (
                "Roasts",
                "• *“hey cortana, insult this guy”* / *“roast Dave”* — spoken roast\n"
                "• `/insult [target]` — posts in-channel; naming a target renders "
                "their @ but **never pings** them\n"
                "Three flavours (sailor-mouth, clean burns, capsuleer-themed). "
                "All in good fun: friendly-fire humor about piloting and gaming "
                "skill only — no slurs, ever. `fun.insults_spicy: false` switches "
                "to the clean-burn pool.",
            ),
            (
                "Rules of engagement",
                "Voice requests answer in voice only — nothing is posted. Slash "
                "replies stay in the channel they were invoked in. Per-guild "
                "cooldowns (`fun.fact_cooldown_s` / `fun.insult_cooldown_s`) keep "
                "the comedy from crowding comms during a fight.",
            ),
        ),
    ),
    "privacy": HelpTopic(
        key="privacy",
        label="Privacy & consent",
        emoji="🔒",
        title="🔒 Privacy & consent",
        description=(
            "**CORTANA records nothing.** Audio lives only in a RAM buffer overwritten "
            "every 1.5 seconds — never written to disk, freed the instant "
            "speech-to-text returns."
        ),
        fields=(
            (
                "What is (and isn't) kept",
                "Only the **transcript of triggered commands** is stored. Speech "
                "without the wake phrase never reaches the recogniser at all — it is "
                "never transcribed, let alone kept.",
            ),
            (
                "Your controls",
                "• `/optout` — your audio is dropped **inside the Rust voice process, "
                "before any processing** and before it ever crosses to the rest of "
                "the bot. An actual drop, not a filter. Toggle again to opt back in.\n"
                "• `/mute-voice` — CORTANA stops speaking replies to *you*; everything "
                "else still works.\n"
                "CORTANA also announces itself in channel text every time it joins voice.",
            ),
        ),
    ),
    "admin": HelpTopic(
        key="admin",
        label="Admin & setup",
        emoji="⚙️",
        title="⚙️ Admin & setup",
        description="Gated on **Manage Guild** or the configured FC role.",
        admin_only=True,
        fields=(
            (
                "Commands",
                "• `/routing list|reload` — inspect / reload the subscription rules\n"
                "• `/gazetteer info|reload|prune` — the system-name set (kept small "
                "on purpose: the corp's operational area, not all of New Eden)\n"
                "• `/fleetmode on|off` — restrict voice triggering to the FC role "
                "during structured ops; slash stays open to everyone\n"
                "• `/health` — pipeline status, STT confidence, incident counts "
                "(hourly self-reports also land in #bot-health)\n"
                "• `/botstatus` · `/doctor` · `/reload` — one-screen ops status, "
                "offline preflight checks, and the SIGHUP-equivalent config reload\n"
                "• `/restart` — restart the brain process remotely (back in ~15s; "
                "voice connection survives) — the kick for a wedged bot\n"
                "• `/clearall` — resolve EVERY active incident card at once (the "
                "board-wipe after an op or a test session)",
            ),
            (
                "Config files (hot-reload: SIGHUP or the reload commands)",
                "• `/etc/cortana/cortana.yaml` — **all Discord IDs live here** (guild, "
                "#intel-alerts / #intel-live / #bot-health channel ids, @Pilot and FC "
                "role ids, watched voice channels) plus thresholds\n"
                "• `/etc/cortana/routing.yaml` — which role is mentioned for which "
                "incident types where; quiet hours per role\n"
                "• `/etc/cortana/gazetteer.yaml` — region allowlist / jump radius "
                "scoping the system set",
            ),
            (
                "First-time setup",
                "1. Create the channels (#intel-alerts, #intel-live, #bot-health) and "
                "roles (@Pilot, FC, alert roles)\n"
                "2. Put every ID in `cortana.yaml` (`discord:` section)\n"
                "3. Provide the bot token via systemd `LoadCredential=` — never in "
                "config or env\n"
                "4. Seed the gazetteer scope in `gazetteer.yaml`, then "
                "`/gazetteer reload`\n"
                "5. Write `routing.yaml`, then `/routing reload` — `/subscribe` now "
                "offers those roles",
            ),
        ),
    ),
}


# ── main page ────────────────────────────────────────────────────────────────

_MAIN_DESCRIPTION = (
    "CORTANA sits in your corp's voice channel and turns a spoken report into a "
    "live incident card, role pings, and a spoken confirmation — in about a "
    "second and a half. Every voice command has a slash twin, so everything "
    "keeps working from chat alone.\n\n"
    "**Say:** *“Hey Cortana, hostiles Otanuomi, three battleships”*\n"
    "**CORTANA answers in voice:** *“Hostiles Otanuomi, pinged.”* — and the card "
    "is already posted.\n\n"
    "Pick a topic below, or jump straight there with `/help topic:…`."
)

_MAIN_FIELDS: tuple[tuple[str, str], ...] = (
    (
        "Channels",
        "• **#intel-alerts** — incidents that mention a role: the loud feed\n"
        "• **#intel-live** — every incident, mention-free: the quiet feed\n"
        "• **#bot-health** — CORTANA's hourly self-reports, for the admins",
    ),
    (
        "Privacy",
        "Audio is **never recorded** — RAM only, overwritten every 1.5s; only "
        "command transcripts are kept. `/optout` removes your audio entirely.",
    ),
)


def main_embed() -> dict[str, object]:
    """The /help front page as an embed dict (``discord.Embed.from_dict`` shape)."""
    return {
        "title": "CORTANA — voice-activated fleet intel",
        "description": _MAIN_DESCRIPTION,
        "color": 0x3498DB,
        "fields": [{"name": n, "value": v, "inline": False} for n, v in _MAIN_FIELDS],
        "footer": {"text": "CORTANA · /help"},
    }


def topic_embed(key: str) -> dict[str, object]:
    """One topic page as an embed dict; raises ``KeyError`` for unknown topics."""
    topic = HELP_TOPICS[key]
    return {
        "title": topic.title,
        "description": topic.description,
        "color": 0x3498DB,
        "fields": [{"name": n, "value": v, "inline": False} for n, v in topic.fields],
        "footer": {"text": "CORTANA · /help"},
    }


def visible_topics(*, include_admin: bool) -> tuple[HelpTopic, ...]:
    """Topics for the select menu — the admin page only for admins."""
    return tuple(t for t in HELP_TOPICS.values() if include_admin or not t.admin_only)


# ── custom_id build / parse (views.py registry: aura:help:{topic}) ───────────

_HELP_TEMPLATE = r"aura:help:(?P<topic>[a-z]+)"
_HELP_RE = re.compile(rf"^{_HELP_TEMPLATE}$")

#: The picker's own slot in the aura:help:{topic} scheme; the chosen topic key
#: travels in the select option value.
_MENU_TOPIC = "menu"


def help_custom_id(topic: str) -> str:
    """Build an ``aura:help:{topic}`` custom_id."""
    if not re.fullmatch(r"[a-z]+", topic):
        raise ValueError(f"invalid help topic token {topic!r}")
    return f"aura:help:{topic}"


def parse_help_custom_id(custom_id: str) -> str | None:
    """Parse an ``aura:help:{topic}`` custom_id; None for foreign/invalid ids."""
    m = _HELP_RE.match(custom_id)
    return m.group("topic") if m is not None else None


# ── discord surface (thin wrappers over the content table) ───────────────────


def _menu_select(topics: tuple[HelpTopic, ...]) -> discord.ui.Select[discord.ui.View]:
    """A plain select (no callback — dispatch runs through the dynamic item)."""
    return discord.ui.Select(
        custom_id=help_custom_id(_MENU_TOPIC),
        placeholder="Pick a help topic…",
        options=[discord.SelectOption(label=t.label, value=t.key, emoji=t.emoji) for t in topics],
    )


class HelpMenuView(discord.ui.View):
    """Persistent view carrying the topic picker (views.py pattern)."""

    def __init__(self, topics: tuple[HelpTopic, ...]) -> None:
        super().__init__(timeout=None)
        self.add_item(_menu_select(topics))


async def dispatch_help_topic(interaction: discord.Interaction, topic_key: str) -> None:
    """Send one topic page, ephemeral. Re-gates the admin topic on dispatch."""
    topic = HELP_TOPICS.get(topic_key)
    if topic is None:
        await interaction.response.send_message(
            "That help topic no longer exists — run `/help` again.", ephemeral=True
        )
        return
    if topic.admin_only and not _is_admin(interaction):
        await interaction.response.send_message(
            "Admin only — needs Manage Guild or the FC role.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        embed=discord.Embed.from_dict(topic_embed(topic.key)), ephemeral=True
    )


class HelpTopicSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=_HELP_TEMPLATE,
):
    """Dynamic handler for the ``aura:help:*`` select, any message age."""

    def __init__(self, custom_id: str) -> None:
        super().__init__(
            discord.ui.Select(
                custom_id=custom_id,
                options=[discord.SelectOption(label="​", value="​")],
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[discord.ui.View],
        match: re.Match[str],
        /,
    ) -> HelpTopicSelect:
        return cls(match.string)

    async def callback(self, interaction: discord.Interaction) -> None:
        from cortana.dsc.views import run_component_action

        values = self.item.values
        if not values:  # pragma: no cover — a select interaction always has one
            return
        await run_component_action(
            interaction, "help-menu", dispatch_help_topic(interaction, values[0])
        )


# ── the cog ──────────────────────────────────────────────────────────────────


class HelpCog(commands.Cog):
    """/help — the slash twin of the voice HELP intent (constraint 10)."""

    def __init__(self, bot: AuraBot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="How CORTANA works: commands, voice, privacy")
    @app_commands.describe(topic="Jump straight to one help page")
    @app_commands.choices(
        topic=[app_commands.Choice(name=t.label, value=t.key) for t in HELP_TOPICS.values()]
    )
    async def help(self, interaction: discord.Interaction, topic: str | None = None) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        # Same engine entry point as voice "help": writes the command_log row;
        # the outcome posts nothing (GDD §6.1 — spoken reply only). The embed
        # below is the reply, so the outcome's utterance is not rendered.
        raw = f"/help {topic}" if topic else "/help"
        parsed = ParsedCommand(
            intent=Intent.HELP, system_text=None, group_alias=None, detail=topic, raw=raw
        )
        await self.bot.engine.report(interaction.guild_id, interaction.user.id, parsed, None)

        if topic is not None:
            await dispatch_help_topic(interaction, topic)
            return

        await interaction.response.send_message(
            embed=discord.Embed.from_dict(main_embed()),
            view=HelpMenuView(visible_topics(include_admin=_is_admin(interaction))),
            ephemeral=True,
        )

    @app_commands.command(name="capabilities", description="A quick summary of what CORTANA can do")
    async def capabilities(self, interaction: discord.Interaction) -> None:
        """Slash twin of the voice CAPABILITIES intent (constraint 10): sends the
        SAME fixed summary CORTANA speaks (``tts.capabilities()``), mention-free
        (constraint 11). Points new players at ``/help`` for the full manual."""
        await interaction.response.send_message(
            f"🛰️ {tts.capabilities()}\n\nRun `/help` for the full command list.",
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=True,
        )
