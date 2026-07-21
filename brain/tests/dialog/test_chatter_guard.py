"""Chatter guard on the report path (GDD §6.8 + §8.3).

The live defect: with conversation mode running, a wake followed by ordinary
chit-chat trips a loose report keyword (a bare colour word like "red") AND a
phonetic scrap that resolves weakly, posting a junk "unconfirmed" CODE ORANGE
card. The exact failing transcript from the live log is reproduced here through
the REAL grammar + phonetic resolver, and the engine guard is proven to drop it
as chatter while a genuine callout ("hostiles in Jita", HIGH tier) still posts.

Transcript-only (constraint 5): no audio anywhere near these tests.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cortana.config import ConversationConfig
from cortana.dialog.engine import DialogEngine
from cortana.dialog.types import DialogSession, DialogState, Report
from cortana.nlu import grammar, phonetics
from cortana.types import IncidentOutcome, Intent, MatchCandidate, Outcome, Resolution, Tier

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_phonetics import CFG, NO_PRIORS, FakeGazetteer  # noqa: E402

# The exact transcript from the live log (Bug report): casual conversation that
# tripped HOSTILE_SPOTTED via the bare colour word "red" and resolved a scrap to
# a real system, posting a CODE ORANGE "unconfirmed" card.
FAILING_TRANSCRIPT = (
    "Of course. You, you, why, why? You got to pick and choose a color. "
    "Between blue and red, which do you choose?"
)

# A gazetteer with enough near-homophones of OU-AIT that a junk scrap lands
# uncertain (MEDIUM), mirroring the large live map — a tiny map would give the
# scrap an artificially clean HIGH match with no competitors.
CHATTER_NAMES = [
    "Otanuomi",
    "Kisogo",
    "Jita",
    "Amarr",
    "Rens",
    "Dodixie",
    "O-R5TS",
    "OU-AIT",
    "OU-A2P",
    "OUTA-4",
    "U-QVWD",
    "Dital",
]


def _resolve(text: str, names: list[str]) -> Resolution:
    gaz = FakeGazetteer(names=names)
    return phonetics.resolve(text, gaz, NO_PRIORS, CFG, None)


def test_failing_transcript_matches_report_but_resolves_sub_high() -> None:
    """Reproduce the root cause with the real code: the sentence DOES claim a
    report intent (the bare word "red"), and its junk system window does NOT
    resolve to a confident (HIGH) system — so the chatter guard's precondition
    holds."""
    parsed = grammar.parse(FAILING_TRANSCRIPT)
    assert parsed is not None
    assert parsed.intent is Intent.HOSTILE_SPOTTED  # tripped by the bare "red"
    assert parsed.system_text  # a phonetic scrap window
    res = _resolve(parsed.system_text, CHATTER_NAMES)
    assert res.tier is not Tier.HIGH  # not a confident callout — chatter


def test_real_callouts_resolve_high() -> None:
    """The callouts that MUST still work resolve HIGH, so the guard never touches
    them regardless of conversation state."""
    for transcript, expect_intent in [
        ("hostiles in Jita", Intent.HOSTILE_SPOTTED),
        ("under attack in Otanuomi", Intent.UNDER_ATTACK),
        ("enemy spotted O-R5TS", Intent.HOSTILE_SPOTTED),
    ]:
        parsed = grammar.parse(transcript)
        assert parsed is not None and parsed.intent is expect_intent
        assert parsed.system_text
        res = _resolve(parsed.system_text, CHATTER_NAMES)
        assert res.tier is Tier.HIGH, transcript


# ── the engine guard ─────────────────────────────────────────────────────────


def _conv(enabled: bool) -> ConversationConfig:
    return ConversationConfig(enabled=enabled, backend="local", local_url="http://x")


def _fake_conversation(*, ops_in_progress: bool = False):
    # ops_quiet() returns True when ops ARE in progress (banter must go silent);
    # False here means quiet → conversation available.
    return SimpleNamespace(
        backend_live=lambda: True,
        ops_quiet=lambda gid: ops_in_progress,
        active=lambda uid: False,
        reset_user=lambda uid: None,
    )


def _holder(conv: ConversationConfig) -> SimpleNamespace:
    return SimpleNamespace(
        current=SimpleNamespace(
            conversation=conv,
            discord=SimpleNamespace(
                mentions_enabled=False,
                channels=SimpleNamespace(intel_live=111),
            ),
            areas=SimpleNamespace(learn=False),
            dialog=SimpleNamespace(confirm_reports="off"),
            matching=SimpleNamespace(),  # unused: phonetics.resolve is patched
        )
    )


def _engine(conv: ConversationConfig, incidents, *, conversation) -> DialogEngine:
    health = Mock(degraded=False)
    speaker = Mock(say=AsyncMock(return_value=True), chirp=AsyncMock(return_value=None))
    discipline = Mock(may_mention=Mock(return_value=True))
    return DialogEngine(
        _holder(conv),  # type: ignore[arg-type]
        capture=None,
        transcriber=Mock(),
        speaker=speaker,
        incidents=incidents,
        discipline=discipline,
        gazetteer=Mock(),
        conn=Mock(),
        health=health,
        chat_provider=lambda: (None, "disabled"),
        member_role_ids=lambda uid: [],
        send_channel=AsyncMock(return_value=True),
        shutdown=Mock(),
        conversation=conversation,
    )


def _session() -> DialogSession:
    return DialogSession(user_id=1, guild_id=9, state=DialogState.IDLE, gen=2)


def _medium() -> Resolution:
    return Resolution(
        tier=Tier.MEDIUM,
        candidates=(
            MatchCandidate(8, "OU-AIT", 0.72),
            MatchCandidate(9, "OU-A2P", 0.66),
        ),
    )


def _incidents_mock() -> Mock:
    m = Mock()
    m.build_prior_context = Mock(return_value=NO_PRIORS)
    m.report = AsyncMock(return_value=IncidentOutcome(Outcome.POSTED, "posted.", None, 1))
    return m


async def test_chatter_dropped_when_conversation_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact failing transcript, resolved MEDIUM, does NOT open an incident
    while conversation mode is available — it is dropped as chatter."""
    monkeypatch.setattr(phonetics, "resolve", lambda *a, **k: _medium())
    incidents = _incidents_mock()
    engine = _engine(_conv(enabled=True), incidents, conversation=_fake_conversation())

    parsed = grammar.parse(FAILING_TRANSCRIPT)
    assert parsed is not None
    await engine._report(_session(), Report(parsed))

    incidents.report.assert_not_called()  # no card opened — dropped
    engine._health.record_rejected.assert_called()


