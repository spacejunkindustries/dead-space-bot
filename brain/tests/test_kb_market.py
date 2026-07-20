"""Pins the real behaviour of the killboard market-data layer.

No network, no Discord, no real files. The :class:`aiohttp` seam is faked with a
stub session that returns canned JSON in the exact field shapes the AODP API
returns (verified live against the EAST server), so these tests lock:

* :class:`killboard.market.MarketClient` — ``prices()`` parses the confirmed
  ``item_id``/``city``/``quality``/``sell_price_*``/``buy_price_*`` fields; a
  ``0`` price on the ``0001-01-01`` epoch date is treated as *no data*
  (``has_data`` False), never a real ``0``; the TTL cache serves a repeated
  query without a second ``session.get``; a give-up (the session raises) returns
  ``[]`` rather than propagating; and the host is the region's
  ``.albion-online-data.com`` one.
* :class:`killboard.items.ItemIndex` — id/name resolve, enchant reattach, and
  autocomplete search over a hand-written items.txt snippet.
* :mod:`killboard.value` — pure item extraction + aggregation, and value summing
  against a fake price map (no-data filtered, quality-1 fall-back).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from killboard import market, value
from killboard.items import ItemIndex
from killboard.market import AODP_HOSTS, MarketClient, PriceRow

# ── the confirmed AODP price-object shapes (verbatim from the EAST server) ─────

# The T4_BAG example given in the API spec: real sell data, no buy data.
_BAG_ROW: dict[str, Any] = {
    "item_id": "T4_BAG",
    "city": "Caerleon",
    "quality": 1,
    "sell_price_min": 5999,
    "sell_price_min_date": "2026-07-19T21:30:00",
    "sell_price_max": 6444,
    "sell_price_max_date": "2026-07-19T21:30:00",
    "buy_price_min": 0,
    "buy_price_min_date": "0001-01-01T00:00:00",
    "buy_price_max": 0,
    "buy_price_max_date": "0001-01-01T00:00:00",
}

# A row with NO data at all: every price 0, every date the .NET epoch sentinel.
_NO_DATA_ROW: dict[str, Any] = {
    "item_id": "T8_2H_HOLYSTAFF",
    "city": "Caerleon",
    "quality": 1,
    "sell_price_min": 0,
    "sell_price_min_date": "0001-01-01T00:00:00",
    "sell_price_max": 0,
    "sell_price_max_date": "0001-01-01T00:00:00",
    "buy_price_min": 0,
    "buy_price_min_date": "0001-01-01T00:00:00",
    "buy_price_max": 0,
    "buy_price_max_date": "0001-01-01T00:00:00",
}

# A junk row with no usable item_id: must be dropped, never crash the parse.
_JUNK_ROW: dict[str, Any] = {"city": "Caerleon", "quality": 1, "sell_price_min": 42}


# ── the aiohttp seam ──────────────────────────────────────────────────────────


class _FakeResp:
    """A stand-in for an aiohttp response, used only as an async context manager."""

    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> _FakeResp:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def json(self, content_type: Any = None) -> Any:  # noqa: ARG002
        return self._payload


class _FakeSession:
    """A canned :class:`aiohttp.ClientSession`.

    ``get(url, ...)`` records the URL and returns the same canned payload for
    every call (one batch → one call in these tests). When ``raise_exc`` is set,
    every ``get`` raises it — the give-up path the retry loop must swallow.
    """

    def __init__(self, payload: Any = None, *, raise_exc: BaseException | None = None) -> None:
        self.closed = False
        self.get_urls: list[str] = []
        self._payload = payload
        self._raise = raise_exc

    def get(self, url: str, *, timeout: Any = None, **_kw: Any) -> _FakeResp:  # noqa: ARG002
        self.get_urls.append(url)
        if self._raise is not None:
            raise self._raise
        return _FakeResp(200, self._payload)

    async def close(self) -> None:
        self.closed = True


def _make_cfg(
    *,
    region: str = "east",
    cache_ttl_s: int = 300,
    default_cities: list[str] | None = None,
) -> SimpleNamespace:
    """A minimal live-config stand-in exposing ``.region`` and ``.market``.

    ``cfg.market`` is wired in separately in production; the client reads it with
    ``getattr`` fallbacks, so this SimpleNamespace is enough to exercise it.
    """
    return SimpleNamespace(
        region=region,
        market=SimpleNamespace(
            cache_ttl_s=cache_ttl_s,
            request_timeout_s=10,
            default_cities=default_cities if default_cities is not None else ["Caerleon"],
            default_quality=1,
            user_agent="DeadKillboard-test/1.0",
        ),
    )


def _client_with_session(session: _FakeSession, cfg: SimpleNamespace | None = None) -> MarketClient:
    """A :class:`MarketClient` whose session is pre-seeded, so no real socket
    is ever opened (``_get_session`` returns the live, non-closed fake)."""
    client = MarketClient(lambda: cfg if cfg is not None else _make_cfg())
    client._session = session  # type: ignore[assignment]
    return client


# ── prices(): parsing + no-data sentinel ──────────────────────────────────────


async def test_prices_parses_confirmed_fields() -> None:
    """``prices()`` maps the confirmed AODP object onto :class:`PriceRow`."""
    session = _FakeSession([_BAG_ROW])
    client = _client_with_session(session)

    rows = await client.prices(["T4_BAG"])

    assert len(rows) == 1
    row = rows[0]
    assert row.item_id == "T4_BAG"
    assert row.city == "Caerleon"
    assert row.quality == 1
    assert row.sell_min == 5999
    assert row.sell_min_date == "2026-07-19T21:30:00"
    assert row.sell_max == 6444
    assert row.buy_min == 0
    assert row.buy_max == 0
    assert row.buy_max_date == "0001-01-01T00:00:00"


async def test_prices_no_data_row_is_unknown_not_zero() -> None:
    """A 0/epoch row parses (never dropped for its zeros) but reads has_data=False
    so it renders as 'unknown', while a real row reads has_data=True."""
    session = _FakeSession([_BAG_ROW, _NO_DATA_ROW, _JUNK_ROW])
    client = _client_with_session(session)

    rows = await client.prices(["T4_BAG", "T8_2H_HOLYSTAFF"])

    # The junk row (no item_id) is dropped; the two real objects survive.
    by_id = {r.item_id: r for r in rows}
    assert set(by_id) == {"T4_BAG", "T8_2H_HOLYSTAFF"}
    assert by_id["T4_BAG"].has_data is True
    assert by_id["T8_2H_HOLYSTAFF"].has_data is False


def test_price_row_has_data_epoch_zero_is_false() -> None:
    """Unit-pins the sentinel directly: 0 silver on the epoch date is not data;
    a positive price on a real date is; a positive price on the epoch is not."""
    epoch = "0001-01-01T00:00:00"
    unknown = PriceRow("X", "Caerleon", 1, 0, epoch, 0, epoch, 0, epoch, 0, epoch)
    known = PriceRow(
        "X",
        "Caerleon",
        1,
        500,
        "2026-07-19T21:30:00",
        600,
        "2026-07-19T21:30:00",
        0,
        epoch,
        0,
        epoch,
    )
    stale = PriceRow("X", "Caerleon", 1, 500, epoch, 0, epoch, 0, epoch, 0, epoch)
    assert unknown.has_data is False
    assert known.has_data is True
    assert stale.has_data is False


# ── prices(): TTL cache ───────────────────────────────────────────────────────


async def test_cache_hit_avoids_second_get() -> None:
    """A byte-identical query within the TTL is served from the in-memory cache;
    the second call must NOT touch the network (rate-limit etiquette, §13)."""
    session = _FakeSession([_BAG_ROW])
    client = _client_with_session(session)

    first = await client.prices(["T4_BAG"], cities=["Caerleon"], qualities=[1])
    second = await client.prices(["T4_BAG"], cities=["Caerleon"], qualities=[1])

    assert len(session.get_urls) == 1  # only the first call hit the wire
    assert second == first  # same rows returned from cache


async def test_cache_key_is_order_independent() -> None:
    """The cache key normalises id/city/quality order, so a reordered but
    equivalent query is a cache hit, not a fresh fetch."""
    session = _FakeSession([_BAG_ROW, _NO_DATA_ROW])
    client = _client_with_session(session)

    await client.prices(["T4_BAG", "T8_2H_HOLYSTAFF"], cities=["Caerleon"], qualities=[1])
    await client.prices(["T8_2H_HOLYSTAFF", "T4_BAG"], cities=["Caerleon"], qualities=[1])

    assert len(session.get_urls) == 1


# ── prices(): give-up on a raising session ────────────────────────────────────


async def test_prices_give_up_returns_empty_not_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the session raises on every attempt, ``prices()`` returns ``[]`` and
    never propagates — a flaky market costs freshness, never a crash (§13)."""

    async def _no_sleep(*_a: Any, **_k: Any) -> None:
        return None

    # Keep the retry/backoff loop instant rather than sleeping real seconds.
    monkeypatch.setattr(market.asyncio, "sleep", _no_sleep)

    session = _FakeSession(raise_exc=aiohttp.ClientError("connection reset"))
    client = _client_with_session(session)

    rows = await client.prices(["T4_BAG"])

    assert rows == []
    # attempt 0 + _MAX_RETRIES retries were all tried before giving up.
    assert len(session.get_urls) == market._MAX_RETRIES + 1


