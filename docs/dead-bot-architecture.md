# "Dead" — modular Discord bot platform architecture

**Status: build spec (final).** This document is the merged, buildable design for turning the
single-purpose CORTANA voice bot into **Dead**, a modular Discord bot for the Dead Gaming
community that hosts multiple game/utility modules as add-ons. The first add-on module is the
Albion Online killboard (`docs/` companion: the Albion Killboard GDD). CORTANA remains the
flagship and its voice/dialog/IPC internals are preserved byte-for-byte.

This is the lead-architect final call after weighing three proposals (minimal-wrap,
clean-platform, hybrid). The decision is a **hybrid biased to preservation**, and the bias is
not stylistic — it is forced by real code (see §0.1).

---

## 0. Design thesis

`brain/cortana/__main__.py::App` is *already* a supervised, single-loop composition root: one
event loop (`asyncio.run`, `App.run`), a task supervisor (`_spawn`), a shared shutdown
`asyncio.Event`, an ordered graceful teardown under an 8s budget (`_shutdown_sequence`), and an
all-or-nothing reload transaction (`_reload_transaction`). The platform is **not** a rewrite and
**not** a new framework that spins its own loop. It is:

1. **`App` promoted to the Dead core** — it keeps owning the loop, signals, supervision,
   shutdown, and reload, and *gains* a module host (`ModuleManager`) plus a second, isolating
   task supervisor (`ModuleSupervisor`).
2. **A genuine `BotModule` contract** (`brain/dead/module.py`) that new games/utilities
   implement. Albion is built native against it.
3. **CORTANA wrapped as a module** via a thin *delegating facade* (`CortanaModule`) so `/status`,
   health aggregation, and the module registry treat it uniformly — **without relocating a single
   line of voice wiring**.

### 0.1 Why the facade, not a relocation (the forcing function)

`brain/tests/test_app.py::make_app` (a required CI gate) does exactly this:

```python
app = App(holder)
app.dialog = rig.dialog ; app.capture = rig.capture ; app.engine = rig.engine
app.health = rig.health ; app.speaker = rig.speaker ; app.bot = _Bot(roles) ; app.conn = None
await app._on_control({"t": "left", "user_id": str(USER)})
```

The test constructs `App` directly, sets the voice components as `App` attributes, and calls
`App._on_control`. Moving `_on_control` and the voice component wiring out of `App` (as the
clean-platform / hybrid "move `App.setup()` into `CortanaModule`" proposals suggested) would
break this safety net and force edits to the very test that guards the voice invariants. Combined
with owner requirement (2) — *voice internals must not be rewritten* — this decides it: **`App`
keeps its voice wiring, `_on_control`, and its `.dialog/.capture/.engine/.health/.speaker/
.bot/.conn` attributes exactly as they are.** CORTANA-as-a-module is expressed by a facade that
*reads* those already-built components, never by moving them.

### 0.2 Two-tier supervision (the core crash-safety idea)

`App._spawn` has one policy: **any critical task exit ⇒ `_shutdown.set()` ⇒ process exit ⇒
systemd `Restart=always`.** That is correct for CORTANA's six voice/dialog tasks — a dead dialog
wheel *should* bounce the process so Ears re-handshakes DAVE cleanly. It is the *wrong* policy for
a bolt-on killboard poller hitting a flaky, unofficial API.

So the core exposes **two** spawn primitives:

| Primitive | Owner | On task exit/crash | Used by |
|---|---|---|---|
| `_spawn(name, coro)` (unchanged) | `App` | sets `_shutdown` → process restart | CORTANA's 6 tasks |
| `ModuleSupervisor.spawn(module, task, factory, backoff)` | `brain/dead/supervisor.py` | catch, log, mark degraded, restart with backoff; quarantine after storm; **never** touches `_shutdown` | every add-on module task |

This is the honest reading of "one module failing never takes the bot down": **add-on module
failures are isolated; CORTANA's process-fatal contract is retained where it already relies on
it.** CORTANA itself remains the privileged base whose setup/voice failure is genuinely fatal
(exit 78/69) — voice is not made hot-swappable, and must not be.

---

## 1. Package layout

