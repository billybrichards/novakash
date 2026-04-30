-- Migration: probability_lgb_v12 columns for v12 LGB shadow scoring
-- Date: 2026-04-30
-- Purpose: Persist v12 LGB probability alongside existing v9/v10 signals so
--          strategies and analytics can compare ensembles without disrupting
--          live behavior.
-- Per hub note #295 (v12 feature provenance) and engine PR #431.
-- Safe to run multiple times (ADD COLUMN IF NOT EXISTS, CREATE INDEX IF NOT EXISTS).
--
-- Default NULL — existing strategies (v9_lgb_only / v10_lgb_only / v4_fusion)
-- ignore null fields, no behavior change.

-- ── signal_evaluations ─────────────────────────────────────────────────────
ALTER TABLE signal_evaluations
  ADD COLUMN IF NOT EXISTS probability_lgb_v12 DOUBLE PRECISION;

-- ── window_snapshots ───────────────────────────────────────────────────────
ALTER TABLE window_snapshots
  ADD COLUMN IF NOT EXISTS probability_lgb_v12 DOUBLE PRECISION;

-- ── Partial indexes for shadow-strategy comparison queries ─────────────────
-- Only index rows where v12 is populated (sparse during shadow rollout).
CREATE INDEX IF NOT EXISTS idx_se_v12_outcome
  ON signal_evaluations(probability_lgb_v12, outcome)
  WHERE probability_lgb_v12 IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ws_v12_outcome
  ON window_snapshots(probability_lgb_v12, outcome)
  WHERE probability_lgb_v12 IS NOT NULL;

-- Note on derived comparison fields (e.g. ensemble_disagreement_v9_v12):
-- computed on-the-fly via the v_signal_comparison view rather than stored,
-- to keep this migration minimal. See views/v_signal_comparison.sql.

-- ── Verify ─────────────────────────────────────────────────────────────────
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_name IN ('signal_evaluations','window_snapshots')
  AND column_name = 'probability_lgb_v12'
ORDER BY table_name;
