"""Slash-twin dispatch tests for IntelCog — constraint 10 (GDD §7, §20).

The cog is a thin adapter; these tests pin the wiring: /under-attack drives
Intent.UNDER_ATTACK through the shared ``_report`` path, /cancel drives
Intent.CANCEL through ``IncidentEngine.report`` (never ``cancel()`` directly),
and /relay drives ``IncidentEngine.broadcast`` — every twin shares the one
engine entry point with the voice path, including the severity (``code:``)
and audience parity fields.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from cortana.dsc.cogs.intel import IntelCog
from cortana.types import IncidentOutcome, Intent, Outcome, Severity


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

    async def fake_report(
        interaction: Any,
        intent: Any,
        system: str,
        detail: Any,
        name: str,
        *,
        code: Any = None,
        audience: Any = None,
    ) -> None:
        recorded.append((intent, system, detail, name, code, audience))

    cog._report = fake_report  # type: ignore[method-assign]
    await IntelCog.under_attack.callback(cog, make_interaction(), "Otanuomi", "pointed on gate")
    assert recorded == [
        (Intent.UNDER_ATTACK, "Otanuomi", "pointed on gate", "under-attack", None, None)
    ]


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


# ── /relay: the freeform relay's slash twin (constraint 10, GDD §8.6/§20) ────


class _BroadcastEngine:
    def __init__(self, outcome: IncidentOutcome) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def broadcast(
        self,
        guild_id: int,
        reporter_id: int,
        text: str,
        **kwargs: Any,
    ) -> IncidentOutcome:
        self.calls.append(
            {"guild_id": guild_id, "reporter_id": reporter_id, "text": text, **kwargs}
        )
        return self.outcome


class _Discipline:
    def __init__(self, may: bool) -> None:
        self.may = may

    def may_mention(self, role_ids: Any) -> bool:
        list(role_ids)  # consume the generator like the real gate does
        return self.may


async def test_relay_dispatches_through_engine_broadcast() -> None:
    engine = _BroadcastEngine(IncidentOutcome(Outcome.POSTED, "Relayed.", None, None))
    bot = SimpleNamespace(engine=engine, discipline=_Discipline(True))
    cog = IntelCog(bot)  # type: ignore[arg-type]
    interaction = make_interaction(guild_id=1, user_id=42)
    interaction.user.roles = [SimpleNamespace(id=111)]

    await IntelCog.relay.callback(cog, interaction, "blop fleet moving to Moe 8 gate")

    assert interaction.response.deferred
    assert len(engine.calls) == 1
    call = engine.calls[0]
    assert (call["guild_id"], call["reporter_id"]) == (1, 42)
    assert call["text"] == "blop fleet moving to Moe 8 gate"
    assert call["severity"] is None
    assert call["group_alias"] is None
    assert call["caller_may_mention"] is True
    assert interaction.followup.messages == ["✅ Relayed."]


async def test_relay_maps_code_and_audience_to_voice_fields() -> None:
    # code:red → Severity.HIGH, audience:all-hands → group_alias "all_hands" —
    # exactly the parsed fields the voice grammar produces (constraint 10).
    engine = _BroadcastEngine(IncidentOutcome(Outcome.POSTED, "Relayed.", None, None))
    bot = SimpleNamespace(engine=engine, discipline=_Discipline(False))
    cog = IntelCog(bot)  # type: ignore[arg-type]
    interaction = make_interaction(guild_id=1, user_id=42)
    interaction.user.roles = []

    await IntelCog.relay.callback(cog, interaction, "cyno up", code="red", audience="all-hands")

    call = engine.calls[0]
    assert call["severity"] is Severity.HIGH
    assert call["group_alias"] == "all_hands"
    assert call["caller_may_mention"] is False  # the @Pilot gate rides along


async def test_relay_is_guild_only() -> None:
    engine = _BroadcastEngine(IncidentOutcome(Outcome.POSTED, None, None, None))
    cog = IntelCog(SimpleNamespace(engine=engine))  # type: ignore[arg-type]
    interaction = make_interaction(guild_id=None)
    await IntelCog.relay.callback(cog, interaction, "some intel")
    assert engine.calls == []
    assert interaction.response.messages == ["Guild only."]


# ── autocomplete offers the full seeded map, not just the scoped set ─────────


async def test_autocomplete_offers_out_of_scope_systems() -> None:
    # The "manual report only listed ~8 systems" fix: autocomplete must reach
    # the full seeded map, with the scoped set surfaced first (GDD §8.1).
    from cortana.dsc.cogs.intel import system_autocomplete
    from cortana.types import SystemEntry

    scoped = SystemEntry(id=1, name="Otanuomi", region="Home", constellation=None, metaphone="ATNM")
    distant = SystemEntry(id=2, name="Otawa", region="Faraway", constellation=None, metaphone="AT")

    class _Gaz:
        systems = (scoped,)
        all_systems = (scoped, distant)

    interaction = SimpleNamespace(client=SimpleNamespace(gazetteer=_Gaz()))
    choices = await system_autocomplete(interaction, "ota")  # type: ignore[arg-type]
    names = [c.value for c in choices]
    assert "Otanuomi" in names  # scoped
    assert "Otawa" in names  # out of scope, but still offered
    assert names.index("Otanuomi") < names.index("Otawa")  # home region first