class _FlakySession(_FakeSession):
    """A session that raises for its first ``fail_first`` ``get`` calls, then
    returns the canned payload — models AODP recovering after a transient blip."""

    def __init__(self, payload: Any, *, fail_first: int) -> None:
        super().__init__(payload)
        self._fail_first = fail_first

    def get(self, url: str, *, timeout: Any = None, **_kw: Any) -> _FakeResp:  # noqa: ARG002
        self.get_urls.append(url)
        if len(self.get_urls) <= self._fail_first:
            raise aiohttp.ClientError("transient")
        return _FakeResp(200, self._payload)


async def test_give_up_is_not_cached_so_recovery_refetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A total fetch failure must NOT poison the cache: once AODP recovers the
    next call fetches again rather than serving the empty give-up for the TTL."""

    async def _no_sleep(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(market.asyncio, "sleep", _no_sleep)

    # First query series (attempt 0 + _MAX_RETRIES) all raise → give-up → [].
    session = _FlakySession([_BAG_ROW], fail_first=market._MAX_RETRIES + 1)
    client = _client_with_session(session)

    first = await client.prices(["T4_BAG"])
    assert first == []  # gave up
    assert "" not in client._price_cache  # nothing cached
    assert client._price_cache == {}  # the empty give-up was NOT stored

    # AODP is back now; the next call must hit the wire again and get real data.
    second = await client.prices(["T4_BAG"])
    assert len(second) == 1
    assert second[0].item_id == "T4_BAG"


async def test_empty_but_successful_response_is_cached() -> None:
    """A genuinely-empty *successful* response is still cached (it is not a
    failure) — only give-ups are withheld from the cache."""
    session = _FakeSession([])  # 200 OK, empty array
    client = _client_with_session(session)

    first = await client.prices(["T4_BAG"])
    second = await client.prices(["T4_BAG"])

    assert first == [] and second == []
    assert len(session.get_urls) == 1  # second served from cache


async def test_cache_key_includes_region_no_cross_region_bleed() -> None:
    """A price query is cached per region: an identical query after a region hot
    reload re-fetches (the previous region's rows must not leak across)."""
    session = _FakeSession([_BAG_ROW])
    cfg = _make_cfg(region="east")
    client = _client_with_session(session, cfg=cfg)

    await client.prices(["T4_BAG"], cities=["Caerleon"], qualities=[1])
    assert len(session.get_urls) == 1

    # Hot-reload the region on the same live client/cache.
    cfg.region = "west"
    await client.prices(["T4_BAG"], cities=["Caerleon"], qualities=[1])

    # Different region ⇒ different key ⇒ a fresh fetch (no stale-region hit).
    assert len(session.get_urls) == 2
    assert session.get_urls[1].startswith("https://west.albion-online-data.com")


async def test_price_cache_is_bounded() -> None:
    """The TTL cache never grows past the cap even under a stream of distinct,
    never-repeated queries (one per unique loadout on the feed path)."""
    session = _FakeSession([_BAG_ROW])
    client = _client_with_session(session)

    for i in range(market._MAX_CACHE_ENTRIES + 50):
        await client.prices([f"T4_ITEM_{i}"])

    assert len(client._price_cache) <= market._MAX_CACHE_ENTRIES


# ── host: the AODP .albion-online-data.com host per region ────────────────────


async def test_prices_hits_the_region_aodp_host() -> None:
    """The URL is built from the region's AODP host, NOT the gameinfo host."""
    session = _FakeSession([_BAG_ROW])
    client = _client_with_session(session, cfg=_make_cfg(region="east"))

    await client.prices(["T4_BAG"])

    assert len(session.get_urls) == 1
    url = session.get_urls[0]
    assert url.startswith("https://east.albion-online-data.com/api/v2/stats/prices/")
    assert "albion-online-data.com" in url
    assert "T4_BAG" in url
    assert "locations=Caerleon" in url


def test_aodp_hosts_mapping() -> None:
    """The three regional hosts are the AODP ones, keyed by cfg.killboard.region."""
    assert AODP_HOSTS == {
        "west": "https://west.albion-online-data.com",
        "europe": "https://europe.albion-online-data.com",
        "east": "https://east.albion-online-data.com",
    }


async def test_bad_region_returns_empty_and_no_fetch() -> None:
    """An unknown region yields ``[]`` and never touches the network."""
    session = _FakeSession([_BAG_ROW])
    client = _client_with_session(session, cfg=_make_cfg(region="mars"))

    rows = await client.prices(["T4_BAG"])

    assert rows == []
    assert session.get_urls == []


# ── ItemIndex: resolve / search ───────────────────────────────────────────────

_ITEMS_TXT = """
  1: T4_BAG : Adept's Bag
  2: T5_BAG : Expert's Bag
  3: T8_2H_HOLYSTAFF : Elder's Great Holy Staff
  T3_2H_TOOL_TRACKING : Journeyman's Tracking Toolkit
"""


def _index() -> ItemIndex:
    return ItemIndex.from_text(_ITEMS_TXT)


def test_item_resolve_exact_id() -> None:
    """An exact id resolves case-insensitively to its canonical form."""
    idx = _index()
    assert idx.resolve("T4_BAG") == "T4_BAG"
    assert idx.resolve("t8_2h_holystaff") == "T8_2H_HOLYSTAFF"


def test_item_resolve_enchant_reattached() -> None:
    """An ``@n`` enchant is split off, matched against the base id, and reattached
    to the resolved result (the AODP API accepts any enchant)."""
    idx = _index()
    assert idx.resolve("t8_2h_holystaff@3") == "T8_2H_HOLYSTAFF@3"
    assert idx.resolve("holy staff@2") == "T8_2H_HOLYSTAFF@2"


def test_item_resolve_fuzzy_name() -> None:
    """A localized-name query resolves to the item id via core-name matching."""
    idx = _index()
    assert idx.resolve("great holy staff") == "T8_2H_HOLYSTAFF"
    # A tie between the two bags breaks deterministically toward the lower tier.
    assert idx.resolve("bag") == "T4_BAG"


def test_item_resolve_tier_hint_biases() -> None:
    """A tier word in the query biases the fuzzy match toward that tier."""
    idx = _index()
    assert idx.resolve("expert bag") == "T5_BAG"


def test_item_resolve_unknown_is_none() -> None:
    """A query far from every entry resolves to ``None``, not a wild best-effort."""
    idx = _index()
    assert idx.resolve("dreadnought battleship hull") is None
    assert idx.resolve("") is None


def test_item_name_of() -> None:
    """``name_of`` returns the display name, falling back to the base for @n ids."""
    idx = _index()
    assert idx.name_of("T4_BAG") == "Adept's Bag"
    assert idx.name_of("T8_2H_HOLYSTAFF@3") == "Elder's Great Holy Staff"
    assert idx.name_of("T9_NONEXISTENT") is None


def test_item_search_offers_candidates() -> None:
    """``search`` returns (id, name) candidates for autocomplete, best first."""
    idx = _index()
    results = idx.search("bag")
    ids = [item_id for item_id, _name in results]
    assert "T4_BAG" in ids
    assert "T5_BAG" in ids
    # Each result carries its display name.
    assert dict(results)["T4_BAG"] == "Adept's Bag"


def test_item_search_empty_query_is_empty() -> None:
    idx = _index()
    assert idx.search("") == []


# ── value: pure item extraction ───────────────────────────────────────────────


def test_items_of_extracts_and_aggregates() -> None:
    """``items_of`` walks worn Equipment then Inventory, skips null holes, and
    sums identical (item_id, quality) lines in first-seen order."""
    event = {
        "Victim": {
            "Equipment": {
                "MainHand": {"Type": "T8_2H_HOLYSTAFF", "Quality": 1},
                "Bag": {"Type": "T4_BAG", "Quality": 3},
                "Head": None,
            },
            "Inventory": [
                {"Type": "T5_POTION", "Count": 3, "Quality": 1},
                None,
                {"Type": "T5_POTION", "Count": 2, "Quality": 1},
                {"Type": "", "Count": 1},
            ],
        }
    }
    triples = value.items_of(event, "victim")
    assert triples == [
        ("T8_2H_HOLYSTAFF", 1, 1),
        ("T4_BAG", 1, 3),
        ("T5_POTION", 5, 1),  # two inventory stacks summed
    ]


def test_items_of_naked_player_is_empty() -> None:
    """A player with no equipment/inventory (or absent entirely) yields ``[]``."""
    assert value.items_of({"Victim": {}}, "victim") == []
    assert value.items_of({}, "killer") == []


# ── value: estimate against a fake price map ──────────────────────────────────


class _FakeMarket:
    """A stand-in for :class:`MarketClient` that returns a canned row list and
    records the ids it was queried for."""

    def __init__(self, rows: list[PriceRow]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, ...]] = []

    async def prices(
        self,
        item_ids: list[str],
        cities: list[str] | None = None,  # noqa: ARG002
        qualities: list[int] | None = None,  # noqa: ARG002
    ) -> list[PriceRow]:
        self.calls.append(tuple(item_ids))
        return list(self._rows)


