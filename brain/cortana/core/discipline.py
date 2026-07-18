"""Notification discipline — GDD §11.1 layered defences.

Pure state machine over *injected* ``now`` datetimes: nothing in this module
reads the wall clock, so every behaviour is deterministic under test. The
incident engine is the sole caller and runs on the event loop; the class is
not thread-safe and does not need to be.

Layers implemented here:

- **Per-user cooldown** — one mention per pilot per ``discipline.user_cooldown_s``.
- **Global circuit breaker** — more than ``circuit_breaker.max_mentions``
  mentions inside ``circuit_breaker.window_min`` minutes opens the breaker:
  all mentions are suppressed until the window slides past the flood.
  Incidents keep being posted (the engine strips mentions from the routing
  decision instead of dropping the card). The "flood control active" notice
  is emitted exactly once per open episode via :meth:`should_announce_flood`.
- **Pilot-role gate** — only members holding the ``@Pilot`` role may trigger
  mentions (:meth:`may_mention`).
- **Fleet-ops mode** — while enabled, voice-triggered commands are accepted
  only from the FC role; slash commands are unaffected (:meth:`check`).

The dedupe window (layer 1) lives in the incident engine and quiet hours
(layer 6) live in routing — see GDD §9.2 and §10.1.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Literal

import structlog

from cortana.config import ConfigHolder

__all__ = ["Discipline", "Source"]

log = structlog.get_logger(__name__)

#: Where a command arrived from. Fleet-ops mode gates only the voice path.
Source = Literal["voice", "slash"]


class Discipline:
    """Cooldowns, circuit breaker, pilot gate, and fleet-ops mode.

    All time-dependent methods take ``now`` explicitly; state advances only
    through those calls. Config is read from ``holder.current`` at the point
    of use so a SIGHUP reload takes effect immediately.
    """

    def __init__(self, holder: ConfigHolder) -> None:
        self._holder = holder
        self._last_mention: dict[int, datetime] = {}
        self._mentions: deque[datetime] = deque()
        self._fleetmode = False
        self._flood_announced = False

    # ── per-user cooldown + circuit breaker ──────────────────────────────────

    def allow_mention(self, user_id: int, now: datetime) -> bool:
        """True if a mention triggered by ``user_id`` may go out right now.

        False when the user's cooldown is still running OR the global circuit
        breaker is open. Does NOT record anything — call
        :meth:`record_mention` after the mention is actually sent.
        """
        if self.breaker_open(now):
            return False
        cooldown_s = self._holder.current.discipline.user_cooldown_s
        last = self._last_mention.get(user_id)
        return last is None or (now - last) >= timedelta(seconds=cooldown_s)

    def record_mention(self, user_id: int, now: datetime) -> None:
        """Record that a mention triggered by ``user_id`` was sent at ``now``."""
        self._last_mention[user_id] = now
        self._mentions.append(now)

    def breaker_open(self, now: datetime) -> bool:
        """True while more than ``max_mentions`` were sent in the last window.

        The breaker closes automatically once the sliding window drops the
        flood; closing also re-arms the one-shot flood announcement.
        """
        cb = self._holder.current.discipline.circuit_breaker
        cutoff = now - timedelta(minutes=cb.window_min)
        while self._mentions and self._mentions[0] < cutoff:
            self._mentions.popleft()
        is_open = len(self._mentions) > cb.max_mentions
        if not is_open and self._flood_announced:
            self._flood_announced = False
            log.info("circuit_breaker_closed", mentions_in_window=len(self._mentions))
        return is_open

    def should_announce_flood(self, now: datetime) -> bool:
        """True exactly once per breaker-open episode — the caller posts/speaks

        "flood control active" on True and never again until the breaker has
        closed and reopened.
        """
        if not self.breaker_open(now):
            return False
        if self._flood_announced:
            return False
        self._flood_announced = True
        log.warning("circuit_breaker_open", mentions_in_window=len(self._mentions))
        return True

    # ── role gates ────────────────────────────────────────────────────────────

    def may_mention(self, member_role_ids: Iterable[int]) -> bool:
        """Pilot-role gate (GDD §11.1 layer 4): only ``@Pilot`` triggers mentions."""
        pilot = self._holder.current.discord.roles.pilot
        return pilot in set(member_role_ids)

    def may_voice_trigger(self, member_role_ids: Iterable[int]) -> bool:
        """Fleet-ops gate (GDD §11.4): under fleetmode only the FC may voice-trigger."""
        if not self._fleetmode:
            return True
        fc = self._holder.current.discord.roles.fc
        return fc in set(member_role_ids)

    def check(self, member_role_ids: Iterable[int], source: Source) -> bool:
        """May a command from ``source`` proceed for a member with these roles?

        Slash commands are always allowed (GDD §11.4 — fleetmode restricts
        the *voice* path only); voice commands defer to :meth:`may_voice_trigger`.
        """
        if source == "slash":
            return True
        return self.may_voice_trigger(member_role_ids)

    # ── fleet-ops mode ────────────────────────────────────────────────────────

    def set_fleetmode(self, enabled: bool) -> None:
        """Toggle fleet-ops mode (``/fleetmode`` — GDD §11.4)."""
        self._fleetmode = enabled
        log.info("fleetmode_set", enabled=enabled)

    @property
    def fleetmode(self) -> bool:
        return self._fleetmode
