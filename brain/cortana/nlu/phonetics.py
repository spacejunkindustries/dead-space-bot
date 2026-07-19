"""Phonetic system-name resolution — GDD §8.2–§8.5.

STT errors are phonetic, not typographic (constraint 7): Whisper writes
*"oh tan you oh me"*, which is character-distance-far from *Otanuomi* but
phonetically adjacent. So candidates are scored Metaphone-first:

    base = phonetic_weight · phonetic_sim + text_weight · text_sim

then reweighted by the context priors (GDD §8.4) and cut into confidence
tiers (GDD §8.3). The learned-alias table is consulted BEFORE any phonetic
matching (GDD §8.5) — an exact alias hit resolves immediately at full
confidence.

``double_metaphone`` is implemented here in pure Python (the pypi
``metaphone`` package does not build in this environment). It follows the
shape of Lawrence Philips' Double Metaphone: consonant classes collapse,
vowels survive only word-initially, and ambiguous graphemes fork the
primary/alternate codes. It does not chase every English-orthography edge
case — the gazetteer is EVE system names and STT phonetic spellings, not a
census file — but every rule it does implement matches the original.

Everything in this module is a pure function apart from the optional sqlite
alias lookup in :func:`resolve`; scoring helpers are importable and testable
in isolation (CLAUDE.md conventions).
"""

from __future__ import annotations

import re
import sqlite3
from functools import lru_cache
from typing import TYPE_CHECKING

import structlog

from cortana.config import MatchingConfig, PriorsConfig, TiersConfig
from cortana.core import areas, db
from cortana.types import MatchCandidate, PriorContext, Resolution, SystemEntry, Tier

if TYPE_CHECKING:  # pragma: no cover — import cycle guard only
    from cortana.nlu.gazetteer import Gazetteer

__all__ = [
    "base_score",
    "double_metaphone",
    "levenshtein",
    "normalize",
    "resolve",
    "similarity",
    "tier_for",
]

log = structlog.get_logger(__name__)

#: How many candidates survive into the Resolution (GDD §8.2: "top-3").
TOP_N = 3

#: How many candidates the priors reweight before the final cut. Slightly
#: wider than TOP_N so a strong prior can promote a 4th/5th-ranked candidate
#: into the top-3 (a fleet fight is spatially clustered — GDD §8.4).
_RERANK_POOL = 8

_VOWELS = frozenset("AEIOUY")

# Filler dropped during normalisation (GDD §8.2 "strip filler").
_FILLER = frozenset(("the", "a", "an", "in", "at", "on", "um", "uh", "er", "like", "system", "sys"))

_DIGIT_WORDS = {
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "nine",
}

# ── the "tack" convention (GDD §8.2) ─────────────────────────────────────────
# EVE pilots speak the hyphen in a nullsec designation as "tack" (also "dash"
# or "hyphen") and spell the letters out: M-OEE8 is said "m tack o double e
# eight". Whisper renders that as a string of single-letter tokens, which the
# window matcher would otherwise treat as unrelated words.

#: Spoken renderings of the hyphen inside a spelled system name.
_HYPHEN_WORDS = frozenset(("tack", "tac", "dash", "hyphen"))

#: "double e" → "ee", "triple x" → "xxx" — radio spelling multipliers.
_MULTIPLIER_WORDS = {"double": 2, "triple": 3}

#: Reverse of ``_DIGIT_WORDS`` — inside a spelled span, "eight" is the digit 8.
_WORD_DIGITS = {word: digit for digit, word in _DIGIT_WORDS.items()}


# ── Double Metaphone ─────────────────────────────────────────────────────────


def _is_vowel(word: str, i: int) -> bool:
    return 0 <= i < len(word) and word[i] in _VOWELS


