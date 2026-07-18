"""Config loader tests: the schema-driven validator, coercion, unknown-key
rejection, cross-field checks, reload-class diffing, and golden loads of the
shipped example files."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from functools import reduce
from pathlib import Path
from typing import Any

import pytest

from cortana.config import (
    AuraConfig,
    ConfigError,
    RoutingFileConfig,
    _build_stt,
    _build_wake,
    diff_configs,
    load_config,
)
from cortana.config_schema import CROSS_CHECKS, KEYS, REQUIRED, Reload, Section

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


def test_wake_extra_models_defaults_empty_and_preserves_path_case() -> None:
    assert _build_wake(_wake({})).extra_models == ()
    cfg = _build_wake(
        _wake({"extra_models": ["/opt/models/Hey_Jarvis.onnx", "/opt/models/glados.onnx"]})
    )
    # Linux paths are case-sensitive — the list must NOT be lowercased.
    assert cfg.extra_models == ("/opt/models/Hey_Jarvis.onnx", "/opt/models/glados.onnx")


def test_wake_extra_models_rejects_non_strings_and_non_lists() -> None:
    with pytest.raises(ConfigError, match=r"wake.extra_models\[1\]: expected str"):
        _build_wake(_wake({"extra_models": ["/ok.onnx", 7]}))
    with pytest.raises(ConfigError, match=r"wake.extra_models: expected list"):
        _build_wake(_wake({"extra_models": "/ok.onnx"}))


def test_wake_extra_models_shares_wake_model_reload_class() -> None:
    # Same reload class as wake.model: a SIGHUP list change rebuilds the
    # per-user model banks through the pool, no restart.
    from cortana.config_schema import key_by_path

    assert key_by_path("wake.extra_models").reload is Reload.SIGHUP
    assert key_by_path("wake.model").reload is Reload.SIGHUP


def test_personality_accepts_bratty() -> None:
    from cortana.config import _personality

    assert _personality("bratty") == "bratty"
    with pytest.raises(ConfigError, match="tts.personality"):
        _personality("feral")


def test_unquoted_yaml_off_is_accepted_for_offable_keys() -> None:
    # YAML 1.1 parses a bare `off` as boolean False — an unquoted
    # `join_announcement: off` once crash-looped a deployment.
    from cortana.config import _build_discord
    from cortana.config import _build_stt as build_stt

    base = {
        "discord": {
            "token_file": "/dev/null",
            "guild_id": 1,
            "channels": {"intel_alerts": 1, "intel_live": 2, "health": 3},
            "roles": {"pilot": 1, "fc": 2},
            "watch_voice_channels": [9],
            "join_announcement": False,  # what YAML gives you for bare `off`
        }
    }
    assert _build_discord(base).join_announcement == "off"
    stt = {
        "stt": {
            "backend": "faster-whisper",
            "model": "small",
            "compute_type": "int8",
            "cpu_threads": 2,
            "whisper_cpp_url": "http://x/",
            "relay_mode": False,
        }
    }
    assert build_stt(stt).relay_mode == "off"


def _discord_base() -> dict[str, object]:
    return {
        "token_file": "/dev/null",
        "guild_id": 1,
        "channels": {"intel_alerts": 1, "intel_live": 2, "health": 3},
        "watch_voice_channels": [9],
    }


def test_roles_section_is_optional() -> None:
    # An empty `roles:` header (every key commented out) parses as None and
    # once crash-looped a deployment with "expected a mapping, got NoneType".
    from cortana.config import _build_discord

    for roles in ({}, None, "absent"):
        d = _discord_base()
        if roles != "absent":
            d["roles"] = roles
        cfg = _build_discord({"discord": d})
        assert cfg.roles.pilot == 0
        assert cfg.roles.fc == 0


def test_partial_roles_section_keeps_the_configured_gate() -> None:
    from cortana.config import _build_discord

    d = _discord_base()
    d["roles"] = {"pilot": 42}
    cfg = _build_discord({"discord": d})
    assert cfg.roles.pilot == 42
    assert cfg.roles.fc == 0


def test_empty_required_section_names_the_missing_key() -> None:
    # `channels:` with nothing under it must say which KEY is missing, not
    # "expected a mapping, got NoneType".
    from cortana.config import _build_discord

    d = _discord_base()
    d["channels"] = None
    with pytest.raises(ConfigError, match="discord.channels.intel_alerts: missing required key"):
        _build_discord({"discord": d})


# ── schema completeness ──────────────────────────────────────────────────────

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _leaf_paths(obj: Any, prefix: str = "") -> Iterator[str]:
    for f in dataclasses.fields(obj):
        value = getattr(obj, f.name)
        if dataclasses.is_dataclass(value):
            yield from _leaf_paths(value, f"{prefix}{f.name}.")
        else:
            yield f"{prefix}{f.name}"


def test_every_dataclass_field_has_a_key_and_vice_versa(config_dict, write_config) -> None:
    cfg = load_config(write_config(config_dict))
    assert set(_leaf_paths(cfg)) == {k.path for k in KEYS}


def test_schema_defaults_match_dataclass_defaults(config_dict, write_config) -> None:
    # The dataclass defaults exist for direct construction in tests/tools;
    # they must never drift from the schema (the single source of truth).
    cfg = load_config(write_config(config_dict))
    for key in KEYS:
        if key.default is REQUIRED:
            continue
        parent = reduce(getattr, key.path.split(".")[:-1], cfg)
        f = next(f for f in dataclasses.fields(parent) if f.name == key.name)
        if f.default is not dataclasses.MISSING:
            assert f.default == key.default, key.path


def test_every_key_lives_in_a_declared_section() -> None:
    from cortana.config_schema import SECTIONS

    section_paths = {s.path for s in SECTIONS}
    for key in KEYS:
        assert key.section in section_paths, key.path


def test_optional_sections_only_carry_defaulted_keys() -> None:
    # An absent optional section must be loadable — so every key under one
    # needs a default.
    from cortana.config_schema import SECTIONS

    optional = {s.path for s in SECTIONS if s.optional}
    for key in KEYS:
        if key.section in optional:
            assert key.default is not REQUIRED, key.path


def test_section_helpers() -> None:
    s = Section("discord.channels", "doc")
    assert s.name == "channels"
    assert s.parent == "discord"
    assert Section("wake", "doc").parent == ""


# ── golden files ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["cortana.yaml.example", "cortana.dev.yaml"])
def test_shipped_config_files_load_unchanged(name: str) -> None:
    cfg = load_config(_CONFIG_DIR / name)
    assert isinstance(cfg, AuraConfig)
    assert cfg.stt.watchdog_s == 15.0
    assert cfg.routing.file in ("", "/etc/cortana/routing.yaml")


# ── unknown-key rejection ────────────────────────────────────────────────────


def test_unknown_key_rejected_with_did_you_mean(config_dict, write_config) -> None:
    config_dict["wake"].pop("threshold")
    config_dict["wake"]["treshold"] = 0.5
    pattern = r"wake: unknown key 'treshold'.*did you mean 'threshold'"
    with pytest.raises(ConfigError, match=pattern):
        load_config(write_config(config_dict))


def test_unknown_top_level_section_rejected(config_dict, write_config) -> None:
    config_dict["wakee"] = {"threshold": 0.5}
    with pytest.raises(ConfigError, match=r"top level: unknown key 'wakee'.*did you mean 'wake'"):
        load_config(write_config(config_dict))


def test_unknown_key_without_close_match_still_rejected(config_dict, write_config) -> None:
    config_dict["incidents"]["zzz_bogus"] = 1
    with pytest.raises(ConfigError, match=r"incidents: unknown key 'zzz_bogus' — Fix:"):
        load_config(write_config(config_dict))


def test_misspelled_mentions_enabled_can_no_longer_silently_default(
    config_dict, write_config
) -> None:
    # The audit's headline silent-wrong case: a typo used to revert the key
    # to its default with zero evidence.
    config_dict["discord"]["mentions_enabld"] = False
    with pytest.raises(ConfigError, match=r"did you mean 'mentions_enabled'"):
        load_config(write_config(config_dict))


# ── error contract ───────────────────────────────────────────────────────────


def test_errors_carry_a_fix_hint(config_dict, write_config) -> None:
    cases: list[dict[str, Any]] = []
    d1 = dict(config_dict)
    d1["wake"] = {**config_dict["wake"], "threshold": 1.5}  # range
    cases.append(d1)
    d2 = dict(config_dict)
    d2["wake"] = {k: v for k, v in config_dict["wake"].items() if k != "model"}  # missing
    cases.append(d2)
    d3 = dict(config_dict)
    d3["tts"] = {**config_dict["tts"], "effect": "holo"}  # choices
    cases.append(d3)
    for data in cases:
        with pytest.raises(ConfigError, match=r" — Fix: "):
            load_config(write_config(data))


# ── generic YAML 1.1 coercion for choices-keys ───────────────────────────────


def test_yaml_true_for_a_choices_key_without_a_true_word_is_rejected() -> None:
    # `join_announcement: on` parses as True; no legal value spells it, so
    # the coercion must NOT invent one.
    from cortana.config import _build_discord

    d = _discord_base()
    d["join_announcement"] = True
    with pytest.raises(ConfigError, match=r"discord.join_announcement: must be one of"):
        _build_discord({"discord": d})


def test_here_on_severity_members_are_validated() -> None:
    from cortana.config import _build_discord

    d = _discord_base()
    d["here_on_severity"] = ["high", "RED"]
    with pytest.raises(ConfigError, match=r"discord.here_on_severity\[1\]: must be one of"):
        _build_discord({"discord": d})
    d["here_on_severity"] = ["HIGH", "medium"]
    assert _build_discord({"discord": d}).here_on_severity == ("high", "medium")


def test_tts_effect_is_validated(config_dict, write_config) -> None:
    config_dict["tts"]["effect"] = "Holographic"
    assert load_config(write_config(config_dict)).tts.effect == "holographic"
    config_dict["tts"]["effect"] = "holo"
    with pytest.raises(ConfigError, match=r"tts.effect: must be one of none\|holographic"):
        load_config(write_config(config_dict))


# ── cross-field checks ───────────────────────────────────────────────────────


def test_matching_weights_must_sum_to_one(config_dict, write_config) -> None:
    config_dict["matching"]["phonetic_weight"] = 0.5
    with pytest.raises(ConfigError, match=r"phonetic_weight \+ text_weight must sum to 1.0"):
        load_config(write_config(config_dict))


def test_tier_ordering_enforced(config_dict, write_config) -> None:
    config_dict["matching"]["tiers"]["medium_min"] = 0.9
    with pytest.raises(ConfigError, match=r"matching.tiers.medium_min: must be <="):
        load_config(write_config(config_dict))


def test_preroll_must_fit_the_privacy_ring(config_dict, write_config) -> None:
    from cortana.audio.capture import RING_MS

    config_dict["capture"]["preroll_ms"] = RING_MS + 1
    with pytest.raises(ConfigError, match=r"capture.preroll_ms: exceeds the fixed"):
        load_config(write_config(config_dict))


def test_whisper_cpp_url_required_only_for_whisper_cpp(config_dict, write_config) -> None:
    # Deleting the line while on faster-whisper is fine now (the audit's
    # tidy-the-config crash-loop); whisper-cpp still requires it non-empty.
    cfg = load_config(write_config(config_dict))
    assert cfg.stt.whisper_cpp_url  # default supplied
    config_dict["stt"]["backend"] = "whisper-cpp"
    config_dict["stt"]["whisper_cpp_url"] = ""
    with pytest.raises(ConfigError, match=r"stt.whisper_cpp_url: required when"):
        load_config(write_config(config_dict))


def test_cross_checks_registry_covers_the_documented_rules() -> None:
    names = {c.name for c in CROSS_CHECKS}
    assert {
        "matching_weights_sum",
        "matching_tier_order",
        "capture_preroll_fits_ring",
        "stt_whisper_cpp_url_required",
    } <= names


# ── section shape ────────────────────────────────────────────────────────────


def test_chat_section_missing_or_empty_means_off(config_dict, write_config) -> None:
    cfg = load_config(write_config(config_dict))  # no chat: at all
    assert cfg.chat.enabled is False
    config_dict["chat"] = None  # bare `chat:` header
    cfg = load_config(write_config(config_dict))
    assert cfg.chat.enabled is False


def test_chat_section_non_mapping_raises(config_dict, write_config) -> None:
    # Changed behaviour (audit finding config.py:574): a scalar `chat:` used
    # to silently disable the paid feature; it is now a hard error like
    # every other section.
    config_dict["chat"] = "enabled"
    with pytest.raises(ConfigError, match=r"chat: expected a mapping, got str"):
        load_config(write_config(config_dict))


def test_routing_section_defaults_and_resolution(config_dict, write_config, tmp_path) -> None:
    cfg = load_config(write_config(config_dict))
    assert cfg.routing.file == ""
    assert cfg.routing.resolve(tmp_path / "cortana.yaml") == tmp_path / "routing.yaml"
    config_dict["routing"] = {"file": "/etc/cortana/other.yaml"}
    cfg = load_config(write_config(config_dict))
    assert cfg.routing.resolve(tmp_path / "cortana.yaml") == Path("/etc/cortana/other.yaml")


def test_stt_watchdog_s_validated(config_dict, write_config) -> None:
    config_dict["stt"]["watchdog_s"] = 0
    with pytest.raises(ConfigError, match=r"stt.watchdog_s: must be > 0"):
        load_config(write_config(config_dict))
    config_dict["stt"]["watchdog_s"] = 20
    assert load_config(write_config(config_dict)).stt.watchdog_s == 20.0


# ── diff_configs / reload classes ────────────────────────────────────────────


def test_diff_configs_buckets_by_reload_class(config_dict, write_config) -> None:
    old = load_config(write_config(config_dict))
    config_dict["wake"]["threshold"] = 0.6  # hot
    config_dict["tts"]["personality"] = "cortana"  # sighup
    config_dict["gazetteer"]["home_system"] = "Jita"  # engine
    config_dict["stt"]["model"] = "base"  # restart
    new = load_config(write_config(config_dict))
    buckets = diff_configs(old, new)
    assert buckets[Reload.HOT] == ("wake.threshold",)
    assert buckets[Reload.SIGHUP] == ("tts.personality",)
    assert buckets[Reload.ENGINE] == ("gazetteer.home_system",)
    assert buckets[Reload.RESTART] == ("stt.model",)


def test_diff_configs_no_changes_is_all_empty(config_dict, write_config) -> None:
    cfg = load_config(write_config(config_dict))
    buckets = diff_configs(cfg, cfg)
    assert all(paths == () for paths in buckets.values())
    assert set(buckets) == set(Reload)


def test_restart_bound_keys_are_classified_restart() -> None:
    from cortana.config_schema import key_by_path

    for path in (
        "stt.backend",
        "stt.model",
        "stt.watchdog_s",
        "capture.vad_aggressiveness",
        "ipc.socket",
        "database.path",
        "discord.token_file",
    ):
        assert key_by_path(path).reload is Reload.RESTART, path
    # The wake model pool rebuilds per-user models live on config-generation
    # change (audio workstream), so wake.model / wake.vad_threshold are
    # sighup-class — the receipt must not claim a restart is needed when the
    # detector already applied the edit (review finding).
    for path in ("wake.model", "wake.vad_threshold"):
        assert key_by_path(path).reload is Reload.SIGHUP, path


def test_routing_file_config_is_frozen_default() -> None:
    assert RoutingFileConfig().file == ""
