-- Migration: v9.2 persistence columns for shadow evaluation + future training
-- Date: 2026-05-07
-- Purpose: Persist v9.2-optuna LGB probability alongside existing v9/v9.1/v12
--          signals so the canary can be evaluated and its data reused for
--          the next training cycle.
--
-- Persistence approach: (a) signal_evaluations columns — chosen because
-- signal_evaluations is the canonical training input per hub note #353.
-- Adding cohort metadata here means the training pipeline can directly
-- use the gate data without an extra JOIN (avoids the sidecar writer
-- regression pattern flagged in hub note #366).
--
-- Per hub note #356 (PR-B handover), hub note #353 (4 training pipeline
-- bugs postmortem), hub note #366 (engine action plan for Schema A→B
-- regression), and engine PR "feat(strategies): v9_2_super_lgb_only canary".
-- Safe to run multiple times (ADD COLUMN IF NOT EXISTS, CREATE INDEX IF NOT EXISTS).
--
-- Default NULL — existing strategies ignore null fields; no behavior change.

-- ── signal_evaluations — v9.2 persistence columns ─────────────────────────
ALTER TABLE signal_evaluations
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_2 DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS v9_2_conviction       DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS v9_2_pred_direction   TEXT,
  ADD COLUMN IF NOT EXISTS v9_2_cohort           TEXT,    -- e.g. 'TRANSITION_UP'
  ADD COLUMN IF NOT EXISTS v9_2_gate_fired       BOOLEAN;

-- ── window_snapshots — v9.2 probability for quick comparison queries ───────
ALTER TABLE window_snapshots
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_2 DOUBLE PRECISION;

-- ── Partial indexes — only index rows where v9.2 is populated ─────────────
-- Sparse during canary rollout; partial indexes keep catalogue overhead tiny.
CREATE INDEX IF NOT EXISTS idx_se_v9_2_cohort_fired
  ON signal_evaluations(v9_2_cohort, v9_2_gate_fired, outcome)
  WHERE probability_lgb_v9_2 IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_se_v9_2_outcome
  ON signal_evaluations(probability_lgb_v9_2, outcome)
  WHERE probability_lgb_v9_2 IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ws_v9_2_outcome
  ON window_snapshots(probability_lgb_v9_2, outcome)
  WHERE probability_lgb_v9_2 IS NOT NULL;

-- ── Verify ─────────────────────────────────────────────────────────────────
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_name IN ('signal_evaluations', 'window_snapshots')
  AND column_name IN (
    'probability_lgb_v9_2',
    'v9_2_conviction',
    'v9_2_pred_direction',
    'v9_2_cohort',
    'v9_2_gate_fired'
  )
ORDER BY table_name, column_name;