@lru_cache(maxsize=8192)
def double_metaphone(word: str) -> tuple[str, str]:
    """Encode ``word`` into (primary, alternate) Double Metaphone codes.

    Input may be any case and contain non-letters; only A–Z survive. Both
    codes are equal unless an ambiguous grapheme forked them. Empty input
    encodes to ``("", "")``.
    """
    w = re.sub(r"[^A-Z]", "", word.upper())
    if not w:
        return ("", "")

    primary: list[str] = []
    alternate: list[str] = []

    def add(p: str, a: str | None = None) -> None:
        """Append to the codes, collapsing immediate repeats of a class."""
        a = p if a is None else a
        if p and not (primary and primary[-1] == p and len(p) == 1):
            primary.append(p)
        if a and not (alternate and alternate[-1] == a and len(a) == 1):
            alternate.append(a)

    n = len(w)
    i = 0

    # Silent initial clusters: GNome, KNife, PNeumonia, WRite, PSyche.
    if w[:2] in ("GN", "KN", "PN", "WR", "PS"):
        i = 1
    elif w[0] == "X":  # Xavier
        add("S")
        i = 1
    elif w[:2] == "WH":
        add("W")
        i = 2

    while i < n:
        c = w[i]

        if c in _VOWELS:
            if i == 0:
                add("A")
            i += 1
            continue

        if c == "B":
            add("P")
            i += 2 if w[i : i + 2] == "BB" else 1
        elif c == "C":
            if w[i : i + 2] == "CH":
                if w[i + 2 : i + 3] == "R":  # CHRis
                    add("K")
                else:
                    add("X", "K")
                i += 2
            elif w[i : i + 3] == "CIA":
                add("X")
                i += 2
            elif w[i + 1 : i + 2] in ("I", "E", "Y"):
                add("S")
                i += 2
            elif w[i : i + 2] == "CK" or w[i : i + 2] == "CC":
                add("K")
                i += 2
            else:
                add("K")
                i += 1
        elif c == "D":
            if w[i : i + 2] == "DG" and w[i + 2 : i + 3] in ("E", "I", "Y"):
                add("J")  # eDGe
                i += 3
            elif w[i : i + 2] in ("DT", "DD"):
                add("T")
                i += 2
            else:
                add("T")
                i += 1
        elif c == "F":
            add("F")
            i += 2 if w[i : i + 2] == "FF" else 1
        elif c == "G":
            if w[i : i + 2] == "GH":
                if i == 0:
                    add("K")
                elif _is_vowel(w, i - 1) and not _is_vowel(w, i + 2):
                    add("", "F")  # niGHt (silent) / lauGH (F)
                else:
                    add("K")
                i += 2
            elif w[i : i + 2] == "GN":
                add("N", "KN")
                i += 2
            elif w[i + 1 : i + 2] in ("E", "I", "Y"):
                add("J", "K")  # German / Geiger
                i += 2 if w[i : i + 2] == "GG" else 1
            else:
                add("K")
                i += 2 if w[i : i + 2] == "GG" else 1
        elif c == "H":
            if (i == 0 or _is_vowel(w, i - 1)) and _is_vowel(w, i + 1):
                add("H")
            i += 1  # otherwise silent
        elif c == "J":
            add("J", "H")  # Jose
            i += 1
        elif c == "K":
            add("K")
            i += 2 if w[i : i + 2] == "KK" else 1
        elif c == "L":
            add("L")
            i += 2 if w[i : i + 2] == "LL" else 1
        elif c == "M":
            add("M")
            i += 2 if w[i : i + 2] == "MM" else 1
        elif c == "N":
            add("N")
            i += 2 if w[i : i + 2] == "NN" else 1
        elif c == "P":
            if w[i : i + 2] == "PH":
                add("F")
                i += 2
            else:
                add("P")
                i += 2 if w[i : i + 2] == "PP" else 1
        elif c == "Q":
            add("K")
            i += 1
        elif c == "R":
            add("R")
            i += 2 if w[i : i + 2] == "RR" else 1
        elif c == "S":
            if w[i : i + 2] == "SH":
                add("X")
                i += 2
            elif w[i : i + 3] in ("SIO", "SIA"):
                add("S", "X")
                i += 1
            elif w[i : i + 3] == "SCH":
                add("SK", "X")
                i += 3
            elif w[i : i + 2] == "SC":
                if w[i + 2 : i + 3] in ("I", "E", "Y"):
                    add("S")  # SCIence
                else:
                    add("SK")  # SCar
                i += 2
            else:
                add("S")
                i += 2 if w[i : i + 2] == "SS" else 1
        elif c == "T":
            if w[i : i + 4] == "TION" or w[i : i + 3] == "TIA" or w[i : i + 3] == "TCH":
                add("X")
                i += 3
            elif w[i : i + 2] == "TH":
                add("0", "T")  # THeta
                i += 2
            else:
                add("T")
                i += 2 if w[i : i + 2] == "TT" else 1
        elif c == "V":
            add("F")
            i += 2 if w[i : i + 2] == "VV" else 1
        elif c == "W":
            if _is_vowel(w, i + 1):
                add("W", "F")
            i += 1  # trailing/consonant W is silent
        elif c == "X":
            add("KS")
            i += 1
        elif c == "Z":
            add("S", "TS")
            i += 2 if w[i : i + 2] == "ZZ" else 1
        else:  # pragma: no cover — every A–Z letter is handled above
            i += 1

    return ("".join(primary), "".join(alternate))


