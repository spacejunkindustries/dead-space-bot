"""Grammar tests — GDD §6. ``parse`` is a pure function over transcripts."""

from __future__ import annotations

import pytest

from cortana.nlu.grammar import (
    bare_code,
    broadcast_severity,
    broadcast_text,
    clean_callsign,
    confirm_reply,
    parse,
    sanitize_callsign,
    system_reply,
)
from cortana.types import Intent, Severity

# ── the GDD §6.5 examples ────────────────────────────────────────────────────


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
        # STT drift on the verb tense — Whisper writes what it hears.
        "registered me as space junkie",
        "registering space junkie",
        "my callsign is space junkie",
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


@pytest.mark.parametrize(
    "phrase",
    ["unregister", "Aura Command, unregister me", "forget me", "unregistered me"],
)
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


# ── freeform intel relay (GDD §8.6) ──────────────────────────────────────────


def test_broadcast_text_strips_wake_and_signoff() -> None:
    from cortana.nlu.grammar import broadcast_text

    assert broadcast_text("Hey Jarvis, blop fleet moving to Moe 8 gate, over") == (
        "blop fleet moving to Moe 8 gate"
    )
    assert broadcast_text("jarvis stay docked in branch") == "stay docked in branch"


def test_wants_all_hands() -> None:
    from cortana.nlu.grammar import wants_all_hands

    assert wants_all_hands("cyno up in MOEE-8, all hands")
    assert not wants_all_hands("blop fleet moving to Moe 8")


@pytest.mark.parametrize(
    "signoff",
    ["over", "over!", "rover", "over and out", "over out", "ova"],
)
def test_over_variants_stripped(signoff: str) -> None:
    p = parse(f"hostiles Jita {signoff}")
    assert p is not None
    assert p.intent is Intent.HOSTILE_SPOTTED
    assert p.system_text == "Jita"


# ── spoken colour codes (GDD §6.4) ───────────────────────────────────────────


def test_inline_code_sets_severity_and_never_claims_red() -> None:
    cmd = parse("code red hostiles in umi")
    assert cmd is not None
    # "red" belongs to the colour code, not the HOSTILE_SPOTTED \breds?\b —
    # the intent must come from "hostiles" and the code must carry HIGH.
    assert cmd.intent is Intent.HOSTILE_SPOTTED
    assert cmd.severity is Severity.HIGH
    assert cmd.system_text == "umi"


def test_inline_code_orange_on_a_report() -> None:
    cmd = parse("code orange, hostiles in Otanuomi, three battleships")
    assert cmd is not None
    assert cmd.intent is Intent.HOSTILE_SPOTTED
    assert cmd.severity is Severity.MEDIUM
    assert cmd.system_text == "Otanuomi"


def test_no_code_leaves_severity_none() -> None:
    cmd = parse("hostiles in Otanuomi")
    assert cmd is not None
    assert cmd.severity is None


@pytest.mark.parametrize(
    ("phrase", "severity"),
    [
        ("code red", Severity.HIGH),
        ("hey jarvis, code orange", Severity.MEDIUM),
        ("Code yellow.", Severity.NONE),
        ("hey cortana code red over", Severity.HIGH),
    ],
)
def test_bare_code_detected(phrase: str, severity: Severity) -> None:
    assert bare_code(phrase) is severity


def test_bare_code_rejects_utterances_with_content() -> None:
    assert bare_code("code orange hostiles in umi") is None
    assert bare_code("no code here") is None


def test_broadcast_severity_and_code_stripped_from_text() -> None:
    assert broadcast_severity("code red blop fleet inbound") is Severity.HIGH
    assert broadcast_text("code red blop fleet inbound") == "blop fleet inbound"
    assert broadcast_severity("blop fleet inbound") is None


# ── report envelope and STT-drift fixes ──────────────────────────────────────


def test_report_envelope_stripped() -> None:
    cmd = parse("report, I been tackled in umi, end report")
    assert cmd is not None
    assert cmd.intent is Intent.UNDER_ATTACK
    assert cmd.system_text == "umi"


def test_end_of_report_signoff_variants() -> None:
    for tail in ("end report", "end of report", "report ends", "end transmission"):
        cmd = parse(f"hostiles in Otanuomi {tail}")
        assert cmd is not None, tail
        assert cmd.system_text == "Otanuomi", tail


