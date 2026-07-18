"""FunEngine tests — GDD §13.2. Pure logic: shuffle bags, cooldowns, filters,
target/topic extraction, and the shipped-library integrity gate."""

from __future__ import annotations

import dataclasses
import json
import random
from pathlib import Path

import pytest

from cortana.config import FunConfig
from cortana.core.fun import (
    CATEGORIES,
    FunCooldown,
    FunEngine,
    FunUnavailable,
    extract_target,
    load_library,
    match_category,
)
from tests.test_incidents import StubHolder, make_config

GUILD = 5555


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


def _write_library(
    base: Path,
    *,
    astronomy: list[str] | None = None,
    insults: list[dict] | None = None,
) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    if astronomy is not None:
        (base / "astronomy.json").write_text(json.dumps(astronomy), encoding="utf-8")
    if insults is not None:
        (base / "insults_test.json").write_text(json.dumps(insults), encoding="utf-8")
    return base


def _engine(
    base: Path,
    *,
    fact_cooldown_s: int = 0,
    insult_cooldown_s: int = 0,
    insults_spicy: bool = True,
    clock: _Clock | None = None,
) -> FunEngine:
    cfg = dataclasses.replace(
        make_config(),
        fun=FunConfig(
            fact_cooldown_s=fact_cooldown_s,
            insult_cooldown_s=insult_cooldown_s,
            insults_spicy=insults_spicy,
        ),
    )
    return FunEngine(
        StubHolder(cfg),  # type: ignore[arg-type]
        load_library(base),
        clock=clock if clock is not None else _Clock(),
        rng=random.Random(42),
    )


FACTS = [f"Space fact number {i} with enough length to pass the gate." for i in range(5)]
INSULTS = [
    {"text": "You fly like a shuttle with no capacitor.", "spicy": False},
    {"text": "You're a damn menace to your own fleet.", "spicy": True},
]


# ── loading ──────────────────────────────────────────────────────────────────


def test_load_library_reads_facts_and_insults(tmp_path: Path) -> None:
    lib = load_library(_write_library(tmp_path, astronomy=FACTS, insults=INSULTS))
    assert lib.facts["astronomy"] == tuple(FACTS)
    assert len(lib.insults) == 2
    assert lib.insults[1].spicy is True


def test_load_library_skips_junk(tmp_path: Path) -> None:
    base = _write_library(tmp_path, astronomy=FACTS)
    (base / "notacategory.json").write_text(json.dumps(["orphan fact, long enough to keep"]))
    (base / "broken.json").write_text("{not json")
    (base / "physics.json").write_text(json.dumps("not a list"))
    overlong = "x" * 500
    (base / "history.json").write_text(json.dumps([overlong, "kept: a short real line here"]))
    lib = load_library(base)
    assert set(lib.facts) == {"astronomy", "history"}
    assert lib.facts["history"] == ("kept: a short real line here",)


def test_load_library_missing_dir_is_empty(tmp_path: Path) -> None:
    lib = load_library(tmp_path / "nope")
    assert lib.facts == {} and lib.insults == ()


def test_empty_library_raises_unavailable(tmp_path: Path) -> None:
    eng = _engine(tmp_path / "nope")
    with pytest.raises(FunUnavailable):
        eng.next_fact(GUILD)
    with pytest.raises(FunUnavailable):
        eng.next_insult(GUILD)


# ── topic / target extraction ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, None),
        ("tell me a", None),
        ("space", "astronomy"),
        ("give me a fact about the deep sea", "ocean"),
        ("animal", "animals"),
        ("something about quantum stuff", "physics"),
        ("eve lore", "eve"),
        ("astronomy", "astronomy"),
    ],
)
def test_match_category(text: str | None, expected: str | None) -> None:
    assert match_category(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, None),
        ("this guy", None),
        ("him", None),
        ("dave", "Dave"),
        ("my buddy steve", "Steve"),
        ("@dave`*", "Dave"),
        ("the entire opposing fleet and their alliance too", None),
    ],
)
def test_extract_target(text: str | None, expected: str | None) -> None:
    assert extract_target(text) == expected


