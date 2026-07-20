"""Killboard model tests — pure parsers (killboard GDD §6, §11).

No I/O: ``killboard.model`` is a pure transformation of decoded gameinfo dicts,
so every test here is a hand-written fixture. Pins classification, tolerant
partial-JSON parsing, and participant extraction.
"""

from __future__ import annotations

from typing import Any

from killboard.model import (
    ASSIST,
    DEATH,
    KILL,
    EventRow,
    Participant,
    classify,
    parse_event,
    participants_of,
)

GUILD = "guild-tracked"
OTHER = "guild-other"


def _event(**overrides: Any) -> dict[str, Any]:
    """A well-formed baseline event; override individual keys per test."""
    base: dict[str, Any] = {
        "EventId": 42,
        "TimeStamp": "2026-07-20T12:00:00Z",
        "Killer": {"Id": "k1", "Name": "Killer", "GuildId": OTHER, "AverageItemPower": 1200.0},
        "Victim": {"Id": "v1", "Name": "Victim", "GuildId": OTHER, "AverageItemPower": 1000.0},
        "TotalVictimKillFame": 50000,
        "numberOfParticipants": 1,
        "Participants": [],
        "BattleId": 777,
        "Location": "Martlock",
    }
    base.update(overrides)
    return base


# ── classify ─────────────────────────────────────────────────────────────────


def test_classify_kill_when_guild_is_killer() -> None:
    ev = _event(Killer={"Id": "k1", "GuildId": GUILD})
    assert classify(ev, GUILD) == KILL


def test_classify_death_when_guild_is_victim() -> None:
    ev = _event(Victim={"Id": "v1", "GuildId": GUILD})
    assert classify(ev, GUILD) == DEATH


def test_classify_assist_when_guild_is_participant_only() -> None:
    ev = _event(
        Killer={"Id": "k1", "GuildId": OTHER},
        Victim={"Id": "v1", "GuildId": OTHER},
        Participants=[{"Id": "p1", "GuildId": GUILD, "DamageDone": 500}],
    )
    assert classify(ev, GUILD) == ASSIST


def test_classify_none_when_uninvolved() -> None:
    ev = _event(
        Killer={"Id": "k1", "GuildId": OTHER},
        Victim={"Id": "v1", "GuildId": OTHER},
        Participants=[{"Id": "p1", "GuildId": OTHER}],
    )
    assert classify(ev, GUILD) is None


def test_classify_kill_takes_precedence_over_participant() -> None:
    # Guild is both the killer and a listed participant — KILL wins.
    ev = _event(
        Killer={"Id": "k1", "GuildId": GUILD},
        Participants=[{"Id": "k1", "GuildId": GUILD}],
    )
    assert classify(ev, GUILD) == KILL


def test_classify_none_for_blank_target_guild() -> None:
    ev = _event(Killer={"Id": "k1", "GuildId": GUILD})
    assert classify(ev, "") is None
    assert classify(ev, "   ") is None


def test_classify_blank_guild_field_does_not_match_blank_target() -> None:
    # Empty GuildId on the event collapses to None, never matching the guild.
    ev = _event(Killer={"Id": "k1", "GuildId": ""}, Victim={"Id": "v1", "GuildId": ""})
    assert classify(ev, GUILD) is None


# ── parse_event: tolerance ───────────────────────────────────────────────────


def test_parse_event_none_when_event_id_absent() -> None:
    ev = _event(Killer={"Id": "k1", "GuildId": GUILD})
    del ev["EventId"]
    assert parse_event(ev, GUILD) is None


def test_parse_event_none_when_event_id_unparseable() -> None:
    ev = _event(EventId="not-a-number", Killer={"Id": "k1", "GuildId": GUILD})
    assert parse_event(ev, GUILD) is None


def test_parse_event_none_when_guild_uninvolved() -> None:
    ev = _event(
        Killer={"Id": "k1", "GuildId": OTHER},
        Victim={"Id": "v1", "GuildId": OTHER},
    )
    assert parse_event(ev, GUILD) is None


def test_parse_event_none_for_non_dict() -> None:
    assert parse_event(None, GUILD) is None  # type: ignore[arg-type]
    assert parse_event([1, 2, 3], GUILD) is None  # type: ignore[arg-type]
    assert parse_event("junk", GUILD) is None  # type: ignore[arg-type]


