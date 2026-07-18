"""Fixed voice-command grammar — GDD §6.

``<wake> <intent> [<system>] [<group>] [<detail>...]``

The grammar is regex over the transcript, nothing more (constraint 6: no LLM
in the command path — it would be slower, cost money per fight, hallucinate
system names, and be undebuggable at 02:00). Fifteen intents, matched in
severity order so *"tackled, need help in Kisogo"* resolves to
``UNDER_ATTACK`` rather than a sighting. The two personal-ping intents are the
one exception to severity-first: "ping me for hostiles" *names* incident types
without reporting one, so ``PING_ME``/``PING_ME_CLEAR`` are matched before the
type words can claim the utterance (a genuine distress call never contains
"ping me"). For ``PING_ME`` the recognised type words are carried in
``detail`` as a comma-separated list of ``Intent`` values (GDD §6.1).

``system_text`` is the raw token window believed to name a system — it is NOT
resolved here; ``aura.nlu.phonetics.resolve`` owns that. ``detail`` is
captured verbatim and never parsed (GDD §6.3). For ``TIMER``/``FORMUP`` the
duration text is left inside ``detail`` — the incident engine re-parses it
with ``aura.core.incidents.parse_duration``. For ``REGISTER`` the callsign is
the cleaned post-intent remainder, carried in ``detail`` (GDD §6.1) — it is a
name for the registry keyed on the Discord user id Ears already attaches to
every utterance, never anything derived from the audio itself (GDD §19).
"""

from __future__ import annotations

import re

from aura.types import Intent, ParsedCommand, Severity

__all__ = [
    "PING_TYPE_ORDER",
    "bare_code",
    "broadcast_severity",
    "clean_callsign",
    "encode_ping_types",
    "override_query",
    "parse",
    "relay_framed",
    "sanitize_callsign",
    "system_reply",
]

# Leading wake-phrase residue. The wake detector gates on audio, but the
# capture window starts 300ms of pre-roll before the trigger, so the phrase
# (or Whisper's rendering of it) usually survives into the transcript.
_WAKE_RE = re.compile(
    r"^\W*(?:hey\s+|ok(?:ay)?\s+)?"
    r"(?:aura|ora|or\s+a|aura's|laura|oracle"
    # the pretrained interim wake word and the trained replacement, with
    # Whisper's routine renderings of each
    r"|jarvis|jarvus|jervis|cortana|cortana's|cortina|katana|montana)[\s,]*"
    r"(?:command(?:er)?)?\b[\s,.:;-]*",
    re.IGNORECASE,
)

# STT mishearings of the jargon trigger words, normalized to the canonical
# spelling *before* intent matching. STT errors on EVE/military vocabulary are
# phonetic and routine (GDD §8.6) — "hostiles" comes back as "hustiles",
# "hostels", "ostiles" — and a fixed grammar that only accepts the exact word
# throws the command away. This is fixed-grammar normalization, not an LLM
# (constraint 6): a small, auditable table of the words STT actually produces.
# Extend it from the command_log when a real mishearing slips through.
_JARGON_NORMALIZE: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(?:hustiles?|hostels?|hostals?|hostals?|ostiles?|hostiels?|hostials?|"
            r"hastiles?|haustiles?|hostilds?|hostvilles?)\b",
            re.I,
        ),
        "hostiles",
    ),
    (re.compile(r"\b(?:newts?|nutes?|noots?|neutes?|knutes?)\b", re.I), "neuts"),
    (re.compile(r"\b(?:tickled|tackle)\b", re.I), "tackled"),
    (re.compile(r"\b(?:gatecamp|gate\s*champ|gate\s*camps?)\b", re.I), "gate camp"),
    # Whisper's spellings of "register" — a real fleet said "Register Space
    # Junkie", got "Regester Space Junkie", and the command fell through to
    # the freeform relay instead of registering the callsign.
    (
        re.compile(r"\b(?:regester|registar|redgister|rejister|regiser|regista)\b", re.I),
        "register",
    ),
)

