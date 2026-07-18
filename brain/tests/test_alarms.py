"""AlarmBus tests — GDD §11.3.

The contract under test: one edited-in-place #bot-health card per active
``(code, key)``; message ids persisted in ``app_state`` so a restart adopts
the existing card instead of duplicating it; clears edit the card to a
resolved state and drop the persisted ids; a poster that cannot deliver
(Discord not ready) never crashes the bus — the card lands on a later
:meth:`flush`.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from cortana.alarms import (
    INTERACTION_ERROR_ALARM_AFTER,
    AlarmBus,
    AlarmCode,
    AlarmSeverity,
)
from cortana.core import db


class FakePoster:
    """Records sends/edits; toggling ``ready`` simulates pre-ready Discord."""

    def __init__(self) -> None:
        self.ready = True
        self.sent: list[dict] = []
        self.edits: list[tuple[int, int, dict]] = []
        self.deleted_messages: set[int] = set()
        self._next_id = 100

    async def send(self, content: str, embed: dict | None) -> tuple[int, int] | None:
        if not self.ready:
            return None
        self._next_id += 1
        self.sent.append(embed or {})
        return (7, self._next_id)

    async def edit(
        self, channel_id: int, message_id: int, content: str, embed: dict | None
    ) -> bool | None:
        if not self.ready:
            return None
        if message_id in self.deleted_messages:
            return False
        self.edits.append((channel_id, message_id, embed or {}))
        return True

    @property
    def last_message_id(self) -> int:
        return self._next_id


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    db.migrate(connection)
    return connection


def make_bus(conn: sqlite3.Connection, poster: FakePoster) -> AlarmBus:
    clock = {"now": 1000.0}
    bus = AlarmBus(conn, send=poster.send, edit=poster.edit, clock=lambda: clock["now"])
    bus._test_clock = clock  # type: ignore[attr-defined] — test hook
    return bus


def persisted_keys(conn: sqlite3.Connection) -> list[str]:
    rows = db.query(conn, "SELECT key FROM app_state WHERE key LIKE 'alarm:%'")
    return [row["key"] for row in rows]


async def test_raise_posts_one_card_and_persists_ids(conn: sqlite3.Connection) -> None:
    poster = FakePoster()
    bus = make_bus(conn, poster)
    await bus.raise_alarm(AlarmCode.EARS_DOWN, AlarmSeverity.CRITICAL, "Ears is gone", "restart it")
    assert len(poster.sent) == 1
    assert "EARS_DOWN" in poster.sent[0]["title"]
    assert "Ears is gone" in poster.sent[0]["description"]
    assert "restart it" in poster.sent[0]["description"]
    assert persisted_keys(conn) == ["alarm:EARS_DOWN:"]
    stored = json.loads(
        db.query_value(conn, "SELECT value FROM app_state WHERE key = ?", ("alarm:EARS_DOWN:",))
    )
    assert stored["message_id"] == poster.last_message_id
    assert bus.active_count() == 1


async def test_repeat_raises_edit_in_place_with_count(conn: sqlite3.Connection) -> None:
    poster = FakePoster()
    bus = make_bus(conn, poster)
    await bus.raise_alarm(AlarmCode.POST_FAILURE, AlarmSeverity.CRITICAL, "post failed", "fix")
    await bus.raise_alarm(AlarmCode.POST_FAILURE, AlarmSeverity.CRITICAL, "post failed", "fix")
    await bus.raise_alarm(AlarmCode.POST_FAILURE, AlarmSeverity.CRITICAL, "post failed", "fix")
    assert len(poster.sent) == 1  # ONE card, ever
    assert len(poster.edits) == 2
    fields = {f["name"]: f["value"] for f in poster.edits[-1][2]["fields"]}
    assert fields["Count"] == "3"
    assert bus.active_count() == 1


async def test_keyed_alarms_get_independent_cards(conn: sqlite3.Connection) -> None:
    poster = FakePoster()
    bus = make_bus(conn, poster)
    await bus.raise_alarm(AlarmCode.CHANNEL_UNWRITABLE, AlarmSeverity.CRITICAL, "a", "f", key="1")
    await bus.raise_alarm(AlarmCode.CHANNEL_UNWRITABLE, AlarmSeverity.CRITICAL, "b", "f", key="2")
    assert len(poster.sent) == 2
    assert sorted(persisted_keys(conn)) == [
        "alarm:CHANNEL_UNWRITABLE:1",
        "alarm:CHANNEL_UNWRITABLE:2",
    ]
    assert bus.active_count() == 2


async def test_clear_edits_resolved_state_and_drops_persistence(conn: sqlite3.Connection) -> None:
    poster = FakePoster()
    bus = make_bus(conn, poster)
    await bus.raise_alarm(AlarmCode.WAKE_FAULTED, AlarmSeverity.CRITICAL, "wake dead", "reload")
    await bus.clear(AlarmCode.WAKE_FAULTED)
    assert len(poster.edits) == 1
    assert poster.edits[0][2]["title"].startswith("✅")
    assert "resolved" in poster.edits[0][2]["title"]
    assert persisted_keys(conn) == []
    assert bus.active_count() == 0


async def test_clear_without_matching_alarm_is_noop(conn: sqlite3.Connection) -> None:
    poster = FakePoster()
    bus = make_bus(conn, poster)
    await bus.clear(AlarmCode.TIMER_UNDELIVERED)
    assert poster.sent == []
    assert poster.edits == []


async def test_restart_adopts_persisted_card_instead_of_duplicating(
    conn: sqlite3.Connection,
) -> None:
    poster = FakePoster()
    bus = make_bus(conn, poster)
    await bus.raise_alarm(AlarmCode.EARS_DOWN, AlarmSeverity.CRITICAL, "down", "check")
    card_id = poster.last_message_id

    # "Restart": a brand-new bus over the same database.
    bus2 = make_bus(conn, poster)
    await bus2.raise_alarm(AlarmCode.EARS_DOWN, AlarmSeverity.CRITICAL, "still down", "check")
    assert len(poster.sent) == 1  # no second card
    assert poster.edits[-1][1] == card_id  # the SAME message was edited
    fields = {f["name"]: f["value"] for f in poster.edits[-1][2]["fields"]}
    assert fields["Count"] == "2"  # count survives the restart


async def test_clear_across_restart_resolves_the_old_card(conn: sqlite3.Connection) -> None:
    poster = FakePoster()
    bus = make_bus(conn, poster)
    await bus.raise_alarm(
        AlarmCode.CONFIG_RESTART_PENDING, AlarmSeverity.WARNING, "restart pending", "restart"
    )
    card_id = poster.last_message_id

    # The restart that applies the pending keys clears the alarm it survived.
    bus2 = make_bus(conn, poster)
    await bus2.clear(AlarmCode.CONFIG_RESTART_PENDING)
    assert len(poster.sent) == 1
    assert poster.edits[-1][1] == card_id
    assert poster.edits[-1][2]["title"].startswith("✅")
    assert persisted_keys(conn) == []


async def test_poster_down_queues_and_flush_delivers(conn: sqlite3.Connection) -> None:
    poster = FakePoster()
    poster.ready = False  # Discord not ready yet
    bus = make_bus(conn, poster)
    await bus.raise_alarm(AlarmCode.ROUTING_ZERO_RULES, AlarmSeverity.CRITICAL, "no rules", "fix")
    assert poster.sent == []  # nothing delivered...
    assert bus.active_count() == 1  # ...but the alarm is live

    await bus.flush()
    assert poster.sent == []  # still down; still no crash

    poster.ready = True
    await bus.flush()
    assert len(poster.sent) == 1  # delivered on the first ready flush
    assert persisted_keys(conn) == ["alarm:ROUTING_ZERO_RULES:"]


async def test_deleted_card_is_reposted_once(conn: sqlite3.Connection) -> None:
    poster = FakePoster()
    bus = make_bus(conn, poster)
    await bus.raise_alarm(AlarmCode.EARS_DOWN, AlarmSeverity.CRITICAL, "down", "check")
    poster.deleted_messages.add(poster.last_message_id)
    await bus.raise_alarm(AlarmCode.EARS_DOWN, AlarmSeverity.CRITICAL, "down", "check")
    assert len(poster.sent) == 2  # re-posted, not lost
    stored = json.loads(
        db.query_value(conn, "SELECT value FROM app_state WHERE key = ?", ("alarm:EARS_DOWN:",))
    )
    assert stored["message_id"] == poster.last_message_id  # new ids persisted


async def test_reraise_after_clear_posts_a_fresh_card(conn: sqlite3.Connection) -> None:
    poster = FakePoster()
    bus = make_bus(conn, poster)
    await bus.raise_alarm(AlarmCode.EARS_DOWN, AlarmSeverity.CRITICAL, "down", "check")
    await bus.clear(AlarmCode.EARS_DOWN)
    await bus.raise_alarm(AlarmCode.EARS_DOWN, AlarmSeverity.CRITICAL, "down again", "check")
    # The resolved card stays as history; the new episode gets a new card.
    assert len(poster.sent) == 2
    assert bus.active_count() == 1


async def test_interaction_errors_alarm_after_threshold(conn: sqlite3.Connection) -> None:
    poster = FakePoster()
    bus = make_bus(conn, poster)
    for _ in range(INTERACTION_ERROR_ALARM_AFTER - 1):
        await bus.record_interaction_error("hostiles")
    assert poster.sent == []  # below threshold: counted, not alarmed
    await bus.record_interaction_error("hostiles")
    assert len(poster.sent) == 1
    assert "INTERACTION_ERRORS — hostiles" in poster.sent[0]["title"]
    # A different command has its own counter and card.
    await bus.record_interaction_error("camp")
    assert len(poster.sent) == 1


async def test_concurrent_raises_post_exactly_one_card(conn: sqlite3.Connection) -> None:
    """Two raises of the same (code, key) in flight together — e.g. two
    /hostiles both hitting POST_FAILURE — must produce ONE card. Without the
    card lock both saw message_id None mid-send and double-posted, orphaning
    the first card as a permanently-active embed no clear() would ever edit."""
    import asyncio

    class SlowPoster(FakePoster):
        async def send(self, content: str, embed: dict | None) -> tuple[int, int] | None:
            await asyncio.sleep(0.01)  # a real Discord POST yields mid-flight
            return await super().send(content, embed)

    poster = SlowPoster()
    bus = make_bus(conn, poster)

    await asyncio.gather(
        bus.raise_alarm(AlarmCode.POST_FAILURE, AlarmSeverity.CRITICAL, "a", "fix"),
        bus.raise_alarm(AlarmCode.POST_FAILURE, AlarmSeverity.CRITICAL, "b", "fix"),
    )

    assert len(poster.sent) == 1  # one card posted…
    assert len(poster.edits) == 1  # …and the second raise edited it in place
    assert bus.active_count() == 1

    await bus.clear(AlarmCode.POST_FAILURE)
    assert bus.active_count() == 0  # nothing orphaned: the one card resolved


async def test_flush_racing_a_raise_does_not_double_post(conn: sqlite3.Connection) -> None:
    import asyncio

    class SlowPoster(FakePoster):
        async def send(self, content: str, embed: dict | None) -> tuple[int, int] | None:
            await asyncio.sleep(0.01)
            return await super().send(content, embed)

    poster = SlowPoster()
    bus = make_bus(conn, poster)

    await asyncio.gather(
        bus.raise_alarm(AlarmCode.EARS_DOWN, AlarmSeverity.CRITICAL, "down", "fix"),
        bus.flush(),
        bus.flush(),
    )

    assert len(poster.sent) == 1