# ── pure scoring helpers (GDD §8.2) ──────────────────────────────────────────


def levenshtein(a: str, b: str) -> int:
    """Classic edit distance, O(len(a)·len(b)); inputs here are short."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def similarity(a: str, b: str) -> float:
    """``1 − levenshtein/len`` in [0, 1]; 0.0 when either side is empty."""
    if not a or not b:
        return 0.0
    return 1.0 - levenshtein(a, b) / max(len(a), len(b))


def _fuse_spelled(run: list[tuple[str, bool]]) -> str | None:
    """Fuse one spelled-name span ("m", "tack", "o", "ee", "8" → "m-oee8").

    A span fuses only when it is at least two tokens long and contains a
    letter or a spoken hyphen — a bare pair of digits ("4 4") is two numerals,
    not a spelling, and falls back to normal handling.
    """
    if len(run) < 2:
        return None
    anchored = False
    parts: list[str] = []
    for tok, forced in run:
        if not forced and tok in _HYPHEN_WORDS:
            parts.append("-")
            anchored = True
            continue
        part = _WORD_DIGITS.get(tok, tok)
        if forced or part.isalpha():
            anchored = True
        parts.append(part)
    return "".join(parts) if anchored else None


def normalize(text: str) -> list[str]:
    """Lowercase, strip punctuation and filler, expand standalone numerals,
    and fuse spelled system names (GDD §8.2 "tack" convention): hyphen words
    fold to ``-``, adjacent single letters/digits fuse into one token, and
    "double e"/"triple x" expand to "ee"/"xxx" — so *"m tack o double e 8"*
    becomes the single token ``m-oee8``.
    """
    raw = [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]

    # Expand "double <char>"/"triple <char>" into a forced spelled part.
    expanded: list[tuple[str, bool]] = []
    i = 0
    while i < len(raw):
        tok = raw[i]
        if tok in _MULTIPLIER_WORDS and i + 1 < len(raw):
            char = _WORD_DIGITS.get(raw[i + 1], raw[i + 1])
            if len(char) == 1:
                expanded.append((char * _MULTIPLIER_WORDS[tok], True))
                i += 2
                continue
        expanded.append((tok, False))
        i += 1

    tokens: list[str] = []

    def emit(run: list[tuple[str, bool]]) -> None:
        fused = _fuse_spelled(run)
        if fused is not None:
            tokens.append(fused)
            return
        for tok, _ in run:  # not a spelling — normal filler/numeral handling
            if tok not in _FILLER:
                tokens.append(_DIGIT_WORDS.get(tok, tok))

    run: list[tuple[str, bool]] = []
    for tok, forced in expanded:
        # Spelled-span members: forced expansions, hyphen words, single
        # letters/digits, and spoken digit words ("one d q one tack a").
        if forced or tok in _HYPHEN_WORDS or tok in _WORD_DIGITS or len(tok) == 1:
            run.append((tok, forced))
            continue
        emit(run)
        run = []
        if tok not in _FILLER:
            tokens.append(_DIGIT_WORDS.get(tok, tok))
    emit(run)
    return tokens


def _phonetic_similarity(a: str, b: str) -> float:
    """Best similarity across the primary/alternate code pairs of both sides."""
    pa, aa = double_metaphone(a)
    pb, ab = double_metaphone(b)
    return max(similarity(pa, pb), similarity(pa, ab), similarity(aa, pb), similarity(aa, ab))


def _collapse(text: str) -> str:
    """Lowercase and drop everything but letters/digits — the comparison form
    used for character-level scoring and prefix checks."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


