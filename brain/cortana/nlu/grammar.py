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

from cortana.types import Intent, ParsedCommand, Severity

__all__ = [
    "PING_TYPE_ORDER",
    "STT_VOCAB_BIAS",
    "bare_code",
    "bare_override",
    "broadcast_severity",
    "clean_callsign",
    "confirm_reply",
    "dismissal",
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
    # Fun-command trigger words (GDD §13.2), from live transcripts: "insult
    # Space Monkey" came back "Insalt Space Monkey"; "roast Space Monkey"
    # came back "Rara, Woust, Space Monkey".
    (re.compile(r"\b(?:insalts?|in[\s-]salts?|ensalts?|insaults?)\b", re.I), "insult"),
    (re.compile(r"\b(?:woust|roust|wrost|rosts?)\b", re.I), "roast"),
)

#: Spoken command vocabulary for the Whisper bias prompt (GDD §5.3). The
#: gazetteer-only prompt biased the decoder so hard toward system names that
#: casual command words came back system-shaped (live incident: "roast" →
#: "Woust") — listing the grammar's own trigger words alongside the system
#: names keeps both vocabularies decodable. Appended AFTER the system names:
#: Whisper truncates an over-long initial_prompt from the front, so the tail
#: is the part guaranteed to survive.
STT_VOCAB_BIAS = (
    "Commands: hostiles, reds, neuts, enemies, war target, tackled, bubbled, "
    "scrambled, pointed, webbed, jammed, under attack, need backup, help me, "
    "send help, gate camp, code red, code orange, clear, status, timer, "
    "form up, cancel, register, ping me, who am I, update chase, report, "
    "command override, tell me a fact, trivia, insult, roast, over."
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
    # STT writes "ping me" as pin/pink/pinging me routinely (live incident:
    # a mangled "ping me for gate camp" fell through to GATE_CAMP and
    # posted a junk camp card) — match the phonetic neighbourhood.
    (
        Intent.PING_ME_CLEAR,
        re.compile(r"\bstop\s+(?:ping|pin|pink)(?:n?ing|s)?(?:\s+me)?\b", re.I),
    ),
    (Intent.PING_ME, re.compile(r"\b(?:ping|pin|pink|pinging)[\s-]*me\b", re.I)),
    # The distress vocabulary (GDD §6.1). Pilots describe the SAME situation a
    # dozen ways and cannot enumerate them up front (live request), so the
    # pattern covers the EWAR/tackle verbs that mean "I am in a fight, come
    # now" — whether the pilot is the one being held ("I'm scrambled",
    # "bubbled", "pointed") or holding tackle on a target ("I've got them
    # webbed", "scrambling them"). Either way the fleet response is identical:
    # warp to them. All of these outrank HOSTILE_SPOTTED so "tackled by reds"
    # is a distress call, never demoted to a sighting. New verbs come from real
    # fleet audio — extend from the transcript channel, not from guessing.
    (
        Intent.UNDER_ATTACK,
        re.compile(
            r"\bunder\s+(?:attack|fire)\b"
            r"|\b(?:being|getting|taking)\s+(?:attacked|shot|hit)\b"
            r"|\btaking\s+fire\b"
            r"|\battacked\b"
            r"|\btackl(?:ed|ing)\b"
            r"|\bbubbl(?:ed|ing)\b"
            r"|\bscram(?:med|bled|bling)?\b"
            r"|\bpointed\b"
            r"|\bpinned\b"
            r"|\bweb(?:bed|bing)\b"
            r"|\bjam(?:med|ming)\b"
            r"|\bneut(?:ed|ing)\b"
            r"|\bengaged\b"
            r"|\b(?:point|tackle|scram|web|tackled)\s+on\s+(?:me|us|him|them)\b",
            re.I,
        ),
    ),
    # "need help" / "help me" / "send help", plus the freeform phrasing
    # "request(ing) [heavy] assistance" — courtesy adjectives are optional, the
    # type word is what matters. Sits BELOW UNDER_ATTACK so "tackled … and
    # request heavy assistance" is always the distress call, never demoted to
    # the assist phrasing; ABOVE the bare HELP intent so "help me" is a distress
    # call and only a lone "help" reaches the manual.
    (
        Intent.ASSIST_REQUEST,
        re.compile(
            r"\bneed\s+(?:help|backup|back\s*up|assistance|reinforcements|support|dps|guns)\b"
            r"|\bhelp\s+me\b"
            r"|\bsend\s+(?:help|backup|back\s*up|reinforcements|support|the\s+fleet|the\s+cavalry|"
            r"someone|bodies|dps|guns)\b"
            r"|\brequest(?:ing)?\s+(?:heavy\s+|immediate\s+|urgent\s+)?"
            r"(?:assistance|help|backup|back\s*up|reinforcements|support)\b"
            r"|\b(?:get|come)\s+(?:over\s+here|to\s+me|to\s+us|here|quick|fast|now)\b",
            re.I,
        ),
    ),
    # Sighting vocabulary: the neutral/hostile nouns pilots call out. "enemy"/
    # "enemies" and "war target(s)" (EVE's term for a shootable pilot) join the
    # standard set; "gankers"/"bad guys" are the colloquial forms. Below the
    # distress intents so any of these prefixed by a tackle verb escalates.
    (
        Intent.HOSTILE_SPOTTED,
        re.compile(
            r"\bhostiles?\b|\breds?\b|\bneuts?\b|\benem(?:y|ies)\b"
            r"|\bwar\s*targets?\b|\bgankers?\b|\bbad\s+guys\b",
            re.I,
        ),
    ),
    (Intent.GATE_CAMP, re.compile(r"\bgate\s*camp(?:ed|ers)?\b", re.I)),
    (Intent.RESOLVE, re.compile(r"\bclear(?:ed)?\b", re.I)),
    (Intent.TIMER, re.compile(r"\btimer\b", re.I)),
    (Intent.FORMUP, re.compile(r"\bform(?:\s|-)?up\b", re.I)),
    (Intent.QUERY, re.compile(r"\bstatus\b", re.I)),
    # HELP sits below ASSIST_REQUEST so "need help" is always a distress call;
    # a bare "help" (nothing else claimed it) is a request for the manual.
    (Intent.HELP, re.compile(r"\bhelp\b", re.I)),
    # "cancelled"/"canceled" too: "chase cancelled" must land here, not on
    # the chase pattern below.
    (Intent.CANCEL, re.compile(r"\bcancel(?:led|ed)?\b", re.I)),
    # Fun commands (GDD §13.2). Below every report/manage intent so a real
    # distress call containing one of these words ("that's a fact") can
    # never be demoted to entertainment; above CHASE so a bare leading
    # "roast him" isn't claimed as a chase retarget.
    (Intent.FACT, re.compile(r"\bfacts?\b|\btrivia\b", re.I)),
    (Intent.INSULT, re.compile(r"\binsult(?:s|ed|ing)?\b|\broast(?:s|ing)?\b", re.I)),
    # Chase mode (GDD §13.1): "update chase Kisogo" / "chase Kisogo"
    # retargets the pilot's live incident card as the target moves. Sits
    # BELOW the distress intents, RESOLVE and CANCEL — "tackled … giving
    # chase" is a distress call, "chase done, clear Kisogo" is a clear, and
    # "chase cancelled" is a cancel. Both the bare word and "chase mode"
    # count only in LEADING position (the utterance is wake-gated, so a
    # command starts with it) — mid-sentence chatter like "we're in chase
    # mode after the vexor" must never silently retarget a live card;
    # explicit "update chase" works anywhere.
    (
        Intent.CHASE_UPDATE,
        re.compile(r"\bupdate\s+chase\b(?:\s+mode)?|^\W*chase\b(?:\s+mode)?", re.I),
    ),
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
    (
        "in",
        "at",
        "on",
        "near",
        "the",
        "a",
        "an",
        "um",
        "uh",
        "er",
        "im",
        "i'm",
        "we're",
        "were",
        # "in system UMI" — "system" is a noise word before the name, never
        # part of it; pilots say it constantly and it wrecked resolution.
        "system",
        "systems",
    )
)

