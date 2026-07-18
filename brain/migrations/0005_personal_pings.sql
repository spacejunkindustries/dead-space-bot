-- Migration 0005 — personal ping subscriptions (GDD §10.3 "ping me for ...").
-- Applied by aura/core/db.py; on success the runner sets PRAGMA user_version = 5.
--
-- A user mention, not a role: each row asks CORTANA to append <@user_id> to the
-- mention line of matching incident cards. types_json is a JSON array of
-- Intent values; system_id NULL means the subscription covers all systems.
-- Capped per user by discipline.personal_pings_max; personal pings ride the
-- existing mention discipline and can never cause @here (constraint 11).

CREATE TABLE personal_pings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    types_json  TEXT NOT NULL,
    system_id   INTEGER REFERENCES systems(id),
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_personal_pings_guild ON personal_pings(guild_id);