#: Minimum abbreviation-match score to promote a nullsec short-form match —
#: high enough that only a clearly-spoken prefix ("UMI"→UMI-KK) counts.
_ABBREV_PROMOTE_MIN = 0.78

#: Score granted by a unique-prefix promotion (GDD §8.2). Clears the HIGH
#: tier floor on its own — an unambiguous spelled prefix is a deliberate,
#: exact utterance, not a fuzzy hearing.
_PREFIX_PROMOTE_SCORE = 0.90

#: Shortest collapsed window eligible for prefix promotion ("mo" for M-OEE8).
#: Single characters are far too promiscuous to promote.
_PREFIX_MIN_LEN = 2


def eve_abbrev(name: str) -> str | None:
    """The spoken short form of a nullsec system name — the part before the
    first hyphen. EVE nullsec systems are ``PREFIX-SUFFIX`` ("UMI-KK",
    "MOEE-8", "1DQ1-A", "NVLF6-2K") and pilots call them by the prefix alone;
    the full designation is almost never said. Returns ``None`` when there is
    no distinct hyphenated short form (high/low-sec names like "Jita")."""
    head = name.split("-", 1)[0].strip()
    if head and head != name and len(head) >= 2:
        return head
    return None


def base_score(window: str, name: str, cfg: MatchingConfig) -> float:
    """``phonetic_weight·phonetic_sim + text_weight·text_sim`` — GDD §8.2.

    ``window`` is a joined token window from the transcript, ``name`` a
    gazetteer system name. Spaces/hyphens are collapsed on both sides so
    *"oh tan you oh me"* lines up with *Otanuomi* character-wise too. For a
    hyphenated nullsec name the window is also scored against the spoken
    short form (:func:`eve_abbrev`), so *"UMI"* matches *UMI-KK* and *"Moe 8"*
    matches *MOEE-8* — the full tail no longer drags the confidence down.
    """
    a = _collapse(window)
    b = _collapse(name)
    score = cfg.phonetic_weight * _phonetic_similarity(a, b) + cfg.text_weight * similarity(a, b)

    abbrev = eve_abbrev(name)
    if abbrev is not None:
        c = _collapse(abbrev)
        # Score the window against the short form, but only *promote* the match
        # when it is strong (>= _ABBREV_PROMOTE_MIN) and the window is nearly
        # as long as the prefix. A clearly-spoken abbreviation ("UMI", "Moe")
        # clears this bar; a coincidental phonetic overlap between a noise
        # window ("oh me") and a prefix ("MOEE") does not — which keeps
        # abbreviations from inventing spurious competitors.
        if len(a) >= max(3, len(c) - 1):
            abbrev_score = cfg.phonetic_weight * _phonetic_similarity(a, c) + cfg.text_weight * (
                similarity(a, c)
            )
            if abbrev_score >= _ABBREV_PROMOTE_MIN:
                score = max(score, abbrev_score)
    return score


def _promote_unique_prefixes(
    windows: list[str],
    best_base: dict[int, float],
    names: dict[int, str],
) -> None:
    """Unique-prefix promotion for spelled 1-char-head names (GDD §8.2).

    :func:`eve_abbrev` cannot cover names like ``M-OEE8`` — the pre-hyphen
    head is a single character, so there is no spoken short form. Pilots say
    them spelled ("m tack o", "m tack o double e"), which collapses to a
    *prefix* of the collapsed name ("mo", "moee"). A window that is a prefix
    of exactly ONE active-set collapsed name promotes that name to a strong
    match; a prefix shared by several names is ambiguous and never promotes —
    the confirm-flow (§8.3) owns that case, not a guess.
    """
    collapsed = {system_id: _collapse(name) for system_id, name in names.items()}
    for window in windows:
        w = _collapse(window)
        if len(w) < _PREFIX_MIN_LEN:
            continue
        hits = [system_id for system_id, cname in collapsed.items() if cname.startswith(w)]
        if len(hits) != 1:
            continue
        name = names[hits[0]]
        # Only names with no spoken short form need this path; UMI-KK et al
        # are already covered by the abbreviation promotion in base_score.
        if "-" not in name or eve_abbrev(name) is not None:
            continue
        best_base[hits[0]] = max(best_base[hits[0]], _PREFIX_PROMOTE_SCORE)


