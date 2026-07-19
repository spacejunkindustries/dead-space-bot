-- Migration 0008 — learned custom areas (GDD §8.5a "confirm-and-learn a place word").
-- Applied by aura/core/db.py; on success the runner sets PRAGMA user_version = 8.
--
-- The systemless twin of the aliases table (§8.5): a learned alias is
-- phrase -> system_id; a custom area is phrase -> display_name with NO system.
-- When a pilot reports a place that resolves to no system, CORTANA asks once
-- ("Did you say <word>?"); on an explicit yes the confirmed word is stored here
-- and every later report of it resolves at full confidence, posting verbatim
-- (system_id NULL, GDD §8.6). Text only — the confirmed word, never audio
-- (constraint 5) — exactly like the aliases table. Per-guild; capped by
-- areas.max_per_guild; managed with /areas-list | /areas-forget | /areas-add.
CREATE TABLE custom_areas (
    guild_id      INTEGER NOT NULL,
    phrase        TEXT    NOT NULL,   -- normalized lookup key: text.strip().lower()
    display_name  TEXT    NOT NULL,   -- verbatim word the pilot confirmed; shown on the card
    learned_by    INTEGER NOT NULL,
    learned_at    TEXT    NOT NULL,
    uses          INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, phrase)
);
