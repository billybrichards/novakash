-- Migration: v9.2 post-hoc isotonic calibration column
-- Date: 2026-05-18
-- Purpose: Layer-2 post-hoc isotonic calibration for the v9.2 LGB signal.
--          probability_lgb_v9_2_post_iso is the output of a non-parametric
--          isotonic regression fit on the v9.2 raw probability. Fit on
--          11-day window; test ECE 0.1644 → 0.0216 (-87%); Brier -10.6%.
--          Details in hub note #536 (iso findings + architecture).
--          Companion timesfm PR: feat/v9_2_post_iso_layer.
--          Engine PR: feat/v9_2_post_iso_column_and_strategies.
--
-- Cross-reference: migrations/add_probability_lgb_v9_2.sql (2026-05-07)
-- That migration added the raw probability and cohort metadata columns.
-- This migration adds the calibrated column as a peer field — it is read
-- independently by the iso strategy variants and does NOT replace the raw
-- column (which remains active for v9_2_raw_lgb and v9_2_super_lgb_only).
--
-- Safe to run multiple times: ADD COLUMN IF NOT EXISTS on every ALTER,
-- CREATE INDEX IF NOT EXISTS on every index.
--
-- Default NULL — existing strategies are unaffected. The calibrated value
-- is only emitted by timesfm-service when V9_2_POST_ISO_ENABLED=true.

-- ── signal_evaluations — calibrated v9.2 iso probability ─────────────────
ALTER TABLE signal_evaluations
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_2_post_iso DOUBLE PRECISION;

-- ── window_snapshots — calibrated v9.2 iso probability for comparison ─────
ALTER TABLE window_snapshots
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_2_post_iso DOUBLE PRECISION;

-- ── Partial indexes — sparse during rollout; partial indexes keep overhead ─
-- Indexed by (calibrated_prob, outcome) to support WR-vs-threshold queries.
-- Mirrors the idx_se_v9_2_outcome / idx_ws_v9_2_outcome pattern from
-- add_probability_lgb_v9_2.sql.
CREATE INDEX IF NOT EXISTS idx_se_v9_2_post_iso_outcome
  ON signal_evaluations(probability_lgb_v9_2_post_iso, outcome)
  WHERE probability_lgb_v9_2_post_iso IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ws_v9_2_post_iso_outcome
  ON window_snapshots(probability_lgb_v9_2_post_iso, outcome)
  WHERE probability_lgb_v9_2_post_iso IS NOT NULL;

-- ── Verify ─────────────────────────────────────────────────────────────────
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_name IN ('signal_evaluations', 'window_snapshots')
  AND column_name IN (
    'probability_lgb_v9_2',
    'probability_lgb_v9_2_post_iso'
  )
ORDER BY table_name, column_name;
