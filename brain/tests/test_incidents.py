"""Incident engine tests — GDD §9/§13. In-memory sqlite, FakePoster, injected clock."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aura.config import (
    AuraConfig,
    CaptureConfig,
    ChannelsConfig,
    CircuitBreakerConfig,
    DatabaseConfig,
    DisciplineConfig,
    DiscordConfig,
    GazetteerConfig,
    HealthConfig,
    IncidentsConfig,
    IpcConfig,
    MatchingConfig,
    PriorsConfig,
    RolesConfig,
    SttConfig,
    TiersConfig,
    TtsConfig,
    WakeConfig,
)
from aura.core import db
from aura.core.discipline import Discipline
from aura.core.incidents import IncidentEngine, parse_duration
from aura.nlu import phonetics
from aura.types import (
    AlertChannel,
    CardRender,
    Intent,
    MatchCandidate,
    Outcome,
    ParsedCommand,
    PriorContext,
    Resolution,
    ResponderState,
    SystemEntry,
    Tier,
)

GUILD = 1
PILOT_ROLE = 111
FC_ROLE = 222
HD_ROLE = 801
MINERS_ROLE = 802
ROLE_IDS = {"@Home-Defense": HD_ROLE, "@Miners": MINERS_ROLE}

T0 = datetime(2026, 7, 17, 12, 0, 0, tzinfo=UTC)

RULES_YAML = """\
rules:
  - role: "@Home-Defense"
    types: [UNDER_ATTACK, ASSIST_REQUEST, HOSTILE_SPOTTED]
    scope: {}
    escalate_at: UNDER_ATTACK
  - role: "@Miners"
    types: [HOSTILE_SPOTTED, GATE_CAMP]
    scope:
      systems: [Otanuomi, Kisogo]
    escalate_at: never
group_aliases:
  miners: "@Miners"
  defense: "@Home-Defense"
"""

SYSTEMS = [
    (1, "Otanuomi", "Kisogo-region"),
    (2, "Kisogo", "Kisogo-region"),
    (3, "Alenia", "Lowsec-North"),
    (4, "Hulmate", "Kisogo-region"),
]


def make_config(
    *,
    user_cooldown_s: int = 30,
    max_mentions: int = 12,
    window_min: int = 10,
    dedupe_window_s: int = 90,
    stale_after_min: int = 20,
    cancel_window_s: int = 30,
    personal_pings_max: int = 10,
    mentions_enabled: bool = True,
) -> AuraConfig:
    return AuraConfig(
        discord=DiscordConfig(
            token_file="/dev/null",
            guild_id=GUILD,
            channels=ChannelsConfig(intel_alerts=10, intel_live=11, health=12),
            roles=RolesConfig(pilot=PILOT_ROLE, fc=FC_ROLE),
            watch_voice_channels=(9,),
            auto_join=True,
            mentions_enabled=mentions_enabled,
        ),
        wake=WakeConfig(model="wake.onnx", threshold=0.55, refractory_ms=2000),
        capture=CaptureConfig(
            preroll_ms=300, endpoint_silence_ms=400, max_utterance_ms=6000, vad_aggressiveness=2
        ),
        stt=SttConfig(
            backend="faster-whisper",
            model="small",
            compute_type="int8",
            cpu_threads=2,
            bias_with_gazetteer=True,
            whisper_cpp_url="http://127.0.0.1:8080/inference",
        ),
        matching=MatchingConfig(
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
        ),
        incidents=IncidentsConfig(
            dedupe_window_s=dedupe_window_s,
            stale_after_min=stale_after_min,
            cancel_window_s=cancel_window_s,
        ),
        discipline=DisciplineConfig(
            user_cooldown_s=user_cooldown_s,
            circuit_breaker=CircuitBreakerConfig(max_mentions=max_mentions, window_min=window_min),
            personal_pings_max=personal_pings_max,
        ),
        tts=TtsConfig(
            enabled=True,
            voice="voice.onnx",
            binary="/usr/local/bin/piper",
            max_utterance_s=3.0,
        ),
        gazetteer=GazetteerConfig(file="gazetteer.yaml", home_system="Otanuomi"),
        ipc=IpcConfig(socket="/run/aura/aura.sock"),
        health=HealthConfig(report_interval_min=60, voice_silence_alarm_s=60),
        database=DatabaseConfig(path=":memory:"),
    )


class StubHolder:
    """Duck-typed ConfigHolder: fixed AuraConfig, no YAML file needed."""

    def __init__(self, cfg: AuraConfig) -> None:
        self.current = cfg


class Clock:
    """Deterministic injected clock — the engine reads time only through this."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class FakeGazetteer:
    entries: dict[int, SystemEntry]
    home: int | None = 1
    jumps_map: dict[tuple[int, int], int] = field(default_factory=dict)

    @property
    def systems(self) -> tuple[SystemEntry, ...]:
        return tuple(self.entries.values())

    def by_id(self, system_id: int) -> SystemEntry | None:
        return self.entries.get(system_id)

    def by_name(self, name: str) -> SystemEntry | None:
        for entry in self.entries.values():
            if entry.name.lower() == name.lower():
                return entry
        return None

    def jumps(self, a_id: int, b_id: int) -> int | None:
        if a_id == b_id:
            return 0
        return self.jumps_map.get((a_id, b_id), self.jumps_map.get((b_id, a_id)))

    @property
    def home_system_id(self) -> int | None:
        return self.home


class FakePoster:
    def __init__(self) -> None:
        self.posts: list[tuple[int, AlertChannel, str, CardRender]] = []
        self.edits: list[tuple[int, int, str, CardRender]] = []
        self._msg = 5000

    async def post(
        self, guild_id: int, channel: AlertChannel, content: str, card: CardRender
    ) -> tuple[int, int]:
        self._msg += 1
        self.posts.append((guild_id, channel, content, card))
        return (900, self._msg)

    async def edit(self, channel_id: int, message_id: int, content: str, card: CardRender) -> None:
        self.edits.append((channel_id, message_id, content, card))


@dataclass
class Env:
    engine: IncidentEngine
    poster: FakePoster
    conn: sqlite3.Connection
    clock: Clock
    discipline: Discipline


