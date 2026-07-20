"""The routing-alarm gate on the global mentions kill-switch (silent mode).

``AuraBot._load_routing_rules`` raises CRITICAL "nobody gets mentioned" alarms so
a phone admin sees a broken routing config before pilots do — but ONLY when
mentions are actually enabled. With ``discord.mentions_enabled`` off (the
deliberate silent-mode switch, e.g. during testing), zero active rules is the
intended state, so the alarm is cleared, not raised.

Driven by calling the method unbound with a minimal fake ``self`` — no Discord,
no real bot — so the gate logic is exercised in isolation.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from cortana.alarms import AlarmCode
from cortana.dsc.bot import AuraBot


class _Rec:
    def __init__(self) -> None:
        self.raised: list[AlarmCode] = []
        self.cleared: list[AlarmCode] = []


def _fake_bot(
    *,
    mentions_enabled: bool,
    rule_count: int,
    unresolved_name: str | None = None,
    guild_present: bool = True,
) -> tuple[Any, _Rec]:
    rec = _Rec()
    guild = SimpleNamespace(roles=[SimpleNamespace(name="Home-Defense", id=1)])

    def _load(resolve_role: Any) -> int:
        # Simulate the engine asking the resolver about a role name; an unknown
        # one populates the caller's `unresolved` set (the SKIPPED-rules path).
        if unresolved_name is not None:
            resolve_role(unresolved_name)
        return rule_count

    async def _raise(code: AlarmCode, *_a: Any) -> None:
        rec.raised.append(code)

    async def _clear(code: AlarmCode) -> None:
        rec.cleared.append(code)

    holder = SimpleNamespace(
        current=SimpleNamespace(
            discord=SimpleNamespace(mentions_enabled=mentions_enabled, guild_id=1)
        )
    )
    fake = SimpleNamespace(
        holder=holder,
        get_guild=lambda _gid: guild if guild_present else None,
        engine=SimpleNamespace(load_routing_rules=_load),
        _raise_alarm=_raise,
        _clear_alarm=_clear,
    )
    return fake, rec


async def test_zero_rules_is_silent_when_mentions_off() -> None:
    fake, rec = _fake_bot(mentions_enabled=False, rule_count=0)
    await AuraBot._load_routing_rules(fake)
    assert AlarmCode.ROUTING_ZERO_RULES not in rec.raised  # not paged
    assert AlarmCode.ROUTING_ZERO_RULES in rec.cleared  # actively cleared


async def test_zero_rules_is_critical_when_mentions_on() -> None:
    fake, rec = _fake_bot(mentions_enabled=True, rule_count=0)
    await AuraBot._load_routing_rules(fake)
    assert AlarmCode.ROUTING_ZERO_RULES in rec.raised  # the guardrail returns


async def test_active_rules_clear_the_alarm_regardless() -> None:
    fake, rec = _fake_bot(mentions_enabled=True, rule_count=3)
    await AuraBot._load_routing_rules(fake)
    assert AlarmCode.ROUTING_ZERO_RULES not in rec.raised
    assert AlarmCode.ROUTING_ZERO_RULES in rec.cleared


async def test_unresolved_roles_silent_when_mentions_off() -> None:
    fake, rec = _fake_bot(mentions_enabled=False, rule_count=1, unresolved_name="@Ghost")
    await AuraBot._load_routing_rules(fake)
    assert AlarmCode.ROLE_UNRESOLVED not in rec.raised
    assert AlarmCode.ROLE_UNRESOLVED in rec.cleared


async def test_unresolved_roles_warned_when_mentions_on() -> None:
    fake, rec = _fake_bot(mentions_enabled=True, rule_count=1, unresolved_name="@Ghost")
    await AuraBot._load_routing_rules(fake)
    assert AlarmCode.ROLE_UNRESOLVED in rec.raised


async def test_guild_missing_silent_when_mentions_off() -> None:
    fake, rec = _fake_bot(mentions_enabled=False, rule_count=0, guild_present=False)
    await AuraBot._load_routing_rules(fake)
    assert AlarmCode.ROUTING_ZERO_RULES not in rec.raised
    assert AlarmCode.ROUTING_ZERO_RULES in rec.cleared
