"""Voice gateway census → join/leave steering.

Regression coverage for the muted-pilot bug: a pilot sitting in voice muted
until they need to shout a report is *present*, so CORTANA must stay with them
(GDD §1.2). Only the §20 silence alarm cares about the unmuted count.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cortana.dsc.bot import AuraBot
from cortana.voice_gateway import VoiceGateway

GUILD = 4200
CHANNEL = 9


class _FakeIpc:
    def __init__(self) -> None:
        self.controls: list[dict[str, Any]] = []

    async def send_control(self, msg: dict[str, Any]) -> None:
        self.controls.append(msg)


class _StubHolder:
    def __init__(self, *, auto_join: bool = True, join_announcement: str = "daily") -> None:
        discord = SimpleNamespace(
            guild_id=GUILD,
            watch_voice_channels=(CHANNEL,),
            auto_join=auto_join,
            join_announcement=join_announcement,
        )
        self.current = SimpleNamespace(discord=discord)


def _member(*, bot: bool = False, self_mute: bool = False, mute: bool = False) -> Any:
    voice = SimpleNamespace(self_mute=self_mute, mute=mute)
    return SimpleNamespace(bot=bot, voice=voice)


async def _noop_announce(_channel_id: int) -> None:
    return None


def _gateway(holder: _StubHolder, ipc: _FakeIpc) -> VoiceGateway:
    # join_debounce_s large so a scheduled join never fires mid-test; leave is
    # synchronous and is what the muted-pilot regression exercises.
    return VoiceGateway(holder, ipc, None, _noop_announce, join_debounce_s=999)  # type: ignore[arg-type]


# ── the census split (GDD §1.2 vs §20) ───────────────────────────────────────


def test_census_counts_present_and_unmuted_separately() -> None:
    channel = SimpleNamespace(
        members=[
            _member(),  # unmuted human
            _member(self_mute=True),  # muted human — present, not unmuted
            _member(mute=True),  # server-muted human — present, not unmuted
            _member(bot=True),  # CORTANA itself — never counted
        ]
    )
    present, unmuted = AuraBot._human_census(channel)
    assert present == 3
    assert unmuted == 1


def test_census_member_without_voice_state_counts_as_present() -> None:
    # Transient cache: member is in the channel list but .voice is momentarily
    # None. They are present (keep CORTANA) but not countable as unmuted.
    channel = SimpleNamespace(members=[SimpleNamespace(bot=False, voice=None)])
    present, unmuted = AuraBot._human_census(channel)
    assert present == 1
    assert unmuted == 0


# ── join/leave is driven by presence, not unmuted ────────────────────────────


@pytest.mark.asyncio
async def test_muted_present_pilot_does_not_trigger_leave() -> None:
    ipc = _FakeIpc()
    gw = _gateway(_StubHolder(), ipc)
    gw._joined_channel_id = CHANNEL  # CORTANA is already in the channel

    # One pilot present but fully muted: present=1, unmuted=0.
    await gw.on_voice_update(CHANNEL, 1, 0)

    assert gw.joined_channel_id == CHANNEL
    assert not any(c["t"] == "leave" for c in ipc.controls)


@pytest.mark.asyncio
async def test_empty_channel_triggers_leave() -> None:
    ipc = _FakeIpc()
    gw = _gateway(_StubHolder(), ipc)
    gw._joined_channel_id = CHANNEL

    await gw.on_voice_update(CHANNEL, 0, 0)

    assert gw.joined_channel_id is None
    assert any(c["t"] == "leave" and c["guild_id"] == str(GUILD) for c in ipc.controls)


@pytest.mark.asyncio
async def test_muted_present_pilot_schedules_join() -> None:
    ipc = _FakeIpc()
    gw = _gateway(_StubHolder(), ipc)

    # Not joined yet; a present-but-muted pilot must still pull CORTANA in.
    await gw.on_voice_update(CHANNEL, 1, 0)

    assert gw._pending_channel_id == CHANNEL
    gw._cancel_pending_join()


@pytest.mark.asyncio
async def test_unmuted_count_feeds_census_listener() -> None:
    ipc = _FakeIpc()
    gw = _gateway(_StubHolder(), ipc)
    gw._joined_channel_id = CHANNEL
    seen: list[int] = []
    gw.set_census_listener(seen.append)

    # Three present, one unmuted: the §20 alarm must hear the unmuted count.
    await gw.on_voice_update(CHANNEL, 3, 1)

    assert seen == [1]
    assert gw.joined_channel_id == CHANNEL


@pytest.mark.asyncio
async def test_unmuted_defaults_to_present_when_omitted() -> None:
    ipc = _FakeIpc()
    gw = _gateway(_StubHolder(), ipc)
    gw._joined_channel_id = CHANNEL
    seen: list[int] = []
    gw.set_census_listener(seen.append)

    await gw.on_voice_update(CHANNEL, 2)

    assert seen == [2]


# ── §19 join-announcement cadence (discord.join_announcement) ────────────────


class _CountingAnnounce:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, _channel_id: int) -> None:
        self.calls += 1


def _db_conn() -> Any:
    from cortana.core import db as dbmod

    conn = dbmod.connect(":memory:")
    dbmod.migrate(conn)
    return conn


async def test_join_announcement_daily_posts_once_and_survives_restart() -> None:
    holder = _StubHolder(join_announcement="daily")
    ipc = _FakeIpc()
    announce = _CountingAnnounce()
    conn = _db_conn()
    gw = VoiceGateway(holder, ipc, conn, announce, join_debounce_s=999)

    await gw._send_join(CHANNEL, announce=True)
    await gw._send_join(CHANNEL, announce=True)  # rejoin churn
    assert announce.calls == 1

    # A fresh gateway on the same DB — i.e. a process restart, the exact
    # source of the spam — still suppresses within the 24h window.
    gw2 = VoiceGateway(holder, ipc, conn, announce, join_debounce_s=999)
    await gw2._send_join(CHANNEL, announce=True)
    assert announce.calls == 1


async def test_join_announcement_off_never_posts() -> None:
    holder = _StubHolder(join_announcement="off")
    gw = VoiceGateway(
        holder, _FakeIpc(), _db_conn(), (announce := _CountingAnnounce()), join_debounce_s=999
    )
    await gw._send_join(CHANNEL, announce=True)
    assert announce.calls == 0


async def test_join_announcement_every_posts_each_join() -> None:
    holder = _StubHolder(join_announcement="every")
    gw = VoiceGateway(
        holder, _FakeIpc(), _db_conn(), (announce := _CountingAnnounce()), join_debounce_s=999
    )
    await gw._send_join(CHANNEL, announce=True)
    await gw._send_join(CHANNEL, announce=True)
    assert announce.calls == 2
