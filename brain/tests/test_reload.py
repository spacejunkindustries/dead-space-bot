"""Reload-transaction tests: all-or-nothing swap, reload-class bucketing,
engine reloaders, sighup appliers, and the operator receipt."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from cortana.config import AuraConfig, ConfigHolder
from cortana.reload import ReloadResult, reload_all


def _write(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def holder(config_dict, write_config) -> ConfigHolder:
    return ConfigHolder(write_config(config_dict))


async def test_no_changes_is_a_clean_receipt(holder) -> None:
    result = await reload_all(holder)
    assert result.ok and result.swapped
    assert result.applied == ()
    assert result.restart_pending == ()
    assert result.rejected == ()
    assert "No changes detected." in result.lines()


async def test_hot_key_applies_and_is_listed(holder, config_dict) -> None:
    config_dict["wake"]["threshold"] = 0.7
    _write(holder.path, config_dict)
    result = await reload_all(holder)
    assert result.ok
    assert "wake.threshold" in result.applied
    assert holder.current.wake.threshold == 0.7


async def test_restart_key_is_reported_pending_not_silently_absorbed(holder, config_dict) -> None:
    config_dict["stt"]["model"] = "base"
    _write(holder.path, config_dict)
    result = await reload_all(holder)
    assert result.ok
    assert result.restart_pending == ("stt.model",)
    assert any("Restart pending" in line for line in result.lines())


async def test_invalid_cortana_yaml_rejects_and_keeps_old_config(holder, config_dict) -> None:
    old = holder.current
    config_dict["wake"]["threshold"] = 5.0
    _write(holder.path, config_dict)
    result = await reload_all(holder)
    assert not result.ok and not result.swapped
    assert result.applied == ()
    assert any("cortana.yaml" in r and "wake.threshold" in r for r in result.rejected)
    assert holder.current is old
    assert any("REJECTED" in line for line in result.lines())


async def test_file_validator_failure_rejects_everything(holder, config_dict) -> None:
    old = holder.current
    config_dict["wake"]["threshold"] = 0.7  # a perfectly valid edit
    _write(holder.path, config_dict)

    def bad_gazetteer(_cfg: AuraConfig) -> None:
        raise ValueError("within_jumps_of.system 'Otanumi' is not in the systems table")

    result = await reload_all(holder, file_validators={"gazetteer.yaml": bad_gazetteer})
    assert not result.ok and not result.swapped
    assert holder.current is old  # the valid cortana.yaml edit did NOT apply
    assert any("gazetteer.yaml" in r for r in result.rejected)


async def test_validators_see_the_candidate_config(holder, config_dict) -> None:
    config_dict["gazetteer"]["include_all"] = True
    _write(holder.path, config_dict)
    seen: list[bool] = []

    def spy(cfg: AuraConfig) -> None:
        seen.append(cfg.gazetteer.include_all)

    result = await reload_all(holder, file_validators={"gazetteer.yaml": spy})
    assert result.ok
    assert seen == [True]


async def test_engine_reloaders_always_run(holder) -> None:
    # gazetteer.yaml / routing.yaml may change on disk without any
    # cortana.yaml key changing — the engines must rebuild regardless.
    calls: list[str] = []

    async def gaz(_cfg: AuraConfig) -> None:
        calls.append("gazetteer")

    def routing(_cfg: AuraConfig) -> None:  # sync reloaders work too
        calls.append("routing")

    result = await reload_all(holder, engine_reloaders={"gazetteer": gaz, "routing": routing})
    assert result.ok
    assert calls == ["gazetteer", "routing"]
    assert set(result.engine_reload) == {"gazetteer", "routing"}


async def test_engine_reloader_failure_reports_but_config_stays_swapped(
    holder, config_dict
) -> None:
    config_dict["wake"]["threshold"] = 0.7
    _write(holder.path, config_dict)

    async def broken(_cfg: AuraConfig) -> None:
        raise RuntimeError("systems table is empty")

    result = await reload_all(holder, engine_reloaders={"gazetteer": broken})
    assert result.swapped and not result.ok
    assert holder.current.wake.threshold == 0.7  # swap already happened
    assert any("engine gazetteer" in r for r in result.rejected)
    assert any("WITH ERRORS" in line for line in result.lines())


async def test_applier_runs_only_when_its_key_changed(holder, config_dict) -> None:
    calls: list[str] = []

    async def personality(cfg: AuraConfig) -> None:
        calls.append(cfg.tts.personality)

    result = await reload_all(holder, appliers={"tts.personality": personality})
    assert result.ok and calls == []  # nothing changed

    config_dict["tts"]["personality"] = "cortana"
    _write(holder.path, config_dict)
    result = await reload_all(holder, appliers={"tts.personality": personality})
    assert result.ok
    assert calls == ["cortana"]
    assert "tts.personality" in result.applied


async def test_applier_prefix_matches_whole_section(holder, config_dict) -> None:
    calls: list[int] = []

    def chat_applier(cfg: AuraConfig) -> None:
        calls.append(cfg.chat.max_tokens)

    config_dict["chat"] = {"enabled": False, "max_tokens": 512}
    _write(holder.path, config_dict)
    result = await reload_all(holder, appliers={"chat": chat_applier})
    assert result.ok
    assert calls == [512]


async def test_applier_failure_is_reported(holder, config_dict) -> None:
    config_dict["tts"]["personality"] = "bratty"
    _write(holder.path, config_dict)

    def broken(_cfg: AuraConfig) -> None:
        raise RuntimeError("piper exploded")

    result = await reload_all(holder, appliers={"tts.personality": broken})
    assert result.swapped and not result.ok
    assert any("applier tts.personality" in r for r in result.rejected)


def test_receipt_lines_are_phone_readable() -> None:
    result = ReloadResult(
        ok=True,
        applied=("wake.threshold",),
        engine_reload=("gazetteer",),
        restart_pending=("stt.model",),
        swapped=True,
    )
    text = result.summary()
    assert "Reload applied." in text
    assert "Applied: wake.threshold" in text
    assert "Engines reloaded: gazetteer" in text
    assert "Restart pending" in text and "stt.model" in text
