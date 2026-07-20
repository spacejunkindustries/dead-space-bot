"""Pins the feed's exactly-once + never-drop guarantees (killboard GDD §7.3).

No real Discord: fake channels stand in for ``bot.get_channel``. The load-bearing
case is a transient send failure — a passing Discord 5xx/429 must DEFER the event
(leave it unposted for the next drain), never silently mark it posted and drop the
kill. A real in-memory :class:`KbStore` backs the ``posted`` bookkeeping.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from cortana.config import KbFeedConfig, KillboardConfig
from cortana.core import db
from killboard.feed import Feed, _is_backfill_death, _PostResult
from killboard.model import DEATH, KILL, EventRow
from killboard.store import MIGRATIONS_DIR, KbStore

KILLS_CHANNEL = 111
DEATHS_CHANNEL = 222


class _FailChannel:
    """A resolvable channel whose send always raises transiently (5xx/429)."""

    async def send(self, **_kwargs: Any) -> Any:
        raise discord.DiscordException("rate limited")


class _OkChannel:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        return SimpleNamespace(id=999)


class _Bot:
    def __init__(self, channels: dict[int, Any]) -> None:
        self._channels = channels

    def get_channel(self, cid: int) -> Any:
        return self._channels.get(cid)


class _Cards:
    def __init__(self) -> None:
        #: records the last render() call so tests can assert the feed hands the
        #: parsed raw event (with equipment) to the card — the gear-grid fix.
        self.last_kwargs: dict[str, Any] | None = None

    async def render(self, row: EventRow, parts: list[Any], **kwargs: Any) -> bytes | None:
        self.last_kwargs = kwargs
        return None  # embed-only; keeps the test off Pillow


async def _inline_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
    return fn(*args, **kwargs)


def _feed(bot: _Bot, store: KbStore) -> Feed:
    cfg = SimpleNamespace(killboard=KillboardConfig(feed=KbFeedConfig(kills_channel=KILLS_CHANNEL)))
    return Feed(bot, store, _Cards(), lambda: cfg, _inline_to_thread)  # type: ignore[arg-type]


@pytest.fixture
def store() -> KbStore:
    conn: sqlite3.Connection = db.connect(":memory:")
    db.migrate(conn, MIGRATIONS_DIR)
    return KbStore(conn)


def _kill_row(event_id: int = 1) -> EventRow:
    return EventRow(
        event_id=event_id,
        timestamp="2026-07-20T12:00:00Z",
        killer_id="K",
        killer_name="Killer",
        killer_guild_id="G1",
        killer_ip=1000.0,
        victim_id="V",
        victim_name="Victim",
        victim_guild_id="OTHER",
        victim_ip=900.0,
        total_fame=5000,
        relation=KILL,
        num_participants=1,
        battle_id=None,
        location="Somewhere",
    )


def _death_row(event_id: int, *, minutes_ago: float, timestamp: str | None = None) -> EventRow:
    """A DEATH row (tracked guild is the victim). ``minutes_ago`` sets the
    timestamp relative to real now unless ``timestamp`` is given explicitly."""
    from datetime import UTC, datetime, timedelta

    ts = (
        timestamp
        if timestamp is not None
        else (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()
    )
    return EventRow(
        event_id=event_id,
        timestamp=ts,
        killer_id="K",
        killer_name="Ganker",
        killer_guild_id="OTHER",
        killer_ip=1000.0,
        victim_id="V",
        victim_name="Member",
        victim_guild_id="G1",
        victim_ip=900.0,
        total_fame=5000,
        relation=DEATH,
        num_participants=1,
        battle_id=None,
        location="Somewhere",
    )


def _death_feed(bot: _Bot, store: KbStore) -> Feed:
    cfg = SimpleNamespace(
        killboard=KillboardConfig(feed=KbFeedConfig(deaths_channel=DEATHS_CHANNEL))
    )
    return Feed(bot, store, _Cards(), lambda: cfg, _inline_to_thread)  # type: ignore[arg-type]


async def test_transient_send_failure_defers_and_never_marks_posted(store: KbStore) -> None:
    """A resolvable channel that refuses transiently must DEFER — the event stays
    unposted so the next drain retries it, instead of being marked posted and
    silently dropped (the §7.3 never-drop invariant)."""
    row = _kill_row(1)
    store.upsert_event(row, "{}")
    feed = _feed(_Bot({KILLS_CHANNEL: _FailChannel()}), store)

    result = await feed._post_one(row)

    assert result is _PostResult.DEFERRED
    assert store.count_unposted() == 1  # NOT marked posted — retryable


async def test_successful_send_marks_posted_once(store: KbStore) -> None:
    row = _kill_row(2)
    store.upsert_event(row, "{}")
    channel = _OkChannel()
    feed = _feed(_Bot({KILLS_CHANNEL: channel}), store)

    result = await feed._post_one(row)

    assert result is _PostResult.SENT
    assert len(channel.sent) == 1
    # Every send is non-pinging (constraint 11).
    assert channel.sent[0]["allowed_mentions"] is not None
    assert store.count_unposted() == 0  # recorded posted exactly once


async def test_slow_market_loot_value_times_out_not_hangs(
    store: KbStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow-but-healthy AODP must never serialize latency onto the feed: the
    loot-value lookup is bounded and degrades to None (unknown) past its deadline
    instead of stalling the post (§7.3 timeliness)."""
    import asyncio

    from killboard import feed as feed_mod

    async def _hang(*_a: Any, **_k: Any) -> dict[str, Any]:
        await asyncio.sleep(60)  # slower than the deadline
        return {"total": 123}

    monkeypatch.setattr(feed_mod, "estimate_value", _hang)
    monkeypatch.setattr(feed_mod, "_LOOT_VALUE_TIMEOUT_S", 0.05)

    cfg = SimpleNamespace(
        killboard=SimpleNamespace(market=SimpleNamespace(enabled=True)),
    )
    feed = Feed(
        _Bot({}),
        store,
        _Cards(),
        lambda: cfg,  # type: ignore[arg-type]
        _inline_to_thread,
        market=object(),  # non-None so the value path is taken
    )

    value = await feed._loot_value({"Victim": {}})

    assert value is None  # timed out → unknown, not a hang


