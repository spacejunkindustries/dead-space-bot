"""Slash-twin dispatch tests for SubsCog's callsign commands — constraint 10.

Same pattern as test_intel_cog: the cog is a thin adapter, so these pin the
wiring — /register, /unregister and /whoami all drive their intents through
``IncidentEngine.report`` (never the registry directly), sharing the one
engine entry point with the voice path.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from aura.dsc.cogs.subs import SubsCog
from aura.types import IncidentOutcome, Intent, Outcome


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


async def test_register_dispatches_through_engine_report() -> None:
    engine = _Engine(IncidentOutcome(Outcome.POSTED, "Registered you as Space Junkie.", None, None))
    cog = SubsCog(SimpleNamespace(engine=engine))  # type: ignore[arg-type]
    interaction = make_interaction(guild_id=1, user_id=42)

    await SubsCog.register.callback(cog, interaction, "Space Junkie")

    assert interaction.response.deferred
    assert len(engine.reports) == 1
    guild_id, user_id, parsed, resolution = engine.reports[0]
    assert (guild_id, user_id) == (1, 42)
    assert parsed.intent is Intent.REGISTER
    assert parsed.system_text is None
    assert parsed.detail == "Space Junkie"
    assert parsed.raw == "/register Space Junkie"
    assert resolution is None
    assert interaction.followup.messages == ["✅ Registered you as Space Junkie."]


async def test_register_sanitises_but_preserves_typed_case() -> None:
    engine = _Engine(IncidentOutcome(Outcome.POSTED, None, None, None))
    cog = SubsCog(SimpleNamespace(engine=engine))  # type: ignore[arg-type]

    await SubsCog.register.callback(cog, make_interaction(), "<@123> `xX SpaceJunkie Xx`")

    (_, _, parsed, _) = engine.reports[0]
    assert parsed.detail == "123 xX SpaceJunkie Xx"  # markdown/mention stripped, case kept


async def test_unregister_dispatches_intent_unregister() -> None:
    engine = _Engine(IncidentOutcome(Outcome.POSTED, "Unregistered.", None, None))
    cog = SubsCog(SimpleNamespace(engine=engine))  # type: ignore[arg-type]
    interaction = make_interaction()

    await SubsCog.unregister.callback(cog, interaction)

    (_, _, parsed, resolution) = engine.reports[0]
    assert parsed.intent is Intent.UNREGISTER
    assert parsed.detail is None
    assert parsed.raw == "/unregister"
    assert resolution is None
    assert interaction.followup.messages == ["✅ Unregistered."]


async def test_whoami_rejected_outcome_is_rendered() -> None:
    engine = _Engine(IncidentOutcome(Outcome.POSTED, "You are not registered.", None, None))
    cog = SubsCog(SimpleNamespace(engine=engine))  # type: ignore[arg-type]
    interaction = make_interaction()

    await SubsCog.whoami.callback(cog, interaction)

    (_, _, parsed, _) = engine.reports[0]
    assert parsed.intent is Intent.WHOAMI
    assert interaction.followup.messages == ["✅ You are not registered."]


async def test_callsign_commands_are_guild_only() -> None:
    engine = _Engine(IncidentOutcome(Outcome.POSTED, None, None, None))
    cog = SubsCog(SimpleNamespace(engine=engine))  # type: ignore[arg-type]
    for command in (SubsCog.register, SubsCog.unregister, SubsCog.whoami):
        interaction = make_interaction(guild_id=None)
        if command is SubsCog.register:
            await command.callback(cog, interaction, "Space Junkie")
        else:
            await command.callback(cog, interaction)
        assert interaction.response.messages == ["Guild only."]
    assert engine.reports == []
