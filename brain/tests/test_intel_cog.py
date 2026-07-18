"""Slash-twin dispatch tests for IntelCog — constraint 10 (GDD §7, §20).

The cog is a thin adapter; these tests pin the wiring: /under-attack drives
Intent.UNDER_ATTACK through the shared ``_report`` path, and /cancel drives
Intent.CANCEL through ``IncidentEngine.report`` (never ``cancel()`` directly),
so both twins share the one engine entry point with the voice path.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from cortana.dsc.cogs.intel import IntelCog
from cortana.types import IncidentOutcome, Intent, Outcome


class _Response:
    def __init__(self) -> None:
        self.deferred = False
        self.messages: list[str] = []

    async def defer(self, **kwargs: Any) -> None:
        self.deferred = True

    async def send_message(self, content: str, **kwargs: Any) -> None:
        self.messages.append(content)


class _Followup:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, content: str, **kwargs: Any) -> None:
        self.messages.append(content)


def make_interaction(guild_id: int | None = 1, user_id: int = 42) -> Any:
    return SimpleNamespace(
        guild_id=guild_id,
        user=SimpleNamespace(id=user_id),
        response=_Response(),
        followup=_Followup(),
    )


class _Engine:
    def __init__(self, outcome: IncidentOutcome) -> None:
        self.outcome = outcome
        self.reports: list[tuple[int, int, Any, Any]] = []

    async def report(self, guild_id: int, user_id: int, parsed: Any, resolution: Any) -> Any:
        self.reports.append((guild_id, user_id, parsed, resolution))
        return self.outcome


async def test_under_attack_dispatches_through_shared_report_path() -> None:
    cog = IntelCog(SimpleNamespace())  # type: ignore[arg-type]
    recorded: list[tuple[Any, ...]] = []

    async def fake_report(interaction: Any, intent: Any, system: str, detail: Any, name: str):
        recorded.append((intent, system, detail, name))

    cog._report = fake_report  # type: ignore[method-assign]
    await IntelCog.under_attack.callback(cog, make_interaction(), "Otanuomi", "pointed on gate")
    assert recorded == [(Intent.UNDER_ATTACK, "Otanuomi", "pointed on gate", "under-attack")]


async def test_cancel_reports_intent_cancel_through_the_engine() -> None:
    engine = _Engine(IncidentOutcome(Outcome.POSTED, "Cancelled.", None, 7))
    cog = IntelCog(SimpleNamespace(engine=engine))  # type: ignore[arg-type]
    interaction = make_interaction(guild_id=1, user_id=42)

    await IntelCog.cancel.callback(cog, interaction)

    assert interaction.response.deferred
    assert len(engine.reports) == 1
    guild_id, user_id, parsed, resolution = engine.reports[0]
    assert (guild_id, user_id) == (1, 42)
    assert parsed.intent is Intent.CANCEL
    assert parsed.system_text is None
    assert parsed.raw == "/cancel"
    assert resolution is None
    assert interaction.followup.messages == ["✅ Cancelled."]


async def test_cancel_rejected_outcome_is_rendered() -> None:
    engine = _Engine(IncidentOutcome(Outcome.REJECTED, "Nothing to cancel.", None, None))
    cog = IntelCog(SimpleNamespace(engine=engine))  # type: ignore[arg-type]
    interaction = make_interaction()
    await IntelCog.cancel.callback(cog, interaction)
    assert interaction.followup.messages == ["❌ Nothing to cancel."]


async def test_cancel_is_guild_only() -> None:
    engine = _Engine(IncidentOutcome(Outcome.POSTED, None, None, None))
    cog = IntelCog(SimpleNamespace(engine=engine))  # type: ignore[arg-type]
    interaction = make_interaction(guild_id=None)
    await IntelCog.cancel.callback(cog, interaction)
    assert engine.reports == []
    assert interaction.response.messages == ["Guild only."]
