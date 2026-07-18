-- Small persistent key/value store for process-level state that must
-- survive restarts. First user: the §19 join-announcement rate limit —
-- restart churn used to re-post the consent notice on every rejoin, and an
-- in-memory timestamp resets exactly when the spam happens.
CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
