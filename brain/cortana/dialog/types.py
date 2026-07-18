"""Dialog state machine vocabulary — states, events, actions, session.

Everything here is pure data. The machine (:mod:`cortana.dialog.machine`)
consumes and produces these; the engine (:mod:`cortana.dialog.engine`)
executes the actions against the real world. No I/O, no clocks.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Union

from cortana.types import MatchCandidate, ParsedCommand, Resolution, Severity

__all__ = [
    "Action",
    "ArmWindow",
    "Classified",
    "ConfirmPending",
    "DialogEvent",
    "DialogSession",
    "DialogState",
    "DisarmWindow",
    "Ev",
    "Line",
    "NoteRejected",
    "PendingConfirm",
    "PendingKind",
    "Relay",
    "Report",
    "RunOverride",
    "RunStt",
    "Speak",
]


class DialogState(Enum):
    """Where one pilot's dialog with CORTANA currently sits."""

    IDLE = "idle"
    #: A capture is open — the pilot is (or may be about to start) talking.
    LISTENING = "listening"
    #: The utterance is with STT / the incident engine.
    THINKING = "thinking"
    #: A wake-free window is armed for a LOW-tier "say again" system retry.
    AWAIT_RETRY_SYSTEM = "await_retry_system"
    #: A wake-free window is armed after a bare "code <colour>" opener.
    AWAIT_SEVERITY_REPORT = "await_severity_report"
    #: A wake-free window is armed after a bare "command override".
    AWAIT_OVERRIDE_QUESTION = "await_override_question"
    #: A wake-free window is armed after an unintelligible utterance.
    AWAIT_REPEAT = "await_repeat"
    #: A wake-free window is armed after a MEDIUM-tier "say again to confirm"
    #: (GDD §8.3): an affirmative or an exact repeat completes the command.
    AWAIT_CONFIRM = "await_confirm"


#: The AWAIT_* states — a wake-free window is armed in exactly these.
AWAIT_STATES = frozenset(
    {
        DialogState.AWAIT_RETRY_SYSTEM,
        DialogState.AWAIT_SEVERITY_REPORT,
        DialogState.AWAIT_OVERRIDE_QUESTION,
        DialogState.AWAIT_REPEAT,
        DialogState.AWAIT_CONFIRM,
    }
)


class PendingKind(Enum):
    """What the armed window / in-flight utterance is a continuation of."""

    NONE = "none"
    RETRY_SYSTEM = "retry_system"
    SEVERITY = "severity"
    OVERRIDE = "override"
    REPEAT = "repeat"
    CONFIRM = "confirm"


class Ev(Enum):
    """Event kinds fed to :func:`cortana.dialog.machine.transition`."""

    WAKE_HIT = "wake_hit"  # capture opened via the wake word
    WINDOW_OPENED = "window_opened"  # capture opened via an armed window
    CAPTURE_EMITTED = "capture_emitted"  # utterance PCM handed off
    CAPTURE_ABANDONED = "capture_abandoned"  # capture closed with zero speech
    STT_FAILED = "stt_failed"
    CLASSIFIED = "classified"  # STT + grammar produced a Classified
    ENGINE_REJECTED_LOW = "engine_rejected_low"  # report bounced at LOW tier
    ENGINE_ASKED = "engine_asked"  # MEDIUM tier: "say again to confirm"
    DEADLINE = "deadline"  # the armed window / AWAIT state timed out
    RESET = "reset"  # user left / Ears reconnected


@dataclass(frozen=True, slots=True)
class PendingConfirm:
    """One §8.3 confirm awaiting the pilot's answer: the command as parsed,
    the MEDIUM-tier candidate CORTANA read back, and — when the engine already
    posted an uncertain card — the incident whose pick button this mirrors.
    ``incident_id`` is ``None`` for destructive/scheduling commands
    (clear/timer/form up), which post nothing until confirmed."""

    parsed: ParsedCommand
    candidate: MatchCandidate
    incident_id: int | None = None


@dataclass(frozen=True, slots=True)
class Classified:
    """Everything the machine needs to know about one transcript.

    Built by the engine from grammar/config lookups so the machine itself
    stays free of imports and I/O. ``confident`` is the ``relay_min_logprob``
    verdict; ``chat_available`` is whether the §6.6 override channel is up.
    """

    text: str
    confident: bool
    override_query: str | None = None
    bare_override: bool = False
    bare_code: Severity | None = None
    parsed: ParsedCommand | None = None
    system_reply: str | None = None
    framed: bool = False
    relay_text: str = ""
    relay_mode: str = "framed"
    chat_available: bool = False
    #: Standalone yes/no verdict for an AWAIT_CONFIRM window (GDD §8.3):
    #: ``"yes"`` / ``"no"`` / ``None`` when the utterance is neither.
    confirm_reply: str | None = None


