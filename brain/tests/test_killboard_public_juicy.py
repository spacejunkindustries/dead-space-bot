"""Pins the public (server-wide) juicy feed — notable kills to the juicy channel.

No real Discord or network: a fake bot + channel, a scripted global-events API,
and a fake market. Locks the load-bearing behaviour: qualify on fame first / loot
second (OR, so a low-fame high-loot gank still posts), in-memory dedup across
scans, and off/no-channel gating.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cortana.config import (
    KbFeedConfig,
    KbMarketConfig,
    KbPublicJuicyConfig,
    KillboardConfig,
)
from killboard.public_juicy import PublicJuicyFeed, _guild_name

JUICY_CHANNEL = 555


class _OkChannel:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        return SimpleNamespace(id=1)


class _Bot:
    def __init__(self, channels: dict[int, Any]) -> None:
        self._channels = channels

    def get_channel(self, cid: int) -> Any:
        return self._channels.get(cid)


class _Cards:
    async def render(self, *_a: Any, **_k: Any) -> bytes | None:
        return None  # embed-only; keeps the test off Pillow


class _Api:
    """Serves one scripted page 0 of global events; deeper pages are empty."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events
        self.calls = 0

    async def global_events(self, limit: int = 51, offset: int = 0) -> list[dict[str, Any]]:
        self.calls += 1
        return list(self._events) if offset == 0 else []


class _Market:
    """Prices every kill's loot at a fixed total (or None)."""

    def __init__(self, total: int | None) -> None:
        self._total = total


async def _inline_to_thread(fn: Any, *a: Any, **k: Any) -> Any:
    return fn(*a, **k)


def _raw_kill(event_id: int, *, fame: int, killer_guild: str = "AAA") -> dict[str, Any]:
    return {
        "EventId": event_id,
        "TimeStamp": "2026-07-20T12:00:00Z",
        "Killer": {"Id": "K", "Name": "Killer", "GuildName": killer_guild},
        "Victim": {"Id": f"V{event_id}", "Name": "Victim", "GuildName": "ZZZ"},
        "TotalVictimKillFame": fame,
    }


def _cfg(
    *,
    enabled: bool = True,
    juicy_channel: int = JUICY_CHANNEL,
    min_fame: int = 2_000_000,
    min_loot: int = 1_000_000,
    market: bool = False,
    scan_pages: int = 1,
    max_posts: int = 5,
    max_priced: int = 60,
) -> SimpleNamespace:
    kb = KillboardConfig(
        feed=KbFeedConfig(
            juicy_channel=juicy_channel, juicy_min_fame=min_fame, juicy_min_loot=min_loot
        ),
        market=KbMarketConfig(enabled=market),
        public_juicy=KbPublicJuicyConfig(
            enabled=enabled,
            scan_pages=scan_pages,
            max_posts_per_scan=max_posts,
            max_priced_per_scan=max_priced,
        ),
    )
    return SimpleNamespace(killboard=kb)


def _feed(
    bot: _Bot, api: _Api, cfg: SimpleNamespace, market: _Market | None = None
) -> PublicJuicyFeed:
    return PublicJuicyFeed(bot, api, _Cards(), market, lambda: cfg, _inline_to_thread)  # type: ignore[arg-type]


# ── qualify on fame (free, no market) ────────────────────────────────────────


async def test_high_fame_kill_posts_without_market() -> None:
    """A kill over the fame bar posts even with the market off (fame is free)."""
    channel = _OkChannel()
    api = _Api([_raw_kill(1, fame=3_000_000), _raw_kill(2, fame=100)])
    feed = _feed(_Bot({JUICY_CHANNEL: channel}), api, _cfg(market=False))

    await feed._scan_once()

    assert len(channel.sent) == 1  # only the 3M-fame kill; the 100-fame one is skipped


async def test_low_fame_high_loot_gank_posts_via_loot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The classic juicy gank — low fame, high loot — must post via the loot bar
    (proving OR, not AND)."""
    channel = _OkChannel()
    api = _Api([_raw_kill(1, fame=50)])  # far below the 2M fame bar

    async def _priced(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"total": 1_500_000}  # 1.5M loot, above the 1M loot bar

    monkeypatch.setattr("killboard.public_juicy.estimate_value", _priced)
    feed = _feed(_Bot({JUICY_CHANNEL: channel}), api, _cfg(market=True), _Market(1_500_000))

    await feed._scan_once()

    assert len(channel.sent) == 1


async def test_low_fame_low_loot_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Below both bars → not posted, and remembered so it isn't re-priced."""
    channel = _OkChannel()
    api = _Api([_raw_kill(1, fame=50)])

    async def _cheap(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"total": 10_000}

    monkeypatch.setattr("killboard.public_juicy.estimate_value", _cheap)
    feed = _feed(_Bot({JUICY_CHANNEL: channel}), api, _cfg(market=True), _Market(10_000))

    await feed._scan_once()

    assert channel.sent == []
    assert 1 in feed._seen  # remembered, so the next scan won't re-price it