# Freeform courtesy padding (GDD §6.1): stressed pilots wrap commands in
# narrative — "please report that I am tackled by enemies in system M tack O
# and request heavy assistance please". Intent keywords are matched on the
# raw text FIRST, so no word here can break recognition; this finite set is
# stripped from the *system window only* — never from ``detail``, never from
# callsigns. "assistance"/"backup"/"reinforcements" are ASSIST_REQUEST type
# words when prefixed by need/request (removed as whole intent phrases before
# this set applies); here they only mop up unprefixed stragglers that would
# otherwise pollute the window.
_COURTESY = frozenset(
    (
        "please",
        "kindly",
        "that",
        "this",
        "by",
        "with",
        "for",
        "and",
        "some",
        "enemies",
        "enemy",
        "guys",
        "request",
        "requesting",
        "requested",
        "heavy",
        "immediate",
        "immediately",
        "urgent",
        "urgently",
        "assistance",
        "backup",
        "reinforcements",
        "send",
        "sending",
        "right",
        "now",
    )
)

#: Everything stripped from a system window: prepositions + courtesy padding.
_WINDOW_FILLER = _FILLER | _COURTESY

# Multi-word courtesy phrases, stripped from the system window before
# tokenisation. These can NOT be token filler: "i" is a letter pilots spell
# ("u m i tack k k" → UMI-KK) and "you" appears inside STT renderings of real
# names ("oh tan you oh me" → Otanuomi) — only the exact phrases are safe.
_PADDING_PHRASES_RE = re.compile(
    r"\b(?:i\s+am|we\s+are|i'?ve\s+been|we'?ve\s+been|they\s+are"
    r"|there\s+(?:is|are)|be\s+advised|thank\s+you|thanks)\b",
    re.I,
)