@dataclass(frozen=True, slots=True)
class DialogEvent:
    kind: Ev
    #: Window/capture generation the event belongs to; ``None`` for events
    #: that are not generation-scoped (WAKE_HIT, RESET).
    gen: int | None = None
    classified: Classified | None = None
    parsed: ParsedCommand | None = None  # ENGINE_REJECTED_LOW payload
    confirm: PendingConfirm | None = None  # ENGINE_ASKED payload


@dataclass(frozen=True, slots=True)
class DialogSession:
    """One pilot's complete dialog state. Frozen — the machine returns a new
    session; the engine swaps it atomically. Timing hot-path data
    (last-audio timestamps) deliberately lives in the engine, not here."""

    user_id: int
    guild_id: int
    state: DialogState = DialogState.IDLE
    #: Generation token: increments on every capture/window arm. Events and
    #: emissions carry it; stale ones are dropped instead of being
    #: misattributed to a newer dialog.
    gen: int = 0
    #: Wake-free retries remaining in THIS dialog. Only a fresh wake refills
    #: it — the structural guarantee that no failure loop can sustain itself.
    retries_left: int = 0
    pending: PendingKind = PendingKind.NONE
    ctx_severity: Severity | None = None
    ctx_retry: ParsedCommand | None = None
    ctx_confirm: PendingConfirm | None = None

    def fresh(self, *, max_retries: int) -> DialogSession:
        """A new dialog for this user: budget refilled, context cleared."""
        return replace(
            self,
            state=DialogState.LISTENING,
            gen=self.gen + 1,
            retries_left=max_retries,
            pending=PendingKind.NONE,
            ctx_severity=None,
            ctx_retry=None,
            ctx_confirm=None,
        )

    def idle(self) -> DialogSession:
        """Back to IDLE with context cleared. The retry budget is NOT
        refilled — only a wake does that."""
        return replace(
            self,
            state=DialogState.IDLE,
            pending=PendingKind.NONE,
            ctx_severity=None,
            ctx_retry=None,
            ctx_confirm=None,
        )


class Line(Enum):
    """Which scripted §12.1 line to speak — the engine maps these to
    :mod:`cortana.tts` so personality stays out of the machine."""

    ACK = "ack"  # wake acknowledgement (chirp or "Go ahead.")
    GO_AHEAD = "go_ahead"  # spoken prompt for a subdialog window
    SAY_AGAIN = "say_again"
    NOT_UNDERSTOOD = "not_understood"
    STANDING_DOWN = "standing_down"
    CODE_ACK = "code_ack"  # carries the severity in Speak.severity
    OVERRIDE_UNAVAILABLE = "override_unavailable"


@dataclass(frozen=True, slots=True)
class Speak:
    line: Line
    severity: Severity | None = None  # CODE_ACK payload


@dataclass(frozen=True, slots=True)
class ArmWindow:
    """Arm the wake-free capture window for session.gen (already bumped)."""

    gen: int


@dataclass(frozen=True, slots=True)
class DisarmWindow:
    pass


@dataclass(frozen=True, slots=True)
class RunStt:
    gen: int


@dataclass(frozen=True, slots=True)
class Report:
    """Run the full command path: pilot gate → resolve → IncidentEngine."""

    parsed: ParsedCommand
    #: Severity inherited from a "code <colour>" opener (None = none).
    inherited: Severity | None = None
    #: The LOW-retry rebind source, when this Report came from a system reply.
    rebound_from: ParsedCommand | None = None
    #: A confirmed §8.3 candidate rides here as a ready-made HIGH-tier
    #: resolution: the engine skips phonetic re-resolution entirely — the
    #: pilot already vouched for exactly this system.
    forced_resolution: Resolution | None = None


@dataclass(frozen=True, slots=True)
class Relay:
    """Freeform intel relay (GDD §8.6) through the broadcast path."""

    text: str
    severity: Severity | None
    framed: bool


@dataclass(frozen=True, slots=True)
class RunOverride:
    query: str


@dataclass(frozen=True, slots=True)
class ConfirmPending:
    """Complete a pending §8.3 confirm: apply the stored candidate through
    the same engine path the card's confirm/pick buttons use."""

    confirm: PendingConfirm


@dataclass(frozen=True, slots=True)
class NoteRejected:
    """Count a rejection in health without any user-facing output."""

    reason: str = ""


Action = Union[  # noqa: UP007 - a named union reads better in match sites
    Speak,
    ArmWindow,
    DisarmWindow,
    RunStt,
    Report,
    Relay,
    RunOverride,
    ConfirmPending,
    NoteRejected,
]


@dataclass(frozen=True, slots=True)
class TransitionResult:
    session: DialogSession
    actions: tuple[Action, ...] = field(default_factory=tuple)
