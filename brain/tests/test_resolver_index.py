"""Blocking-index regression suite — GDD §8.2, constraints 7/8.

The index is a STRICT performance layer: it must change WHICH gazetteer
entries the scorer touches, never the result. These tests pin that two ways:

1. **Equivalence (the neutrality anchor).** For the fixture transcripts, a
   large batch of phonetically-corrupted real names, and pure noise, the
   indexed :func:`_score_pool` returns a Resolution *identical* to the
   brute-force scan over the same pool — same tier, same ordered candidate
   ids, and exact-float-equal scores. Any candidate the full scan would have
   matched but the blocker pruned surfaces here as a mismatch.
2. **The upper bound is valid.** ``_entry_upper_bound`` (and ``_sim_upper``)
   must never underestimate the real ``base_score``/``similarity`` — that is
   what makes a skipped entry provably unable to win.

Plus targeted unit tests for the fallback guard and the pool-global unique-
prefix promotion. No audio anywhere near these tests (constraint 5); the pool
is embedded real system names (tests/resolver_cases.py).
"""

from __future__ import annotations

import random

import pytest

from cortana.config import IndexConfig, MatchingConfig, PriorsConfig, TiersConfig
from cortana.nlu import phonetics
from cortana.nlu.phonetics import (
    PoolIndex,
    _bag,
    _entry_upper_bound,
    _prefix_promotions,
    _promote_unique_prefixes,
    _score_pool,
    _score_pool_bruteforce,
    _score_pool_indexed,
    _sim_upper,
    base_score,
    build_pool_index,
    double_metaphone,
    normalize,
    resolve,
    similarity,
)
from cortana.types import PriorContext, Resolution, SystemEntry, Tier
from tests.resolver_cases import EXPECTED_HIGH, NOISE, POOL_NAMES

CFG = MatchingConfig(
    phonetic_weight=0.6,
    text_weight=0.4,
    tiers=TiersConfig(high_min=0.80, high_margin=0.12, medium_min=0.55),
    priors=PriorsConfig(
        recency_weight=0.35,
        recency_window_min=10,
        proximity_weight=0.25,
        proximity_max_jumps=5,
        reporter_history_weight=0.15,
        home_weight=0.10,
    ),
)
NO_PRIORS = PriorContext()

ENTRIES: tuple[SystemEntry, ...] = tuple(
    SystemEntry(
        id=i + 1,
        name=name,
        region="Region",
        constellation=None,
        metaphone=double_metaphone(name)[0],
    )
    for i, name in enumerate(POOL_NAMES)
)
INDEX: PoolIndex = build_pool_index(ENTRIES)


class Gaz:
    """Minimal gazetteer exposing both pools + their indexes to resolve()."""

    def __init__(self, *, with_index: bool) -> None:
        self._index = INDEX if with_index else None

    @property
    def systems(self) -> tuple[SystemEntry, ...]:
        return ENTRIES

    @property
    def all_systems(self) -> tuple[SystemEntry, ...]:
        return ENTRIES

    @property
    def systems_index(self) -> PoolIndex | None:
        return self._index

    @property
    def all_systems_index(self) -> PoolIndex | None:
        return self._index

    @property
    def home_system_id(self) -> int | None:
        return None

    def jumps(self, a: int, b: int) -> int | None:
        return None

    def config_alias(self, text: str) -> SystemEntry | None:
        return None

    def entry_any(self, system_id: int) -> SystemEntry | None:
        return next((e for e in ENTRIES if e.id == system_id), None)


def _windows(text: str) -> list[str]:
    return phonetics._windows(normalize(text))


def _same(a: Resolution, b: Resolution) -> bool:
    return (
        a.tier == b.tier
        and [c.system_id for c in a.candidates] == [c.system_id for c in b.candidates]
        and [c.score for c in a.candidates] == [c.score for c in b.candidates]
    )


# ── the equivalence anchor (indexed == brute over the full pool) ──────────────


def _assert_pool_equiv(text: str) -> None:
    windows = _windows(text)
    if not windows:
        return
    brute = _score_pool_bruteforce(windows, ENTRIES, NO_PRIORS, CFG, Gaz(with_index=False))
    idx = _score_pool_indexed(windows, ENTRIES, NO_PRIORS, CFG, Gaz(with_index=True), INDEX)
    assert _same(brute, idx), (
        f"indexed diverged from brute for {text!r}:\n"
        f"  brute={brute.tier} {[(c.name, c.score) for c in brute.candidates]}\n"
        f"  idx  ={idx.tier} {[(c.name, c.score) for c in idx.candidates]}"
    )


@pytest.mark.parametrize("text", [t for t, _ in EXPECTED_HIGH] + list(NOISE))
def test_indexed_matches_bruteforce_on_fixture(text: str) -> None:
    _assert_pool_equiv(text)


_VOWELS = "aeiou"