@pytest.fixture()
def make_env(tmp_path: Path) -> Callable[..., Env]:
    def _make(on_mention: Callable[[], None] | None = None, **cfg_overrides: int) -> Env:
        conn = db.connect(":memory:")
        db.migrate(conn)
        db.executemany(
            conn,
            "INSERT INTO systems (id, name, region, metaphone) VALUES (?, ?, ?, ?)",
            [(sid, name, region, name.upper()) for sid, name, region in SYSTEMS],
        )
        gazetteer = FakeGazetteer(
            entries={
                sid: SystemEntry(
                    id=sid, name=name, region=region, constellation=None, metaphone=name.upper()
                )
                for sid, name, region in SYSTEMS
            }
        )
        holder = StubHolder(make_config(**cfg_overrides))
        discipline = Discipline(holder)  # type: ignore[arg-type]
        poster = FakePoster()
        rules_path = tmp_path / "routing.yaml"
        rules_path.write_text(RULES_YAML, encoding="utf-8")
        engine = IncidentEngine(
            conn,
            holder,  # type: ignore[arg-type]
            gazetteer,  # type: ignore[arg-type]
            discipline,
            poster,
            rules_path,
            on_mention=on_mention,
        )
        engine.load_routing_rules(ROLE_IDS.get)
        clock = Clock(T0)
        engine._clock = clock
        return Env(engine=engine, poster=poster, conn=conn, clock=clock, discipline=discipline)

    return _make


# ── small builders / assertion helpers ───────────────────────────────────────


def cmd(
    intent: Intent,
    detail: str | None = None,
    alias: str | None = None,
    raw: str = "synthetic transcript",
) -> ParsedCommand:
    return ParsedCommand(intent=intent, system_text=None, group_alias=alias, detail=detail, raw=raw)


def high(system_id: int, name: str, score: float = 0.92) -> Resolution:
    return Resolution(tier=Tier.HIGH, candidates=(MatchCandidate(system_id, name, score),))


def medium() -> Resolution:
    return Resolution(
        tier=Tier.MEDIUM,
        candidates=(
            MatchCandidate(1, "Otanuomi", 0.62),
            MatchCandidate(2, "Kisogo", 0.58),
            MatchCandidate(3, "Alenia", 0.40),
        ),
    )


def field_value(card: CardRender, name: str) -> str:
    return next(f["value"] for f in card.embed["fields"] if f["name"] == name)


def footer_text(card: CardRender) -> str:
    return card.embed["footer"]["text"]


def custom_ids(card: CardRender) -> list[str]:
    return [b.custom_id for b in card.buttons]


# ── posting, severity, mentions, command_log ─────────────────────────────────


async def test_high_tier_report_posts_and_mentions(make_env: Callable[..., Env]) -> None:
    env = make_env()
    out = await env.engine.report(
        GUILD, 42, cmd(Intent.UNDER_ATTACK, detail="tackled on gate"), high(2, "Kisogo")
    )
    assert out.outcome is Outcome.POSTED
    assert out.incident_id is not None
    assert out.utterance == "Under attack Kisogo, pinged."
    assert len(env.poster.posts) == 1
    _, channel, content, card = env.poster.posts[0]
    assert channel is AlertChannel.ALERTS
    assert "@here" in content
    assert f"<@&{HD_ROLE}>" in content
    assert field_value(card, "System") == "Kisogo"
    row = db.query_one(env.conn, "SELECT * FROM incidents WHERE id = ?", (out.incident_id,))
    assert row is not None
    assert row["severity"] == "high"
    assert row["status"] == "ACTIVE"
    assert row["message_id"] is not None
    log_row = db.query_one(env.conn, "SELECT * FROM command_log")
    assert log_row is not None
    assert log_row["outcome"] == "POSTED"
    assert log_row["tier"] == "HIGH"
    assert log_row["matched_system_id"] == 2
    assert log_row["raw_transcript"] == "synthetic transcript"


async def test_sighting_mentions_without_here(make_env: Callable[..., Env]) -> None:
    env = make_env()
    out = await env.engine.report(GUILD, 42, cmd(Intent.HOSTILE_SPOTTED), high(1, "Otanuomi"))
    assert out.outcome is Outcome.POSTED
    _, channel, content, _ = env.poster.posts[0]
    assert channel is AlertChannel.ALERTS
    assert "@here" not in content
    assert f"<@&{HD_ROLE}>" in content
    assert f"<@&{MINERS_ROLE}>" in content


async def test_group_alias_restricts_mention(make_env: Callable[..., Env]) -> None:
    env = make_env()
    out = await env.engine.report(
        GUILD, 42, cmd(Intent.GATE_CAMP, alias="miners"), high(1, "Otanuomi")
    )
    _, _, content, _ = env.poster.posts[0]
    assert content == f"<@&{MINERS_ROLE}>"
    assert out.utterance == "Gate camp Otanuomi, pinged miners."


# ── dedupe folding (GDD §9.2) ────────────────────────────────────────────────


async def test_duplicate_within_window_folds_and_never_rementions(
    make_env: Callable[..., Env],
) -> None:
    env = make_env()
    out1 = await env.engine.report(GUILD, 42, cmd(Intent.HOSTILE_SPOTTED), high(1, "Otanuomi"))
    env.clock.advance(30)
    out2 = await env.engine.report(
        GUILD, 43, cmd(Intent.HOSTILE_SPOTTED, detail="three battleships"), high(1, "Otanuomi")
    )
    assert out2.outcome is Outcome.FOLDED
    assert out2.incident_id == out1.incident_id
    assert out2.utterance == "Added to Otanuomi, reported by 2."
    assert len(env.poster.posts) == 1  # ONE incident, ONE message (constraint 9)
    assert len(env.poster.edits) == 1
    _, _, edit_content, edit_card = env.poster.edits[0]
    assert edit_content == ""  # no re-mention on fold
    assert field_value(edit_card, "Reported by") == "2"
    rows = db.query(env.conn, "SELECT outcome FROM command_log ORDER BY id")
    assert [r["outcome"] for r in rows] == ["POSTED", "FOLDED"]


async def test_same_reporter_folds_but_count_stays_distinct(
    make_env: Callable[..., Env],
) -> None:
    env = make_env()
    await env.engine.report(GUILD, 42, cmd(Intent.HOSTILE_SPOTTED), high(1, "Otanuomi"))
    env.clock.advance(10)
    out = await env.engine.report(GUILD, 42, cmd(Intent.HOSTILE_SPOTTED), high(1, "Otanuomi"))
    assert out.outcome is Outcome.FOLDED
    _, _, _, card = env.poster.edits[-1]
    assert field_value(card, "Reported by") == "1"  # distinct reporters only