# ── dealing ──────────────────────────────────────────────────────────────────


def test_shuffle_bag_deals_whole_deck_before_repeat(tmp_path: Path) -> None:
    eng = _engine(_write_library(tmp_path, astronomy=FACTS))
    first_deck = {eng.next_fact(GUILD).text for _ in range(len(FACTS))}
    assert first_deck == set(FACTS)  # every fact exactly once per cycle


def test_refill_never_repeats_back_to_back(tmp_path: Path) -> None:
    eng = _engine(_write_library(tmp_path, astronomy=FACTS))
    served = [eng.next_fact(GUILD).text for _ in range(len(FACTS) * 4)]
    assert all(a != b for a, b in zip(served, served[1:], strict=False))


def test_category_request_scopes_the_deal(tmp_path: Path) -> None:
    base = _write_library(tmp_path, astronomy=FACTS)
    (base / "ocean.json").write_text(json.dumps(["The ocean is deep, verified and quite dark."]))
    eng = _engine(base)
    line = eng.next_fact(GUILD, "give me an ocean fact")
    assert line.category_key == "ocean"
    assert line.category_title == CATEGORIES["ocean"][0]


def test_unshipped_category_falls_back_to_any(tmp_path: Path) -> None:
    eng = _engine(_write_library(tmp_path, astronomy=FACTS))
    line = eng.next_fact(GUILD, "eve lore")  # known alias, no eve.json shipped
    assert line.category_key == "astronomy"


# ── cooldowns ────────────────────────────────────────────────────────────────


def test_fact_cooldown_throttles_and_recovers(tmp_path: Path) -> None:
    clock = _Clock()
    eng = _engine(_write_library(tmp_path, astronomy=FACTS), fact_cooldown_s=10, clock=clock)
    eng.next_fact(GUILD)
    with pytest.raises(FunCooldown) as exc:
        eng.next_fact(GUILD)
    assert 9.0 < exc.value.remaining_s <= 10.0
    assert eng.fact_cooldown_remaining(GUILD) > 0
    clock.advance(10.1)
    eng.next_fact(GUILD)  # recovered


def test_cooldowns_are_per_guild_and_per_kind(tmp_path: Path) -> None:
    clock = _Clock()
    eng = _engine(
        _write_library(tmp_path, astronomy=FACTS, insults=INSULTS),
        fact_cooldown_s=10,
        insult_cooldown_s=10,
        clock=clock,
    )
    eng.next_fact(GUILD)
    eng.next_insult(GUILD)  # separate throttle — not blocked by the fact
    eng.next_fact(GUILD + 1)  # separate guild — not blocked either


# ── insults ──────────────────────────────────────────────────────────────────


def test_spicy_filter_serves_clean_lines_only(tmp_path: Path) -> None:
    eng = _engine(_write_library(tmp_path, insults=INSULTS), insults_spicy=False)
    served = {eng.next_insult(GUILD) for _ in range(6)}
    assert served == {INSULTS[0]["text"]}


def test_insult_target_prefix(tmp_path: Path) -> None:
    eng = _engine(_write_library(tmp_path, insults=INSULTS[:1]))
    assert eng.next_insult(GUILD, "my buddy dave").startswith("Dave. ")
    line = eng.next_insult(GUILD, "this guy")  # pronouns → untargeted
    assert line == INSULTS[0]["text"]


# ── the shipped library (integrity gate for the bundled JSON) ────────────────


def test_shipped_library_is_huge_and_well_formed() -> None:
    lib = load_library()
    assert len(lib.facts) >= 14, "most categories must ship"
    assert set(lib.facts) <= set(CATEGORIES)
    assert lib.fact_count >= 1000, "the library is supposed to be huge"
    assert len(lib.insults) >= 150
    assert any(i.spicy for i in lib.insults) and any(not i.spicy for i in lib.insults)
    for lines in lib.facts.values():
        assert all(20 <= len(t) <= 220 for t in lines)
        assert len(set(lines)) == len(lines)  # no in-category dupes
