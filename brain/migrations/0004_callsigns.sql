-- Migration 0004 — pilot callsign registry (GDD §6.1 register/unregister/who-am-i).
-- Applied by aura/core/db.py; on success the runner sets PRAGMA user_version = 4.
--
-- Identity is the Discord user id Ears already attaches to every utterance
-- (SSRC→user map). This is a name registry keyed on that id — no voice
-- biometrics, no audio, nothing derived from the audio path (GDD §19).

CREATE TABLE callsigns (
    user_id        INTEGER PRIMARY KEY,
    callsign       TEXT NOT NULL,
    registered_at  TEXT NOT NULL
);
