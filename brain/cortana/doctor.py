"""Preflight doctor — ``python -m cortana.doctor`` (GDD §17/§18).

Answers "will CORTANA actually run with this configuration?" BEFORE a
restart bricks the service at 02:00. Every check returns PASS / WARN / FAIL
plus a phone-readable fix hint; the output is one aligned table.

Offline checks (the default; safe anywhere, including ExecStartPre):

- all three YAML files parse through the REAL loaders (config / gazetteer /
  routing) — the same validation the service applies, not an approximation;
- every referenced file exists: wake model (+ melspec/embedding features),
  whisper model path, piper voice + sidecar json, piper binary answers
  ``--help``;
- credentials exist, are readable, and are not group/world-readable;
- the database opens read-only and its ``user_version`` is sane against the
  shipped migrations (refuses a database from a NEWER build);
- the gazetteer ``systems`` table is seeded;
- the IPC socket directory is writable;
- placeholder (all-zero) snowflake ids are flagged.

``--online`` additionally authenticates the Discord token, verifies every
configured channel is visible with SEND + EMBED permissions, and checks the
configured role ids exist — network failures degrade to WARN, never FAIL,
so a Discord outage cannot masquerade as a config failure.

``--first-install`` (used by install.sh before the first release is live)
downgrades FAILs for human-installed assets (``FIRST_INSTALL_ASSET_CHECKS``)
to WARN, marked ``[first-install]`` — a fresh droplet must be able to finish
its first deploy and print the setup checklist. Config errors still fail.

Database reads use ``immutable=1`` (the doctor never writes, not even WAL
side files); at the install gate the OLD brain may still be live and writing,
so read errors while ``-wal``/``-shm`` side files are present degrade to
WARN ("db busy") instead of aborting a valid deploy as "corrupt".

Hard rules: the doctor NEVER writes anything (read-only sqlite, no mkdir,
no socket bind) and NEVER starts an engine (no model loads — existence and
cheap metadata only; the single subprocess allowed is ``piper --help``).

Exit codes: 0 = all checks passed; 1 = at least one FAIL;
78 (EX_CONFIG) = a config file failed validation.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast

import yaml

from cortana.config import AuraConfig, ConfigError, load_config
from cortana.config_schema import KEYS

__all__ = [
    "EXIT_CONFIG_INVALID",
    "EXIT_FAIL",
    "EXIT_OK",
    "FIRST_INSTALL_ASSET_CHECKS",
    "FIRST_INSTALL_MARKER",
    "CheckResult",
    "DoctorContext",
    "Status",
    "apply_first_install",
    "exit_code",
    "main",
    "render",
    "run_offline_checks",
    "run_online_checks",
]

EXIT_OK = 0
EXIT_FAIL = 1
#: sysexits.h EX_CONFIG — pairs with RestartPreventExitStatus so a config
#: error stops the unit cleanly instead of crash-looping.
EXIT_CONFIG_INVALID = 78

_SUBPROCESS_TIMEOUT_S = 10.0
_ONLINE_TIMEOUT_S = 10.0

# Discord permission bits (https://docs.discord.com/developers/topics/permissions).
_PERM_ADMINISTRATOR = 1 << 3
_PERM_VIEW_CHANNEL = 1 << 10
_PERM_SEND_MESSAGES = 1 << 11
_PERM_EMBED_LINKS = 1 << 14


class Status(Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One row of the doctor table."""

    check: str
    status: Status
    detail: str
    fix_hint: str = ""
    #: True when this failure means "the configuration itself is invalid"
    #: (drives exit 78 instead of 1).
    config_error: bool = False


def _run_binary(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        cmd, capture_output=True, timeout=_SUBPROCESS_TIMEOUT_S, check=False
    )


@dataclass(slots=True)
class DoctorContext:
    """Everything a check may consult. Injectable for tests: ``env`` replaces
    ``os.environ`` and ``run_binary`` replaces the piper subprocess."""

    config_path: Path
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    run_binary: Callable[[list[str]], subprocess.CompletedProcess[bytes]] = _run_binary
    cfg: AuraConfig | None = None


