"""Windowed leaderboards and member records over the event store (GDD §8).

Everything the paid killbots cannot do lives here: kills, deaths, true K/D
*by count*, and windowed Kill Fame, all derived from the ingested event store
(GDD §8.1) rather than the API's lagging aggregates (GDD §2.4, trap 3).

The module splits cleanly into two testable halves:

* **Pure window math** — :func:`window_for` / :func:`custom_window` turn a
  named period (``today`` / ``week`` / ``month``) or an explicit date range,
  interpreted in the guild's configured timezone (``cfg.killboard.rankings.
  timezone``), into the ``[start, end)`` UTC ISO strings the store compares
  against ``events.timestamp``. No I/O, fully deterministic given ``now``.
* **Ranking builders** — :func:`build_leaderboard` and
  :func:`build_member_record` call the store's aggregate methods (blocking
  sqlite; the caller MUST wrap them in :func:`asyncio.to_thread`) and return
  plain, embeddable data.
* **Embed formatting** — :func:`leaderboard_embed` / :func:`record_embed` are
  pure: they take already-computed data and return a coloured
  :class:`discord.Embed`. No network, no store access.

Nothing here sends a message. The caller (commands / schedule) is responsible
for posting with ``allowed_mentions=discord.AllowedMentions.none()`` — the
killboard is informational and never pings (CLAUDE.md constraint 11).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
import structlog

if TYPE_CHECKING:
    from killboard.store import KbStore

log = structlog.get_logger(__name__)

# ── public vocabularies ──────────────────────────────────────────────────────

#: Named windows a leaderboard can span (GDD §8.1). ``custom`` is served by
#: :func:`custom_window` with an explicit range rather than this mapping.
PERIODS: tuple[str, ...] = ("today", "week", "month")

#: Human labels for the named periods, for embed titles/footers.
PERIOD_LABELS: dict[str, str] = {
    "today": "Today",
    "week": "This Week",
    "month": "This Month",
}

#: Leaderboard metrics this module ranks by (GDD §8.1). A subset of the store's
#: sort orders — ``deaths`` is intentionally not a public ranking metric here.
METRICS: tuple[str, ...] = ("fame", "kills", "kd")

#: Human labels for the metrics, for embed titles.
METRIC_LABELS: dict[str, str] = {
    "fame": "Kill Fame",
    "kills": "Kills",
    "kd": "K/D",
}

#: Embed accent colour per metric (Discord ints).
_METRIC_COLOUR: dict[str, int] = {
    "fame": 0xF1C40F,  # gold — fame
    "kills": 0x2ECC71,  # green — kills
    "kd": 0x5865F2,  # blurple — ratio
}

#: Accent colour for a member-record card.
_RECORD_COLOUR: int = 0x3498DB


@dataclass(frozen=True, slots=True)
class MemberRecord:
    """A member's current-window activity, computed from the event store (§8.2).

    All five figures are *windowed* (by-count kills/deaths, their true ratio,
    summed Kill Fame, and assist participations) — distinct from the lifetime
    Kill Fame read from the API roster, which :func:`record_embed` labels
    separately when supplied.
    """

    player_name: str
    kills: int
    deaths: int
    kd: float
    fame: int
    assists: int


@dataclass(frozen=True, slots=True)
class DailyRanking:
    """A whole-guild ranking snapshot for a window (killboard GDD §8.3).

    Mirrors the "Daily Ranking" card the community killbots post: two guild-wide
    fame totals plus two ordered top-N boards — one by Kill Fame (fame earned),
    one by Death Fame (fame lost). Every figure is windowed from the event store,
    so it is current to the last poll rather than the API's ~daily lifetime lag.
    """

    total_kill_fame: int
    total_death_fame: int
    top_kill_fame: list[tuple[str, int]]
    top_death_fame: list[tuple[str, int]]


# ── pure window math (GDD §8.1) ──────────────────────────────────────────────


def _zone(tz: str) -> ZoneInfo:
    """The configured timezone, falling back to UTC on an unknown name.

    Window boundaries are meaningless if the tz string is garbage, but a bad
    config value must never crash a ranking — so an unresolvable zone degrades
    to UTC with a warning rather than raising.
    """
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        log.warning("kb_rankings_bad_timezone", timezone=tz)
        return ZoneInfo("UTC")


def _to_utc_iso(dt: datetime) -> str:
    """A local aware datetime → the UTC ISO prefix the store compares against.

    Emitted as ``YYYY-MM-DDTHH:MM:SS`` (UTC, no offset suffix): the store does a
    plain string ``>=`` / ``<`` against the raw gameinfo ``TimeStamp`` (also
    UTC), and this bare prefix orders correctly against gameinfo values whether
    or not they carry fractional seconds or a trailing ``Z``.
    """
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _add_month(dt: datetime) -> datetime:
    """First-of-next-month at the same wall-clock time (day is always 1 here)."""
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1)
    return dt.replace(month=dt.month + 1)


def window_for(period: str, tz: str, now: datetime | None = None) -> tuple[str, str]:
    """Compute the ``[start, end)`` UTC ISO bounds for a named ``period``.

    ``period`` is one of :data:`PERIODS`. Boundaries are day-aligned in ``tz``:
    ``today`` is local midnight to local midnight, ``week`` runs Monday→Monday,
    and ``month`` runs from the 1st to the 1st of the next month. ``end`` is
    exclusive and may sit in the future (no event exists past ``now``, so the
    window still reads "so far this period"). ``now`` defaults to the current
    instant and exists for deterministic tests.
    """
    zone = _zone(tz)
    now_local = (now or datetime.now(UTC)).astimezone(zone)
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "today":
        start_local = midnight
        end_local = start_local + timedelta(days=1)
    elif period == "week":
        start_local = midnight - timedelta(days=now_local.weekday())
        end_local = start_local + timedelta(days=7)
    elif period == "month":
        start_local = midnight.replace(day=1)
        end_local = _add_month(start_local)
    else:
        raise ValueError(f"unknown ranking period: {period!r}")

    return _to_utc_iso(start_local), _to_utc_iso(end_local)


def custom_window(start: date, end: date, tz: str) -> tuple[str, str]:
    """A custom day range → ``[start, end)`` UTC ISO bounds (GDD §8.1).

    ``start`` and ``end`` are calendar dates interpreted at local midnight in
    ``tz``. ``end`` is exclusive at the day granularity — pass the day *after*
    the last day you want included. ``date`` inputs are accepted directly;
    ``datetime`` inputs are honoured at their wall-clock time.
    """
    zone = _zone(tz)
    start_dt = _at_local_midnight(start, zone)
    end_dt = _at_local_midnight(end, zone)
    return _to_utc_iso(start_dt), _to_utc_iso(end_dt)


def _at_local_midnight(value: date, zone: ZoneInfo) -> datetime:
    """A ``date`` (or ``datetime``) anchored to local midnight in ``zone``."""
    if isinstance(value, datetime):
        return value.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=zone)
    return datetime(value.year, value.month, value.day, tzinfo=zone)


# ── ranking builders (touch the store — wrap caller-side in to_thread) ────────


def build_leaderboard(
    store: KbStore,
    metric: str,
    start: str,
    end: str | None,
    limit: int = 10,
) -> list[tuple[str, float]]:
    """Ordered ``(member_name, value)`` rows for ``metric`` in the window (§8.1).

    ``metric`` is one of :data:`METRICS`. Queries :meth:`KbStore.leaderboard`
    (already sorted by the metric) and projects each row to its display value:
    integer fame or kills, or the float K/D. Members with a missing name show as
    ``"Unknown"``. Blocking sqlite — call inside ``asyncio.to_thread``.
    """
    if metric not in METRICS:
        raise ValueError(f"unknown ranking metric: {metric!r}")

    rows = store.leaderboard(metric, start, end, limit=limit)
    out: list[tuple[str, float]] = []
    for row in rows:
        name = row.get("player_name") or "Unknown"
        value: float = float(row["kd"]) if metric == "kd" else int(row[metric])
        out.append((name, value))
    return out


def build_member_record(
    store: KbStore,
    player_id: str,
    player_name: str,
    start: str,
    end: str | None,
) -> MemberRecord:
    """A member's windowed kills/deaths/K-D/fame/assists from the store (§8.2).

    Each figure is a separate windowed store query scoped to ``player_id``. The
    returned record carries only event-store (windowed) numbers; lifetime fame
    is supplied separately to :func:`record_embed`. Blocking sqlite — call
    inside ``asyncio.to_thread``.
    """
    return MemberRecord(
        player_name=player_name,
        kills=store.kill_count(start, end, player_id),
        deaths=store.death_count(start, end, player_id),
        kd=store.kd(start, end, player_id),
        fame=store.kill_fame(start, end, player_id),
        assists=store.assists(start, end, player_id),
    )


def build_daily_ranking(
    store: KbStore,
    start: str,
    end: str | None,
    limit: int = 10,
) -> DailyRanking:
    """Whole-guild fame totals + Top Kill/Death Fame boards for the window (§8.3).

    Four store reads: the guild-wide Kill Fame and Death Fame totals, and the
    top-``limit`` members by each. The Death Fame board ranks by fame *lost*
    (``dfame``) and so surfaces members who only died in the window (they appear
    in the store leaderboard's death-only rows). Blocking sqlite — call inside
    ``asyncio.to_thread``.
    """
    kill_rows = store.leaderboard("fame", start, end, limit=limit)
    death_rows = store.leaderboard("dfame", start, end, limit=limit)
    return DailyRanking(
        total_kill_fame=store.kill_fame(start, end),
        total_death_fame=store.death_fame(start, end),
        top_kill_fame=[((r.get("player_name") or "Unknown"), int(r["fame"])) for r in kill_rows],
        top_death_fame=[((r.get("player_name") or "Unknown"), int(r["dfame"])) for r in death_rows],
    )


# ── pure formatting helpers ──────────────────────────────────────────────────


def _fmt_fame(value: float) -> str:
    """Fame with thousands separators, e.g. ``1,204,880``."""
    return f"{int(value):,}"


def _fmt_kd(value: float) -> str:
    """K/D to two decimals, e.g. ``3.25``."""
    return f"{value:.2f}"


def _fmt_value(metric: str, value: float) -> str:
    """A single leaderboard value formatted for its metric."""
    if metric == "fame":
        return _fmt_fame(value)
    if metric == "kd":
        return _fmt_kd(value)
    return f"{int(value):,}"


#: Dead Gaming's brand red — the accent the Daily Ranking card and branded kill
#: cards use. Kept here (not config) so the pure embed builder needs no wiring;
#: the image cards read the same colour from ``cfg.killboard.cards.accent_color``.
DEAD_RED: int = 0xE11212


def _fmt_fame_short(value: int) -> str:
    """Compact fame for board rows: ``116.34k`` / ``68k`` / ``744`` / ``2.5m``.

    Matches the community killbot's ranking style — abbreviated with ``k``/``m``
    and trailing zeros trimmed — so a top-10 stays readable in an embed field.
    """
    v = int(value)
    if abs(v) >= 1_000_000:
        return _trim(v / 1_000_000) + "m"
    if abs(v) >= 1_000:
        return _trim(v / 1_000) + "k"
    return str(v)


def _trim(value: float) -> str:
    """A ≤2-decimal number with trailing zeros and a bare ``.`` removed."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _board_lines(rows: list[tuple[str, int]]) -> str:
    """Numbered ``1. Name — 116.34k`` board text for a ranking field."""
    if not rows:
        return "_No activity._"
    width = len(str(len(rows)))
    return "\n".join(
        f"`{str(rank).rjust(width)}.` **{name}** — {_fmt_fame_short(value)}"
        for rank, (name, value) in enumerate(rows, start=1)
    )


# ── embed builders (pure: take computed data, no store, no network) ───────────


def leaderboard_embed(
    metric: str,
    period_label: str,
    rows: list[tuple[str, float]],
    *,
    guild_name: str | None = None,
    tz: str = "UTC",
    now: datetime | None = None,
) -> discord.Embed:
    """Render a leaderboard (from :func:`build_leaderboard`) as an embed (§8.3).

    Numbered, colour-coded by ``metric``, with a footer naming the guild,
    metric, and timezone the window is measured in. Pure — it never touches the
    store or the network, so it is safe to build on the event loop.
    """
    label = METRIC_LABELS.get(metric, metric)
    title = f"{label} · {period_label}"
    colour = _METRIC_COLOUR.get(metric, _RECORD_COLOUR)

    if rows:
        width = len(str(len(rows)))
        lines = [
            f"`{str(rank).rjust(width)}.` **{name}** — {_fmt_value(metric, value)}"
            for rank, (name, value) in enumerate(rows, start=1)
        ]
        description = "\n".join(lines)
    else:
        description = "_No activity in this window._"

    embed = discord.Embed(
        title=title,
        description=description,
        colour=discord.Colour(colour),
        timestamp=now or datetime.now(UTC),
    )
    footer = " · ".join(
        part for part in (guild_name, f"{label} leaderboard", f"times in {tz}") if part
    )
    embed.set_footer(text=footer)
    return embed


def daily_ranking_embed(
    ranking: DailyRanking,
    period_label: str,
    *,
    heading: str = "Daily Ranking",
    guild_name: str | None = None,
    tz: str = "UTC",
    now: datetime | None = None,
) -> discord.Embed:
    """Render a :class:`DailyRanking` as the combined ranking embed (§8.3).

    A header carrying the two guild-wide fame totals, then a **Top Kill Fame** and
    a **Top Death Fame** board side by side — the layout the community killbots
    post, in Dead Gaming's accent colour. ``heading`` names the period ("Daily
    Ranking", "Weekly Ranking", …). Pure: no store, no network, so it is safe to
    build on the event loop. The image twin is
    :meth:`killboard.cards.CardRenderer.render_ranking_card`; this embed is the
    always-available fallback when cards are off or Pillow is absent.
    """
    header = (
        f"**Total Kill Fame:** {_fmt_fame(ranking.total_kill_fame)}\n"
        f"**Total Death Fame:** {_fmt_fame(ranking.total_death_fame)}"
    )
    embed = discord.Embed(
        title=f"☠️ {heading} · {period_label}",
        description=header,
        colour=discord.Colour(DEAD_RED),
        timestamp=now or datetime.now(UTC),
    )
    embed.add_field(name="🗡️ Top Kill Fame", value=_board_lines(ranking.top_kill_fame), inline=True)
    embed.add_field(
        name="💀 Top Death Fame", value=_board_lines(ranking.top_death_fame), inline=True
    )
    footer = " · ".join(part for part in (guild_name, f"times in {tz}") if part)
    if footer:
        embed.set_footer(text=footer)
    return embed


def record_embed(
    record: MemberRecord,
    *,
    period_label: str = "This Month",
    lifetime_fame: int | None = None,
    guild_name: str | None = None,
    now: datetime | None = None,
) -> discord.Embed:
    """Render a member record (from :func:`build_member_record`) as an embed.

    Shows the member's windowed kills, deaths, true K/D, Kill Fame, and assists
    — all clearly scoped to ``period_label`` (the current month by default).
    When ``lifetime_fame`` is supplied it is shown as a separate, explicitly
    labelled field so the ~daily-lagging career total (GDD §2.4, §8.2) is never
    confused with the windowed figure. Pure — no store, no network.
    """
    embed = discord.Embed(
        title=f"{record.player_name} — {period_label}",
        colour=discord.Colour(_RECORD_COLOUR),
        timestamp=now or datetime.now(UTC),
    )
    embed.add_field(name="Kills", value=f"{record.kills:,}", inline=True)
    embed.add_field(name="Deaths", value=f"{record.deaths:,}", inline=True)
    embed.add_field(name="K/D", value=_fmt_kd(record.kd), inline=True)
    embed.add_field(name="Kill Fame", value=_fmt_fame(record.fame), inline=True)
    embed.add_field(name="Assists", value=f"{record.assists:,}", inline=True)

    if lifetime_fame is not None:
        embed.add_field(
            name="Lifetime Kill Fame",
            value=f"{lifetime_fame:,}",
            inline=True,
        )
        embed.set_footer(
            text=(guild_name + " · " if guild_name else "")
            + "Lifetime fame from the API roster (updates ~daily)"
        )
    elif guild_name:
        embed.set_footer(text=guild_name)

    return embed


__all__ = [
    "DEAD_RED",
    "METRICS",
    "METRIC_LABELS",
    "PERIODS",
    "PERIOD_LABELS",
    "DailyRanking",
    "MemberRecord",
    "build_daily_ranking",
    "build_leaderboard",
    "build_member_record",
    "custom_window",
    "daily_ranking_embed",
    "leaderboard_embed",
    "record_embed",
    "window_for",
]
