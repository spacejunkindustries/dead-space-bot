-- AURA data model — GDD §14.
--
-- This file is the reference snapshot of the current schema. It is applied to a
-- fresh database only via the migration runner in aura/core/db.py, which tracks
-- the applied schema revision in `PRAGMA user_version` (one integer per file in
-- brain/migrations/, ordered by the numeric filename prefix). Never edit this
-- file in place for a live change — add a new migration and regenerate this
-- snapshot in the same commit (CLAUDE.md conventions).

-- ── gazetteer ────────────────────────────────────────────────
CREATE TABLE systems (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    region          TEXT NOT NULL,
    constellation   TEXT,
    metaphone       TEXT NOT NULL,      -- precomputed at load
    x REAL, y REAL, z REAL
);
CREATE INDEX idx_systems_region ON systems(region);
CREATE INDEX idx_systems_metaphone ON systems(metaphone);

CREATE TABLE system_adjacency (
    a_id INTEGER NOT NULL REFERENCES systems(id),
    b_id INTEGER NOT NULL REFERENCES systems(id),
    PRIMARY KEY (a_id, b_id)
);

-- ── learned corrections; consulted BEFORE phonetic matching ──
CREATE TABLE aliases (
    raw_text        TEXT NOT NULL,
    system_id       INTEGER NOT NULL REFERENCES systems(id),
    weight          REAL NOT NULL DEFAULT 1.0,
    learned_at      TEXT NOT NULL,
    corrected_by    INTEGER NOT NULL,
    PRIMARY KEY (raw_text, system_id)
);

-- ── incidents ────────────────────────────────────────────────
CREATE TABLE incidents (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id           INTEGER NOT NULL,
    system_id          INTEGER REFERENCES systems(id),
    system_confidence  REAL,
    type               TEXT NOT NULL,
    severity           TEXT NOT NULL,
    reporter_id        INTEGER NOT NULL,
    detail             TEXT,
    opened_at          TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'ACTIVE',
    message_id         INTEGER,
    channel_id         INTEGER,
    raw_system_text    TEXT               -- transcript window that named the
                                          -- system; alias key for [Wrong — fix]
);
CREATE INDEX idx_inc_active ON incidents(guild_id, status, system_id, type, opened_at);

CREATE TABLE incident_updates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id  INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL,
    text         TEXT,
    at           TEXT NOT NULL
);

CREATE TABLE responders (
    incident_id  INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL,
    state        TEXT NOT NULL,           -- OTW | WATCHING | NO
    at           TEXT NOT NULL,
    PRIMARY KEY (incident_id, user_id)
);

-- ── routing ──────────────────────────────────────────────────
CREATE TABLE subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    role_id       INTEGER NOT NULL,
    types_json    TEXT NOT NULL,
    scope_json    TEXT NOT NULL,
    escalate_at   TEXT,
    quiet_hours_json TEXT
);

-- ── scheduled ────────────────────────────────────────────────
CREATE TABLE timers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    system_id   INTEGER REFERENCES systems(id),
    fires_at    TEXT NOT NULL,
    note        TEXT,
    created_by  INTEGER NOT NULL,
    fired       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_timers_pending ON timers(fired, fires_at);

-- ── consent ──────────────────────────────────────────────────
CREATE TABLE optouts (
    user_id  INTEGER PRIMARY KEY,
    at       TEXT NOT NULL
);
CREATE TABLE voice_mutes (
    user_id  INTEGER PRIMARY KEY,
    at       TEXT NOT NULL
);

-- ── personal reminders (/remindme) ───────────────────────────
CREATE TABLE reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    fires_at    TEXT NOT NULL,
    message     TEXT NOT NULL,
    fired       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_reminders_pending ON reminders(fired, fires_at);
CREATE INDEX idx_reminders_user ON reminders(user_id, fired);

-- ── quick votes (/poll) ──────────────────────────────────────
CREATE TABLE polls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER,
    message_id   INTEGER,
    author_id    INTEGER NOT NULL,
    question     TEXT NOT NULL,
    options_json TEXT NOT NULL,        -- JSON array of option labels
    opened_at    TEXT NOT NULL,
    closed_at    TEXT                  -- NULL while the poll is open
);

CREATE TABLE poll_votes (
    poll_id     INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL,
    option_idx  INTEGER NOT NULL,
    at          TEXT NOT NULL,
    PRIMARY KEY (poll_id, user_id)     -- one vote per pilot; switchable
);

-- ── observability: transcripts only, never audio ─────────────
CREATE TABLE command_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL,
    raw_transcript    TEXT NOT NULL,
    parsed_intent     TEXT,
    matched_system_id INTEGER REFERENCES systems(id),
    confidence        REAL,
    tier              TEXT,               -- HIGH | MEDIUM | LOW
    outcome           TEXT,               -- POSTED | FOLDED | ASKED | REJECTED
    at                TEXT NOT NULL
);
CREATE INDEX idx_cmdlog_at ON command_log(at);
