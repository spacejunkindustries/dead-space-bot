"""Piper TTS: subprocess synthesis, in-memory WAV wrapping, per-guild queues.

GDD §12. The audio path (§12.3) is deliberately minimal:

- Piper runs as a **separate binary** over a subprocess boundary
  (``tts.binary --model tts.voice --output-raw``), text on stdin, raw s16le
  PCM at the model's native rate on stdout. No temp files, ever.
- Brain wraps the raw samples in a WAV header built with :mod:`struct`,
  entirely in memory, and ships the bytes to Ears over IPC. **Brain does not
  resample** — Songbird's Symphonia layer parses the header and resamples to
  48 kHz inside Ears.
- Hard cap ``tts.max_utterance_s`` (§12.2): an utterance that synthesises
  longer than the cap is dropped and :meth:`Speaker.say` returns ``False`` so
  the caller falls back to posting the text in the channel instead.

The voice's native sample rate is read from the Piper voice config JSON
(``<voice>.json`` next to the ``.onnx``) once at init; 22050 Hz is the Piper
default when the file is missing or unreadable.

This module also carries the §12.1 utterance catalogue as pure functions so
every module that speaks uses the exact scripted strings.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import math
import random
import struct
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import structlog

from cortana.config import ConfigHolder, TtsConfig
from cortana.ipc import PRIORITY_ALERT, PRIORITY_NORMAL, IpcServer
from cortana.types import Intent, Severity

__all__ = [
    "DEFAULT_SAMPLE_RATE",
    "Speaker",
    "SynthesisError",
    "ambiguous",
    "area_learned",
    "area_limit",
    "build_wav",
    "chase_hint",
    "chase_no_incident",
    "chase_updated",
    "confirm_area",
    "confirm_report",
    "degraded",
    "flood_control",
    "fun_cooldown",
    "fun_disabled",
    "help_hint",
    "hot_lines",
    "no_pings",
    "not_registered",
    "not_understood",
    "number_word",
    "ping_cleared",
    "ping_limit",
    "ping_sent",
    "ping_types_phrase",
    "pinging_you",
    "post_failed",
    "read_voice_sample_rate",
    "registered",
    "resolved",
    "responders",
    "say_again",
    "say_again_callsign",
    "standing_down",
    "timer_set",
    "unregistered",
    "whoami",
]

log = structlog.get_logger(__name__)

#: Piper's default output rate when the voice config is unavailable.
DEFAULT_SAMPLE_RATE = 22_050

#: Watchdog for one Piper invocation. Piper is ~10× real time on this class of
#: CPU (GDD §12), so a capped 3 s utterance synthesises in well under a
#: second; anything near this bound means the process is wedged and gets
#: killed rather than stalling the per-guild speech queue.
SYNTHESIS_TIMEOUT_S = 10.0

_CHANNELS = 1
_BYTES_PER_SAMPLE = 2  # s16le

#: Line cache bounds: the §12.1 acknowledgement pool is a few dozen short
#: strings; anything longer (override answers, named callouts with arbitrary
#: text) varies per use and would only churn the cache.
_LINE_CACHE_MAX = 64
_LINE_CACHE_TEXT_MAX = 80


class SynthesisError(Exception):
    """Piper failed: missing binary, non-zero exit, timeout, or empty output."""


# ── §12.1 utterance catalogue ─────────────────────────────────────────────────
# Short. Always short. CORTANA is talking over a fight. These are the exact
# scripted strings from the GDD table; do not improvise variants elsewhere.
#
# Personality (GDD §12.4): under tts.personality "cortana", ACKNOWLEDGEMENT
# lines rotate through short variants so CORTANA feels alive; every
# information-carrying line (system names, counts, timers) stays fixed —
# a pilot mid-fight must never have to parse a surprise phrasing for facts.

_personality = "standard"


def set_personality(style: str) -> None:
    """Select the spoken-line flavour ("standard" | "cortana" | "bratty").
    Called by the App at startup and after each SIGHUP config reload."""
    global _personality
    _personality = style


def _pick(standard: str, cortana: tuple[str, ...] = (), bratty: tuple[str, ...] = ()) -> str:
    """One line from the active personality's pool.

    "standard" is always the exact §12.1 string. "bratty" (GDD §12.4 — the
    corp's explicit sailor-vocabulary choice) falls back to the cortana pool
    for lines without a bratty variant. Only ACK-class lines ever vary;
    information-carrying lines are fixed regardless of personality.
    """
    if _personality == "cortana" and cortana:
        return random.choice(cortana)
    if _personality == "bratty":
        pool = bratty or cortana
        if pool:
            return random.choice(pool)
    return standard


_NUMBER_WORDS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
)


def number_word(n: int) -> str:
    """Spell small counts (``2`` → ``"two"``); larger numbers stay digits."""
    return _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)


def ping_sent(system: str, group: str | None = None, *, type_word: str = "Hostiles") -> str:
    """*"Hostiles Otanuomi, pinged."* / *"Hostiles Otanuomi, pinged home defense."*"""
    if group:
        return f"{type_word} {system}, pinged {group}."
    return f"{type_word} {system}, pinged."


def ambiguous(type_word: str, system: str) -> str:
    """*"Hostiles Otanuomi — say again to confirm."*"""
    return f"{type_word} {system} — say again to confirm."


def say_again() -> str:
    """*"Say again the system."* — unresolved system name (LOW tier)."""
    return "Say again the system."


_NOT_UNDERSTOOD_BRATTY = (
    "That was gibberish. Again.",
    "What the hell was that? Repeat.",
    "Nope, didn't catch that. Try words.",
    "Again, slower, like I'm five.",
    "Static and vibes. Say it again.",
    "My ears work. Your mouth doesn't. Again.",
    "Zero idea what that was. Once more.",
    "Come again? That meant nothing.",
)


def not_understood() -> str:
    """*"Say again?"* — the utterance matched no command and no relay frame
    (GDD §8.6 framed mode): CORTANA heard something but won't post it."""
    return _pick("Say again?", bratty=_NOT_UNDERSTOOD_BRATTY)


_STANDING_DOWN_BRATTY = (
    "Nope. Standing down. Wake me later.",
    "I give up. Holler when it's words.",
    "Can't parse that shit. Standing down.",
    "I'm out. Wake me to retry.",
    "Done guessing. Wake me and retry.",
)


def posted_short() -> str:
    """Minimal spoken confirmation when the full outcome line exceeds the
    §12.2 cap (long verbatim system names): the pilot must still HEAR that
    the report landed — silence-plus-channel-text read as a swallowed
    report (live complaint). Info-carrying, so it never varies."""
    return "Posted."


def updated_short() -> str:
    """Short spoken confirmation for a folded/updated card (see posted_short)."""
    return "Updated."


def standing_down() -> str:
    """*"Standing down. Wake me to retry."* — the second consecutive
    unintelligible utterance ends the dialogue: the pilot now KNOWS the
    window is closed and a fresh wake word is needed. Every variant must fit
    the §12.2 3-second cap — the first cut of this line was longer, got
    dropped by the cap, and fell back into the intel channel as text spam."""
    return _pick("Standing down. Wake me to retry.", bratty=_STANDING_DOWN_BRATTY)


def chase_updated(system: str) -> str:
    """*"Chase updated, Kisogo."* — the live card now points at the new system."""
    return f"Chase updated, {system}."


def chase_no_incident() -> str:
    """*"No active incident to chase."* — chase needs a live report first."""
    return "No active incident to chase."


def chase_hint() -> str:
    """*"Say update chase and a system, or clear to finish."* — a bare
    "chase mode", or a chase terminator ("chase mode off") with no system."""
    return "Say update chase and a system, or clear to finish."


def post_failed() -> str:
    """*"Discord post failed."* — the card could not be posted (permissions,
    deleted channel, REST failure). The report was NOT recorded; the pilot
    must know their alert did not land (GDD §20 posture: fail loud)."""
    return "Discord post failed."


_GO_AHEAD_BRATTY = (
    "What. I'm listening.",
    "Yeah, yeah. Talk.",
    "Ugh, fine. Go.",
    "I'm up. What do you want?",
    "This better be fucking important.",
    "Listening. Make it quick.",
    "Oh great, it's you. Go ahead.",
    "You rang? Spit it out.",
    "Hell, I was napping. Talk.",
    "Speak. I haven't got all day.",
)


def go_ahead() -> str:
    """*"Go ahead."* — spoken the instant the wake word fires, so the pilot
    knows CORTANA is listening before they start their report (§5 capture)."""
    return _pick(
        "Go ahead.",
        cortana=("Go ahead.", "Listening.", "I'm here. Go ahead.", "Send it.", "Copy. Go ahead."),
        bratty=_GO_AHEAD_BRATTY,
    )


def responders(n: int, system: str) -> str:
    """*"Two responding to Otanuomi."*"""
    return f"{number_word(n).capitalize()} responding to {system}."


def resolved(system: str) -> str:
    """*"Otanuomi clear."*"""
    return f"{system} clear."


def timer_set(system: str, duration_words: str) -> str:
    """*"Timer Kisogo, four hours."*"""
    return f"Timer {system}, {duration_words}."


def flood_control() -> str:
    """*"Flood control active."*"""
    return "Flood control active."


def degraded() -> str:
    """*"Voice offline, use slash commands."*"""
    return "Voice offline, use slash commands."


#: The natural readback template per report intent (GDD §8.3 confirm-first).
#: A pilot mid-fight parses "Under attack in Taisy, confirm?" far faster than
#: a bare "Heard Taisy, confirm?" — the readback names the SITUATION it's about
#: to post, not just the system, so a wrong intent is as catchable as a wrong
#: system (live request: "go ok i got you tackled in taisy … confirm please").
_CONFIRM_INTENT_PHRASE: dict[Intent, str] = {
    Intent.UNDER_ATTACK: "Under attack in {system}",
    Intent.ASSIST_REQUEST: "Assistance needed in {system}",
    Intent.HOSTILE_SPOTTED: "Hostiles in {system}",
    Intent.GATE_CAMP: "Gate camp in {system}",
}

#: Cap on the verbatim detail folded into a spoken readback. Detail is captured
#: verbatim and can be a whole sentence ("by three battleships and two cruisers
#: and a bunch of frigates …"); past this length it is dropped from SPEECH only
#: (the card still carries it in full) so the readback never blows the §12.2
#: 3-second cap and vanishes, leaving the confirm window with no audible prompt.
_CONFIRM_DETAIL_MAX = 48


def confirm_report(name: str, *, intent: Intent | None = None, detail: str | None = None) -> str:
    """The §8.3 confirm-first readback (``dialog.confirm_reports``).

    With an ``intent`` it reads back the whole situation naturally — *"Under
    attack in Taisy, by two cruisers. Confirm?"* — so a mishearing of either
    the intent or the system gets one audible veto point. A short verbatim
    ``detail`` rides along; a long one is dropped from speech (the card keeps
    it). Falls back to the bare *"Heard <name>. Confirm?"* for intents without
    a readback template. Info-carrying; the wording never varies by
    personality."""
    template = _CONFIRM_INTENT_PHRASE.get(intent) if intent is not None else None
    if template is None:
        return f"Heard {name}. Confirm?"
    phrase = template.format(system=name)
    if detail:
        trimmed = " ".join(detail.split())
        if trimmed and len(trimmed) <= _CONFIRM_DETAIL_MAX:
            phrase = f"{phrase}, {trimmed}"
    return f"{phrase}. Confirm?"


def confirm_area(word: str) -> str:
    """*"Did you say the branch?"* — the §8.5a learn-a-word confirm: the place
    resolved to no system, so CORTANA asks once whether to remember it as a
    custom area. Info-carrying; never varies."""
    return f"Did you say {word}?"


def area_learned(word: str) -> str:
    """*"Learned the branch."* — spoken when a confirmed place is saved (§8.5a),
    so the pilot hears it stuck. Info-carrying; never varies."""
    return f"Learned {word}."


def area_limit() -> str:
    """*"Area limit reached."* — the per-guild ``areas.max_per_guild`` cap: a
    new place can't be learned until an FC prunes with /areas-forget, but the
    report still posts. Info-carrying; never varies."""
    return "Area limit reached."


_FUN_COOLDOWN_BRATTY = (
    "Give it a rest. Cooling down.",
    "One at a time, jeez.",
    "Cooling down. Patience.",
    "Not yet. I'm not a jukebox.",
)


def fun_cooldown() -> str:
    """*"Cooling down."* — the fun-command throttle (GDD §13.2): facts and
    insults share per-guild cooldowns so comedy can't crowd real comms."""
    return _pick("Cooling down.", bratty=_FUN_COOLDOWN_BRATTY)


def fun_disabled() -> str:
    """*"Fun commands are off."* — fun.enabled is false; the pilot asked for
    a fact/insult and must hear WHY nothing follows, not dead air."""
    return "Fun commands are off."


def help_hint() -> str:
    """*"Command list posted to Discord."* — the HELP intent; the App posts the
    /help front page to the intel channel alongside this (the real manual is
    /help — a spoken catalogue would blow the §12.2 3-second cap)."""
    return "Command list posted to Discord."


_SEVERITY_SPOKEN: dict[Severity, str] = {
    Severity.HIGH: "red",
    Severity.MEDIUM: "orange",
    Severity.NONE: "yellow",
}


_RELAYED_BRATTY = (
    "Posted. You're welcome.",
    "Sent it. Miracles happen daily.",
    "Relayed. Don't make me regret it.",
    "Fine, it's posted. Happy?",
    "Done. Absolutely riveting stuff.",
    "Your words, immortalized. God help us.",
    "Shipped it. Now stop fucking around.",
    "Posted. Somebody had to.",
)


def relayed() -> str:
    """*"Relayed."* — spoken after a freeform relay posts, so the pilot knows
    it landed and stops repeating themselves (each repeat is another card and
    another STT decode)."""
    return _pick(
        "Relayed.",
        cortana=("Relayed.", "Copy that. Relayed.", "On the wire.", "Sent it up the chain."),
        bratty=_RELAYED_BRATTY,
    )


_CODE_ACK_BRATTY = (
    "Code {colour}. Shit. Talk to me.",
    "Code {colour}? Fun. Details, now.",
    "Copy code {colour}. Spill it.",
    "Code {colour}, great. What's on fire?",
    "Code {colour}. Go, I'm listening.",
    "Ooh, code {colour}. Report, hotshot.",
)


def code_ack(severity: Severity) -> str:
    """*"Code orange. Go ahead."* — a standalone spoken colour code opens a
    dialogue: CORTANA acknowledges and the report follows in a wake-free window
    (GDD §6.4)."""
    colour = _SEVERITY_SPOKEN[severity]
    return _pick(
        f"Code {colour}. Go ahead.",
        cortana=(
            f"Code {colour}. Go ahead.",
            f"Code {colour} logged. Go ahead.",
            f"Copy code {colour}. Send it.",
        ),
        bratty=tuple(t.format(colour=colour) for t in _CODE_ACK_BRATTY),
    )


def override_unavailable() -> str:
    """*"Override channel unavailable."* — the §6.6 assistant failed or is
    disabled; the fixed line keeps errors off comms."""
    return "Override channel unavailable."


def override_cooldown() -> str:
    """*"Override cooling down."* — per-pilot §6.6 cost throttle."""
    return "Override cooling down."


def override_posted() -> str:
    """*"Answer posted to Discord."* — the reply was too long for the §12.2
    spoken cap and went to the intel channel instead."""
    return "Answer posted to Discord."


def responder_named(name: str, system: str | None) -> str:
    """*"Space Junkie responding to Otanuomi."* — spoken when a pilot presses
    "On my way", so the reporter hears WHO is coming, not just a count."""
    if system:
        return f"{name} responding to {system}."
    return f"{name} is on the way."


def registered(callsign: str) -> str:
    """*"Registered you as Space Junkie."*"""
    return f"Registered you as {callsign}."


def unregistered() -> str:
    """*"Unregistered."*"""
    return "Unregistered."


def not_registered() -> str:
    """*"You are not registered."*"""
    return "You are not registered."


def whoami(callsign: str) -> str:
    """*"You are Space Junkie."*"""
    return f"You are {callsign}."


def say_again_callsign() -> str:
    """*"Say again the callsign."* — REGISTER heard with no usable name."""
    return "Say again the callsign."


# Personal pings (GDD §10.3 / §12.1): spoken type words, pluralized naturally.
# All four report types collapse to "everything".
_PING_TYPE_WORDS: tuple[tuple[Intent, str], ...] = (
    (Intent.HOSTILE_SPOTTED, "hostiles"),
    (Intent.UNDER_ATTACK, "attacks"),
    (Intent.ASSIST_REQUEST, "assist requests"),
    (Intent.GATE_CAMP, "gate camps"),
)


def ping_types_phrase(types: frozenset[Intent]) -> str:
    """Spoken phrase for a personal-ping type set: ``"gate camps"``,
    ``"hostiles and attacks"``, all four → ``"everything"``."""
    words = [word for intent, word in _PING_TYPE_WORDS if intent in types]
    if len(words) == len(_PING_TYPE_WORDS):
        return "everything"
    return " and ".join(words)


def pinging_you(types_phrase: str, system: str | None) -> str:
    """*"Pinging you for gate camps in Otanuomi."* /
    *"Pinging you for everything everywhere."*"""
    where = f"in {system}" if system else "everywhere"
    return f"Pinging you for {types_phrase} {where}."


def ping_cleared() -> str:
    """*"No longer pinging you."*"""
    return "No longer pinging you."


def no_pings() -> str:
    """*"You have no pings set."*"""
    return "You have no pings set."


def ping_limit() -> str:
    """*"Ping limit reached."* — the discipline.personal_pings_max cap."""
    return "Ping limit reached."


def hot_lines() -> tuple[str, ...]:
    """Every short scripted line on the wake/ack hot path, for the current
    personality — the set :meth:`Speaker.warm` pre-synthesises into the line
    cache so acknowledgements play without paying a Piper model load."""
    lines: list[str] = [
        "Go ahead.",
        "Say again?",
        "Standing down. Wake me to retry.",
        "Say again the system.",
        "Say again the callsign.",
        "Relayed.",
        "Flood control active.",
        "Voice offline, use slash commands.",
        "Command list posted to Discord.",
        "Override channel unavailable.",
        "Override cooling down.",
        "Answer posted to Discord.",
    ]
    for colour in _SEVERITY_SPOKEN.values():
        lines.append(f"Code {colour}. Go ahead.")
    if _personality == "cortana":
        lines += ["Listening.", "I'm here. Go ahead.", "Send it.", "Copy. Go ahead."]
        lines += ["Copy that. Relayed.", "On the wire.", "Sent it up the chain."]
        for colour in _SEVERITY_SPOKEN.values():
            lines += [f"Code {colour} logged. Go ahead.", f"Copy code {colour}. Send it."]
    if _personality == "bratty":
        lines += list(_GO_AHEAD_BRATTY) + list(_RELAYED_BRATTY) + list(_NOT_UNDERSTOOD_BRATTY)
        lines += list(_STANDING_DOWN_BRATTY)
        for colour in _SEVERITY_SPOKEN.values():
            lines += [t.format(colour=colour) for t in _CODE_ACK_BRATTY]
    return tuple(dict.fromkeys(lines))


# ── WAV wrapping (in memory — constraint 5 adjacent: nothing touches disk) ───


def build_wav(pcm_s16le: bytes, sample_rate: int) -> bytes:
    """Prepend a canonical 44-byte RIFF/WAVE header to raw mono s16le samples.

    Pure bytes-in/bytes-out; no file objects, no disk. Symphonia on the Ears
    side parses this header and resamples to 48 kHz internally (GDD §12.3).
    """
    if len(pcm_s16le) % _BYTES_PER_SAMPLE != 0:
        raise ValueError(f"PCM byte count {len(pcm_s16le)} is not s16le-aligned")
    byte_rate = sample_rate * _CHANNELS * _BYTES_PER_SAMPLE
    block_align = _CHANNELS * _BYTES_PER_SAMPLE
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm_s16le),  # RIFF chunk size: rest of header + data
        b"WAVE",
        b"fmt ",
        16,  # fmt chunk size (PCM)
        1,  # audio format: PCM
        _CHANNELS,
        sample_rate,
        byte_rate,
        block_align,
        _BYTES_PER_SAMPLE * 8,  # bits per sample
        b"data",
        len(pcm_s16le),
    )
    return header + pcm_s16le


