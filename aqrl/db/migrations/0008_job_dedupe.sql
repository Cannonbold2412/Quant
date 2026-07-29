-- Stage 4 (Implementation_Plan §6): the nervous system starts driving `jobs`.
--
-- `dedupe_key` makes an enqueue idempotent. Two producers of the same
-- follow-on job — a scheduler tick that overlaps a time-driven fire, or an
-- event handler re-run after a crash before its transaction committed — must
-- not enqueue the job twice. `JobRepository.enqueue` checks for an existing
-- row before inserting, and the UNIQUE index is what makes that check safe
-- under concurrency rather than merely optimistic: every call site wraps
-- `enqueue` in a `BEGIN IMMEDIATE` transaction (aqrl/db/connection.py), so
-- the existence check and the insert happen with the write lock already
-- held, and no second writer can interleave between them. NULL keys are
-- exempt (SQLite treats NULLs as distinct in a UNIQUE index), so ad-hoc jobs
-- with no natural dedupe identity are unaffected.

ALTER TABLE jobs ADD COLUMN dedupe_key TEXT;

CREATE UNIQUE INDEX idx_jobs_dedupe ON jobs (dedupe_key);