```
brain/
  dead/                     # NEW — game-agnostic platform kernel
    __init__.py
    module.py               # BotModule Protocol, ModuleContext, ModuleHealth, ModuleStatus, Backoff, ReloadRegistration
    supervisor.py           # ModuleSupervisor: isolated restart/backoff/quarantine + status map
    manager.py              # ModuleManager: registry-driven setup/start/stop/health/reload fan-out + isolation
    registry.py             # MODULES: tuple[BotModule, ...]  — the one edit to add a game
  cortana/                  # UNCHANGED internals; App stays the core & loop owner
    __main__.py             # +~40 additive lines: construct/start/stop/reload the ModuleManager. App/_on_control/attrs preserved.
    cortana_module.py       # NEW — CortanaModule(BotModule): delegating facade over App's built components
    dsc/bot.py              # +~4 lines in setup_hook: add module cogs + dynamic items (after the 9 core cogs)
    dsc/cogs/status.py      # /botstatus surfaces module health snapshot
    alarms.py               # + MODULE_SETUP_FAILED / MODULE_TASK_DEGRADED / MODULE_QUARANTINED alarm codes
    doctor.py               # + optional per-module preflight (skipped when the module is disabled)
    config_schema.py        # + Section("killboard", optional=True) + child sections + Keys
    config.py               # + KillboardConfig dataclasses + _assemble_killboard + AuraConfig.killboard field
    ...                     # ipc.py, voice_gateway.py, dialog/, audio/, tts.py, core/, nlu/  — BYTE-FOR-BYTE UNCHANGED
  killboard/                # NEW — first native module (Albion), own DB + own migration sequence
    __init__.py  module.py  config.py  api.py  model.py  store.py
    poller.py  feed.py  cards.py  rankings.py  battles.py  schedule.py
    commands.py  views.py
    migrations/0001_init.sql
  migrations/               # CORTANA shared-DB migrations — UNCHANGED (0001-0008)
  tests/                    # + test_module_supervisor, test_module_manager, test_killboard_*
```

`brain/pyproject.toml` `[tool.setuptools.packages.find]` gains `dead*` and `killboard*` in
`include` — the only packaging change. `pip install -e brain` then resolves all three;
`python -m cortana` is unaffected; `from cortana.__main__ import App` still resolves. The rebrand
to "Dead" lives in the `dead/` package and docs, **not** in a rename of `App`, the `cortana`
package, or the `cortana-brain.service` unit — renaming those is gratuitous churn against the
load-bearing entrypoint contract and buys nothing.

---

## 2. The BotModule contract (`brain/dead/module.py`)

```python
from __future__ import annotations
import abc, asyncio, enum, sqlite3
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    import structlog
    from discord.ext import commands
    from discord.ui import DynamicItem
    from cortana.alarms import AlarmBus
    from cortana.config import AuraConfig, ConfigHolder


class ModuleStatus(enum.Enum):
    DISABLED = "disabled"     # enabled() returned False; never constructed
    STARTING = "starting"
    OK       = "ok"
    DEGRADED = "degraded"     # a supervised task is crash-looping / backing off
    FAILED   = "failed"       # quarantined (gave up restarting) OR setup() threw


@dataclass(frozen=True)
class ModuleHealth:
    status: ModuleStatus
    detail: str                       # one-line human summary for /botstatus
    metrics: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Backoff:
    base: float = 5.0
    factor: float = 2.0
    cap: float = 60.0
    reset_after: float = 300.0        # a task alive this long resets its failure streak
    max_restarts: int = 8             # consecutive restarts within reset_after → quarantine


@dataclass(frozen=True)
class ModuleContext:
    """Everything the core hands a module. Modules NEVER import App or each other."""
    name: str                         # stable id; namespaces config/db/logs/custom_ids
    holder: ConfigHolder              # read holder.current at point-of-use (SIGHUP hot-reload)
    bot: commands.Bot                 # the shared AuraBot (service locator)
    alarms: AlarmBus                  # operator alarm surface (raise/clear)
    log: structlog.BoundLogger        # pre-bound module=<name>
    shutdown: asyncio.Event           # THE single shared shutdown signal
    supervisor: ModuleSupervisor      # isolated task spawning (restart/backoff)
    to_thread: Callable[..., Awaitable[Any]]   # asyncio.to_thread passthrough (blocking work)
    credentials_dir: str | None       # $CREDENTIALS_DIRECTORY (constraint 12)
    # NOTE: no shared sqlite conn — an add-on that needs storage owns its OWN db file (§6).


class BotModule(abc.ABC):
    name: str                                         # class attribute == ctx.name
    critical: bool = False                            # True only for CORTANA (setup failure is fatal)

    @abc.abstractmethod
    def enabled(self, cfg: AuraConfig) -> bool: ...   # OFF-by-default gate. Pure fn of config.
                                                      # Called BEFORE setup(); a disabled module builds nothing.

    @abc.abstractmethod
    async def setup(self, ctx: ModuleContext) -> None: ...
        # Construct + cross-wire components, open + migrate own DB. NO network, NO Discord login,
        # NO task spawning. Mirrors App.setup() discipline.

    def cogs(self) -> Iterable[commands.Cog]:               return ()   # added in setup_hook
    def dynamic_items(self) -> Iterable[type[DynamicItem]]: return ()   # persistent components

    @abc.abstractmethod
    async def start(self, ctx: ModuleContext) -> None: ...
        # Post-login. Spawn long-lived tasks via ctx.supervisor.spawn(...). Returns promptly.

    async def on_ready(self, bot: commands.Bot) -> None:   return       # per-(re)connect work
    async def reload(self, old: AuraConfig, new: AuraConfig) -> None: return  # SIGHUP hot-apply

    @abc.abstractmethod
    async def stop(self) -> None: ...
        # Bounded, idempotent teardown. Must finish under the per-module budget (2s).

    def health(self) -> ModuleHealth: ...             # pull-based snapshot for /botstatus + doctor
```

