"""Phonetic matcher tests — GDD §8.2–§8.5. Scoring is pure; the alias path
uses an in-memory sqlite db (no audio anywhere near these tests)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from aura.config import MatchingConfig, PriorsConfig, TiersConfig
from aura.core import db
from aura.nlu import phonetics
from aura.nlu.phonetics import (
    base_score,
    double_metaphone,
    levenshtein,
    normalize,
    resolve,
    similarity,
    tier_for,
)
from aura.types import MatchCandidate, PriorContext, SystemEntry, Tier

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

NAMES = [
    "Otanuomi",
    "Kisogo",
    "Alenia",
    "Hulmate",
    "Tannolen",
    "Jita",
    "Amarr",
    "Rens",
    "Dodixie",
    "Osmon",
    "Otela",
]


class FakeGazetteer:
    """Duck-typed stand-in: systems / by_id / by_name / jumps / home."""

    def __init__(self, jumps_map: dict[tuple[int, int], int] | None = None) -> None:
        self.entries = tuple(
            SystemEntry(
                id=i + 1,
                name=name,
                region="Kisogo-region",
                constellation=None,
                metaphone=double_metaphone(name)[0],
            )
            for i, name in enumerate(NAMES)
        )
        self._jumps = jumps_map or {}

    @property
    def systems(self) -> tuple[SystemEntry, ...]:
        return self.entries

    def by_id(self, system_id: int) -> SystemEntry | None:
        return next((e for e in self.entries if e.id == system_id), None)

    def by_name(self, name: str) -> SystemEntry | None:
        return next((e for e in self.entries if e.name.lower() == name.lower()), None)

    def jumps(self, a: int, b: int) -> int | None:
        if a == b:
            return 0
        return self._jumps.get((a, b), self._jumps.get((b, a)))

    @property
    def home_system_id(self) -> int | None:
        return 1


GAZ = FakeGazetteer()
NO_PRIORS = PriorContext()


# ── double metaphone ─────────────────────────────────────────────────────────


def test_metaphone_collapses_stt_spelling() -> None:
    """Constraint 7's flagship case: Whisper's rendering phonetically equals
    the real name even though it is character-distance-far."""
    assert double_metaphone("ohtanyouohme")[0] == double_metaphone("Otanuomi")[0]


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("kissogo", "Kisogo"),
        ("kissago", "Kisogo"),
        ("hool mate".replace(" ", ""), "Hulmate"),
        ("PHIL", "FIL"),
        ("knight", "night"),
    ],
)
def test_metaphone_equivalences(a: str, b: str) -> None:
    assert double_metaphone(a)[0] == double_metaphone(b)[0]


def test_metaphone_shapes() -> None:
    assert double_metaphone("") == ("", "")
    assert double_metaphone("123") == ("", "")
    assert double_metaphone("Otanuomi") == ("ATNM", "ATNM")
    primary, alternate = double_metaphone("Jita")
    assert primary == "JT"
    assert alternate == "HT"  # J forks Jose-style


def test_metaphone_discriminates() -> None:
    assert double_metaphone("Kisogo")[0] != double_metaphone("Otanuomi")[0]
    assert double_metaphone("Jita")[0] != double_metaphone("Rens")[0]


# ── pure scoring helpers ─────────────────────────────────────────────────────


def test_levenshtein() -> None:
    assert levenshtein("", "") == 0
    assert levenshtein("abc", "abc") == 0
    assert levenshtein("abc", "") == 3
    assert levenshtein("kitten", "sitting") == 3


def test_similarity_bounds() -> None:
    assert similarity("abc", "abc") == 1.0
    assert similarity("", "abc") == 0.0
    assert 0.0 < similarity("kisogo", "kissogo") < 1.0


def test_normalize() -> None:
    assert normalize("Hostiles, the Otanuomi!!") == ["hostiles", "otanuomi"]
    assert normalize("system 4 4") == ["four", "four"]
    assert normalize("") == []


def test_base_score_weights_phonetics_over_text() -> None:
    """ "oh tan you oh me" must outscore what raw text distance alone gives."""
    score = base_score("ohtanyouohme", "Otanuomi", CFG)
    text_only = similarity("ohtanyouohme", "otanuomi")
    assert score > text_only
    assert score >= 0.80


# ── tiers (GDD §8.3) ─────────────────────────────────────────────────────────


def _cand(score: float, system_id: int = 1) -> MatchCandidate:
    return MatchCandidate(system_id=system_id, name="X", score=score)


def test_tier_high_needs_margin() -> None:
    assert tier_for([_cand(0.9), _cand(0.5, 2)], CFG.tiers) is Tier.HIGH
    assert tier_for([_cand(0.9), _cand(0.85, 2)], CFG.tiers) is Tier.MEDIUM
    assert tier_for([_cand(0.9)], CFG.tiers) is Tier.HIGH


def test_tier_medium_and_low() -> None:
    assert tier_for([_cand(0.6), _cand(0.55, 2)], CFG.tiers) is Tier.MEDIUM
    assert tier_for([_cand(0.4)], CFG.tiers) is Tier.LOW
    assert tier_for([], CFG.tiers) is Tier.LOW


# ── resolve end-to-end ───────────────────────────────────────────────────────


def test_resolve_exact_name_is_high() -> None:
    r = resolve("Otanuomi", GAZ, NO_PRIORS, CFG)
    assert r.tier is Tier.HIGH
    assert r.best is not None
    assert r.best.name == "Otanuomi"
    assert r.best.score == pytest.approx(1.0)


def test_resolve_stt_mangled_name() -> None:
    r = resolve("oh tan you oh me", GAZ, NO_PRIORS, CFG)
    assert r.tier is Tier.HIGH
    assert r.best is not None
    assert r.best.name == "Otanuomi"


def test_resolve_gibberish_is_low() -> None:
    r = resolve("banana bread recipe", GAZ, NO_PRIORS, CFG)
    assert r.tier is Tier.LOW


def test_resolve_empty_input() -> None:
    r = resolve("", GAZ, NO_PRIORS, CFG)
    assert r.tier is Tier.LOW
    assert r.candidates == ()


def test_resolve_top3_best_first() -> None:
    r = resolve("kisogo", GAZ, NO_PRIORS, CFG)
    assert len(r.candidates) <= 3
    scores = [c.score for c in r.candidates]
    assert scores == sorted(scores, reverse=True)
    assert r.candidates[0].name == "Kisogo"


# ── context priors (GDD §8.4) ────────────────────────────────────────────────


def test_recency_prior_boosts_active_system() -> None:
    base = resolve("kisago", GAZ, NO_PRIORS, CFG)
    boosted = resolve(
        "kisago",
        GAZ,
        PriorContext(recency_min={2: 1.0}, home_system_id=1),
        CFG,
    )
    assert base.best is not None and boosted.best is not None
    assert boosted.best.name == "Kisogo"
    assert boosted.best.score > base.best.score


def test_proximity_prior_uses_jumps() -> None:
    gaz = FakeGazetteer(jumps_map={(2, 3): 1})
    boosted = resolve("kisago", gaz, PriorContext(active_systems=(3,)), CFG)
    plain = resolve("kisago", gaz, NO_PRIORS, CFG)
    assert boosted.best is not None and plain.best is not None
    assert boosted.best.score > plain.best.score


def test_reporter_history_prior() -> None:
    boosted = resolve("kisago", GAZ, PriorContext(reporter_counts={2: 6}), CFG)
    plain = resolve("kisago", GAZ, NO_PRIORS, CFG)
    assert boosted.best is not None and plain.best is not None
    assert boosted.best.score > plain.best.score


def test_home_bias_prior() -> None:
    boosted = resolve("otanoomi", GAZ, PriorContext(home_system_id=1), CFG)
    plain = resolve("otanoomi", GAZ, NO_PRIORS, CFG)
    assert boosted.best is not None and plain.best is not None
    assert boosted.best.name == "Otanuomi"
    assert boosted.best.score > plain.best.score


def test_scores_clamped_to_one() -> None:
    r = resolve(
        "Kisogo",
        GAZ,
        PriorContext(recency_min={2: 0.0}, reporter_counts={2: 10}, home_system_id=2),
        CFG,
    )
    assert r.best is not None
    assert r.best.score <= 1.0


# ── alias table (GDD §8.5) ───────────────────────────────────────────────────


@pytest.fixture
def alias_conn() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    db.migrate(conn)
    db.execute(
        conn,
        "INSERT INTO systems (id, name, region, constellation, metaphone)"
        " VALUES (1, 'Otanuomi', 'Kisogo-region', NULL, 'ATNM')",
    )
    db.execute(
        conn,
        "INSERT INTO aliases (raw_text, system_id, weight, learned_at, corrected_by)"
        " VALUES ('ockthanoome', 1, 1.0, ?, 42)",
        (datetime.now(UTC).isoformat(),),
    )
    return conn


def test_alias_lookup_wins_before_phonetics(alias_conn: sqlite3.Connection) -> None:
    r = resolve("  OckThanoome ", GAZ, NO_PRIORS, CFG, conn=alias_conn)
    assert r.tier is Tier.HIGH
    assert r.best is not None
    assert r.best.system_id == 1
    assert r.best.score == 1.0


def test_alias_miss_falls_through(alias_conn: sqlite3.Connection) -> None:
    r = resolve("kisogo", GAZ, NO_PRIORS, CFG, conn=alias_conn)
    assert r.best is not None
    assert r.best.name == "Kisogo"


def test_alias_to_pruned_system_is_skipped(alias_conn: sqlite3.Connection) -> None:
    db.execute(
        alias_conn,
        "INSERT INTO systems (id, name, region, constellation, metaphone)"
        " VALUES (99, 'Faraway', 'Elsewhere', NULL, 'FRW')",
    )
    db.execute(
        alias_conn,
        "INSERT INTO aliases (raw_text, system_id, weight, learned_at, corrected_by)"
        " VALUES ('far away', 99, 5.0, ?, 42)",
        (datetime.now(UTC).isoformat(),),
    )
    # id 99 is not in the (fake) active gazetteer → phonetic path decides.
    r = resolve("far away", GAZ, NO_PRIORS, CFG, conn=alias_conn)
    assert r.best is None or r.best.system_id != 99


def test_no_conn_skips_alias_lookup() -> None:
    r = phonetics.resolve("ockthanoome", GAZ, NO_PRIORS, CFG, conn=None)
    # Without the alias table this is just a phonetic match attempt.
    assert isinstance(r.tier, Tier)
