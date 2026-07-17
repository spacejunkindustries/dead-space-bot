"""Persistent component views: incident buttons, ambiguity picks, subscriptions.

GDD §9.3 / §8.3 / §10.2. Buttons survive Brain restarts (GDD §9.3): every
component carries a stable ``custom_id`` and interactions are handled by
:class:`discord.ui.DynamicItem` handlers registered once in
``AuraBot.setup_hook`` — no per-message re-registration, no lost buttons.

The views sent with messages carry *plain* buttons (no callbacks); dispatch
always flows through the dynamic-item handlers so the live path and the
post-restart path are the same code. Every handler is a thin adapter over the
one :class:`~aura.core.incidents.IncidentEngine` (CLAUDE.md constraint 10).

custom_id scheme (INTERFACES.md — authoritative):

    aura:inc:{incident_id}:{otw|watch|no}      responder buttons
    aura:inc:{incident_id}:pick:{system_id}    ambiguity candidate pick
    aura:inc:{incident_id}:fix                 [Wrong — fix] → system modal
    aura:sub:{role_id}                         subscription toggle

The pure helpers (build/parse/layout) live in the first section and are unit
tested without any Discord objects (``tests/test_views.py``).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import discord
import structlog

from aura.types import ButtonSpec, CardRender, Outcome, ResponderState

if TYPE_CHECKING:  # pragma: no cover — import cycle guard (bot.py imports us)
    from aura.dsc.bot import AuraBot

__all__ = [
    "AmbiguityView",
    "ComponentAction",
    "IncidentButton",
    "IncidentView",
    "SubscriptionButton",
    "SubscriptionView",
    "SystemFixModal",
    "incident_button_rows",
    "incident_custom_id",
    "parse_custom_id",
    "subscription_buttons",
    "subscription_custom_id",
    "view_from_card",
]

log = structlog.get_logger(__name__)

# ── pure helpers: custom_id build / parse / layout ───────────────────────────

#: Responder button action tokens → engine states (GDD §9.3).
RESPOND_ACTIONS: dict[str, ResponderState] = {
    "otw": ResponderState.OTW,
    "watch": ResponderState.WATCHING,
    "no": ResponderState.NO,
}

_INCIDENT_TEMPLATE = r"aura:inc:(?P<incident>[0-9]+):(?P<action>otw|watch|no|fix|pick:[0-9]+)"
_SUBSCRIPTION_TEMPLATE = r"aura:sub:(?P<role>[0-9]+)"

_INCIDENT_RE = re.compile(rf"^{_INCIDENT_TEMPLATE}$")
_SUBSCRIPTION_RE = re.compile(rf"^{_SUBSCRIPTION_TEMPLATE}$")

#: Discord allows at most five buttons per action row.
_ROW_WIDTH = 5


@dataclass(frozen=True, slots=True)
class ComponentAction:
    """One parsed component ``custom_id``.

    ``kind`` is ``"respond"`` (with ``state``), ``"pick"`` (with
    ``system_id``), ``"fix"``, or ``"sub"`` (with ``role_id``).
    """

    kind: str
    incident_id: int | None = None
    state: ResponderState | None = None
    system_id: int | None = None
    role_id: int | None = None


def incident_custom_id(incident_id: int, action: str, system_id: int | None = None) -> str:
    """Build an incident-component ``custom_id`` (INTERFACES.md scheme)."""
    if action == "pick":
        if system_id is None:
            raise ValueError("pick action requires a system_id")
        return f"aura:inc:{incident_id}:pick:{system_id}"
    if action not in ("otw", "watch", "no", "fix"):
        raise ValueError(f"unknown incident action {action!r}")
    return f"aura:inc:{incident_id}:{action}"


def subscription_custom_id(role_id: int) -> str:
    """Build a subscription-toggle ``custom_id``."""
    return f"aura:sub:{role_id}"


def parse_custom_id(custom_id: str) -> ComponentAction | None:
    """Parse any AURA component ``custom_id``; None for foreign/invalid ids."""
    m = _INCIDENT_RE.match(custom_id)
    if m is not None:
        incident_id = int(m.group("incident"))
        action = m.group("action")
        if action in RESPOND_ACTIONS:
            return ComponentAction(
                kind="respond", incident_id=incident_id, state=RESPOND_ACTIONS[action]
            )
        if action == "fix":
            return ComponentAction(kind="fix", incident_id=incident_id)
        # pick:{system_id}
        return ComponentAction(
            kind="pick", incident_id=incident_id, system_id=int(action.split(":", 1)[1])
        )
    m = _SUBSCRIPTION_RE.match(custom_id)
    if m is not None:
        return ComponentAction(kind="sub", role_id=int(m.group("role")))
    return None


def incident_button_rows(specs: Sequence[ButtonSpec]) -> tuple[int | None, ...]:
    """Assign action rows for an incident card's buttons.

    Ambiguity buttons (pick/fix) sit on row 0 and the responder buttons on
    row 1 so an uncertain card (up to 4 + 3 buttons) never overflows a row.
    Cards without ambiguity buttons let Discord auto-flow (row ``None``).
    """
    parsed = [parse_custom_id(s.custom_id) for s in specs]
    has_ambiguity = any(a is not None and a.kind in ("pick", "fix") for a in parsed)
    rows: list[int | None] = []
    for action in parsed:
        if not has_ambiguity or action is None:
            rows.append(None)
        elif action.kind in ("pick", "fix"):
            rows.append(0)
        else:
            rows.append(1)
    return tuple(rows)


def subscription_buttons(
    roles: Sequence[tuple[int, str]], member_role_ids: Collection[int]
) -> tuple[ButtonSpec, ...]:
    """Build the toggle-button specs for the ``/subscribe`` picker.

    One button per subscribable role (from the routing rules); roles the
    member already holds render green with a bell. Capped at 25 — a Discord
    view holds at most five rows of five.
    """
    held = set(member_role_ids)
    specs: list[ButtonSpec] = []
    for role_id, name in roles[:25]:
        subscribed = role_id in held
        specs.append(
            ButtonSpec(
                custom_id=subscription_custom_id(role_id),
                label=name,
                style="success" if subscribed else "secondary",
                emoji="🔔" if subscribed else None,
            )
        )
    return tuple(specs)


# ── discord views (thin wrappers over the pure layer) ────────────────────────

_BUTTON_STYLES: dict[str, discord.ButtonStyle] = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}


def _button(spec: ButtonSpec, row: int | None = None) -> discord.ui.Button[discord.ui.View]:
    return discord.ui.Button(
        style=_BUTTON_STYLES.get(spec.style, discord.ButtonStyle.secondary),
        label=spec.label,
        emoji=spec.emoji,
        custom_id=spec.custom_id,
        disabled=spec.disabled,
        row=row,
    )


class IncidentView(discord.ui.View):
    """Persistent view for an incident card's responder buttons (GDD §9.3).

    The buttons are plain (no callbacks): interaction dispatch runs through
    the :class:`IncidentButton` dynamic handler, which also serves clicks on
    cards posted before the last restart.
    """

    def __init__(self, specs: Sequence[ButtonSpec]) -> None:
        super().__init__(timeout=None)
        rows = incident_button_rows(specs)
        for spec, row in zip(specs, rows):
            self.add_item(_button(spec, row))


class AmbiguityView(IncidentView):
    """Incident view carrying MEDIUM-tier candidate picks + [Wrong — fix] (§8.3)."""


class SubscriptionView(discord.ui.View):
    """Role-toggle picker for ``/subscribe`` (GDD §10.2)."""

    def __init__(
        self, roles: Sequence[tuple[int, str]], member_role_ids: Collection[int]
    ) -> None:
        super().__init__(timeout=None)
        for i, spec in enumerate(subscription_buttons(roles, member_role_ids)):
            self.add_item(_button(spec, row=i // _ROW_WIDTH))


def view_from_card(card: CardRender) -> IncidentView | None:
    """Build the view for a rendered card; None when the card has no buttons
    (resolved/cancelled cards drop their components entirely)."""
    if not card.buttons:
        return None
    parsed = [parse_custom_id(s.custom_id) for s in card.buttons]
    if any(a is not None and a.kind in ("pick", "fix") for a in parsed):
        return AmbiguityView(card.buttons)
    return IncidentView(card.buttons)


# ── interaction dispatch (single path, live and post-restart) ────────────────

_RESPOND_CONFIRM: dict[ResponderState, str] = {
    ResponderState.OTW: "🚀 Marked on your way.",
    ResponderState.WATCHING: "👀 Marked watching.",
    ResponderState.NO: "❌ Marked can't respond.",
}


async def dispatch_incident_action(
    interaction: discord.Interaction, action: ComponentAction
) -> None:
    """Handle one incident-card component press via the incident engine."""
    bot = cast("AuraBot", interaction.client)
    assert action.incident_id is not None
    if action.kind == "fix":
        await interaction.response.send_modal(SystemFixModal(bot, action.incident_id))
        return

    await interaction.response.defer()
    if action.kind == "respond":
        assert action.state is not None
        outcome = await bot.engine.respond(action.incident_id, interaction.user.id, action.state)
        if outcome.outcome is Outcome.REJECTED:
            await interaction.followup.send(
                "That incident is already resolved.", ephemeral=True
            )
            return
        # GDD §9.3: the first "On my way" answers audibly into voice.
        if outcome.utterance is not None and interaction.guild_id is not None:
            await bot.speaker.say(interaction.guild_id, outcome.utterance)
        await interaction.followup.send(_RESPOND_CONFIRM[action.state], ephemeral=True)
        return

    # pick:{system_id} — confirm a MEDIUM-tier candidate. The engine updates
    # the card in place; alias learning needs the misheard raw text, which a
    # button press does not carry, so raw_text is empty here (no alias row).
    assert action.system_id is not None
    outcome = await bot.engine.correct_system(
        action.incident_id, interaction.user.id, action.system_id, raw_text=""
    )
    if outcome.outcome is Outcome.REJECTED:
        await interaction.followup.send("That incident no longer exists.", ephemeral=True)
        return
    await interaction.followup.send(
        outcome.utterance or "System confirmed.", ephemeral=True
    )


async def dispatch_subscription_toggle(
    interaction: discord.Interaction, action: ComponentAction
) -> None:
    """Toggle one subscription role on the pressing member (GDD §10.2)."""
    bot = cast("AuraBot", interaction.client)
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await interaction.response.send_message("Guild only.", ephemeral=True)
        return
    assert action.role_id is not None
    if action.role_id not in bot.routing_role_ids():
        await interaction.response.send_message(
            "That role is no longer a subscription role — run `/subscribe` again.",
            ephemeral=True,
        )
        return
    role = guild.get_role(action.role_id)
    if role is None:
        await interaction.response.send_message(
            "That role no longer exists on this server.", ephemeral=True
        )
        return

    subscribed = role in member.roles
    try:
        if subscribed:
            await member.remove_roles(role, reason="AURA /subscribe toggle")
        else:
            await member.add_roles(role, reason="AURA /subscribe toggle")
    except discord.Forbidden:
        await interaction.response.send_message(
            f"I can't manage **{role.name}** — my role must sit above it and I need "
            "the Manage Roles permission (GDD §17.4). Ask an admin to reorder the roles.",
            ephemeral=True,
        )
        return

    new_role_ids = {r.id for r in member.roles} ^ {role.id}
    view = SubscriptionView(bot.subscription_role_pairs(guild), new_role_ids)
    verb = "Unsubscribed from" if subscribed else "Subscribed to"
    await interaction.response.edit_message(
        content=f"{verb} **{role.name}**. Toggle your subscriptions:", view=view
    )
    log.info(
        "subscription_toggled",
        user_id=member.id,
        role_id=role.id,
        subscribed=not subscribed,
    )


# ── dynamic items: the restart-proof handlers (GDD §9.3) ─────────────────────


class IncidentButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_INCIDENT_TEMPLATE,
):
    """Dynamic handler for every ``aura:inc:*`` component, any message age."""

    def __init__(self, custom_id: str) -> None:
        super().__init__(discord.ui.Button(custom_id=custom_id, label="​"))

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[discord.ui.View],
        match: re.Match[str],
        /,
    ) -> IncidentButton:
        return cls(match.string)

    async def callback(self, interaction: discord.Interaction) -> None:
        action = parse_custom_id(self.item.custom_id or "")
        if action is None:  # pragma: no cover — template guarantees a parse
            return
        await dispatch_incident_action(interaction, action)


class SubscriptionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_SUBSCRIPTION_TEMPLATE,
):
    """Dynamic handler for every ``aura:sub:*`` toggle button."""

    def __init__(self, custom_id: str) -> None:
        super().__init__(discord.ui.Button(custom_id=custom_id, label="​"))

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[discord.ui.View],
        match: re.Match[str],
        /,
    ) -> SubscriptionButton:
        return cls(match.string)

    async def callback(self, interaction: discord.Interaction) -> None:
        action = parse_custom_id(self.item.custom_id or "")
        if action is None:  # pragma: no cover — template guarantees a parse
            return
        await dispatch_subscription_toggle(interaction, action)


# ── [Wrong — fix] system entry (GDD §8.5) ────────────────────────────────────


class SystemFixModal(discord.ui.Modal, title="Correct the system"):
    """Typed correction for a mis-resolved incident.

    A select can hold 25 options and the gazetteer holds hundreds, so the fix
    path takes typed input matched against the gazetteer instead.
    """

    system: discord.ui.TextInput[SystemFixModal] = discord.ui.TextInput(
        label="System name", placeholder="e.g. Otanuomi", max_length=64
    )

    def __init__(self, bot: AuraBot, incident_id: int) -> None:
        super().__init__()
        self._bot = bot
        self._incident_id = incident_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = str(self.system.value).strip()
        entry = self._bot.gazetteer.by_name(text)
        if entry is None:
            lowered = text.lower()
            matches = [
                s for s in self._bot.gazetteer.systems if s.name.lower().startswith(lowered)
            ]
            if len(matches) == 1:
                entry = matches[0]
            elif matches:
                names = ", ".join(s.name for s in matches[:5])
                await interaction.followup.send(
                    f"Ambiguous — did you mean one of: {names}?", ephemeral=True
                )
                return
            else:
                await interaction.followup.send(
                    f"Unknown system `{text}` — it isn't in the operational gazetteer.",
                    ephemeral=True,
                )
                return
        outcome = await self._bot.engine.correct_system(
            self._incident_id, interaction.user.id, entry.id, raw_text=""
        )
        if outcome.outcome is Outcome.REJECTED:
            await interaction.followup.send("That incident no longer exists.", ephemeral=True)
            return
        await interaction.followup.send(
            outcome.utterance or f"Corrected to {entry.name}.", ephemeral=True
        )
