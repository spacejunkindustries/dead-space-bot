"""Operator-surface cog tests: /botstatus, /doctor, /reload, and the
rewired /routing reload + /gazetteer reload (GDD §18).

Same fake-interaction style as the other cog tests: the cogs are thin
adapters, so these pin the wiring — /reload and both admin reload actions
drive the ONE injected reload transaction (``bot.request_reload``), and
failures always answer the admin instead of leaving an eternal spinner.
"""

from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace
from typing import Any

import pytest

import cortana.dsc.cogs.status as status_mod
from cortana.core import db
from cortana.doctor import CheckResult, Status
from cortana.dsc.cogs.admin import AdminCog
from cortana.dsc.cogs.status import StatusCog, format_uptime
from cortana.reload import ReloadResult


class _Response:
    def __init__(self) -> None:
        self.deferred = False
        self.messages: list[str] = []

    def is_done(self) -> bool:
        return self.deferred or bool(self.messages)

    async def defer(self, **kwargs: Any) -> None:
        self.deferred = True

    async def send_message(self, content: str = "", **kwargs: Any) -> None:
        self.messages.append(content)


class _Followup:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.embeds: list[Any] = []

    async def send(self, content: str = "", **kwargs: Any) -> None:
        self.messages.append(content)
        if "embed" in kwargs and kwargs["embed"] is not None:
            self.embeds.append(kwargs["embed"])
        if "embeds" in kwargs:
            self.embeds.extend(kwargs["embeds"])


