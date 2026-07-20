"""Battle summaries: large fights condensed to one embed (killboard GDD §9).

Large-scale fights the tracked guild took part in are fetched from the gameinfo
``/battles`` endpoint and each condensed to a single Discord embed — participating
guilds and alliances, player counts and kills/deaths per side, total fame swung,
and a link to the full battleboard.

Two things shape this module:

* **The battles endpoint is among the least reliable** (killboard GDD §2.1, §13).
  Every path here tolerates gaps quietly: :meth:`Battles.recent` returns ``[]`` on
  any failure and never raises, so a missing battle degrades freshness but never
  blocks the kill feed or crashes the poller.
* **Only real fights should post.** :func:`passes_threshold` gates on the battle's
  scale (total players and total fame) so routine skirmishes don't spam the battle
  channel (killboard GDD §9). It is a pure function — trivially testable.

The gameinfo API is undocumented and returns partial/``null`` fields (killboard GDD
§2.4); every extractor here uses tolerant ``.get()`` access with defaults and never
assumes a field is present or well-typed.

This module builds embeds; it never *sends* them. The caller (the cog / feed) is
responsible for sending with ``allowed_mentions=discord.AllowedMentions.none()`` —
the killboard is informational and must never ping anyone (CLAUDE.md constraint 11).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

import discord
import structlog

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

    from cortana.config import KillboardConfig
    from killboard.api import KbApi

log = structlog.get_logger(__name__)

#: Embed accent for battle cards — a neutral gold, distinct from the feed's
#: green kills / red deaths so a battle summary reads as its own kind of post.
_BATTLE_COLOUR = discord.Colour(0xC9A227)

#: Official killboard battleboard URL for a battle id (killboard GDD §9). The
#: web killboard is region-agnostic on the battle path; a bad id simply 404s in
#: the browser, which is harmless for a best-effort link.
_BATTLEBOARD_URL = "https://albiononline.com/killboard/battles/{battle_id}"

#: How many participating guilds / alliances to list before collapsing the tail
#: into an "…and N more" line, keeping the embed within Discord's field limits.
_MAX_SIDES = 8


class Battles:
    """Fetch and summarise large-scale battles for the tracked guild (GDD §9).

    Constructed with:

    * ``api`` — the shared :class:`~killboard.api.KbApi` client.
    * ``cfg_provider`` — a zero-arg callable returning the *current*
      :class:`~cortana.config.KillboardConfig`, read live on every call so a hot
      reload of the tracked guild or thresholds applies to the next fetch.
    * ``log`` — a bound structlog logger from the module context.

    Nothing here holds mutable state; the store and the feed are untouched — this
    class only reads the API and shapes embeds.
    """

    def __init__(
        self,
        api: KbApi,
        cfg_provider: Callable[[], KillboardConfig],
        log: BoundLogger,
    ) -> None:
        self._api = api
        self._cfg_provider = cfg_provider
        self._log = log

    async def recent(self, limit: int) -> list[dict[str, Any]]:
        """Recent battles the guild fought that clear the posting threshold (§9).

        Fetches ``/battles`` for the tracked guild, drops anything below
        ``cfg.battles.min_players`` / ``cfg.battles.min_fame`` via
        :func:`passes_threshold`, and returns up to ``limit`` battle dicts
        newest-first (the endpoint already sorts recent).

        Tolerant of the least-reliable endpoint (killboard GDD §2.1, §13): a
        missing guild id, an empty response, or *any* exception yields ``[]`` and
        a logged warning — never a crash, never a raise into the caller.
        """
        if limit <= 0:
            return []
        try:
            cfg = self._cfg_provider()
            guild_id = cfg.guild_id
            if not guild_id:
                return []
            raw = await self._api.battles(guild_id)
            min_players = cfg.battles.min_players
            min_fame = cfg.battles.min_fame
            kept: list[dict[str, Any]] = []
            for battle in raw:
                if not isinstance(battle, dict):
                    continue
                if passes_threshold(battle, min_players, min_fame):
                    kept.append(battle)
                if len(kept) >= limit:
                    break
            return kept
        except Exception as exc:  # noqa: BLE001 — GDD §9/§13: tolerate gaps, never crash.
            self._log.warning("kb_battles.recent_failed", error=str(exc))
            return []

    @staticmethod
    def build_summary_embed(battle: dict[str, Any]) -> discord.Embed:
        """Condense one battle into a single summary embed (killboard GDD §9).

        Renders participating guilds and alliances with player counts and
        kills/deaths per side, the total fame swung and total kills, and a link to
        the full battleboard. Every field is read tolerantly (§2.4) — a battle
        missing guilds, players, fame, or a timestamp still produces a valid embed
        rather than raising.

        Pure and side-effect free: it builds an embed but does not send it. The
        caller must send with ``AllowedMentions.none()`` (CLAUDE.md constraint 11).
        """
        battle_id = _battle_id(battle)
        players = _player_count(battle)
        total_fame = _int(battle.get("totalFame"))
        total_kills = _int(battle.get("totalKills"))

        title = f"Battle #{battle_id}" if battle_id is not None else "Large-scale battle"
        embed = discord.Embed(title=title, colour=_BATTLE_COLOUR)
        if battle_id is not None:
            embed.url = _BATTLEBOARD_URL.format(battle_id=battle_id)

        embed.description = (
            f"**{players}** players · **{_fmt_num(total_fame)}** fame · **{total_kills}** kills"
        )

        guild_sides = _summarise_sides(battle, "guilds")
        if guild_sides:
            embed.add_field(
                name="Guilds",
                value=_sides_block(guild_sides),
                inline=False,
            )

        alliance_sides = _summarise_sides(battle, "alliances")
        if alliance_sides:
            embed.add_field(
                name="Alliances",
                value=_sides_block(alliance_sides),
                inline=False,
            )

        started = _parse_time(battle.get("startTime"))
        if started is not None:
            embed.timestamp = started

        embed.set_footer(text="Battle summary")
        return embed


def passes_threshold(battle: dict[str, Any], min_players: int, min_fame: int) -> bool:
    """Whether a battle is large enough to post (killboard GDD §9).

    Pure filter: a battle posts only when its scale — total participating players
    **and** total fame swung — reaches the configured floors, so routine
    skirmishes never spam the battle channel. Both thresholds must be met.

    Tolerant of partial data (§2.4): a battle missing ``totalFame`` reads as ``0``
    fame and an absent player list reads as ``0`` players, so junk simply fails the
    gate rather than raising.
    """
    players = _player_count(battle)
    total_fame = _int(battle.get("totalFame"))
    return players >= min_players and total_fame >= min_fame


# ── tolerant extraction helpers (killboard GDD §2.4) ──────────────────────────


def _battle_id(battle: dict[str, Any]) -> int | None:
    """The battle's id as an int, or ``None`` if absent/unparseable."""
    raw = battle.get("id")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def _player_count(battle: dict[str, Any]) -> int:
    """Total distinct players in the battle, tolerating shape drift.

    Prefers the ``players`` collection (a dict keyed by player id in the observed
    shape, occasionally a list), falls back to an explicit ``totalPlayers`` count,
    then to the summed ``players`` counts of the per-guild breakdown, and finally
    to ``0`` — never raising on a missing or oddly-typed field (§2.4).
    """
    players = battle.get("players")
    if isinstance(players, dict):
        return len(players)
    if isinstance(players, list):
        return len(players)

    explicit = battle.get("totalPlayers")
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return explicit

    guilds = battle.get("guilds")
    if isinstance(guilds, dict):
        total = 0
        for side in guilds.values():
            if isinstance(side, dict):
                total += _int(side.get("players"))
        if total:
            return total
    return 0


