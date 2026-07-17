"""Fixed voice-command grammar — GDD §6.

``<wake> <intent> [<system>] [<group>] [<detail>...]``

The grammar is regex over the transcript, nothing more (constraint 6: no LLM
in the command path — it would be slower, cost money per fight, hallucinate
system names, and be undebuggable at 02:00). Nine intents, matched in
severity order so *"tackled, need help in Kisogo"* resolves to
``UNDER_ATTACK`` rather than a sighting.

``system_text`` is the raw token window believed to name a system — it is NOT
resolved here; ``aura.nlu.phonetics.resolve`` owns that. ``detail`` is
captured verbatim and never parsed (GDD §6.3). For ``TIMER``/``FORMUP`` the
duration text is left inside ``detail`` — the incident engine re-parses it
with ``aura.core.incidents.parse_duration``.
"""

from __future__ import annotations

import re

from aura.types import Intent, ParsedCommand

__all__ = ["parse"]

# Leading wake-phrase residue. The wake detector gates on audio, but the
# capture window starts 300ms of pre-roll before the trigger, so the phrase
# (or Whisper's rendering of it) usually survives into the transcript.
_WAKE_RE = re.compile(
    r"^\W*(?:hey\s+|ok(?:ay)?\s+)?(?:aura|ora|or\s+a|aura's|laura|oracle)[\s,]*"
    r"(?:command(?:er)?)?\b[\s,.:;-]*",
    re.IGNORECASE,
)

# Intent patterns in match-priority order (GDD §6.1): the higher-severity
# pattern wins regardless of position in the utterance.
_INTENT_PATTERNS: tuple[tuple[Intent, re.Pattern[str]], ...] = (
    (Intent.UNDER_ATTACK, re.compile(r"\bunder\s+attack\b|\btackled\b|\bpoint\s+on\s+me\b", re.I)),
    (Intent.ASSIST_REQUEST, re.compile(r"\bneed\s+(?:help|backup|back\s*up)\b", re.I)),
    (Intent.HOSTILE_SPOTTED, re.compile(r"\bhostiles?\b|\breds?\b|\bneuts?\b", re.I)),
    (Intent.GATE_CAMP, re.compile(r"\bgate\s*camp(?:ed|ers)?\b", re.I)),
    (Intent.RESOLVE, re.compile(r"\bclear(?:ed)?\b", re.I)),
    (Intent.TIMER, re.compile(r"\btimer\b", re.I)),
    (Intent.FORMUP, re.compile(r"\bform(?:\s|-)?up\b", re.I)),
    (Intent.QUERY, re.compile(r"\bstatus\b", re.I)),
    (Intent.CANCEL, re.compile(r"\bcancel\b", re.I)),
)

# Group targeting (GDD §6.2). Deliberately few — every alias is another token
# the recogniser can confuse with a system name.
_GROUP_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("all_hands", re.compile(r"\ball\s+hands\b", re.I)),
    ("miners", re.compile(r"\b(?:miners?|mining)\s+only\b", re.I)),
    ("defense", re.compile(r"\b(?:home\s+)?defen[cs]e\s+only\b", re.I)),
)

# Prepositions/filler between the intent word and the system name
# ("tackled *in* Kisogo"). Stripped from the system window only; detail stays
# verbatim.
_FILLER = frozenset(
    ("in", "at", "on", "near", "the", "a", "an", "um", "uh", "er", "im", "i'm", "we're", "were")
)

# Where a spoken duration starts, for splitting "timer Kisogo four hours".
# Vocabulary mirrors aura.core.incidents.parse_duration.
_NUMBER_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    "fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|"
    "fifty|sixty|half|a|an"
)
_DURATION_START_RE = re.compile(
    rf"\b(?:(?:\d+(?:\.\d+)?|{_NUMBER_WORDS})\s+)*(?:\d+\s*)?(?:hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)

_SYSTEMLESS_INTENTS = frozenset((Intent.QUERY, Intent.CANCEL))


def _strip_filler(text: str) -> str:
    tokens = [t for t in re.split(r"[\s,.;:!?]+", text) if t]
    kept = [t for t in tokens if t.lower() not in _FILLER]
    return " ".join(kept)


def _remove_intent_phrases(text: str) -> str:
    """Drop *secondary* intent phrases from the system window.

    "tackled, need help in Kisogo": UNDER_ATTACK already won; "need help" in
    the remainder is emphasis, not a system name.
    """
    for _, pattern in _INTENT_PATTERNS:
        text = pattern.sub(" ", text)
    return text


def _split_system_detail(remainder: str) -> tuple[str | None, str | None]:
    """Comma-segment the remainder: first substantive segment names the
    system, the rest is verbatim detail (GDD §6.3)."""
    segments = [seg.strip() for seg in remainder.split(",")]
    system_text: str | None = None
    detail_segments: list[str] = []
    for seg in segments:
        if system_text is None:
            candidate = _strip_filler(_remove_intent_phrases(seg))
            if candidate:
                system_text = candidate
            continue
        if seg:
            detail_segments.append(seg)
    detail = ", ".join(detail_segments) if detail_segments else None
    return system_text, detail


def _split_timer(remainder: str) -> tuple[str | None, str | None]:
    """``timer <system> <duration>`` — the duration text goes verbatim into
    ``detail`` and is re-parsed by the incident engine."""
    flat = remainder.replace(",", " ")
    m = _DURATION_START_RE.search(flat)
    if m is None:
        system_text = _strip_filler(_remove_intent_phrases(flat))
        return system_text or None, None
    system_text = _strip_filler(_remove_intent_phrases(flat[: m.start()]))
    detail = flat[m.start() :].strip()
    return system_text or None, detail or None


def parse(transcript: str) -> ParsedCommand | None:
    """Parse one transcript into a :class:`ParsedCommand`, or ``None`` when no
    intent is recognised (the utterance is then dropped, GDD §6)."""
    if not transcript or not transcript.strip():
        return None
    work = _WAKE_RE.sub("", transcript, count=1)

    intent: Intent | None = None
    match: re.Match[str] | None = None
    for candidate, pattern in _INTENT_PATTERNS:
        found = pattern.search(work)
        if found is not None:
            intent, match = candidate, found
            break
    if intent is None or match is None:
        return None

    remainder = work[match.end() :].strip(" ,.;:!?-")

    # Group targeting is grammatically a suffix, but STT reorders things —
    # accept it anywhere in the utterance, strip it from the system window.
    group_alias: str | None = None
    for alias, pattern in _GROUP_PATTERNS:
        if pattern.search(work):
            group_alias = alias
            remainder = pattern.sub(" ", remainder)
            break

    if intent in _SYSTEMLESS_INTENTS:
        return ParsedCommand(
            intent=intent, system_text=None, group_alias=group_alias, detail=None, raw=transcript
        )

    if intent in (Intent.TIMER, Intent.FORMUP):
        system_text, detail = _split_timer(remainder)
    else:
        system_text, detail = _split_system_detail(remainder)

    return ParsedCommand(
        intent=intent,
        system_text=system_text,
        group_alias=group_alias,
        detail=detail,
        raw=transcript,
    )