def _windows(tokens: list[str]) -> list[str]:
    """Sliding 1–3 token windows, plus the full span when it is longer —
    Whisper splits one system name into many short tokens."""
    out: list[str] = []
    seen: set[str] = set()
    for size in (1, 2, 3):
        for start in range(len(tokens) - size + 1):
            joined = "".join(tokens[start : start + size])
            if joined not in seen:
                seen.add(joined)
                out.append(joined)
    if len(tokens) > 3:
        joined = "".join(tokens)
        if joined not in seen:
            out.append(joined)
    return out


def _prior_multiplier(
    system_id: int,
    priors: PriorContext,
    cfg: PriorsConfig,
    gazetteer: Gazetteer,
) -> float:
    """1 + Σ weight·factor — the cheap multiplicative reweighting of GDD §8.4."""
    boost = 0.0

    minutes = priors.recency_min.get(system_id)
    if minutes is not None and cfg.recency_window_min > 0:
        boost += cfg.recency_weight * max(0.0, 1.0 - minutes / cfg.recency_window_min)

    if priors.active_systems and cfg.proximity_max_jumps > 0:
        best: int | None = None
        for active_id in priors.active_systems:
            d = gazetteer.jumps(system_id, active_id)
            if d is not None and (best is None or d < best):
                best = d
        if best is not None and best <= cfg.proximity_max_jumps:
            boost += cfg.proximity_weight * (1.0 - best / cfg.proximity_max_jumps)

    count = priors.reporter_counts.get(system_id, 0)
    if count > 0:
        boost += cfg.reporter_history_weight * min(count, 5) / 5.0

    home = priors.home_system_id
    if home is not None:
        if system_id == home:
            boost += cfg.home_weight
        else:
            d = gazetteer.jumps(system_id, home)
            if d is not None and d == 1:
                boost += cfg.home_weight * 0.5

    return 1.0 + boost


def tier_for(candidates: list[MatchCandidate], tiers: TiersConfig) -> Tier:
    """Confidence tiers — GDD §8.3. CORTANA never silently guesses."""
    if not candidates:
        return Tier.LOW
    top1 = candidates[0].score
    margin_ok = len(candidates) == 1 or top1 - candidates[1].score >= tiers.high_margin
    if top1 >= tiers.high_min and margin_ok:
        return Tier.HIGH
    if top1 >= tiers.medium_min:
        return Tier.MEDIUM
    return Tier.LOW


def _alias_lookup(
    conn: sqlite3.Connection, raw_text: str, gazetteer: Gazetteer
) -> MatchCandidate | None:
    """Learned-alias exact hit — consulted BEFORE phonetic matching (GDD §8.5).

    Keys are ``raw_text.strip().lower()``, exactly as the incident engine
    writes them on a [Wrong — fix] correction.
    """
    key = raw_text.strip().lower()
    if not key:
        return None
    rows = db.query(
        conn,
        "SELECT system_id FROM aliases WHERE raw_text = ? ORDER BY weight DESC, learned_at DESC",
        (key,),
    )
    for row in rows:
        # entry_any, not by_id: a learned alias must resolve even to a system
        # outside the current scope (a corp that moved, or a distant report) —
        # the alias table IS the pilot's correction, scope must not veto it.
        entry = gazetteer.entry_any(row["system_id"])
        if entry is not None:  # aliases to systems missing from the seed skipped
            return MatchCandidate(system_id=entry.id, name=entry.name, score=1.0)
    return None


