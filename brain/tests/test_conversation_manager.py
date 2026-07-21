"""ConversationManager: sessions, the turn/TTL/cooldown budget, and the hard
wall (GDD §6.8).

A fake backend + an injected monotonic clock keep every case pure. The hard-wall
signature test is the structural guarantee that the chat layer can never be
handed an incident/routing/mention capability.
"""

from __future__ import annotations

import inspect
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from cortana.config import ConversationConfig
from cortana.conversation import (
    ConversationManager,
    conversation_available,
    ops_quiet,
)
from cortana.dialog.types import RunChat


class _Holder:
    def __init__(self, conversation: ConversationConfig) -> None:
        self.current = SimpleNamespace(conversation=conversation)


class _FakeBackend:
    """Records converse() calls and returns a scripted reply."""

    def __init__(self, reply: str = "Sure thing.") -> None:
        self.reply = reply
        self.calls: list[list[dict[str, str]]] = []

    async def converse(self, user_id: int, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        return self.reply

    async def ask(self, user_id: int, query: str) -> str:  # pragma: no cover — unused
        return self.reply

    async def close(self) -> None:  # pragma: no cover — unused
        return None


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _manager(
    cfg: ConversationConfig,
    backend: _FakeBackend | None,
    clock: _Clock,
    last_incident_at=None,
) -> ConversationManager:
    return ConversationManager(
        _Holder(cfg),
        conversation_provider=lambda: (backend, "ready" if backend else "disabled"),
        last_incident_at=last_incident_at,
        clock=clock,
    )


def _cfg(**kw) -> ConversationConfig:
    defaults = dict(
        enabled=True,
        backend="local",
        local_url="http://x",
        math_tool=False,  # default off in tests so the backend is exercised
        user_cooldown_s=0.0,
        max_turns=8,
        max_history_turns=6,
        session_ttl_seconds=180,
    )
    defaults.update(kw)
    return ConversationConfig(**defaults)


# ── happy path + history ─────────────────────────────────────────────────────


async def test_reply_calls_backend_and_returns_text() -> None:
    clock = _Clock()
    backend = _FakeBackend("Hello, pilot.")
    mgr = _manager(_cfg(), backend, clock)
    reply = await mgr.reply(1, 9, "hi")
    assert reply == "Hello, pilot."
    assert backend.calls == [[{"role": "user", "content": "hi"}]]


async def test_history_replays_prior_turns() -> None:
    clock = _Clock()
    backend = _FakeBackend("ok")
    mgr = _manager(_cfg(), backend, clock)
    await mgr.reply(1, 9, "first")
    await mgr.reply(1, 9, "second")
    # The second call replays the first exchange as context.
    assert backend.calls[1] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "second"},
    ]


async def test_history_trimmed_to_max_history_turns() -> None:
    clock = _Clock()
    backend = _FakeBackend("ok")
    mgr = _manager(_cfg(max_history_turns=1, max_turns=99), backend, clock)
    await mgr.reply(1, 9, "a")
    await mgr.reply(1, 9, "b")
    await mgr.reply(1, 9, "c")
    # Only the single most-recent exchange (b/ok) survives as context for c.
    assert backend.calls[2] == [
        {"role": "user", "content": "b"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "c"},
    ]


async def test_max_history_turns_zero_is_stateless() -> None:
    clock = _Clock()
    backend = _FakeBackend("ok")
    mgr = _manager(_cfg(max_history_turns=0, max_turns=99), backend, clock)
    await mgr.reply(1, 9, "a")
    await mgr.reply(1, 9, "b")
    assert backend.calls[1] == [{"role": "user", "content": "b"}]


# ── turn budget + TTL ────────────────────────────────────────────────────────


async def test_turns_exhaust_then_reply_returns_none() -> None:
    clock = _Clock()
    backend = _FakeBackend("ok")
    mgr = _manager(_cfg(max_turns=2), backend, clock)
    assert await mgr.reply(1, 9, "a") == "ok"
    assert await mgr.reply(1, 9, "b") == "ok"
    assert mgr.active(1) is False
    assert await mgr.reply(1, 9, "c") is None  # budget spent
    assert len(backend.calls) == 2  # the exhausted turn never reached the backend


async def test_fresh_session_after_expiry_refills_budget() -> None:
    clock = _Clock()
    backend = _FakeBackend("ok")
    mgr = _manager(_cfg(max_turns=1, session_ttl_seconds=100), backend, clock)
    assert await mgr.reply(1, 9, "a") == "ok"
    assert await mgr.reply(1, 9, "b") is None  # exhausted
    clock.t += 101  # past the TTL
    assert await mgr.reply(1, 9, "c") == "ok"  # a fresh session, full budget


async def test_ttl_expiry_drops_stale_thread() -> None:
    clock = _Clock()
    backend = _FakeBackend("ok")
    mgr = _manager(_cfg(session_ttl_seconds=50, max_history_turns=6), backend, clock)
    await mgr.reply(1, 9, "old context")
    clock.t += 51
    await mgr.reply(1, 9, "new topic")
    # The stale thread did not bleed: the second call carries no prior history.
    assert backend.calls[1] == [{"role": "user", "content": "new topic"}]


async def test_active_false_when_expired() -> None:
    clock = _Clock()
    mgr = _manager(_cfg(session_ttl_seconds=30), _FakeBackend(), clock)
    await mgr.reply(1, 9, "hi")
    assert mgr.active(1) is True
    clock.t += 31
    assert mgr.active(1) is False


# ── cooldown ─────────────────────────────────────────────────────────────────