**What a module owns:** its config subtree (`cfg.<name>.*`), its own DB file + migration
sequence, its cog(s) + a private `aura:<ns>:*` custom_id prefix, its background tasks (only via
`ctx.supervisor`), its alarms, its health line. **What it must never own or touch:** the event
loop, the Discord token, the IPC socket, `self_deaf`/the audio path, and `decide_mentions()` —
escalation stays the sole authority in `core/routing.py` (constraint 11). Killboard posts feed
embeds with `AllowedMentions.none()` and never pings, so it never approaches that seam.

---

## 3. The registry, manager, and supervisor (`brain/dead/`)

### 3.1 `registry.py`

```python
def build_modules() -> tuple[BotModule, ...]:
    from cortana.cortana_module import CortanaModule
    from killboard.module import KillboardModule
    return (CortanaModule(), KillboardModule())   # order = start order; CORTANA first
```

Adding game #3 = one import + one entry here. No core edit.

### 3.2 `ModuleManager` (`manager.py`)

Registry-driven fan-out with **per-phase isolation**:

```python
class ModuleManager:
    def __init__(self, *, holder, bot, alarms, shutdown, supervisor, log): ...
    async def setup_enabled(self) -> None:
        # for each module: if not module.enabled(holder.current): mark DISABLED; continue
        # else build ctx, try/except await module.setup(ctx):
        #   on exception (non-critical): log + alarms.raise(MODULE_SETUP_FAILED) + status=FAILED
        #                                + DROP from the started set (isolation). Boot continues.
        #   on exception (critical=CORTANA): re-raise (fatal — but CORTANA never routes here; §4).
    def all_cogs(self) -> list[commands.Cog]         # non-CORTANA module cogs, for setup_hook
    def all_dynamic_items(self) -> list[type[DynamicItem]]
    async def start_enabled(self) -> None            # await module.start(ctx) per healthy module
    async def on_ready(self) -> None                 # fan out module.on_ready(bot)
    async def reload_all(self, old, new) -> None     # fan out module.reload(old,new); isolated per module
    async def stop_all(self) -> None                 # reverse order; each stop() under wait_for(2s), suppressed
    def health_snapshot(self) -> dict[str, ModuleHealth]   # name → health(); merges supervisor status map
```

### 3.3 `ModuleSupervisor` (`supervisor.py`)

The isolated Tier-2 supervisor. **A crash here never reaches `App._spawn`, so it can never set
`_shutdown`.**

```python
class ModuleSupervisor:
    def __init__(self, shutdown: asyncio.Event, alarms, log): ...
    def spawn(self, module: str, task: str,
              factory: Callable[[], Awaitable[None]],   # a FACTORY, not a coroutine (re-creatable)
              *, backoff: Backoff = Backoff()) -> None:
        # creates its own asyncio.Task (tracked in _tasks) running _runner()
    async def stop(self, module: str) -> None:          # cancel a module's tasks, await with budget
    async def stop_all(self) -> None
    def status(self, module: str) -> ModuleStatus       # OK / DEGRADED / FAILED

# _runner loop, per task:
#   fails = 0 ; started = loop.time()
#   while not shutdown.is_set():
#       try:
#           await factory()                  # a full run loop, or one work cycle
#           return                           # clean return = task chose to finish; do NOT restart
#       except asyncio.CancelledError:
#           raise                            # shutdown path — propagate, fall out fast
#       except Exception:
#           log.exception("module_task_crashed", module=module, task=task)
#           status[module] = DEGRADED
#           if loop.time() - started > backoff.reset_after: fails = 0     # survived long enough
#           fails += 1
#           if fails > backoff.max_restarts:
#               log.error("module_quarantined", module=module, task=task)
#               status[module] = FAILED
#               await alarms.raise_(MODULE_QUARANTINED, ...)              # no @here (constraint 11)
#               return                                                    # give up; process stays up
#           if fails == 2: await alarms.raise_(MODULE_TASK_DEGRADED, ...) # rate-limited surface
#           await _sleep_or_shutdown(min(base*factor**(fails-1), cap), shutdown)  # cancellable
#           started = loop.time()
```

