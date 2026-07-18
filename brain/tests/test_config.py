"""Config loader tests. Focused on the fields with non-trivial parsing —
defaults, normalisation and validation — not the whole schema."""

from __future__ import annotations

import pytest

from aura.config import ConfigError, _build_wake

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
