-- Migration: v9.5-style ETH 5m LGB probability column
-- Date: 2026-05-22
-- Purpose: Persist the v9.5 ETH-trained LGB head's raw probability so the
--          new GHOST strategy `v9_5_eth_raw_lgb` and downstream SQL analysis
--          can read it from signal_evaluations / window_snapshots like the
--          v9.2 / v9.2-eth / v9.2-post-iso columns. Companion timesfm-service
--          PR (sibling agent, feat/v9_5_eth_emission) emits the value on
--          /v4/snapshot.timescales.5m.probability_lgb_v9_5_eth once
--          V9_5_ETH_ENABLED=true; the column is NULL on every existing row
--          and on every snapshot when the flag is off, so this migration is
--          safe to apply ahead of the model PR landing.
--
-- Background:
--   - RDS notes #579: dedup analysis (intermediate operating-point sweep).
--   - RDS notes #584: final operating point — UP=0.96, DOWN=0.04,
--     eval_offset [60, 240]. Projected ~91-92% per-window WR with ~36% fire
--     rate on walk-forward OOF data. About 3-4x more fires per day than
--     v9.4 LIVE at matched selectivity.
--   - v9.5 architecture: 22-day corpus, walk-forward CV (5x4d), new feature
--     sources (Polymarket gamma at 5m, Binance depth book, Polymarket CLOB
--     pre-event aggregates).
--   - PR feat/v9_5_eth_strategy (this branch) — engine wiring + strategy.
--   - Companion timesfm PR: feat/v9_5_eth_emission (sibling agent).
--   - Engine precedent: feat/v9_2_eth_raw_lgb_ghost (landed 2026-05-20).
--
-- Architectural mirror of migrations/add_probability_lgb_v9_2_eth.sql
-- (2026-05-20). The v9.5 ETH column is a peer field — independent of the
-- v9.2 / v9.2-eth / v9.2-post-iso columns, which remain unchanged. The new
-- GHOST strategy reads this column ONLY for asset=ETH.
--
-- Safe to run multiple times: ADD COLUMN IF NOT EXISTS on every ALTER,
-- CREATE INDEX IF NOT EXISTS on every index.
--
-- Default NULL — existing strategies are unaffected. The probability is
-- only emitted by timesfm-service when V9_5_ETH_ENABLED=true.

-- ── signal_evaluations — ETH v9.5-style raw probability ──────────────────
ALTER TABLE signal_evaluations
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_5_eth DOUBLE PRECISION;

-- ── window_snapshots — ETH v9.5-style raw probability for comparison ─────
ALTER TABLE window_snapshots
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_5_eth DOUBLE PRECISION;

-- ── Partial indexes — sparse during rollout; partial indexes keep overhead ─
-- Indexed by (probability, outcome) to support WR-vs-threshold queries.
-- Mirrors the idx_se_v9_2_eth_outcome / idx_ws_v9_2_eth_outcome pattern
-- from add_probability_lgb_v9_2_eth.sql.
CREATE INDEX IF NOT EXISTS idx_se_v9_5_eth_outcome
  ON signal_evaluations(probability_lgb_v9_5_eth, outcome)
  WHERE probability_lgb_v9_5_eth IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ws_v9_5_eth_outcome
  ON window_snapshots(probability_lgb_v9_5_eth, outcome)
  WHERE probability_lgb_v9_5_eth IS NOT NULL;

-- ── Verify ─────────────────────────────────────────────────────────────────
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_name IN ('signal_evaluations', 'window_snapshots')
  AND column_name IN (
    'probability_lgb_v9_2_eth',
    'probability_lgb_v9_5_eth'
  )
ORDER BY table_name, column_name;
