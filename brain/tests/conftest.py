"""Shared fixtures: a complete, valid cortana.yaml as a dict, pointing every
path at pytest's tmp dir, plus a writer that serialises it to disk. Used by
the config / reload / doctor suites so each test mutates one key from a
known-good baseline instead of restating the whole file."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml


def make_config_dict(tmp: Path) -> dict[str, Any]:
    """A fully valid config tree with every required key present."""
    return {
        "discord": {
            "token_file": str(tmp / "token"),
            "guild_id": 100,
            "channels": {"intel_alerts": 201, "intel_live": 202, "health": 203},
            "roles": {"pilot": 301, "fc": 302},
            "watch_voice_channels": [401],
            "auto_join": True,
        },
        "wake": {
            "model": str(tmp / "wake.onnx"),
            "threshold": 0.55,
            "refractory_ms": 2000,
        },
        "capture": {
            "preroll_ms": 300,
            "endpoint_silence_ms": 1000,
            "max_utterance_ms": 12000,
            "vad_aggressiveness": 2,
        },
        "stt": {
            "backend": "faster-whisper",
            "model": "small",
            "compute_type": "int8",
            "cpu_threads": 2,
        },
        "matching": {
            "phonetic_weight": 0.6,
            "text_weight": 0.4,
            "tiers": {"high_min": 0.80, "high_margin": 0.12, "medium_min": 0.55},
            "priors": {
                "recency_weight": 0.35,
                "recency_window_min": 10,
                "proximity_weight": 0.25,
                "proximity_max_jumps": 5,
                "reporter_history_weight": 0.15,
                "home_weight": 0.10,
            },
        },
        "incidents": {"dedupe_window_s": 90, "stale_after_min": 20, "cancel_window_s": 30},
        "discipline": {
            "user_cooldown_s": 30,
            "circuit_breaker": {"max_mentions": 12, "window_min": 10},
        },
        "tts": {
            "enabled": True,
            "voice": str(tmp / "voice.onnx"),
            "binary": str(tmp / "piper"),
            "max_utterance_s": 3,
        },
        "gazetteer": {
            "file": str(tmp / "gazetteer.yaml"),
            "home_system": None,
            "include_all": False,
        },
        "ipc": {"socket": str(tmp / "run" / "cortana.sock")},
        "health": {"report_interval_min": 60, "voice_silence_alarm_s": 60},
        "database": {"path": str(tmp / "cortana.db")},
    }


@pytest.fixture
def config_dict(tmp_path: Path) -> dict[str, Any]:
    return make_config_dict(tmp_path)


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[dict[str, Any]], Path]:
    def _write(data: dict[str, Any], name: str = "cortana.yaml") -> Path:
        path = tmp_path / name
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    return _write