# "in system X" / "in X" / "at X" — when the pilot anchors the system name
# with a preposition, everything before the LAST anchor is narrative padding.
_ANCHOR_TOKENS = frozenset(("in", "at", "near"))

#: Spoken hyphens inside a spelled system name ("m tack o" → M-O…). Mirrors
#: the vocabulary in ``cortana.nlu.phonetics``; here it only guards the
#: article "a" from being stripped out of a spelling ("one d q one tack a").
_TACK_WORDS = frozenset(("tack", "tac", "dash", "hyphen"))

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
    kept = _keep_window_tokens(tokens, _WINDOW_FILLER | _PING_FILLER)
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
    # "system"/"systems" are report-window noise words, NOT callsign filler —
    # "register System Junkie" must keep its name intact.
    callsign_filler = _CALLSIGN_FILLER | (_FILLER - {"system", "systems"})
    while tokens and tokens[0].lower() in callsign_filler:
        tokens.pop(0)
    cleaned = sanitize_callsign(" ".join(tokens))
    return cleaned.title() if cleaned else None


def _spelled_neighbor(tokens: list[str], idx: int) -> bool:
    """True when the token at ``idx`` sits next to a spelled-name part —
    a single letter/digit or a spoken hyphen ("tack")."""
    for j in (idx - 1, idx + 1):
        if 0 <= j < len(tokens):
            t = tokens[j].lower()
            if t in _TACK_WORDS or (len(t) == 1 and t.isalnum()):
                return True
    return False


def _keep_window_tokens(tokens: list[str], filler: frozenset[str]) -> list[str]:
    """Drop filler/courtesy tokens from a system window. The article "a" is
    also a letter pilots spell ("one d q one tack a" → 1DQ1-A): it survives
    when adjacent to other spelled parts."""
    kept: list[str] = []
    for idx, tok in enumerate(tokens):
        low = tok.lower()
        if low in filler and not (low == "a" and _spelled_neighbor(tokens, idx)):
            continue
        kept.append(tok)
    return kept


def _strip_filler(text: str) -> str:
    text = _PADDING_PHRASES_RE.sub(" ", text)
    tokens = [t for t in re.split(r"[\s,.;:!?]+", text) if t]
    return " ".join(_keep_window_tokens(tokens, _WINDOW_FILLER))