def _corrupt(name: str, rng: random.Random) -> str:
    """Whisper-style phonetic corruption of a real system name."""
    c = name.lower()
    mode = rng.randint(0, 5)
    if mode == 0:  # spelled out per letter (the "tack" rendering)
        return " ".join(c.replace("-", " tack "))
    if mode == 1:  # dropped vowels
        return "".join(ch for ch in c if ch not in _VOWELS) or c
    if mode == 2:  # duplicated vowels
        return "".join(ch * 2 if ch in _VOWELS else ch for ch in c)
    if mode == 3:  # split into random chunks
        parts, i = [], 0
        while i < len(c):
            step = rng.randint(1, 3)
            parts.append(c[i : i + step])
            i += step
        return " ".join(parts)
    if mode == 4:  # homophone fragments
        return c.replace("o", "oh ").replace("u", "you ").replace("i", "ee")
    return c + " " + rng.choice(["hostile", "camp", "spotted", "inbound"])


def test_indexed_matches_bruteforce_under_corruption() -> None:
    """The accuracy-regression harness: corrupt real names every which way and
    assert the indexed resolver never diverges from the full scan."""
    rng = random.Random(20240720)
    for _ in range(400):
        name = rng.choice(POOL_NAMES)
        _assert_pool_equiv(_corrupt(name, rng))


def test_indexed_matches_bruteforce_on_noise() -> None:
    rng = random.Random(99)
    for _ in range(120):
        text = " ".join(
            "".join(
                rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(rng.randint(1, 6))
            )
            for _ in range(rng.randint(1, 5))
        )
        _assert_pool_equiv(text)


# ── the upper bound is valid (never underestimates) ───────────────────────────


def test_sim_upper_bounds_similarity() -> None:
    rng = random.Random(3)
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    for _ in range(4000):
        x = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 10)))
        y = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 10)))
        assert _sim_upper(_bag(x), _bag(y)) >= similarity(x, y) - 1e-12


def test_entry_upper_bound_bounds_base_score() -> None:
    """No entry's cheap bound may fall below its true base_score — the property
    that lets a below-floor entry be skipped without changing the result."""
    rng = random.Random(11)
    windows = [t for t, _ in EXPECTED_HIGH] + ["oh me", "moe", "1 d q", "z z z"]
    for _ in range(1500):
        entry = rng.choice(ENTRIES)
        idx = INDEX.entries[entry.id]
        raw = rng.choice(windows)
        for w in _windows(raw) or [phonetics._collapse(raw)]:
            cw = phonetics._collapse(w)
            p, a = double_metaphone(cw)
            bound = _entry_upper_bound(
                _bag(cw), _bag(p), _bag(a), idx, CFG.phonetic_weight, CFG.text_weight
            )
            assert bound >= base_score(w, entry.name, CFG) - 1e-12


# ── expected resolutions (indexed path, end to end) ───────────────────────────


@pytest.mark.parametrize(("transcript", "expected"), EXPECTED_HIGH)
def test_expected_high_resolutions(transcript: str, expected: str) -> None:
    r = resolve(transcript, Gaz(with_index=True), NO_PRIORS, CFG, conn=None)
    assert r.tier is Tier.HIGH
    assert r.candidates[0].name == expected


@pytest.mark.parametrize(("transcript", "expected"), EXPECTED_HIGH)
def test_expected_resolution_is_index_independent(transcript: str, expected: str) -> None:
    """The same command resolves identically with the index on vs off."""
    on = resolve(transcript, Gaz(with_index=True), NO_PRIORS, CFG, conn=None)
    off = resolve(transcript, Gaz(with_index=False), NO_PRIORS, CFG, conn=None)
    assert _same(on, off)


# ── fallback guard + pool-global promotion ────────────────────────────────────


def test_disabled_index_uses_bruteforce() -> None:
    """The master switch forces the reference path even with an index present."""
    cfg = MatchingConfig(
        phonetic_weight=0.6,
        text_weight=0.4,
        tiers=CFG.tiers,
        priors=CFG.priors,
        index=IndexConfig(enabled=False),
    )
    windows = _windows("oh tan you oh me")
    viadispatch = _score_pool(windows, ENTRIES, NO_PRIORS, cfg, Gaz(with_index=False), INDEX)
    brute = _score_pool_bruteforce(windows, ENTRIES, NO_PRIORS, cfg, Gaz(with_index=False))
    assert _same(viadispatch, brute)


def test_small_pool_still_neutral() -> None:
    """When the pool is smaller than the rerank floor, every entry is scored —
    the guard never prunes below a full scan."""
    small = ENTRIES[:5]
    index = build_pool_index(small)
    windows = _windows("jita")
    brute = _score_pool_bruteforce(windows, small, NO_PRIORS, CFG, Gaz(with_index=False))
    idx = _score_pool_indexed(windows, small, NO_PRIORS, CFG, Gaz(with_index=False), index)
    assert _same(brute, idx)


def test_prefix_promotions_match_bruteforce_promotion() -> None:
    """The index-side unique-prefix promotion must equal the brute-force one,
    evaluated pool-globally (a prefix shared by several names never promotes)."""
    names = {e.id: e.name for e in ENTRIES}
    for raw in ("m tack o", "one d q", "five z", "u", "z"):
        windows = _windows(raw)
        best_base = dict.fromkeys(names, 0.0)
        _promote_unique_prefixes(windows, best_base, names)
        brute_promoted = {sid: v for sid, v in best_base.items() if v > 0.0}
        assert _prefix_promotions(windows, INDEX) == brute_promoted
