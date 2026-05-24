-- Migration: v9.5 ETH 5m LGB pure-probability column (no blend)
-- Date: 2026-05-24
-- Purpose: Persist the v9.5 ETH LGB head's PURE probability (LGB raw → isotonic,
--          no TimesFM HF classifier blend) so the new GHOST strategy
--          `v9_5_eth_pure_lgb` can read it from signal_evaluations /
--          window_snapshots like the existing v9.2 / v9.5 ETH blend columns.
--          Companion timesfm-service PR (sibling agent) emits the value on
--          /v4/snapshot.timescales.5m.probability_lgb_v9_5_eth_pure once the
--          per-asset PURE emission flag is flipped on; the column is NULL on
--          every existing row and on every snapshot until then, so this
--          migration is safe to apply ahead of the timesfm-side flip.
--
-- Background — why a separate column:
--   - The existing `probability_lgb_v9_5_eth` column is populated by the
--     LIVE `app/v2_scorer.py:blend_ensemble` path which is a 50/50 mix of
--     the LGB head + the TimesFM HF classifier output. The classifier
--     saturates at ~0.84, capping the blended probability at ~0.92 (see
--     RDS notes #631, #632 — the "blend bug" discovery).
--   - The PURE column persists the un-blended LGB+iso output so we can
--     trade the model's own calibration directly. Walk-forward CV (5×4d):
--       UP   p ≥ 0.915 → 90.3% WR (Wilson LB 88.6%), ~83.1 fires/day
--       DOWN p ≤ 0.095 → 90.4% WR (Wilson LB 88.7%), ~88.5 fires/day
--     vs blend at the same WR fires ~10× less often (8-9 fires/day).
--   - Keeping the two columns SEPARATE means the existing blend strategies
--     (v9_5_eth_blend née v9_5_eth_raw_lgb, etc.) keep reading the blend
--     column unchanged. The new PURE strategy reads the new column.
--
-- Architectural mirror of migrations/add_probability_lgb_v9_5_eth.sql
-- (2026-05-22). PURE is a peer field — independent of v9.5 ETH (blend)
-- and the v9.2 / v9.3 BTC / v9.5 XRP columns.
--
-- Safe to run multiple times: ADD COLUMN IF NOT EXISTS on every ALTER,
-- CREATE INDEX IF NOT EXISTS on every index.
--
-- Default NULL — existing strategies are unaffected. The probability is
-- only emitted by timesfm-service when the per-asset PURE emission flag
-- (e.g. V9_5_ETH_PURE_ENABLED) is flipped on.
--
-- References:
--   - RDS notes #631, #632 — blend bug discovery (LGB+classifier blend caps
--     v9.5 probabilities at ~0.92 due to classifier saturation at ~0.84).
--   - Walk-forward CV results: /tmp/v9_5_eth_walkforward_results.md
--   - Sibling timesfm PR: feat/v9_5_eth_pure_emission (forthcoming).
--   - Engine precedent: migrations/add_probability_lgb_v9_5_eth.sql (2026-05-22).

-- ── signal_evaluations — ETH v9.5 PURE LGB+iso probability ───────────────
ALTER TABLE signal_evaluations
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_5_eth_pure DOUBLE PRECISION;

-- ── window_snapshots — ETH v9.5 PURE LGB+iso probability for comparison ──
ALTER TABLE window_snapshots
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_5_eth_pure DOUBLE PRECISION;

-- ── Partial indexes — sparse during rollout; partial indexes keep overhead ─
-- Indexed by (probability, outcome) to support WR-vs-threshold queries.
-- Mirrors the idx_se_v9_5_eth_outcome / idx_ws_v9_5_eth_outcome pattern.
CREATE INDEX IF NOT EXISTS idx_se_v9_5_eth_pure_outcome
  ON signal_evaluations(probability_lgb_v9_5_eth_pure, outcome)
  WHERE probability_lgb_v9_5_eth_pure IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ws_v9_5_eth_pure_outcome
  ON window_snapshots(probability_lgb_v9_5_eth_pure, outcome)
  WHERE probability_lgb_v9_5_eth_pure IS NOT NULL;

-- ── Verify ─────────────────────────────────────────────────────────────────
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_name IN ('signal_evaluations', 'window_snapshots')
  AND column_name IN (
    'probability_lgb_v9_5_eth',
    'probability_lgb_v9_5_eth_pure'
  )
ORDER BY table_name, column_name;