def _remove_intent_phrases(text: str) -> str:
    """Drop *secondary* intent phrases from the system window.

    "tackled, need help in Kisogo": UNDER_ATTACK already won; "need help" in
    the remainder is emphasis, not a system name.
    """
    for _, pattern in _INTENT_PATTERNS:
        text = pattern.sub(" ", text)
    return text


#: Continuation verbs that begin the REST of the sentence, not the name:
#: "under attack M-TAC-O requiring heavy assistance" — the window must cut
#: before "requiring" (live junk card: System "M-TAC-O requiring").
_TRAILING_CUT = frozenset(
    {
        "requiring",
        "require",
        "requires",
        "requesting",
        "request",
        "requests",
        "needing",
        "needs",
        "need",
        "send",
        "sending",
        "bring",
        "bringing",
        "over",
        "out",
    }
)

#: A window that is ONLY a pronoun is a fragment of the sentence ("point on
#: me", "they're on us"), never a location (live junk card: System "me").
_PRONOUN_ONLY = frozenset(
    {"me", "us", "you", "them", "him", "her", "it", "here", "there", "everyone"}
)


def _finalize_window(tokens: list[str]) -> str | None:
    """Trim a kept token window to the part that can actually be a name."""
    cut = len(tokens)
    for idx, tok in enumerate(tokens):
        if idx > 0 and tok.lower() in _TRAILING_CUT:
            cut = idx
            break
    kept = tokens[:cut]
    while kept and kept[0].lower() in _TRAILING_CUT:
        kept = kept[1:]
    text = " ".join(kept)
    if not text or all(tok.lower() in _PRONOUN_ONLY for tok in kept):
        return None
    return text


def _extract_system(segment: str) -> str | None:
    """The system window of one segment (GDD §6.1 padding tolerance).

    Secondary intent phrases and courtesy padding are stripped aggressively.
    When the pilot anchors the name with a preposition — "in system X",
    "in X", "at X" — everything before the LAST anchor is narrative
    ("tackled *by enemies in* system M tack O") and is discarded, provided
    something substantive survives after the anchor.
    """
    work = _PADDING_PHRASES_RE.sub(" ", _remove_intent_phrases(segment))
    tokens = [t for t in re.split(r"[\s,.;:!?]+", work) if t]
    anchor: int | None = None
    for idx, tok in enumerate(tokens):
        if tok.lower() in _ANCHOR_TOKENS:
            anchor = idx
    if anchor is not None:
        tail = _finalize_window(_keep_window_tokens(tokens[anchor + 1 :], _WINDOW_FILLER))
        if tail:
            return tail
    return _finalize_window(_keep_window_tokens(tokens, _WINDOW_FILLER))


def _split_system_detail(remainder: str) -> tuple[str | None, str | None]:
    """Comma-segment the remainder: first substantive segment names the
    system, the rest is verbatim detail (GDD §6.3)."""
    segments = [seg.strip() for seg in remainder.split(",")]
    system_text: str | None = None
    detail_segments: list[str] = []
    for seg in segments:
        if system_text is None:
            candidate = _extract_system(seg)
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
        return _extract_system(flat), None
    system_text = _extract_system(flat[: m.start()])
    detail = flat[m.start() :].strip()
    return system_text, detail or None


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


# ── confirm-window replies (GDD §8.3 AWAIT_CONFIRM) ──────────────────────────