Check = Callable[[DoctorContext], list[CheckResult]]


# ── offline checks ───────────────────────────────────────────────────────────


def check_config(ctx: DoctorContext) -> list[CheckResult]:
    try:
        ctx.cfg = load_config(ctx.config_path)
    except ConfigError as exc:
        return [
            CheckResult(
                "config",
                Status.FAIL,
                str(exc),
                "fix cortana.yaml before restarting — the service will refuse this file",
                config_error=True,
            )
        ]
    return [
        CheckResult(
            "config",
            Status.PASS,
            f"{ctx.config_path.name}: {len(KEYS)} keys validated",
        )
    ]


def check_gazetteer_yaml(ctx: DoctorContext) -> list[CheckResult]:
    assert ctx.cfg is not None
    # The REAL scope-file loader (not the full Gazetteer — that needs the db
    # and would count as starting an engine).
    from cortana.nlu.gazetteer import GazetteerError, _load_scope

    path = Path(ctx.cfg.gazetteer.file)
    try:
        _load_scope(path)
    except GazetteerError as exc:
        return [
            CheckResult(
                "gazetteer.yaml",
                Status.FAIL,
                str(exc),
                f"fix {path} — /gazetteer reload and startup both refuse it",
                config_error=True,
            )
        ]
    return [CheckResult("gazetteer.yaml", Status.PASS, f"{path} parses")]


class _NullGazetteer:
    """Satisfies the routing loader's name lookups without opening the db.

    Unknown system names in scope rules only produce loader warnings, so
    structural validation still runs in full."""

    def by_name(self, name: str) -> None:
        return None


def check_routing_yaml(ctx: DoctorContext) -> list[CheckResult]:
    assert ctx.cfg is not None
    from cortana.core.routing import RoutingConfigError, load_group_aliases, load_rules
    from cortana.nlu.gazetteer import Gazetteer

    path = ctx.cfg.routing.resolve(ctx.config_path)
    if not path.is_file():
        if ctx.cfg.routing.file:
            return [
                CheckResult(
                    "routing.yaml",
                    Status.FAIL,
                    f"routing.file points at {path}, which does not exist",
                    "create the file or fix routing.file in cortana.yaml",
                    config_error=True,
                )
            ]
        return [
            CheckResult(
                "routing.yaml",
                Status.WARN,
                f"no routing.yaml at {path} — ZERO routing rules will load",
                "cards will post with no role mentions; create routing.yaml or set routing.file",
            )
        ]
    gaz = cast("Gazetteer", _NullGazetteer())
    try:
        load_rules(path, gaz, lambda _name: 1)
        load_group_aliases(path, lambda _name: 1)
    except RoutingConfigError as exc:
        return [
            CheckResult(
                "routing.yaml",
                Status.FAIL,
                str(exc),
                f"fix {path} — /routing reload and startup both refuse it",
                config_error=True,
            )
        ]
    return [CheckResult("routing.yaml", Status.PASS, f"{path} parses")]


def _openwakeword_resources() -> Path | None:
    spec = importlib.util.find_spec("openwakeword")
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations))) / "resources" / "models"


def check_wake_files(ctx: DoctorContext) -> list[CheckResult]:
    assert ctx.cfg is not None
    model = Path(ctx.cfg.wake.model)
    if not model.is_file() or model.stat().st_size == 0:
        return [
            CheckResult(
                "wake.model",
                Status.FAIL,
                f"wake model missing or empty: {model}",
                "point wake.model at the trained openWakeWord .onnx chain",
            )
        ]
    results = [CheckResult("wake.model", Status.PASS, f"{model.name} present")]
    # openWakeWord also needs its melspectrogram + embedding feature models:
    # either shipped next to the wake model or in the package resources dir.
    feature_dirs = [model.parent]
    pkg = _openwakeword_resources()
    if pkg is not None:
        feature_dirs.append(pkg)
    missing = [
        stem
        for stem in ("melspectrogram", "embedding_model")
        if not any(d.is_dir() and list(d.glob(f"{stem}*.onnx")) for d in feature_dirs)
    ]
    if missing:
        results.append(
            CheckResult(
                "wake.features",
                Status.WARN,
                f"openWakeWord feature models not found: {', '.join(missing)}",
                "run openwakeword.utils.download_models() at install time or "
                f"place them next to {model.name}",
            )
        )
    else:
        results.append(CheckResult("wake.features", Status.PASS, "melspec + embedding present"))
    return results


