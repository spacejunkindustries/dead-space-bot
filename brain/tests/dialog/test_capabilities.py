"""CAPABILITIES intent — the spoken overview path through DialogEngine (GDD §6.1).

Voice in → voice out, mirroring the FACT/INSULT fun path: a fixed canned line is
spoken, nothing reaches the incident engine, nothing is posted, nothing pings.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from cortana import tts
from cortana.dialog.engine import DialogEngine
from cortana.dialog.types import DialogSession, DialogState, Report
from cortana.nlu import grammar
from cortana.types import Intent, ParsedCommand


class _FakeSpeaker:
    def __init__(self, spoken: bool = True) -> None:
        self.spoken = spoken
        self.said: list[str] = []
        self.max_s: list[float | None] = []

    async def say(self, guild_id, text, priority, user_id=None, max_s=None, **kw) -> bool:
        self.said.append(text)
        self.max_s.append(max_s)
        return self.spoken


def _holder() -> SimpleNamespace:
    return SimpleNamespace(current=SimpleNamespace(fun=SimpleNamespace(max_speak_s=20.0)))


def _engine(speaker: _FakeSpeaker, incidents: Mock, sent: list) -> DialogEngine:
    async def _send(channel_id, content, embed=None):
        sent.append((channel_id, content))
        return True

    return DialogEngine(
        _holder(),  # type: ignore[arg-type]
        capture=None,
        transcriber=Mock(),
        speaker=speaker,  # type: ignore[arg-type]
        incidents=incidents,
        discipline=Mock(),
        gazetteer=Mock(),
        conn=Mock(),
        health=Mock(),
        chat_provider=lambda: (None, "disabled"),
        member_role_ids=lambda uid: [],
        send_channel=_send,
        shutdown=asyncio.Event(),
    )


def _session() -> DialogSession:
    return DialogSession(user_id=1, guild_id=9, state=DialogState.IDLE, gen=2)


async def test_report_speaks_capabilities_and_never_escalates() -> None:
    speaker = _FakeSpeaker(spoken=True)
    incidents = Mock()
    sent: list = []
    engine = _engine(speaker, incidents, sent)
    parsed = ParsedCommand(Intent.CAPABILITIES, None, None, None, "hey cortana what can you do")

    await engine._report(_session(), Report(parsed))

    # Spoke the fixed overview …
    assert speaker.said == [tts.capabilities()]
    # … with the fun path's larger cap, not the §12.2 command cap …
    assert speaker.max_s == [20.0]
    # … and NOTHING reached the incident engine or a channel (voice-only).
    incidents.report.assert_not_called()
    incidents.broadcast.assert_not_called()
    assert sent == []


async def test_capabilities_unspoken_is_dropped_not_posted() -> None:
    """Muted / over-cap / synth failure: the line is dropped, never posted —
    a capabilities tour is not worth spamming the intel channel."""
    speaker = _FakeSpeaker(spoken=False)
    sent: list = []
    engine = _engine(speaker, Mock(), sent)

    await engine._capabilities_reply(_session())

    assert speaker.said == [tts.capabilities()]
    assert sent == []


def test_grammar_to_engine_intent_matches() -> None:
    """The grammar produces exactly the intent the engine intercepts."""
    parsed = grammar.parse("hey cortana, what can you do")
    assert parsed is not None
    assert parsed.intent is Intent.CAPABILITIES
