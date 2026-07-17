"""Slash-twin dispatch tests for SubsCog's callsign and ping commands —
constraint 10.

Same pattern as test_intel_cog: the cog is a thin adapter, so these pin the
wiring — /register, /unregister, /whoami, /pingme and /pingme-clear all drive
their intents through ``IncidentEngine.report`` (never the registries
directly), sharing the one engine entry point with the voice path.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from aura.core.personal_pings import PingSub
from aura.dsc.cogs.subs import SubsCog
from aura.types import IncidentOutcome, Intent, Outcome, SystemEntry, Tier


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


# ── personal pings: /pingme /mypings /pingme-clear (GDD §10.3) ───────────────

ALL_TYPES = "HOSTILE_SPOTTED,UNDER_ATTACK,ASSIST_REQUEST,GATE_CAMP"


class _Gazetteer:
    """Just enough for resolve_typed_system and _ping_line."""

    def __init__(self) -> None:
        self.entries = {
            1: SystemEntry(id=1, name="Otanuomi", region="R", constellation=None, metaphone="OTNM")
        }

    def by_id(self, system_id: int) -> SystemEntry | None:
        return self.entries.get(system_id)

    def by_name(self, name: str) -> SystemEntry | None:
        for entry in self.entries.values():
            if entry.name.lower() == name.lower():
                return entry
        return None


class _PingRegistry:
    def __init__(self, subs: list[PingSub] | None = None) -> None:
        self.subs = subs or []
        self.removed: list[tuple[int, int, int]] = []

    def list_for(self, guild_id: int, user_id: int) -> tuple[PingSub, ...]:
        return tuple(s for s in self.subs if s.guild_id == guild_id and s.user_id == user_id)

    async def remove(self, guild_id: int, user_id: int, index: int) -> PingSub | None:
        self.removed.append((guild_id, user_id, index))
        mine = self.list_for(guild_id, user_id)
        return mine[index - 1] if 1 <= index <= len(mine) else None


def make_ping_bot(engine: _Engine, subs: list[PingSub] | None = None) -> Any:
    engine.personal_pings = _PingRegistry(subs)  # type: ignore[attr-defined]
    return SimpleNamespace(engine=engine, gazetteer=_Gazetteer())


def sub(types: frozenset[Intent], system_id: int | None, sub_id: int = 1) -> PingSub:
    return PingSub(
        id=sub_id, guild_id=1, user_id=42, types=types, system_id=system_id, created_at="t"
    )


async def test_pingme_dispatches_through_engine_report() -> None:
    engine = _Engine(
        IncidentOutcome(Outcome.POSTED, "Pinging you for gate camps in Otanuomi.", None, None)
    )
    cog = SubsCog(make_ping_bot(engine))  # type: ignore[arg-type]
    interaction = make_interaction(guild_id=1, user_id=42)

    await SubsCog.pingme.callback(cog, interaction, "GATE_CAMP", "Otanuomi")

    assert len(engine.reports) == 1
    guild_id, user_id, parsed, resolution = engine.reports[0]
    assert (guild_id, user_id) == (1, 42)
    assert parsed.intent is Intent.PING_ME
    assert parsed.detail == "GATE_CAMP"
    assert parsed.system_text == "Otanuomi"
    assert parsed.raw == "/pingme GATE_CAMP Otanuomi"
    assert resolution is not None
    assert resolution.tier is Tier.HIGH  # typed input resolves at full confidence
    assert resolution.best.system_id == 1
    assert interaction.followup.messages == ["✅ Pinging you for gate camps in Otanuomi."]


async def test_pingme_anything_encodes_all_types_no_system() -> None:
    engine = _Engine(
        IncidentOutcome(Outcome.POSTED, "Pinging you for everything everywhere.", None, None)
    )
    cog = SubsCog(make_ping_bot(engine))  # type: ignore[arg-type]

    await SubsCog.pingme.callback(cog, make_interaction(), "ALL")

    (_, _, parsed, resolution) = engine.reports[0]
    assert parsed.detail == ALL_TYPES
    assert parsed.system_text is None
    assert resolution is None


async def test_pingme_unknown_system_rejected_without_dispatch() -> None:
    engine = _Engine(IncidentOutcome(Outcome.POSTED, None, None, None))
    cog = SubsCog(make_ping_bot(engine))  # type: ignore[arg-type]
    interaction = make_interaction()

    await SubsCog.pingme.callback(cog, interaction, "GATE_CAMP", "Nowhere")

    assert engine.reports == []
    assert "Unknown system" in interaction.response.messages[0]


async def test_pingme_clear_all_dispatches_through_engine_report() -> None:
    engine = _Engine(IncidentOutcome(Outcome.POSTED, "No longer pinging you.", None, None))
    cog = SubsCog(make_ping_bot(engine))  # type: ignore[arg-type]
    interaction = make_interaction()

    await SubsCog.pingme_clear.callback(cog, interaction, None)

    (_, _, parsed, resolution) = engine.reports[0]
    assert parsed.intent is Intent.PING_ME_CLEAR
    assert parsed.detail is None
    assert parsed.raw == "/pingme-clear"
    assert resolution is None
    assert interaction.followup.messages == ["✅ No longer pinging you."]


async def test_pingme_clear_by_index_removes_one() -> None:
    engine = _Engine(IncidentOutcome(Outcome.POSTED, None, None, None))
    subs = [sub(frozenset({Intent.GATE_CAMP}), 1)]
    cog = SubsCog(make_ping_bot(engine, subs))  # type: ignore[arg-type]
    interaction = make_interaction(guild_id=1, user_id=42)

    await SubsCog.pingme_clear.callback(cog, interaction, 1)

    assert engine.reports == []  # index removal is slash-only, no engine round-trip
    assert engine.personal_pings.removed == [(1, 42, 1)]  # type: ignore[attr-defined]
    assert interaction.followup.messages == ["✅ Removed ping #1: Gate camps — Otanuomi."]


async def test_pingme_clear_bad_index_reports_error() -> None:
    engine = _Engine(IncidentOutcome(Outcome.POSTED, None, None, None))
    cog = SubsCog(make_ping_bot(engine, []))  # type: ignore[arg-type]
    interaction = make_interaction(guild_id=1, user_id=42)

    await SubsCog.pingme_clear.callback(cog, interaction, 3)

    assert interaction.followup.messages == ["❌ No personal ping #3 — check `/mypings`."]


async def test_mypings_lists_indexed_lines() -> None:
    engine = _Engine(IncidentOutcome(Outcome.POSTED, None, None, None))
    subs = [
        sub(frozenset({Intent.GATE_CAMP}), 1, sub_id=1),
        sub(
            frozenset(
                {
                    Intent.HOSTILE_SPOTTED,
                    Intent.UNDER_ATTACK,
                    Intent.ASSIST_REQUEST,
                    Intent.GATE_CAMP,
                }
            ),
            None,
            sub_id=2,
        ),
    ]
    cog = SubsCog(make_ping_bot(engine, subs))  # type: ignore[arg-type]
    interaction = make_interaction(guild_id=1, user_id=42)

    await SubsCog.mypings.callback(cog, interaction)

    (text,) = interaction.response.messages
    assert "1. Gate camps — Otanuomi" in text
    assert "2. Everything — everywhere" in text


async def test_mypings_empty() -> None:
    engine = _Engine(IncidentOutcome(Outcome.POSTED, None, None, None))
    cog = SubsCog(make_ping_bot(engine, []))  # type: ignore[arg-type]
    interaction = make_interaction(guild_id=1, user_id=42)

    await SubsCog.mypings.callback(cog, interaction)

    assert interaction.response.messages == ["You have no pings set. `/pingme` to add one."]


async def test_ping_commands_are_guild_only() -> None:
    engine = _Engine(IncidentOutcome(Outcome.POSTED, None, None, None))
    cog = SubsCog(make_ping_bot(engine))  # type: ignore[arg-type]
    for args in (
        (SubsCog.pingme, ("GATE_CAMP", "Otanuomi")),
        (SubsCog.mypings, ()),
        (SubsCog.pingme_clear, (1,)),
    ):
        command, extra = args
        interaction = make_interaction(guild_id=None)
        await command.callback(cog, interaction, *extra)
        assert interaction.response.messages == ["Guild only."]
    assert engine.reports == []