Key properties:
- **Factory, not coroutine** — a crashed coroutine can't be re-awaited; each attempt calls
  `factory()` fresh (e.g. `lambda: self.poller.run()`).
- **Contained** — module exceptions are caught here; `_shutdown` is never touched.
- **Quarantine** caps crash storms so a permanently-broken poller cannot busy-loop a vCPU; the
  module goes `FAILED`, visible in `/botstatus`, everything else keeps running.
- **Shared `_shutdown`** — the supervisor watches the same `asyncio.Event` as `App` and
  `DialogEngine.run()`; `_sleep_or_shutdown` makes backoff cancellable so `stop_all()` is fast.
- **Belt and braces** — a module's own loop body still catches transient errors and backs off in
  place (the `_sweep_loop`/`_timer_loop` pattern); the supervisor is the outer net for
  *unexpected* faults.

---

## 4. How CORTANA is wrapped (`cortana/cortana_module.py` + additive `__main__.py` seams)

**No CORTANA voice code moves.** `App.setup()` keeps building every component in its strict order
(DB → gazetteer → alarms → discipline → IPC → speaker → STT → capture → health → engine → AuraBot
→ dialog → gateway). `App._run_async` keeps binding IPC before login and `_spawn`-ing the six
Tier-1 tasks. `_shutdown_sequence` keeps sending no voice-leave. `_on_control` stays on `App`. All
twelve hard constraints stay where they live (Ears + `capture` + `core/routing.py`).

`CortanaModule` is a **delegating facade** holding a reference to the `App`:

```python
class CortanaModule(BotModule):
    name = "cortana"
    critical = True
    def __init__(self, app: "App"): self._app = app
    def enabled(self, cfg) -> bool:          return True                 # the flagship; always on
    async def setup(self, ctx) -> None:      return                      # App.setup() already ran
    async def start(self, ctx) -> None:      return                      # App._run_async already spawned
    async def on_ready(self, bot) -> None:   return                      # App/on_ready already loads routing+census
    async def stop(self) -> None:            return                      # App._shutdown_sequence owns voice teardown
    def cogs(self):                          return ()                   # the 9 cogs stay hard-wired in setup_hook
    def health(self) -> ModuleHealth:                                    # reads the live HealthReporter
        h = self._app.health
        return ModuleHealth(OK if h.ok else DEGRADED, h.summary_line(), h.metrics())
```

CORTANA's tasks stay on `App._spawn` (fatal), never on `ModuleSupervisor`. Its setup failure
stays fatal because it never routes through `ModuleManager.setup_enabled` (App builds it directly,
before the manager exists). The facade exists purely so `/botstatus` and health aggregation list
CORTANA and killboard through one code path — the "wrap its startup as a module" framing, with
zero relocation risk.

### 4.1 The five additive seams in `__main__.py` / `dsc/bot.py`

1. **Construct the host at the tail of `App.setup()`** (after component 18, so modules may depend
   on any CORTANA service, and `self.bot` exists):
   ```python
   self.supervisor = ModuleSupervisor(self._shutdown, self.alarms, log)
   self.modules = ModuleManager(holder=self.holder, bot=self.bot, alarms=self.alarms,
                                shutdown=self._shutdown, supervisor=self.supervisor, log=log)
   self.modules.register(CortanaModule(self))          # facade; enabled()->True, lifecycle no-ops
   self.modules.register(*build_modules_except_cortana())   # KillboardModule(), ...
   await self.modules.setup_enabled()                  # each enabled add-on: own DB migrate + build
   self.bot.modules = self.modules                     # service-locator injection (like bot.fun)
   self.bot.module_cogs = self.modules.all_cogs()
   self.bot.module_dynamic_items = self.modules.all_dynamic_items()
   ```
   Disabled modules construct nothing — killboard is fully dark until configured (req 5).

2. **Cog seam** — `dsc/bot.py::setup_hook`, ~4 lines *after* the 9 core cogs and the existing
   `add_dynamic_items`, so tree sync stays hash-gated/background:
   ```python
   self.add_dynamic_items(*getattr(self, "module_dynamic_items", ()))
   for cog in getattr(self, "module_cogs", ()):
       await self.add_cog(cog)
   ```
   New commands change the tree digest → the existing `_background_tree_sync` re-syncs
   automatically. Sync never moves back onto the critical path.

3. **Start seam** — `_run_async`, after the six `_spawn` calls:
   ```python
   await self.modules.start_enabled()                  # uses ModuleSupervisor, NOT _spawn
   ```

4. **Shutdown seam** — `_shutdown_sequence`, add-ons stop *first* (reverse order), before the
   unchanged voice teardown, inside a bounded sub-budget so the whole thing stays < 8s:
   ```python
   await self.modules.stop_all()                       # each add-on stop() under wait_for(2s)
   # …UNCHANGED: gateway.close() (no leave) → ipc.stop() → speaker.close() → bot.close() → cancel tasks
   ```
   CortanaModule.stop() is a no-op, so the voice teardown runs exactly as today.