async def test_duplicate_outside_window_opens_new_incident(
    make_env: Callable[..., Env],
) -> None:
    env = make_env()
    out1 = await env.engine.report(GUILD, 42, cmd(Intent.HOSTILE_SPOTTED), high(1, "Otanuomi"))
    env.clock.advance(91)  # dedupe_window_s = 90
    out2 = await env.engine.report(GUILD, 42, cmd(Intent.HOSTILE_SPOTTED), high(1, "Otanuomi"))
    assert out2.outcome is Outcome.POSTED
    assert out2.incident_id != out1.incident_id
    assert len(env.poster.posts) == 2


async def test_different_type_same_system_is_not_a_duplicate(
    make_env: Callable[..., Env],
) -> None:
    env = make_env()
    await env.engine.report(GUILD, 42, cmd(Intent.HOSTILE_SPOTTED), high(1, "Otanuomi"))
    env.clock.advance(5)
    out = await env.engine.report(GUILD, 43, cmd(Intent.GATE_CAMP), high(1, "Otanuomi"))
    assert out.outcome is Outcome.POSTED
    assert len(env.poster.posts) == 2


# ── confidence tiers (GDD §8.3) ──────────────────────────────────────────────


async def test_medium_tier_posts_flagged_uncertain_with_candidates(
    make_env: Callable[..., Env],
) -> None:
    env = make_env()
    out = await env.engine.report(GUILD, 42, cmd(Intent.HOSTILE_SPOTTED), medium())
    assert out.outcome is Outcome.ASKED
    assert out.utterance == "Hostiles Otanuomi — say again to confirm."
    assert len(env.poster.posts) == 1  # posted anyway: speed beats certainty
    card = env.poster.posts[0][3]
    ids = custom_ids(card)
    inc = out.incident_id
    assert f"aura:inc:{inc}:pick:1" in ids
    assert f"aura:inc:{inc}:pick:2" in ids
    assert f"aura:inc:{inc}:pick:3" in ids
    assert f"aura:inc:{inc}:fix" in ids
    assert f"aura:inc:{inc}:otw" in ids
    assert "unconfirmed" in card.embed["description"]
    log_row = db.query_one(env.conn, "SELECT * FROM command_log")
    assert log_row["outcome"] == "ASKED"
    assert log_row["tier"] == "MEDIUM"


async def test_report_posts_catch_all_when_location_unmatched(
    make_env: Callable[..., Env],
) -> None:
    # GDD §8.6 catch-all: a report whose location does not match the gazetteer
    # still posts, with the spoken location on the card verbatim — never dropped.
    env = make_env()
    spoken = ParsedCommand(
        intent=Intent.HOSTILE_SPOTTED,
        system_text="UMI",
        group_alias=None,
        detail="three battleships",
        raw="hostiles UMI three battleships",
    )
    low = Resolution(tier=Tier.LOW, candidates=(MatchCandidate(1, "Otanuomi", 0.3),))
    out = await env.engine.report(GUILD, 42, spoken, low)
    assert out.outcome is Outcome.POSTED
    assert out.incident_id is not None
    assert len(env.poster.posts) == 1
    _, _, _, card = env.poster.posts[-1]
    assert "UMI" in card.embed["title"]  # the raw spoken location, shown as-is
    row = db.query_one(
        env.conn,
        "SELECT system_id, raw_system_text FROM incidents WHERE id = ?",
        (out.incident_id,),
    )
    assert row["system_id"] is None  # unmatched — no gazetteer id
    assert row["raw_system_text"] == "UMI"


async def test_report_posts_even_with_no_location(make_env: Callable[..., Env]) -> None:
    # No usable location at all → still an alert (generic), never a rejection.
    env = make_env()
    out = await env.engine.report(GUILD, 42, cmd(Intent.UNDER_ATTACK), None)
    assert out.outcome is Outcome.POSTED
    assert len(env.poster.posts) == 1


async def test_non_report_intent_still_rejects_on_low(make_env: Callable[..., Env]) -> None:
    # clear/timer/form up act on a specific system, so they still need a match.
    env = make_env()
    out = await env.engine.report(GUILD, 42, cmd(Intent.RESOLVE), None)
    assert out.outcome is Outcome.REJECTED
    assert out.utterance == "Say again the system."
    assert env.poster.posts == []


# ── resolve / cancel ─────────────────────────────────────────────────────────


async def test_resolve_greys_card_in_place(make_env: Callable[..., Env]) -> None:
    env = make_env()
    out = await env.engine.report(GUILD, 42, cmd(Intent.UNDER_ATTACK), high(1, "Otanuomi"))
    env.clock.advance(120)
    resolved = await env.engine.report(GUILD, 43, cmd(Intent.RESOLVE), high(1, "Otanuomi"))
    assert resolved.outcome is Outcome.POSTED
    assert resolved.utterance == "Otanuomi clear."
    assert resolved.incident_id == out.incident_id
    assert len(env.poster.posts) == 1  # edit in place, never a second message
    _, _, _, card = env.poster.edits[-1]
    assert "Resolved" in footer_text(card)
    assert "✅" in card.embed["title"]
    assert card.buttons == ()
    row = db.query_one(env.conn, "SELECT status FROM incidents WHERE id = ?", (out.incident_id,))
    assert row["status"] == "RESOLVED"


async def test_resolve_without_active_incident_rejected(make_env: Callable[..., Env]) -> None:
    env = make_env()
    out = await env.engine.resolve_system(GUILD, 42, 2)
    assert out.outcome is Outcome.REJECTED
    assert out.utterance == "No active incident for Kisogo."


async def test_cancel_within_window(make_env: Callable[..., Env]) -> None:
    env = make_env()
    posted = await env.engine.report(GUILD, 7, cmd(Intent.UNDER_ATTACK), high(1, "Otanuomi"))
    env.clock.advance(10)
    out = await env.engine.report(GUILD, 7, cmd(Intent.CANCEL), None)
    assert out.outcome is Outcome.POSTED
    assert out.utterance == "Cancelled."
    assert out.incident_id == posted.incident_id
    _, _, _, card = env.poster.edits[-1]
    assert "Cancelled" in footer_text(card)
    row = db.query_one(env.conn, "SELECT status FROM incidents WHERE id = ?", (posted.incident_id,))
    assert row["status"] == "RESOLVED"