def make_interaction(guild_id: int | None = 1, user_id: int = 42) -> Any:
    return SimpleNamespace(
        guild_id=guild_id,
        guild=None,
        user=SimpleNamespace(id=user_id),
        response=_Response(),
        followup=_Followup(),
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    db.migrate(connection)
    return connection


class _Health:
    stt_watchdog_degraded = False
    stt_degraded = False
    low_streak = 0
    wake_faulted = False
    wake_counters = {"frames_seen": 500, "vad_speech": 10, "inferences": 8, "hits": 2}


class _Alarms:
    def __init__(self, active: list[Any] | None = None) -> None:
        self._active = active or []

    def active(self) -> tuple[Any, ...]:
        return tuple(self._active)

    def active_count(self) -> int:
        return len(self._active)


def make_bot(conn: sqlite3.Connection, **overrides: Any) -> Any:
    bot = SimpleNamespace(
        conn=conn,
        health_reporter=_Health(),
        alarms=_Alarms(),
        ipc_status=lambda: (True, 2.0),
        dialog_sessions=lambda: 1,
        voice_gateway=SimpleNamespace(joined_channel_id=555),
        discipline=SimpleNamespace(fleetmode=False),
        started_at_monotonic=time.monotonic() - 3720,  # 1h 2m ago
        request_reload=None,
        holder=SimpleNamespace(path="/etc/cortana/cortana.yaml"),
        engine=SimpleNamespace(_rules=[1, 2, 3]),
        gazetteer=SimpleNamespace(systems=(1, 2, 3, 4)),
    )
    for name, value in overrides.items():
        setattr(bot, name, value)
    return bot


# ── /botstatus ───────────────────────────────────────────────────────────────


async def test_botstatus_renders_one_phone_sized_embed(conn: sqlite3.Connection) -> None:
    bot = make_bot(conn)
    cog = StatusCog(bot)  # type: ignore[arg-type]
    interaction = make_interaction()

    await StatusCog.botstatus.callback(cog, interaction)

    assert interaction.response.deferred
    assert len(interaction.followup.embeds) == 1
    embed = interaction.followup.embeds[0]
    fields = {f.name: f.value for f in embed.fields}
    assert "connected" in fields["Ears"]
    assert fields["Voice"] == "in <#555>"
    assert fields["Uptime"] == "1h 2m"
    assert fields["STT"] == "ok"
    assert "hits 2" in fields["Wake"]
    assert fields["Dialogs in flight"] == "1"
    assert fields["Incidents (1h)"] == "0"
    assert fields["Active alarms"] == "none"


async def test_botstatus_surfaces_alarms_and_latches(conn: sqlite3.Connection) -> None:
    health = _Health()
    health.stt_watchdog_degraded = True
    active = [SimpleNamespace(code=SimpleNamespace(value="EARS_DOWN"), first_seen=1)]
    bot = make_bot(
        conn,
        health_reporter=health,
        alarms=_Alarms(active),
        ipc_status=lambda: (False, 34.0),
        voice_gateway=SimpleNamespace(joined_channel_id=None),
    )
    cog = StatusCog(bot)  # type: ignore[arg-type]
    interaction = make_interaction()

    await StatusCog.botstatus.callback(cog, interaction)

    embed = interaction.followup.embeds[0]
    fields = {f.name: f.value for f in embed.fields}
    assert "DOWN" in fields["Ears"]
    assert fields["Voice"] == "not in voice"
    assert "LATCHED" in fields["STT"]
    assert fields["Active alarms"] == "1 — EARS_DOWN"


def test_format_uptime() -> None:
    assert format_uptime(59) == "0m"
    assert format_uptime(3720) == "1h 2m"
    assert format_uptime(93784) == "1d 2h 3m"


# ── /doctor ──────────────────────────────────────────────────────────────────


async def test_doctor_posts_the_offline_table_ephemerally(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = [
        CheckResult("config", Status.PASS, "42 keys validated"),
        CheckResult("wake.model", Status.FAIL, "missing", "point wake.model at the .onnx"),
    ]
    monkeypatch.setattr(status_mod, "run_offline_checks", lambda ctx: results)
    bot = make_bot(conn)
    cog = StatusCog(bot)  # type: ignore[arg-type]
    interaction = make_interaction()

    await StatusCog.doctor.callback(cog, interaction)

    assert interaction.response.deferred
    assert len(interaction.followup.embeds) == 1
    text = interaction.followup.embeds[0].description
    assert "PASS" in text and "FAIL" in text
    assert "wake.model" in text
    assert "fix: point wake.model" in text


async def test_doctor_chunks_a_long_table(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = [CheckResult(f"check-{i}", Status.PASS, "x" * 120) for i in range(80)]
    monkeypatch.setattr(status_mod, "run_offline_checks", lambda ctx: results)
    bot = make_bot(conn)
    cog = StatusCog(bot)  # type: ignore[arg-type]
    interaction = make_interaction()

    await StatusCog.doctor.callback(cog, interaction)

    assert len(interaction.followup.embeds) > 1
    for embed in interaction.followup.embeds:
        assert len(embed.description) <= 4096


# ── /reload ──────────────────────────────────────────────────────────────────


def _receipt(**kwargs: Any) -> ReloadResult:
    defaults: dict[str, Any] = {"ok": True, "swapped": True}
    defaults.update(kwargs)
    return ReloadResult(**defaults)


async def test_reload_calls_injected_transaction_and_replies_summary(
    conn: sqlite3.Connection,
) -> None:
    calls: list[int] = []

    async def request_reload() -> ReloadResult:
        calls.append(1)
        return _receipt(applied=("tts.personality",), restart_pending=("ipc.socket",))

    bot = make_bot(conn, request_reload=request_reload)
    cog = StatusCog(bot)  # type: ignore[arg-type]
    interaction = make_interaction()

    await StatusCog.reload.callback(cog, interaction)

    assert calls == [1]
    (message,) = interaction.followup.messages
    assert "Reload applied" in message
    assert "tts.personality" in message
    assert "Restart pending" in message and "ipc.socket" in message


async def test_reload_unwired_answers_instead_of_spinning(conn: sqlite3.Connection) -> None:
    bot = make_bot(conn, request_reload=None)
    cog = StatusCog(bot)  # type: ignore[arg-type]
    interaction = make_interaction()

    await StatusCog.reload.callback(cog, interaction)

    assert interaction.followup.messages == [
        "Reload isn't wired in this process — restart the service instead."
    ]


async def test_reload_internal_failure_is_answered(conn: sqlite3.Connection) -> None:
    async def request_reload() -> ReloadResult:
        raise RuntimeError("boom")

    bot = make_bot(conn, request_reload=request_reload)
    cog = StatusCog(bot)  # type: ignore[arg-type]
    interaction = make_interaction()

    await StatusCog.reload.callback(cog, interaction)

    (message,) = interaction.followup.messages
    assert message.startswith("❌ Reload failed internally")


# ── admin cog rewiring: /routing reload + /gazetteer reload ──────────────────


async def test_routing_reload_goes_through_the_shared_transaction(
    conn: sqlite3.Connection,
) -> None:
    calls: list[int] = []

    async def request_reload() -> ReloadResult:
        calls.append(1)
        return _receipt()

    bot = make_bot(conn, request_reload=request_reload)
    cog = AdminCog(bot)  # type: ignore[arg-type]
    interaction = make_interaction()
    interaction.guild = SimpleNamespace(roles=[])

    await AdminCog.routing.callback(cog, interaction, "reload")

    assert calls == [1]
    (message,) = interaction.followup.messages
    assert message.startswith("✅")
    assert "Routing rules active: 3." in message


async def test_gazetteer_reload_reports_rejection_not_spinner(conn: sqlite3.Connection) -> None:
    """A bad gazetteer.yaml comes back as a receipt rejection line — the
    admin on a phone gets a message, never an unhandled GazetteerError."""

    async def request_reload() -> ReloadResult:
        return ReloadResult(
            ok=False,
            rejected=("gazetteer.yaml: regions must be a list of strings",),
        )

    bot = make_bot(conn, request_reload=request_reload)
    cog = AdminCog(bot)  # type: ignore[arg-type]
    interaction = make_interaction()

    await AdminCog.gazetteer.callback(cog, interaction, "reload")

    (message,) = interaction.followup.messages
    assert message.startswith("⚠️")
    assert "REJECTED" in message
    assert "gazetteer.yaml: regions must be a list of strings" in message


async def test_gazetteer_reload_success_reports_counts(conn: sqlite3.Connection) -> None:
    async def request_reload() -> ReloadResult:
        return _receipt(engine_reload=("gazetteer", "routing"))

    bot = make_bot(conn, request_reload=request_reload)
    cog = AdminCog(bot)  # type: ignore[arg-type]
    interaction = make_interaction()

    await AdminCog.gazetteer.callback(cog, interaction, "reload")

    (message,) = interaction.followup.messages
    assert message.startswith("✅")
    assert "Gazetteer active set: 4 → 4 systems." in message


async def test_admin_reload_unwired_is_answered(conn: sqlite3.Connection) -> None:
    bot = make_bot(conn, request_reload=None)
    cog = AdminCog(bot)  # type: ignore[arg-type]
    interaction = make_interaction()

    await AdminCog.gazetteer.callback(cog, interaction, "reload")

    assert interaction.followup.messages == [
        "Reload isn't wired in this process — restart the service instead."
    ]