5. **Reload seam** — `_reload_transaction`, register `self.modules.reload_all(old, new)` as one
   more applier so killboard HOT keys hot-apply in the same all-or-nothing swap. Enabling a dark
   module is RESTART-class (surfaced as `CONFIG_RESTART_PENDING`, not hot-started).

`on_ready` fan-out (`self.modules.on_ready()`) is called from `AuraBot.on_ready` after the
existing routing-rules load + census seed.

---

## 5. Configuration (`config_schema.py` + `config.py`)

One optional top-level section `killboard`, with child sections. Every key carries a non-REQUIRED
default (required for optional sections by `test_optional_sections_only_carry_defaulted_keys`).
The GDD's flat top-level blocks (`albion:`, `poller:`, `feed:`, …) are **nested under
`killboard.`** so they cannot collide with CORTANA's keys.

**Sections:** `killboard`, `killboard.poller`, `killboard.feed`, `killboard.cards`,
`killboard.rankings`, `killboard.battles`, `killboard.storage`, `killboard.staleness`.

**Representative keys** (Reload class in brackets; full table regenerated into GDD §16):

| Key | Type | Reload | Default |
|---|---|---|---|
| `killboard.enabled` | bool | RESTART | `false` |
| `killboard.region` | str (west/europe/east) | RESTART | `west` |
| `killboard.guild_name` | opt_str | RESTART | `null` |
| `killboard.guild_id` | opt_str | RESTART | `null` |
| `killboard.poller.interval_seconds` | int (≥15) | HOT | `45` |
| `killboard.poller.request_timeout_seconds` | int | HOT | `10` |
| `killboard.poller.max_retries` | int | HOT | `3` |
| `killboard.poller.backoff_base_seconds` | int | HOT | `5` |
| `killboard.poller.page_limit` | int | HOT | `51` |
| `killboard.poller.max_backfill_pages` | int | RESTART | `20` |
| `killboard.feed.kills_channel` | opt_str | HOT | `null` |
| `killboard.feed.deaths_channel` | opt_str | HOT | `null` |
| `killboard.feed.min_fame` | int | HOT | `0` |
| `killboard.feed.juicy_channel` | opt_str | HOT | `null` |
| `killboard.feed.juicy_min_fame` | int | HOT | `2000000` |
| `killboard.feed.ignore_deaths_below_ip` | int | HOT | `0` |
| `killboard.feed.blob_participant_threshold` | int | HOT | `20` |
| `killboard.feed.blob_channel` | opt_str | HOT | `null` |
| `killboard.feed.catchup_max_posts` | int | HOT | `20` |
| `killboard.feed.post_delay_ms` | int | HOT | `750` |
| `killboard.cards.enabled` | bool | HOT | `true` |
| `killboard.cards.icon_cache_dir` | str | RESTART | `/var/lib/dead/killboard/icons` |
| `killboard.cards.render_base` | str | HOT | `https://render.albiononline.com/v1` |
| `killboard.rankings.timezone` | str | HOT | `UTC` |
| `killboard.battles.channel` | opt_str | HOT | `null` |
| `killboard.battles.min_players` | int | HOT | `20` |
| `killboard.battles.min_fame` | int | HOT | `5000000` |
| `killboard.storage.db_path` | str | RESTART | `/var/lib/dead/killboard/killboard.db` |
| `killboard.staleness.warn_after_minutes` | int | HOT | `30` |
| `killboard.staleness.no_events_notice_hours` | int | HOT | `6` |

**Scheduled ranking posts are NOT config keys.** The GDD's `rankings.schedules` is a list of
dicts, which the flat scalar/list schema cannot express cleanly and the field↔KEYS bijection would
reject. Instead schedules are **DB rows** in the killboard `schedules` table (already in the GDD
data model), managed by the `/killboard schedule` admin command and seeded empty. Only
`killboard.rankings.timezone` lives in config.

**Off-by-default gate (req 5)** lives in `KillboardModule.enabled()`, NOT in a `CrossCheck`:
```python
def enabled(self, cfg) -> bool:
    kb = cfg.killboard
    return (kb.enabled
            and (kb.guild_id or kb.guild_name)
            and (kb.feed.kills_channel or kb.feed.deaths_channel))
```
No `CrossCheck` may *reject* an unconfigured `killboard` section — that would break
`test_shipped_config_files_load_unchanged`. An optional `CrossCheck` may enforce internal
consistency only when `enabled` is true (e.g. `juicy_min_fame >= min_fame`), returning the
`section.key: problem — Fix: …` shape.