async def test_cancel_after_window_rejected(make_env: Callable[..., Env]) -> None:
    env = make_env()
    await env.engine.report(GUILD, 7, cmd(Intent.UNDER_ATTACK), high(1, "Otanuomi"))
    env.clock.advance(31)  # cancel_window_s = 30
    out = await env.engine.report(GUILD, 7, cmd(Intent.CANCEL), None)
    assert out.outcome is Outcome.REJECTED
    assert out.utterance == "Nothing to cancel."


async def test_cancel_only_kills_own_incident(make_env: Callable[..., Env]) -> None:
    env = make_env()
    await env.engine.report(GUILD, 7, cmd(Intent.UNDER_ATTACK), high(1, "Otanuomi"))
    out = await env.engine.cancel(GUILD, 99)  # someone else
    assert out.outcome is Outcome.REJECTED


# ── responder loop (GDD §9.3) ────────────────────────────────────────────────


async def test_responder_counts_and_otw_utterances(make_env: Callable[..., Env]) -> None:
    env = make_env()
    posted = await env.engine.report(GUILD, 42, cmd(Intent.UNDER_ATTACK), high(1, "Otanuomi"))
    inc = posted.incident_id
    assert inc is not None
    first = await env.engine.respond(inc, 100, ResponderState.OTW)
    assert first.utterance == "One responding to Otanuomi."
    second = await env.engine.respond(inc, 101, ResponderState.OTW)
    assert second.utterance == "Two responding to Otanuomi."
    watcher = await env.engine.respond(inc, 102, ResponderState.WATCHING)
    assert watcher.utterance is None  # spoken count only on OTW transitions
    repeat = await env.engine.respond(inc, 100, ResponderState.OTW)
    assert repeat.utterance is None  # no transition, no announcement
    _, _, _, card = env.poster.edits[-1]
    assert field_value(card, "Responders") == "🚀 2 · 👀 1 · ❌ 0"
    assert len(env.poster.posts) == 1  # all responder activity is edits


async def test_responder_state_change_updates_counts(make_env: Callable[..., Env]) -> None:
    env = make_env()
    posted = await env.engine.report(GUILD, 42, cmd(Intent.UNDER_ATTACK), high(1, "Otanuomi"))
    inc = posted.incident_id
    await env.engine.respond(inc, 100, ResponderState.OTW)
    changed = await env.engine.respond(inc, 100, ResponderState.NO)
    assert changed.utterance is None
    _, _, _, card = env.poster.edits[-1]
    assert field_value(card, "Responders") == "🚀 0 · 👀 0 · ❌ 1"
    back = await env.engine.respond(inc, 100, ResponderState.OTW)
    assert back.utterance == "One responding to Otanuomi."


async def test_respond_on_resolved_incident_rejected(make_env: Callable[..., Env]) -> None:
    env = make_env()
    posted = await env.engine.report(GUILD, 42, cmd(Intent.UNDER_ATTACK), high(1, "Otanuomi"))
    await env.engine.resolve_system(GUILD, 42, 1)
    out = await env.engine.respond(posted.incident_id, 100, ResponderState.OTW)
    assert out.outcome is Outcome.REJECTED


# ── staleness sweep (GDD §9.1) ───────────────────────────────────────────────


async def test_stale_sweep_marks_silently(make_env: Callable[..., Env]) -> None:
    env = make_env()
    old = await env.engine.report(GUILD, 42, cmd(Intent.HOSTILE_SPOTTED), high(1, "Otanuomi"))
    env.clock.advance(19 * 60)
    fresh = await env.engine.report(GUILD, 43, cmd(Intent.GATE_CAMP), high(2, "Kisogo"))
    env.clock.advance(2 * 60)  # old is now 21 min silent; fresh only 2
    posts_before = len(env.poster.posts)
    stale_ids = await env.engine.sweep_stale()
    assert stale_ids == [old.incident_id]
    assert len(env.poster.posts) == posts_before  # silent: edits only, no posts
    _, _, content, card = env.poster.edits[-1]
    assert content == ""
    assert "Stale" in footer_text(card)
    row = db.query_one(env.conn, "SELECT status FROM incidents WHERE id = ?", (old.incident_id,))
    assert row["status"] == "STALE"
    row = db.query_one(env.conn, "SELECT status FROM incidents WHERE id = ?", (fresh.incident_id,))
    assert row["status"] == "ACTIVE"
    assert await env.engine.sweep_stale() == []  # idempotent


# ── discipline integration: cooldown + circuit breaker ───────────────────────


async def test_user_cooldown_suppresses_mentions_but_still_posts(
    make_env: Callable[..., Env],
) -> None:
    env = make_env()
    await env.engine.report(GUILD, 42, cmd(Intent.UNDER_ATTACK), high(1, "Otanuomi"))
    env.clock.advance(10)  # inside the 30s cooldown
    out = await env.engine.report(GUILD, 42, cmd(Intent.UNDER_ATTACK), high(2, "Kisogo"))
    assert out.outcome is Outcome.POSTED  # incident still logged and posted
    _, channel, content, _ = env.poster.posts[-1]
    assert content == ""  # but no mention
    assert channel is AlertChannel.LIVE
    assert out.utterance == "Under attack Kisogo, posted."


async def test_circuit_breaker_suppresses_and_announces_once(
    make_env: Callable[..., Env],
) -> None:
    env = make_env(max_mentions=1, user_cooldown_s=1)
    r1 = await env.engine.report(GUILD, 1, cmd(Intent.UNDER_ATTACK), high(1, "Otanuomi"))
    env.clock.advance(2)
    r2 = await env.engine.report(GUILD, 2, cmd(Intent.UNDER_ATTACK), high(2, "Kisogo"))
    assert "@here" in env.poster.posts[0][2]
    assert "@here" in env.poster.posts[1][2]
    env.clock.advance(2)  # third report: 2 mentions > max of 1 → breaker open
    r3 = await env.engine.report(GUILD, 3, cmd(Intent.UNDER_ATTACK), high(3, "Alenia"))
    assert r3.outcome is Outcome.POSTED  # cards keep flowing
    _, channel, content, _ = env.poster.posts[2]
    assert channel is AlertChannel.LIVE
    assert "Flood control active" in content  # announced...
    assert r3.utterance == "Flood control active."
    env.clock.advance(2)
    r4 = await env.engine.report(GUILD, 4, cmd(Intent.UNDER_ATTACK), high(4, "Hulmate"))
    _, _, content4, _ = env.poster.posts[3]
    assert content4 == ""  # ...exactly once
    assert r4.utterance == "Under attack Hulmate, posted."
    assert all(r.incident_id is not None for r in (r1, r2, r3, r4))


