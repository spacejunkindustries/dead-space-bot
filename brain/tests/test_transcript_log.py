"""STT transcript review-log line formatting — GDD §8.7.

``transcript_line`` is a pure function: given what CORTANA heard and how the
grammar parsed it, produce the single scannable line posted to
``discord.channels.transcript``. Transcript only — no audio, no user id
(constraint 5)."""

from __future__ import annotations

from cortana.dialog.engine import transcript_line
from cortana.nlu.grammar import parse
from cortana.types import Intent, Severity


def test_parsed_command_line_names_intent_and_system() -> None:
    parsed = parse("under attack in Kisogo")
    line = transcript_line("hey cortana under attack in Kisogo", -0.21, parsed)
    assert "under attack in kisogo" in line.lower()
    assert Intent.UNDER_ATTACK.value in line
    assert "Kisogo" in line
    assert "-0.21" in line


def test_no_command_line_says_so() -> None:
    line = transcript_line("uh what was that", -1.4, None)
    assert "no command" in line
    assert "-1.40" in line


def test_silence_is_labelled() -> None:
    assert "(silence)" in transcript_line("   ", -3.0, None)


def test_spoken_code_is_shown() -> None:
    parsed = parse("code red hostiles Otanuomi")
    line = transcript_line("code red hostiles Otanuomi", -0.1, parsed)
    assert Intent.HOSTILE_SPOTTED.value in line
    assert f"code {Severity.HIGH.value}" in line


def test_long_hallucination_is_truncated_to_one_line() -> None:
    huge = "word " * 200
    line = transcript_line(huge, -0.5, None)
    assert "…" in line
    assert len(line) < 260  # a scannable line, never a paragraph
    assert "\n" not in line
