-- Migration: BTC PURE LGB probability columns (v9.2, v9.3, v12 — no blend)
-- Date: 2026-05-24
-- Purpose: Persist the PURE (un-blended, post-iso-cal LGB) probabilities for
--          the THREE in-scope BTC heads — v9.2, v9.3, v12 — so the new
--          GHOST strategies in this PR can read them from signal_evaluations
--          / window_snapshots alongside the existing blended columns.
--
--          Sibling of migrations/add_probability_lgb_v9_5_eth_pure.sql
--          (ETH variant). Companion timesfm-service PR
--          feat/v9_3_btc_pure_lgb_emission (#163) emits the three PURE
--          values on /v4/snapshot.timescales.5m gated by per-model env
--          flags (all default OFF):
--            V9_3_BTC_PURE_ENABLED → probability_lgb_v9_3_btc_pure
--            V9_2_PURE_ENABLED     → probability_lgb_v9_2_pure
--            V12_PURE_ENABLED      → probability_lgb_v12_pure
--          Each column is NULL on every existing row and on every snapshot
--          until the matching env flag is flipped, so this migration is
--          safe to apply ahead of the timesfm-side flip.
--
-- Background — why three separate columns:
--   The existing blended columns (probability_lgb_v9_2 /
--   probability_lgb_v9_3_btc / probability_lgb_v12) are populated by the
--   LIVE app/v2_scorer.py:blend_ensemble path which is a 50/50 mix of the
--   LGB head + the TimesFM HF classifier output. The classifier saturates
--   ~0.84, capping every well-behaved BTC LGB at per-model ceilings
--   confirmed by walk-forward CV (/tmp/btc_walkforward_results.md):
--
--     Model | Pure max | Blend max | 90% WR fires (PURE vs BLEND)
--     v9.2  | 0.956    | 0.826     |  452 vs 285
--     v9.3  | 1.000    | 0.916     | 1725 vs 788   ← best stand-alone
--     v12   | 0.921    | 0.805     |  383 vs 128
--
--   The PURE columns persist the un-blended LGB+iso output so strategies
--   can trade the model's own calibration directly. The v9_2 + v12 AND
--   combo on PURE values: 281 UP fires @ 96.4% WR vs 4 on blend.
--
--   Keeping the columns SEPARATE means the existing LIVE strategies
--   (v9_2_v12_combo LIVE, v9_2_raw_lgb LIVE, v9_2_super_lgb_only GHOST,
--   v12_lgb_combo GHOST, v9_3_btc_raw_lgb GHOST, v9_3_btc_tight GHOST)
--   keep reading the blend columns unchanged. The two NEW GHOST
--   strategies in this PR (v9_3_btc_pure_lgb, v9_2_v12_combo_pure) read
--   the new columns explicitly.
--
-- Architectural mirror of migrations/add_probability_lgb_v9_5_eth_pure.sql
-- (2026-05-24, ETH variant). The three PURE columns are peer fields —
-- independent of every existing blend column.
--
-- Safe to run multiple times: ADD COLUMN IF NOT EXISTS on every ALTER,
-- CREATE INDEX IF NOT EXISTS on every index.
--
-- Default NULL — existing strategies are unaffected. The probabilities
-- are only emitted by timesfm-service when the matching per-model PURE
-- env flag is on.
--
-- References:
--   - RDS notes #618, #631, #632 — blend bug discovery (LGB+classifier
--     blend caps probabilities at per-model ceilings due to classifier
--     saturation at ~0.84).
--   - Walk-forward CV results: /tmp/btc_walkforward_results.md
--   - Sibling timesfm PR: feat/v9_3_btc_pure_lgb_emission (#163).
--   - Engine precedent: migrations/add_probability_lgb_v9_5_eth_pure.sql.

-- ── signal_evaluations — BTC v9.3 PURE LGB+iso probability ───────────────
ALTER TABLE signal_evaluations
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_3_btc_pure DOUBLE PRECISION;

-- ── signal_evaluations — BTC v9.2 PURE LGB+iso probability ───────────────
ALTER TABLE signal_evaluations
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_2_pure DOUBLE PRECISION;

-- ── signal_evaluations — BTC v12 PURE LGB+iso probability ────────────────
ALTER TABLE signal_evaluations
  ADD COLUMN IF NOT EXISTS probability_lgb_v12_pure DOUBLE PRECISION;

-- ── window_snapshots — same three PURE columns for comparison ────────────
ALTER TABLE window_snapshots
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_3_btc_pure DOUBLE PRECISION;
ALTER TABLE window_snapshots
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_2_pure DOUBLE PRECISION;
ALTER TABLE window_snapshots
  ADD COLUMN IF NOT EXISTS probability_lgb_v12_pure DOUBLE PRECISION;

-- ── Partial indexes — sparse during rollout; partial indexes keep overhead ─
-- Indexed by (probability, outcome) to support WR-vs-threshold queries.
-- Mirrors the idx_se_v9_3_btc_outcome / idx_ws_v9_3_btc_outcome pattern.
CREATE INDEX IF NOT EXISTS idx_se_v9_3_btc_pure_outcome
  ON signal_evaluations(probability_lgb_v9_3_btc_pure, outcome)
  WHERE probability_lgb_v9_3_btc_pure IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ws_v9_3_btc_pure_outcome
  ON window_snapshots(probability_lgb_v9_3_btc_pure, outcome)
  WHERE probability_lgb_v9_3_btc_pure IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_se_v9_2_pure_outcome
  ON signal_evaluations(probability_lgb_v9_2_pure, outcome)
  WHERE probability_lgb_v9_2_pure IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ws_v9_2_pure_outcome
  ON window_snapshots(probability_lgb_v9_2_pure, outcome)
  WHERE probability_lgb_v9_2_pure IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_se_v12_pure_outcome
  ON signal_evaluations(probability_lgb_v12_pure, outcome)
  WHERE probability_lgb_v12_pure IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ws_v12_pure_outcome
  ON window_snapshots(probability_lgb_v12_pure, outcome)
  WHERE probability_lgb_v12_pure IS NOT NULL;

-- ── Verify ─────────────────────────────────────────────────────────────────
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_name IN ('signal_evaluations', 'window_snapshots')
  AND column_name IN (
    'probability_lgb_v9_3_btc',
    'probability_lgb_v9_3_btc_pure',
    'probability_lgb_v9_2',
    'probability_lgb_v9_2_pure',
    'probability_lgb_v12',
    'probability_lgb_v12_pure'
  )
ORDER BY table_name, column_name;