# ── timers and form-ups (GDD §13) ────────────────────────────────────────────


def test_parse_duration_variants() -> None:
    assert parse_duration("four hours") == timedelta(hours=4)
    assert parse_duration("fifteen minutes") == timedelta(minutes=15)
    assert parse_duration("1h") == timedelta(hours=1)
    assert parse_duration("2 hours 30 minutes") == timedelta(hours=2, minutes=30)
    assert parse_duration("an hour") == timedelta(hours=1)
    assert parse_duration("half an hour") == timedelta(minutes=30)
    assert parse_duration("twenty five minutes") == timedelta(minutes=25)
    assert parse_duration("no duration here") is None
    assert parse_duration("") is None


async def test_timer_created_and_fires_when_due(make_env: Callable[..., Env]) -> None:
    env = make_env()
    out = await env.engine.report(
        GUILD, 42, cmd(Intent.TIMER, detail="four hours"), high(2, "Kisogo")
    )
    assert out.outcome is Outcome.POSTED
    assert out.utterance == "Timer Kisogo, four hours."  # §12.1 catalogue string
    assert env.poster.posts == []  # timers schedule a ping, no card
    row = db.query_one(env.conn, "SELECT * FROM timers")
    assert row is not None
    assert row["system_id"] == 2
    assert row["fired"] == 0
    assert datetime.fromisoformat(row["fires_at"]) == T0 + timedelta(hours=4)

    assert await env.engine.fire_due_timers(T0 + timedelta(hours=3)) == []  # not yet
    pings = await env.engine.fire_due_timers(T0 + timedelta(hours=4, seconds=1))
    assert len(pings) == 1
    assert pings[0].system_name == "Kisogo"
    assert pings[0].guild_id == GUILD
    assert pings[0].created_by == 42
    assert await env.engine.fire_due_timers(T0 + timedelta(hours=5)) == []  # fired once


async def test_timer_mixed_duration_spoken_in_words(make_env: Callable[..., Env]) -> None:
    env = make_env()
    out = await env.engine.report(
        GUILD, 42, cmd(Intent.TIMER, detail="90 minutes"), high(2, "Kisogo")
    )
    assert out.utterance == "Timer Kisogo, one hour thirty minutes."


async def test_timer_without_duration_rejected(make_env: Callable[..., Env]) -> None:
    env = make_env()
    out = await env.engine.report(
        GUILD, 42, cmd(Intent.TIMER, detail="soonish maybe"), high(2, "Kisogo")
    )
    assert out.outcome is Outcome.REJECTED
    assert out.utterance == "Say again the timer duration."
    assert db.query(env.conn, "SELECT * FROM timers") == []


async def test_formup_posts_op_card_with_rsvp(make_env: Callable[..., Env]) -> None:
    env = make_env()
    out = await env.engine.report(
        GUILD, 42, cmd(Intent.FORMUP, detail="fifteen minutes"), high(1, "Otanuomi")
    )
    assert out.outcome is Outcome.POSTED
    assert out.utterance == "Form up Otanuomi, fifteen minutes."
    assert out.incident_id is not None
    _, channel, content, card = env.poster.posts[0]
    assert channel is AlertChannel.LIVE
    assert content == ""  # form-ups never mention
    assert "Form-up" in card.embed["title"]
    assert f"aura:inc:{out.incident_id}:otw" in custom_ids(card)
    # RSVP goes through the same respond() loop as incidents.
    rsvp = await env.engine.respond(out.incident_id, 100, ResponderState.OTW)
    assert rsvp.utterance == "One responding to Otanuomi."
    # And the countdown ping is scheduled.
    pings = await env.engine.fire_due_timers(T0 + timedelta(minutes=16))
    assert len(pings) == 1
    assert pings[0].note == "Form-up Otanuomi"


# ── status query, correction, priors ─────────────────────────────────────────


async def test_query_status_speaks_summary(make_env: Callable[..., Env]) -> None:
    env = make_env()
    out = await env.engine.report(GUILD, 42, cmd(Intent.QUERY), None)
    assert out.outcome is Outcome.POSTED
    assert out.utterance == "All clear, no active incidents."
    await env.engine.report(GUILD, 42, cmd(Intent.UNDER_ATTACK), high(1, "Otanuomi"))
    out = await env.engine.report(GUILD, 42, cmd(Intent.QUERY), None)
    assert out.utterance == "One active incident: Otanuomi."


async def test_correct_system_updates_card_and_learns_alias(
    make_env: Callable[..., Env],
) -> None:
    env = make_env()
    posted = await env.engine.report(GUILD, 42, cmd(Intent.HOSTILE_SPOTTED), medium())
    assert posted.outcome is Outcome.ASKED
    out = await env.engine.correct_system(posted.incident_id, 55, 2, raw_text="oh tan you oh me")
    assert out.outcome is Outcome.POSTED
    assert out.utterance == "Corrected to Kisogo."
    row = db.query_one(
        env.conn,
        "SELECT system_id, system_confidence FROM incidents WHERE id = ?",
        (posted.incident_id,),
    )
    assert row["system_id"] == 2
    assert row["system_confidence"] == 1.0
    alias = db.query_one(
        env.conn, "SELECT * FROM aliases WHERE raw_text = ?", ("oh tan you oh me",)
    )
    assert alias is not None
    assert alias["system_id"] == 2
    assert alias["corrected_by"] == 55
    _, _, _, card = env.poster.edits[-1]
    assert field_value(card, "System") == "Kisogo"
    assert all(":pick:" not in cid for cid in custom_ids(card))  # confirmed now