def _real_row(item_id: str, quality: int, sell_min: int) -> PriceRow:
    good = "2026-07-19T21:30:00"
    return PriceRow(
        item_id,
        "Caerleon",
        quality,
        sell_min,
        good,
        sell_min + 100,
        good,
        0,
        "0001-01-01T00:00:00",
        0,
        "0001-01-01T00:00:00",
    )


def _no_data_row(item_id: str, quality: int) -> PriceRow:
    epoch = "0001-01-01T00:00:00"
    return PriceRow(item_id, "Caerleon", quality, 0, epoch, 0, epoch, 0, epoch, 0, epoch)


async def test_estimate_value_sums_priced_and_falls_back_quality() -> None:
    """``estimate_value`` sums count*reference_price, counts unpriced lines,
    filters no-data rows, and falls back to quality 1 when the exact quality
    never traded."""
    event = {
        "Victim": {
            "Equipment": {
                "MainHand": {"Type": "T8_2H_HOLYSTAFF", "Quality": 1},
                "Bag": {"Type": "T4_BAG", "Quality": 3},  # only q1 priced → fallback
                "Head": {"Type": "T6_HEAD_CLOTH_SET1", "Quality": 1},  # no data → unpriced
            },
            "Inventory": [
                {"Type": "T5_POTION", "Count": 3, "Quality": 1},
                {"Type": "T5_POTION", "Count": 2, "Quality": 1},
            ],
        }
    }
    rows = [
        _real_row("T8_2H_HOLYSTAFF", 1, 1000),
        _real_row("T4_BAG", 1, 500),
        _real_row("T5_POTION", 1, 100),
        _no_data_row("T6_HEAD_CLOTH_SET1", 1),
    ]
    fake = _FakeMarket(rows)

    result = await value.estimate_value(event, fake)  # type: ignore[arg-type]

    # 1000*1 + 500*1 (q3→q1 fallback) + 100*5 = 2000; the head is unpriced.
    assert result["total"] == 2000
    assert result["priced"] == 3
    assert result["unpriced"] == 1

    by_item = {e["item_id"]: e for e in result["by_item"]}
    assert by_item["T4_BAG"]["quality"] == 3
    assert by_item["T4_BAG"]["quality_used"] == 1  # fell back to quality 1
    assert by_item["T5_POTION"]["subtotal"] == 500
    assert by_item["T6_HEAD_CLOTH_SET1"]["priced"] is False
    assert by_item["T6_HEAD_CLOTH_SET1"]["unit_price"] is None