def _summarise_sides(battle: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Per-side rows for ``guilds`` or ``alliances``, sorted by fame desc.

    Each row is ``{name, players, kills, deaths, fame}``. Player counts per side
    are taken from the side's own ``players`` figure when present, otherwise
    derived by attributing entries in the battle's ``players`` collection to their
    guild/alliance. Missing fields collapse to ``0`` / ``"Unknown"`` (§2.4).
    """
    sides = battle.get(key)
    if not isinstance(sides, dict) or not sides:
        return []

    attributed = _players_per_side(battle, key)

    rows: list[dict[str, Any]] = []
    for side_id, side in sides.items():
        if not isinstance(side, dict):
            continue
        name = side.get("name") or side.get("Name") or "Unknown"
        players = _int(side.get("players"))
        if not players:
            players = attributed.get(str(side_id), 0)
        rows.append(
            {
                "name": str(name),
                "players": players,
                "kills": _int(side.get("kills")),
                "deaths": _int(side.get("deaths")),
                "fame": _int(side.get("killFame")),
            }
        )

    rows.sort(key=lambda r: (r["fame"], r["kills"]), reverse=True)
    return rows


def _players_per_side(battle: dict[str, Any], key: str) -> dict[str, int]:
    """Count players per guild/alliance id from the battle's ``players`` map.

    A fallback for when a side omits its own ``players`` count. Attribution keys
    on ``guildId`` (for ``guilds``) or ``allianceId`` (for ``alliances``) found on
    each player entry. Tolerant of a missing/oddly-typed ``players`` collection.
    """
    id_field = "allianceId" if key == "alliances" else "guildId"
    players = battle.get("players")
    entries: list[Any]
    if isinstance(players, dict):
        entries = list(players.values())
    elif isinstance(players, list):
        entries = players
    else:
        return {}

    counts: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        side_id = entry.get(id_field)
        if side_id in (None, ""):
            continue
        counts[str(side_id)] = counts.get(str(side_id), 0) + 1
    return counts


def _sides_block(sides: list[dict[str, Any]]) -> str:
    """Render side rows into one embed-field string, capped at :data:`_MAX_SIDES`.

    Each line reads ``**Name** · Np · K/D · <fame> fame``; any overflow past the
    cap collapses to a single "…and N more" tail so the field stays within
    Discord's 1024-character limit.
    """
    shown = sides[:_MAX_SIDES]
    lines = [
        f"**{_clip(side['name'], 32)}** · {side['players']}p · "
        f"{side['kills']}/{side['deaths']} · {_fmt_num(side['fame'])} fame"
        for side in shown
    ]
    remaining = len(sides) - len(shown)
    if remaining > 0:
        lines.append(f"…and {remaining} more")
    return "\n".join(lines) or "—"


def _parse_time(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp tolerantly, returning ``None`` on any failure.

    Accepts a trailing ``Z`` (UTC) which older Pythons' ``fromisoformat`` reject,
    and truncates over-long fractional seconds the API sometimes emits.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        # Trim fractional seconds to microsecond precision and retry once.
        if "." in text:
            head, _, tail = text.partition(".")
            frac = tail[:6]
            tz = ""
            for marker in ("+", "-"):
                idx = tail.find(marker)
                if idx != -1:
                    frac = tail[:idx][:6]
                    tz = tail[idx:]
                    break
            try:
                return datetime.fromisoformat(f"{head}.{frac}{tz}")
            except ValueError:
                return None
        return None


def _int(value: Any) -> int:
    """Coerce a possibly-missing/float/string number to ``int``, else ``0`` (§2.4)."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _fmt_num(value: int) -> str:
    """Compact human number: ``5_000_000`` → ``5.0M``, ``12_300`` → ``12.3K``."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _clip(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` chars with an ellipsis when it overruns."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = ["Battles", "passes_threshold"]
