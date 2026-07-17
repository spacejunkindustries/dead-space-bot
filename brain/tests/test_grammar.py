"""Grammar tests — GDD §6. ``parse`` is a pure function over transcripts."""

from __future__ import annotations

import pytest

from aura.nlu.grammar import clean_callsign, parse, sanitize_callsign, system_reply
from aura.types import Intent

# ── the GDD §6.4 examples ────────────────────────────────────────────────────


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


# ── callsign registry intents (GDD §6.1) ─────────────────────────────────────


def test_register_captures_title_cased_callsign() -> None:
    cmd = parse("Aura Command, register space junkie")
    assert cmd is not None
    assert cmd.intent is Intent.REGISTER
    assert cmd.system_text is None
    assert cmd.detail == "Space Junkie"


@pytest.mark.parametrize(
    "phrase",
    [
        "call me space junkie",
        "register me as space junkie",
        "Aura Command, register Space Junkie",
    ],
)
def test_register_synonyms_and_filler(phrase: str) -> None:
    cmd = parse(phrase)
    assert cmd is not None
    assert cmd.intent is Intent.REGISTER
    assert cmd.detail == "Space Junkie"


def test_register_without_callsign_yields_no_detail() -> None:
    cmd = parse("Aura Command, register")
    assert cmd is not None
    assert cmd.intent is Intent.REGISTER
    assert cmd.detail is None


@pytest.mark.parametrize("phrase", ["unregister", "Aura Command, unregister me", "forget me"])
def test_unregister_synonyms(phrase: str) -> None:
    cmd = parse(phrase)
    assert cmd is not None
    assert cmd.intent is Intent.UNREGISTER
    assert cmd.system_text is None
    assert cmd.detail is None


def test_unregister_never_matches_register() -> None:
    cmd = parse("aura command unregister")
    assert cmd is not None
    assert cmd.intent is Intent.UNREGISTER


@pytest.mark.parametrize("phrase", ["who am I", "Aura Command, who am i", "whoami"])
def test_whoami_synonyms(phrase: str) -> None:
    cmd = parse(phrase)
    assert cmd is not None
    assert cmd.intent is Intent.WHOAMI
    assert cmd.system_text is None
    assert cmd.detail is None


# ── personal pings (GDD §6.1 PING_ME / PING_ME_CLEAR) ────────────────────────

ALL_TYPES = "HOSTILE_SPOTTED,UNDER_ATTACK,ASSIST_REQUEST,GATE_CAMP"


def test_ping_me_gate_camps_with_system() -> None:
    cmd = parse("Aura Command, ping me for gate camps in Otanuomi")
    assert cmd is not None
    assert cmd.intent is Intent.PING_ME
    assert cmd.system_text == "Otanuomi"
    assert cmd.detail == "GATE_CAMP"


@pytest.mark.parametrize(
    ("phrase", "detail"),
    [
        ("ping me for hostiles in Kisogo", "HOSTILE_SPOTTED"),
        ("ping me for reds in Kisogo", "HOSTILE_SPOTTED"),
        ("ping me for neuts in Kisogo", "HOSTILE_SPOTTED"),
        ("ping me for gate camps in Kisogo", "GATE_CAMP"),
        ("ping me for gate camp in Kisogo", "GATE_CAMP"),
        ("ping me for under attack in Kisogo", "UNDER_ATTACK"),
        ("ping me for attacks in Kisogo", "UNDER_ATTACK"),
        ("ping me for tackled in Kisogo", "UNDER_ATTACK"),
        ("ping me for need help in Kisogo", "ASSIST_REQUEST"),
        ("ping me for need backup in Kisogo", "ASSIST_REQUEST"),
        ("ping me for assist requests in Kisogo", "ASSIST_REQUEST"),
    ],
)
def test_ping_me_type_word_matrix(phrase: str, detail: str) -> None:
    """Each §6.1 type synonym maps to its incident type; PING_ME wins the
    intent even though the utterance contains report type words."""
    cmd = parse(phrase)
    assert cmd is not None
    assert cmd.intent is Intent.PING_ME
    assert cmd.detail == detail
    assert cmd.system_text == "Kisogo"


@pytest.mark.parametrize(
    "phrase",
    [
        "ping me for anything",
        "ping me for everything",
        "ping me for all",
        "ping me for everything everywhere",
    ],
)
def test_ping_me_anything_covers_all_types_everywhere(phrase: str) -> None:
    cmd = parse(phrase)
    assert cmd is not None
    assert cmd.intent is Intent.PING_ME
    assert cmd.detail == ALL_TYPES
    assert cmd.system_text is None  # "everywhere" is never a system window


def test_ping_me_without_type_words_defaults_to_all_types() -> None:
    cmd = parse("ping me in Otanuomi")
    assert cmd is not None
    assert cmd.intent is Intent.PING_ME
    assert cmd.detail == ALL_TYPES
    assert cmd.system_text == "Otanuomi"