# Radio sign-off at the tail of an utterance ("...three battleships, over").
# "over"/"out" are the pilot's cue that they are done talking; they are not
# part of the report, so they are stripped before parsing (the trailing
# silence after them is what actually ends the capture).
_SIGNOFF_RE = re.compile(
    r"[\s,.;:!?-]*\b(?:"
    r"over\s+and\s+out|over\s+out|overandout|over|out|copy|roger|clear\s+skies"
    # radio-procedure closers for the "report … end report" envelope
    r"|end\s+(?:of\s+)?report|report\s+ends?|end\s+transmission"
    # common STT renderings of "over" at the tail of a transmission
    r"|rover|ova|oveur|ovah"
    r")\b[\s,.;:!?-]*$",
    re.I,
)

# Radio-procedure opener: "report, I've been tackled in UMI, end report".
# A leading "report(ing)" frames the message; it is not part of it.
_REPORT_OPENER_RE = re.compile(r"^\W*report(?:ing)?\b[\s,.:;-]*", re.I)

# ── spoken threat colours (GDD §6.4) ─────────────────────────────────────────

# "code red / orange / yellow" — spoken severity, mirroring the card labels
# (CODE RED = high, CODE ORANGE = medium, CODE YELLOW = none/info). Matched
# anywhere in the utterance and stripped before intent matching — "code red"
# must never be claimed by the HOSTILE_SPOTTED "\breds?\b" pattern.
_CODE_RE = re.compile(r"\bcode\s+(red|orange|yellow)\b[\s,.:;-]*", re.I)
_CODE_SEVERITY: dict[str, Severity] = {
    "red": Severity.HIGH,
    "orange": Severity.MEDIUM,
    "yellow": Severity.NONE,
}


def _extract_code(text: str) -> tuple[Severity | None, str]:
    """Pull the first spoken colour code out of ``text``; strip all of them."""
    m = _CODE_RE.search(text)
    if m is None:
        return None, text
    return _CODE_SEVERITY[m.group(1).lower()], _CODE_RE.sub(" ", text)


def _normalize_jargon(text: str) -> str:
    for pattern, canonical in _JARGON_NORMALIZE:
        text = pattern.sub(canonical, text)
    return text