def check_whisper_model(ctx: DoctorContext) -> list[CheckResult]:
    assert ctx.cfg is not None
    stt = ctx.cfg.stt
    if stt.backend == "whisper-cpp":
        return [
            CheckResult(
                "stt.model",
                Status.PASS,
                f"backend whisper-cpp — model lives with the server at {stt.whisper_cpp_url}",
            )
        ]
    model = Path(stt.model)
    if "/" in stt.model or model.exists():
        if not model.exists():
            return [
                CheckResult(
                    "stt.model",
                    Status.FAIL,
                    f"whisper model path does not exist: {model}",
                    "point stt.model at the model directory, or use a size "
                    "name (e.g. small) to download on first load",
                )
            ]
        return [CheckResult("stt.model", Status.PASS, f"{model} present")]
    return [
        CheckResult(
            "stt.model",
            Status.PASS,
            f"model name {stt.model!r} — downloaded to the cache on first load if absent",
        )
    ]


def check_piper(ctx: DoctorContext) -> list[CheckResult]:
    assert ctx.cfg is not None
    tts = ctx.cfg.tts
    if not tts.enabled:
        return [CheckResult("tts", Status.PASS, "tts disabled — voice checks skipped")]
    results: list[CheckResult] = []
    voice = Path(tts.voice)
    if voice.is_file() and voice.stat().st_size > 0:
        results.append(CheckResult("tts.voice", Status.PASS, f"{voice.name} present"))
    else:
        results.append(
            CheckResult(
                "tts.voice",
                Status.FAIL,
                f"piper voice missing or empty: {voice}",
                "point tts.voice at the Piper .onnx voice model",
            )
        )
    sidecar = Path(str(voice) + ".json")
    if sidecar.is_file():
        results.append(CheckResult("tts.voice.json", Status.PASS, f"{sidecar.name} present"))
    else:
        results.append(
            CheckResult(
                "tts.voice.json",
                Status.FAIL,
                f"voice sidecar missing: {sidecar}",
                "ship the .onnx.json config next to the voice model",
            )
        )
    try:
        proc = ctx.run_binary([tts.binary, "--help"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        results.append(
            CheckResult(
                "tts.binary",
                Status.FAIL,
                f"cannot run {tts.binary} --help: {exc}",
                "install piper or point tts.binary at the piper binary",
            )
        )
        return results
    if proc.returncode == 0:
        results.append(CheckResult("tts.binary", Status.PASS, f"{tts.binary} --help runs"))
    else:
        results.append(
            CheckResult(
                "tts.binary",
                Status.WARN,
                f"{tts.binary} --help exited {proc.returncode}",
                "verify the piper binary works: it will be spawned per synthesis",
            )
        )
    return results


def _credential_result(check: str, path: Path, fix: str) -> CheckResult:
    if not path.is_file():
        return CheckResult(check, Status.FAIL, f"credential missing: {path}", fix)
    if not os.access(path, os.R_OK):
        return CheckResult(
            check,
            Status.FAIL,
            f"credential not readable by this user: {path}",
            "fix ownership/permissions so the service user can read it",
        )
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        return CheckResult(
            check,
            Status.WARN,
            f"{path} is group/world readable (mode {mode:03o})",
            f"chmod 600 {path}",
        )
    return CheckResult(check, Status.PASS, f"{path} readable, mode {mode:03o}")


def check_credentials(ctx: DoctorContext) -> list[CheckResult]:
    assert ctx.cfg is not None
    cred_dir = ctx.env.get("CREDENTIALS_DIRECTORY")
    token_path = Path(cred_dir) / "token" if cred_dir else Path(ctx.cfg.discord.token_file)
    results = [
        _credential_result(
            "credentials.token",
            token_path,
            "provide the Discord token via systemd LoadCredential=token:... "
            "(or discord.token_file for dev runs)",
        )
    ]
    if ctx.cfg.chat.enabled:
        key_path = Path(cred_dir) / "anthropic" if cred_dir else Path(ctx.cfg.chat.api_key_file)
        results.append(
            _credential_result(
                "credentials.anthropic",
                key_path,
                "provide the Anthropic key via LoadCredential=anthropic:... "
                "or disable chat.enabled",
            )
        )
    return results


def check_database(ctx: DoctorContext) -> list[CheckResult]:
    assert ctx.cfg is not None
    from cortana.core.db import MIGRATIONS_DIR, MigrationError, _discover_migrations

    try:
        newest = _discover_migrations(MIGRATIONS_DIR)[-1][0]
    except (MigrationError, IndexError) as exc:
        return [
            CheckResult(
                "database",
                Status.FAIL,
                f"cannot enumerate shipped migrations: {exc}",
                "the install is broken — redeploy",
            )
        ]
    path = Path(ctx.cfg.database.path)
    if not path.is_file():
        return [
            CheckResult(
                "database",
                Status.WARN,
                f"{path} does not exist yet — created (and migrated) at first startup",
                "seed the gazetteer after first start: python -m cortana.nlu.seed",
            )
        ]
    try:
        # Read-only + immutable: a plain mode=ro open of a WAL database still
        # creates -shm/-wal side files, and the doctor NEVER writes anything.
        # immutable assumes no concurrent writer, which is NOT always true:
        # the install gate runs while the OLD brain is still live and writing.
        # An immutable reader racing a WAL checkpoint can see torn pages
        # (SQLITE_CORRUPT) — so any read error while a live writer is evident
        # (-wal/-shm side files present) degrades to WARN instead of aborting
        # a valid deploy with a bogus "corrupt database" verdict.
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as exc:
        return [_db_read_error(path, exc, "cannot open read-only")]
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        seeded = _count_systems(conn)
    except sqlite3.Error as exc:
        conn.close()
        return [_db_read_error(path, exc, "cannot read")]
    conn.close()

    results: list[CheckResult] = []
    if version > newest:
        results.append(
            CheckResult(
                "database",
                Status.FAIL,
                f"user_version {version} is AHEAD of newest migration {newest:04d}",
                "this build is older than the database — deploy the newer "
                "build (never run an old brain against a new schema)",
            )
        )
    elif version < newest:
        results.append(
            CheckResult(
                "database",
                Status.WARN,
                f"user_version {version} behind newest migration {newest:04d} — "
                "applied automatically at startup",
            )
        )
    else:
        results.append(CheckResult("database", Status.PASS, f"{path} at migration {version:04d}"))

    if seeded is None:
        results.append(
            CheckResult(
                "gazetteer.rows",
                Status.FAIL,
                "systems table missing — the gazetteer is unseeded",
                "seed it: python -m cortana.nlu.seed --db " + str(path),
            )
        )
    elif seeded == 0:
        results.append(
            CheckResult(
                "gazetteer.rows",
                Status.FAIL,
                "systems table is EMPTY — no system name can ever resolve",
                "seed it: python -m cortana.nlu.seed --db " + str(path),
            )
        )
    else:
        results.append(CheckResult("gazetteer.rows", Status.PASS, f"{seeded} systems seeded"))
    return results


def _live_writer_evident(path: Path) -> bool:
    """True when WAL side files exist next to the db — an open writer
    connection (the running service) keeps them on disk; a clean shutdown
    removes them."""
    return any(path.with_name(path.name + suffix).is_file() for suffix in ("-wal", "-shm"))


def _db_read_error(path: Path, exc: sqlite3.Error, what: str) -> CheckResult:
    if _live_writer_evident(path):
        return CheckResult(
            "database",
            Status.WARN,
            f"db busy — a live writer holds {path} ({exc}); check skipped",
            "the service is running and writing; re-run the doctor after it "
            "restarts (or stop it) for a full database check",
        )
    return CheckResult(
        "database",
        Status.FAIL,
        f"{what} {path}: {exc}",
        "the database file may be corrupt — restore the nightly backup",
    )


def _count_systems(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("SELECT COUNT(*) FROM systems").fetchone()
    except sqlite3.Error:
        return None
    return int(row[0])


def check_ipc_socket(ctx: DoctorContext) -> list[CheckResult]:
    assert ctx.cfg is not None
    sock_dir = Path(ctx.cfg.ipc.socket).parent
    if not sock_dir.is_dir():
        return [
            CheckResult(
                "ipc.socket",
                Status.WARN,
                f"socket directory {sock_dir} does not exist",
                "created by systemd (tmpfiles) at boot; for dev runs mkdir it first",
            )
        ]
    if not os.access(sock_dir, os.W_OK):
        return [
            CheckResult(
                "ipc.socket",
                Status.FAIL,
                f"socket directory {sock_dir} is not writable — Brain cannot bind",
                f"fix ownership/permissions on {sock_dir}",
            )
        ]
    return [CheckResult("ipc.socket", Status.PASS, f"{sock_dir} writable")]


def check_placeholder_ids(ctx: DoctorContext) -> list[CheckResult]:
    assert ctx.cfg is not None
    d = ctx.cfg.discord
    zeros = [
        name
        for name, value in (
            ("discord.guild_id", d.guild_id),
            ("discord.channels.intel_alerts", d.channels.intel_alerts),
            ("discord.channels.intel_live", d.channels.intel_live),
            ("discord.channels.health", d.channels.health),
        )
        if value == 0
    ]
    if 0 in d.watch_voice_channels:
        zeros.append("discord.watch_voice_channels")
    if not zeros:
        return [CheckResult("discord.ids", Status.PASS, "no placeholder ids")]
    return [
        CheckResult(
            "discord.ids",
            Status.WARN,
            "placeholder (all-zero) ids: " + ", ".join(zeros),
            "paste the real snowflake ids — the bot boots but every post fails",
        )
    ]


#: Checks that need a valid config; run in order after ``check_config``.
OFFLINE_CHECKS: tuple[Check, ...] = (
    check_gazetteer_yaml,
    check_routing_yaml,
    check_wake_files,
    check_whisper_model,
    check_piper,
    check_credentials,
    check_database,
    check_ipc_socket,
    check_placeholder_ids,
)


def run_offline_checks(ctx: DoctorContext) -> list[CheckResult]:
    """Run every offline check. A failed config parse short-circuits the
    rest (they all need the config)."""
    results = check_config(ctx)
    if ctx.cfg is None:
        return results
    for check in OFFLINE_CHECKS:
        results.extend(check(ctx))
    return results


# ── online checks (--online) ─────────────────────────────────────────────────


class OnlineClient(Protocol):
    """The few Discord REST reads the doctor needs. The real implementation
    wraps ``discord.http.HTTPClient``; tests inject a fake."""

    async def login(self, token: str) -> dict[str, Any]: ...

    async def channel(self, channel_id: int) -> dict[str, Any]: ...

    async def guild_roles(self, guild_id: int) -> list[dict[str, Any]]: ...

    async def my_member(self, guild_id: int) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class _DiscordClient:
    """Thin wrapper over discord.py's HTTP layer — REST only, no gateway,
    no cache, short timeouts."""

    def __init__(self) -> None:
        self._http: Any = None
        self._me_id = 0

    async def login(self, token: str) -> dict[str, Any]:
        from discord.http import HTTPClient

        self._http = HTTPClient(asyncio.get_running_loop())
        data = await asyncio.wait_for(self._http.static_login(token), _ONLINE_TIMEOUT_S)
        self._me_id = int(data["id"])
        return dict(data)

    async def channel(self, channel_id: int) -> dict[str, Any]:
        data = await asyncio.wait_for(self._http.get_channel(channel_id), _ONLINE_TIMEOUT_S)
        return dict(data)

    async def guild_roles(self, guild_id: int) -> list[dict[str, Any]]:
        data = await asyncio.wait_for(self._http.get_roles(guild_id), _ONLINE_TIMEOUT_S)
        return [dict(r) for r in data]

    async def my_member(self, guild_id: int) -> dict[str, Any]:
        data = await asyncio.wait_for(
            self._http.get_member(guild_id, self._me_id), _ONLINE_TIMEOUT_S
        )
        return dict(data)

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()


def compute_channel_permissions(
    *,
    me_id: int,
    member_role_ids: set[int],
    roles_by_id: dict[int, int],
    guild_id: int,
    overwrites: list[dict[str, Any]],
) -> int:
    """Standard Discord permission resolution from raw REST payloads.

    ``roles_by_id`` maps role id → permissions bitfield (the @everyone role
    shares the guild id). Pure — unit-tested without any network.
    """
    base = roles_by_id.get(guild_id, 0)
    for rid in member_role_ids:
        base |= roles_by_id.get(rid, 0)
    if base & _PERM_ADMINISTRATOR:
        return ~0
    everyone_ow = [o for o in overwrites if int(o["id"]) == guild_id]
    for ow in everyone_ow:
        base = (base & ~int(ow["deny"])) | int(ow["allow"])
    role_allow = role_deny = 0
    for ow in overwrites:
        if str(ow.get("type")) in ("0", "role") and int(ow["id"]) in member_role_ids:
            role_allow |= int(ow["allow"])
            role_deny |= int(ow["deny"])
    base = (base & ~role_deny) | role_allow
    for ow in overwrites:
        if str(ow.get("type")) in ("1", "member") and int(ow["id"]) == me_id:
            base = (base & ~int(ow["deny"])) | int(ow["allow"])
    return base


def _routing_role_names(path: Path) -> list[str]:
    """Role names referenced by routing.yaml (best effort, already
    validated structurally by the offline check)."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    entries = data.get("rules", []) if isinstance(data, dict) else data
    names = [
        str(e["role"])
        for e in entries or []
        if isinstance(e, dict) and isinstance(e.get("role"), str)
    ]
    if isinstance(data, dict) and isinstance(data.get("group_aliases"), dict):
        names.extend(str(v) for v in data["group_aliases"].values())
    return names


async def run_online_checks(
    ctx: DoctorContext, client: OnlineClient | None = None
) -> list[CheckResult]:
    """Token auth + channel permissions + role existence. Network problems
    degrade to WARN — a Discord outage must never read as a config error."""
    if ctx.cfg is None:
        parse = check_config(ctx)
        if ctx.cfg is None:
            return parse
    from cortana.dsc.bot import TokenError, read_token

    try:
        token = read_token(ctx.cfg.discord)
    except TokenError as exc:
        return [
            CheckResult(
                "discord.auth",
                Status.FAIL,
                str(exc),
                "provide the token via LoadCredential=token:... or discord.token_file",
            )
        ]

    own_client = client is None
    live = client if client is not None else _DiscordClient()
    try:
        return await _online_checks(ctx, live, token)
    except (TimeoutError, OSError) as exc:
        return [
            CheckResult(
                "discord.auth",
                Status.WARN,
                f"online checks skipped: {exc}",
                "no network from here — rerun --online when connectivity is back",
            )
        ]
    finally:
        if own_client:
            await live.close()


async def _online_checks(ctx: DoctorContext, client: OnlineClient, token: str) -> list[CheckResult]:
    assert ctx.cfg is not None
    import discord

    cfg = ctx.cfg.discord
    try:
        me = await client.login(token)
    except discord.LoginFailure as exc:
        return [
            CheckResult(
                "discord.auth",
                Status.FAIL,
                f"token rejected: {exc}",
                "the Discord token is wrong or revoked — reissue it and update the credential",
            )
        ]
    except discord.HTTPException as exc:
        return [
            CheckResult(
                "discord.auth",
                Status.WARN,
                f"online checks skipped: Discord API error: {exc}",
                "rerun --online later",
            )
        ]
    me_id = int(me["id"])
    results = [
        CheckResult("discord.auth", Status.PASS, f"authenticated as {me.get('username')} ({me_id})")
    ]

    if cfg.guild_id == 0:
        results.append(
            CheckResult(
                "discord.guild",
                Status.WARN,
                "guild_id is a placeholder — permission and role checks skipped",
                "set discord.guild_id",
            )
        )
        return results

    try:
        roles = await client.guild_roles(cfg.guild_id)
        member = await client.my_member(cfg.guild_id)
    except discord.HTTPException as exc:
        results.append(
            CheckResult(
                "discord.guild",
                Status.FAIL,
                f"cannot read guild {cfg.guild_id}: {exc}",
                "is the bot invited to this guild? check discord.guild_id",
            )
        )
        return results
    roles_by_id = {int(r["id"]): int(r["permissions"]) for r in roles}
    member_role_ids = {int(r) for r in member.get("roles", [])}

    named = [
        ("channel.intel_alerts", cfg.channels.intel_alerts),
        ("channel.intel_live", cfg.channels.intel_live),
        ("channel.health", cfg.channels.health),
    ]
    if ctx.cfg.chat.enabled and ctx.cfg.chat.answer_channel:
        named.append(("channel.chat_answers", ctx.cfg.chat.answer_channel))
    for name, channel_id in named:
        if channel_id == 0:
            results.append(
                CheckResult(name, Status.WARN, "placeholder id — skipped", "set the channel id")
            )
            continue
        try:
            channel = await client.channel(channel_id)
        except discord.HTTPException as exc:
            results.append(
                CheckResult(
                    name,
                    Status.FAIL,
                    f"channel {channel_id} not visible: {exc}",
                    "check the id and the bot's channel permissions",
                )
            )
            continue
        perms = compute_channel_permissions(
            me_id=me_id,
            member_role_ids=member_role_ids,
            roles_by_id=roles_by_id,
            guild_id=cfg.guild_id,
            overwrites=list(channel.get("permission_overwrites", [])),
        )
        missing = [
            label
            for label, bit in (
                ("VIEW", _PERM_VIEW_CHANNEL),
                ("SEND", _PERM_SEND_MESSAGES),
                ("EMBED", _PERM_EMBED_LINKS),
            )
            if not perms & bit
        ]
        if missing:
            results.append(
                CheckResult(
                    name,
                    Status.FAIL,
                    f"#{channel.get('name', channel_id)}: missing {', '.join(missing)}",
                    "grant the bot View Channel + Send Messages + Embed Links there",
                )
            )
        else:
            results.append(
                CheckResult(name, Status.PASS, f"#{channel.get('name', channel_id)}: postable")
            )

    for name, role_id in (("role.pilot", cfg.roles.pilot), ("role.fc", cfg.roles.fc)):
        if role_id == 0:
            continue
        if role_id in roles_by_id:
            results.append(CheckResult(name, Status.PASS, f"role {role_id} exists"))
        else:
            results.append(
                CheckResult(
                    name,
                    Status.WARN,
                    f"role {role_id} not found in the guild — that gate is broken",
                    "fix the id or remove it (0 = gate off)",
                )
            )

    role_names = {r["name"] for r in roles if "name" in r}
    routing_path = ctx.cfg.routing.resolve(ctx.config_path)
    if routing_path.is_file():
        unresolved = sorted(
            {
                n
                for n in _routing_role_names(routing_path)
                if n.lstrip("@") not in role_names and n not in role_names
            }
        )
        if unresolved:
            results.append(
                CheckResult(
                    "routing.roles",
                    Status.WARN,
                    "routing.yaml roles not in the guild: " + ", ".join(unresolved),
                    "rules with these roles are SKIPPED — create the roles or fix the names",
                )
            )
        else:
            results.append(CheckResult("routing.roles", Status.PASS, "all routing roles resolve"))
    return results


# ── first install (--first-install) ──────────────────────────────────────────

#: Checks whose FAIL on a brand-new droplet means "a human has not installed
#: this asset yet" (install.sh's checklist: wake model, piper binary + voice,
#: path-based whisper weights, gazetteer seed) — not "this deploy is broken".
#: ``--first-install`` downgrades exactly these to WARN so the very first
#: deploy can complete enable-only and actually print that checklist.
#: Config errors are never downgraded: a broken YAML is broken on day one too.
FIRST_INSTALL_ASSET_CHECKS: frozenset[str] = frozenset(
    {
        "wake.model",
        "stt.model",
        "tts.voice",
        "tts.voice.json",
        "tts.binary",
        "gazetteer.rows",
    }
)

#: Marker appended to downgraded rows — install.sh greps for it to decide
#: between a normal deploy and the enable-only first-install path.
FIRST_INSTALL_MARKER = "[first-install]"


def apply_first_install(results: list[CheckResult]) -> list[CheckResult]:
    """Downgrade human-installed-asset FAILs to WARN (first install only)."""
    out: list[CheckResult] = []
    for r in results:
        if r.status is Status.FAIL and not r.config_error and r.check in FIRST_INSTALL_ASSET_CHECKS:
            out.append(
                CheckResult(
                    r.check,
                    Status.WARN,
                    f"{r.detail} {FIRST_INSTALL_MARKER}",
                    r.fix_hint,
                )
            )
        else:
            out.append(r)
    return out


# ── rendering + exit codes ───────────────────────────────────────────────────


def render(results: list[CheckResult], config_path: Path) -> str:
    """One aligned, phone-readable table."""
    width = max((len(r.check) for r in results), default=0)
    lines = [f"CORTANA doctor — {config_path}", ""]
    for r in results:
        lines.append(f" {r.status.value:<4}  {r.check:<{width}}  {r.detail}")
        if r.fix_hint and r.status is not Status.PASS:
            lines.append(f"       {'':<{width}}  fix: {r.fix_hint}")
    counts = {s: sum(1 for r in results if r.status is s) for s in Status}
    lines.append("")
    lines.append(
        f"{counts[Status.PASS]} passed, {counts[Status.WARN]} warned, {counts[Status.FAIL]} failed"
    )
    return "\n".join(lines)


def exit_code(results: list[CheckResult]) -> int:
    if any(r.config_error and r.status is Status.FAIL for r in results):
        return EXIT_CONFIG_INVALID
    if any(r.status is Status.FAIL for r in results):
        return EXIT_FAIL
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cortana.doctor",
        description="CORTANA preflight checks (read-only; never starts engines)",
    )
    parser.add_argument(
        "--config",
        default="/etc/cortana/cortana.yaml",
        help="path to cortana.yaml (default: /etc/cortana/cortana.yaml)",
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help="additionally verify the Discord token, channel permissions, "
        "and role ids (network; degrades to WARN when unreachable)",
    )
    parser.add_argument(
        "--first-install",
        action="store_true",
        help="treat missing human-installed assets (wake model, piper, "
        "gazetteer seed) as warnings — used by install.sh before the first "
        "release is live; config errors still fail",
    )
    args = parser.parse_args(argv)

    ctx = DoctorContext(config_path=Path(args.config))
    results = run_offline_checks(ctx)
    if args.online and ctx.cfg is not None:
        results.extend(asyncio.run(run_online_checks(ctx)))
    if args.first_install:
        results = apply_first_install(results)
    print(render(results, ctx.config_path))
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