def test_regester_misspelling_registers() -> None:
    # A real fleet said "Register Space Junkie"; Whisper wrote "Regester" and
    # the command fell through to the relay. Never again.
    cmd = parse("Regester Space Junkie")
    assert cmd is not None
    assert cmd.intent is Intent.REGISTER
    assert cmd.detail == "Space Junkie"


def test_jarvis_and_cortana_wake_residue_stripped() -> None:
    for wake in ("hey jarvis", "hey cortana", "cortana"):
        cmd = parse(f"{wake} hostiles in Otanuomi")
        assert cmd is not None, wake
        assert cmd.system_text == "Otanuomi", wake


def test_stuttered_hallucination_collapses_in_relay() -> None:
    assert broadcast_text("Rens, Rens, Rens") == "Rens"
    assert broadcast_text("Rens Rens") == "Rens Rens"  # 2x = emphasis, kept


# ── command override (GDD §6.6) ──────────────────────────────────────────────


def test_override_query_extracts_question() -> None:
    from cortana.nlu.grammar import override_query

    q = override_query("hey cortana command override please tell me the weather in Chicago")
    assert q == "please tell me the weather in Chicago"
    assert override_query("override, what's the capital of France, over") == (
        "what's the capital of France"
    )


def test_override_never_diverts_reports() -> None:
    from cortana.nlu.grammar import override_query

    # "override" mid-sentence is a report word, not the doorway.
    assert override_query("hostiles are trying to override the gate in Kisogo") is None
    assert override_query("hostiles in Otanuomi") is None
    # A bare "command override" with no question is not a query either.
    assert override_query("command override") is None


def test_override_accepts_stt_phonetic_renderings() -> None:
    # STT renders "override" phonetically; the doorway must still open.
    from cortana.nlu.grammar import override_query

    expected = "what's the weather in Chicago"
    for heard in (
        "command over ride what's the weather in Chicago",
        "command over-ride what's the weather in Chicago",
        "command overide what's the weather in Chicago",
        "command overdrive what's the weather in Chicago",
        "command overwrite what's the weather in Chicago",
        "commander override what's the weather in Chicago",
        "hey cortana over ride what's the weather in Chicago",
    ):
        assert override_query(heard) == expected, heard


# ── relay framing (GDD §8.6, relay_mode: framed) ─────────────────────────────


def test_relay_framed_accepts_explicit_frames() -> None:
    from cortana.nlu.grammar import relay_framed

    assert relay_framed("report blop fleet on the Kisogo gate end report")
    assert relay_framed("hey jarvis reporting fleet movement to Rens, over")
    assert relay_framed("code red blop fleet inbound")
    assert relay_framed("cyno up, all hands")


def test_relay_framed_rejects_unframed_speech() -> None:
    from cortana.nlu.grammar import relay_framed

    # The junk that used to become CODE YELLOW cards: crosstalk, lone system
    # names, hallucinated repeats.
    assert not relay_framed("Arvas")
    assert not relay_framed("How's everybody else doing")
    assert not relay_framed("Rens, Rens, Rens")
    assert not relay_framed("hey jarvis Kisogo")
    assert not relay_framed("")


# ── chase mode + "system" noise word (GDD §13 / §6.3) ────────────────────────


def test_system_noise_word_is_stripped_from_the_window() -> None:
    from cortana.nlu.grammar import parse

    p = parse("I'm tackled code red in system UMI over")
    assert p is not None
    assert p.intent is Intent.UNDER_ATTACK
    assert p.system_text == "UMI"
    assert p.severity is Severity.HIGH

    p2 = parse("hostiles code orange system Otanuomi, three battleships over")
    assert p2 is not None
    assert p2.system_text == "Otanuomi"
    assert p2.detail == "three battleships"


def test_chase_update_parses_with_and_without_update_prefix() -> None:
    from cortana.nlu.grammar import parse

    for heard in ("update chase Kisogo", "chase Kisogo", "hey cortana update chase mode UMI over"):
        p = parse(heard)
        assert p is not None, heard
        assert p.intent is Intent.CHASE_UPDATE, heard
        assert p.system_text in ("Kisogo", "UMI")


def test_bare_chase_mode_parses_without_system() -> None:
    from cortana.nlu.grammar import parse

    p = parse("hey cortana chase mode")
    assert p is not None
    assert p.intent is Intent.CHASE_UPDATE
    assert p.system_text is None