async def test_button_correction_learns_alias_from_stored_transcript(
    make_env: Callable[..., Env],
) -> None:
    """§8.5 via the pick/fix buttons: raw_text="" falls back to the incident's
    stored raw_system_text, and the learned alias resolves at HIGH next time."""
    env = make_env()
    parsed = ParsedCommand(
        intent=Intent.HOSTILE_SPOTTED,
        system_text="oh tan you oh me",
        group_alias=None,
        detail=None,
        raw="hostiles oh tan you oh me",
    )
    posted = await env.engine.report(GUILD, 42, parsed, medium())
    assert posted.outcome is Outcome.ASKED
    row = db.query_one(
        env.conn, "SELECT raw_system_text FROM incidents WHERE id = ?", (posted.incident_id,)
    )
    assert row["raw_system_text"] == "oh tan you oh me"

    out = await env.engine.correct_system(posted.incident_id, 55, 2, raw_text="")
    assert out.outcome is Outcome.POSTED
    alias = db.query_one(
        env.conn, "SELECT * FROM aliases WHERE raw_text = ?", ("oh tan you oh me",)
    )
    assert alias is not None
    assert alias["system_id"] == 2
    assert alias["corrected_by"] == 55

    # The live resolve path now alias-hits at full confidence (HIGH tier).
    resolution = phonetics.resolve(
        "oh tan you oh me",
        env.engine._gazetteer,  # noqa: SLF001 — same fake the engine renders with
        PriorContext(),
        make_config().matching,
        env.conn,
    )
    assert resolution.tier is Tier.HIGH
    assert resolution.best is not None
    assert resolution.best.system_id == 2


async def test_button_correction_without_stored_transcript_writes_no_alias(
    make_env: Callable[..., Env],
) -> None:
    """A pre-migration incident (raw_system_text NULL) still gets its card
    corrected; no alias row is invented."""
    env = make_env()
    posted = await env.engine.report(GUILD, 42, cmd(Intent.HOSTILE_SPOTTED), medium())
    db.execute(
        env.conn,
        "UPDATE incidents SET raw_system_text = NULL WHERE id = ?",
        (posted.incident_id,),
    )
    out = await env.engine.correct_system(posted.incident_id, 55, 2, raw_text="")
    assert out.outcome is Outcome.POSTED
    row = db.query_one(
        env.conn, "SELECT system_id FROM incidents WHERE id = ?", (posted.incident_id,)
    )
    assert row["system_id"] == 2
    assert db.query(env.conn, "SELECT * FROM aliases") == []


async def test_explicit_raw_text_beats_stored_transcript(
    make_env: Callable[..., Env],
) -> None:
    env = make_env()
    parsed = ParsedCommand(
        intent=Intent.HOSTILE_SPOTTED,
        system_text="stored window",
        group_alias=None,
        detail=None,
        raw="hostiles stored window",
    )
    posted = await env.engine.report(GUILD, 42, parsed, medium())
    await env.engine.correct_system(posted.incident_id, 55, 2, raw_text="caller supplied")
    assert db.query_one(env.conn, "SELECT * FROM aliases WHERE raw_text = 'caller supplied'")
    assert db.query_one(env.conn, "SELECT * FROM aliases WHERE raw_text = 'stored window'") is None


async def test_on_mention_callback_fires_only_when_mentions_send(
    make_env: Callable[..., Env],
) -> None:
    """The injected mention counter fires once per mention actually sent and
    stays silent when discipline suppresses (health 'Mentions' field wiring)."""
    calls: list[None] = []
    env = make_env(on_mention=lambda: calls.append(None))
    await env.engine.report(GUILD, 42, cmd(Intent.UNDER_ATTACK), high(1, "Otanuomi"))
    assert len(calls) == 1
    env.clock.advance(5)  # inside the 30s per-user cooldown → suppressed
    await env.engine.report(GUILD, 42, cmd(Intent.UNDER_ATTACK), high(2, "Kisogo"))
    assert len(calls) == 1
    # Non-mention intents never fire it.
    await env.engine.report(GUILD, 42, cmd(Intent.TIMER, detail="1h"), high(2, "Kisogo"))
    assert len(calls) == 1


# ── callsign registry through the shared report path (GDD §6.1) ──────────────


async def test_register_through_report_path(make_env: Callable[..., Env]) -> None:
    env = make_env()
    out = await env.engine.report(GUILD, 42, cmd(Intent.REGISTER, detail="Space Junkie"), None)
    assert out.outcome is Outcome.POSTED
    assert out.utterance == "Registered you as Space Junkie."
    assert out.card is None
    assert out.incident_id is None
    assert env.poster.posts == []  # no card, no mentions — ever
    assert env.engine.callsigns.lookup(42) == "Space Junkie"
    log_row = db.query_one(env.conn, "SELECT * FROM command_log")
    assert log_row is not None
    assert log_row["parsed_intent"] == "REGISTER"
    assert log_row["outcome"] == "POSTED"


async def test_register_without_callsign_rejected(make_env: Callable[..., Env]) -> None:
    env = make_env()
    out = await env.engine.report(GUILD, 42, cmd(Intent.REGISTER), None)
    assert out.outcome is Outcome.REJECTED
    assert out.utterance == "Say again the callsign."
    assert db.query(env.conn, "SELECT * FROM callsigns") == []


async def test_unregister_through_report_path(make_env: Callable[..., Env]) -> None:
    env = make_env()
    missing = await env.engine.report(GUILD, 42, cmd(Intent.UNREGISTER), None)
    assert missing.outcome is Outcome.REJECTED
    assert missing.utterance == "You are not registered."
    await env.engine.report(GUILD, 42, cmd(Intent.REGISTER, detail="Space Junkie"), None)
    out = await env.engine.report(GUILD, 42, cmd(Intent.UNREGISTER), None)
    assert out.outcome is Outcome.POSTED
    assert out.utterance == "Unregistered."
    assert env.engine.callsigns.lookup(42) is None
    rows = db.query(env.conn, "SELECT parsed_intent, outcome FROM command_log ORDER BY id")
    assert [(r["parsed_intent"], r["outcome"]) for r in rows] == [
        ("UNREGISTER", "REJECTED"),
        ("REGISTER", "POSTED"),
        ("UNREGISTER", "POSTED"),
    ]


async def test_whoami_through_report_path(make_env: Callable[..., Env]) -> None:
    env = make_env()
    out = await env.engine.report(GUILD, 42, cmd(Intent.WHOAMI), None)
    assert out.outcome is Outcome.POSTED
    assert out.utterance == "You are not registered."
    await env.engine.report(GUILD, 42, cmd(Intent.REGISTER, detail="Space Junkie"), None)
    out = await env.engine.report(GUILD, 42, cmd(Intent.WHOAMI), None)
    assert out.utterance == "You are Space Junkie."


