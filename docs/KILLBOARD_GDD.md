# Albion Killboard — Design Document

**A self-hosted Albion Online killboard for a single guild**
discord.py module · DigitalOcean VPS · SQLite
Version 1.0 · July 2026

---

## 1. Product definition

A Discord bot module that watches one Albion Online guild and posts its PvP activity to Discord in real time:

- **Kill & death feed** — every kill and death involving the tracked guild, as a rich embed with a rendered kill card (gear, item power, fame, damage contribution).
- **Fame rankings** — daily, weekly, and monthly Kill Fame leaderboards for guild members, posted on a schedule.
- **A real kill counter** — kills, deaths, and true K/D *by count* per member and per guild, over any window. The game's own API does not expose this number; this bot derives it (§8), which is the main reason to run your own rather than rent one.
- **Battle summaries** — large fights the guild took part in, condensed to one card.
- **On-demand queries** — slash commands for anyone in the server to pull rankings, a member's record, or the guild's recent kills.

It is a self-contained module that runs inside an existing self-hosted discord.py bot on a VPS. It adds its own cog, its own SQLite tables, and its own background task; it does not touch anything else the bot does.

### 1.1 What it replaces

Hosted Albion killbots charge a monthly subscription for exactly this feature set. This runs on hardware you already operate, tracks the one guild you care about, and costs nothing beyond the VPS. It also gives you the by-count statistics the paid options can't, because those come from your own event store rather than the API.

### 1.2 Target

**One guild.** The guild name or ID is a config value. Everything — feed, rankings, counters — is scoped to that guild. Multi-guild tracking is deliberately out of scope: a single target keeps the polling budget tiny and the data model simple.

---

## 2. Data source: the Albion gameinfo API

This section is normative. The API's quirks dictate the whole ingestion design, and several of them are traps.

### 2.1 What it is

Albion's public killboard is backed by an HTTP API at `gameinfo.albiononline.com`. It is **owned by the game's developer but not officially supported for third-party use** — it exists to serve the official killboard site, and there is no documentation, no API key, and no stability guarantee. Requests are plain unauthenticated `GET`s returning JSON.

The practical consequence: **treat it as a best-effort feed, not a reliable service.** The PvP endpoints are frequently slow, `504 Gateway Timeout` is common under load, and battles occasionally go missing from responses. The bot is engineered around this (§13), not in spite of it.

### 2.2 Servers are separate, and so are their API hosts

Albion runs three regional servers with fully separate economies, guilds, and data. A guild exists on exactly one, and each server has its own API host. Set the correct one in config:

| Server | Region | API base |
|---|---|---|
| Americas (West) | Washington, D.C. | `https://gameinfo.albiononline.com/api/gameinfo` |
| Europe | Amsterdam | `https://gameinfo-ams.albiononline.com/api/gameinfo` |
| Asia / East | Singapore | `https://gameinfo-sgp.albiononline.com/api/gameinfo` |

Hitting the wrong host returns a guild-not-found or empty data — a silent failure, so this is the first thing to verify on setup.

### 2.3 Endpoints used

Append to the region host from §2.2.

| Endpoint | Returns | Used for |
|---|---|---|
| `/search?q={name}` | Matching players, guilds, alliances (with IDs) | Resolving the guild name → guild ID once at setup |
| `/events?guildId={id}&sort=recent&limit={n}&offset={o}` | Kill events where the guild is killer **or** victim | The feed and the event store (§5) |
| `/guilds/{id}` | Guild-level Kill Fame, Death Fame, member count | Guild summary card |
| `/guilds/{id}/members` | Per-member lifetime Kill/Death Fame and ratio | Member roster + lifetime fame |
| `/guilds/{id}/top` | The guild's highest-fame kills | "Top kills" command |
| `/battles?guildId={id}&sort=recent` | Large-scale fights | Battle summaries (§9) |

**Item icons** come from a *different, documented, and reliable* host — the render service at `https://render.albiononline.com/v1/item/{ItemType}.png` (append `@{0-3}` for enchant level). Unlike the gameinfo API, this one is officially supported and cacheable, so kill cards never depend on the flaky endpoint for imagery.

### 2.4 The three traps

Everything in §5 and §8 exists to handle these.

**Trap 1 — there is no kill *count*.** The API exposes Kill Fame and Death Fame, but a lifetime "total kills" number comes back null on every endpoint. Fame is not a proxy for count: one high-value gank can outweigh fifty small kills. **The only way to get a true count is to ingest events yourself and count them.** This is the core of the design.

