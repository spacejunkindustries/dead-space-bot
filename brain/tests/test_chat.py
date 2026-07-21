"""The §6.6 "command override" assistant — client, key loading, cooldown."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cortana.chat import ChatClient, ChatCooldownError, ChatError, read_api_key
from cortana.config import ChatConfig


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
    assert cfg.backend == "anthropic"
    assert cfg.local_url == ""


# ── on-box SLM backend (GDD §6.6, chat.backend: local) ───────────────────────


import json  # noqa: E402

from cortana.chat import LocalChatClient  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _local_holder(**cfg: Any) -> _Holder:
    base = {
        "enabled": True,
        "backend": "local",
        "local_url": "http://127.0.0.1:8081/v1/chat/completions",
    }
    base.update(cfg)
    return _Holder(ChatConfig(**base))


def _openai_reply(text: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


async def test_local_backend_posts_and_returns_text(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse(_openai_reply("Warping to you now, hold tight."))

    monkeypatch.setattr("cortana.chat.urllib.request.urlopen", fake_urlopen)
    client = LocalChatClient(_local_holder(model="qwen2.5-1.5b-instruct"))
    reply = await client.ask(42, "you there?")
    assert reply == "Warping to you now, hold tight."
    assert captured["url"] == "http://127.0.0.1:8081/v1/chat/completions"
    assert captured["body"]["model"] == "qwen2.5-1.5b-instruct"
    assert captured["body"]["messages"][0]["role"] == "system"
    assert captured["body"]["messages"][1] == {"role": "user", "content": "you there?"}
    assert captured["body"]["max_tokens"] == 300


async def test_local_backend_cooldown_blocks_rapid_repeat(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "cortana.chat.urllib.request.urlopen",
        lambda request, timeout=None: _FakeResponse(_openai_reply("Copy.")),
    )
    client = LocalChatClient(_local_holder(user_cooldown_s=999))
    assert await client.ask(7, "first") == "Copy."
    with pytest.raises(ChatCooldownError):
        await client.ask(7, "too soon")


async def test_local_backend_cooldown_not_armed_on_failure(monkeypatch: Any) -> None:
    def boom(request: Any, timeout: float | None = None) -> _FakeResponse:
        raise OSError("connection refused")

    monkeypatch.setattr("cortana.chat.urllib.request.urlopen", boom)
    client = LocalChatClient(_local_holder(user_cooldown_s=999))
    with pytest.raises(ChatError):
        await client.ask(7, "first")
    # A failed request must not convert the pilot's retry into "cooling down".
    with pytest.raises(ChatError):  # still ChatError (transport), not ChatCooldownError
        await client.ask(7, "retry")


async def test_local_backend_malformed_response_is_chat_error(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "cortana.chat.urllib.request.urlopen",
        lambda request, timeout=None: _FakeResponse({"unexpected": "shape"}),
    )
    client = LocalChatClient(_local_holder())
    with pytest.raises(ChatError):
        await client.ask(1, "hello")


async def test_local_backend_unconfigured_url_is_chat_error() -> None:
    client = LocalChatClient(_local_holder(local_url=""))
    with pytest.raises(ChatError):
        await client.ask(1, "hello")


# ── close(): release the connection pool on replace/shutdown ──────────────────


async def test_chat_client_close_releases_pool_and_is_idempotent() -> None:
    """ChatClient.close() aclose()s the AsyncAnthropic pool once and is idempotent:
    a second close (or a close before the SDK was ever built) does nothing more."""
    calls = {"n": 0}

    class _FakeSdk:
        async def aclose(self) -> None:
            calls["n"] += 1

    client, _ = make_client([])
    client._client = _FakeSdk()  # type: ignore[assignment]

    await client.close()
    assert calls["n"] == 1
    assert client._client is None  # cleared, so a re-close is a no-op

    await client.close()  # idempotent — does not aclose again
    assert calls["n"] == 1


async def test_chat_client_close_no_client_is_noop() -> None:
    """A close before _sdk() ever built the pool must not raise."""
    client, _ = make_client([])
    client._client = None
    await client.close()  # no client to release — clean no-op


async def test_local_chat_client_close_returns_none() -> None:
    """LocalChatClient has no persistent client; close() satisfies the protocol
    without error."""
    client = LocalChatClient(_local_holder())
    assert await client.close() is None


# ── converse(): the conversation lane (GDD §6.8) ─────────────────────────────

from cortana.config import ConversationConfig  # noqa: E402


class _DualHolder:
    """Exposes BOTH sections so a client can be pointed at either lane."""

    def __init__(self, chat: ChatConfig, conversation: ConversationConfig) -> None:
        self.current = SimpleNamespace(chat=chat, conversation=conversation)


def _conv_client(responses: list[Any], **cfg: Any) -> tuple[ChatClient, _FakeMessages]:
    conv = ConversationConfig(enabled=True, backend="anthropic", model="claude-haiku-4-5", **cfg)
    holder = _DualHolder(ChatConfig(enabled=True), conv)
    client = ChatClient(holder, api_key="sk-test", config_section="conversation")  # type: ignore[arg-type]
    fake = _FakeMessages(responses)
    client._client = SimpleNamespace(messages=fake)
    return client, fake


async def test_converse_reads_conversation_section_and_has_no_cooldown() -> None:
    client, fake = _conv_client(
        [_response("Hello, pilot."), _response("Still here.")], max_tokens=150
    )
    history = [{"role": "user", "content": "hi"}]
    assert await client.converse(1, history) == "Hello, pilot."
    # A second immediate call goes straight through — NO ask()-style cooldown.
    assert await client.converse(1, history) == "Still here."
    # max_tokens comes from the conversation section (150), not chat's 300.
    assert fake.calls[0]["max_tokens"] == 150
    # No web search on the conversation lane, and it carries the history verbatim.
    assert "tools" not in fake.calls[0]
    assert fake.calls[0]["messages"] == history
    assert "CORTANA" in fake.calls[0]["system"]


async def test_converse_refusal_and_empty_raise() -> None:
    client, _ = _conv_client([_response("", stop_reason="refusal")])
    with pytest.raises(ChatError):
        await client.converse(1, [{"role": "user", "content": "q"}])
    client2, _ = _conv_client([_response("   ")])
    with pytest.raises(ChatError):
        await client2.converse(1, [{"role": "user", "content": "q"}])


async def test_local_converse_sends_persona_plus_history(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse(_openai_reply("Chatting away."))

    monkeypatch.setattr("cortana.chat.urllib.request.urlopen", fake_urlopen)
    conv = ConversationConfig(
        enabled=True,
        backend="local",
        local_url="http://127.0.0.1:11434/v1/chat/completions",
        model="qwen2.5:7b",
        max_tokens=150,
        timeout_s=20.0,
    )
    holder = _DualHolder(ChatConfig(), conv)
    client = LocalChatClient(holder, config_section="conversation")  # type: ignore[arg-type]
    history = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    # Two rapid calls both succeed — no cooldown on the conversation lane.
    assert await client.converse(1, history) == "Chatting away."
    assert await client.converse(1, history) == "Chatting away."
    body = captured["body"]
    assert body["model"] == "qwen2.5:7b"
    assert body["max_tokens"] == 150
    assert body["messages"][0]["role"] == "system"  # the persona is prepended
    assert body["messages"][1:] == history
    assert captured["timeout"] == 20.0


async def test_local_converse_unconfigured_url_is_chat_error() -> None:
    conv = ConversationConfig(enabled=True, backend="local", local_url="")
    holder = _DualHolder(ChatConfig(), conv)
    client = LocalChatClient(holder, config_section="conversation")  # type: ignore[arg-type]
    with pytest.raises(ChatError):
        await client.converse(1, [{"role": "user", "content": "hi"}])
