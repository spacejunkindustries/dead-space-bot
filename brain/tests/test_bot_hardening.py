"""bot.py hardening tests: hash-gated tree sync and the interaction error
boundary (GDD §11.3).

tree.sync used to run unguarded inside setup_hook — a 429/403 killed the
process and systemd's restart hammered the same rate-limited endpoint.
These pin the replacement contract: sync only when the payload hash changed
(persisted in app_state), never raise, TREE_SYNC_STALE on failure.

The boundary tests pin: every component/modal failure ANSWERS the
interaction ephemerally and counts into the AlarmBus.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from cortana.alarms import AlarmCode
from cortana.core import db
from cortana.dsc.bot import _TREE_HASH_KEY, sync_command_tree, tree_payload_hash
from cortana.dsc.views import run_component_action


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    db.migrate(connection)
    return connection


class _Command:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def to_dict(self, tree: Any) -> dict[str, Any]:
        return dict(self._payload)


class _Tree:
    def __init__(self, payloads: list[dict[str, Any]], *, fail: bool = False) -> None:
        self._payloads = payloads
        self._fail = fail
        self.sync_calls = 0

    def get_commands(self, *, guild: Any = None) -> list[_Command]:
        return [_Command(p) for p in self._payloads]

    async def sync(self, *, guild: Any = None) -> list[dict[str, Any]]:
        self.sync_calls += 1
        if self._fail:
            response = SimpleNamespace(status=429, reason="Too Many Requests")
            raise discord.HTTPException(response, "rate limited")  # type: ignore[arg-type]
        return list(self._payloads)


class _Bus:
    def __init__(self) -> None:
        self.raised: list[AlarmCode] = []
        self.cleared: list[AlarmCode] = []

    async def raise_alarm(self, code: AlarmCode, *args: Any, **kwargs: Any) -> None:
        self.raised.append(code)

    async def clear(self, code: AlarmCode, key: str | None = None) -> None:
        self.cleared.append(code)


GUILD = SimpleNamespace(id=1)
PAYLOADS = [{"name": "hostiles", "options": []}, {"name": "camp", "options": []}]


def test_payload_hash_is_order_insensitive_and_content_sensitive() -> None:
    a = tree_payload_hash(PAYLOADS)
    b = tree_payload_hash(list(reversed(PAYLOADS)))
    assert a == b  # cog load order can never force a re-sync
    changed = tree_payload_hash([{"name": "hostiles", "options": [{"name": "code"}]}])
    assert changed != a


async def test_first_sync_runs_and_stores_hash(conn: sqlite3.Connection) -> None:
    tree = _Tree(PAYLOADS)
    bus = _Bus()
    outcome = await sync_command_tree(conn, tree, GUILD, bus)  # type: ignore[arg-type]
    assert outcome == "synced"
    assert tree.sync_calls == 1
    stored = db.query_value(conn, "SELECT value FROM app_state WHERE key = ?", (_TREE_HASH_KEY,))
    assert stored == tree_payload_hash(PAYLOADS)
    assert AlarmCode.TREE_SYNC_STALE in bus.cleared


async def test_unchanged_payload_skips_the_rest_call(conn: sqlite3.Connection) -> None:
    tree = _Tree(PAYLOADS)
    await sync_command_tree(conn, tree, GUILD, None)
    outcome = await sync_command_tree(conn, tree, GUILD, None)
    assert outcome == "skipped"
    assert tree.sync_calls == 1  # the restart-storm case: zero API traffic


async def test_changed_payload_resyncs(conn: sqlite3.Connection) -> None:
    await sync_command_tree(conn, _Tree(PAYLOADS), GUILD, None)
    changed = _Tree([*PAYLOADS, {"name": "relay", "options": []}])
    outcome = await sync_command_tree(conn, changed, GUILD, None)
    assert outcome == "synced"
    assert changed.sync_calls == 1


async def test_sync_failure_alarms_and_never_raises(conn: sqlite3.Connection) -> None:
    tree = _Tree(PAYLOADS, fail=True)
    bus = _Bus()
    outcome = await sync_command_tree(conn, tree, GUILD, bus)  # type: ignore[arg-type]
    assert outcome == "failed"
    assert bus.raised == [AlarmCode.TREE_SYNC_STALE]
    # The hash is NOT stored — the next startup retries the sync.
    stored = db.query_value(conn, "SELECT value FROM app_state WHERE key = ?", (_TREE_HASH_KEY,))
    assert stored is None


async def test_failed_sync_retries_on_next_start_then_clears(conn: sqlite3.Connection) -> None:
    bus = _Bus()
    await sync_command_tree(conn, _Tree(PAYLOADS, fail=True), GUILD, bus)  # type: ignore[arg-type]
    outcome = await sync_command_tree(conn, _Tree(PAYLOADS), GUILD, bus)  # type: ignore[arg-type]
    assert outcome == "synced"
    assert AlarmCode.TREE_SYNC_STALE in bus.cleared


# ── the interaction error boundary ───────────────────────────────────────────


class _Response:
    def __init__(self, done: bool = False) -> None:
        self._done = done
        self.messages: list[tuple[str, bool]] = []

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content: str, *, ephemeral: bool = False, **kw: Any) -> None:
        self.messages.append((content, ephemeral))


class _Followup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send(self, content: str, *, ephemeral: bool = False, **kw: Any) -> None:
        self.messages.append((content, ephemeral))


class _ErrBus:
    def __init__(self) -> None:
        self.names: list[str] = []

    async def record_interaction_error(self, name: str) -> None:
        self.names.append(name)


def make_interaction(*, done: bool = False, bus: _ErrBus | None = None) -> Any:
    return SimpleNamespace(
        response=_Response(done),
        followup=_Followup(),
        client=SimpleNamespace(alarms=bus),
    )


async def test_boundary_answers_ephemerally_and_counts(conn: sqlite3.Connection) -> None:
    bus = _ErrBus()
    interaction = make_interaction(bus=bus)

    async def dispatch() -> None:
        raise RuntimeError("sqlite is locked")

    await run_component_action(interaction, "incident-card", dispatch())
    assert interaction.response.messages == [("Something broke — logged.", True)]
    assert bus.names == ["incident-card"]


async def test_boundary_uses_followup_after_defer(conn: sqlite3.Connection) -> None:
    bus = _ErrBus()
    interaction = make_interaction(done=True, bus=bus)

    async def dispatch() -> None:
        raise RuntimeError("boom after defer")

    await run_component_action(interaction, "poll-vote", dispatch())
    assert interaction.response.messages == []
    assert interaction.followup.messages == [("Something broke — logged.", True)]
    assert bus.names == ["poll-vote"]


async def test_boundary_passes_success_through_untouched() -> None:
    bus = _ErrBus()
    interaction = make_interaction(bus=bus)
    ran: list[int] = []

    async def dispatch() -> None:
        ran.append(1)

    await run_component_action(interaction, "help-menu", dispatch())
    assert ran == [1]
    assert interaction.response.messages == []
    assert bus.names == []


async def test_boundary_survives_missing_bus_and_dead_interaction() -> None:
    # No alarms attribute on the client, and the reply itself raising: the
    # boundary must swallow both — it is the last line, it never raises.
    class _DeadResponse:
        def is_done(self) -> bool:
            return False

        async def send_message(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("interaction token expired")

    interaction = SimpleNamespace(
        response=_DeadResponse(), followup=_Followup(), client=SimpleNamespace()
    )

    async def dispatch() -> None:
        raise RuntimeError("original failure")

    await run_component_action(interaction, "subscription-toggle", dispatch())  # no raise