**Trap 2 — the event window is shallow and volatile.** The events feed holds only a rolling window of the most recent events and pushes new ones every couple of minutes. There is no deep historical pagination. **Miss a polling window and those events are gone forever.** The bot must poll continuously and persist every event to its own database — the database *is* the history, because the API has none to give.

**Trap 3 — aggregate stats lag by up to a day.** The guild- and member-level fame totals from `/guilds/{id}` and `/guilds/{id}/members` update roughly once per day and have stalled for several days at a time. They are fine for a lifetime roster snapshot but useless for anything current. **All current and windowed rankings are computed from the ingested event store instead** (§8), which is both fresher and more granular than the API aggregates.

---

## 3. Architecture

A single long-running process — the existing bot — gaining one cog and one background task.

```
┌──────────────────────────────────────────────────────────────┐
│  VPS — existing discord.py bot process                      │
│                                                              │
│  ┌────────────────────┐        ┌──────────────────────────┐ │
│  │  Poller task       │        │  Killboard cog           │ │
│  │  (asyncio loop)    │        │                          │ │
│  │                    │        │  slash commands          │ │
│  │  every ~45s:       │        │  scheduled ranking posts │ │
│  │   fetch events ────┼───┐    │  feed poster             │ │
│  │   dedup by EventId │   │    │  card renderer (Pillow)  │ │
│  │   persist          │   │    └──────────────────────────┘ │
│  │   enqueue new      │   │              │                  │
│  └────────────────────┘   │              │                  │
│           │               ▼              ▼                  │
│           │        ┌──────────────────────────┐            │
│           └───────►│  SQLite: aoedge.db        │            │
│                    │  events, participants,    │            │
│                    │  poll_state, posted, …    │            │
│                    └──────────────────────────┘            │
│                                                              │
│  icon cache: /var/lib/aoedge/icons/  ◄── render.albion…    │
└──────────────────────────────────────────────────────────────┘
```

### 3.1 Two responsibilities, cleanly split

- **The poller** is the only thing that talks to the gameinfo API. It fetches, deduplicates, persists, and hands new events to the poster. It is defensive by design — every failure mode in §13 is its concern.
- **The cog** owns everything Discord-facing: posting feed cards, running scheduled rankings, and answering slash commands. It reads from SQLite and never calls the gameinfo API directly for feed data.

The database is the boundary between them. The poller writes; the cog reads. This means a slow or failing API degrades the *feed's freshness* but never blocks a slash command — `/kd` and `/ranking` answer instantly from stored data even while the API is down.

---

## 4. Component manifest

| Module | Responsibility |
|---|---|
| `killboard/__init__.py` | Cog registration, task startup/shutdown |
| `killboard/api.py` | Async gameinfo client: region host, timeouts, retry/backoff, JSON parsing, tolerant field access |
| `killboard/poller.py` | Poll loop, EventId high-water-mark, dedup, persistence, staleness detection, new-event queue |
| `killboard/model.py` | Event/participant parsing from raw JSON into typed rows; guild-relation classification (§6) |
| `killboard/store.py` | SQLite access, migrations, windowed count/fame queries, backup hook |
| `killboard/feed.py` | Consumes the new-event queue → builds embeds → posts to feed channels → records in `posted` |
| `killboard/cards.py` | Kill-card image compositing (Pillow); icon fetch + on-disk cache from the render service |
| `killboard/rankings.py` | Windowed Kill Fame / kill-count / K-D leaderboards; formatting |
| `killboard/battles.py` | Battle fetch, guild-participation filter, summary embed |
| `killboard/schedule.py` | Daily/weekly/monthly scheduled ranking posts |
| `killboard/commands.py` | Slash commands (§10) |
| `killboard/config.py` | Module config load + validation (§12) |
| `killboard/migrations/` | Ordered SQL migrations |

---

## 5. Event ingestion pipeline

The heart of the bot. Because the API keeps no history (§2.4, traps 2 & 3), this pipeline *is* the historical record.

