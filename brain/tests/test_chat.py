"""The §6.6 "command override" assistant — client, key loading, cooldown."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aura.chat import ChatClient, ChatCooldownError, ChatError, read_api_key
from aura.config import ChatConfig


class _Holder:
    def __init__(self, chat: ChatConfig) -> None:
        self.current = SimpleNamespace(chat=chat)


def _text_block(text: str) -> Any:
    return SimpleNamespace(type="text", text=text)


class _FakeMessages:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _response(text: str, stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[_text_block(text)],
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )


def make_client(responses: list[Any], **cfg: Any) -> tuple[ChatClient, _FakeMessages]:
    client = ChatClient(_Holder(ChatConfig(enabled=True, **cfg)), api_key="sk-test")  # type: ignore[arg-type]
    fake = _FakeMessages(responses)
    client._client = SimpleNamespace(messages=fake)
    return client, fake


async def test_ask_returns_text_and_passes_config() -> None:
    client, fake = make_client([_response("It is sunny in Chicago, 21 degrees.")])
    reply = await client.ask(42, "what's the weather in Chicago?")
    assert reply == "It is sunny in Chicago, 21 degrees."
    call = fake.calls[0]
    assert call["model"] == "claude-haiku-4-5"
    assert call["max_tokens"] == 300
    # Haiku gets the basic web-search variant, one use max.
    assert call["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}]
    assert call["messages"] == [{"role": "user", "content": "what's the weather in Chicago?"}]


async def test_web_search_variant_tracks_model() -> None:
    client, fake = make_client([_response("ok")], model="claude-opus-4-8")
    await client.ask(42, "hi")
    assert fake.calls[0]["tools"][0]["type"] == "web_search_20260209"


async def test_web_search_off_sends_no_tools() -> None:
    client, fake = make_client([_response("ok")], web_search=False)
    await client.ask(42, "hi")
    assert "tools" not in fake.calls[0]


async def test_cooldown_is_per_user_and_shared_across_surfaces() -> None:
    client, _ = make_client([_response("a"), _response("b"), _response("c")])
    await client.ask(42, "first")
    with pytest.raises(ChatCooldownError):
        await client.ask(42, "again too soon")  # same pilot, any surface
    await client.ask(43, "different pilot")  # unaffected


async def test_failed_ask_does_not_burn_cooldown() -> None:
    # The cooldown arms on SUCCESS only: after "Override unavailable" the
    # pilot's immediate retry must reach the API, not a throttle message.
    client, _ = make_client([_response("", stop_reason="refusal"), _response("recovered")])
    with pytest.raises(ChatError):
        await client.ask(42, "q")
    assert await client.ask(42, "q") == "recovered"


async def test_pause_turn_is_resumed() -> None:
    paused = SimpleNamespace(
        stop_reason="pause_turn",
        content=[_text_block("searching…")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    client, fake = make_client([paused, _response("21 degrees.")])
    reply = await client.ask(42, "weather?")
    assert reply == "21 degrees."
    assert len(fake.calls) == 2
    # The resume carries the paused assistant turn back.
    assert fake.calls[1]["messages"][1]["role"] == "assistant"


async def test_refusal_and_empty_raise_chat_error() -> None:
    client, _ = make_client([_response("", stop_reason="refusal")])
    with pytest.raises(ChatError):
        await client.ask(42, "q")
    client2, _ = make_client([_response("   ")])
    with pytest.raises(ChatError):
        await client2.ask(43, "q")


def test_read_api_key_prefers_credentials_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cred_dir = tmp_path / "creds"
    cred_dir.mkdir()
    (cred_dir / "anthropic").write_text("sk-from-systemd\n")
    fallback = tmp_path / "anthropic"
    fallback.write_text("sk-from-file\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cred_dir))
    assert read_api_key(str(fallback)) == "sk-from-systemd"
    monkeypatch.delenv("CREDENTIALS_DIRECTORY")
    assert read_api_key(str(fallback)) == "sk-from-file"
    assert read_api_key(str(tmp_path / "missing")) is None


def test_read_api_key_unreadable_file_degrades_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A root-owned 0600 key file read by the aura user raises PermissionError;
    # that must mean "channel off", never a Brain startup crash.
    fallback = tmp_path / "anthropic"
    fallback.write_text("sk-secret\n")
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    def _denied(self: Path, *a: object, **k: object) -> str:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "read_text", _denied)
    assert read_api_key(str(fallback)) is None


def test_read_api_key_empty_credential_means_no_key(tmp_path: Path) -> None:
    # install.sh guarantees the credential source exists, empty when unused —
    # an empty file must read as "no key", not a truthy empty string.
    fallback = tmp_path / "anthropic"
    fallback.write_text("")
    assert read_api_key(str(fallback)) is None


def test_chat_config_defaults_to_disabled() -> None:
    cfg = ChatConfig()
    assert cfg.enabled is False
    assert cfg.model == "claude-haiku-4-5"
