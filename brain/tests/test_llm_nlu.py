"""LLM understanding brain (GDD §6.7): pure JSON parsing + the HTTP call.

No real model — parse_nlu_json is pure and interpret() is exercised against a
stubbed urlopen, so these run offline and deterministically."""

from __future__ import annotations

import json
from typing import Any

import pytest

from cortana.config import NluConfig
from cortana.nlu import llm_nlu
from cortana.nlu.llm_nlu import parse_nlu_json
from cortana.types import Intent, Severity


def test_parse_full_command() -> None:
    p = parse_nlu_json(
        '{"intent":"UNDER_ATTACK","place":"taisy","detail":"two cruisers","severity":"high"}',
        "im tackled in taisy",
    )
    assert p is not None
    assert p.intent is Intent.UNDER_ATTACK
    assert p.system_text == "taisy"
    assert p.detail == "two cruisers"
    assert p.severity is Severity.HIGH
    assert p.raw == "im tackled in taisy"  # transcript kept verbatim


def test_parse_tolerates_prose_and_fences() -> None:
    p = parse_nlu_json(
        'Sure:\n```json\n{"intent":"HOSTILE_SPOTTED","place":"kisogo","severity":"medium"}\n```',
        "reds in kisogo",
    )
    assert p is not None and p.intent is Intent.HOSTILE_SPOTTED and p.system_text == "kisogo"


@pytest.mark.parametrize(
    "reply",
    [
        '{"intent":"NONE","place":null}',  # not a report
        '{"intent":"UNDER_ATTACK","place":null}',  # report with no place → can't resolve
        '{"intent":"REGISTER","place":"x"}',  # intent outside the allowed report set
        "not json at all",
        "{ broken json",
        "[]",  # not an object
    ],
)
def test_parse_rejects_non_commands(reply: str) -> None:
    assert parse_nlu_json(reply, "raw") is None


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self._body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": content}}]}
        ).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _cfg(**kw: Any) -> NluConfig:
    base = {
        "understanding": True,
        "url": "http://127.0.0.1:11434/v1/chat/completions",
        "model": "m",
    }
    base.update(kw)
    return NluConfig(**base)


def test_interpret_posts_and_parses(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse('{"intent":"ASSIST_REQUEST","place":"taisy","severity":"high"}')

    monkeypatch.setattr("cortana.nlu.llm_nlu.urllib.request.urlopen", fake_urlopen)
    p = llm_nlu.interpret(_cfg(), "we need help in taisy right now")
    assert p is not None and p.intent is Intent.ASSIST_REQUEST and p.system_text == "taisy"
    assert captured["body"]["model"] == "m"
    assert captured["body"]["messages"][-1]["content"] == "we need help in taisy right now"


def test_interpret_off_or_unreachable_returns_none(monkeypatch: Any) -> None:
    # No URL → never calls out.
    assert llm_nlu.interpret(_cfg(url=""), "im tackled in taisy") is None
    # Endpoint down → swallowed to None (degrade to grammar-only, never crash).

    def boom(request: Any, timeout: float | None = None) -> _FakeResponse:
        raise OSError("connection refused")

    monkeypatch.setattr("cortana.nlu.llm_nlu.urllib.request.urlopen", boom)
    assert llm_nlu.interpret(_cfg(), "im tackled in taisy") is None


def test_interpret_empty_transcript_is_none() -> None:
    assert llm_nlu.interpret(_cfg(), "   ") is None