```
every poll_interval_seconds (default 45):

  fetch /events?guildId={id}&sort=recent&limit=51&offset=0
        │
        ├─ request fails (timeout / 5xx / 429) ──► backoff, record fail, retry next tick
        │
        ▼
  parse events (tolerant: never crash on a missing field)
        │
        ▼
  for each event, newest→oldest:
        │  EventId ≤ last_event_id ?  ──► stop; everything older is already stored
        │
        ├─ classify relation to tracked guild (KILL / DEATH / ASSIST)   [§6]
        ├─ upsert into events + participants
        └─ if not already in `posted`, enqueue for the feed
        │
        ▼
  last_event_id ← max(EventId seen)
  last_success_at ← now;  if new events: last_advanced_at ← now
```

### 5.1 Deduplication by EventId

Every Albion kill event carries a unique, monotonically increasing integer `EventId`. That single fact makes dedup trivial and reliable: **persist the highest EventId processed, and on each poll ignore everything at or below it.** No fuzzy matching, no timestamp windows. The `events` table uses `EventId` as its primary key, so a duplicate insert is a no-op even if the high-water-mark logic and the poll overlap.

### 5.2 Never miss an event

With `limit=51` (the endpoint's maximum per request) and a 45-second interval, the bot pulls up to 51 of the guild's most recent events every poll. As long as the guild produces fewer than 51 events in any 45-second span, nothing is ever missed. For a guild whose activity can burst above that — large ZvZ nights — the poller **auto-paginates**: if the oldest event in a page is still newer than `last_event_id`, it fetches the next page (`offset += 51`) until it reaches known ground or the server's offset ceiling (~1000). This guarantees completeness during spikes without wasting requests during quiet hours.

### 5.3 First-run backfill

On first launch against a fresh database, `last_event_id` is 0, so the poller pages backward from the newest event through the server's available window (offset up to ~1000, in pages of 51) to seed as much recent history as the API still holds. From then on it only ever moves forward. Backfill is bounded by what the API retains — it captures the recent past, not the guild's entire lifetime, because that data no longer exists anywhere to fetch.

### 5.4 What is stored

Full event JSON is retained per row (`raw_json`) alongside the extracted columns. This costs a little disk and buys two things: kill cards can be re-rendered later without re-fetching, and if a future feature needs a field not currently extracted, the data is already there. At a few kilobytes per event and a realistic event rate, a year of history is well under the size of a single game screenshot's worth of concern.

---

## 6. Classifying guild involvement

Each ingested event is labelled from the tracked guild's point of view:

| Relation | Condition |
|---|---|
| **KILL** | The killer's `GuildId` is the tracked guild |
| **DEATH** | The victim's `GuildId` is the tracked guild |
| **ASSIST** | A tracked-guild member appears in `Participants` (dealt damage) but is neither the killer nor the victim |

An event can be both a KILL and an ASSIST-heavy group kill; the final-blow killer's guild determines KILL/DEATH, and participant rows capture everyone who contributed. Assists are stored so that damage contribution shows up on cards and counts toward a member's activity, even when they didn't land the killing blow.

Solo vs. group is derived from `numberOfParticipants`; blob kills (large participant counts) can be visually flagged or routed to a separate channel via config (§7.2).

---

## 7. Kill & death feed

### 7.1 The card

Each feed post is a Discord embed plus a composited **kill card image**:

- **Header** — `Killer ▸ Victim`, colour-coded (green when the guild got the kill, red when it took the death).
- **Gear grid** — victim's equipped items rendered as icons (weapon, off-hand, head, chest, boots, cape, bag, mount, potion, food), pulled from the render service and cached on disk. Killer's build shown compactly alongside.
- **Item power** — average IP for each side.
- **Fame** — the kill fame awarded.
- **Damage contribution** — a bar list of the top participants by damage share, so group kills show who actually did the work.
- **Footer** — location, timestamp, and a link to the event on the official killboard.

Cards are rendered with Pillow. Icons are fetched once from the render service and cached under `/var/lib/aoedge/icons/{ItemType}@{enchant}.png`; subsequent cards reuse the local copy, so the feed stays fast and puts near-zero load on Albion's servers.

### 7.2 Channels and filtering

Configurable routing keeps the feed readable:

| Setting | Effect |
|---|---|
| `kills_channel` | Where guild kills post |
| `deaths_channel` | Where guild deaths post (may be the same channel) |
| `min_fame` | Suppress trivial kills below a fame threshold from the main feed |
| `juicy_channel` + `juicy_min_fame` | High-value kills above a threshold also post here, for a highlights feed |
| `ignore_deaths_below_ip` | Optionally skip low-IP "naked" deaths that just clutter the feed |
| `blob_participant_threshold` | Kills with participant counts above this route to an optional ZvZ channel |

### 7.3 Posting discipline

- The `posted` table records every event already sent, keyed by EventId, so a restart mid-poll never double-posts.
- Deaths and kills are posted oldest-first within a batch so the channel reads chronologically.
- If the bot was offline and backfill ingests a large burst, feed posting is rate-limited (a short delay between messages) and capped per catch-up cycle, with a single "posted N older events" summary rather than flooding the channel.

---

## 8. Rankings and the kill counter

This is the capability the paid bots can't match, because it comes from the event store, not the API.

### 8.1 Everything is a query over stored events

Because every guild event is persisted with a timestamp, relation, and participants, any window and any metric is a straightforward query:

| Metric | Derivation |
|---|---|
| **Kill count** | Count of KILL rows (or events where the member dealt damage) in the window |
| **Death count** | Count of DEATH rows for the member in the window |
| **True K/D** | Kill count ÷ death count — *by number*, the stat the API never provides |
| **Kill Fame** | Sum of awarded fame across the member's kills in the window |
| **Assists** | Count of ASSIST participations |
| **Damage dealt** | Sum of participant damage across the window |

Windows are arbitrary: today, this week, this month, or a custom range. Rankings can be per-member (guild leaderboard) or guild-wide totals.

### 8.2 Two notions of fame, kept distinct

- **Windowed Kill Fame** — computed from ingested events. Current to the last poll, granular to any window. This is what scheduled rankings and leaderboards use.
- **Lifetime Kill Fame** — read from `/guilds/{id}/members`. Reflects the member's whole career but updates only ~daily. Shown on a member's profile card as their all-time figure, clearly labelled as lifetime.

Keeping them separate avoids the confusion of a "this week" number that silently includes career totals.

### 8.3 Scheduled posts

The scheduler posts leaderboards automatically:

- **Daily** — yesterday's top killers by fame and by count.
- **Weekly** — the week's leaders, plus most-improved.
- **Monthly** — the month in review: top fame, top count, best K/D, most active.

Each is a config-defined `(kind, channel, hour_utc)` row. The scheduler is idempotent — it records `last_run` and will not double-post if the bot restarts within the same period.

---

## 9. Battle summaries

Large fights are fetched from `/battles?guildId={id}` and condensed to a single embed: participating guilds and alliances, player counts per side, total fame swung, kills/deaths per side, and a link to the full battleboard. A battle only posts if the guild's participation crosses a configurable threshold (`battle_min_players` / `battle_min_fame`), so routine skirmishes don't spam a battle channel. Because the battles endpoint is among the least reliable (§13), battle posting tolerates gaps quietly and never blocks the kill feed.

---

## 10. Commands

All slash commands read from the local store and answer instantly, independent of API health.

| Command | Purpose |
|---|---|
| `/ranking [period] [metric]` | Leaderboard — period `today\|week\|month`, metric `fame\|kills\|kd` |
| `/record <player>` | A member's kills, deaths, K/D, fame, and assists for the current month, plus lifetime fame |
| `/recent [count]` | The guild's most recent kills/deaths as a compact list |
| `/topkills [period]` | Highest-fame kills in the window, with cards |
| `/guild` | Guild summary — member count, lifetime fame, this month's totals |
| `/battles [count]` | Recent battles the guild fought |
| `/killboard-status` | *(admin)* Poller health: last successful poll, last new event, events stored, consecutive failures |
| `/killboard-config` | *(admin)* View and set channels, thresholds, region, tracked guild |
| `/killboard-schedule` | *(admin)* Manage scheduled ranking posts |

---

## 11. Data model

```sql
-- Ingested events — the historical record the API does not keep.
CREATE TABLE events (
    event_id           INTEGER PRIMARY KEY,   -- Albion EventId: unique, increasing
    timestamp          TEXT NOT NULL,
    killer_id          TEXT,
    killer_name        TEXT,
    killer_guild_id    TEXT,
    killer_ip          REAL,
    victim_id          TEXT,
    victim_name        TEXT,
    victim_guild_id    TEXT,
    victim_ip          REAL,
    total_fame         INTEGER,               -- TotalVictimKillFame
    relation           TEXT NOT NULL,         -- KILL | DEATH | ASSIST (guild POV)
    num_participants   INTEGER,
    battle_id          INTEGER,
    location           TEXT,
    raw_json           TEXT NOT NULL,         -- full event for re-render / future fields
    ingested_at        TEXT NOT NULL
);
CREATE INDEX idx_events_ts       ON events(timestamp);
CREATE INDEX idx_events_relation ON events(relation, timestamp);

-- Per-participant damage/heal share, for cards, assists, and damage rankings.
CREATE TABLE participants (
    event_id     INTEGER NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    player_id    TEXT NOT NULL,
    player_name  TEXT,
    guild_id     TEXT,
    damage_done  REAL,
    healing_done REAL,
    PRIMARY KEY (event_id, player_id)
);
CREATE INDEX idx_participants_player ON participants(player_id);

-- Single-row poller state: high-water mark + staleness tracking.
CREATE TABLE poll_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    last_event_id     INTEGER NOT NULL DEFAULT 0,
    last_success_at   TEXT,
    last_advanced_at  TEXT,                    -- last time a NEW event arrived
    consecutive_fails INTEGER NOT NULL DEFAULT 0
);

-- Feed-side dedup, independent of ingestion.
CREATE TABLE posted (
    event_id   INTEGER PRIMARY KEY REFERENCES events(event_id),
    message_id INTEGER,
    channel_id INTEGER,
    posted_at  TEXT NOT NULL
);

-- Lifetime roster snapshot from /guilds/{id}/members (updates ~daily).
CREATE TABLE members (
    player_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    kill_fame   INTEGER,
    death_fame  INTEGER,
    last_seen   TEXT,
    updated_at  TEXT
);

-- Scheduled ranking posts.
CREATE TABLE schedules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,                 -- daily | weekly | monthly
    channel_id  INTEGER NOT NULL,
    hour_utc    INTEGER NOT NULL,
    last_run    TEXT
);
```

`events` + `participants` answer every ranking and counter query. `poll_state` is the ingestion brain. `posted` keeps the feed exactly-once. `members` backs lifetime figures. `schedules` drives automated posts.

---

## 12. Configuration

```yaml
albion:
  region: west                      # west | europe | east  → selects API host (§2.2)
  guild_name: "<your guild name>"   # resolved to guild_id once at startup via /search
  guild_id: null                    # optional: set directly to skip name resolution

poller:
  interval_seconds: 45
  request_timeout_seconds: 10
  max_retries: 3
  backoff_base_seconds: 5           # exponential: 5, 10, 20 …
  page_limit: 51                    # endpoint maximum
  max_backfill_pages: 20            # first-run seed depth (≈ server ceiling)

feed:
  kills_channel: 000000000000000000
  deaths_channel: 000000000000000000
  min_fame: 0
  juicy_channel: null
  juicy_min_fame: 2000000
  ignore_deaths_below_ip: 0
  blob_participant_threshold: 20
  blob_channel: null
  catchup_max_posts: 20             # cap when returning from downtime
  post_delay_ms: 750                # spacing between feed messages

cards:
  enabled: true
  icon_cache_dir: /var/lib/aoedge/icons
  render_base: https://render.albiononline.com/v1

rankings:
  timezone: UTC
  schedules:
    - { kind: daily,   channel: 000000000000000000, hour_utc: 12 }
    - { kind: weekly,  channel: 000000000000000000, hour_utc: 12 }   # Mondays
    - { kind: monthly, channel: 000000000000000000, hour_utc: 12 }   # 1st of month

battles:
  channel: null
  min_players: 20
  min_fame: 5000000

storage:
  db_path: /var/lib/aoedge/aoedge.db

staleness:
  warn_after_minutes: 30            # no successful poll → warn in status
  no_events_notice_hours: 6         # no NEW events → note (guild may just be quiet)
```

---

## 13. Reliability and API-failure handling

The gameinfo API is flaky (§2.1). The bot's job is to stay correct and quiet through that.

| Condition | Detection | Response |
|---|---|---|
| Request timeout / `5xx` / `504` | Non-200 or exception from the client | Exponential backoff, increment `consecutive_fails`, retry next tick. Feed keeps serving stored data. |
| Rate limiting (`429`) | Status 429 | Back off harder; lengthen interval temporarily. |
| Stalled data | `last_success_at` older than `warn_after_minutes` | Surface in `/killboard-status`; optional one-time admin ping. No channel spam. |
| No new events for hours | `last_advanced_at` old but polls succeeding | Treated as "guild is quiet," not an error — noted in status only after `no_events_notice_hours`. |
| Malformed / partial event | Field missing during parse | Tolerant parser fills nulls and stores what it can; the event is never dropped for a missing optional field. |
| Wrong region host | Guild resolves empty at startup | Fail fast on launch with a clear message naming the likely region mismatch. |
| Activity spike above page size | Oldest event in page still newer than high-water mark | Auto-paginate (§5.2) until caught up. |
| Bot restart mid-poll | — | High-water mark + `posted` table make ingestion and feed both idempotent; no gaps, no duplicates. |

The guiding rule: **a broken API costs freshness, never correctness, and never a crash.** Stored history is always queryable; the feed simply resumes when the API recovers.

---

## 14. Deployment

### 14.1 Footprint

The module lives inside the existing bot process, so there is no new service to run — just new files, new config, and one background task the bot starts on load. Additional dependencies:

```
pillow            # kill-card compositing
aiohttp           # async API + icon fetch (already present if the bot makes HTTP calls)
```

Runtime data lives under `/var/lib/aoedge/` (database + icon cache); ensure the bot's user owns it.

### 14.2 If run as its own process instead

The design also stands alone as a dedicated bot if preferred — same code, its own Discord token, its own `systemd` unit with `Restart=always`. The only difference is a second token and process; the architecture is identical.

### 14.3 Operations

| Concern | Approach |
|---|---|
| **Backups** | Nightly `sqlite3 aoedge.db ".backup"` to off-box storage. The event store is irreplaceable — the API cannot re-serve old events (§2.4). |
| **Icon cache** | Grows slowly, bounded by the number of distinct items; safe to delete, repopulates on demand. |
| **Monitoring** | `/killboard-status` for at-a-glance health; the poller logs each cycle's outcome. |
| **Guild moves servers** | Change `region` and re-resolve `guild_id`; existing history is retained. |

### 14.4 Bot permissions

| Permission | Why |
|---|---|
| `Send Messages`, `Embed Links` | Post feed and ranking embeds |
| `Attach Files` | Post rendered kill cards |
| `Read Message History` | Command context |
| **Intent** — message content not required | All interaction is via slash commands |

---

## 15. API etiquette

The gameinfo API is unsupported and shared by the whole community's tools. The bot is a good citizen:

- **One guild, gentle interval.** A single `/events` call every 45 seconds is a negligible load, versus scraping broad global feeds.
- **Icons cached locally.** Each item icon is fetched from the render service once, then served from disk forever.
- **Backoff on failure.** The bot slows down when the API struggles rather than hammering it.
- **A descriptive User-Agent** identifying the bot, so operators can be contacted if needed.
- **No credentials, no scraping of anything gated** — only the same public endpoints the official killboard already serves.

---

## 16. Sources

- Albion gameinfo API — endpoint reference (search, players, guilds, guild members): https://forum.albiononline.com/index.php/Thread/117487-Albion-Api/
- Guild kill events via `guildId` parameter: https://forum.albiononline.com/index.php/Thread/139446-SBI-can-we-please-talk-about-your-unoffical-API/
- Guild `/top` endpoint (gist): https://gist.github.com/KishoreKaushal/430d735413c9412ce986747579cb17a1
- `totalKills` not exposed by any endpoint: https://forum.albiononline.com/index.php/Thread/164429-TotalKills-from-API/
- Events window is shallow / recent-events only, pushed every few minutes: https://findley.dev/projects/albion-stats/
- Regional API hosts (West / `ams` EU / `sgp` East): https://forum.albiononline.com/index.php/Thread/195086-Gameinfo-API-on-Europe/ · https://forum.albiononline.com/index.php/Thread/203817-API-server-is-not-working-well-Please-fix-it/
- Server locations and launch dates: https://en.wikipedia.org/wiki/Albion_Online
- Aggregate stats update ~daily / stalls: https://forum.albiononline.com/index.php/Thread/172795-Character-Stats-API/
- PvP endpoints slow / 504s / missing battles: https://forum.albiononline.com/index.php/Thread/131017-Update-on-API-New-Endpoints/
- Render service (item icons), officially documented: https://forum.albiononline.com/index.php/Thread/131017-Update-on-API-New-Endpoints/ · https://wiki.albiononline.com/wiki/API:Render_service

---

*The gameinfo API is unofficial and changes without notice. Verify the region host and the guild ID resolution on first setup, and confirm the endpoint shapes against a live response before relying on any single field.*