**Assembly (`config.py`):** add frozen `KillboardConfig` (+ nested `KbPollerConfig`,
`KbFeedConfig`, `KbCardsConfig`, `KbRankingsConfig`, `KbBattlesConfig`, `KbStorageConfig`,
`KbStalenessConfig`), an `_assemble_killboard(v)` reading `v["killboard.*"]`, and
`killboard: KillboardConfig = field(default_factory=KillboardConfig)` on `AuraConfig` wired into
`_assemble`. Field defaults **must** equal schema defaults (bijection + parity tests).

**Consumers** read `holder.current.killboard.*` at point-of-use (never cached) so SIGHUP retunes
thresholds/interval live.

**Secrets:** none. The gameinfo API is unauthenticated public GETs (GDD §2.1, §15); the render
service is public. No `*_file` key and no `LoadCredential=` line are added — constraint 12 is
satisfied by having nothing to satisfy.

**CI keep-green procedure:** after schema edits run `python scripts/gen_config_docs.py` (no
`--check`) to rewrite the GDD §16 table, add the `killboard:` block (all defaults) to
`config/cortana.yaml.example` (dev.yaml may omit it — the section is optional and resolves to
defaults), then `cd brain && pytest tests/test_config.py`. This is exactly what keeps
`gen_config_docs.py --check` and the bijection/parity/shipped-yaml tests green (req 6).

---

## 6. Database namespacing — killboard owns its own file

The migration runner enforces strict contiguous-from-0001 numbering with **one `user_version` per
database** (`_discover_migrations`), so two independent module sequences cannot coexist in
`cortana.db`. The killboard shares nothing with the EVE gazetteer and would FK-reference only its
own tables, so it gets its **own SQLite file** — the clean isolation the DB ground-truth calls
out and the GDD already assumes (`aoedge.db`).

- `KillboardModule.setup()` opens its own connection:
  `conn = await ctx.to_thread(db.connect, cfg.killboard.storage.db_path)` then
  `await ctx.to_thread(db.migrate, conn, migrations_dir=KILLBOARD_MIGRATIONS_DIR)` with its own
  `0001_init.sql` numbered from 1.
- It reuses `core/db.py`'s five helpers (`execute/executemany/query/query_one/query_value`) and
  wraps **every** call in `asyncio.to_thread` — same discipline. Tables use the GDD §11 schema
  unprefixed (they are alone in their file). No FK to CORTANA's `systems(id)`.
- `killboard.storage.db_path` is `Reload.RESTART`. Runtime dir `/var/lib/dead/killboard/` (db +
  icon cache), created by `install.sh` and owned by the `aura` user.

**Known coupling (flagged, accepted):** `core/db.py`'s `_CONN_LOCK` is module-global, so
killboard's statement helpers serialize against CORTANA's even though the connections differ. At
killboard's volume (a handful of writes per 45s) this is negligible and, if anything, adds safety.
A future high-write module would justify a per-connection lock; not now.

**Isolation dividend:** a killboard schema fault or DB corruption cannot touch CORTANA's DB;
killboard migrations never interleave into CORTANA's global 0001–0008 chain; the irreplaceable
event store gets its own nightly backup target.

---

## 7. The Albion killboard module (`brain/killboard/`)

Maps the GDD component manifest onto the platform. Pure logic (parsing, classification, ranking
SQL, card layout) lives in module-level functions so tests run without Discord or network.

