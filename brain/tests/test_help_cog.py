"""Help surface tests: content coverage, custom_ids, topic renders, and the
HELP intent's full path (grammar → engine → utterance) — constraint 10.

The coverage test is the load-bearing one: it collects every registered app
command from the cog modules and asserts each appears somewhere in the help
content, so /help cannot silently go stale as commands are added.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from discord import app_commands

from aura import tts
from aura.core import db
from aura.core.discipline import Discipline
from aura.core.incidents import IncidentEngine
from aura.dsc.cogs.admin import AdminCog
from aura.dsc.cogs.help import (
    HELP_TOPICS,
    HelpCog,
    help_custom_id,
    main_embed,
    parse_help_custom_id,
    topic_embed,
    visible_topics,
)
from aura.dsc.cogs.intel import IntelCog
from aura.dsc.cogs.ops import OpsCog
from aura.dsc.cogs.subs import SubsCog
from aura.dsc.cogs.utility import UtilityCog
from aura.nlu import grammar
from aura.types import IncidentOutcome, Intent, Outcome, ParsedCommand
from tests.test_incidents import FakeGazetteer, FakePoster, StubHolder, make_config

ALL_COGS = (IntelCog, SubsCog, OpsCog, UtilityCog, AdminCog, HelpCog)


# ── coverage: every registered app command appears in the help content ───────


def registered_command_names() -> set[str]:
    """Top-level app-command names across all cogs (groups count once)."""
    names: set[str] = set()
    for cls in ALL_COGS:
        for attr in vars(cls).values():
            if isinstance(attr, app_commands.Group | app_commands.Command):
                root: Any = attr
                while root.parent is not None:
                    root = root.parent
                names.add(root.name)
    return names


def all_help_text() -> str:
    chunks = [str(main_embed())]
    chunks.extend(str(topic_embed(key)) for key in HELP_TOPICS)
    return "\n".join(chunks)


def test_every_registered_command_is_documented() -> None:
    names = registered_command_names()
    assert len(names) >= 25  # sanity: the collection actually found the cogs
    text = all_help_text()
    missing = sorted(name for name in names if f"/{name}" not in text)
    assert not missing, f"commands absent from help content: {missing}"


# ── custom_id round-trip (aura:help:{topic}) ─────────────────────────────────


def test_custom_id_round_trip() -> None:
    for key in (*HELP_TOPICS, "menu"):
        assert parse_help_custom_id(help_custom_id(key)) == key


def test_custom_id_rejects_foreign_ids() -> None:
    for bad in ("aura:inc:5:otw", "aura:sub:9", "aura:help:", "aura:help:Bad Topic", "help:menu"):
        assert parse_help_custom_id(bad) is None


# ── topic renders ────────────────────────────────────────────────────────────


def _assert_embed_within_limits(embed: dict[str, Any]) -> None:
    assert embed["title"]
    assert len(str(embed["title"])) <= 256
    assert embed["description"]
    assert len(str(embed["description"])) <= 4096
    fields = embed["fields"]
    assert fields and len(fields) <= 25
    for field in fields:
        assert field["name"] and len(field["name"]) <= 256
        assert field["value"] and len(field["value"]) <= 1024


def test_each_topic_renders_within_discord_limits() -> None:
    for key in HELP_TOPICS:
        _assert_embed_within_limits(topic_embed(key))
    _assert_embed_within_limits(main_embed())


def test_admin_topic_hidden_from_non_admin_menu() -> None:
    keys = {t.key for t in visible_topics(include_admin=False)}
    assert "admin" not in keys
    assert keys == {k for k, t in HELP_TOPICS.items() if not t.admin_only}
    assert {t.key for t in visible_topics(include_admin=True)} == set(HELP_TOPICS)


# ── HELP intent: grammar parse ───────────────────────────────────────────────


def test_grammar_parses_help() -> None:
    cmd = grammar.parse("Aura Command, help")
    assert cmd is not None
    assert cmd.intent is Intent.HELP
    assert cmd.system_text is None
    assert cmd.detail is None


def test_need_help_is_still_a_distress_call() -> None:
    cmd = grammar.parse("Aura Command, need help in Kisogo")
    assert cmd is not None
    assert cmd.intent is Intent.ASSIST_REQUEST


# ── HELP intent: engine dispatch + utterance ─────────────────────────────────


def make_engine(tmp_path: Any) -> tuple[IncidentEngine, Any]:
    conn = db.connect(":memory:")
    db.migrate(conn)
    holder = StubHolder(make_config())
    engine = IncidentEngine(
        conn,
        holder,  # type: ignore[arg-type]
        FakeGazetteer(entries={}),  # type: ignore[arg-type]
        Discipline(holder),  # type: ignore[arg-type]
        FakePoster(),
        tmp_path / "routing.yaml",
    )
    return engine, conn


async def test_engine_help_speaks_hint_and_logs(tmp_path: Any) -> None:
    engine, conn = make_engine(tmp_path)
    parsed = grammar.parse("Aura Command, help")
    assert parsed is not None

    outcome = await engine.report(1, 42, parsed, None)

    assert outcome.outcome is Outcome.POSTED
    assert outcome.utterance == "Check help in Discord."
    assert outcome.card is None  # posts nothing
    assert outcome.incident_id is None
    row = db.query_one(conn, "SELECT * FROM command_log")
    assert row is not None
    assert row["parsed_intent"] == "HELP"
    assert row["raw_transcript"] == "Aura Command, help"
    assert row["outcome"] == "POSTED"


def test_help_utterance_catalogue() -> None:
    assert tts.help_hint() == "Check help in Discord."


# ── the /help slash twin (thin-adapter wiring, test_subs_cog pattern) ────────


class _Response:
    def __init__(self) -> None:
        self.messages: list[tuple[Any, dict[str, Any]]] = []

    async def send_message(self, content: Any = None, **kwargs: Any) -> None:
        self.messages.append((content, kwargs))


def make_interaction(guild_id: int | None = 1, user_id: int = 42) -> Any:
    return SimpleNamespace(
        guild_id=guild_id,
        guild=None,  # _is_admin sees no guild/Member → non-admin
        user=SimpleNamespace(id=user_id),
        response=_Response(),
    )


class _Engine:
    def __init__(self) -> None:
        self.reports: list[tuple[int, int, Any, Any]] = []

    async def report(self, guild_id: int, user_id: int, parsed: Any, resolution: Any) -> Any:
        self.reports.append((guild_id, user_id, parsed, resolution))
        return IncidentOutcome(Outcome.POSTED, "Check help in Discord.", None, None)


async def test_slash_help_dispatches_through_engine_and_sends_menu() -> None:
    engine = _Engine()
    cog = HelpCog(SimpleNamespace(engine=engine))  # type: ignore[arg-type]
    interaction = make_interaction()

    await HelpCog.help.callback(cog, interaction, None)

    (guild_id, user_id, parsed, resolution) = engine.reports[0]
    assert (guild_id, user_id) == (1, 42)
    assert isinstance(parsed, ParsedCommand)
    assert parsed.intent is Intent.HELP
    assert parsed.raw == "/help"
    assert resolution is None
    ((content, kwargs),) = interaction.response.messages
    assert content is None
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].title == "AURA — voice-activated fleet intel"
    # Non-admin menu: one select, admin topic absent from its options.
    select = kwargs["view"].children[0]
    assert select.custom_id == "aura:help:menu"
    values = [option.value for option in select.options]
    assert "admin" not in values
    assert set(values) == {k for k, t in HELP_TOPICS.items() if not t.admin_only}


async def test_slash_help_topic_sends_that_page() -> None:
    engine = _Engine()
    cog = HelpCog(SimpleNamespace(engine=engine))  # type: ignore[arg-type]
    interaction = make_interaction()

    await HelpCog.help.callback(cog, interaction, "privacy")

    (_, _, parsed, _) = engine.reports[0]
    assert parsed.raw == "/help privacy"
    ((_, kwargs),) = interaction.response.messages
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].title == HELP_TOPICS["privacy"].title


async def test_slash_help_admin_topic_gated() -> None:
    engine = _Engine()
    cog = HelpCog(SimpleNamespace(engine=engine))  # type: ignore[arg-type]
    interaction = make_interaction()

    await HelpCog.help.callback(cog, interaction, "admin")

    ((content, _),) = interaction.response.messages
    assert content == "Admin only — needs Manage Guild or the FC role."


async def test_slash_help_is_guild_only() -> None:
    engine = _Engine()
    cog = HelpCog(SimpleNamespace(engine=engine))  # type: ignore[arg-type]
    interaction = make_interaction(guild_id=None)

    await HelpCog.help.callback(cog, interaction, None)

    assert engine.reports == []
    ((content, _),) = interaction.response.messages
    assert content == "Guild only."