async def test_estimate_value_empty_kit_is_unknown_total() -> None:
    """A naked victim yields total=None ('unknown'), never a misleading 0, and
    never even queries the market."""
    fake = _FakeMarket([])
    result = await value.estimate_value({"Victim": {}}, fake)  # type: ignore[arg-type]
    assert result["total"] is None
    assert result["priced"] == 0
    assert result["by_item"] == []
    assert fake.calls == []  # nothing to price → no lookup


async def test_estimate_value_dead_market_is_all_unpriced() -> None:
    """When the market returns nothing, every line is unpriced and total=None —
    degraded, but never a crash and never a fake 0 (§13)."""
    event = {"Victim": {"Equipment": {"MainHand": {"Type": "T8_2H_HOLYSTAFF", "Quality": 1}}}}
    fake = _FakeMarket([])
    result = await value.estimate_value(event, fake)  # type: ignore[arg-type]
    assert result["total"] is None
    assert result["priced"] == 0
    assert result["unpriced"] == 1


# ── /market notice replies must not leave a dangling public spinner ──────────


class _FakeInteraction:
    """A minimal discord.Interaction stand-in for the /market notice path: a
    command deferred publicly, then a notice is delivered. Records whether the
    public placeholder was deleted and how the followup was sent."""

    def __init__(self, *, delete_raises: bool = False) -> None:
        self.deleted = False
        self.followups: list[tuple[Any, dict[str, Any]]] = []
        self._delete_raises = delete_raises
        self.followup = SimpleNamespace(send=self._send)

    async def delete_original_response(self) -> None:
        if self._delete_raises:
            import discord

            raise discord.HTTPException(SimpleNamespace(status=404, reason="Not Found"), "gone")
        self.deleted = True

    async def _send(self, content: Any = None, **kwargs: Any) -> None:
        self.followups.append((content, kwargs))


async def test_market_notice_deletes_public_placeholder_then_replies_ephemeral() -> None:
    """A /market notice (disabled, item miss, no data) must delete the public
    "thinking…" placeholder the command deferred, then answer ephemerally — else
    the spinner dangles publicly forever, the spam the ephemeral reply avoids."""
    from killboard.market_commands import MarketCog

    inter = _FakeInteraction()
    await MarketCog._text(object(), inter, "Market data is turned off.")  # type: ignore[arg-type]

    assert inter.deleted is True  # public placeholder removed
    assert len(inter.followups) == 1
    _content, kwargs = inter.followups[0]
    assert kwargs.get("ephemeral") is True


async def test_market_notice_tolerates_a_missing_placeholder() -> None:
    """If the placeholder is already gone (delete raises), the notice still goes
    out ephemerally — the delete is best-effort, never fatal."""
    from killboard.market_commands import MarketCog

    inter = _FakeInteraction(delete_raises=True)
    await MarketCog._text(object(), inter, "Couldn't find that item.")  # type: ignore[arg-type]

    assert len(inter.followups) == 1
    assert inter.followups[0][1].get("ephemeral") is True
