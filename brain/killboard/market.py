"""Async client for the Albion Online Data Project (AODP) market API.

The killboard's PvP data comes from the unofficial *gameinfo* API (see
:mod:`killboard.api`); market *prices* come from an entirely separate community
project — the **Albion Online Data Project** — with its own hosts, its own
schema, and its own rate limits. This module is the thin async client for that
second API. It is deliberately kept apart from :class:`killboard.api.KbApi`:
different base URLs, a different tolerant row shape, and an in-memory price
cache that :class:`~killboard.api.KbApi` has no use for.

The guiding rule of the killboard (GDD §13) holds here too: *a flaky API costs
freshness, never correctness, and never a crash.* Every public method returns
``[]`` (or an empty result) on give-up and logs at ``warning``; a caller never
sees an exception raised out of a market lookup.

Config is read live through a zero-argument provider that returns the current
:class:`~cortana.config.KillboardConfig`, so a hot reload of ``cfg.killboard``
(region, market timeouts, cache TTL, default cities/quality) applies on the very
next request without recreating the client. The market-specific knobs live under
``cfg.killboard.market.*`` and are read defensively with ``getattr`` fallbacks,
so this module imports and runs even before that config section is wired in.

Rate limits are real (180 req/min AND 300 req/5min) and enforced community-wide,
so prices are cached in memory with a TTL (``cache_ttl_s``, default 300s) keyed
by the normalized query — repeated lookups within the window never hit the
network. Item-id batches are capped under the API's 4096-char URL limit.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiohttp
import structlog

from killboard.config import USER_AGENT

if TYPE_CHECKING:
    from cortana.config import KillboardConfig

log = structlog.get_logger(__name__)


# ── AODP hosts (per region) ──────────────────────────────────────────────────
#: The Albion Online Data Project host per region. These are the AODP's OWN
#: hosts and are unrelated to the gameinfo ``region_host`` mapping in
#: :mod:`killboard.config` — reusing the gameinfo host here returns 404s. Keys
#: match ``cfg.killboard.region`` ("west" | "europe" | "east").
AODP_HOSTS: dict[str, str] = {
    "west": "https://west.albion-online-data.com",
    "europe": "https://europe.albion-online-data.com",
    "east": "https://east.albion-online-data.com",
}

#: The AODP "no data" sentinel: a price of 0 dated to the .NET epoch means the
#: item/city/quality has never been seen, NOT a real price of zero silver.
_EPOCH_DATE: str = "0001-01-01T00:00:00"

#: URL length is capped at 4096 chars by the API. We keep the comma-joined
#: item-id segment well under that to leave room for host + path + query.
_MAX_ITEMIDS_CHARS: int = 3500
#: A hard cap on items per batch as a second guard, independent of char length.
_MAX_ITEMS_PER_BATCH: int = 100

#: Retry budget for a single HTTP attempt series. The market API is steadier
#: than gameinfo, so this is modest; 429s back off harder (§13 etiquette).
_MAX_RETRIES: int = 3
_BACKOFF_BASE_S: float = 0.5


@dataclass(frozen=True, slots=True)
class PriceRow:
    """One AODP current-price row (item × city × quality).

    Mirrors the API object exactly. Prices are integer silver; a value of ``0``
    paired with the ``0001-01-01`` epoch date means *no data*, never a real
    price — use :attr:`has_data` to tell the difference and render "unknown"
    rather than a misleading ``0``.
    """

    item_id: str
    city: str
    quality: int
    sell_min: int
    sell_min_date: str
    sell_max: int
    sell_max_date: str
    buy_min: int
    buy_min_date: str
    buy_max: int
    buy_max_date: str

    @property
    def has_data(self) -> bool:
        """``True`` when this row carries a real observation.

        A row has data when either the minimum sell price or the maximum buy
        price is positive AND its accompanying date is not the AODP epoch
        sentinel. Everything else is "unknown" and must not be shown as a 0.
        """
        if self.sell_min > 0 and not _is_epoch(self.sell_min_date):
            return True
        if self.buy_max > 0 and not _is_epoch(self.buy_max_date):  # noqa: SIM103
            return True
        return False


class MarketClient:
    """Async AODP market client scoped to one killboard (GDD §13, market layer).

    Constructed with a zero-argument ``cfg_provider`` returning the *current*
    :class:`~cortana.config.KillboardConfig`; region, timeout, cache TTL, and
    default cities/quality are read on every call so hot reloads apply live.
    Typical wiring: ``MarketClient(lambda: ctx.holder.current.killboard)``.

    A single :class:`aiohttp.ClientSession` is created lazily and reused; call
    :meth:`close` on shutdown. Current-price lookups are served from an in-memory
    TTL cache keyed by the normalized query to respect the API's rate limits.
    """

    def __init__(self, cfg_provider: Callable[[], KillboardConfig]) -> None:
        self._cfg_provider = cfg_provider
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        #: query-key → (expires_at_monotonic, rows)
        self._price_cache: dict[str, tuple[float, list[PriceRow]]] = {}

    # ── config views ─────────────────────────────────────────────────────────

    def _market_cfg(self) -> _ResolvedMarket:
        """Resolve the live market config, tolerant of the section being absent.

        ``cfg.killboard.market`` is wired in separately; until then (or if a
        half-set config omits fields) every knob falls back to a safe default so
        this client keeps importing and running.
        """
        cfg = self._cfg_provider()
        market = getattr(cfg, "market", None)
        region = str(getattr(cfg, "region", "west"))
        return _ResolvedMarket(
            region=region,
            cache_ttl_s=int(getattr(market, "cache_ttl_s", 300) or 300),
            request_timeout_s=int(getattr(market, "request_timeout_s", 10) or 10),
            default_cities=_str_list(getattr(market, "default_cities", None)),
            default_quality=int(getattr(market, "default_quality", 1) or 1),
            user_agent=str(getattr(market, "user_agent", "") or USER_AGENT),
        )

    def _base_url(self, region: str) -> str | None:
        """The AODP base URL for ``region``, or ``None`` (logged) if unknown."""
        base = AODP_HOSTS.get(region.strip().lower())
        if base is None:
            log.warning("kb_market.bad_region", region=region)
        return base

    # ── session ──────────────────────────────────────────────────────────────

    async def _get_session(self, user_agent: str) -> aiohttp.ClientSession:
        """Return the shared session, creating it on first use.

        The lock stops two concurrent first calls from each opening (and leaking)
        a session. The descriptive User-Agent is a session-wide default header,
        required by AODP etiquette.
        """
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(headers={"User-Agent": user_agent})
            return self._session

    async def _get_json(self, url: str, timeout_s: int, user_agent: str) -> Any | None:
        """GET ``url`` and return parsed JSON, or ``None`` on give-up.

        Retries up to :data:`_MAX_RETRIES` on timeouts and ``5xx`` with
        exponential backoff; a ``429`` backs off harder (an extra doubling) to
        respect rate limits; other ``4xx`` are non-retryable and return ``None``
        immediately. Never raises to the caller (§13).
        """
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        session = await self._get_session(user_agent)

        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with session.get(url, timeout=timeout) as resp:
                    status = resp.status
                    if status == 200:
                        try:
                            return await resp.json(content_type=None)
                        except (aiohttp.ContentTypeError, ValueError) as exc:
                            log.warning("kb_market.bad_json", url=url, error=str(exc))
                            return None
                    if status == 429:
                        delay = _BACKOFF_BASE_S * (2 ** (attempt + 1))
                        log.warning(
                            "kb_market.rate_limited", url=url, attempt=attempt, retry_in=delay
                        )
                        if attempt >= _MAX_RETRIES:
                            return None
                        await asyncio.sleep(delay)
                        continue
                    if 500 <= status < 600:
                        delay = _BACKOFF_BASE_S * (2**attempt)
                        log.warning(
                            "kb_market.server_error",
                            url=url,
                            status=status,
                            attempt=attempt,
                            retry_in=delay,
                        )
                        if attempt >= _MAX_RETRIES:
                            return None
                        await asyncio.sleep(delay)
                        continue
                    log.warning("kb_market.client_error", url=url, status=status)
                    return None
            except (TimeoutError, aiohttp.ClientError) as exc:
                delay = _BACKOFF_BASE_S * (2**attempt)
                log.warning(
                    "kb_market.request_failed",
                    url=url,
                    error=str(exc),
                    attempt=attempt,
                    retry_in=delay,
                )
                if attempt >= _MAX_RETRIES:
                    return None
                await asyncio.sleep(delay)
                continue
        return None

    # ── prices ───────────────────────────────────────────────────────────────

    async def prices(
        self,
        item_ids: list[str],
        cities: list[str] | None = None,
        qualities: list[int] | None = None,
    ) -> list[PriceRow]:
        """Fetch current prices for ``item_ids`` across ``cities`` and ``qualities``.

        ``item_ids`` are AODP unique ids, enchant via ``@1``..``@3``
        (e.g. ``"T8_2H_HOLYSTAFF@3"``). ``cities`` and ``qualities`` fall back to
        the configured defaults when omitted. Results are served from the TTL
        cache when a byte-identical query is repeated within ``cache_ttl_s``.

        Requests are batched under the 4096-char URL cap; a failed batch is
        skipped (logged), so a partial network failure yields the rows that did
        come back rather than nothing. Returns ``[]`` on total give-up — never
        raises (§13). Rows include no-data sentinels; use
        :attr:`PriceRow.has_data` to filter them.
        """
        mkt = self._market_cfg()
        base = self._base_url(mkt.region)
        if base is None:
            return []

        ids = _dedupe(item_ids)
        if not ids:
            return []
        use_cities = _str_list(cities) or mkt.default_cities
        use_qualities = _int_list(qualities) or [mkt.default_quality]

        key = _cache_key(ids, use_cities, use_qualities)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        loc_q = _location_quality_query(use_cities, use_qualities)
        out: list[PriceRow] = []
        for batch in _batch_items(ids):
            item_seg = quote(",".join(batch), safe=",@")
            url = f"{base}/api/v2/stats/prices/{item_seg}.json{loc_q}"
            data = await self._get_json(url, mkt.request_timeout_s, mkt.user_agent)
            for obj in _as_dict_list(data):
                row = _parse_price_row(obj)
                if row is not None:
                    out.append(row)

        self._cache_put(key, out, mkt.cache_ttl_s)
        return out

    # ── history ──────────────────────────────────────────────────────────────

    async def history(
        self,
        item_id: str,
        city: str,
        days: int,
    ) -> list[dict[str, Any]]:
        """Fetch a price-history series for one item in one city over ``days``.

        Chooses a time-scale from the window — hourly for a short window, 6-hour
        for up to a week, daily beyond that — matching the API's ``time-scale``
        (1|6|24). Returns a flat list of points, newest-last as the API orders
        them, each ``{"timestamp": str, "avg_price": int, "item_count": int}``.
        Returns ``[]`` on failure or no data — never raises (§13).
        """
        mkt = self._market_cfg()
        base = self._base_url(mkt.region)
        if base is None:
            return []

        item = _first_str(item_id)
        town = _first_str(city)
        if item is None or town is None:
            return []

        scale = _time_scale_for_days(days)
        item_seg = quote(item, safe="@")
        loc = quote(town, safe="")
        url = (
            f"{base}/api/v2/stats/history/{item_seg}.json"
            f"?locations={loc}&qualities={mkt.default_quality}&time-scale={scale}"
        )
        data = await self._get_json(url, mkt.request_timeout_s, mkt.user_agent)

        points: list[dict[str, Any]] = []
        for series in _as_dict_list(data):
            raw = series.get("data")
            if not isinstance(raw, list):
                continue
            for pt in raw:
                if not isinstance(pt, dict):
                    continue
                ts = _first_str(pt.get("timestamp"))
                if ts is None:
                    continue
                points.append(
                    {
                        "timestamp": ts,
                        "avg_price": _int(pt.get("avg_price")),
                        "item_count": _int(pt.get("item_count")),
                    }
                )
        return points

    # ── gold ─────────────────────────────────────────────────────────────────

    async def gold(self, count: int = 24) -> list[dict[str, Any]]:
        """Fetch the last ``count`` gold price samples (silver per 1 gold).

        Returns a list of ``{"price": int, "timestamp": str}`` newest-last as the
        API orders them, or ``[]`` on failure — never raises (§13).
        """
        mkt = self._market_cfg()
        base = self._base_url(mkt.region)
        if base is None:
            return []

        n = count if count > 0 else 24
        url = f"{base}/api/v2/stats/gold.json?count={n}"
        data = await self._get_json(url, mkt.request_timeout_s, mkt.user_agent)

        out: list[dict[str, Any]] = []
        for obj in _as_dict_list(data):
            ts = _first_str(obj.get("timestamp"))
            if ts is None:
                continue
            out.append({"price": _int(obj.get("price")), "timestamp": ts})
        return out

    # ── cache ────────────────────────────────────────────────────────────────

    def _cache_get(self, key: str) -> list[PriceRow] | None:
        """Return live cached rows for ``key``, or ``None`` if absent/expired.

        Expired entries are evicted on access so the cache does not grow
        unbounded across a long-lived process.
        """
        entry = self._price_cache.get(key)
        if entry is None:
            return None
        expires_at, rows = entry
        if time.monotonic() >= expires_at:
            self._price_cache.pop(key, None)
            return None
        return rows

    def _cache_put(self, key: str, rows: list[PriceRow], ttl_s: int) -> None:
        if ttl_s <= 0:
            return
        self._price_cache[key] = (time.monotonic() + ttl_s, rows)

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the underlying session. Idempotent; safe if never opened."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


# ── resolved config view ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _ResolvedMarket:
    """A snapshot of the market config knobs read at call time."""

    region: str
    cache_ttl_s: int
    request_timeout_s: int
    default_cities: list[str]
    default_quality: int
    user_agent: str


# ── helpers ──────────────────────────────────────────────────────────────────


def _is_epoch(date: str) -> bool:
    """True when ``date`` is the AODP no-data epoch sentinel."""
    return date.startswith("0001-01-01")


def _int(value: Any, default: int = 0) -> int:
    """Best-effort int coercion; junk (None, non-numeric) yields ``default``."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (ValueError, TypeError):
            return default
    return default


