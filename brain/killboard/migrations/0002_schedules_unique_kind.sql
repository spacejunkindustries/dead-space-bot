-- Enforce one schedule row per kind (daily | weekly | monthly).
--
-- Single-row-per-kind was previously guaranteed only by _upsert_schedule's
-- DELETE-then-INSERT, which is two separate committed statements: two concurrent
-- `/killboard schedule-add` calls for the same kind could interleave and leave
-- two rows, double-posting that ranking every period. This adds the UNIQUE index
-- that lets the upsert become a single atomic `INSERT ... ON CONFLICT(kind)`.

-- De-duplicate any rows an earlier race already created, keeping the newest
-- (highest id) per kind, so the unique index can be built.
DELETE FROM schedules
WHERE id NOT IN (SELECT MAX(id) FROM schedules GROUP BY kind);

CREATE UNIQUE INDEX ix_schedules_kind ON schedules (kind);