async def test_card_shows_reporter_callsign_when_registered(
    make_env: Callable[..., Env],
) -> None:
    env = make_env()
    await env.engine.report(GUILD, 42, cmd(Intent.REGISTER, detail="Space Junkie"), None)
    out = await env.engine.report(GUILD, 42, cmd(Intent.HOSTILE_SPOTTED), high(1, "Otanuomi"))
    assert out.card is not None
    assert field_value(out.card, "Reported by") == "Space Junkie"
    # A fold goes back to the distinct-reporter count ("reported by 2", §9.1).
    env.clock.advance(10)
    folded = await env.engine.report(GUILD, 43, cmd(Intent.HOSTILE_SPOTTED), high(1, "Otanuomi"))
    assert folded.outcome is Outcome.FOLDED
    assert field_value(folded.card, "Reported by") == "2"


async def test_card_falls_back_to_count_when_unregistered(
    make_env: Callable[..., Env],
) -> None:
    env = make_env()
    out = await env.engine.report(GUILD, 42, cmd(Intent.HOSTILE_SPOTTED), high(1, "Otanuomi"))
    assert field_value(out.card, "Reported by") == "1"


# ── personal pings through the shared report path (GDD §10.3) ────────────────


def ping_cmd(detail: str, system_text: str | None = None) -> ParsedCommand:
    return ParsedCommand(
        intent=Intent.PING_ME,
        system_text=system_text,
        group_alias=None,
        detail=detail,
        raw="synthetic transcript",
    )


async def test_ping_me_through_report_path(make_env: Callable[..., Env]) -> None:
    env = make_env()
    out = await env.engine.report(GUILD, 99, ping_cmd("GATE_CAMP", "Otanuomi"), high(1, "Otanuomi"))
    assert out.outcome is Outcome.POSTED
    assert out.utterance == "Pinging you for gate camps in Otanuomi."
    assert out.card is None
    assert out.incident_id is None
    assert env.poster.posts == []  # no card, no mentions from the command itself
    row = db.query_one(env.conn, "SELECT * FROM personal_pings")
    assert row is not None
    assert row["user_id"] == 99
    assert row["system_id"] == 1
    log_row = db.query_one(env.conn, "SELECT * FROM command_log")
    assert log_row is not None
    assert log_row["parsed_intent"] == "PING_ME"
    assert log_row["outcome"] == "POSTED"


async def test_ping_me_everywhere_utterance(make_env: Callable[..., Env]) -> None:
    env = make_env()
    out = await env.engine.report(
        GUILD, 99, ping_cmd("HOSTILE_SPOTTED,UNDER_ATTACK,ASSIST_REQUEST,GATE_CAMP"), None
    )
    assert out.outcome is Outcome.POSTED
    assert out.utterance == "Pinging you for everything everywhere."


async def test_ping_me_sub_high_resolution_rejected(make_env: Callable[..., Env]) -> None:
    """A subscription silently scoped to the wrong system would never fire —
    anything below HIGH tier asks again instead of storing."""
    env = make_env()
    for resolution in (medium(), None):
        out = await env.engine.report(
            GUILD, 99, ping_cmd("GATE_CAMP", "oh tan you oh me"), resolution
        )
        assert out.outcome is Outcome.REJECTED
        assert out.utterance == "Say again the system."
    assert db.query(env.conn, "SELECT * FROM personal_pings") == []


async def test_ping_me_cap_rejected(make_env: Callable[..., Env]) -> None:
    env = make_env(personal_pings_max=1)
    first = await env.engine.report(GUILD, 99, ping_cmd("GATE_CAMP"), None)
    assert first.outcome is Outcome.POSTED
    out = await env.engine.report(GUILD, 99, ping_cmd("HOSTILE_SPOTTED"), None)
    assert out.outcome is Outcome.REJECTED
    assert out.utterance == "Ping limit reached."


async def test_ping_me_clear_through_report_path(make_env: Callable[..., Env]) -> None:
    env = make_env()
    none_yet = await env.engine.report(GUILD, 99, cmd(Intent.PING_ME_CLEAR), None)
    assert none_yet.outcome is Outcome.REJECTED
    assert none_yet.utterance == "You have no pings set."
    await env.engine.report(GUILD, 99, ping_cmd("GATE_CAMP"), None)
    out = await env.engine.report(GUILD, 99, cmd(Intent.PING_ME_CLEAR), None)
    assert out.outcome is Outcome.POSTED
    assert out.utterance == "No longer pinging you."
    assert db.query(env.conn, "SELECT * FROM personal_pings") == []


async def test_personal_subscriber_mentioned_on_matching_incident(
    make_env: Callable[..., Env],
) -> None:
    env = make_env()
    await env.engine.report(GUILD, 99, ping_cmd("GATE_CAMP", "Otanuomi"), high(1, "Otanuomi"))
    out = await env.engine.report(GUILD, 42, cmd(Intent.GATE_CAMP), high(1, "Otanuomi"))
    assert out.outcome is Outcome.POSTED
    assert out.utterance == "Gate camp Otanuomi, pinged."
    _, channel, content, _ = env.poster.posts[-1]
    assert channel is AlertChannel.ALERTS
    assert "<@99>" in content
    assert "@here" not in content  # constraint 11: personal pings never @here


async def test_personal_only_mention_still_goes_to_alerts(
    make_env: Callable[..., Env],
) -> None:
    """No role rule matches Alenia — a personal subscriber alone carries the
    card into #intel-alerts (a mention is a mention)."""
    env = make_env()
    await env.engine.report(GUILD, 99, ping_cmd("GATE_CAMP"), None)  # everywhere
    out = await env.engine.report(GUILD, 42, cmd(Intent.GATE_CAMP), high(3, "Alenia"))
    assert out.outcome is Outcome.POSTED
    _, channel, content, _ = env.poster.posts[-1]
    assert channel is AlertChannel.ALERTS
    assert content == "<@99>"


async def test_fold_never_repings_personal_subscribers(make_env: Callable[..., Env]) -> None:
    env = make_env()
    await env.engine.report(GUILD, 99, ping_cmd("GATE_CAMP", "Otanuomi"), high(1, "Otanuomi"))
    await env.engine.report(GUILD, 42, cmd(Intent.GATE_CAMP), high(1, "Otanuomi"))
    env.clock.advance(30)
    folded = await env.engine.report(GUILD, 43, cmd(Intent.GATE_CAMP), high(1, "Otanuomi"))
    assert folded.outcome is Outcome.FOLDED
    assert len(env.poster.posts) == 1  # one incident, one message
    _, _, edit_content, _ = env.poster.edits[-1]
    assert edit_content == ""  # the fold edit carries no mentions at all


