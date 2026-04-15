-- One-shot migration: restore the 5 columns that the redeemer loop expects
-- on playwright_state.  The columns are already declared in
-- pg_system_repo.py::ensure_playwright_tables() via
-- "ALTER TABLE playwright_state ADD COLUMN IF NOT EXISTS ..." but that
-- routine was only invoked when the Playwright browser automation path
-- was wired on.  Montreal runs in live-redeem-only mode (no browser) so
-- the columns never existed on disk and every redeemer state write
-- errored with "column X does not exist".
--
-- This SQL is pure ADD-COLUMN-IF-NOT-EXISTS, so:
--   * safe to run on boxes where the columns already exist (no-op)
--   * safe to re-run (idempotent)
--   * no data is modified or deleted
--
-- Companion code fix (same PR) moves ensure_playwright_tables() up so
-- it runs whenever the redeemer starts, preventing regression on any
-- freshly-provisioned PG instance.
--
-- Application on Montreal — already done manually via asyncpg at
-- 2026-04-15 ~20:40 UTC — this file is committed for audit history
-- and for any future fresh DBs.
--
-- Ref: Hub master performance note #37, engine.log at
-- 2026-04-15T20:15:40Z ("db.playwright_state.error: column
-- \"quota_used_today\" does not exist").

ALTER TABLE playwright_state ADD COLUMN IF NOT EXISTS redeem_request_type TEXT DEFAULT 'all';
ALTER TABLE playwright_state ADD COLUMN IF NOT EXISTS quota_used_today INTEGER DEFAULT 0;
ALTER TABLE playwright_state ADD COLUMN IF NOT EXISTS quota_limit INTEGER DEFAULT 100;
ALTER TABLE playwright_state ADD COLUMN IF NOT EXISTS cooldown_until TIMESTAMPTZ;
ALTER TABLE playwright_state ADD COLUMN IF NOT EXISTS cooldown_reason TEXT;
