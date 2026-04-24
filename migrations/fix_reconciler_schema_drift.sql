-- One-shot migration: add two columns the engine writes but the live
-- Railway DB has never had.
--
-- Two schema-drift warnings spam engine logs ~2x/min:
--   1. reconciler manual_trades fetch_joined SQL references
--      manual_trades.market_slug, which was added in the orchestrator /
--      manual_trade_poller path but never landed in a migration.
--   2. window surface update path writes window_snapshots.regime_persistence
--      (added alongside the cedar regime classifier work); column missing
--      on prod, so the UPDATE silently fails and the warning fires.
--
-- This SQL is pure ADD-COLUMN-IF-NOT-EXISTS, so:
--   * safe to run on boxes where the columns already exist (no-op)
--   * safe to re-run (idempotent)
--   * no data is modified or deleted
--
-- Companion code in this PR fixes the v8_champion redemption write-back
-- (the primary reason for the PR); this migration is a bonus cleanup so
-- the logs stop spewing schema-drift noise that masks real errors.

ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS market_slug TEXT;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS regime_persistence FLOAT;
