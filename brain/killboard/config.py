"""Pure, stateless views and helpers over the killboard config section.

A thin layer over ``cfg.killboard.*`` (see :class:`cortana.config.KillboardConfig`
and killboard GDD §2.2, §12). It resolves the regional gameinfo API host, carries
the bot's User-Agent (§15), and builds render-service icon URLs.

This module is deliberately dependency-free: it imports neither ``discord`` nor
``aiohttp`` and holds no state. Everything here is a pure function or constant, so
it can be imported from anywhere in the package without side effects.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)

#: Descriptive User-Agent sent on every gameinfo/render request (killboard GDD
#: §15). The API is unofficial and shared community-wide; a self-identifying
#: agent lets Albion's operators contact us if the bot ever misbehaves.
USER_AGENT: str = (
    "DeadKillboard/1.0 (+https://github.com/dead-space-bot; "
    "self-hosted single-guild Albion killboard)"
)

#: The gameinfo API host per region (killboard GDD §2.2). A guild lives on
#: exactly one regional server; hitting the wrong host returns empty/not-found
#: data — a *silent* failure — so this mapping is the first thing to verify on
#: setup. Keys match ``cfg.killboard.region`` ("west" | "europe" | "east").
REGION_HOSTS: dict[str, str] = {
    "west": "https://gameinfo.albiononline.com/api/gameinfo",
    "europe": "https://gameinfo-ams.albiononline.com/api/gameinfo",
    "east": "https://gameinfo-sgp.albiononline.com/api/gameinfo",
}

#: RENDER note — item icons come from a *different*, documented, reliable host
#: (the render service, killboard GDD §2.3), NEVER the flaky gameinfo API. Kill
#: cards fetch each icon once from here and cache it on disk, so imagery never
#: depends on the unstable PvP endpoints. The base is configurable
#: (``cfg.killboard.cards.render_base``); this is the documented default.
RENDER_BASE_DEFAULT: str = "https://render.albiononline.com/v1"


def region_host(region: str) -> str:
    """Return the gameinfo API base URL for ``region`` (killboard GDD §2.2).

    ``region`` is matched case-insensitively against "west", "europe", "east".
    An unknown region raises :class:`ValueError` naming the valid options — a
    misconfigured region is a setup error that must fail loudly, not silently
    query the wrong server.
    """
    key = region.strip().lower()
    try:
        return REGION_HOSTS[key]
    except KeyError:
        valid = ", ".join(sorted(REGION_HOSTS))
        raise ValueError(f"unknown killboard region {region!r}; expected one of: {valid}") from None


def render_icon_url(render_base: str, item_type: str, enchant: int = 0) -> str:
    """Build a render-service icon URL for an item (killboard GDD §2.3, §7.1).

    ``https://render.albiononline.com/v1/item/{ItemType}.png`` with ``@{level}``
    appended for enchant levels 1-3. Enchant 0 (or any value outside 1-3, since
    the API only serves those) yields the base, unenchanted icon. ``render_base``
    is normalised to tolerate a trailing slash.
    """
    base = render_base.rstrip("/")
    suffix = f"@{enchant}" if 1 <= enchant <= 3 else ""
    return f"{base}/item/{item_type}{suffix}.png"