def test_distress_words_always_beat_chase() -> None:
    from cortana.nlu.grammar import parse

    # "tackled ... chase" is a distress call, never a chase command.
    p = parse("I'm tackled in Kisogo they're giving chase")
    assert p is not None
    assert p.intent is Intent.UNDER_ATTACK


def test_mid_sentence_chase_chatter_never_claims_the_intent() -> None:
    from cortana.nlu.grammar import parse

    # A live card must never be silently retargeted by chatter.
    assert parse("let's chase them down") is None
    assert parse("we should chase him") is None
    # The explicit forms work anywhere; bare "chase" only leads.
    p = parse("okay update chase Alenia")
    assert p is not None and p.intent is Intent.CHASE_UPDATE


def test_chase_terminators_never_become_system_names() -> None:
    from cortana.nlu.grammar import parse

    for heard in ("chase mode off", "chase is over", "chase done", "chase stopped"):
        p = parse(heard)
        assert p is not None and p.intent is Intent.CHASE_UPDATE, heard
        assert p.system_text is None, heard
    # "chase cancelled" is a CANCEL; "chase done, clear Kisogo" is a clear.
    p = parse("chase cancelled")
    assert p is not None and p.intent is Intent.CANCEL
    p = parse("chase done, clear Kisogo")
    assert p is not None and p.intent is Intent.RESOLVE and p.system_text == "Kisogo"


def test_chase_mode_mid_sentence_never_claims() -> None:
    from cortana.nlu.grammar import parse

    p = parse("we're in chase mode after the vexor")
    assert p is None or p.intent is not Intent.CHASE_UPDATE


def test_callsign_starting_with_system_survives() -> None:
    from cortana.nlu.grammar import parse

    p = parse("register system junkie")
    assert p is not None and p.intent is Intent.REGISTER
    assert p.detail == "System Junkie"


@pytest.mark.parametrize(
    "heard",
    [
        "pin me for gate camps in moee 8",  # the live mis-hearing
        "pink me for gate camp and moee 8",
        "pinging me for hostiles in otanuomi",
        "ping-me for camps in kisogo",
    ],
)
def test_ping_me_survives_phonetic_mangling(heard: str) -> None:
    """Live incident: STT wrote 'ping me' as pin/pink/pinging me, the strict
    pattern missed, and 'gate camp' fell through to GATE_CAMP — posting a
    junk camp card instead of subscribing. Constraint 7 applies to command
    words too."""
    cmd = parse(heard)
    assert cmd is not None
    assert cmd.intent is Intent.PING_ME


def test_stop_pinging_survives_phonetic_mangling() -> None:
    for heard in ("stop pinning me", "stop pinking me", "stop pings"):
        cmd = parse(heard)
        assert cmd is not None
        assert cmd.intent is Intent.PING_ME_CLEAR


# ── freeform padding + spelled system names (GDD §6.1 / §8.2) ────────────────


def test_operator_acceptance_sentence() -> None:
    """The spec sentence: tackled outranks the assist phrasing, and the
    spelled system survives the padding intact."""
    cmd = parse(
        "please report that i am tackled by enemies in system m tack o"
        " and request heavy assistance please"
    )
    assert cmd is not None
    assert cmd.intent is Intent.UNDER_ATTACK
    assert cmd.system_text == "m tack o"