# Standalone yes/no vocabulary for the §8.3 confirm window. A small fixed
# table (constraint 6) of the words pilots actually answer with, plus the
# radio-procedure acknowledgements ("roger", "copy") that the sign-off strip
# would otherwise swallow — which is why this runs on the raw wake-stripped
# text, before any sign-off handling.
_CONFIRM_YES = frozenset(
    (
        "yes",
        "yeah",
        "yep",
        "yup",
        "ya",
        "yah",
        "aye",
        "affirmative",
        "affirm",
        "confirm",
        "confirmed",
        "correct",
        "roger",
        "copy",
        "right",
        "positive",
        # Flexible affirmations pilots actually say to a readback (live
        # request: "yes or confirm or thats right or ok or post it"). "post"/
        # "send"/"go" mean "commit it"; "okay"/"good"/"sure"/"yup" are plain
        # agreement. A destructive confirm still fails closed on any _CONFIRM_NO
        # word, so a wider YES set only helps the common non-destructive case.
        "ok",
        "okay",
        "kay",
        "post",
        "send",
        "go",
        "good",
        "sure",
        "great",
        "perfect",
        "yessir",
        "yers",
    )
)
_CONFIRM_NO = frozenset(
    (
        "no",
        "nope",
        "nah",
        "negative",
        "cancel",
        "canceled",
        "cancelled",
        "wrong",
        "incorrect",
        "belay",
        "disregard",
        "abort",
        "stop",
    )
)
# Words allowed to ride along without breaking standalone-ness ("yes please",
# "yeah do it", "confirm it, over").
_CONFIRM_FILLER = frozenset(
    (
        "do",
        "it",
        "that",
        "thats",
        "is",
        "the",
        "please",
        "over",
        "out",
        "um",
        "uh",
        "er",
        "and",
        "for",
        "on",
        "its",
    )
)
_CONFIRM_YES_ALLOWED = _CONFIRM_YES | _CONFIRM_FILLER
_DO_IT_RE = re.compile(r"\bdo\s+it\b", re.I)


def dismissal(transcript: str) -> bool:
    """True for a *standalone* dismissal — the pilot closing the dialog.

    "End transmission", "disregard", "never mind", "belay that", "stand
    down", a bare "stop" — spoken during a capture, a retry window, or as a
    fresh wake command, any of these ends the dialog immediately with
    "Standing down." (live complaint: in heavy chatter the say-again loop
    had no spoken exit). Standalone-ness is required — "stop pinging me" is
    a command, not a dismissal. "cancel" is deliberately absent: it is the
    CANCEL intent (retract the last incident) and must keep that meaning.
    """
    if not transcript or not transcript.strip():
        return False
    work = _BROADCAST_WAKE_RE.sub("", transcript, count=1)
    work = _WAKE_RE.sub("", work, count=1)
    tokens = [t for t in re.split(r"[\s,.;:!?'-]+", work.lower()) if t]
    kept = [t for t in tokens if t not in _DISMISS_FILLER]
    return " ".join(kept) in _DISMISS_PHRASES


#: Words allowed to ride along with a dismissal ("okay never mind, over").
_DISMISS_FILLER = frozenset(
    ("ok", "okay", "please", "over", "out", "um", "uh", "er", "that", "it", "now", "thanks")
)

#: The dismissal vocabulary, matched after filler removal. Phrases only —
#: no single common word except the imperatives pilots actually use.
_DISMISS_PHRASES = frozenset(
    (
        "end transmission",
        "end of transmission",
        "never mind",
        "nevermind",
        "disregard",
        "belay",
        "stand down",
        "standing down",
        "stop",
        "stop listening",
        "shut up",
        "forget",
        "as you were",
    )
)


def confirm_reply(transcript: str) -> str | None:
    """Classify a standalone confirm-window reply: ``"yes"``, ``"no"``, or
    ``None`` when the utterance carries other content (GDD §8.3).

    Any negative word anywhere vetoes ("no, wrong", "yes— no, cancel"): a
    destructive confirm must fail closed. An affirmative counts only when the
    whole utterance is affirmation ("yes", "yeah do it", "confirm, over") —
    "yes, hostiles Kisogo" is a command, not a reply, and returns ``None``
    so the grammar can claim it.
    """
    if not transcript or not transcript.strip():
        return None
    work = _BROADCAST_WAKE_RE.sub("", transcript, count=1)
    work = _WAKE_RE.sub("", work, count=1)
    work = work.replace("'", "")
    tokens = [t for t in re.split(r"[\s,.;:!?-]+", work.lower()) if t]
    if not tokens:
        return None
    if any(t in _CONFIRM_NO for t in tokens):
        return "no"
    has_yes = any(t in _CONFIRM_YES for t in tokens) or _DO_IT_RE.search(work) is not None
    if has_yes and all(t in _CONFIRM_YES_ALLOWED for t in tokens):
        return "yes"
    return None


