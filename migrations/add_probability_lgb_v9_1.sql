-- Migration: probability_lgb_v9_1 column for v9.1 LGB shadow scoring
-- Date: 2026-05-14
-- Purpose: Persist v9.1 LGB probability on signal_evaluations so v9_1_lgb_only
--          ghost performance can be analysed the same way as v9_2/v12.
-- Per hub note #313 (v9.1 retrain provenance).
-- Safe to run multiple times (ADD COLUMN IF NOT EXISTS, CREATE INDEX IF NOT EXISTS).
--
-- Default NULL — existing strategies ignore null fields, no behavior change.

-- ── signal_evaluations ─────────────────────────────────────────────────────
ALTER TABLE signal_evaluations
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_1 DOUBLE PRECISION;

-- ── Partial index for shadow-strategy comparison queries ───────────────────
-- Only index rows where v9_1 is populated (sparse during ghost rollout).
CREATE INDEX IF NOT EXISTS idx_se_v9_1_outcome
  ON signal_evaluations(probability_lgb_v9_1, outcome)
  WHERE probability_lgb_v9_1 IS NOT NULL;

-- ── Verify ─────────────────────────────────────────────────────────────────
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_name = 'signal_evaluations'
  AND column_name = 'probability_lgb_v9_1';
