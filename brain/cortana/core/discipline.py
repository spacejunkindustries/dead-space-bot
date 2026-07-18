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

import json
from collections import deque
from collections.abc import Callable, Iterable
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

    The class stays pure (no I/O): durability is provided by the composition
    root, which restores :meth:`snapshot` output at startup and persists a
    fresh snapshot whenever :attr:`on_state_change` fires — so a mid-flood
    restart does not close the breaker and ``/fleetmode on`` survives a
    restart instead of silently reopening voice to everyone mid-op.
    """

    def __init__(self, holder: ConfigHolder) -> None:
        self._holder = holder
        self._last_mention: dict[int, datetime] = {}
        self._mentions: deque[datetime] = deque()
        self._fleetmode = False
        self._flood_announced = False
        #: Fired (sync, no args) after every persistent-state mutation —
        #: fleetmode toggles and mention (un)records. The composition root
        #: wires this to schedule an ``app_state`` snapshot write; ``None``
        #: (tests, seeder) means no persistence.
        self.on_state_change: Callable[[], None] | None = None

    # ── durability snapshot (restored by the composition root) ───────────────

    def snapshot(self) -> str:
        """JSON snapshot of the persistent state: fleetmode, the breaker
        window, and per-user cooldowns. Stale entries are pruned on the read
        paths after restore, so the snapshot is a plain dump."""
        return json.dumps(
            {
                "fleetmode": self._fleetmode,
                "last_mention": {str(u): t.isoformat() for u, t in self._last_mention.items()},
                "mentions": [t.isoformat() for t in self._mentions],
            },
            separators=(",", ":"),
        )

    def restore(self, snapshot: str) -> None:
        """Restore a :meth:`snapshot`. Tolerant: unreadable state is discarded
        with a warning — discipline state is a blast-radius bound, never worth
        failing startup over."""
        try:
            data = json.loads(snapshot)
            fleetmode = bool(data.get("fleetmode", False))
            last_mention = {
                int(user_id): datetime.fromisoformat(at)
                for user_id, at in dict(data.get("last_mention", {})).items()
            }
            mentions = deque(sorted(datetime.fromisoformat(at) for at in data.get("mentions", [])))
        except (ValueError, TypeError, AttributeError) as exc:
            log.warning("discipline_snapshot_unreadable", error=str(exc))
            return
        self._fleetmode = fleetmode
        self._last_mention = last_mention
        self._mentions = mentions
        log.info(
            "discipline_state_restored",
            fleetmode=fleetmode,
            mentions_in_window=len(mentions),
            cooldowns=len(last_mention),
        )

    def _notify(self) -> None:
        if self.on_state_change is not None:
            self.on_state_change()

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
        self._notify()

    def last_mention_at(self, user_id: int) -> datetime | None:
        """This user's recorded cooldown anchor — captured by the engine
        before :meth:`record_mention` so a failed post can roll the charge
        back with :meth:`unrecord_mention`."""
        return self._last_mention.get(user_id)

    def unrecord_mention(self, user_id: int, now: datetime, previous: datetime | None) -> None:
        """Roll back one :meth:`record_mention` charge (the post failed).

        ``now`` is the exact timestamp that was recorded; ``previous`` is the
        user's cooldown anchor from :meth:`last_mention_at` captured before
        the charge. A phantom mention from a failed post must never run a
        cooldown or open the breaker — but the charge itself happens under
        the engine lock *before* delivery so concurrent reports still
        serialize against the cooldown.
        """
        try:
            self._mentions.remove(now)
        except ValueError:  # pragma: no cover — double rollback; nothing to undo
            return
        if previous is None:
            self._last_mention.pop(user_id, None)
        else:
            self._last_mention[user_id] = previous
        self._notify()

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
        """Pilot-role gate (GDD §11.1 layer 4): only ``@Pilot`` triggers mentions.

        An unconfigured pilot role (0) means the gate is off — anyone may
        trigger mentions. The circuit breaker and cooldowns still bound the
        blast radius (§11.2/§11.3).
        """
        pilot = self._holder.current.discord.roles.pilot
        if pilot == 0:
            return True
        return pilot in set(member_role_ids)

    def may_voice_trigger(self, member_role_ids: Iterable[int]) -> bool:
        """Fleet-ops gate (GDD §11.4): under fleetmode only the FC may voice-trigger.

        An unconfigured FC role (0) means fleetmode cannot restrict anyone —
        the alternative (fleetmode silently blocking every voice command) is
        a trap for corps that never wired roles up.
        """
        if not self._fleetmode:
            return True
        fc = self._holder.current.discord.roles.fc
        if fc == 0:
            return True
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
        """Toggle fleet-ops mode (``/fleetmode`` — GDD §11.4). Persisted via
        :attr:`on_state_change` so a mid-op restart keeps the gate up."""
        self._fleetmode = enabled
        log.info("fleetmode_set", enabled=enabled)
        self._notify()

    @property
    def fleetmode(self) -> bool:
        return self._fleetmode
