-- Migration: v9.5-style XRP 5m LGB probability column
-- Date: 2026-05-23
-- Purpose: Persist the v9.5 XRP-trained LGB head's raw probability so the
--          new GHOST strategies `v9_5_xrp_raw_lgb` and `v9_5_xrp_tight` and
--          downstream SQL analysis can read it from signal_evaluations /
--          window_snapshots like the v9.2 / v9.2-eth / v9.3-btc columns.
--          Companion timesfm-service PR #160 (merged 2026-05-23) emits the
--          value on /v4/snapshot.timescales.5m.probability_lgb_v9_5_xrp
--          once V9_5_XRP_ENABLED=true; the column is NULL on every
--          existing row and on every snapshot when the flag is off, so
--          this migration is safe to apply at any time.
--
-- Background:
--   - Timesfm-repo PR #160 + RDS note #593: training + walk-forward CV results.
--   - 8.62-day full corpus (1505 windows, 928 OOF), Optuna-tuned (40 trials),
--     67-feature schema (9 features NaN-filled on XRP serve path pending a
--     loader-side follow-up analogous to PR #158 for ETH).
--   - Two operating points consume this column:
--       * v9_5_xrp_raw_lgb (drop-in moderate operating point):
--         UP p>=0.82 -> 74.8% WR (476 windows),
--         DOWN p<=0.20 -> 71.9% WR (551 windows).
--       * v9_5_xrp_tight (high-precision corner):
--         UP p>=0.95 -> 80.9% WR (282 windows),
--         DOWN p<=0.05 -> 83.1% WR (319 windows).
--         Constrained to late-window band (eval_offset 120-240, where
--         tight-corner fires concentrate).
--
--   - XRP corpus is thinner than v9.5 ETH (22d) and v9.3 BTC (24d) so the
--     90%+ tight-corner pockets are NOT reachable on UP side. UP ceiling
--     is ~83% WR at thr=0.97 (n=263); DOWN reaches 93% only at thr<=0.005
--     (n=241), too close to a degenerate always-fire-DOWN point.
--   - Strategy design doc: /tmp/v9_5_xrp_strategy_design.md
--   - PR feat/v9_5_xrp_strategies (this branch) — engine wiring + strategies.
--   - Engine precedent: feat/v9_3_btc_strategies (commit 5e8c147) —
--     mirrored file-for-file.
--
-- Architectural mirror of migrations/add_probability_lgb_v9_3_btc_column.sql
-- (2026-05-22) and add_probability_lgb_v9_5_eth.sql. The v9.5 XRP column is
-- a peer field — independent of the v9.2 / v9.2-eth / v9.3-btc / v9.2-xrp
-- columns, which remain unchanged. Both new GHOST strategies read this
-- column ONLY for asset=XRP.
--
-- Safe to run multiple times: ADD COLUMN IF NOT EXISTS on every ALTER,
-- CREATE INDEX IF NOT EXISTS on every index.
--
-- Default NULL — existing strategies are unaffected. The probability is
-- only emitted by timesfm-service when V9_5_XRP_ENABLED=true.

-- ── signal_evaluations — XRP v9.5-style raw probability ──────────────────
ALTER TABLE signal_evaluations
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_5_xrp DOUBLE PRECISION;

-- ── window_snapshots — XRP v9.5-style raw probability for comparison ─────
ALTER TABLE window_snapshots
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_5_xrp DOUBLE PRECISION;

-- ── Partial indexes — sparse during rollout; partial indexes keep overhead ─
-- Indexed by (probability, outcome) to support WR-vs-threshold queries.
-- Mirrors the idx_se_v9_3_btc_outcome / idx_ws_v9_3_btc_outcome pattern
-- from add_probability_lgb_v9_3_btc_column.sql.
CREATE INDEX IF NOT EXISTS idx_se_v9_5_xrp_outcome
  ON signal_evaluations(probability_lgb_v9_5_xrp, outcome)
  WHERE probability_lgb_v9_5_xrp IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ws_v9_5_xrp_outcome
  ON window_snapshots(probability_lgb_v9_5_xrp, outcome)
  WHERE probability_lgb_v9_5_xrp IS NOT NULL;

-- ── Verify ─────────────────────────────────────────────────────────────────
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_name IN ('signal_evaluations', 'window_snapshots')
  AND column_name IN (
    'probability_lgb_v9_2_xrp',
    'probability_lgb_v9_5_xrp'
  )
ORDER BY table_name, column_name;