async def test_missing_channel_is_skipped_not_deferred(store: KbStore) -> None:
    """A structurally-absent channel (deleted / bad id) is marked posted so it
    can't wedge the backlog forever — distinct from a transient failure."""
    row = _kill_row(3)
    store.upsert_event(row, "{}")
    feed = _feed(_Bot({}), store)  # get_channel returns None

    result = await feed._post_one(row)

    assert result is _PostResult.SKIPPED
    assert store.count_unposted() == 0


class _ForbiddenChannel:
    """A resolvable channel whose send raises a PERMANENT 403 (bot lacks Send
    Messages) — the case that must SKIP, not DEFER."""

    async def send(self, **_kwargs: Any) -> Any:
        raise discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"
        )


class _BadRequestChannel:
    """A resolvable channel whose send raises a PERMANENT 400 (e.g. a malformed /
    oversized embed) — also must SKIP, not DEFER."""

    async def send(self, **_kwargs: Any) -> Any:
        raise discord.HTTPException(
            SimpleNamespace(status=400, reason="Bad Request"), "Invalid Form Body"
        )


async def test_permanent_forbidden_is_skipped_not_deferred(store: KbStore) -> None:
    """A 403 (missing Send Messages) on the only target must SKIP (marked posted)
    — NOT defer. Deferring a permanent error would re-attempt this same oldest
    event every drain and wedge every newer kill behind it forever."""
    row = _kill_row(4)
    store.upsert_event(row, "{}")
    feed = _feed(_Bot({KILLS_CHANNEL: _ForbiddenChannel()}), store)

    result = await feed._post_one(row)

    assert result is _PostResult.SKIPPED
    assert store.count_unposted() == 0  # marked posted → cannot wedge the backlog


async def test_permanent_bad_request_is_skipped_not_deferred(store: KbStore) -> None:
    """A 400 (malformed embed) is permanent too — SKIP, don't defer."""
    row = _kill_row(5)
    store.upsert_event(row, "{}")
    feed = _feed(_Bot({KILLS_CHANNEL: _BadRequestChannel()}), store)

    result = await feed._post_one(row)

    assert result is _PostResult.SKIPPED
    assert store.count_unposted() == 0


async def test_server_5xx_still_defers(store: KbStore) -> None:
    """A 500 is transient — it must still DEFER (retry next drain), so the
    permanent/transient split didn't break the never-drop guarantee."""

    class _ServerErrorChannel:
        async def send(self, **_kwargs: Any) -> Any:
            raise discord.HTTPException(
                SimpleNamespace(status=503, reason="Service Unavailable"), "try later"
            )

    row = _kill_row(6)
    store.upsert_event(row, "{}")
    feed = _feed(_Bot({KILLS_CHANNEL: _ServerErrorChannel()}), store)

    result = await feed._post_one(row)

    assert result is _PostResult.DEFERRED
    assert store.count_unposted() == 1  # NOT marked posted — retryable


async def test_feed_passes_raw_event_with_equipment_to_card(store: KbStore) -> None:
    """The card must receive the PARSED raw event (with Victim/Killer Equipment),
    not the bare EventRow — the flat row carries no raw_json, so without this the
    gear grid renders empty. Regression for the empty-loadout kill card."""
    import json

    row = _kill_row(7)
    raw = {
        "Victim": {"Name": "Victim", "Equipment": {"MainHand": {"Type": "T4_MAIN_SWORD@1"}}},
        "Killer": {"Name": "Killer", "Equipment": {"MainHand": {"Type": "T6_2H_AXE"}}},
    }
    store.upsert_event(row, json.dumps(raw))

    cards = _Cards()
    cfg = SimpleNamespace(killboard=KillboardConfig(feed=KbFeedConfig(kills_channel=KILLS_CHANNEL)))
    channel = _OkChannel()
    feed = Feed(_Bot({KILLS_CHANNEL: channel}), store, cards, lambda: cfg, _inline_to_thread)

    await feed._post_one(row)

    assert cards.last_kwargs is not None
    passed = cards.last_kwargs.get("raw_event")
    assert passed is not None, "feed did not hand the raw event to the card"
    # The equipment the gear grid needs is present in what the card received.
    assert passed["Victim"]["Equipment"]["MainHand"]["Type"] == "T4_MAIN_SWORD@1"
    assert passed["Killer"]["Equipment"]["MainHand"]["Type"] == "T6_2H_AXE"


