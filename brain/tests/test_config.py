"""Config loader tests. Focused on the fields with non-trivial parsing —
defaults, normalisation and validation — not the whole schema."""

from __future__ import annotations

import pytest

from aura.config import ConfigError, _build_stt, _build_wake

_WAKE_BASE = {"model": "wake.onnx", "threshold": 0.55, "refractory_ms": 2000}


def _wake(extra: dict[str, object]) -> dict[str, dict[str, object]]:
    return {"wake": {**_WAKE_BASE, **extra}}


def test_wake_ack_defaults_to_beep() -> None:
    cfg = _build_wake(_wake({}))
    assert cfg.ack == "beep"


@pytest.mark.parametrize("value", ["voice", "beep", "none", "VOICE", "Beep"])
def test_wake_ack_accepts_valid_and_normalises_case(value: str) -> None:
    cfg = _build_wake(_wake({"ack": value}))
    assert cfg.ack == value.lower()


def test_wake_ack_rejects_unknown() -> None:
    with pytest.raises(ConfigError, match="wake.ack"):
        _build_wake(_wake({"ack": "chime"}))


_STT_BASE = {
    "backend": "faster-whisper",
    "model": "small",
    "compute_type": "int8",
    "cpu_threads": 2,
    "whisper_cpp_url": "http://127.0.0.1:8080/inference",
}


def _stt(extra: dict[str, object]) -> dict[str, dict[str, object]]:
    return {"stt": {**_STT_BASE, **extra}}


def test_relay_mode_defaults_to_framed() -> None:
    assert _build_stt(_stt({})).relay_mode == "framed"


@pytest.mark.parametrize("value", ["framed", "open", "off"])
def test_relay_mode_accepts_valid(value: str) -> None:
    assert _build_stt(_stt({"relay_mode": value})).relay_mode == value


def test_relay_mode_rejects_unknown() -> None:
    with pytest.raises(ConfigError, match="stt.relay_mode"):
        _build_stt(_stt({"relay_mode": "loose"}))


def test_wake_vad_threshold_default_and_bounds() -> None:
    assert _build_wake(_wake({})).vad_threshold == 0.0  # OPT-IN: on-by-default once killed wake
    assert _build_wake(_wake({"vad_threshold": 0.4})).vad_threshold == 0.4
    with pytest.raises(ConfigError, match="wake.vad_threshold"):
        _build_wake(_wake({"vad_threshold": 1.5}))


def test_personality_accepts_bratty() -> None:
    from aura.config import _personality

    assert _personality("bratty") == "bratty"
    with pytest.raises(ConfigError, match="tts.personality"):
        _personality("feral")