def resolve(
    text: str,
    gazetteer: Gazetteer,
    priors: PriorContext,
    cfg: MatchingConfig,
    conn: sqlite3.Connection | None = None,
    *,
    guild_id: int | None = None,
) -> Resolution:
    """Resolve a spoken system reference against the gazetteer — GDD §8.2.

    Blocking (sqlite alias lookup, O(windows·systems) scoring): production
    callers run this via ``asyncio.to_thread``. Pass ``conn=None`` in pure
    scoring tests to skip the alias table; ``guild_id`` is needed only to
    consult the per-guild custom-area table (§8.5a).
    """
    # FC-authored custom names (GDD §8.5) win first — the corp's own vocabulary
    # for a place is explicit configuration, so it resolves at full confidence
    # before both the learned-alias table and any phonetic matching.
    config_hit = gazetteer.config_alias(text)
    if config_hit is not None:
        log.info("config_alias_hit", raw=text.strip().lower(), system=config_hit.name)
        return Resolution(
            tier=Tier.HIGH,
            candidates=(MatchCandidate(config_hit.id, config_hit.name, 1.0),),
        )

    if conn is not None:
        hit = _alias_lookup(conn, text, gazetteer)
        if hit is not None:
            log.info("alias_hit", raw=text.strip().lower(), system=hit.name)
            return Resolution(tier=Tier.HIGH, candidates=(hit,))

    tokens = normalize(text)
    if not tokens:
        return _area_or_low(conn, guild_id, text)
    windows = _windows(tokens)

    # Tier 1: the scoped active set, WITH context priors (home bias, proximity,
    # recency) — small pool, home-region accuracy (GDD §8.2/§8.4).
    scoped = _score_pool(windows, gazetteer.systems, priors, cfg, gazetteer)

    # Tier 2 (GDD §8.1): if the scoped set produced no confident match, re-score
    # against the ENTIRE seeded k-space map so a report of ANY real system still
    # resolves — the fix for a corp whose scope is small or who roams. The
    # home/proximity priors naturally don't fire out of region, so the full-map
    # pass runs without them; a MEDIUM full-map hit rides the confirm-first flow
    # (§8.3). Only engaged when scoped failed AND the full map is genuinely
    # wider, so an in-region corp pays nothing.
    if (
        cfg.full_map_fallback
        and scoped.tier is Tier.LOW
        and len(gazetteer.all_systems) > len(gazetteer.systems)
    ):
        full = _score_pool(windows, gazetteer.all_systems, priors, cfg, gazetteer)
        if full.tier is not Tier.LOW:
            log.info("full_map_fallback_hit", raw=text.strip().lower(), tier=str(full.tier))
            return full
    # Only when NO system resolves (LOW) does a learned custom area apply — it is
    # the systemless twin for genuinely-unknown places (GDD §8.5a). Ordered last
    # so a real system, phonetic match included, always wins over an area of the
    # same name; the learn gate only ever creates areas for LOW words anyway.
    if scoped.tier is Tier.LOW:
        return _area_or_low(conn, guild_id, text, scoped)
    return scoped


def _area_or_low(
    conn: sqlite3.Connection | None,
    guild_id: int | None,
    text: str,
    low: Resolution | None = None,
) -> Resolution:
    """A learned custom area (GDD §8.5a) if one matches, else the LOW result.
    The systemless fallback consulted only when phonetic resolution failed."""
    if conn is not None and guild_id is not None:
        area = areas.lookup_area(conn, guild_id, text)
        if area is not None:
            log.info("custom_area_hit", raw=text.strip().lower(), area=area)
            return Resolution(tier=Tier.HIGH, candidates=(), area_name=area)
    return low if low is not None else Resolution(tier=Tier.LOW, candidates=())


def _score_pool(
    windows: list[str],
    systems: tuple[SystemEntry, ...],
    priors: PriorContext,
    cfg: MatchingConfig,
    gazetteer: Gazetteer,
) -> Resolution:
    """Score the sliding windows against one system pool and rank (GDD §8.2)."""
    if not systems:
        return Resolution(tier=Tier.LOW, candidates=())
    best_base: dict[int, float] = {}
    names: dict[int, str] = {}
    for entry in systems:
        best = 0.0
        for window in windows:
            score = base_score(window, entry.name, cfg)
            if score > best:
                best = score
        best_base[entry.id] = best
        names[entry.id] = entry.name

    _promote_unique_prefixes(windows, best_base, names)

    pool = sorted(best_base.items(), key=lambda kv: kv[1], reverse=True)[:_RERANK_POOL]
    rescored = [
        MatchCandidate(
            system_id=system_id,
            name=names[system_id],
            score=min(1.0, base * _prior_multiplier(system_id, priors, cfg.priors, gazetteer)),
        )
        for system_id, base in pool
    ]
    rescored.sort(key=lambda c: c.score, reverse=True)
    top = rescored[:TOP_N]
    return Resolution(tier=tier_for(top, cfg.tiers), candidates=tuple(top))