# Freeform relay (GDD §8.6): leading wake residue, tolerant of any wake phrase
# ("hey jarvis", "aura command", "hey overseer"). Matches nothing when absent,
# so a message that happens not to start with the wake word is left intact.
_BROADCAST_WAKE_RE = re.compile(
    r"^\W*(?:(?:hey|ok(?:ay)?|hi)\s+)?"
    r"(?:jarvis|aura|cortana|cortina|katana|overseer|alexa)(?:\s+command)?[\s,.:;!?-]*",
    re.I,
)
_ALL_HANDS_RE = re.compile(r"\ball\s+hands\b|\bat\s+here\b|\bping\s+everyone\b", re.I)

#: Words that END a chase rather than name a system ("chase mode off",
#: "chase is done"). "over" never appears here — the signoff strip owns it.
_CHASE_TERMINATORS = frozenset(
    ("is", "off", "done", "end", "ended", "ending", "stop", "stopped", "complete", "finished")
)


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


def bare_override(transcript: str) -> bool:
    """True for a *standalone* "command override" — the doorway spoken with
    no question attached (GDD §6.6 dialogue opener).

    Happens constantly in practice: the capture window closes on the pause
    after "command override," before the pilot asks their question. The App
    acknowledges and reopens a wake-free window; the next utterance is the
    question itself, no prefix needed.
    """
    if not transcript or not transcript.strip():
        return False
    work = _BROADCAST_WAKE_RE.sub("", transcript, count=1)
    work = _WAKE_RE.sub("", work, count=1)
    m = _OVERRIDE_RE.match(work)
    if m is None:
        return False
    rest = _SIGNOFF_RE.sub("", work[m.end() :]).strip(" ,.;:!?-")
    return not rest


def bare_code(transcript: str) -> Severity | None:
    """Detect a *standalone* colour code — the dialogue opener (GDD §6.4).

    "Code orange." with nothing else after wake/sign-off stripping means the
    pilot is announcing severity first and will give the report in the next
    breath: CORTANA acknowledges and reopens a wake-free capture window. Returns
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

    if intent is Intent.FACT:
        # Fun command (GDD §13.2): the topic word usually PRECEDES the intent
        # word ("space fact"), so the topic window is the whole utterance
        # minus the intent match; the FunEngine matches it against its
        # category aliases. Never resolved against the gazetteer.
        topic = match.re.sub(" ", work).strip(" ,.;:!?-")
        return ParsedCommand(
            intent=intent,
            system_text=None,
            group_alias=group_alias,
            detail=topic or None,
            raw=transcript,
            severity=severity,
        )

    if intent is Intent.INSULT:
        # The remainder may name a target ("insult Dave") — cleaned by the
        # FunEngine, never parsed here beyond capture (GDD §6.3 spirit).
        return ParsedCommand(
            intent=intent,
            system_text=None,
            group_alias=group_alias,
            detail=remainder or None,
            raw=transcript,
            severity=severity,
        )

    if intent in (Intent.TIMER, Intent.FORMUP):
        system_text, detail = _split_timer(remainder)
    else:
        system_text, detail = _split_system_detail(remainder)

    # "chase mode off" / "chase is over" / "chase done" — the pilot is
    # CLOSING the chase, not naming a system called "off". A terminator
    # window means no system; the engine answers with the chase hint
    # instead of writing junk onto the live card verbatim.
    if (
        intent is Intent.CHASE_UPDATE
        and system_text is not None
        and system_text.lower() in _CHASE_TERMINATORS
    ):
        system_text = None

    return ParsedCommand(
        intent=intent,
        system_text=system_text,
        group_alias=group_alias,
        detail=detail,
        raw=transcript,
        severity=severity,
    )