| File | Contents / key signatures |
|---|---|
| `module.py` | `class KillboardModule(BotModule)`. `enabled()` per §5. `setup(ctx)`: open+migrate own DB (`KbStore`), construct `KbApi`, `Poller`, `Feed`, `Rankings`, `Battles`, `Scheduler`, `CardRenderer`, build the cog — **no network**. `cogs()`→`[KillboardCog(...)]`. `dynamic_items()`→`[RankingPageButton]` (`aura:kb:*`). `start(ctx)`: resolve `guild_id` from name if needed (first network touch, post-login; fail-fast region-mismatch message per GDD §13), then `ctx.supervisor.spawn("killboard","poller", lambda: self.poller.run())`, `…"feed"`, `…"scheduler"`. `stop()`: cancel via supervisor + `await self.api.close()`. `health()`: reads `poll_state` → `ModuleHealth`. |
| `config.py` | Typed view over `cfg.killboard.*` + `region_host(region) -> str` (the §2.2 host table). |
| `api.py` | `class KbApi` — `aiohttp.ClientSession`, region host, descriptive User-Agent (§15), per-request timeout, retry/exponential backoff (5,10,20), 429 → longer cool-down, tolerant JSON. Methods: `search_guild`, `events(guild_id,limit,offset)`, `guild`, `members`, `top`, `battles`, `close`. The only thing that touches gameinfo. |
| `model.py` | Pure parsers: `parse_event(raw) -> EventRow`, `participants_of(raw) -> list[ParticipantRow]`, `classify(raw, tracked_guild_id) -> Relation` (KILL/DEATH/ASSIST, §6). Tolerant field access — never raises on a missing optional field. |
| `store.py` | `class KbStore(conn)` over `core/db.py` helpers (all via `to_thread`): `open_and_migrate`, `high_water_mark`, `upsert_event`, `upsert_participants`, `record_poll`, `mark_posted`, `unposted_events`, windowed `kill_count/death_count/kd/kill_fame/assists(window, player=None)`, `recent(n)`, `members_snapshot`. Owns the migrations-dir constant. |
| `poller.py` | `class Poller` — `run()` = the §5 loop: fetch `limit=51`, stop at `EventId ≤ last_event_id`, auto-paginate on spikes (§5.2), first-run backfill (§5.3), update `poll_state`, enqueue new ids for the feed. Per-cycle try/except + backoff-in-place; the supervisor is the outer net. Handed to `supervisor.spawn` as a factory. |
| `feed.py` | `class Feed` — drains the new-event queue, builds embeds + composited card, routes to kills/deaths/juicy/blob channels per §7.2, records in `posted` (exactly-once across restart), rate-limits catch-up with a single "posted N older" summary (§7.3). **All posts use `AllowedMentions.none()`; never `@here`** (constraint 11). |
| `cards.py` | `class CardRenderer` — Pillow compositing (§7.1); icon fetch from the render service, cached under `killboard.cards.icon_cache_dir`. **All Pillow work runs in `asyncio.to_thread`** (blocking CPU off the event loop — req 4). Pure layout functions unit-tested with icon fetch stubbed. |
| `rankings.py` | Pure windowed leaderboard builders over `KbStore` (§8): fame/count/KD, formatting to embeds. |
| `battles.py` | `class Battles` — fetch `/battles`, guild-participation threshold filter (§9), summary embed. Tolerates gaps quietly, never blocks the feed. |
| `schedule.py` | `class Scheduler` — `run()` drives the `schedules` table idempotently via `last_run` (§8.3). The third supervised task. |
| `commands.py` | `class KillboardCog(commands.Cog)`, `bot` under `TYPE_CHECKING`. **All commands under one `app_commands.Group("killboard")`** (`/killboard ranking|record|recent|topkills|guild|battles`) to avoid flat global-tree collisions and conserve the 100-command ceiling. Admin subcommands (`status`, `config`, `schedule`) reuse `_is_admin` imported from `cortana.dsc.cogs.admin`. Reads only from `KbStore` — instant, API-independent (§10). |
| `views.py` | `RankingPageButton` and any pagination as plain buttons + a `DynamicItem` handler on `aura:kb:*`, registered once via the core seam, callbacks wrapped in `run_component_action`. Pure build/parse helpers, unit-tested without Discord. |
| `migrations/0001_init.sql` | The GDD §11 schema (`events`, `participants`, `poll_state`, `posted`, `members`, `schedules`) — own 0001 sequence / own `user_version`. |

Help coverage: every `/killboard …` command is added to `help.py::HELP_TOPICS` (enforced by
`test_help_cog.py`).

---

## 8. Entrypoint & deploy back-compat

**Zero systemd / install.sh / CI-workflow structural change.** `ExecStart` stays
`python -m cortana --config /etc/cortana/cortana.yaml`. `cortana/__main__.py` keeps
`main`/`build_app`/`App`/`configure_logging` and exit codes 78 (`EX_CONFIG`) / 69 (auth), both
paired with `RestartPreventExitStatus`. `test_app.py`'s `import cortana.__main__` / `App(holder)`
/ `app._on_control` stay valid because `App` and its voice attributes are untouched.

Additive deploy deltas:
- `brain/requirements.txt`: add `pillow` and `aiohttp` (both permitted wheels; **not** PyNaCl /
  discord.py[voice] — constraint 2; no ffmpeg — constraint 3).
- `brain/pyproject.toml`: `packages.find` include `dead*`, `killboard*`.
- `deploy/install.sh` STAGE: `install -d -o aura -g aura /var/lib/dead/killboard` (db + icon
  cache). Inert when killboard is disabled.
- `deploy/cortana-brain.service`: no new `LoadCredential` (no secret). Optionally add
  `StateDirectory=dead` for the runtime dir; the `install.sh` mkdir already covers it.

No new unit. Killboard runs in-process (GDD §14.1); the §14.2 own-process/own-token option is
declined — a second process duplicates the token and loses the shared client for no benefit on a
4-vCPU/8GB box.

`cortana.doctor` stays module-runnable (install.sh GATE runs it as `aura` via `setpriv`). It gains
an optional per-module preflight (each enabled module may expose a `doctor()` check — killboard:
region host reachable, guild resolves) that is **skipped when the module is disabled**, so a
voice-only deploy stays green.

