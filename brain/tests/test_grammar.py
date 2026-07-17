"""Grammar tests — GDD §6. ``parse`` is a pure function over transcripts."""

from __future__ import annotations

import pytest

from aura.nlu.grammar import parse
from aura.types import Intent

# ── the nine GDD §6.4 examples ───────────────────────────────────────────────


def test_hostiles_with_detail() -> None:
    cmd = parse("Aura Command, hostiles Otanuomi, three battleships")
    assert cmd is not None
    assert cmd.intent is Intent.HOSTILE_SPOTTED
    assert cmd.system_text == "Otanuomi"
    assert cmd.detail == "three battleships"
    assert cmd.group_alias is None
    assert cmd.raw == "Aura Command, hostiles Otanuomi, three battleships"


def test_tackled_need_help() -> None:
    cmd = parse("Aura Command, tackled in Kisogo, need help")
    assert cmd is not None
    assert cmd.intent is Intent.UNDER_ATTACK
    assert cmd.system_text == "Kisogo"


def test_gate_camp_miners_only() -> None:
    cmd = parse("Aura Command, gate camp Otanuomi, miners only")
    assert cmd is not None
    assert cmd.intent is Intent.GATE_CAMP
    assert cmd.system_text == "Otanuomi"
    assert cmd.group_alias == "miners"


def test_clear() -> None:
    cmd = parse("Aura Command, clear Otanuomi")
    assert cmd is not None
    assert cmd.intent is Intent.RESOLVE
    assert cmd.system_text == "Otanuomi"


def test_timer_duration_stays_in_detail() -> None:
    cmd = parse("Aura Command, timer Kisogo four hours")
    assert cmd is not None
    assert cmd.intent is Intent.TIMER
    assert cmd.system_text == "Kisogo"
    assert cmd.detail == "four hours"


def test_formup() -> None:
    cmd = parse("Aura Command, form up Otanuomi fifteen minutes")
    assert cmd is not None
    assert cmd.intent is Intent.FORMUP
    assert cmd.system_text == "Otanuomi"
    assert cmd.detail == "fifteen minutes"


def test_status() -> None:
    cmd = parse("Aura Command, status")
    assert cmd is not None
    assert cmd.intent is Intent.QUERY
    assert cmd.system_text is None
    assert cmd.detail is None


def test_cancel() -> None:
    cmd = parse("Aura Command, cancel")
    assert cmd is not None
    assert cmd.intent is Intent.CANCEL


# ── severity precedence (GDD §6.1) ───────────────────────────────────────────


def test_tackled_beats_need_help() -> None:
    """The GDD's own example: "tackled, need help in Kisogo" → UNDER_ATTACK."""
    cmd = parse("tackled, need help in Kisogo")
    assert cmd is not None
    assert cmd.intent is Intent.UNDER_ATTACK
    assert cmd.system_text == "Kisogo"


def test_under_attack_beats_hostiles() -> None:
    cmd = parse("hostiles everywhere, we are under attack in Alenia")
    assert cmd is not None
    assert cmd.intent is Intent.UNDER_ATTACK
    assert cmd.system_text == "Alenia"


@pytest.mark.parametrize(
    ("phrase", "intent"),
    [
        ("reds Otanuomi", Intent.HOSTILE_SPOTTED),
        ("neuts Otanuomi", Intent.HOSTILE_SPOTTED),
        ("point on me Kisogo", Intent.UNDER_ATTACK),
        ("need backup Kisogo", Intent.ASSIST_REQUEST),
        ("need help Kisogo", Intent.ASSIST_REQUEST),
    ],
)
def test_intent_synonyms(phrase: str, intent: Intent) -> None:
    cmd = parse(phrase)
    assert cmd is not None
    assert cmd.intent is intent


# ── group targeting (GDD §6.2) ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("phrase", "alias"),
    [
        ("hostiles Otanuomi miners only", "miners"),
        ("hostiles Otanuomi defense only", "defense"),
        ("hostiles Otanuomi defence only", "defense"),
        ("under attack Kisogo all hands", "all_hands"),
        ("all hands under attack Kisogo", "all_hands"),
    ],
)
def test_group_aliases(phrase: str, alias: str) -> None:
    cmd = parse(phrase)
    assert cmd is not None
    assert cmd.group_alias == alias
    assert cmd.system_text is not None
    assert "only" not in cmd.system_text
    assert "hands" not in cmd.system_text


# ── STT realities ────────────────────────────────────────────────────────────


def test_mangled_system_name_passes_through() -> None:
    """The resolver owns matching — grammar passes the raw window through."""
    cmd = parse("hostiles oh tan you oh me")
    assert cmd is not None
    assert cmd.system_text == "oh tan you oh me"


def test_wake_residue_variants_stripped() -> None:
    for prefix in ("Aura Command,", "aura command", "Ora command ,", "hey aura command:"):
        cmd = parse(f"{prefix} clear Kisogo")
        assert cmd is not None, prefix
        assert cmd.intent is Intent.RESOLVE
        assert cmd.system_text == "Kisogo"


def test_filler_stripped_from_system_window() -> None:
    cmd = parse("under attack in the Kisogo")
    assert cmd is not None
    assert cmd.system_text == "Kisogo"


def test_timer_without_duration_yields_no_detail() -> None:
    cmd = parse("timer Kisogo")
    assert cmd is not None
    assert cmd.intent is Intent.TIMER
    assert cmd.system_text == "Kisogo"
    assert cmd.detail is None


def test_timer_numeric_duration() -> None:
    cmd = parse("timer Kisogo 45 minutes")
    assert cmd is not None
    assert cmd.system_text == "Kisogo"
    assert cmd.detail == "45 minutes"


def test_no_intent_returns_none() -> None:
    assert parse("random chatter about mining fleets") is None
    assert parse("") is None
    assert parse("   ") is None


def test_hostiles_without_system() -> None:
    cmd = parse("hostiles")
    assert cmd is not None
    assert cmd.intent is Intent.HOSTILE_SPOTTED
    assert cmd.system_text is None