def _str(value: Any, default: str = "") -> str:
    """A stripped string view of ``value`` (``default`` when None/blank)."""
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _first_str(value: Any) -> str | None:
    """A non-empty stripped string, or ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _str_list(value: Any) -> list[str]:
    """Coerce a list-ish (or comma string) into a clean list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, list | tuple | set):
        parts = list(value)
    else:
        return []
    out: list[str] = []
    for p in parts:
        s = _first_str(p)
        if s is not None:
            out.append(s)
    return out


def _int_list(value: Any) -> list[int]:
    """Coerce a list-ish of quality levels into a clean list of ints."""
    if value is None:
        return []
    items = list(value) if isinstance(value, list | tuple | set) else [value]
    out: list[int] = []
    for it in items:
        n = _int(it, default=-1)
        if n >= 0:
            out.append(n)
    return out


def _dedupe(item_ids: list[str]) -> list[str]:
    """De-duplicate item ids, preserving first-seen order and dropping blanks."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in item_ids:
        s = _first_str(raw)
        if s is None or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _batch_items(ids: list[str]) -> list[list[str]]:
    """Split ids into batches under the URL-length and per-batch caps.

    Keeps each batch's comma-joined length under :data:`_MAX_ITEMIDS_CHARS`
    (leaving room for host, path, and query within the API's 4096-char URL
    limit) and at most :data:`_MAX_ITEMS_PER_BATCH` ids.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    length = 0
    for item in ids:
        add = len(item) + (1 if current else 0)
        too_long = length + add > _MAX_ITEMIDS_CHARS
        too_many = len(current) >= _MAX_ITEMS_PER_BATCH
        if current and (too_long or too_many):
            batches.append(current)
            current = []
            length = 0
            add = len(item)
        current.append(item)
        length += add
    if current:
        batches.append(current)
    return batches


