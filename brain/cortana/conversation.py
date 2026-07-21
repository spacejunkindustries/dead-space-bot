"""Conversation mode — freestyle back-and-forth with CORTANA (GDD §6.8).

Opt-in, OFF by default. When the fleet is idle and a pilot says something that
is NOT a fleet command (the grammar AND the §6.7 understanding brain both
missed), CORTANA chats back — banter, chit-chat, arithmetic — with wake-free
turn-taking. It is never on the command path (constraint 6): the residue
branch that reaches here sits strictly below the grammar-command dispatch, so
a real callout always preempts and routes through the deterministic path.

**The hard wall (constraints 9 + 11).** :class:`ConversationManager` is built
with NO :class:`~cortana.core.incidents.IncidentEngine`, no routing, and no
``decide_mentions`` handle — structurally it cannot post a card or mint a
mention. Its only product is a string the engine speaks (and, for an over-long
reply, an optional channel post the engine forces to ``AllowedMentions.none()``).
The signature is pinned by a test so the wall can never be quietly widened.

The turn/TTL/cooldown budget lives here, on a counter separate from the dialog
failure-retry budget: each spoken reply spends one of ``max_turns``, a stale
thread is dropped after ``session_ttl_seconds``, and ``user_cooldown_s`` paces
a single pilot. Only a fresh wake refills the budget (the loop-safety
guarantee). Arithmetic is answered by the deterministic on-box evaluator
(:mod:`cortana.chat_math`) first, so numbers are exact, never hallucinated —
the same "the model never gets the dangerous power" posture as §6.7.

The backend is the §6.6 :class:`~cortana.chat.ChatBackend` abstraction, reused:
a conversation-purposed client reads the ``conversation.*`` config lane (local
qwen2.5:7b by default — free, on-box), so a corp can run chit-chat local while
the override channel stays cloud.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from cortana import chat_math

if TYPE_CHECKING:
    from cortana.chat import ChatBackend
    from cortana.config import ConfigHolder, ConversationConfig

log = structlog.get_logger(__name__)

__all__ = [
    "ConversationManager",
    "ConversationSession",
    "conversation_available",
    "ops_quiet",
]

#: One turn of history is a user message plus CORTANA's reply.
_ENTRIES_PER_EXCHANGE = 2


@dataclass(slots=True)
class ConversationSession:
    """One pilot's live conversation thread.

    Owned and mutated in place by :class:`ConversationManager`. ``expires_at``
    / ``last_turn_at`` are monotonic-clock stamps (the manager's injected
    clock); ``history`` is the rolling ``{role, content}`` transcript replayed
    to the model, trimmed to ``max_history_turns`` exchanges."""

    user_id: int
    guild_id: int
    turns_left: int
    expires_at: float
    history: list[dict[str, str]] = field(default_factory=list)
    last_turn_at: float | None = None


def ops_quiet(
    now: datetime,
    last_incident_at: datetime | None,
    *,
    enabled: bool,
    window_min: int,
) -> bool:
    """Whether banter should stay silent because ops are live/recent (§6.8).

    Pure and testable. ``True`` only when the quiet-during-ops guard is on AND
    an incident was active within ``window_min`` of ``now``. No incident (or a
    stale one beyond the window, or the guard off) → ``False`` (chat allowed)."""
    if not enabled or last_incident_at is None:
        return False
    return now - last_incident_at <= timedelta(minutes=window_min)


def conversation_available(
    cfg: ConversationConfig,
    *,
    backend_live: bool,
    ops_quiet: bool,  # noqa: A002 — mirrors the predicate name for call-site clarity
) -> bool:
    """The one gate the dialog machine reads (as ``Classified.conversation_available``).

    Pure: chat may claim residue only when the feature is enabled, a backend is
    live, and ops are not in progress. Off/half-set/ops-live all collapse to
    ``False``, which makes the residue branch byte-for-byte the old path."""
    return cfg.enabled and backend_live and not ops_quiet


class ConversationManager:
    """Per-speaker chat sessions + the turn/TTL/cooldown budget (GDD §6.8).

    Shared by the DialogEngine (voice) and the ``/chat`` slash twin, so a
    pilot's voice and slash turns ride ONE rolling history (constraint 10).
    Constructed with NO incident/routing/mention handle — the hard wall."""

    def __init__(
        self,
        holder: ConfigHolder,
        *,
        conversation_provider: Callable[[], tuple[ChatBackend | None, str]],
        last_incident_at: Callable[[int], datetime | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._holder = holder
        #: Returns ``(backend, status)`` — the conversation-lane client, rebuilt
        #: on reload by ``App._refresh_conversation``. Never an incident sink.
        self._provider = conversation_provider
        #: Read-only accessor for the guild's most-recent active incident time,
        #: feeding the ops-quiet predicate. Read-ONLY: it can observe an
        #: incident, never create or touch one (constraint 9 held structurally).
        self._last_incident_at = last_incident_at
        self._clock = clock
        self._sessions: dict[int, ConversationSession] = {}

    # ── read-only gates (the engine consults these before ever converse-ing) ──

    def backend_live(self) -> bool:
        """A conversation backend is configured and up right now."""
        backend, _status = self._provider()
        return backend is not None

    def ops_quiet(self, guild_id: int) -> bool:
        """Whether banter should stay silent for this guild (ops live/recent)."""
        cfg = self._holder.current.conversation
        last = self._last_incident_at(guild_id) if self._last_incident_at is not None else None
        return ops_quiet(
            datetime.now(UTC),
            last,
            enabled=cfg.quiet_during_ops,
            window_min=cfg.quiet_window_min,
        )

    def active(self, user_id: int) -> bool:
        """Does this pilot have a live session with turns left?

        The engine gates ``CONVERSE_ARM`` on this — a turn-taking window is
        re-armed only while the budget holds, so the wake-free window cannot
        self-sustain (loop-safety)."""
        session = self._sessions.get(user_id)
        if session is None:
            return False
        return self._clock() <= session.expires_at and session.turns_left > 0

    def reset_user(self, user_id: int) -> None:
        """Purge one pilot's conversation thread (voice reset / Ears reconnect)."""
        self._sessions.pop(user_id, None)

    # ── the one turn ──────────────────────────────────────────────────────────

    async def reply(self, user_id: int, guild_id: int, text: str) -> str | None:
        """Produce CORTANA's reply to one non-command utterance, or ``None``.

        ``None`` — spoken nothing, arm nothing — when: the feature is off, no
        backend is live, the turn/TTL budget is spent, the pilot spoke again
        inside ``user_cooldown_s``, or the backend failed/returned empty. A
        distress call can never be lost here because this path handles ONLY
        residue the grammar already declined (constraint 6).

        Arithmetic is answered by the deterministic evaluator first — an exact
        number that never touches the model. Everything else replays the rolling
        history and calls the backend."""
        cfg = self._holder.current.conversation
        if not cfg.enabled:
            return None
        backend, _status = self._provider()
        if backend is None:
            return None
        now = self._clock()
        session = self._sessions.get(user_id)
        if session is None or now > session.expires_at:
            # A fresh thread: full budget, empty history. Expiry starting a new
            # session is how a stale thread never bleeds into the next chat.
            session = ConversationSession(
                user_id=user_id,
                guild_id=guild_id,
                turns_left=cfg.max_turns,
                expires_at=now + cfg.session_ttl_seconds,
            )
            self._sessions[user_id] = session
        # Cooldown: a too-soon turn is dropped WITHOUT spending the budget —
        # light anti-spam that keeps the back-and-forth fluid.
        if session.last_turn_at is not None and now - session.last_turn_at < cfg.user_cooldown_s:
            log.info("conversation_cooldown", user_id=user_id)
            return None
        if session.turns_left <= 0:
            log.info("conversation_turns_exhausted", user_id=user_id)
            return None

        reply_text: str | None = None
        if cfg.math_tool:
            calc = chat_math.try_calc(text)
            if calc is not None:
                reply_text = calc
                log.info("conversation_math", user_id=user_id, answer=calc)
        if reply_text is None:
            messages = self._history_window(session, cfg) + [{"role": "user", "content": text}]
            try:
                reply_text = await backend.converse(user_id, messages)
            except Exception as exc:  # noqa: BLE001 — comms stay clean; drop silently
                log.warning("conversation_backend_failed", user_id=user_id, error=str(exc))
                return None
            if not reply_text or not reply_text.strip():
                return None
            reply_text = reply_text.strip()

        # Commit the turn: append to the rolling history, spend a turn, refresh
        # the TTL, and stamp the cooldown clock.
        session.history.append({"role": "user", "content": text})
        session.history.append({"role": "assistant", "content": reply_text})
        self._trim_history(session, cfg)
        session.turns_left -= 1
        session.last_turn_at = now
        session.expires_at = now + cfg.session_ttl_seconds
        return reply_text

    # ── history plumbing ──────────────────────────────────────────────────────

    @staticmethod
    def _history_window(
        session: ConversationSession, cfg: ConversationConfig
    ) -> list[dict[str, str]]:
        """The prior exchanges to replay as context (``max_history_turns`` of
        them; 0 = stateless)."""
        if cfg.max_history_turns <= 0:
            return []
        keep = cfg.max_history_turns * _ENTRIES_PER_EXCHANGE
        return list(session.history[-keep:])

    @staticmethod
    def _trim_history(session: ConversationSession, cfg: ConversationConfig) -> None:
        """Bound the stored history so a long session can't grow without limit."""
        if cfg.max_history_turns <= 0:
            session.history.clear()
            return
        keep = cfg.max_history_turns * _ENTRIES_PER_EXCHANGE
        if len(session.history) > keep:
            del session.history[:-keep]
