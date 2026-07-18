-- Migration 0007 — incident durability (GDD §9.1 / §13).
--
-- Timers become at-least-once: `fired = 1` is now the CLAIM (this row was
-- picked up by the announcer) and `announced_at` records the actual delivery.
-- A claim whose announcement failed (403, Discord down, crash mid-announce)
-- keeps announced_at NULL and is re-offered on the next poll tick instead of
-- being silently eaten. Rows already fired under the old at-most-once scheme
-- are backfilled as announced so the upgrade never replays old timers.
ALTER TABLE timers ADD COLUMN announced_at TEXT;
UPDATE timers SET announced_at = fires_at WHERE fired = 1;

-- Uncertain-card durability: the MEDIUM-tier confirm candidates (system id,
-- display name, score triples, JSON) are persisted on the incident row so a
-- Brain restart re-arms the pick buttons instead of re-rendering a
-- 0.55-confidence guess as a confirmed card. Cleared whenever the incident
-- is confirmed, corrected, retargeted, resolved, or swept.
ALTER TABLE incidents ADD COLUMN pending_candidates TEXT;