async def test_juicy_by_loot_routes_low_fame_high_loot_kill(
    store: KbStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A low-FAME kill with high LOOT value mirrors to the juicy channel even
    though it never meets juicy_min_fame — the low-fame/high-loot gank."""
    import json

    from killboard import feed as feed_mod

    JUICY = 222
    row = _kill_row(9)  # relation KILL, total_fame=5000 (well below juicy_min_fame)
    store.upsert_event(row, json.dumps({"Victim": {"Name": "V"}, "Killer": {"Name": "K"}}))

    async def _high_loot(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"total": 1_800_000}

    monkeypatch.setattr(feed_mod, "estimate_value", _high_loot)

    kills, juicy = _OkChannel(), _OkChannel()
    cfg = SimpleNamespace(
        killboard=SimpleNamespace(
            feed=KbFeedConfig(
                kills_channel=KILLS_CHANNEL,
                juicy_channel=JUICY,
                juicy_min_fame=2_000_000,  # fame gate the kill can't meet
                juicy_min_loot=1_000_000,  # loot gate it clears
            ),
            market=SimpleNamespace(enabled=True),
        )
    )
    feed = Feed(
        _Bot({KILLS_CHANNEL: kills, JUICY: juicy}),
        store,
        _Cards(),
        lambda: cfg,  # type: ignore[arg-type]
        _inline_to_thread,
        market=object(),
    )

    result = await feed._post_one(row)

    assert result is _PostResult.SENT
    assert len(juicy.sent) == 1  # high loot -> juicy, despite low fame
    assert len(kills.sent) == 1  # still in the main feed too


async def test_juicy_by_loot_off_when_loot_below_threshold(
    store: KbStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loot under juicy_min_loot does NOT reach the juicy channel."""
    import json

    from killboard import feed as feed_mod

    JUICY = 223
    row = _kill_row(10)
    store.upsert_event(row, json.dumps({"Victim": {"Name": "V"}, "Killer": {"Name": "K"}}))

    async def _low_loot(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"total": 50_000}

    monkeypatch.setattr(feed_mod, "estimate_value", _low_loot)

    juicy = _OkChannel()
    cfg = SimpleNamespace(
        killboard=SimpleNamespace(
            feed=KbFeedConfig(
                kills_channel=KILLS_CHANNEL,
                juicy_channel=JUICY,
                juicy_min_loot=1_000_000,
            ),
            market=SimpleNamespace(enabled=True),
        )
    )
    feed = Feed(
        _Bot({KILLS_CHANNEL: _OkChannel(), JUICY: juicy}),
        store,
        _Cards(),
        lambda: cfg,  # type: ignore[arg-type]
        _inline_to_thread,
        market=object(),
    )

    await feed._post_one(row)
    assert len(juicy.sent) == 0  # below threshold -> not juicy


# ── deaths recency gate: member death HISTORY must not spam the feed ──────────


def test_is_backfill_death_gate() -> None:
    """Pure gate: a death older than the window is backfill; a recent one is not;
    window 0 disables it; an unparseable/missing timestamp posts (not backfill)."""
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)

    def at(mins: float) -> EventRow:
        return _death_row(1, minutes_ago=0, timestamp=(now - timedelta(minutes=mins)).isoformat())

    assert _is_backfill_death(at(120), 60, now=now) is True  # 2h old > 60m
    assert _is_backfill_death(at(5), 60, now=now) is False  # fresh
    assert _is_backfill_death(at(120), 0, now=now) is False  # gate disabled
    assert _is_backfill_death(_death_row(1, minutes_ago=0, timestamp=""), 60, now=now) is False


async def test_old_death_is_seeded_not_posted(store: KbStore) -> None:
    """A weeks-old death (from the per-member sweep's history) must be seeded as
    posted — kept for Death Fame, never turned into a stale card in the feed."""
    row = _death_row(50, minutes_ago=120)  # > 60m default window
    store.upsert_event(row, "{}")
    channel = _OkChannel()
    feed = _death_feed(_Bot({DEATHS_CHANNEL: channel}), store)

    result = await feed._post_one(row)

    assert result is _PostResult.SKIPPED
    assert channel.sent == []  # never posted an old death card
    assert store.count_unposted() == 0  # seeded as posted; row stays for Death Fame


async def test_recent_death_still_posts(store: KbStore) -> None:
    """A genuinely recent death is unaffected by the gate and posts normally."""
    row = _death_row(51, minutes_ago=2)  # within the 60m window
    store.upsert_event(row, "{}")
    channel = _OkChannel()
    feed = _death_feed(_Bot({DEATHS_CHANNEL: channel}), store)

    result = await feed._post_one(row)

    assert result is _PostResult.SENT
    assert len(channel.sent) == 1
    assert store.count_unposted() == 0