def test_ping_me_multiple_type_words() -> None:
    cmd = parse("ping me for hostiles and gate camps in Otanuomi")
    assert cmd is not None
    assert cmd.detail == "HOSTILE_SPOTTED,GATE_CAMP"
    assert cmd.system_text == "Otanuomi"


def test_ping_me_without_system_covers_everywhere() -> None:
    cmd = parse("Aura Command, ping me for gate camps")
    assert cmd is not None
    assert cmd.intent is Intent.PING_ME
    assert cmd.detail == "GATE_CAMP"
    assert cmd.system_text is None


@pytest.mark.parametrize(
    "phrase",
    ["stop pinging me", "Aura Command, stop pinging me", "stop pinging", "stop pings"],
)
def test_ping_me_clear_synonyms(phrase: str) -> None:
    cmd = parse(phrase)
    assert cmd is not None
    assert cmd.intent is Intent.PING_ME_CLEAR
    assert cmd.system_text is None
    assert cmd.detail is None


def test_stop_pinging_never_matches_ping_me() -> None:
    cmd = parse("aura command stop pinging me")
    assert cmd is not None
    assert cmd.intent is Intent.PING_ME_CLEAR


def test_tackled_without_ping_me_still_under_attack() -> None:
    """The severity-first rule is untouched for genuine reports."""
    cmd = parse("tackled in Kisogo")
    assert cmd is not None
    assert cmd.intent is Intent.UNDER_ATTACK


# ── callsign sanitisation ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("space junkie", "Space Junkie"),
        ("me as space junkie", "Space Junkie"),
        ("@space #junkie", "Space Junkie"),
        ("`space` <junkie>", "Space Junkie"),
        ("**space** __junkie__", "Space Junkie"),
        ("  space   junkie  ", "Space Junkie"),
        ("", None),
        ("@#`<>", None),
        ("as me my", None),
    ],
)
def test_clean_callsign(raw: str, expected: str | None) -> None:
    assert clean_callsign(raw) == expected


def test_clean_callsign_caps_at_32_chars() -> None:
    cleaned = clean_callsign("a" * 60)
    assert cleaned is not None
    assert len(cleaned) <= 32


def test_sanitize_callsign_preserves_typed_case() -> None:
    """Slash input is exact: no title-casing, only markdown/mention strip."""
    assert sanitize_callsign("xX SpaceJunkie Xx") == "xX SpaceJunkie Xx"
    assert sanitize_callsign("<@12345> `boom`") == "12345 boom"
    assert sanitize_callsign("   ") is None


# ── system_reply (GDD §8.3 "say again" retry window) ─────────────────────────


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("Kisogo", "Kisogo"),
        ("in Kisogo", "Kisogo"),
        ("Aura Command, Kisogo", "Kisogo"),
        ("uh, Otanuomi", "Otanuomi"),
        ("", None),
        ("   ", None),
        ("um uh", None),
    ],
)
def test_system_reply_normalises_bare_names(transcript: str, expected: str | None) -> None:
    assert system_reply(transcript) == expected


# ── STT mishearing tolerance + radio sign-off (voice UX fixes) ────────────────


@pytest.mark.parametrize(
    "transcript",
    [
        "hustiles Jita",
        "Hustiles, Jita",
        "hostels jita",
        "ostiles jita",
    ],
)
def test_hostiles_mishearings_normalize(transcript: str) -> None:
    parsed = parse(transcript)
    assert parsed is not None
    assert parsed.intent is Intent.HOSTILE_SPOTTED
    assert parsed.system_text is not None


def test_neuts_mishearing_normalizes() -> None:
    parsed = parse("newts in Amarr")
    assert parsed is not None
    assert parsed.intent is Intent.HOSTILE_SPOTTED


def test_gate_camp_mishearing_normalizes() -> None:
    parsed = parse("gate champ Otanuomi")
    assert parsed is not None
    assert parsed.intent is Intent.GATE_CAMP


@pytest.mark.parametrize("signoff", ["over", "out", "over and out", "copy", "roger"])
def test_radio_signoff_stripped(signoff: str) -> None:
    parsed = parse(f"under attack Kisogo {signoff}")
    assert parsed is not None
    assert parsed.intent is Intent.UNDER_ATTACK
    assert parsed.system_text == "Kisogo"
    # the sign-off word never survives into the report
    assert signoff.split()[0] not in (parsed.detail or "").lower()


def test_signoff_only_at_the_tail() -> None:
    # "over" mid-utterance (part of a real word/name) is not a sign-off.
    parsed = parse("hostiles Jita, overheating, over")
    assert parsed is not None
    assert parsed.intent is Intent.HOSTILE_SPOTTED
    assert "overheating" in (parsed.detail or "").lower()