---

## 9. Health, status, and alarms

- `ModuleManager.health_snapshot()` merges each module's `health()` with the supervisor's status
  map. `StatusCog` `/botstatus` renders one section per module (CORTANA voice health + killboard
  poller health) through one code path.
- `/killboard status` (admin) surfaces poller detail: last successful poll, last new event, events
  stored, consecutive failures (GDD §10).
- New alarm codes in `alarms.py`: `MODULE_SETUP_FAILED`, `MODULE_TASK_DEGRADED`,
  `MODULE_QUARANTINED` — operator-facing cards in `#bot-health`, raised through the existing
  `AlarmBus` with `AllowedMentions.none()` (they never `@here`).

---

## 10. Test plan

- `test_module_supervisor.py`: crash → restart with backoff; clean return does not restart;
  `CancelledError` propagates on shutdown (does not hang the 8s budget); quarantine after
  `max_restarts`; failure-streak reset after `reset_after`; `_shutdown` is never set by a module
  crash.
- `test_module_manager.py`: disabled module builds nothing; a module whose `setup()` throws is
  quarantined and boot continues; `all_cogs()` excludes CORTANA; reload fan-out is isolated per
  module; `stop_all()` honors the per-module 2s budget and reverse order.
- `test_killboard_model.py`: KILL/DEATH/ASSIST classification; tolerant parse of partial JSON.
- `test_killboard_store.py`: high-water-mark + dedup by `EventId`; windowed count/fame/KD queries
  on `db.connect(":memory:")`.
- `test_killboard_poller.py`: fake `KbApi` — high-water advance, pagination on spikes, first-run
  backfill bound, no double-enqueue.
- `test_killboard_cards.py`: layout math + `aura:kb:*` id build/parse (no Discord objects).
- Existing gates stay green: `test_app.py` (App/`_on_control` untouched), `test_config.py`
  (bijection/parity/shipped-yaml), `test_help_cog.py` (killboard commands documented),
  `ruff check`/`ruff format --check` over `dead/` + `killboard/`, `gen_config_docs.py --check`.
- No live API, no real Discord, audio path untouched (constraint 5).

---

## 11. Risks & honest tradeoffs

1. **Shared-client blast radius.** Killboard cogs live in the same `AuraBot`. A killboard command
   that raises is caught by the tree error boundary — fine — but Pillow or a DB call *not*
   offloaded to a thread would stall voice too. Hard rule: all killboard CPU/DB work is
   `to_thread`; a `test_killboard_no_blocking` guard is worth adding. Sharpest edge of one-process
   efficiency (req 4).
2. **CORTANA is not truly isolatable.** Its setup/voice failure is genuinely fatal (exit 78/69).
   Isolation protects the platform *from add-on modules*; it does not make voice optional. Do not
   try to make voice hot-swappable — it destabilizes the thing the owner just stabilized.
3. **Two spawn policies invite misuse.** A killboard task wired to `App._spawn` instead of
   `ctx.supervisor.spawn` would take the whole bot (and Ears' DAVE session) down on an API blip —
   the exact failure the platform exists to prevent. Mitigation: `ModuleContext` deliberately does
   **not** expose `_spawn`; add-ons can only reach `ctx.supervisor`. Review-checklist item.
4. **Global command-tree ceiling.** All modules share one global tree copied onto the single guild;
   `copy_global_to` replaces the guild set each start. `app_commands.Group` per game mitigates
   collisions now; the 100-command ceiling is a scaling limit to watch as games #3+ land.
5. **`_CONN_LOCK` is module-global** (§6) — separate DB files still serialize on it. Negligible at
   killboard's write rate; revisit for a chatty future module.
6. **Shutdown-budget pressure.** `stop_all()` shares CORTANA's 8s window. Per-module `wait_for(2s)`
   + cancel-not-await bounds it; every future module must honor it. CortanaModule.stop() is a no-op
   so voice teardown is unaffected.
7. **Unofficial Albion API.** The feed rests on an undocumented endpoint that 504s and can change
   shape. The design absorbs this (store-is-history, tolerant parse, degrade-not-crash); the
   supervisor means a total outage or parser fault only marks killboard `DEGRADED`/`FAILED` —
   CORTANA never notices.

---

## 12. Build phases (see the file worklist in the structured spec)

`core` (dead kernel) → `cortana-adapter` (facade + 5 seams) → `killboard` (native module) →
`hardening` (supervisor/manager tests, alarms, doctor) → `docs-config-deploy` (schema, examples,
GDD regen, install.sh, requirements). Land the core + adapter in one PR that changes **no runtime
behavior** (all seams no-op when no add-on module is enabled), gated by the existing green
`test_app.py`; add killboard in a second PR.