async def test_cap_limits_posts_per_scan_to_the_biggest() -> None:
    """The hard cap is the volume control: with many qualifiers, only the top-N
    (by value) post — a low threshold on the global firehose can't flood."""
    channel = _OkChannel()
    # Six kills all clear the 2M fame bar; cap is 2 → only two post.
    api = _Api([_raw_kill(i, fame=2_000_000 + i) for i in range(1, 7)])
    feed = _feed(_Bot({JUICY_CHANNEL: channel}), api, _cfg(market=False, max_posts=2))

    await feed._scan_once()

    assert len(channel.sent) == 2  # capped, not all six
    # All six were still evaluated + remembered, so the next scan re-posts nothing.
    assert len(feed._seen) == 6
    channel.sent.clear()
    await feed._scan_once()
    assert channel.sent == []


async def test_pricing_budget_caps_market_lookups_per_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-scan loot-pricing budget bounds AODP calls: only max_priced sub-fame
    events are priced, so the global firehose can't drive ~100 lookups per scan."""
    calls = {"n": 0}

    async def _counting(*_a: Any, **_k: Any) -> dict[str, Any]:
        calls["n"] += 1
        return {"total": 20_000_000}  # every priced event clears the loot bar

    monkeypatch.setattr("killboard.public_juicy.estimate_value", _counting)
    # 8 sub-fame kills (below the 2M fame bar), market on, budget 3.
    api = _Api([_raw_kill(i, fame=100) for i in range(1, 9)])
    channel = _OkChannel()
    feed = _feed(
        _Bot({JUICY_CHANNEL: channel}),
        api,
        _cfg(market=True, max_priced=3, max_posts=5),
        _Market(0),
    )

    await feed._scan_once()

    assert calls["n"] == 3  # priced exactly the budget, not all 8
    assert len(channel.sent) == 3  # the 3 priced (all clear the loot bar)
    assert len(feed._seen) == 8  # all 8 still remembered (won't re-price next scan)


async def test_fame_only_when_pricing_budget_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_priced_per_scan=0 makes the public feed fame-only — zero AODP calls."""
    calls = {"n": 0}

    async def _counting(*_a: Any, **_k: Any) -> dict[str, Any]:
        calls["n"] += 1
        return {"total": 20_000_000}

    monkeypatch.setattr("killboard.public_juicy.estimate_value", _counting)
    api = _Api([_raw_kill(1, fame=100), _raw_kill(2, fame=3_000_000)])  # one sub-fame, one fame-hit
    channel = _OkChannel()
    feed = _feed(_Bot({JUICY_CHANNEL: channel}), api, _cfg(market=True, max_priced=0), _Market(0))

    await feed._scan_once()

    assert calls["n"] == 1  # only the fame-hit is priced (for display); no sub-fame pricing
    assert len(channel.sent) == 1  # just the 3M-fame kill


async def test_dedup_across_scans() -> None:
    """A kill posted on one scan is not re-posted on the next (in-memory dedup)."""
    channel = _OkChannel()
    api = _Api([_raw_kill(1, fame=3_000_000)])
    feed = _feed(_Bot({JUICY_CHANNEL: channel}), api, _cfg(market=False))

    await feed._scan_once()
    await feed._scan_once()

    assert len(channel.sent) == 1  # still just one


async def test_disabled_does_nothing() -> None:
    channel = _OkChannel()
    api = _Api([_raw_kill(1, fame=9_000_000)])
    feed = _feed(_Bot({JUICY_CHANNEL: channel}), api, _cfg(enabled=False))

    await feed._scan_once()

    assert channel.sent == []
    assert api.calls == 0  # off → never even hits the API


async def test_no_channel_does_nothing() -> None:
    channel = _OkChannel()
    api = _Api([_raw_kill(1, fame=9_000_000)])
    feed = _feed(_Bot({JUICY_CHANNEL: channel}), api, _cfg(juicy_channel=0))

    await feed._scan_once()

    assert channel.sent == []
    assert api.calls == 0


def test_guild_name_helper() -> None:
    assert _guild_name({"GuildName": "DEAD Renegadez"}) == "DEAD Renegadez"
    assert _guild_name({"GuildName": ""}) is None
    assert _guild_name({}) is None
    assert _guild_name(None) is None