# Intent patterns in match-priority order (GDD §6.1): the higher-severity
# pattern wins regardless of position in the utterance. The personal-ping
# intents sit above the type words because their utterances *contain* type
# words ("ping me for gate camps"); PING_ME_CLEAR before PING_ME so "stop
# pinging me" can never be claimed as a new subscription.
_INTENT_PATTERNS: tuple[tuple[Intent, re.Pattern[str]], ...] = (
    (Intent.PING_ME_CLEAR, re.compile(r"\bstop\s+ping(?:ing|s)?(?:\s+me)?\b", re.I)),
    (Intent.PING_ME, re.compile(r"\bping\s+me\b", re.I)),
    (Intent.UNDER_ATTACK, re.compile(r"\bunder\s+attack\b|\btackled\b|\bpoint\s+on\s+me\b", re.I)),
    (Intent.ASSIST_REQUEST, re.compile(r"\bneed\s+(?:help|backup|back\s*up)\b", re.I)),
    (Intent.HOSTILE_SPOTTED, re.compile(r"\bhostiles?\b|\breds?\b|\bneuts?\b", re.I)),
    (Intent.GATE_CAMP, re.compile(r"\bgate\s*camp(?:ed|ers)?\b", re.I)),
    (Intent.RESOLVE, re.compile(r"\bclear(?:ed)?\b", re.I)),
    (Intent.TIMER, re.compile(r"\btimer\b", re.I)),
    (Intent.FORMUP, re.compile(r"\bform(?:\s|-)?up\b", re.I)),
    (Intent.QUERY, re.compile(r"\bstatus\b", re.I)),
    # HELP sits below ASSIST_REQUEST so "need help" is always a distress call;
    # a bare "help" (nothing else claimed it) is a request for the manual.
    (Intent.HELP, re.compile(r"\bhelp\b", re.I)),
    (Intent.CANCEL, re.compile(r"\bcancel\b", re.I)),
    # Callsign registry (GDD §6.1). UNREGISTER before REGISTER so the longer
    # word can never be claimed by the shorter pattern.
    (
        Intent.UNREGISTER,
        re.compile(r"\bunregister(?:ed|ing)?(?:\s+me)?\b|\bforget\s+me\b", re.I),
    ),
    (Intent.WHOAMI, re.compile(r"\bwho\s+am\s+i\b|\bwhoami\b", re.I)),
    # "register", plus STT drift ("registered"/"registering") and the natural
    # phrasings "call me …" / "my callsign is …".
    (
        Intent.REGISTER,
        re.compile(r"\bregister(?:ed|ing)?\b|\bcall\s+me\b|\bcallsign\b", re.I),
    ),
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

_SYSTEMLESS_INTENTS = frozenset(
    (
        Intent.QUERY,
        Intent.HELP,
        Intent.CANCEL,
        Intent.UNREGISTER,
        Intent.WHOAMI,
        Intent.PING_ME_CLEAR,
    )
)

# ── personal pings (GDD §6.1 PING_ME) ────────────────────────────────────────

#: Canonical order for the encoded type list — matches the §12.1 spoken order.
PING_TYPE_ORDER: tuple[Intent, ...] = (
    Intent.HOSTILE_SPOTTED,
    Intent.UNDER_ATTACK,
    Intent.ASSIST_REQUEST,
    Intent.GATE_CAMP,
)

# The §6.1 synonym vocabulary, re-scoped to the PING_ME remainder ("ping me
# for gate camps in Otanuomi"). Scanned — not matched in priority order —
# because "ping me for hostiles and gate camps" names several types at once.
_PING_TYPE_PATTERNS: tuple[tuple[Intent, re.Pattern[str]], ...] = (
    (Intent.HOSTILE_SPOTTED, re.compile(r"\bhostiles?\b|\breds?\b|\bneuts?\b", re.I)),
    (Intent.UNDER_ATTACK, re.compile(r"\b(?:under\s+)?attacks?\b|\btackled\b", re.I)),
    (
        Intent.ASSIST_REQUEST,
        re.compile(
            r"\bassist\s+requests?\b|\bassists?\b|\bneed\s+(?:help|backup|back\s*up)\b", re.I
        ),
    ),
    (Intent.GATE_CAMP, re.compile(r"\bgate\s*camps?\b", re.I)),
)

# "anything" / "everything" / "all" → all four report types.
_PING_ALL_RE = re.compile(r"\banything\b|\beverything\b|\ball\b", re.I)

# Connective filler specific to the PING_ME phrasing ("ping me *for* gate
# camps *and* hostiles"). "everywhere"/"anywhere" name the no-system default
# out loud — they must never be mistaken for a system window.
_PING_FILLER = frozenset(("for", "and", "please", "when", "me", "everywhere", "anywhere"))


def encode_ping_types(types: frozenset[Intent]) -> str:
    """Encode a PING_ME type set into the ``detail`` slot: comma-separated
    ``Intent`` values in canonical order — readable in ``command_log``, shared
    by voice and slash (constraint 10)."""
    return ",".join(str(t) for t in PING_TYPE_ORDER if t in types)


def _parse_ping_me(remainder: str) -> tuple[str | None, str]:
    """Split a PING_ME remainder into ``(system_text, encoded_types)``.

    Type words are scanned with the §6.1 synonym vocabulary and stripped;
    whatever survives filler removal is the system window. No type word (or an
    explicit "anything"/"everything"/"all") means all four report types —
    "ping me in Otanuomi" is a subscription to everything there.
    """
    types: set[Intent] = set()
    work = remainder
    for intent, pattern in _PING_TYPE_PATTERNS:
        if pattern.search(work):
            types.add(intent)
            work = pattern.sub(" ", work)
    if _PING_ALL_RE.search(work) or not types:
        types = set(PING_TYPE_ORDER)
        work = _PING_ALL_RE.sub(" ", work)
    tokens = [t for t in re.split(r"[\s,.;:!?]+", work) if t]
    kept = [t for t in tokens if t.lower() not in (_FILLER | _PING_FILLER)]
    system_text = " ".join(kept) or None
    return system_text, encode_ping_types(frozenset(types))


# ── callsign cleaning (GDD §6.1 REGISTER) ────────────────────────────────────

#: Hard cap on a stored callsign. Cards and utterances stay short (§12.1).
_CALLSIGN_MAX_LEN = 32

# Markdown/mention machinery is stripped so a callsign can never smuggle a
# ping or formatting into a card: @mentions, #channels, backticks, <...> tags.
_CALLSIGN_STRIP_RE = re.compile(r"[@#`<>*_~|\\]")

# Connective filler between the intent word and the name itself:
# "register *me as* Space Junkie", "call me Space Junkie".
_CALLSIGN_FILLER = frozenset(("as", "me", "my", "is", "name", "callsign"))


def sanitize_callsign(text: str) -> str | None:
    """Shared callsign sanitiser for both input paths (constraint 10).

    Strips markdown/mention characters, collapses whitespace, and caps the
    result at 32 characters. Case is preserved — the slash twin's typed value
    is exact. Returns ``None`` when nothing usable survives.
    """
    cleaned = _CALLSIGN_STRIP_RE.sub("", text)
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned[:_CALLSIGN_MAX_LEN].strip()
    return cleaned or None


def clean_callsign(text: str) -> str | None:
    """Voice-path callsign cleaning: filler stripped, then sanitised and
    title-cased (STT emits lowercase; a callsign is a proper name)."""
    tokens = [t for t in re.split(r"[\s,.;:!?]+", text) if t]
    while tokens and tokens[0].lower() in (_CALLSIGN_FILLER | _FILLER):
        tokens.pop(0)
    cleaned = sanitize_callsign(" ".join(tokens))
    return cleaned.title() if cleaned else None


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


def system_reply(transcript: str) -> str | None:
    """Clean a bare system-name reply spoken into a reopened window (GDD §8.3).

    After a LOW-tier "Say again the system." the pilot answers with just the
    name ("Kisogo", "in Kisogo"). This applies the module's own normalisation
    — wake-residue strip plus filler strip — so the reply is handled exactly
    like the normal system window. Returns ``None`` when nothing survives.
    """
    if not transcript or not transcript.strip():
        return None
    work = _WAKE_RE.sub("", transcript, count=1)
    work = _SIGNOFF_RE.sub("", work)
    cleaned = _strip_filler(work.strip(" ,.;:!?-"))
    return cleaned or None


# Freeform relay (GDD §8.6): leading wake residue, tolerant of any wake phrase
# ("hey jarvis", "aura command", "hey overseer"). Matches nothing when absent,
# so a message that happens not to start with the wake word is left intact.
_BROADCAST_WAKE_RE = re.compile(
    r"^\W*(?:(?:hey|ok(?:ay)?|hi)\s+)?(?:jarvis|aura|overseer|alexa)(?:\s+command)?[\s,.:;!?-]*",
    re.I,
)
_ALL_HANDS_RE = re.compile(r"\ball\s+hands\b|\bat\s+here\b|\bping\s+everyone\b", re.I)


# Whisper hallucinating on near-silence produces a word stuttered three or
# more times ("Rens, Rens, Rens"). Real speech repeats for emphasis at most
# twice; three-plus identical words in a row collapse to one.
_REPEAT_RE = re.compile(r"\b(\w[\w'-]*)(?:[\s,.;:!?-]+\1\b){2,}", re.I)


def broadcast_text(transcript: str) -> str:
    """Clean a freeform relay message: strip a leading wake phrase, a spoken
    colour code, and a trailing radio sign-off, leaving the intel itself
    (GDD §8.6). Stuttered hallucinations collapse to a single word."""
    work = _BROADCAST_WAKE_RE.sub("", transcript, count=1)
    work = _SIGNOFF_RE.sub("", work)
    work = _REPORT_OPENER_RE.sub("", work, count=1)
    _, work = _extract_code(work)
    work = _REPEAT_RE.sub(r"\1", work)
    return work.strip(" ,.;:!?-")


def broadcast_severity(transcript: str) -> Severity | None:
    """The spoken colour code of a freeform relay, if any (GDD §6.4)."""
    severity, _ = _extract_code(transcript)
    return severity


# "Command override" — the explicit doorway to the out-of-band assistant
# (GDD §6.6). Leading position only (after wake residue), so a report that
# happens to contain the word "override" mid-sentence can never be diverted.
# STT renders "override" phonetically — "over ride", "overide", "over-ride",
# "overdrive", "overwrite" — and a doorway that only opens on the dictionary
# spelling stays shut half the time. All variants are accepted; leading
# position keeps them from ever claiming a real report.
_OVERRIDE_RE = re.compile(
    r"^\W*(?:command(?:er|s)?[\s,]+|com+a?nd[\s,]+)?"
    r"(?:over\s*[- ]?\s*(?:ride|write|drive)|over(?:r?ide|ride|write|drive))\b[\s,.:;-]*",
    re.I,
)


def override_query(transcript: str) -> str | None:
    """Extract the question from a "command override …" utterance.

    Returns the query text, or ``None`` when the utterance is not an
    override (then it flows to the normal grammar untouched — constraint 6).
    A bare "command override" with no question also returns ``None``.
    """
    if not transcript or not transcript.strip():
        return None
    work = _BROADCAST_WAKE_RE.sub("", transcript, count=1)
    work = _WAKE_RE.sub("", work, count=1)
    m = _OVERRIDE_RE.match(work)
    if m is None:
        return None
    rest = _SIGNOFF_RE.sub("", work[m.end() :]).strip(" ,.;:!?-")
    return rest or None


def bare_code(transcript: str) -> Severity | None:
    """Detect a *standalone* colour code — the dialogue opener (GDD §6.4).

    "Code orange." with nothing else after wake/sign-off stripping means the
    pilot is announcing severity first and will give the report in the next
    breath: AURA acknowledges and reopens a wake-free capture window. Returns
    the severity, or ``None`` when the utterance carries other content (then
    the code rides along inline instead).
    """
    if not transcript or not transcript.strip():
        return None
    work = _BROADCAST_WAKE_RE.sub("", transcript, count=1)
    work = _WAKE_RE.sub("", work, count=1)
    work = _SIGNOFF_RE.sub("", work)
    severity, work = _extract_code(work)
    if severity is None:
        return None
    return severity if len(work.strip(" ,.;:!?-")) < 3 else None


def wants_all_hands(transcript: str) -> bool:
    """True when a freeform message asks to ping everyone (@here)."""
    return bool(_ALL_HANDS_RE.search(transcript))


def relay_framed(transcript: str) -> bool:
    """True when a freeform utterance is *explicitly framed* as intel —
    a "report …" opener, a spoken colour code, or an all-hands phrase.

    Under ``stt.relay_mode: framed`` (the default) only framed speech may
    post a relay card: an unmatched transcript with no framing is far more
    likely an STT mishearing or crosstalk than intel, and every junk card
    costs the channel trust (GDD §8.6).
    """
    if not transcript or not transcript.strip():
        return False
    work = _BROADCAST_WAKE_RE.sub("", transcript, count=1)
    work = _WAKE_RE.sub("", work, count=1)
    if _REPORT_OPENER_RE.match(work):
        return True
    severity, _ = _extract_code(work)
    if severity is not None:
        return True
    return wants_all_hands(work)


def parse(transcript: str) -> ParsedCommand | None:
    """Parse one transcript into a :class:`ParsedCommand`, or ``None`` when no
    intent is recognised (the utterance is then dropped, GDD §6)."""
    if not transcript or not transcript.strip():
        return None
    work = _WAKE_RE.sub("", transcript, count=1)
    work = _SIGNOFF_RE.sub("", work)
    work = _REPORT_OPENER_RE.sub("", work, count=1)
    work = _normalize_jargon(work)
    # Spoken colour first: "code red" carries severity, and its "red" must be
    # gone before the HOSTILE_SPOTTED pattern can misread it as a sighting.
    severity, work = _extract_code(work)

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
            intent=intent,
            system_text=None,
            group_alias=group_alias,
            detail=None,
            raw=transcript,
            severity=severity,
        )

    if intent is Intent.PING_ME:
        # The remainder names incident types (carried encoded in ``detail``)
        # and optionally a system window — resolved later by phonetics, same
        # pipeline as reports (GDD §8.2).
        system_text, encoded = _parse_ping_me(remainder)
        return ParsedCommand(
            intent=intent,
            system_text=system_text,
            group_alias=group_alias,
            detail=encoded,
            raw=transcript,
            severity=severity,
        )

    if intent is Intent.REGISTER:
        # The whole remainder is the callsign — cleaned, never resolved
        # against the gazetteer. Carried in ``detail`` (GDD §6.1).
        return ParsedCommand(
            intent=intent,
            system_text=None,
            group_alias=group_alias,
            detail=clean_callsign(remainder),
            raw=transcript,
            severity=severity,
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
        severity=severity,
    )
