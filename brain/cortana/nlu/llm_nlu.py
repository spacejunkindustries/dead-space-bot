"""LLM natural-language understanding — the "understanding brain" (GDD §6.7).

Whisper turns a pilot's voice into text; this turns that text into a command
when the fixed grammar (§6.1) couldn't. It runs an **on-box** model (llama.cpp /
Ollama, OpenAI-compatible) and asks it for ONE structured intel command as JSON.

This is the deliberate, owner-authorised relaxation of the old "no LLM in the
command path" rule — and it is made safe by two things the model never gets to
touch:

1. **The place is resolved deterministically.** The model returns the place as a
   *string*; :func:`cortana.nlu.phonetics.resolve` maps that string to a real
   system (the seeded k-space map) or a learned area — the model can never
   invent a system that doesn't exist.
2. **Nothing pings until the pilot confirms.** The command flows through the
   same confirm-first / learn-a-word path as a grammar command, so a
   misunderstanding is caught out loud before any alert fires.

Grammar-first: the engine only calls this when the fast fixed grammar produced
no command, so clear callouts stay instant and only the messy ones pay the model
round-trip. Off by default (needs a model); blocking HTTP rides
``asyncio.to_thread``, stdlib only, mirroring the local chat backend.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request

import structlog

from cortana.config import NluConfig
from cortana.types import Intent, ParsedCommand, Severity

log = structlog.get_logger(__name__)

__all__ = ["interpret", "parse_nlu_json"]

#: The command intents the model may emit. Report intents only — the LLM is the
#: understanding brain for *callouts*; registration/pings/fun stay grammar-only.
_ALLOWED: dict[str, Intent] = {
    "UNDER_ATTACK": Intent.UNDER_ATTACK,
    "ASSIST_REQUEST": Intent.ASSIST_REQUEST,
    "HOSTILE_SPOTTED": Intent.HOSTILE_SPOTTED,
    "GATE_CAMP": Intent.GATE_CAMP,
    "RESOLVE": Intent.RESOLVE,
}

_SEVERITY: dict[str, Severity] = {
    "high": Severity.HIGH,
    "medium": Severity.MEDIUM,
    "none": Severity.NONE,
}

#: Kept short on purpose — a small local model follows a tight instruction with
#: a couple of examples far better than a wall of prose.
_SYSTEM = (
    "You convert an EVE Echoes fleet pilot's transcribed voice into ONE combat-intel "
    "command. Reply with ONLY a compact JSON object, no prose.\n"
    'Fields: "intent" = one of UNDER_ATTACK (a friendly is being attacked/tackled/'
    "scrammed/bubbled), ASSIST_REQUEST (asking for help/backup/dps), HOSTILE_SPOTTED "
    "(enemies/reds/neuts seen, no attack), GATE_CAMP (enemies camping a gate), RESOLVE "
    '(a system is clear), or NONE (not a report). "place" = the system or place they '
    'named, short string, or null. "detail" = ship counts/types they said, or null. '
    '"severity" = high (under attack/urgent), medium (sighting/camp), or none.\n'
    "Never invent a place that was not said. If it is not intel, use intent NONE.\n"
    "Examples:\n"
    'in: "we got like three reds burning into taisy someone help" '
    'out: {"intent":"ASSIST_REQUEST","place":"taisy","detail":"three reds","severity":"high"}\n'
    'in: "yeah im tackled in kisogo by a couple battleships" '
    'out: {"intent":"UNDER_ATTACK","place":"kisogo",'
    '"detail":"a couple battleships","severity":"high"}\n'
    'in: "otanuomi is clear now" '
    'out: {"intent":"RESOLVE","place":"otanuomi","detail":null,"severity":"none"}\n'
    'in: "haha nice shot man" out: {"intent":"NONE","place":null,"detail":null,"severity":"none"}'
)


def parse_nlu_json(text: str, raw: str) -> ParsedCommand | None:
    """Turn the model's reply into a :class:`ParsedCommand`, or ``None``.

    Pure and defensive: pulls the first ``{...}`` object out of the reply (small
    models sometimes wrap it in prose or ``` fences), validates the intent
    against :data:`_ALLOWED`, and drops anything it doesn't recognise —
    ``NONE``/unknown intents, or a report intent with no place, become ``None``
    so the utterance falls through exactly as an unmatched grammar parse would.
    ``raw`` is the original transcript, stored verbatim on the command."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    intent = _ALLOWED.get(str(data.get("intent", "")).strip().upper())
    if intent is None:
        return None
    place = data.get("place")
    place = place.strip() if isinstance(place, str) and place.strip() else None
    # A report intent with no place can't be resolved or confirmed — let it fall
    # through rather than posting an "unknown location" card from a guess.
    if place is None:
        return None
    detail = data.get("detail")
    detail = detail.strip() if isinstance(detail, str) and detail.strip() else None
    severity = _SEVERITY.get(str(data.get("severity", "")).strip().lower())
    return ParsedCommand(
        intent=intent,
        system_text=place,
        group_alias=None,
        detail=detail,
        raw=raw,
        severity=severity,
    )


def interpret(cfg: NluConfig, transcript: str) -> ParsedCommand | None:
    """Blocking: ask the on-box model to read ``transcript`` as a command.

    Returns a :class:`ParsedCommand` (place still a raw string, resolved
    downstream) or ``None`` when the model says it isn't a report / can't be
    parsed / the endpoint is unreachable. Runs in a worker thread
    (``asyncio.to_thread``); every failure is swallowed to ``None`` so a flaky
    model degrades to "grammar only", never to a crashed utterance."""
    if not transcript.strip() or not cfg.url:
        return None
    body = json.dumps(
        {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": transcript.strip()},
            ],
            "temperature": 0.0,
            "max_tokens": 120,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        cfg.url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
    except (OSError, http.client.HTTPException, ValueError, KeyError, IndexError, TypeError) as exc:
        log.warning("nlu_request_failed", error=str(exc))
        return None
    parsed = parse_nlu_json(str(content), transcript)
    if parsed is not None:
        log.info("nlu_interpreted", intent=parsed.intent.value, place=parsed.system_text)
    return parsed