async def test_reporter_not_personally_pinged_for_own_report(
    make_env: Callable[..., Env],
) -> None:
    env = make_env()
    await env.engine.report(GUILD, 42, ping_cmd("GATE_CAMP"), None)
    await env.engine.report(GUILD, 42, cmd(Intent.GATE_CAMP), high(1, "Otanuomi"))
    _, _, content, _ = env.poster.posts[-1]
    assert "<@42>" not in content


async def test_personal_ping_rides_reporter_cooldown(make_env: Callable[..., Env]) -> None:
    """Discipline suppression applies to personal pings exactly like roles."""
    env = make_env()
    await env.engine.report(GUILD, 99, ping_cmd("GATE_CAMP"), None)
    await env.engine.report(GUILD, 42, cmd(Intent.GATE_CAMP), high(1, "Otanuomi"))
    assert "<@99>" in env.poster.posts[-1][2]
    env.clock.advance(10)  # inside reporter 42's 30s cooldown
    out = await env.engine.report(GUILD, 42, cmd(Intent.GATE_CAMP), high(2, "Kisogo"))
    assert out.outcome is Outcome.POSTED
    _, channel, content, _ = env.poster.posts[-1]
    assert content == ""  # suppressed — user mention included
    assert channel is AlertChannel.LIVE
    assert out.utterance == "Gate camp Kisogo, posted."


async def test_personal_ping_never_manufactures_here_on_non_red(
    make_env: Callable[..., Env],
) -> None:
    """A personal subscriber alone must never produce @here on a non-CODE-RED
    incident (constraint 11): a CODE ORANGE sighting stays a user mention only.
    (CODE RED @heres by severity — see test_here_on_severity_pings_red.)"""
    env = make_env()
    env.engine._rules = []  # noqa: SLF001 — isolate the personal path
    await env.engine.report(GUILD, 99, ping_cmd("HOSTILE_SPOTTED"), None)
    await env.engine.report(GUILD, 42, cmd(Intent.HOSTILE_SPOTTED), high(1, "Otanuomi"))
    _, _, content, _ = env.poster.posts[-1]
    assert content == "<@99>"
    assert "@here" not in content


async def test_build_prior_context(make_env: Callable[..., Env]) -> None:
    env = make_env()
    await env.engine.report(GUILD, 42, cmd(Intent.UNDER_ATTACK), high(1, "Otanuomi"))
    env.clock.advance(120)
    ctx = env.engine.build_prior_context(GUILD, 42)
    assert ctx.active_systems == (1,)
    assert ctx.home_system_id == 1
    assert ctx.reporter_counts == {1: 1}
    assert 1 in ctx.recency_min
    assert ctx.recency_min[1] == pytest.approx(2.0)
    other = env.engine.build_prior_context(GUILD, 99)
    assert other.reporter_counts == {}


async def test_broadcast_relays_freeform_intel(make_env: Callable[..., Env]) -> None:
    # GDD §8.6: anything not matching the grammar is relayed to the channel.
    env = make_env()
    out = await env.engine.broadcast(GUILD, 42, "blop fleet moving to Moe 8 gate")
    assert out.outcome is Outcome.POSTED
    assert len(env.poster.posts) == 1
    _, channel, content, card = env.poster.posts[-1]
    assert content == ""  # no @here without all-hands
    assert card.embed["description"] == "blop fleet moving to Moe 8 gate"
    row = db.query_one(env.conn, "SELECT parsed_intent, outcome FROM command_log")
    assert row["parsed_intent"] == "BROADCAST" and row["outcome"] == "POSTED"


async def test_broadcast_all_hands_pings_here(make_env: Callable[..., Env]) -> None:
    env = make_env()
    out = await env.engine.broadcast(GUILD, 42, "cyno up, all hands", here=True)
    assert out.outcome is Outcome.POSTED
    _, _, content, _ = env.poster.posts[-1]
    assert "@here" in content


async def test_silent_mode_posts_without_pinging(make_env: Callable[..., Env]) -> None:
    # mentions_enabled=False: incidents still post, but AURA mentions nobody.
    env = make_env(mentions_enabled=False)
    out = await env.engine.report(GUILD, 42, cmd(Intent.UNDER_ATTACK), high(1, "Otanuomi"))
    assert out.outcome is Outcome.POSTED
    _, _, content, _ = env.poster.posts[-1]
    assert content == ""  # no @here, no role mention
    b = await env.engine.broadcast(GUILD, 42, "cyno up, all hands", here=True)
    assert b.outcome is Outcome.POSTED
    assert env.poster.posts[-1][2] == ""  # silent even with all-hands


async def test_card_shows_threat_code(make_env: Callable[..., Env]) -> None:
    env = make_env()
    await env.engine.report(GUILD, 42, cmd(Intent.UNDER_ATTACK), high(1, "Otanuomi"))
    _, _, _, card = env.poster.posts[-1]
    assert "CODE RED" in card.embed["title"]
    await env.engine.report(GUILD, 43, cmd(Intent.HOSTILE_SPOTTED), high(2, "Kisogo"))
    _, _, _, card2 = env.poster.posts[-1]
    assert "CODE ORANGE" in card2.embed["title"]


async def test_here_on_severity_pings_red_without_rules(make_env: Callable[..., Env]) -> None:
    # Ping-by-colour: CODE RED fires @here even with no routing rules, when
    # mentions are enabled and "high" is in here_on_severity (the default).
    env = make_env()  # mentions_enabled defaults True, here_on_severity ("high",)
    out = await env.engine.report(GUILD, 42, cmd(Intent.UNDER_ATTACK), high(1, "Otanuomi"))
    assert out.outcome is Outcome.POSTED
    _, _, content, _ = env.poster.posts[-1]
    assert "@here" in content
    # CODE ORANGE (medium) does not, by default.
    await env.engine.report(GUILD, 43, cmd(Intent.HOSTILE_SPOTTED), high(2, "Kisogo"))
    _, _, content2, _ = env.poster.posts[-1]
    assert "@here" not in content2