async def test_cooldown_drops_turn_without_spending_budget() -> None:
    clock = _Clock()
    backend = _FakeBackend("ok")
    mgr = _manager(_cfg(user_cooldown_s=5.0, max_turns=8), backend, clock)
    assert await mgr.reply(1, 9, "a") == "ok"
    # A turn 1s later is inside the cooldown → dropped, no backend call, no spend.
    clock.t += 1
    assert await mgr.reply(1, 9, "b") is None
    assert len(backend.calls) == 1
    # Past the cooldown it goes through and the budget is intact.
    clock.t += 5
    assert await mgr.reply(1, 9, "c") == "ok"


# ── math tool ────────────────────────────────────────────────────────────────


async def test_math_answered_without_backend() -> None:
    clock = _Clock()
    backend = _FakeBackend("SHOULD NOT BE CALLED")
    mgr = _manager(_cfg(math_tool=True), backend, clock)
    assert await mgr.reply(1, 9, "what is 6 times 7") == "42"
    assert backend.calls == []  # the model was never consulted


async def test_math_off_falls_through_to_backend() -> None:
    clock = _Clock()
    backend = _FakeBackend("Forty-two, probably.")
    mgr = _manager(_cfg(math_tool=False), backend, clock)
    assert await mgr.reply(1, 9, "what is 6 times 7") == "Forty-two, probably."
    assert len(backend.calls) == 1


# ── off / no-backend / reset ─────────────────────────────────────────────────


async def test_disabled_returns_none() -> None:
    clock = _Clock()
    mgr = _manager(_cfg(enabled=False), _FakeBackend(), clock)
    assert await mgr.reply(1, 9, "hi") is None


async def test_no_backend_returns_none() -> None:
    clock = _Clock()
    mgr = _manager(_cfg(), None, clock)
    assert await mgr.reply(1, 9, "hi") is None
    assert mgr.backend_live() is False


async def test_backend_exception_drops_silently() -> None:
    clock = _Clock()

    class _Boom:
        async def converse(self, user_id, messages):
            raise RuntimeError("model down")

    mgr = _manager(_cfg(), _Boom(), clock)  # type: ignore[arg-type]
    assert await mgr.reply(1, 9, "hi") is None


async def test_empty_reply_returns_none() -> None:
    clock = _Clock()
    mgr = _manager(_cfg(), _FakeBackend("   "), clock)
    assert await mgr.reply(1, 9, "hi") is None


async def test_reset_user_purges_session() -> None:
    clock = _Clock()
    backend = _FakeBackend("ok")
    mgr = _manager(_cfg(max_history_turns=6), backend, clock)
    await mgr.reply(1, 9, "remember this")
    mgr.reset_user(1)
    assert mgr.active(1) is False
    await mgr.reply(1, 9, "fresh")
    assert backend.calls[1] == [{"role": "user", "content": "fresh"}]


# ── ops-quiet predicate (pure) ───────────────────────────────────────────────


def test_ops_quiet_fresh_incident_true() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    recent = now - timedelta(minutes=3)
    assert ops_quiet(now, recent, enabled=True, window_min=10) is True


def test_ops_quiet_stale_incident_false() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    stale = now - timedelta(minutes=30)
    assert ops_quiet(now, stale, enabled=True, window_min=10) is False


def test_ops_quiet_none_false() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    assert ops_quiet(now, None, enabled=True, window_min=10) is False


def test_ops_quiet_disabled_false() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    recent = now - timedelta(minutes=1)
    assert ops_quiet(now, recent, enabled=False, window_min=10) is False


def test_manager_ops_quiet_reads_incident_getter() -> None:
    clock = _Clock()
    recent = datetime.now(UTC) - timedelta(minutes=2)
    mgr = _manager(
        _cfg(quiet_during_ops=True, quiet_window_min=10),
        _FakeBackend(),
        clock,
        last_incident_at=lambda gid: recent,
    )
    assert mgr.ops_quiet(9) is True
    mgr_no = _manager(
        _cfg(quiet_during_ops=True, quiet_window_min=10),
        _FakeBackend(),
        clock,
        last_incident_at=lambda gid: None,
    )
    assert mgr_no.ops_quiet(9) is False


# ── availability predicate (pure) ────────────────────────────────────────────


def test_conversation_available_all_gates() -> None:
    on = _cfg(enabled=True)
    off = _cfg(enabled=False)
    assert conversation_available(on, backend_live=True, ops_quiet=False) is True
    assert conversation_available(off, backend_live=True, ops_quiet=False) is False
    assert conversation_available(on, backend_live=False, ops_quiet=False) is False
    assert conversation_available(on, backend_live=True, ops_quiet=True) is False


# ── THE HARD WALL ────────────────────────────────────────────────────────────


def test_manager_constructor_is_exactly_the_walled_signature() -> None:
    """ConversationManager's constructor is pinned to EXACTLY its walled
    signature: a config holder, a backend provider, a READ-ONLY incident-time
    getter, and a clock — nothing that could post a card or mint a mention
    (constraints 9 + 11). Any new parameter trips this test, forcing a reviewer
    to consciously prove the addition can't escalate.

    ``last_incident_at`` is a ``Callable[[int], datetime | None]`` — it can only
    OBSERVE the newest incident timestamp for the ops-quiet predicate; it holds
    no engine, no routing, and no mention authority."""
    params = set(inspect.signature(ConversationManager.__init__).parameters)
    assert params == {"self", "holder", "conversation_provider", "last_incident_at", "clock"}
    # And it must never be handed the IncidentEngine / a routing / a mention API.
    forbidden = ("incidentengine", "incidents", "routing", "decide_mentions", "poster", "mention")
    for name in params:
        assert not any(bad in name.lower() for bad in forbidden), name


def test_runchat_carries_only_text() -> None:
    """The RunChat action must carry ONLY the transcript text — no incident, no
    severity, no mention surface can ride to the chat executor."""
    names = {f.name for f in fields(RunChat)}
    assert names == {"text"}