def _location_quality_query(cities: list[str], qualities: list[int]) -> str:
    """Build the ``?locations=..&qualities=..`` suffix (empty parts omitted)."""
    params: list[str] = []
    if cities:
        params.append("locations=" + quote(",".join(cities), safe=","))
    if qualities:
        params.append("qualities=" + ",".join(str(q) for q in qualities))
    return ("?" + "&".join(params)) if params else ""


def _cache_key(ids: list[str], cities: list[str], qualities: list[int]) -> str:
    """A normalized, order-independent key for the price cache."""
    return "|".join(
        (
            ",".join(sorted(ids)),
            ",".join(sorted(c.casefold() for c in cities)),
            ",".join(str(q) for q in sorted(qualities)),
        )
    )


def _time_scale_for_days(days: int) -> int:
    """Map a history window in days to the API ``time-scale`` (1|6|24)."""
    if days <= 2:
        return 1
    if days <= 7:
        return 6
    return 24


def _parse_price_row(obj: dict[str, Any]) -> PriceRow | None:
    """Parse one AODP price object into a :class:`PriceRow`, tolerantly.

    Drops only rows with no usable ``item_id`` or ``city`` (nothing to key on);
    all price/date fields degrade to ``0`` / ``""`` rather than raising.
    """
    item_id = _first_str(obj.get("item_id"))
    city = _first_str(obj.get("city"))
    if item_id is None or city is None:
        return None
    return PriceRow(
        item_id=item_id,
        city=city,
        quality=_int(obj.get("quality"), default=1),
        sell_min=_int(obj.get("sell_price_min")),
        sell_min_date=_str(obj.get("sell_price_min_date")),
        sell_max=_int(obj.get("sell_price_max")),
        sell_max_date=_str(obj.get("sell_price_max_date")),
        buy_min=_int(obj.get("buy_price_min")),
        buy_min_date=_str(obj.get("buy_price_min_date")),
        buy_max=_int(obj.get("buy_price_max")),
        buy_max_date=_str(obj.get("buy_price_max_date")),
    )


def _as_dict_list(data: Any) -> list[dict[str, Any]]:
    """Coerce a parsed response into a list of dicts, tolerating junk (§2.4)."""
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


__all__ = [
    "AODP_HOSTS",
    "MarketClient",
    "PriceRow",
]
