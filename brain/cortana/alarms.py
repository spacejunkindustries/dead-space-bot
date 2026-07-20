"""AlarmBus — every "I dropped something the corp expects" event, one surface.

GDD §11.3. CORTANA's failure modes used to speak three different languages:
some posted one-shot lines to #bot-health, some only wrote journald, and some
said nothing at all. The bus replaces all of them with one contract:

- A **closed** :class:`AlarmCode` enum — a failure class that matters gets a
  code or it does not get raised. No stringly-typed alarm names.
- **One edited-in-place card per active ``(code, key)``** in #bot-health:
  first-seen, last-seen, occurrence count, and a phone-readable fix hint.
  Repeats edit the card (the incident-card invariant, constraint 9, applied
  to ops); :meth:`clear` edits it to a ✅ resolved state instead of deleting
  it, so the operator sees that it broke AND that it recovered.
- **Message ids persisted in ``app_state``** under ``alarm:<code>:<key>`` so
  a Brain restart re-adopts the existing card — restarts never duplicate.
- **Code-first journald mirror**: the structured log *event* is the alarm
  code (``journalctl -u cortana-brain | grep EARS_DOWN``), raised and cleared.

Discord I/O goes through injected async ``send``/``edit`` callables (the
composition root wraps the health channel); both are expected to return
``None``/``False`` while Discord is not ready or the channel is unwritable.
The bus never crashes on that — the card stays *dirty* and :meth:`flush`
(called from the health loop) retries until it lands. Raising an alarm from
any startup path is therefore always safe.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

import structlog

from cortana.core import db

__all__ = ["AlarmBus", "AlarmCode", "AlarmSeverity", "INTERACTION_ERROR_ALARM_AFTER"]

log = structlog.get_logger(__name__)

#: Repeated interaction-handler errors on the same command before the
#: INTERACTION_ERRORS card is raised (a single 5xx is noise; three are a bug).
INTERACTION_ERROR_ALARM_AFTER = 3


class AlarmCode(Enum):
    """The closed set of operator alarms. Adding a failure class means adding
    a code here — there is no side door."""

    ROUTING_ZERO_RULES = "ROUTING_ZERO_RULES"
    ROLE_UNRESOLVED = "ROLE_UNRESOLVED"
    CHANNEL_UNWRITABLE = "CHANNEL_UNWRITABLE"
    WAKE_FAULTED = "WAKE_FAULTED"
    STT_DEGRADED = "STT_DEGRADED"
    EARS_DOWN = "EARS_DOWN"
    CONFIG_RESTART_PENDING = "CONFIG_RESTART_PENDING"
    TREE_SYNC_STALE = "TREE_SYNC_STALE"
    TIMER_UNDELIVERED = "TIMER_UNDELIVERED"
    INTERACTION_ERRORS = "INTERACTION_ERRORS"
    POST_FAILURE = "POST_FAILURE"
    VOICE_ABSENT = "VOICE_ABSENT"
    # Add-on module supervision (dead/ kernel). CORTANA never raises these —
    # its failures are fatal (systemd restart), not contained.
    MODULE_SETUP_FAILED = "MODULE_SETUP_FAILED"
    MODULE_TASK_DEGRADED = "MODULE_TASK_DEGRADED"
    MODULE_QUARANTINED = "MODULE_QUARANTINED"


class AlarmSeverity(Enum):
    WARNING = "warning"
    CRITICAL = "critical"


_COLOR_WARNING = 0xF1C40F
_COLOR_CRITICAL = 0xE74C3C
_COLOR_RESOLVED = 0x2ECC71

#: ``send(content, embed) -> (channel_id, message_id) | None`` — None means
#: "could not post right now" (not ready / unwritable); the bus retries.
SendFn = Callable[[str, dict[str, Any] | None], Awaitable[tuple[int, int] | None]]
#: ``edit(channel_id, message_id, content, embed) -> True | None | False``:
#: True = landed; None = transient failure (not ready / REST error), retry
#: the edit; False = the message is GONE (deleted), re-post a fresh card.
EditFn = Callable[[int, int, str, dict[str, Any] | None], Awaitable[bool | None]]


@dataclass(slots=True)
class ActiveAlarm:
    """One live (or resolving) alarm card."""

    code: AlarmCode
    key: str | None
    severity: AlarmSeverity
    summary: str
    fix_hint: str
    first_seen: int
    last_seen: int
    count: int
    channel_id: int | None = None
    message_id: int | None = None
    resolved: bool = False
    #: True when the Discord card reflects the current state.
    synced: bool = False


def _state_key(code: AlarmCode, key: str | None) -> str:
    return f"alarm:{code.value}:{key or ''}"


class AlarmBus:
    """Raise / clear operator alarms; owns the #bot-health card lifecycle."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        send: SendFn,
        edit: EditFn,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._conn = conn
        self._send = send
        self._edit = edit
        self._clock = clock
        self._alarms: dict[str, ActiveAlarm] = {}
        #: app_state rows from a previous process lifetime, adopted lazily.
        self._persisted: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._interaction_errors: dict[str, int] = {}
        # Serialises card delivery: two concurrent raises of one (code, key)
        # — e.g. two /hostiles both hitting POST_FAILURE — must not both see
        # message_id None mid-send and double-post the card (which would
        # orphan the first as a permanently-active 🚨 embed).
        self._card_lock = asyncio.Lock()

    # ── the read surface (/botstatus) ────────────────────────────────────────

    def active(self) -> tuple[ActiveAlarm, ...]:
        """Unresolved alarms, oldest first."""
        live = [a for a in self._alarms.values() if not a.resolved]
        return tuple(sorted(live, key=lambda a: a.first_seen))

    def active_count(self) -> int:
        return sum(1 for a in self._alarms.values() if not a.resolved)

    # ── raise / clear ────────────────────────────────────────────────────────

    async def raise_alarm(
        self,
        code: AlarmCode,
        severity: AlarmSeverity,
        summary: str,
        fix_hint: str,
        key: str | None = None,
    ) -> None:
        """Raise (or re-raise) an alarm. Idempotent per ``(code, key)``: the
        first raise posts a card, repeats edit it in place. Never raises."""
        await self._ensure_loaded()
        now = int(self._clock())
        sk = _state_key(code, key)
        alarm = self._alarms.get(sk)
        if alarm is None or alarm.resolved:
            persisted = self._persisted.pop(sk, {})
            alarm = ActiveAlarm(
                code=code,
                key=key,
                severity=severity,
                summary=summary,
                fix_hint=fix_hint,
                first_seen=int(persisted.get("first_seen", now)),
                last_seen=now,
                count=int(persisted.get("count", 0)) + 1,
                channel_id=persisted.get("channel_id"),
                message_id=persisted.get("message_id"),
            )
            if self._alarms.get(sk) is not None:  # re-raise of a resolving card
                prior = self._alarms[sk]
                alarm.channel_id = prior.channel_id
                alarm.message_id = prior.message_id
                alarm.first_seen = prior.first_seen
                alarm.count = prior.count + 1
            self._alarms[sk] = alarm
        else:
            alarm.count += 1
            alarm.last_seen = now
            alarm.severity = severity
            alarm.summary = summary
            alarm.fix_hint = fix_hint
        alarm.resolved = False
        alarm.synced = False
        # Journald mirror, code-first: the log EVENT is the alarm code.
        log.warning(
            code.value,
            alarm="raised",
            key=key,
            severity=severity.value,
            count=alarm.count,
            summary=summary,
        )
        await self._sync(sk, alarm)

    async def clear(self, code: AlarmCode, key: str | None = None) -> None:
        """Resolve an alarm: the card is edited to a ✅ state and the
        persisted ids dropped. A clear with no matching alarm (this lifetime
        OR persisted from a previous one) is a silent no-op. Never raises."""
        await self._ensure_loaded()
        sk = _state_key(code, key)
        alarm = self._alarms.get(sk)
        if alarm is None:
            persisted = self._persisted.pop(sk, None)
            if persisted is None:
                return
            now = int(self._clock())
            # Adopt the previous lifetime's card just to resolve it — e.g.
            # CONFIG_RESTART_PENDING after the restart that applied it.
            alarm = ActiveAlarm(
                code=code,
                key=key,
                severity=AlarmSeverity.WARNING,
                summary="(raised before the last restart)",
                fix_hint="",
                first_seen=int(persisted.get("first_seen", now)),
                last_seen=now,
                count=int(persisted.get("count", 1)),
                channel_id=persisted.get("channel_id"),
                message_id=persisted.get("message_id"),
            )
            self._alarms[sk] = alarm
        elif alarm.resolved:
            return
        alarm.resolved = True
        alarm.synced = False
        log.info(code.value, alarm="cleared", key=key, count=alarm.count)
        await self._sync(sk, alarm)

    async def record_interaction_error(self, name: str) -> None:
        """Count one interaction-handler failure for ``name`` (command or
        component); raises INTERACTION_ERRORS keyed by name once the count
        passes :data:`INTERACTION_ERROR_ALARM_AFTER`."""
        count = self._interaction_errors.get(name, 0) + 1
        self._interaction_errors[name] = count
        log.warning("interaction_error_recorded", name=name, count=count)
        if count >= INTERACTION_ERROR_ALARM_AFTER:
            await self.raise_alarm(
                AlarmCode.INTERACTION_ERRORS,
                AlarmSeverity.WARNING,
                f"`{name}` has failed {count} times — pilots are getting error replies.",
                "check the journal for the traceback; a /reload or restart may clear it",
                key=name,
            )

    async def flush(self) -> None:
        """Retry Discord delivery for every dirty card (called periodically —
        this is what makes pre-ready raises safe). Never raises."""
        await self._ensure_loaded()
        for sk, alarm in list(self._alarms.items()):
            if not alarm.synced:
                await self._sync(sk, alarm)

    # ── internals ────────────────────────────────────────────────────────────

    async def _ensure_loaded(self) -> None:
        """Load persisted card ids once per process — restarts adopt, never
        duplicate."""
        if self._loaded:
            return
        self._loaded = True
        try:
            rows = await asyncio.to_thread(
                db.query,
                self._conn,
                "SELECT key, value FROM app_state WHERE key LIKE 'alarm:%'",
            )
        except Exception:
            log.exception("alarm_state_load_failed")
            return
        for row in rows:
            try:
                self._persisted[row["key"]] = json.loads(row["value"])
            except (ValueError, TypeError):
                log.warning("alarm_state_row_invalid", key=row["key"])

    async def _sync(self, sk: str, alarm: ActiveAlarm) -> None:
        """Push one card's current state to Discord; on failure the card
        stays dirty for :meth:`flush`. Never raises.

        Serialised on ``_card_lock``: the message-id check and the send/edit
        it gates must be atomic against concurrent syncs, or two raises in
        flight both post and break the one-card invariant. A sync queued
        behind another re-reads the (shared, mutated) alarm state, so it
        edits the freshly posted card instead of posting a second one."""
        async with self._card_lock:
            await self._sync_locked(sk, alarm)

    async def _sync_locked(self, sk: str, alarm: ActiveAlarm) -> None:
        embed = self._embed(alarm)
        try:
            if alarm.message_id is not None and alarm.channel_id is not None:
                edited = await self._edit(alarm.channel_id, alarm.message_id, "", embed)
                if edited is None:
                    log.info("alarm_card_edit_pending", key=sk)
                    return
                if edited is False:
                    # The card was deleted — re-post below. Still ONE live
                    # card per alarm (the incident-card invariant).
                    log.warning("alarm_card_lost", key=sk)
                    alarm.channel_id = None
                    alarm.message_id = None
            if alarm.message_id is None or alarm.channel_id is None:
                posted = await self._send("", embed)
                if posted is None:
                    log.info("alarm_card_pending", key=sk)
                    return
                alarm.channel_id, alarm.message_id = posted
        except Exception:
            log.exception("alarm_card_delivery_failed", key=sk)
            return
        alarm.synced = True
        try:
            if alarm.resolved:
                await asyncio.to_thread(
                    db.execute, self._conn, "DELETE FROM app_state WHERE key = ?", (sk,)
                )
                self._alarms.pop(sk, None)
            else:
                value = json.dumps(
                    {
                        "channel_id": alarm.channel_id,
                        "message_id": alarm.message_id,
                        "first_seen": alarm.first_seen,
                        "count": alarm.count,
                    }
                )
                await asyncio.to_thread(
                    db.execute,
                    self._conn,
                    "INSERT INTO app_state (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (sk, value),
                )
        except Exception:
            log.exception("alarm_state_persist_failed", key=sk)

    def _embed(self, alarm: ActiveAlarm) -> dict[str, Any]:
        """The card as a ``discord.Embed.from_dict`` payload — phone-sized."""
        suffix = f" — {alarm.key}" if alarm.key else ""
        if alarm.resolved:
            title = f"✅ {alarm.code.value}{suffix} — resolved"
            color = _COLOR_RESOLVED
        elif alarm.severity is AlarmSeverity.CRITICAL:
            title = f"🚨 {alarm.code.value}{suffix}"
            color = _COLOR_CRITICAL
        else:
            title = f"⚠️ {alarm.code.value}{suffix}"
            color = _COLOR_WARNING
        description = alarm.summary
        if alarm.fix_hint and not alarm.resolved:
            description += f"\n\n**Fix:** {alarm.fix_hint}"
        fields = [
            {"name": "First seen", "value": f"<t:{alarm.first_seen}:R>", "inline": True},
            {"name": "Last seen", "value": f"<t:{alarm.last_seen}:R>", "inline": True},
            {"name": "Count", "value": str(alarm.count), "inline": True},
        ]
        return {"title": title, "description": description, "color": color, "fields": fields}
