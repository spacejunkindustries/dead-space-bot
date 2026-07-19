"""The "command override" out-of-band assistant — GDD §6.6.

An explicitly-invoked chat channel: a pilot says *"command override, what's
the weather in Chicago?"* (or types ``/ask``) and the question goes to a
cloud Claude model. Constraint 6 (no LLM in the command path) stands
untouched — the incident grammar, the gazetteer, and the engine never see
this module, and this module never posts intel. It exists to make CORTANA feel
alive on demand, not to interpret reports.

Cost posture: the default model is the cheapest Claude tier and every reply
is capped at ``chat.max_tokens``; ``chat.user_cooldown_s`` throttles per
pilot, and web search (the expensive part) is limited to one search per
question. The API key rides systemd ``LoadCredential=`` (constraint 12) with
``chat.api_key_file`` as the 0600 dev fallback, mirroring the Discord token.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

import structlog

from cortana.config import ConfigHolder

log = structlog.get_logger(__name__)

__all__ = [
    "ChatBackend",
    "ChatClient",
    "ChatCooldownError",
    "ChatError",
    "LocalChatClient",
    "read_api_key",
]

#: Server-tool web-search continuation cap: the API may pause a long
#: server-tool turn (stop_reason "pause_turn"); we resume at most this many
#: times before returning whatever we have.
_MAX_CONTINUATIONS = 3

#: The persona. Short replies, spoken aloud over comms — no markdown, no
#: lists. The one hard rule: never invent in-game intel; that comes from
#: CORTANA's own incident engine, not from a language model (constraint 6).
_SYSTEM = (
    "You are CORTANA, the ship's AI of the DEAD corporation's fleet in EVE "
    "Echoes — calm, capable, lightly wry, loyal to the crew. A pilot has "
    "opened the out-of-band channel ('command override') and asked you a "
    "question relayed from voice comms. Answer in one to three short spoken "
    "sentences — no markdown, no lists, no URLs. If you searched the web, "
    "give the concrete answer, not the sources. Never invent in-game fleet "
    "intel (hostiles, system status, timers): that intel comes only from "
    "CORTANA's own incident reports, so say so and move on."
)


class ChatError(Exception):
    """The override channel could not produce an answer."""


class ChatCooldownError(ChatError):
    """This pilot asked again too soon (``chat.user_cooldown_s``)."""


class ChatBackend(Protocol):
    """The override lane's one method (GDD §6.6). Both the cloud
    (:class:`ChatClient`) and on-box (:class:`LocalChatClient`) backends satisfy
    it, so the dialog engine and the ``/ask`` twin never care which is live."""

    async def ask(self, user_id: int, query: str) -> str:
        """One question in, one short spoken-ready answer out. Raises
        :class:`ChatCooldownError` on throttle, :class:`ChatError` otherwise."""
        ...


def read_api_key(api_key_file: str) -> str | None:
    """Anthropic API key: ``$CREDENTIALS_DIRECTORY/anthropic`` (systemd
    ``LoadCredential=``, constraint 12) first, then the 0600 dev-fallback
    file. ``None`` when neither exists — the channel then stays disabled."""
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred_dir:
        cred = Path(cred_dir) / "anthropic"
        try:
            if cred.is_file():
                key = cred.read_text(encoding="utf-8").strip()
                if key:
                    log.info("chat_key_loaded", source=str(cred))
                    return key
        except OSError as exc:
            log.warning("chat_key_unreadable", source=str(cred), error=str(exc))
    fallback = Path(api_key_file)
    try:
        if fallback.is_file():
            key = fallback.read_text(encoding="utf-8").strip()
            if key:
                log.info("chat_key_loaded", source=api_key_file)
                return key
    except OSError as exc:
        # The dev-fallback file is root-owned when only LoadCredential= is
        # meant to read it; a PermissionError here must degrade to "channel
        # off", never crash Brain's startup (which is what an unhandled read
        # error did when a service-file reinstall dropped LoadCredential=).
        log.warning("chat_key_unreadable", source=api_key_file, error=str(exc))
    return None


def _web_search_tool(model: str) -> dict[str, Any]:
    """One web search per question. The dynamic-filtering variant needs an
    Opus 4.6+/Sonnet 4.6+ class model; Haiku uses the basic variant."""
    lowered = model.lower()
    basic = "haiku" in lowered or "4-5" in lowered
    return {
        "type": "web_search_20250305" if basic else "web_search_20260209",
        "name": "web_search",
        "max_uses": 1,
    }


class ChatClient:
    """Thin async wrapper over the Anthropic Messages API for one-shot
    override questions. No conversation memory — each question stands alone
    (memory would grow token spend every turn; the corp asked for cheap)."""

    def __init__(self, holder: ConfigHolder, api_key: str) -> None:
        self._holder = holder
        self._api_key = api_key
        self._client: Any = None
        self._lock = asyncio.Lock()
        # Shared per-pilot throttle — voice path and /ask slash twin ride the
        # same clock (constraint 10 parity), so the cooldown can't be dodged
        # by switching input surface.
        self._last_ask: dict[int, float] = {}

    def _sdk(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic  # lazy: keep import cost off startup

            self._client = AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def ask(self, user_id: int, query: str) -> str:
        """One question in, one short spoken-ready answer out.

        Serialized: one in-flight question at a time keeps spend predictable
        and the droplet calm. Raises :class:`ChatCooldownError` when the pilot
        asked again too soon, :class:`ChatError` on any other failure —
        callers speak/post a fixed line instead of surfacing errors.
        """
        cfg = self._holder.current.chat
        now = time.monotonic()
        last = self._last_ask.get(user_id)
        if last is not None and now - last < cfg.user_cooldown_s:
            raise ChatCooldownError(f"cooldown: {cfg.user_cooldown_s}s per pilot")
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "system": _SYSTEM,
        }
        if cfg.web_search:
            kwargs["tools"] = [_web_search_tool(cfg.model)]

        messages: list[dict[str, Any]] = [{"role": "user", "content": query}]
        async with self._lock:
            try:
                for _ in range(_MAX_CONTINUATIONS):
                    response = await self._sdk().messages.create(messages=messages, **kwargs)
                    if response.stop_reason != "pause_turn":
                        break
                    # Server-tool turn paused mid-search — resume it.
                    messages = [
                        {"role": "user", "content": query},
                        {"role": "assistant", "content": response.content},
                    ]
            except Exception as exc:  # SDK errors: auth, rate limit, network
                raise ChatError(str(exc)) from exc

        if response.stop_reason == "refusal":
            raise ChatError("model declined the request")
        text = " ".join(
            block.text.strip()
            for block in response.content
            if getattr(block, "type", "") == "text" and block.text.strip()
        ).strip()
        if not text:
            raise ChatError("empty reply")
        # Cooldown arms only on SUCCESS: a failed request must not convert a
        # pilot's immediate retry into "Override cooling down" — during an
        # outage that masks the real error behind a throttle message.
        self._last_ask[user_id] = time.monotonic()
        log.info(
            "override_answered",
            model=cfg.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return text


#: On-box persona. Same voice as the cloud one, minus the web-search clause a
#: local model has no tool for. Short spoken lines, and the one hard rule holds
#: identically: never invent in-game intel (constraint 6 — intel is the
#: incident engine's alone).
_SYSTEM_LOCAL = (
    "You are CORTANA, the ship's AI of the DEAD corporation's fleet in EVE "
    "Echoes — calm, capable, lightly wry, loyal to the crew. A pilot has opened "
    "the out-of-band channel ('command override') and is talking to you over "
    "voice comms. Answer in one to three short spoken sentences — no markdown, "
    "no lists, no URLs. Never invent in-game fleet intel (hostiles, system "
    "status, timers): that intel comes only from CORTANA's own incident "
    "reports, so say so and move on."
)


class LocalChatClient:
    """On-box override backend — an OpenAI-compatible chat-completions server
    (llama.cpp's ``server``, Ollama, …) at ``chat.local_url``.

    The SLM lane: conversational back-and-forth that runs entirely on the
    droplet — no API key, no per-question cost, no network egress. Same
    :meth:`ask` contract and per-pilot cooldown as :class:`ChatClient`
    (constraint 10 parity: voice and ``/ask`` ride one clock), so the engine
    cannot tell the backends apart. Constraint 6 stands untouched — this never
    sees the incident grammar and never posts intel.

    Blocking HTTP rides ``asyncio.to_thread`` (stdlib ``urllib``; the class is
    otherwise async), mirroring the whisper.cpp backend — no extra dependency,
    nothing on the event-loop thread."""

    def __init__(self, holder: ConfigHolder) -> None:
        self._holder = holder
        self._lock = asyncio.Lock()
        self._last_ask: dict[int, float] = {}

    async def ask(self, user_id: int, query: str) -> str:
        cfg = self._holder.current.chat
        now = time.monotonic()
        last = self._last_ask.get(user_id)
        if last is not None and now - last < cfg.user_cooldown_s:
            raise ChatCooldownError(f"cooldown: {cfg.user_cooldown_s}s per pilot")
        if not cfg.local_url:
            raise ChatError("chat.local_url is not configured")
        async with self._lock:
            text = await asyncio.to_thread(
                _local_completion,
                cfg.local_url,
                cfg.model,
                query,
                cfg.max_tokens,
                cfg.timeout_s,
            )
        if not text:
            raise ChatError("empty reply")
        # Cooldown arms only on SUCCESS (see ChatClient): a failed request must
        # not convert a pilot's retry into "cooling down".
        self._last_ask[user_id] = time.monotonic()
        log.info("override_answered_local", model=cfg.model)
        return text


def _local_completion(url: str, model: str, query: str, max_tokens: int, timeout_s: float) -> str:
    """POST one OpenAI-style chat completion and return the reply text.

    Sync + stdlib-only (runs in a worker thread). Every transport/shape error
    surfaces as the :class:`ChatError` the caller contract promises — the pilot
    hears a fixed line, never a raw traceback on comms."""
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_LOCAL},
                {"role": "user", "content": query},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.6,
            "stream": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # OSError covers URLError/TimeoutError/ConnectionReset; HTTPException the
        # mid-request drops urllib doesn't wrap; ValueError a bad JSON body.
        raise ChatError(f"local chat request failed: {exc}") from exc
    try:
        message = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ChatError(f"local chat response malformed: {exc}") from exc
    return str(message).strip()