def build_chirp(sample_rate: int, freq_hz: int = 880, ms: int = 130) -> bytes:
    """A short sine-tone WAV (the wake-ack "listening" chirp), built in memory.

    Two quick rising blips with a cosine fade in/out so there is no click.
    Generated once and reused, so acknowledging the wake word costs no neural
    synthesis — the whole point is that it is instant on a small CPU."""
    n = max(1, sample_rate * ms // 1000)
    fade = max(1, n // 8)
    samples = bytearray()
    for i in range(n):
        # Two-tone blip: second half a fifth higher, for a recognisable rise.
        f = freq_hz if i < n // 2 else int(freq_hz * 1.5)
        env = 1.0
        if i < fade:
            env = 0.5 - 0.5 * math.cos(math.pi * i / fade)
        elif i > n - fade:
            env = 0.5 - 0.5 * math.cos(math.pi * (n - i) / fade)
        value = int(0.35 * env * 32767 * math.sin(2 * math.pi * f * i / sample_rate))
        samples += struct.pack("<h", max(-32768, min(32767, value)))
    return build_wav(bytes(samples), sample_rate)


def holographic(pcm_s16le: bytes, sample_rate: int) -> bytes:
    """Give a synthesised voice a sci-fi "AI hologram" sheen — GDD §12.

    A light modulated-delay chorus (the shimmer) plus a few decaying reflections
    (a spacious, projected feel). This is an audio *effect* over an ordinary
    synthetic voice — it clones no one; it just makes CORTANA sound less like a
    plain TTS and more like a ship's AI. numpy is imported lazily (audio dep).
    Returns s16le at the same rate; empty input passes through untouched."""
    import numpy as np  # lazy — audio dependency

    x = np.frombuffer(pcm_s16le, dtype=np.int16).astype(np.float32) / 32768.0
    n = x.size
    if n == 0:
        return pcm_s16le
    t = np.arange(n)

    # Chorus: mix in a copy read through a slowly modulated delay line.
    base = max(1, int(0.018 * sample_rate))  # 18 ms
    depth = max(1, int(0.002 * sample_rate))  # ±2 ms wobble
    lfo = (depth * np.sin(2 * np.pi * 0.15 * t / sample_rate)).astype(np.int64)
    idx = np.clip(t - base - lfo, 0, n - 1)
    y = x + 0.35 * x[idx]

    # Subtle reverb: a handful of decaying early reflections.
    for delay_ms, gain in ((37, 0.24), (53, 0.17), (71, 0.11)):
        d = int(delay_ms / 1000 * sample_rate)
        if 0 < d < n:
            y[d:] += gain * y[:-d]

    peak = float(np.max(np.abs(y))) or 1.0
    y = (y / peak) * 0.9  # normalise to avoid clipping the effect's sum
    return (y * 32767.0).astype(np.int16).tobytes()


def read_voice_sample_rate(voice_path: str | Path) -> int:
    """Read the native sample rate from the Piper voice config JSON.

    Piper ships ``<name>.onnx`` + ``<name>.onnx.json``; the config carries
    ``audio.sample_rate``. Missing/invalid config falls back to
    :data:`DEFAULT_SAMPLE_RATE` with a warning — a wrong rate only makes the
    voice sound off-pitch, it must not stop the bot.
    """
    config_path = Path(f"{voice_path}.json")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        rate = data["audio"]["sample_rate"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning(
            "piper_voice_config_unreadable",
            path=str(config_path),
            error=str(exc),
            fallback=DEFAULT_SAMPLE_RATE,
        )
        return DEFAULT_SAMPLE_RATE
    if not isinstance(rate, int) or rate <= 0:
        log.warning(
            "piper_voice_config_bad_rate",
            path=str(config_path),
            rate=rate,
            fallback=DEFAULT_SAMPLE_RATE,
        )
        return DEFAULT_SAMPLE_RATE
    return rate


# ── Speaker ──────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _SayJob:
    text: str
    priority: int
    done: asyncio.Future[bool]
    #: Per-utterance override of ``tts.max_utterance_s`` — the fun path
    #: (GDD §13.2) speaks whole facts, which run longer than command replies.
    #: ``None`` = the configured cap applies.
    max_s: float | None = None


#: Priority-queue entry: (rank, seq, job). Rank is the NEGATED wire priority
#: (ipc.PRIORITY_ALERT=2 > NORMAL=1 > LOW=0, and PriorityQueue pops the
#: smallest), seq keeps FIFO order within one priority class and breaks ties
#: before the unorderable job ever gets compared.
_QueueEntry = tuple[int, int, _SayJob]


class Speaker:
    """Synthesises §12.1 utterances and ships them to Ears for playback.

    One :class:`Speaker` serves all guilds. Each guild gets its own
    priority-ordered queue and worker task: ALERT jobs jump ahead of QUEUED
    NORMAL jobs (the in-flight synthesis is never preempted), so a wake ack
    or a CODE RED confirmation can't sit behind an uncached NORMAL synthesis
    past the dialog grace window; jobs of equal priority play in FIFO order.
    The Piper subprocess itself is serialised globally (one synthesis at a
    time — the droplet's cores belong to Whisper).

    :meth:`say` returns ``True`` when the utterance was synthesised and handed
    to IPC, ``False`` when it was suppressed (TTS disabled, muted trigger
    user, synthesis failure, or over the length cap) — the caller then falls
    back to channel text.
    """

    def __init__(self, holder: ConfigHolder, ipc: IpcServer) -> None:
        self._holder = holder
        self._ipc = ipc
        # Cache the voice path with its rate so a SIGHUP voice swap refreshes
        # the rate before the next WAV header is built (see _render). The
        # per-voice rate cache keeps concurrent guild workers straddling a
        # swap consistent: each render uses the rate of the voice it actually
        # synthesised with, never a neighbour's refresh.
        self._voice_path = holder.current.tts.voice
        self._sample_rate = read_voice_sample_rate(self._voice_path)
        self._rate_cache: dict[str, int] = {self._voice_path: self._sample_rate}
        self._synth_lock = asyncio.Lock()
        self._queues: dict[int, asyncio.PriorityQueue[_QueueEntry]] = {}
        self._seq = itertools.count()  # FIFO within a priority class
        self._workers: dict[int, asyncio.Task[None]] = {}
        self._closed = False
        self._voice_mutes: set[int] = set()
        # Rendered-PCM cache for the short scripted lines (§12.1). Piper
        # reloads its voice model on every subprocess spawn — ~1s on this
        # class of CPU — so acknowledgements ("Go ahead.", "Relayed.") are
        # synthesised once and replayed from RAM. Keyed by (text, voice,
        # effect) so a SIGHUP voice/effect swap misses cleanly.
        self._line_cache: OrderedDict[tuple[str, str, str], bytes] = OrderedDict()
        self._prime_task: asyncio.Task[None] | None = None
        # Pre-built "listening" chirp — a short tone, generated once in memory.
        # The wake acknowledgement uses this instead of Piper so it is instant
        # (no neural-voice model load) and gives the pilot immediate feedback.
        self._chirp_wav = build_chirp(self._sample_rate)

    async def chirp(self, guild_id: int, *, user_id: int | None = None) -> bool:
        """Play the instant "listening" tone into ``guild_id`` (wake ack).

        Bypasses Piper entirely — the WAV is pre-generated — so there is no
        synthesis latency before the pilot starts talking. Respects TTS-enabled
        and per-user /mute-voice."""
        if self._closed or not self._holder.current.tts.enabled:
            return False
        if user_id is not None and user_id in self._voice_mutes:
            return False
        # send_tts is False when Ears is not connected: the chirp was NOT
        # played — report that honestly so callers never assume an ack landed.
        return await self._ipc.send_tts(guild_id, PRIORITY_ALERT, self._chirp_wav)

    @property
    def sample_rate(self) -> int:
        """The native output rate of the last-used voice (from ``.onnx.json``)."""
        return self._sample_rate

    # ── /mute-voice (GDD §12.2) ──────────────────────────────────────────────

    def set_voice_mutes(self, user_ids: set[int]) -> None:
        """Replace the muted-user set (loaded from the ``voice_mutes`` table)."""
        self._voice_mutes = set(user_ids)

    def set_muted(self, user_id: int, muted: bool) -> None:
        """Flip one user's ``/mute-voice`` state."""
        if muted:
            self._voice_mutes.add(user_id)
        else:
            self._voice_mutes.discard(user_id)

    def is_muted(self, user_id: int) -> bool:
        return user_id in self._voice_mutes

    # ── public API ───────────────────────────────────────────────────────────

    async def say(
        self,
        guild_id: int,
        text: str,
        priority: int = PRIORITY_NORMAL,
        *,
        user_id: int | None = None,
        max_s: float | None = None,
    ) -> bool:
        """Queue ``text`` for spoken playback in ``guild_id``.

        ``user_id`` is the pilot whose command triggered the reply; if they
        ran ``/mute-voice`` the utterance is suppressed for them. ``max_s``
        overrides ``tts.max_utterance_s`` for this one utterance (the fun
        path speaks whole facts). Returns ``True`` once the WAV has been sent
        to Ears, ``False`` when the speech was dropped for any reason —
        callers fall back to channel text.
        """
        if self._closed or not self._holder.current.tts.enabled:
            return False
        if user_id is not None and user_id in self._voice_mutes:
            log.debug("tts_suppressed_muted_user", user_id=user_id, guild_id=guild_id)
            return False
        job = _SayJob(
            text=text,
            priority=priority,
            done=asyncio.get_running_loop().create_future(),
            max_s=max_s,
        )
        # ALERT (higher wire priority) sorts ahead of queued NORMAL jobs; the
        # seq counter keeps arrival order within a class.
        self._queue_for(guild_id).put_nowait((-priority, next(self._seq), job))
        return await job.done

    async def synthesize(self, text: str, cfg: TtsConfig | None = None) -> bytes:
        """Run Piper once and return raw s16le PCM at the voice's native rate.

        ``cfg`` lets a caller thread ONE tts-config snapshot through an entire
        render (:meth:`_render` passes its own): re-reading ``holder.current``
        here would race a SIGHUP voice swap and cache the new voice's PCM
        under the old voice's key. When omitted, the current config is read.

        Blocking process I/O rides asyncio's subprocess transport — nothing
        runs on the event loop thread itself. One synthesis at a time.
        Raises :class:`SynthesisError` on failure.
        """
        if cfg is None:
            cfg = self._holder.current.tts
        async with self._synth_lock:
            try:
                proc = await asyncio.create_subprocess_exec(
                    cfg.binary,
                    "--model",
                    cfg.voice,
                    "--output-raw",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                raise SynthesisError(f"cannot exec piper binary {cfg.binary!r}: {exc}") from exc
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=text.encode("utf-8") + b"\n"),
                    timeout=SYNTHESIS_TIMEOUT_S,
                )
            except TimeoutError as exc:
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
                raise SynthesisError(f"piper timed out after {SYNTHESIS_TIMEOUT_S}s") from exc
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[:400]
            raise SynthesisError(f"piper exited {proc.returncode}: {detail}")
        if not stdout:
            raise SynthesisError("piper produced no audio")
        if len(stdout) % _BYTES_PER_SAMPLE != 0:
            stdout = stdout[: len(stdout) - (len(stdout) % _BYTES_PER_SAMPLE)]
        return stdout

    async def warm(self) -> None:
        """Prime Piper once at startup (best-effort).

        Piper reloads the voice model on every subprocess spawn; running a
        throwaway synthesis now pulls the model file into the OS page cache so
        the first *real* reply loads from RAM, not disk. Failures are ignored —
        warming must never block or crash startup."""
        if not self._holder.current.tts.enabled:
            return
        try:
            await self.synthesize("ready")
            log.info("tts_warmed")
        except Exception as exc:  # noqa: BLE001 — warming is best-effort
            log.warning("tts_warm_failed", error=str(exc))

    def start_priming(self) -> None:
        """Fill the line cache in the background so the first "Go ahead." of
        the day is already rendered. Called once the app is fully started —
        never during setup, so a startup failure can't tear down the loop
        while a Piper spawn is mid-flight (cancelling subprocess creation
        during ``asyncio.run`` cleanup can wedge the interpreter). Best-effort:
        a failure only means that line synthesises on first use instead."""
        if self._closed or not self._holder.current.tts.enabled:
            return
        if self._prime_task is None or self._prime_task.done():
            self._prime_task = asyncio.create_task(self._prime_lines(), name="tts-prime")

    async def _prime_lines(self) -> None:
        primed = 0
        for line in hot_lines():
            if self._closed:
                return
            try:
                await self._render(line)
                primed += 1
            except Exception:  # noqa: BLE001 — priming is best-effort
                return
        log.info("tts_lines_primed", count=primed)

    async def close(self) -> None:
        """Stop accepting work, drain nothing, cancel all guild workers."""
        self._closed = True
        if self._prime_task is not None:
            self._prime_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._prime_task
            self._prime_task = None
        workers = list(self._workers.values())
        for task in workers:
            task.cancel()
        for task in workers:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._workers.clear()
        for queue in self._queues.values():
            while not queue.empty():
                _, _, job = queue.get_nowait()
                if not job.done.done():
                    job.done.set_result(False)
        self._queues.clear()

    # ── internals ────────────────────────────────────────────────────────────

    def _queue_for(self, guild_id: int) -> asyncio.PriorityQueue[_QueueEntry]:
        queue = self._queues.get(guild_id)
        if queue is None:
            queue = asyncio.PriorityQueue()
            self._queues[guild_id] = queue
            self._workers[guild_id] = asyncio.create_task(
                self._worker(guild_id, queue), name=f"tts-worker-{guild_id}"
            )
        return queue

    async def _worker(self, guild_id: int, queue: asyncio.PriorityQueue[_QueueEntry]) -> None:
        while True:
            _, _, job = await queue.get()
            try:
                spoken = await self._speak(guild_id, job)
            except asyncio.CancelledError:
                if not job.done.done():
                    job.done.set_result(False)
                raise
            except Exception:
                log.exception("tts_worker_error", guild_id=guild_id, text=job.text)
                spoken = False
            if not job.done.done():
                job.done.set_result(spoken)

    async def _rate_for(self, voice: str) -> int:
        """Native sample rate for ``voice``, cached per path. Also tracks the
        last-used voice on ``_voice_path``/``_sample_rate`` (the public
        :attr:`sample_rate`). The attribute pair is mutated with no await in
        between, so concurrent renders always see a consistent pair."""
        rate = self._rate_cache.get(voice)
        if rate is None:
            rate = await asyncio.to_thread(read_voice_sample_rate, voice)
            self._rate_cache[voice] = rate
        self._voice_path = voice
        self._sample_rate = rate
        return rate

    async def _render(self, text: str) -> tuple[bytes, int]:
        """Synthesise ``text`` to effect-applied PCM, via the line cache.

        Returns ``(pcm, sample_rate)`` rendered under ONE tts-config snapshot
        taken at entry: the cache key, the Piper invocation, the effect, and
        the sample rate all come from the same voice. A SIGHUP voice swap
        landing mid-render used to let ``synthesize`` re-read the NEW config
        while the cache key and WAV-header rate still described the OLD voice
        — a wrong-pitch line that persisted in the cache (review finding).

        Short scripted lines are rendered once per (text, voice, effect) and
        replayed from RAM; everything else synthesises fresh. Raises
        :class:`SynthesisError` on failure (never cached).
        """
        cfg = self._holder.current.tts
        sample_rate = await self._rate_for(cfg.voice)
        cacheable = len(text) <= _LINE_CACHE_TEXT_MAX
        key = (text, cfg.voice, cfg.effect)
        if cacheable:
            cached = self._line_cache.get(key)
            if cached is not None:
                self._line_cache.move_to_end(key)
                return cached, sample_rate
        pcm = await self.synthesize(text, cfg)
        if cfg.effect == "holographic":
            pcm = await asyncio.to_thread(holographic, pcm, sample_rate)
        if cacheable:
            self._line_cache[key] = pcm
            while len(self._line_cache) > _LINE_CACHE_MAX:
                self._line_cache.popitem(last=False)
        return pcm, sample_rate

    async def _speak(self, guild_id: int, job: _SayJob) -> bool:
        cfg = self._holder.current.tts
        try:
            pcm, sample_rate = await self._render(job.text)
        except SynthesisError as exc:
            log.warning("tts_synthesis_failed", guild_id=guild_id, text=job.text, error=str(exc))
            return False
        duration_s = len(pcm) / (sample_rate * _BYTES_PER_SAMPLE)
        cap_s = job.max_s if job.max_s is not None else cfg.max_utterance_s
        if duration_s > cap_s:
            # §12.2: hard cap — if it does not fit, it goes to the channel instead.
            log.info(
                "tts_over_cap_dropped",
                guild_id=guild_id,
                text=job.text,
                duration_s=round(duration_s, 2),
                cap_s=cap_s,
            )
            return False
        sent = await self._ipc.send_tts(guild_id, job.priority, build_wav(pcm, sample_rate))
        if not sent:
            # Ears is disconnected (or the write failed): the utterance was
            # never played. Returning False engages the caller's channel-text
            # fallback instead of the prompt silently vanishing (GDD §20).
            log.warning("tts_dropped_no_ears", guild_id=guild_id, text=job.text)
            return False
        log.debug(
            "tts_sent",
            guild_id=guild_id,
            priority=job.priority,
            duration_s=round(duration_s, 2),
        )
        return True