async def test_high_callout_still_posts_with_conversation_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confident (HIGH) callout is never touched by the guard, even while
    conversation mode is available."""
    high = Resolution(tier=Tier.HIGH, candidates=(MatchCandidate(3, "Jita", 1.0),))
    monkeypatch.setattr(phonetics, "resolve", lambda *a, **k: high)
    incidents = _incidents_mock()
    engine = _engine(_conv(enabled=True), incidents, conversation=_fake_conversation())

    parsed = grammar.parse("hostiles in Jita")
    assert parsed is not None
    await engine._report(_session(), Report(parsed))

    incidents.report.assert_awaited_once()  # the callout posted


async def test_medium_still_posts_when_conversation_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard is off by default: with conversation disabled, a MEDIUM report
    reaches the engine exactly as before (byte-for-byte the old path)."""
    monkeypatch.setattr(phonetics, "resolve", lambda *a, **k: _medium())
    incidents = _incidents_mock()
    engine = _engine(_conv(enabled=False), incidents, conversation=_fake_conversation())

    parsed = grammar.parse(FAILING_TRANSCRIPT)
    assert parsed is not None
    await engine._report(_session(), Report(parsed))

    incidents.report.assert_awaited_once()  # unchanged when conversation is off


async def test_guard_relaxes_during_active_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    """During real ops (ops NOT quiet) conversation is unavailable, so an
    uncertain report still opens a card — the guard bites only during idle
    banter, exactly when chatter (not callouts) dominates."""
    monkeypatch.setattr(phonetics, "resolve", lambda *a, **k: _medium())
    incidents = _incidents_mock()
    engine = _engine(
        _conv(enabled=True), incidents, conversation=_fake_conversation(ops_in_progress=True)
    )

    parsed = grammar.parse(FAILING_TRANSCRIPT)
    assert parsed is not None
    await engine._report(_session(), Report(parsed))

    incidents.report.assert_awaited_once()  # ops live → guard relaxed
