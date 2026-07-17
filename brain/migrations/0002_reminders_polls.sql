-- Migration 0002 — reminders and polls (utility cog, GDD §7).
-- Applied by aura/core/db.py; on success the runner sets PRAGMA user_version = 2.

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
