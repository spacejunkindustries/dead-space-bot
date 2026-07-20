"""Restart-proof Discord UI for the killboard (GDD §8, §10).

The only interactive surface the killboard owns is leaderboard pagination: a
``/ranking`` reply carries Prev/Next buttons that page through the windowed
leaderboard. Those buttons must survive a Brain restart — a leaderboard posted
before a redeploy has to keep paging afterwards — so dispatch runs through a
:class:`discord.ui.DynamicItem` handler registered once (via the module's
``dynamic_items()``), exactly like the house pattern in
``cortana/dsc/views.py``. There is no per-message callback state to lose.

The design keeps the *pure* layer sharply separated from Discord:

* :func:`build_id` / :func:`parse_id` are the entire ``custom_id`` contract —
  pure string ↔ tuple, unit-testable with no Discord objects.
* :class:`RankingView` assembles the button row for a page; pure given its
  arguments.
* :class:`RankingPageButton` is the restart-proof dispatcher. Its callback owns
  no ranking logic: it parses the id, finds the cog that can render a page
  (:class:`RankingPageSource`), asks it, and swaps the embed + a freshly built
  view. All the store access, window math, and ``to_thread`` wrapping stay in
  that cog (``killboard/commands.py``), so this module never touches sqlite.

custom_id scheme (namespace ``aura:kb:*``)::

    aura:kb:rank:{metric}:{period}:{page}

``metric`` is one of :data:`~killboard.rankings.METRICS`, ``period`` one of
:data:`~killboard.rankings.PERIODS`, ``page`` a 0-based page index.

Every message this module edits passes ``allowed_mentions=AllowedMentions.
none()`` — the killboard is informational and never pings (CLAUDE.md
constraint 11). Callbacks are defensive: a bad id, a missing source, or a
render failure answers the pilot and is swallowed by the shared error boundary,
never raised into the gateway.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple, Protocol, runtime_checkable

import discord
import structlog

# The house error boundary — reused so the killboard's component dispatch fails
# exactly like CORTANA's (ephemeral reply + alarm accounting), never re-raising.
from cortana.dsc.views import answer_interaction_error, run_component_action
from killboard.rankings import METRICS, PERIODS

if TYPE_CHECKING:
    from collections.abc import Mapping

log = structlog.get_logger(__name__)

__all__ = [
    "RANKING_NAMESPACE",
    "RankingPageButton",
    "RankingPageId",
    "RankingPageSource",
    "RankingRender",
    "RankingView",
    "build_id",
    "parse_id",
]

#: The custom_id namespace every killboard component lives under.
RANKING_NAMESPACE = "aura:kb:rank"

# Alternations are derived from the canonical vocabularies so the template can
# never drift from what the rankings module actually supports.
_METRIC_ALT = "|".join(re.escape(m) for m in METRICS)
_PERIOD_ALT = "|".join(re.escape(p) for p in PERIODS)

#: discord.py matches this (unanchored) against a component's custom_id to route
#: it to :class:`RankingPageButton`. Mirrors the ``cortana/dsc/views.py`` idiom.
_RANKING_TEMPLATE = (
    rf"aura:kb:rank:(?P<metric>{_METRIC_ALT}):(?P<period>{_PERIOD_ALT}):(?P<page>[0-9]+)"
)
_RANKING_RE = re.compile(rf"^{_RANKING_TEMPLATE}$")


# ── pure custom_id contract (unit-testable, no Discord) ──────────────────────


class RankingPageId(NamedTuple):
    """A parsed ranking-page ``custom_id``: metric, period, and 0-based page."""

    metric: str
    period: str
    page: int


def build_id(metric: str, period: str, page: int) -> str:
    """Build a ranking-page ``custom_id`` (namespace :data:`RANKING_NAMESPACE`).

    ``metric`` must be one of :data:`~killboard.rankings.METRICS`, ``period`` one
    of :data:`~killboard.rankings.PERIODS`, and ``page`` a non-negative index.
    Invalid arguments raise :class:`ValueError` — an unroutable id is a
    programming error, caught here rather than producing a dead button.
    """
    if metric not in METRICS:
        raise ValueError(f"unknown ranking metric: {metric!r}")
    if period not in PERIODS:
        raise ValueError(f"unknown ranking period: {period!r}")
    if page < 0:
        raise ValueError(f"page must be non-negative, got {page}")
    return f"{RANKING_NAMESPACE}:{metric}:{period}:{page}"


def parse_id(custom_id: str) -> RankingPageId | None:
    """Parse a ranking-page ``custom_id``; ``None`` for foreign/invalid ids.

    Pure and total — never raises. The template constrains metric and period to
    the known vocabularies, so any match is already valid; anything else (a
    CORTANA incident id, a malformed string) returns ``None``.
    """
    m = _RANKING_RE.match(custom_id)
    if m is None:
        return None
    return RankingPageId(
        metric=m.group("metric"),
        period=m.group("period"),
        page=int(m.group("page")),
    )


# ── the render contract the paginator dispatches to ──────────────────────────


@dataclass(frozen=True, slots=True)
class RankingRender:
    """One rendered leaderboard page: the embed to show and whether a next page
    exists. ``has_next`` drives whether the Next button is enabled — the source
    knows the total row count for the window, the view does not."""

    embed: discord.Embed
    has_next: bool


@runtime_checkable
class RankingPageSource(Protocol):
    """What :class:`RankingPageButton` needs to service a page turn.

    The killboard command cog (``killboard/commands.py``) implements this: it
    owns the store, so it does the window math and the blocking sqlite read
    (wrapped in ``asyncio.to_thread``) and returns a ready
    :class:`RankingRender`. Keeping the contract this narrow is what lets the
    button stay pure of any store or config access.
    """

    async def render_ranking_page(self, metric: str, period: str, page: int) -> RankingRender:
        """Render leaderboard ``metric``/``period`` at 0-based ``page``."""
        ...


def _find_source(client: discord.Client) -> RankingPageSource | None:
    """Locate the cog able to render a ranking page, or ``None``.

    Duck-typed on the :class:`RankingPageSource` protocol rather than a hard cog
    name, so the button keeps working regardless of what the command cog calls
    itself. ``None`` when the killboard is disabled or mid-reload — the callback
    degrades to an ephemeral notice rather than raising.
    """
    cogs: Mapping[str, object] = getattr(client, "cogs", {})
    for cog in cogs.values():
        if isinstance(cog, RankingPageSource):
            return cog
    return None


# ── the paginated view (pure given its arguments) ────────────────────────────

#: Discord ints for the pager buttons.
_PAGER_STYLE = discord.ButtonStyle.secondary


class RankingView(discord.ui.View):
    """The Prev / page-indicator / Next button row for one leaderboard page.

    Timeout-less and stateless: every button carries a stable ``custom_id`` and
    dispatch flows through :class:`RankingPageButton`, so the row is identical
    whether it was just posted or is being rebuilt after a restart. ``has_next``
    is supplied by the caller (it knows the window's total row count); Prev is
    disabled on the first page.
    """

    def __init__(self, metric: str, period: str, page: int, *, has_next: bool) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                style=_PAGER_STYLE,
                emoji="◀",  # ◀
                custom_id=build_id(metric, period, max(page - 1, 0)),
                disabled=page <= 0,
            )
        )
        # Centre indicator: disabled, so it never dispatches; its id is outside
        # the rank template and is harmless if it somehow reaches the gateway.
        self.add_item(
            discord.ui.Button(
                style=_PAGER_STYLE,
                label=f"Page {page + 1}",
                custom_id=f"aura:kb:page:{page}",
                disabled=True,
            )
        )
        self.add_item(
            discord.ui.Button(
                style=_PAGER_STYLE,
                emoji="▶",  # ▶
                custom_id=build_id(metric, period, page + 1),
                disabled=not has_next,
            )
        )


# ── the restart-proof dispatcher ─────────────────────────────────────────────


class RankingPageButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_RANKING_TEMPLATE,
):
    """Dynamic handler for every ``aura:kb:rank:*`` pager button, any age."""

    def __init__(self, custom_id: str) -> None:
        super().__init__(discord.ui.Button(custom_id=custom_id, label="​"))

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[discord.ui.View],
        match: re.Match[str],
        /,
    ) -> RankingPageButton:
        return cls(match.string)

    async def callback(self, interaction: discord.Interaction) -> None:
        await run_component_action(interaction, "kb-ranking-page", self._turn_page(interaction))

    async def _turn_page(self, interaction: discord.Interaction) -> None:
        """Re-render the requested page in place (behind the error boundary)."""
        page_id = parse_id(self.item.custom_id or "")
        if page_id is None:  # pragma: no cover — the template guarantees a parse
            return
        source = _find_source(interaction.client)
        if source is None:
            await answer_interaction_error(
                interaction, "Rankings are unavailable right now — try `/ranking` again."
            )
            return
        render = await source.render_ranking_page(page_id.metric, page_id.period, page_id.page)
        view = RankingView(page_id.metric, page_id.period, page_id.page, has_next=render.has_next)
        await interaction.response.edit_message(
            embed=render.embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
