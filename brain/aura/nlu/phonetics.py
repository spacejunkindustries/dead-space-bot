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

from aura.config import MatchingConfig, PriorsConfig, TiersConfig
from aura.core import db
from aura.types import MatchCandidate, PriorContext, Resolution, Tier

if TYPE_CHECKING:  # pragma: no cover — import cycle guard only
    from aura.nlu.gazetteer import Gazetteer

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


def normalize(text: str) -> list[str]:
    """Lowercase, strip punctuation and filler, expand standalone numerals."""
    tokens: list[str] = []
    for raw in re.split(r"[^a-z0-9]+", text.lower()):
        if not raw or raw in _FILLER:
            continue
        tokens.append(_DIGIT_WORDS.get(raw, raw))
    return tokens


def _phonetic_similarity(a: str, b: str) -> float:
    """Best similarity across the primary/alternate code pairs of both sides."""
    pa, aa = double_metaphone(a)
    pb, ab = double_metaphone(b)
    return max(similarity(pa, pb), similarity(pa, ab), similarity(aa, pb), similarity(aa, ab))


#: Minimum abbreviation-match score to promote a nullsec short-form match —
#: high enough that only a clearly-spoken prefix ("UMI"→UMI-KK) counts.
_ABBREV_PROMOTE_MIN = 0.78


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
    a = re.sub(r"[^a-z0-9]", "", window.lower())
    b = re.sub(r"[^a-z0-9]", "", name.lower())
    score = cfg.phonetic_weight * _phonetic_similarity(a, b) + cfg.text_weight * similarity(a, b)

    abbrev = eve_abbrev(name)
    if abbrev is not None:
        c = re.sub(r"[^a-z0-9]", "", abbrev.lower())
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
    """Confidence tiers — GDD §8.3. AURA never silently guesses."""
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
        entry = gazetteer.by_id(row["system_id"])
        if entry is not None:  # aliases to since-pruned systems are skipped
            return MatchCandidate(system_id=entry.id, name=entry.name, score=1.0)
    return None


def resolve(
    text: str,
    gazetteer: Gazetteer,
    priors: PriorContext,
    cfg: MatchingConfig,
    conn: sqlite3.Connection | None = None,
) -> Resolution:
    """Resolve a spoken system reference against the gazetteer — GDD §8.2.

    Blocking (sqlite alias lookup, O(windows·systems) scoring): production
    callers run this via ``asyncio.to_thread``. Pass ``conn=None`` in pure
    scoring tests to skip the alias table.
    """
    if conn is not None:
        hit = _alias_lookup(conn, text, gazetteer)
        if hit is not None:
            log.info("alias_hit", raw=text.strip().lower(), system=hit.name)
            return Resolution(tier=Tier.HIGH, candidates=(hit,))

    tokens = normalize(text)
    systems = gazetteer.systems
    if not tokens or not systems:
        return Resolution(tier=Tier.LOW, candidates=())

    windows = _windows(tokens)
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