def test_parse_event_full_row() -> None:
    ev = _event(Killer={"Id": "k1", "Name": "K", "GuildId": GUILD, "AverageItemPower": 1234.5})
    row = parse_event(ev, GUILD)
    assert isinstance(row, EventRow)
    assert row.event_id == 42
    assert row.timestamp == "2026-07-20T12:00:00Z"
    assert row.killer_id == "k1"
    assert row.killer_name == "K"
    assert row.killer_guild_id == GUILD
    assert row.killer_ip == 1234.5
    assert row.victim_id == "v1"
    assert row.total_fame == 50000
    assert row.relation == KILL
    assert row.num_participants == 1
    assert row.battle_id == 777
    assert row.location == "Martlock"


def test_parse_event_string_event_id_coerced() -> None:
    ev = _event(EventId="99", Killer={"Id": "k1", "GuildId": GUILD})
    row = parse_event(ev, GUILD)
    assert row is not None
    assert row.event_id == 99


def test_parse_event_tolerates_missing_optional_fields() -> None:
    # Only the fields needed to key + classify are present; everything else gone.
    ev: dict[str, Any] = {"EventId": 7, "Killer": {"GuildId": GUILD}}
    row = parse_event(ev, GUILD)
    assert row is not None
    assert row.event_id == 7
    assert row.relation == KILL
    assert row.timestamp == ""  # missing TimeStamp → empty string, not None
    assert row.killer_id is None
    assert row.killer_name is None
    assert row.killer_ip is None
    assert row.victim_id is None
    assert row.victim_guild_id is None
    assert row.total_fame == 0  # null fame → 0, never None (rankings sum over it)
    assert row.num_participants == 0
    assert row.battle_id is None
    assert row.location is None


def test_parse_event_tolerates_null_subobjects() -> None:
    ev = _event(Killer={"Id": "k1", "GuildId": GUILD}, Victim=None)
    row = parse_event(ev, GUILD)
    assert row is not None
    assert row.victim_id is None
    assert row.victim_guild_id is None
    assert row.victim_ip is None


def test_parse_event_null_fame_defaults_to_zero() -> None:
    ev = _event(Killer={"Id": "k1", "GuildId": GUILD}, TotalVictimKillFame=None)
    row = parse_event(ev, GUILD)
    assert row is not None
    assert row.total_fame == 0


def test_parse_event_location_falls_back_to_kill_location_key() -> None:
    ev = _event(Killer={"Id": "k1", "GuildId": GUILD})
    del ev["Location"]
    ev["KillLocation"] = "Lymhurst"
    row = parse_event(ev, GUILD)
    assert row is not None
    assert row.location == "Lymhurst"


def test_parse_event_num_participants_falls_back_to_array_length() -> None:
    ev = _event(
        Killer={"Id": "k1", "GuildId": GUILD},
        Participants=[{"Id": "a"}, {"Id": "b"}, {"Id": "c"}],
    )
    del ev["numberOfParticipants"]
    row = parse_event(ev, GUILD)
    assert row is not None
    assert row.num_participants == 3


# ── participants_of ──────────────────────────────────────────────────────────


def test_participants_of_absent_returns_empty() -> None:
    assert participants_of({"EventId": 1}) == []


def test_participants_of_null_returns_empty() -> None:
    assert participants_of({"Participants": None}) == []


def test_participants_of_non_list_returns_empty() -> None:
    assert participants_of({"Participants": {"Id": "x"}}) == []


def test_participants_of_skips_non_dict_entries() -> None:
    ev = {"Participants": [{"Id": "p1", "GuildId": GUILD}, "junk", None, 5]}
    parts = participants_of(ev)
    assert len(parts) == 1
    assert parts[0].player_id == "p1"


def test_participants_of_parses_damage_and_heal() -> None:
    ev = {
        "Participants": [
            {
                "Id": "p1",
                "Name": "Alice",
                "GuildId": GUILD,
                "DamageDone": 1500.5,
                "SupportHealingDone": 200.0,
            }
        ]
    }
    parts = participants_of(ev)
    assert parts == [
        Participant(
            player_id="p1",
            player_name="Alice",
            guild_id=GUILD,
            damage_done=1500.5,
            healing_done=200.0,
        )
    ]


def test_participants_of_defaults_missing_numbers_to_zero() -> None:
    ev = {"Participants": [{"Id": "p1", "GuildId": GUILD}]}
    parts = participants_of(ev)
    assert parts[0].damage_done == 0.0
    assert parts[0].healing_done == 0.0
