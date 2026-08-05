-- Stage 12 (Implementation_Plan §15): the Decisions review page's third
-- button (UI-UX-Brief §3.3 / App-Flow §9's "DEFER -> request more research").
--
-- `promotions.human_decision`'s CHECK only admits 'approved' | 'rejected' |
-- 'pending' — a deferred promotion has no honest value to write there
-- without either lying ("rejected", which it isn't) or leaving it "pending"
-- forever (which would leave it stuck in `pending_human_decision`'s queue).
-- One nullable timestamp column, checked with `IS NULL`/`IS NOT NULL` the
-- same way `superseded_by` already marks a revised `knowledge_entries` row,
-- is a portable one-line ALTER rather than a CHECK rewrite or table rebuild
-- — both SQLite and PostgreSQL accept `ADD COLUMN` unchanged (TRD §20).
--
-- `strategies.status` is deliberately untouched by a defer, mirroring A4's
-- own defer path (`handlers/promote.py`): the idea re-enters research via a
-- fresh `research_goals` row (`gates.defer`), not by resuming this
-- strategy's own loop.

ALTER TABLE promotions ADD COLUMN deferred_at TEXT;
