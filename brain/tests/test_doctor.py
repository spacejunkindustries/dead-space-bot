"""Doctor tests — every offline check against tmp-dir fixtures (fake model
files, fake configs, a real migrated sqlite db), plus the online check set
against an injected fake Discord client. No test touches the network; the
doctor itself must never write anything."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord
import pytest
import yaml

from cortana.doctor import (
    EXIT_CONFIG_INVALID,
    EXIT_FAIL,
    EXIT_OK,
    FIRST_INSTALL_MARKER,
    CheckResult,
    DoctorContext,
    Status,
    apply_first_install,
    compute_channel_permissions,
    exit_code,
    main,
    render,
    run_offline_checks,
    run_online_checks,
)

_VIEW = 1 << 10
_SEND = 1 << 11
_EMBED = 1 << 14
_FULL = _VIEW | _SEND | _EMBED

_ROUTING_YAML = """\
- role: "@Home-Defense"
  types: [UNDER_ATTACK, ASSIST_REQUEST]
  escalate_at: UNDER_ATTACK
"""


def _fake_run_ok(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(cmd, 0, b"usage: piper", b"")


def _seed_db(path: Path) -> None:
    from cortana.core import db

    conn = db.connect(path)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO systems (id, name, region, constellation, metaphone) "
        "VALUES (1, 'Jita', 'The Forge', 'Kimotoro', 'JT')"
    )
    conn.commit()
    conn.close()


@pytest.fixture
def healthy(tmp_path: Path, config_dict, write_config, monkeypatch) -> DoctorContext:
    """A tmp-dir deployment where every offline check should PASS."""
    (tmp_path / "wake.onnx").write_bytes(b"onnx")
    (tmp_path / "melspectrogram.onnx").write_bytes(b"m")
    (tmp_path / "embedding_model.onnx").write_bytes(b"e")
    (tmp_path / "voice.onnx").write_bytes(b"v")
    (tmp_path / "voice.onnx.json").write_text("{}", encoding="utf-8")
    (tmp_path / "gazetteer.yaml").write_text("include_all: true\n", encoding="utf-8")
    (tmp_path / "routing.yaml").write_text(_ROUTING_YAML, encoding="utf-8")
    token = tmp_path / "token"
    token.write_text("fake-token", encoding="utf-8")
    token.chmod(0o600)
    (tmp_path / "run").mkdir()
    _seed_db(tmp_path / "cortana.db")
    # Feature models come from the siblings above, never from an installed
    # openwakeword package (present in prod, absent in CI — keep both alike).
    monkeypatch.setattr("cortana.doctor._openwakeword_resources", lambda: None)
    return DoctorContext(
        config_path=write_config(config_dict),
        env={},
        run_binary=_fake_run_ok,
    )


def _by_check(results: list[CheckResult]) -> dict[str, CheckResult]:
    out: dict[str, CheckResult] = {}
    for r in results:
        assert r.check not in out, f"duplicate check name {r.check}"
        out[r.check] = r
    return out


# ── offline: the healthy baseline ────────────────────────────────────────────


def test_healthy_deployment_all_pass(healthy) -> None:
    results = run_offline_checks(healthy)
    failing = [r for r in results if r.status is not Status.PASS]
    assert failing == [], failing
    assert exit_code(results) == EXIT_OK
    names = {r.check for r in results}
    assert {
        "config",
        "gazetteer.yaml",
        "routing.yaml",
        "wake.model",
        "wake.features",
        "stt.model",
        "tts.voice",
        "tts.voice.json",
        "tts.binary",
        "credentials.token",
        "database",
        "gazetteer.rows",
        "ipc.socket",
        "discord.ids",
    } <= names


def test_doctor_never_writes(healthy, tmp_path) -> None:
    before = sorted(p for p in tmp_path.rglob("*"))
    run_offline_checks(healthy)
    after = sorted(p for p in tmp_path.rglob("*"))
    assert before == after


# ── offline: config failures → exit 78 ───────────────────────────────────────


def test_broken_cortana_yaml_short_circuits_with_78(tmp_path) -> None:
    path = tmp_path / "cortana.yaml"
    path.write_text("discord: [not, a, mapping]\n", encoding="utf-8")
    ctx = DoctorContext(config_path=path, env={}, run_binary=_fake_run_ok)
    results = run_offline_checks(ctx)
    assert len(results) == 1
    assert results[0].status is Status.FAIL and results[0].config_error
    assert exit_code(results) == EXIT_CONFIG_INVALID


def test_unknown_key_reported_with_did_you_mean(healthy) -> None:
    data = yaml.safe_load(healthy.config_path.read_text())
    data["wake"]["treshold"] = 0.5
    healthy.config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    results = run_offline_checks(DoctorContext(healthy.config_path, env={}))
    assert exit_code(results) == EXIT_CONFIG_INVALID
    assert "did you mean 'threshold'" in results[0].detail


def test_broken_gazetteer_yaml_is_config_invalid(healthy, tmp_path) -> None:
    (tmp_path / "gazetteer.yaml").write_text("include_all: 3\n", encoding="utf-8")
    results = _by_check(run_offline_checks(healthy))
    assert results["gazetteer.yaml"].status is Status.FAIL
    assert exit_code(list(results.values())) == EXIT_CONFIG_INVALID


def test_broken_routing_yaml_is_config_invalid(healthy, tmp_path) -> None:
    (tmp_path / "routing.yaml").write_text("- role: 42\n  types: [UNDER_ATTACK]\n")
    results = _by_check(run_offline_checks(healthy))
    assert results["routing.yaml"].status is Status.FAIL
    assert "role" in results["routing.yaml"].detail


def test_missing_default_routing_yaml_warns(healthy, tmp_path) -> None:
    (tmp_path / "routing.yaml").unlink()
    results = _by_check(run_offline_checks(healthy))
    assert results["routing.yaml"].status is Status.WARN
    assert "ZERO routing rules" in results["routing.yaml"].detail
    assert exit_code(list(results.values())) == EXIT_OK


def test_missing_explicit_routing_file_fails(healthy, tmp_path) -> None:
    data = yaml.safe_load(healthy.config_path.read_text())
    data["routing"] = {"file": str(tmp_path / "nope.yaml")}
    healthy.config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    results = _by_check(run_offline_checks(healthy))
    assert results["routing.yaml"].status is Status.FAIL


# ── offline: referenced files ────────────────────────────────────────────────


def test_missing_wake_model_fails(healthy, tmp_path) -> None:
    (tmp_path / "wake.onnx").unlink()
    results = _by_check(run_offline_checks(healthy))
    assert results["wake.model"].status is Status.FAIL
    assert exit_code(list(results.values())) == EXIT_FAIL


def test_missing_wake_feature_models_warn(healthy, tmp_path) -> None:
    (tmp_path / "melspectrogram.onnx").unlink()
    results = _by_check(run_offline_checks(healthy))
    assert results["wake.features"].status is Status.WARN
    assert "melspectrogram" in results["wake.features"].detail


def test_whisper_model_name_passes_and_path_must_exist(healthy, tmp_path) -> None:
    results = _by_check(run_offline_checks(healthy))
    assert results["stt.model"].status is Status.PASS  # bare name "small"
    data = yaml.safe_load(healthy.config_path.read_text())
    data["stt"]["model"] = str(tmp_path / "models" / "whisper-small")
    healthy.config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    results = _by_check(run_offline_checks(healthy))
    assert results["stt.model"].status is Status.FAIL


def test_missing_piper_voice_and_sidecar_fail(healthy, tmp_path) -> None:
    (tmp_path / "voice.onnx").unlink()
    (tmp_path / "voice.onnx.json").unlink()
    results = _by_check(run_offline_checks(healthy))
    assert results["tts.voice"].status is Status.FAIL
    assert results["tts.voice.json"].status is Status.FAIL


def test_piper_binary_failures(healthy) -> None:
    def missing(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError(cmd[0])

    healthy.run_binary = missing
    results = _by_check(run_offline_checks(healthy))
    assert results["tts.binary"].status is Status.FAIL

    def nonzero(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 2, b"", b"boom")

    healthy.run_binary = nonzero
    results = _by_check(run_offline_checks(healthy))
    assert results["tts.binary"].status is Status.WARN


def test_tts_disabled_skips_voice_checks(healthy) -> None:
    data = yaml.safe_load(healthy.config_path.read_text())
    data["tts"]["enabled"] = False
    healthy.config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    results = _by_check(run_offline_checks(healthy))
    assert results["tts"].status is Status.PASS
    assert "tts.binary" not in results


# ── offline: credentials ─────────────────────────────────────────────────────


def test_missing_token_fails(healthy, tmp_path) -> None:
    (tmp_path / "token").unlink()
    results = _by_check(run_offline_checks(healthy))
    assert results["credentials.token"].status is Status.FAIL
    assert "LoadCredential" in results["credentials.token"].fix_hint


def test_world_readable_token_warns(healthy, tmp_path) -> None:
    (tmp_path / "token").chmod(0o644)
    results = _by_check(run_offline_checks(healthy))
    assert results["credentials.token"].status is Status.WARN
    assert "chmod 600" in results["credentials.token"].fix_hint


def test_credentials_directory_wins_over_token_file(healthy, tmp_path) -> None:
    cred_dir = tmp_path / "creds"
    cred_dir.mkdir()
    cred = cred_dir / "token"
    cred.write_text("t", encoding="utf-8")
    cred.chmod(0o600)
    (tmp_path / "token").unlink()  # the dev fallback is gone
    healthy.env = {"CREDENTIALS_DIRECTORY": str(cred_dir)}
    results = _by_check(run_offline_checks(healthy))
    assert results["credentials.token"].status is Status.PASS


def test_anthropic_key_checked_only_when_chat_enabled(healthy, tmp_path) -> None:
    results = _by_check(run_offline_checks(healthy))
    assert "credentials.anthropic" not in results
    data = yaml.safe_load(healthy.config_path.read_text())
    data["chat"] = {"enabled": True, "api_key_file": str(tmp_path / "anthropic")}
    healthy.config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    results = _by_check(run_offline_checks(healthy))
    assert results["credentials.anthropic"].status is Status.FAIL


# ── offline: database + gazetteer rows ───────────────────────────────────────


def test_missing_database_warns_and_skips_rows(healthy, tmp_path) -> None:
    (tmp_path / "cortana.db").unlink()
    results = _by_check(run_offline_checks(healthy))
    assert results["database"].status is Status.WARN
    assert "gazetteer.rows" not in results
    assert not (tmp_path / "cortana.db").exists()  # the doctor did not create it


def test_database_ahead_of_migrations_fails(healthy, tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "cortana.db")
    conn.execute("PRAGMA user_version = 9999")
    conn.close()
    results = _by_check(run_offline_checks(healthy))
    assert results["database"].status is Status.FAIL
    assert "AHEAD" in results["database"].detail


def test_database_behind_migrations_warns(healthy, tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "cortana.db")
    conn.execute("PRAGMA user_version = 1")
    conn.close()
    results = _by_check(run_offline_checks(healthy))
    assert results["database"].status is Status.WARN


def test_garbage_database_without_live_writer_fails(healthy, tmp_path) -> None:
    (tmp_path / "cortana.db").write_bytes(b"this is definitely not sqlite")
    results = _by_check(run_offline_checks(healthy))
    assert results["database"].status is Status.FAIL
    assert "restore the nightly backup" in results["database"].fix_hint


def test_unreadable_database_under_live_writer_warns(healthy, tmp_path) -> None:
    # The install gate runs while the OLD brain is still live and writing:
    # an immutable read racing a WAL checkpoint can error. WAL side files are
    # the evidence of that live writer — degrade to WARN, never abort a valid
    # deploy with a bogus "corrupt database" verdict.
    (tmp_path / "cortana.db").write_bytes(b"this is definitely not sqlite")
    (tmp_path / "cortana.db-wal").write_bytes(b"")
    results = _by_check(run_offline_checks(healthy))
    assert results["database"].status is Status.WARN
    assert "db busy" in results["database"].detail
    assert "check skipped" in results["database"].detail
    assert exit_code(list(results.values())) == EXIT_OK


def test_healthy_database_with_wal_sidecars_still_checks(healthy, tmp_path) -> None:
    # Side files alone must not skip the check — a readable db is read.
    (tmp_path / "cortana.db-wal").write_bytes(b"")
    (tmp_path / "cortana.db-shm").write_bytes(b"")
    results = _by_check(run_offline_checks(healthy))
    assert results["database"].status is Status.PASS
    assert results["gazetteer.rows"].status is Status.PASS


def test_empty_systems_table_fails_with_seed_hint(healthy, tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "cortana.db")
    conn.execute("DELETE FROM systems")
    conn.commit()
    conn.close()
    results = _by_check(run_offline_checks(healthy))
    assert results["gazetteer.rows"].status is Status.FAIL
    assert "cortana.nlu.seed" in results["gazetteer.rows"].fix_hint


# ── offline: ipc + placeholders ──────────────────────────────────────────────


def test_missing_socket_dir_warns(healthy, tmp_path) -> None:
    (tmp_path / "run").rmdir()
    results = _by_check(run_offline_checks(healthy))
    assert results["ipc.socket"].status is Status.WARN


def test_placeholder_ids_warn(healthy) -> None:
    data = yaml.safe_load(healthy.config_path.read_text())
    data["discord"]["guild_id"] = 0
    data["discord"]["channels"]["health"] = 0
    healthy.config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    results = _by_check(run_offline_checks(healthy))
    assert results["discord.ids"].status is Status.WARN
    assert "discord.guild_id" in results["discord.ids"].detail
    assert "discord.channels.health" in results["discord.ids"].detail


# ── first install (--first-install) ──────────────────────────────────────────


def _strip_assets(tmp_path: Path, ctx: DoctorContext) -> None:
    """A fresh droplet: config/token/db exist, human-installed assets do not."""
    (tmp_path / "wake.onnx").unlink()
    (tmp_path / "voice.onnx").unlink()
    (tmp_path / "voice.onnx.json").unlink()

    def missing(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError(cmd[0])

    ctx.run_binary = missing
    conn = sqlite3.connect(tmp_path / "cortana.db")
    conn.execute("DELETE FROM systems")
    conn.commit()
    conn.close()


def test_first_install_downgrades_missing_assets_to_warn(healthy, tmp_path) -> None:
    _strip_assets(tmp_path, healthy)
    results = apply_first_install(run_offline_checks(healthy))
    by = _by_check(results)
    for name in ("wake.model", "tts.voice", "tts.voice.json", "tts.binary", "gazetteer.rows"):
        assert by[name].status is Status.WARN, name
        assert FIRST_INSTALL_MARKER in by[name].detail, name
    assert exit_code(results) == EXIT_OK


def test_first_install_never_downgrades_config_errors(tmp_path) -> None:
    path = tmp_path / "cortana.yaml"
    path.write_text("discord: [not, a, mapping]\n", encoding="utf-8")
    ctx = DoctorContext(config_path=path, env={}, run_binary=_fake_run_ok)
    results = apply_first_install(run_offline_checks(ctx))
    assert results[0].status is Status.FAIL
    assert exit_code(results) == EXIT_CONFIG_INVALID


def test_first_install_leaves_non_asset_fails_alone(healthy, tmp_path) -> None:
    # A missing token is an operator mistake install.sh aborts on, not a
    # checklist asset — --first-install must not paper over it.
    (tmp_path / "token").unlink()
    results = apply_first_install(run_offline_checks(healthy))
    by = _by_check(results)
    assert by["credentials.token"].status is Status.FAIL
    assert exit_code(results) == EXIT_FAIL


def test_first_install_with_all_assets_present_marks_nothing(healthy) -> None:
    results = apply_first_install(run_offline_checks(healthy))
    assert all(FIRST_INSTALL_MARKER not in r.detail for r in results)
    assert exit_code(results) == EXIT_OK


def test_main_first_install_flag_returns_ok_and_marks_rows(
    healthy, tmp_path, capsys, monkeypatch
) -> None:
    # main() builds its own context (real env, real subprocess runner):
    # tts.binary points into tmp where no piper exists, wake model removed —
    # both downgrade under --first-install and the marker reaches stdout for
    # install.sh to grep.
    (tmp_path / "wake.onnx").unlink()
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    assert main(["--config", str(healthy.config_path)]) == EXIT_FAIL
    capsys.readouterr()
    code = main(["--config", str(healthy.config_path), "--first-install"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert FIRST_INSTALL_MARKER in out


# ── exit codes + rendering ───────────────────────────────────────────────────


def test_exit_code_precedence() -> None:
    ok = CheckResult("a", Status.PASS, "x")
    warn = CheckResult("b", Status.WARN, "x")
    fail = CheckResult("c", Status.FAIL, "x")
    conf = CheckResult("d", Status.FAIL, "x", config_error=True)
    assert exit_code([ok, warn]) == EXIT_OK
    assert exit_code([ok, fail]) == EXIT_FAIL
    assert exit_code([ok, fail, conf]) == EXIT_CONFIG_INVALID


def test_render_is_aligned_and_carries_fix_hints(healthy, tmp_path) -> None:
    (tmp_path / "wake.onnx").unlink()
    results = run_offline_checks(healthy)
    text = render(results, healthy.config_path)
    assert str(healthy.config_path) in text
    assert " FAIL" in text and " PASS" in text
    assert "fix: point wake.model" in text
    assert text.strip().splitlines()[-1].endswith("failed")


def test_main_returns_78_for_broken_config(tmp_path, capsys) -> None:
    path = tmp_path / "cortana.yaml"
    path.write_text("nonsense: {}\n", encoding="utf-8")
    code = main(["--config", str(path)])
    assert code == EXIT_CONFIG_INVALID
    out = capsys.readouterr().out
    assert "CORTANA doctor" in out and "FAIL" in out


# ── online checks (fake client — no network) ─────────────────────────────────


def _http_exc(status: int) -> discord.HTTPException:
    return discord.HTTPException(SimpleNamespace(status=status, reason="x"), "denied")


class FakeClient:
    def __init__(
        self,
        *,
        me: dict[str, Any] | None = None,
        roles: list[dict[str, Any]] | None = None,
        member: dict[str, Any] | None = None,
        channels: dict[int, Any] | None = None,
        login_exc: Exception | None = None,
    ) -> None:
        self.me = me or {"id": "999", "username": "CORTANA"}
        self.roles = roles if roles is not None else []
        self.member = member or {"roles": []}
        self.channels = channels or {}
        self.login_exc = login_exc
        self.closed = False

    async def login(self, token: str) -> dict[str, Any]:
        if self.login_exc is not None:
            raise self.login_exc
        return self.me

    async def channel(self, channel_id: int) -> dict[str, Any]:
        result = self.channels.get(channel_id)
        if result is None:
            raise _http_exc(404)
        if isinstance(result, Exception):
            raise result
        return result

    async def guild_roles(self, guild_id: int) -> list[dict[str, Any]]:
        return self.roles

    async def my_member(self, guild_id: int) -> dict[str, Any]:
        return self.member

    async def close(self) -> None:
        self.closed = True


def _guild_roles() -> list[dict[str, Any]]:
    return [
        {"id": "100", "name": "@everyone", "permissions": str(_FULL)},
        {"id": "301", "name": "Pilot", "permissions": "0"},
        {"id": "302", "name": "FC", "permissions": "0"},
        {"id": "500", "name": "Home-Defense", "permissions": "0"},
    ]


def _channels() -> dict[int, dict[str, Any]]:
    return {
        cid: {"id": str(cid), "name": f"chan-{cid}", "permission_overwrites": []}
        for cid in (201, 202, 203)
    }


async def test_online_all_good(healthy) -> None:
    client = FakeClient(roles=_guild_roles(), channels=_channels())
    results = _by_check(await run_online_checks(healthy, client))
    assert results["discord.auth"].status is Status.PASS
    for name in ("channel.intel_alerts", "channel.intel_live", "channel.health"):
        assert results[name].status is Status.PASS, name
    assert results["role.pilot"].status is Status.PASS
    assert results["role.fc"].status is Status.PASS
    assert results["routing.roles"].status is Status.PASS


async def test_online_channel_missing_send_fails(healthy) -> None:
    channels = _channels()
    channels[202]["permission_overwrites"] = [
        {"id": "100", "type": 0, "allow": "0", "deny": str(_SEND | _EMBED)}
    ]
    client = FakeClient(roles=_guild_roles(), channels=channels)
    results = _by_check(await run_online_checks(healthy, client))
    assert results["channel.intel_live"].status is Status.FAIL
    assert "SEND" in results["channel.intel_live"].detail
    assert "EMBED" in results["channel.intel_live"].detail


async def test_online_invisible_channel_fails(healthy) -> None:
    channels = _channels()
    del channels[203]
    client = FakeClient(roles=_guild_roles(), channels=channels)
    results = _by_check(await run_online_checks(healthy, client))
    assert results["channel.health"].status is Status.FAIL


async def test_online_bad_token_fails(healthy) -> None:
    client = FakeClient(login_exc=discord.LoginFailure("Improper token"))
    results = await run_online_checks(healthy, client)
    assert len(results) == 1
    assert results[0].status is Status.FAIL
    assert "token" in results[0].detail


async def test_online_no_network_degrades_to_warn(healthy) -> None:
    client = FakeClient(login_exc=OSError("network unreachable"))
    results = await run_online_checks(healthy, client)
    assert len(results) == 1
    assert results[0].status is Status.WARN
    assert "skipped" in results[0].detail


async def test_online_unknown_role_id_warns(healthy) -> None:
    roles = [r for r in _guild_roles() if r["id"] != "302"]
    client = FakeClient(roles=roles, channels=_channels())
    results = _by_check(await run_online_checks(healthy, client))
    assert results["role.fc"].status is Status.WARN


async def test_online_unresolved_routing_role_warns(healthy) -> None:
    roles = [r for r in _guild_roles() if r["name"] != "Home-Defense"]
    client = FakeClient(roles=roles, channels=_channels())
    results = _by_check(await run_online_checks(healthy, client))
    assert results["routing.roles"].status is Status.WARN
    assert "@Home-Defense" in results["routing.roles"].detail


async def test_online_missing_token_file_fails_before_any_network(healthy, tmp_path) -> None:
    (tmp_path / "token").unlink()
    results = await run_online_checks(healthy, FakeClient())
    assert results[0].check == "discord.auth"
    assert results[0].status is Status.FAIL


def test_compute_channel_permissions_admin_bypasses_overwrites() -> None:
    perms = compute_channel_permissions(
        me_id=1,
        member_role_ids={7},
        roles_by_id={100: 0, 7: 1 << 3},  # ADMINISTRATOR
        guild_id=100,
        overwrites=[{"id": "100", "type": 0, "allow": "0", "deny": str(_FULL)}],
    )
    assert perms & _SEND and perms & _VIEW and perms & _EMBED


def test_compute_channel_permissions_role_and_member_overwrites() -> None:
    base = {100: _FULL, 7: 0}
    denied = compute_channel_permissions(
        me_id=1,
        member_role_ids={7},
        roles_by_id=base,
        guild_id=100,
        overwrites=[{"id": "7", "type": 0, "allow": "0", "deny": str(_SEND)}],
    )
    assert not denied & _SEND and denied & _VIEW
    regranted = compute_channel_permissions(
        me_id=1,
        member_role_ids={7},
        roles_by_id=base,
        guild_id=100,
        overwrites=[
            {"id": "7", "type": 0, "allow": "0", "deny": str(_SEND)},
            {"id": "1", "type": 1, "allow": str(_SEND), "deny": "0"},
        ],
    )
    assert regranted & _SEND
