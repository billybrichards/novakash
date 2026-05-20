-- Migration: v9.2-style ETH 5m LGB probability column
-- Date: 2026-05-20
-- Purpose: Persist the ETH-trained v9.2-style LGB head's raw probability so the
--          new GHOST strategy `v9_2_eth_raw_lgb` and downstream SQL analysis can
--          read it from signal_evaluations / window_snapshots like the BTC v9.2
--          columns. Companion timesfm-service PR (bg-agent-1) emits the value
--          on /v4/snapshot.timescales.5m.probability_lgb_v9_2_eth once
--          V9_2_ETH_ENABLED=true; the column is NULL on every existing row
--          and on every snapshot when the flag is off, so this migration is
--          safe to apply ahead of the model PR landing.
--
-- Background:
--   - Hub note #545: data inventory + ETH/XRP labelled signal availability.
--   - Hub note #547: training-pipeline spec for the ETH/XRP v1 heads.
--   - Hub note #550: ETH/XRP v1 training results, corrected WR-at-threshold
--     tables. Recommended operating point UP=0.85, DOWN=0.20, eval_offset
--     60-150 (drops weak δ=180). Projected WR ~92-94% on test.
--   - PR feat/v9_2_eth_raw_lgb_ghost (this branch) — engine wiring + strategy.
--   - Companion timesfm PR: bg-agent-1 v9.2 ETH/XRP training pipeline.
--
-- Architectural mirror of migrations/add_probability_lgb_v9_2_post_iso.sql
-- (2026-05-18). The ETH column is a peer field — independent of the BTC
-- probability_lgb_v9_2 / probability_lgb_v9_2_post_iso columns, which remain
-- unchanged. The new GHOST strategy reads this column ONLY for asset=ETH.
--
-- Safe to run multiple times: ADD COLUMN IF NOT EXISTS on every ALTER,
-- CREATE INDEX IF NOT EXISTS on every index.
--
-- Default NULL — existing strategies are unaffected. The probability is
-- only emitted by timesfm-service when V9_2_ETH_ENABLED=true.

-- ── signal_evaluations — ETH v9.2-style raw probability ──────────────────
ALTER TABLE signal_evaluations
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_2_eth DOUBLE PRECISION;

-- ── window_snapshots — ETH v9.2-style raw probability for comparison ─────
ALTER TABLE window_snapshots
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_2_eth DOUBLE PRECISION;

-- ── Partial indexes — sparse during rollout; partial indexes keep overhead ─
-- Indexed by (probability, outcome) to support WR-vs-threshold queries.
-- Mirrors the idx_se_v9_2_outcome / idx_ws_v9_2_outcome pattern from
-- add_probability_lgb_v9_2.sql.
CREATE INDEX IF NOT EXISTS idx_se_v9_2_eth_outcome
  ON signal_evaluations(probability_lgb_v9_2_eth, outcome)
  WHERE probability_lgb_v9_2_eth IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ws_v9_2_eth_outcome
  ON window_snapshots(probability_lgb_v9_2_eth, outcome)
  WHERE probability_lgb_v9_2_eth IS NOT NULL;

-- ── Verify ─────────────────────────────────────────────────────────────────
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_name IN ('signal_evaluations', 'window_snapshots')
  AND column_name IN (
    'probability_lgb_v9_2',
    'probability_lgb_v9_2_post_iso',
    'probability_lgb_v9_2_eth'
  )
ORDER BY table_name, column_name;