@pytest.mark.parametrize(
    ("heard", "intent", "system_text"),
    [
        # tack-spelled names, "in system" anchored
        (
            "please report that i am tackled by enemies in system m tack o"
            " and request heavy assistance please",
            Intent.UNDER_ATTACK,
            "m tack o",
        ),
        (
            "hey cortana i am under attack in system u m i tack k k please send help",
            Intent.UNDER_ATTACK,
            "u m i tack k k",
        ),
        (
            "request backup in system one d q one tack a please",
            Intent.ASSIST_REQUEST,
            "one d q one tack a",
        ),
        # tack-spelled, plain "in"/"at" anchors
        (
            "we are under attack at m tack o double e 8 send reinforcements now",
            Intent.UNDER_ATTACK,
            "m tack o double e 8",
        ),
        ("hostiles reported in m tack o, three battleships", Intent.HOSTILE_SPOTTED, "m tack o"),
        (
            "please be advised hostiles in u m i tack k k right now",
            Intent.HOSTILE_SPOTTED,
            "u m i tack k k",
        ),
        (
            "please ping me for gate camps in m tack o double e 8",
            Intent.PING_ME,
            "m tack o double e 8",
        ),
        # plain names, anchored
        ("we are requesting heavy assistance in kisogo", Intent.ASSIST_REQUEST, "kisogo"),
        (
            "requesting immediate assistance in system kisogo please",
            Intent.ASSIST_REQUEST,
            "kisogo",
        ),
        (
            "kindly be advised there are hostiles in otanuomi right now",
            Intent.HOSTILE_SPOTTED,
            "otanuomi",
        ),
        ("there is a gate camp at otanuomi, three destroyers", Intent.GATE_CAMP, "otanuomi"),
        ("i'm tackled in kisogo please help", Intent.UNDER_ATTACK, "kisogo"),
        (
            "we are under attack in system otanuomi kindly send backup immediately",
            Intent.UNDER_ATTACK,
            "otanuomi",
        ),
        ("timer in kisogo for four hours please", Intent.TIMER, "kisogo"),
        ("please form up at kisogo in fifteen minutes", Intent.FORMUP, "kisogo"),
        ("hostiles in moe 8 please", Intent.HOSTILE_SPOTTED, "moe 8"),
        # plain names, no anchor at all
        ("request support otanuomi", Intent.ASSIST_REQUEST, "otanuomi"),
        ("hey cortana please clear kisogo thank you", Intent.RESOLVE, "kisogo"),
    ],
)
def test_padded_real_speech_matrix(heard: str, intent: Intent, system_text: str) -> None:
    """Courtesy/narrative padding never breaks intent or system extraction."""
    cmd = parse(heard)
    assert cmd is not None
    assert cmd.intent is intent
    assert cmd.system_text is not None
    assert cmd.system_text.lower() == system_text


@pytest.mark.parametrize(
    "heard",
    [
        "request assistance",
        "requesting heavy assistance",
        "request immediate backup",
        "requesting support",
        "need reinforcements",
        "need assistance",
    ],
)
def test_request_assistance_phrasings_are_assist_requests(heard: str) -> None:
    cmd = parse(heard)
    assert cmd is not None
    assert cmd.intent is Intent.ASSIST_REQUEST
    assert cmd.system_text is None


def test_tackled_outranks_request_assistance() -> None:
    cmd = parse("request heavy assistance we are tackled")
    assert cmd is not None
    assert cmd.intent is Intent.UNDER_ATTACK


def test_courtesy_chatter_without_intent_still_drops() -> None:
    assert parse("please kindly thank you") is None


def test_spelled_article_a_survives_the_window() -> None:
    """ "a" is an article everywhere except inside a spelling."""
    cmd = parse("tackled in one d q one tack a")
    assert cmd is not None
    assert cmd.system_text == "one d q one tack a"
    # …but a real article is still stripped.
    cmd2 = parse("under attack in the Kisogo")
    assert cmd2 is not None
    assert cmd2.system_text == "Kisogo"


# ── confirm-window replies (GDD §8.3 AWAIT_CONFIRM) ──────────────────────────


@pytest.mark.parametrize(
    "heard",
    [
        "yes",
        "Yes.",
        "yeah",
        "yep",
        "aye",
        "affirmative",
        "confirm",
        "confirmed",
        "correct",
        "do it",
        "yeah do it",
        "yes please",
        "confirm it, over",  # radio tail rides along
        "roger",  # radio ack — would vanish under the sign-off strip
        "copy that",
        "hey cortana yes",  # wake residue survives into the window transcript
    ],
)
def test_confirm_reply_affirmatives(heard: str) -> None:
    assert confirm_reply(heard) == "yes"


@pytest.mark.parametrize(
    "heard",
    [
        "no",
        "No.",
        "nope",
        "negative",
        "cancel",
        "wrong",
        "no that's wrong",
        "belay that",
        "yes— no, cancel",  # any negative vetoes: destructive confirms fail closed
    ],
)
def test_confirm_reply_negatives(heard: str) -> None:
    assert confirm_reply(heard) == "no"


@pytest.mark.parametrize(
    "heard",
    [
        "",
        "   ",
        "yes hostiles Kisogo",  # a command, not a reply — the grammar claims it
        "clear Otanuomi",
        "mumble static",
        "Otanuomi",
        "thank you",
    ],
)
def test_confirm_reply_other_content_is_neither(heard: str) -> None:
    assert confirm_reply(heard) is None
