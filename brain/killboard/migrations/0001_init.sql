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
