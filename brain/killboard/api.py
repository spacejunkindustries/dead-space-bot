"""Async client for Albion's unofficial gameinfo API (killboard GDD §2.3, §13, §15).

The gameinfo API is developer-owned but unsupported for third-party use: no key,
no docs, no stability guarantee. The PvP endpoints are slow, ``504`` is common,
and battles occasionally vanish from responses (§2.1). This client is built for
that reality — the guiding rule of §13 is that *a broken API costs freshness,
never correctness, and never a crash*. Every method returns ``None`` / ``[]`` on
give-up and logs at ``warning``; a caller never sees an exception from a flaky
endpoint.

Config is read at call time through a provider callable, so a hot reload of
``cfg.killboard`` (region, timeouts, retries, backoff) takes effect on the very
next request without recreating the client. One lazily-created
:class:`aiohttp.ClientSession` carries the descriptive User-Agent required by the
API etiquette in §15.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import aiohttp
import structlog

from killboard.config import USER_AGENT, region_host

if TYPE_CHECKING:
    from cortana.config import KillboardConfig

log = structlog.get_logger(__name__)


class KbApi:
    """Async gameinfo client scoped to one guild (killboard GDD §2.3).

    Constructed with a zero-argument ``cfg_provider`` that returns the *current*
    :class:`~cortana.config.KillboardConfig`. The provider is called on every
    request, so region and the poller's timeout/retry/backoff settings are read
    live — hot reloads apply without rebuilding the client. Typical wiring:
    ``KbApi(lambda: ctx.holder.current.killboard)``.

    The single :class:`aiohttp.ClientSession` is created lazily on first use and
    reused for the client's lifetime. Call :meth:`close` on shutdown.
    """

    def __init__(self, cfg_provider: Callable[[], KillboardConfig]) -> None:
        self._cfg_provider = cfg_provider
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return the shared session, creating it on first use.

        The lock guards against two concurrent first requests each opening a
        session and leaking one. The User-Agent is set as a session-wide default
        header (§15).
        """
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
            return self._session

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any | None:
        """GET ``{region_host}{path}`` and return parsed JSON, or ``None`` on give-up.

        Applies the poller's ``request_timeout_seconds`` per attempt and retries up
        to ``max_retries`` times with exponential backoff (``backoff_base_seconds
        * 2**n``) on timeouts and ``5xx``. A ``429`` backs off harder (an extra
        doubling) to respect rate limits (§13). A ``4xx`` other than ``429`` is a
        client/not-found condition that will not improve on retry, so it returns
        ``None`` immediately. Never raises to the caller — every failure path is a
        logged ``None`` (§13: never a crash).

        ``path`` must start with ``/`` and is appended to the region host.
        """
        cfg = self._cfg_provider()
        poller = cfg.poller
        try:
            base = region_host(cfg.region)
        except ValueError:
            log.warning("kb_api.bad_region", region=cfg.region)
            return None

        url = f"{base}{path}"
        timeout = aiohttp.ClientTimeout(total=poller.request_timeout_seconds)
        backoff_base = poller.backoff_base_seconds
        max_retries = poller.max_retries

        session = await self._get_session()

        # attempt 0 is the initial try; attempts 1..max_retries are retries.
        for attempt in range(max_retries + 1):
            try:
                async with session.get(url, params=params, timeout=timeout) as resp:
                    status = resp.status
                    if status == 200:
                        try:
                            return await resp.json(content_type=None)
                        except (aiohttp.ContentTypeError, ValueError) as exc:
                            log.warning("kb_api.bad_json", path=path, error=str(exc))
                            return None
                    if status == 429:
                        # Rate limited: back off harder than a normal failure.
                        delay = backoff_base * (2 ** (attempt + 1))
                        log.warning(
                            "kb_api.rate_limited",
                            path=path,
                            attempt=attempt,
                            retry_in=delay,
                        )
                        if attempt >= max_retries:
                            return None
                        await asyncio.sleep(delay)
                        continue
                    if 500 <= status < 600:
                        delay = backoff_base * (2**attempt)
                        log.warning(
                            "kb_api.server_error",
                            path=path,
                            status=status,
                            attempt=attempt,
                            retry_in=delay,
                        )
                        if attempt >= max_retries:
                            return None
                        await asyncio.sleep(delay)
                        continue
                    # Other 4xx (404 not-found, 400 bad request): not retryable.
                    log.warning("kb_api.client_error", path=path, status=status)
                    return None
            except (TimeoutError, aiohttp.ClientError) as exc:
                delay = backoff_base * (2**attempt)
                log.warning(
                    "kb_api.request_failed",
                    path=path,
                    error=str(exc),
                    attempt=attempt,
                    retry_in=delay,
                )
                if attempt >= max_retries:
                    return None
                await asyncio.sleep(delay)
                continue
        return None

    async def search_guild(self, name: str) -> dict[str, Any] | None:
        """Resolve a guild name to its API record via ``/search`` (§2.3).

        Returns the guild whose ``Name`` matches ``name`` case-insensitively.
        Returns ``None`` if the request fails, no guilds come back, or NONE match
        exactly — the search is prefix/fuzzy on the API side, so a typo or a
        wrong-region query returns plausible-but-wrong candidates; auto-resolving
        to the first of those would silently track the wrong guild's kills. Fail
        loud (§13 "resolves empty → fail fast") instead of guessing.
        """
        data = await self._get("/search", {"q": name})
        if not isinstance(data, dict):
            return None
        guilds = data.get("guilds") or []
        if not isinstance(guilds, list) or not guilds:
            return None

        wanted = name.strip().casefold()
        for guild in guilds:
            if not isinstance(guild, dict):
                continue
            gname = guild.get("Name")
            if isinstance(gname, str) and gname.strip().casefold() == wanted:
                return guild
        candidates = [g.get("Name") for g in guilds if isinstance(g, dict)]
        log.warning("kb_api.guild_no_exact_match", wanted=name, candidates=candidates[:10])
        return None

    async def events(
        self, guild_id: str, limit: int = 51, offset: int = 0
    ) -> list[dict[str, Any]] | None:
        """Fetch recent kill events involving the guild (§2.3, §5).

        ``/events?guildId={id}&sort=recent&limit={limit}&offset={offset}``. Returns
        the raw event dicts newest-first, an empty list for a genuinely empty
        window, or **None when the request gave up** (timeout / exhausted 5xx /
        429). The None-vs-[] distinction is load-bearing for the poller: a failed
        deeper page during a spike must NOT be mistaken for end-of-window, or the
        high-water mark advances past events that were never fetched (data loss,
        killboard GDD §5.2). ``limit`` is capped by the endpoint at 51.
        """
        data = await self._get(
            "/events",
            {
                "guildId": guild_id,
                "sort": "recent",
                "limit": limit,
                "offset": offset,
            },
        )
        if data is None:
            return None  # gave up — not an empty window
        return _as_dict_list(data)

    async def player_deaths(self, player_id: str, limit: int = 51) -> list[dict[str, Any]]:
        """Fetch a player's recent DEATH events (§2.3, §5).

        ``/players/{id}/deaths`` — the guild-events endpoint is kill-only (it only
        returns events where the guild landed the final blow), so guild deaths are
        gathered per-member from here and ingested the same way (classify → DEATH).
        Returns the raw event dicts (newest-first) or ``[]`` on failure — a flaky
        member fetch costs that member's deaths this sweep, never the whole poll.
        """
        data = await self._get(f"/players/{player_id}/deaths", {"limit": limit})
        return _as_dict_list(data)

    async def guild(self, guild_id: str) -> dict[str, Any] | None:
        """Fetch the guild summary (lifetime Kill/Death Fame, member count) (§2.3).

        ``/guilds/{id}``. Returns the raw guild dict or ``None`` on failure.
        """
        data = await self._get(f"/guilds/{guild_id}")
        return data if isinstance(data, dict) else None

    async def members(self, guild_id: str) -> list[dict[str, Any]]:
        """Fetch the guild's member roster with lifetime fame (§2.3, §8.2).

        ``/guilds/{id}/members``. Returns the list of member dicts or ``[]`` on
        failure. These aggregates update only ~daily (§2.4, trap 3) and back the
        lifetime figures only, never windowed rankings.
        """
        data = await self._get(f"/guilds/{guild_id}/members")
        return _as_dict_list(data)

    async def top(self, guild_id: str) -> list[dict[str, Any]]:
        """Fetch the guild's highest-fame kills (§2.3).

        ``/guilds/{id}/top``. Returns the list of event dicts or ``[]`` on failure.
        """
        data = await self._get(f"/guilds/{guild_id}/top")
        return _as_dict_list(data)

    async def battles(self, guild_id: str) -> list[dict[str, Any]]:
        """Fetch recent large-scale battles the guild took part in (§2.3, §9).

        ``/battles?guildId={id}&sort=recent``. This is among the least reliable
        endpoints (§13), so a failure returns ``[]`` quietly rather than
        propagating — battle posting tolerates gaps and never blocks the feed.
        """
        data = await self._get("/battles", {"guildId": guild_id, "sort": "recent"})
        return _as_dict_list(data)

    async def close(self) -> None:
        """Close the underlying session. Idempotent; safe to call when unopened."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


def _as_dict_list(data: Any) -> list[dict[str, Any]]:
    """Coerce a parsed response into a list of dicts, tolerating junk (§2.4).

    A non-list response (``None`` on failure, or an unexpected shape) yields ``[]``;
    non-dict elements are dropped rather than crashing a downstream ``.get()``.
    """
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]
