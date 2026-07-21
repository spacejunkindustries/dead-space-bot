# Deployment facts — the live install

**This file is the ground truth for how this bot is actually deployed.** The GDD
and CLAUDE.md describe the *design space* (and often assume a 2-vCPU droplet as the
teaching example); this file records what the **real production host** is, so no
future change re-derives the wrong assumption. When the design docs and this file
disagree about the running environment, **this file wins** — and if the host
changes, update this file in the same breath.

Nothing secret lives here. Tokens are systemd `LoadCredential=` only
(`/etc/cortana/token`, `/etc/cortana/anthropic`); the live guild/channel ids live
in `/etc/cortana/cortana.yaml` on the host, not in the repo.

---

## The host

- **Netcup RS 4000 G12 — AMD EPYC 9645, 12 dedicated vCPUs, 32 GB DDR5 ECC, 1 TB
  NVMe, `x86_64`, Debian 13 (trixie).** This replaced the earlier DigitalOcean
  4-vCPU box (migrated 2026-07-21). Every "on a 2-vCPU droplet do X / on a ≥4-vCPU
  box do Y" tradeoff in the GDD and config comments → **take the ≥4-vCPU branch**,
  and then some: this box has real headroom for the big models.
- **Debian 13 caveat — Python 3.12 is built from source.** The system `python3` on
  trixie is 3.13 (which removed `audioop`, a module discord.py imports), and there
  is no `python3.12` apt package. So `python3.12` is a source build at
  `/usr/local/bin/python3.12` (`./configure --with-ensurepip=install && make &&
  make altinstall`), with symlinks at `/usr/bin/python3.12` and inside the Piper
  venv so their interpreter references resolve. `deploy/install.sh` detects a
  working `python3.12` on PATH and skips the apt packages the distro lacks (PR #83).
- Voice/model settings for this host (the box's whole reason for being):
  - `capture.streaming: true` — live/early-commit recognition ON (GDD §5.5).
  - `stt.model: large-v3` — max-accuracy Whisper (was `base` on the DO box); the
    weights live in the HF cache (`HF_HOME=/var/lib/cortana/hf`), auto-downloaded on
    first load. `compute_type: int8`. Swap to `distil-large-v3` if streaming latency
    ever bites — near-large accuracy, ~6× faster.
  - `stt.cpu_threads: 4` — the 12 dedicated cores absorb it while Ears' high
    `CPUWeight` Opus mixer keeps its own.
- On-box LLMs, both live: `nlu.understanding: true` → **Ollama `qwen2.5:7b`** at
  `http://127.0.0.1:11434/v1/chat/completions` (grammar-first, GDD §6.7; upgraded
  from `llama3.2:3b` on the move to the 32 GB box); `chat.enabled: true` →
  Claude Haiku via API (no local CPU). Ollama is installed as its own systemd
  service; `ollama pull qwen2.5:7b` seeded the model.
- **Piper** is a pip venv at `/opt/piper` (not a standalone binary);
  `/usr/local/bin/piper` symlinks into it. When copying it between hosts, the
  venv's interpreter symlink must be repointed at the local `python3.12`.

## Deploy / operate

- Source checkout: `/opt/aura-src`. Deploy is:
  ```
  cd /opt/aura-src && git pull && bash deploy/install.sh
  systemctl restart cortana-brain
  ```
- `deploy/install.sh` is **staged, doctor-gated, auto-rollback** (GDD §17.5): it
  runs `python -m cortana.doctor` against the live config BEFORE swapping the
  release, and refuses (exit 78, nothing changed) on a bad config. This has already
  caught a bad hand-edit — **trust the gate; it is the safety net for mistakes.**
- Services: `cortana-brain.service`, `cortana-ears.service`, and
  `cortana-backup.timer` (below).
- Hot vs restart: most killboard/feed knobs are HOT (`/reload` in Discord applies
  them); `stt.*`, `database.path`, `discord.guild_id`, and enabling a new supervised
  loop (`public_juicy.enabled`) need a **restart**.

## Current operational posture (as configured)

- **Mentions are OFF on purpose** (`discord.mentions_enabled: false`) — the corp is
  still in testing and does not want live `@`-pings yet. The routing rules are not
  finished (roles not yet mapped); this is fine while mentions are off, and the
  "zero routing rules / nobody gets mentioned" alarm is **silenced whenever mentions
  are off** (it returns as CRITICAL the moment mentions are enabled). Turning pings
  on later = set real role names in `routing.yaml`, set `mentions_enabled: true`,
  `/reload`.
- **Killboard is live.** Guild **DEAD Renegadez**, region **west** (Americas). Kills,
  deaths (per-member sweep), Death Fame, rankings, and the market layer are all on.
- **Public-juicy feed is on** — server-wide notable kills to the juicy channel,
  independent of the tracked guild. Tuned for "whales only": `juicy_min_loot` set
  high (≈50M), `max_posts_per_scan` small (≈3), `max_priced_per_scan` sized to the
  scan window so no whale is missed, short `interval_seconds`, `scan_pages: 1`.
- **Nightly DB backups** run via `cortana-backup.timer` → `/var/lib/cortana/backups/`
  (both sqlite DBs, gzipped, keep-7, idle priority, self-pruning).

## Still human-only (cannot be automated — GDD "needs a human")

- Verify DAVE actually negotiates (real token + real voice channel + a person
  talking; `self_deaf` must be `false`).
- Wake-phrase false-accept rate against real comms audio.
- Tuning thresholds/prior weights from real fleet recordings.

## Change log — the Albion/killboard build-out (this era)

Merged to `main` in order:

- Market layer (AODP): item loot value on cards + `/market` commands.
- Dead-Gaming branded cards + Daily/Weekly/Monthly ranking cards (reaper watermark,
  DEAD roundel), detailed Albion-style kill card (killer-L / victim-R paperdolls,
  centre stats, damage/heal, dropped-loot grid).
- Death Fame fix: the guild `/events` feed is **kill-only**, so guild deaths are
  swept **per-member** from `/players/{id}/deaths` (config `killboard.poller.track_deaths`);
  a recency gate (`deaths_post_window_minutes`) keeps the first sweep from spamming
  weeks-old death cards while still filling Death Fame.
- Public-juicy feed: a separate supervised loop over Albion's **server-wide** kill
  feed, fame-first / loot-second qualification (OR), a hard per-scan post cap and a
  per-scan loot-pricing budget (bounded concurrency).
- Two adversarial audits → verified fixes: feed-wedge on a permanent 4xx, DST
  schedule skip, schedule double-post race, `health()` off the event loop, the
  DAVE-watchdog wall-clock→monotonic fix, a sqlite-connection leak, an unbounded
  queue, and the public-feed pricing bound.
- Routing alarm gated on `mentions_enabled` (no false CRITICAL while pings are off).
- Ops: nightly DB backup timer.
- Killboard + CORTANA audit fixes (PR #82): public-juicy pricing budget, deaths-sweep
  N+1 batched, `json.dumps` off the loop, `/reload` single-flight + ChatClient close,
  `voice_offline` latch clear, and the **gazetteer metaphone blocking index**
  (provably accuracy-neutral, 5–8× faster resolves).

## Change log — the Netcup migration (2026-07-21)

Moved off the DigitalOcean 4-vCPU box onto the Netcup RS 4000 G12 (12 dedicated
vCPUs / 32 GB / Debian 13), preserving full history:

- Built `python3.12` from source (Debian's is 3.13); `install.sh` made Debian-aware
  (PR #83). Deploy key on the box for `git pull`; source at `/opt/aura-src`.
- Config + secrets (`/etc/cortana/*`) and the model assets (`/opt/piper`,
  `/opt/cortana/models/*`) rsync'd from the old box; the full `cortana.db`
  (5485 systems + incidents + command log) copied at cutover with the old box
  stopped, so zero data loss.
- Upgraded the models the 32 GB box was bought for: `stt.model base → large-v3`,
  `cpu_threads 2 → 4`, understanding LLM `llama3.2:3b → qwen2.5:7b`.
- Verified live: brain + ears active, IPC handshake, `bot_ready`, large-v3 warmed,
  killboard polling. Old DO box kept **stopped** as a warm rollback, then retired.

## Open ops to-do (recommended, not yet done)

- **Provision swap + set `MemoryMax`/`MemoryHigh` + `TasksMax` on both units — now
  more urgent:** the Netcup box ships with **0 swap**, and large-v3 + qwen2.5:7b
  resident is real memory. No limits today → an OOM takes the whole box, not one cgroup.
- Create `/etc/cortana/alert_webhook` (0600) so `OnFailure=` alerts reach Discord,
  not just `/var/lib/cortana/alerts.log`.
- Cap journald retention (`SystemMaxUse=` in `journald.conf`) — transcript + poll
  logs grow unbounded otherwise.
- Finish `Type=notify-reload` + `WatchdogSec` wiring so a wedged-but-alive brain is
  caught (currently `Type=simple`).
- `ufw` deny-inbound except SSH (the bot opens no listening ports; IPC is a unix socket).
