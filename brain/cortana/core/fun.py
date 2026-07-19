"""FunEngine — the fact library and the insult maker (GDD §13.2).

The one shared engine behind the voice ``FACT``/``INSULT`` intents and their
slash twins ``/fact``/``/insult`` (constraint 10). Pure logic over a static,
bundled library:

- **Facts** ship as JSON arrays of strings in ``cortana/data/facts/*.json``,
  one file per category — written and accuracy-gated offline, loaded once at
  startup. No network, no LLM (constraint 6 stays intact: this is a lookup,
  not generation).
- **Insults** ship as ``cortana/data/facts/insults_*.json`` arrays of
  ``{"text", "spicy"}`` — ``spicy`` marks profanity so ``fun.insults_spicy``
  can tone the pool down without a redeploy.
- A per-guild **shuffle bag** deals the whole deck before any repeat, so a
  long evening on comms doesn't loop the same five facts.
- Per-guild **cooldowns** (facts and insults separately) keep comedy from
  crowding real comms; ALERT-priority incident speech always jumps the fun
  line anyway (§12.2 queue ordering).

Delivery policy (the user's explicit choice): voice requests are answered in
voice ONLY — no channel post; the slash twins answer in the channel they were
invoked in, and only there.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from cortana.nlu.grammar import sanitize_callsign

if TYPE_CHECKING:
    from cortana.config import ConfigHolder

__all__ = [
    "CATEGORIES",
    "FactLine",
    "FunCooldown",
    "FunEngine",
    "FunLibrary",
    "FunUnavailable",
    "extract_target",
    "load_library",
    "match_category",
]

log = structlog.get_logger(__name__)

#: Hard ceiling on any line read aloud — defence against a malformed library
#: entry; the authored caps are 160 (facts) / 140 (insults).
_LINE_MAX_LEN = 220

#: Combined-pool key for "any category" in the shuffle-bag map.
_ALL = "*"


class FunUnavailable(Exception):
    """The library (or the requested slice of it) has nothing to serve."""


class FunCooldown(Exception):
    """The per-guild throttle is active; ``remaining_s`` says for how long."""

    def __init__(self, remaining_s: float) -> None:
        super().__init__(f"cooling down for {remaining_s:.0f}s")
        self.remaining_s = remaining_s


@dataclass(frozen=True, slots=True)
class FactLine:
    """One served fact: the spoken text plus its category (display) title."""

    text: str
    category_key: str
    category_title: str


#: Category key → (display title, spoken aliases). The alias vocabulary is
#: what the voice path matches ("space fact", "fact about animals"); the
#: slash twin offers the keys as choices. Fixed table, constraint 6.
CATEGORIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "astronomy": (
        "Space & astronomy",
        ("space", "astronomy", "star", "stars", "planet", "planets", "cosmos", "galaxy"),
    ),
    "physics": ("Physics & quantum", ("physics", "quantum", "relativity")),
    "history": ("History", ("history", "historical", "ancient")),
    "military": (
        "Military & naval",
        ("military", "war", "navy", "naval", "army", "battle", "battles"),
    ),
    "tech": (
        "Technology & computing",
        ("tech", "technology", "computer", "computers", "computing", "internet"),
    ),
    "animals": (
        "Animals & wildlife",
        ("animal", "animals", "wildlife", "nature", "bug", "bugs", "bird", "birds"),
    ),
    "body": ("The human body", ("body", "medical", "medicine", "anatomy", "brain", "health")),
    "ocean": ("Ocean & deep sea", ("ocean", "oceans", "sea", "deep", "marine")),
    "gaming": (
        "Games & internet culture",
        ("gaming", "game", "games", "videogame", "videogames", "esports"),
    ),
    "math": ("Math & numbers", ("math", "maths", "mathematics", "number", "numbers")),
    "geography": (
        "Geography & weird world",
        ("geography", "world", "country", "countries", "earth", "place", "places"),
    ),
    "engineering": (
        "Engineering & rockets",
        ("engineering", "rocket", "rockets", "machine", "machines", "bridge", "bridges"),
    ),
    "language": ("Words & language", ("language", "languages", "word", "words", "etymology")),
    "food": ("Food & drink", ("food", "drink", "cooking", "coffee", "eating")),
    "science": ("Science", ("science", "chemistry", "biology", "geology", "weather")),
    "eve": ("New Eden lore", ("eve", "echoes", "eden", "lore", "capsuleer", "concord")),
}

_TOKEN_RE = re.compile(r"[\s,.;:!?'-]+")

#: Connective filler around a spoken topic ("tell me a fact ABOUT the ocean")
#: and around a spoken insult target ("insult THIS GUY for me").
_TOPIC_FILLER = frozenset(
    (
        "tell",
        "give",
        "hit",
        "share",
        "read",
        "say",
        "me",
        "us",
        "a",
        "an",
        "the",
        "about",
        "on",
        "of",
        "another",
        "again",
        "one",
        "more",
        "please",
        "quick",
        "quickly",
        "random",
        "fun",
        "cool",
        "interesting",
        "little",
        "some",
        "your",
        "best",
        "favorite",
        "favourite",
        "now",
        "time",
        "do",
        "you",
        "know",
        "got",
        "any",
        "and",
        "drop",
        "with",
        "for",
    )
)

_TARGET_FILLER = frozenset(
    (
        "this",
        "that",
        "guy",
        "dude",
        "man",
        "girl",
        "him",
        "her",
        "them",
        "the",
        "a",
        "an",
        "my",
        "our",
        "friend",
        "buddy",
        "mate",
        "please",
        "for",
        "me",
        "us",
        "at",
        "on",
        "to",
        "here",
        "little",
        "good",
        "real",
        "hard",
        "ass",
        "out",
        "of",
        "everyone",
        "somebody",
        "someone",
    )
)

#: Longest plausible spoken target: a first name or short handle.
_TARGET_MAX_TOKENS = 3


def match_category(text: str | None) -> str | None:
    """Map a spoken topic window to a category key, or ``None`` for "any".

    Token-level alias lookup over the fixed table — "space", "give me an
    animal fact", "fact about the deep sea" all land. Unknown topics fall
    back to the whole library rather than rejecting: a fact is a fact.
    """
    if not text:
        return None
    for token in _TOKEN_RE.split(text.lower()):
        if not token:
            continue
        for key, (_title, aliases) in CATEGORIES.items():
            if token == key or token in aliases:
                return key
    return None


def extract_target(text: str | None) -> str | None:
    """Pull a spoken insult target out of the post-intent remainder.

    "this guy" / "him" / empty → ``None`` (an untargeted roast); a surviving
    short name is title-cased and sanitised with the callsign rules so it can
    never smuggle markdown or a mention into a channel post.
    """
    if not text:
        return None
    tokens = [t for t in _TOKEN_RE.split(text) if t]
    kept = [t for t in tokens if t.lower() not in _TARGET_FILLER]
    if not kept or len(kept) > _TARGET_MAX_TOKENS:
        return None
    cleaned = sanitize_callsign(" ".join(kept))
    return cleaned.title() if cleaned else None


# ── library loading ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Insult:
    text: str
    spicy: bool


@dataclass(frozen=True, slots=True)
class FunLibrary:
    """The loaded content: facts per category plus the insult pool."""

    facts: dict[str, tuple[str, ...]]
    insults: tuple[_Insult, ...]

    @property
    def fact_count(self) -> int:
        return sum(len(v) for v in self.facts.values())


def _clean_line(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    line = " ".join(value.split())
    if not line or len(line) > _LINE_MAX_LEN:
        return None
    return line


def load_library(base: Path | None = None) -> FunLibrary:
    """Load ``cortana/data/facts`` (or ``base`` in tests).

    Tolerant by design: a malformed file or entry is logged and skipped —
    a bad line in the joke library must never stop the intel bot.
    """
    if base is None:
        base = Path(str(resources.files("cortana") / "data" / "facts"))
    facts: dict[str, tuple[str, ...]] = {}
    insults: list[_Insult] = []
    if not base.is_dir():
        log.warning("fun_library_missing", path=str(base))
        return FunLibrary(facts={}, insults=())
    for path in sorted(base.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("fun_library_file_skipped", path=str(path), error=str(exc))
            continue
        if not isinstance(data, list):
            log.warning("fun_library_file_skipped", path=str(path), error="not a JSON array")
            continue
        if path.stem.startswith("insults_"):
            for item in data:
                if not isinstance(item, dict):
                    continue
                text = _clean_line(item.get("text"))
                if text is not None:
                    insults.append(_Insult(text=text, spicy=bool(item.get("spicy", False))))
            continue
        if path.stem not in CATEGORIES:
            log.warning("fun_library_unknown_category", path=str(path))
            continue
        lines = tuple(dict.fromkeys(t for v in data if (t := _clean_line(v)) is not None))
        if lines:
            facts[path.stem] = lines
    log.info(
        "fun_library_loaded",
        categories=len(facts),
        facts=sum(len(v) for v in facts.values()),
        insults=len(insults),
    )
    return FunLibrary(facts=facts, insults=tuple(insults))


# ── the engine ───────────────────────────────────────────────────────────────


class FunEngine:
    """Shuffle-bag deals + cooldowns over the loaded library. One instance
    serves all guilds; both input surfaces call it (constraint 10)."""

    def __init__(
        self,
        holder: ConfigHolder,
        library: FunLibrary | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self._holder = holder
        self._library = library if library is not None else load_library()
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()
        #: (guild_id, pool_key) → remaining shuffled positions (dealt from the end).
        self._bags: dict[tuple[int, str], list[int]] = {}
        #: (guild_id, pool_key) → last dealt position, for no-immediate-repeat.
        self._last: dict[tuple[int, str], int] = {}
        self._fact_ready_at: dict[int, float] = {}
        self._insult_ready_at: dict[int, float] = {}
        # Stable flattened view for the "any category" pool.
        self._all_facts: tuple[tuple[str, str], ...] = tuple(
            (key, text) for key, lines in sorted(self._library.facts.items()) for text in lines
        )

    @property
    def fact_count(self) -> int:
        return len(self._all_facts)

    @property
    def insult_count(self) -> int:
        return len(self._library.insults)

    def categories_loaded(self) -> tuple[str, ...]:
        """Category keys that actually have facts, in table order."""
        return tuple(k for k in CATEGORIES if k in self._library.facts)

    # ── dealing ──────────────────────────────────────────────────────────────

    def _deal(self, guild_id: int, pool_key: str, size: int) -> int:
        """Next position from the (guild, pool) shuffle bag; refills and
        reshuffles when empty, never repeating the last deal back-to-back."""
        bag_key = (guild_id, pool_key)
        bag = self._bags.get(bag_key)
        if not bag:
            bag = list(range(size))
            self._rng.shuffle(bag)
            last = self._last.get(bag_key)
            if size > 1 and last is not None and bag[-1] == last:
                swap = self._rng.randrange(size - 1)
                bag[-1], bag[swap] = bag[swap], bag[-1]
            self._bags[bag_key] = bag
        pos = bag.pop()
        self._last[bag_key] = pos
        return pos

    def _check_cooldown(self, ready_at: dict[int, float], guild_id: int, cooldown_s: float) -> None:
        now = self._clock()
        ready = ready_at.get(guild_id, 0.0)
        if now < ready:
            raise FunCooldown(ready - now)
        ready_at[guild_id] = now + cooldown_s

    def fact_cooldown_remaining(self, guild_id: int) -> float:
        return max(0.0, self._fact_ready_at.get(guild_id, 0.0) - self._clock())

    def insult_cooldown_remaining(self, guild_id: int) -> float:
        return max(0.0, self._insult_ready_at.get(guild_id, 0.0) - self._clock())

    # ── public API (both surfaces) ───────────────────────────────────────────

    def next_fact(self, guild_id: int, topic_text: str | None = None) -> FactLine:
        """Deal one fact — from the matched category, or the whole library.

        Raises :class:`FunCooldown` under the throttle, :class:`FunUnavailable`
        when the library (or the named category) is empty.
        """
        category = match_category(topic_text)
        if category is not None and category not in self._library.facts:
            category = None  # a known alias with no shipped file → any fact
        pool = self._library.facts.get(category, ()) if category else self._all_facts
        if not pool:
            raise FunUnavailable("fact library is empty")
        self._check_cooldown(
            self._fact_ready_at, guild_id, self._holder.current.fun.fact_cooldown_s
        )
        if category:
            pos = self._deal(guild_id, category, len(pool))
            text = self._library.facts[category][pos]
            key = category
        else:
            pos = self._deal(guild_id, _ALL, len(pool))
            key, text = self._all_facts[pos]
        return FactLine(text=text, category_key=key, category_title=CATEGORIES[key][0])

    def next_insult(self, guild_id: int, target_text: str | None = None) -> str:
        """Deal one insult, optionally addressed to a spoken/selected target.

        ``fun.insults_spicy: false`` restricts the pool to the clean lines.
        Raises :class:`FunCooldown` / :class:`FunUnavailable` like facts.
        """
        cfg = self._holder.current.fun
        if cfg.insults_spicy:
            pool: tuple[str, ...] = tuple(i.text for i in self._library.insults)
            pool_key = "insults"
        else:
            pool = tuple(i.text for i in self._library.insults if not i.spicy)
            pool_key = "insults_clean"
        if not pool:
            raise FunUnavailable("insult library is empty")
        self._check_cooldown(self._insult_ready_at, guild_id, cfg.insult_cooldown_s)
        line = pool[self._deal(guild_id, pool_key, len(pool))]
        target = extract_target(target_text)
        return f"{target}. {line}" if target else line
