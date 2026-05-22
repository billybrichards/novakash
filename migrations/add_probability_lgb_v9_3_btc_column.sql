-- Migration: v9.3-style BTC 5m LGB probability column
-- Date: 2026-05-22
-- Purpose: Persist the v9.3 BTC-trained LGB head's raw probability so the
--          new GHOST strategies `v9_3_btc_raw_lgb` and `v9_3_btc_tight` and
--          downstream SQL analysis can read it from signal_evaluations /
--          window_snapshots like the v9.2 / v9.2-eth / v9.2-post-iso columns.
--          Companion timesfm-service PR (not yet opened — Billy approves
--          before that's created) will emit the value on
--          /v4/snapshot.timescales.5m.probability_lgb_v9_3_btc once
--          V9_3_BTC_ENABLED=true; the column is NULL on every existing row
--          and on every snapshot when the flag is off, so this migration is
--          safe to apply ahead of the model PR landing.
--
-- Background:
--   - Timesfm-repo notes #585, #587, #589, #590: walk-forward CV results.
--   - 24-day backtest on BTC corpus, walk-forward CV.
--   - Two operating points consume this column:
--       * v9_3_btc_raw_lgb (drop-in replacement for v9_2_raw_lgb):
--         UP p>=0.72 -> 79.9% WR (2419 windows),
--         DOWN p<=0.20 -> 82.8% WR (2320 windows).
--         +10.2pp UP / +5.6pp DOWN vs v9.2 BTC LIVE baseline.
--       * v9_3_btc_tight (high-precision corner):
--         UP p>=0.935 -> 90.3% WR (1725 windows),
--         DOWN p<=0.065 -> 90.4% WR (1590 windows).
--         Constrained to early-window band (stc 130-280s -> eval_offset 20-170).
--   - BTC ebook chapter: /home/billyrichards/scans2025/v9_3_btc_ebook_chapter.html
--   - PR feat/v9_3_btc_strategies (this branch) — engine wiring + strategies.
--   - Companion timesfm PR: NOT YET OPENED — Billy approves before scorer
--     PR is created.
--   - Engine precedent: feat/v9_5_eth_strategy (PR pending) — mirrored
--     file-for-file from commit 9d7a9c0.
--
-- Architectural mirror of migrations/add_probability_lgb_v9_2_eth.sql
-- (2026-05-20) and the v9.5 ETH analog. The v9.3 BTC column is a peer field
-- — independent of the v9.2 / v9.2-eth / v9.2-post-iso columns, which
-- remain unchanged. Both new GHOST strategies read this column ONLY for
-- asset=BTC.
--
-- Safe to run multiple times: ADD COLUMN IF NOT EXISTS on every ALTER,
-- CREATE INDEX IF NOT EXISTS on every index.
--
-- Default NULL — existing strategies are unaffected. The probability is
-- only emitted by timesfm-service when V9_3_BTC_ENABLED=true.

-- ── signal_evaluations — BTC v9.3-style raw probability ──────────────────
ALTER TABLE signal_evaluations
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_3_btc DOUBLE PRECISION;

-- ── window_snapshots — BTC v9.3-style raw probability for comparison ─────
ALTER TABLE window_snapshots
  ADD COLUMN IF NOT EXISTS probability_lgb_v9_3_btc DOUBLE PRECISION;

-- ── Partial indexes — sparse during rollout; partial indexes keep overhead ─
-- Indexed by (probability, outcome) to support WR-vs-threshold queries.
-- Mirrors the idx_se_v9_2_eth_outcome / idx_ws_v9_2_eth_outcome pattern
-- from add_probability_lgb_v9_2_eth.sql.
CREATE INDEX IF NOT EXISTS idx_se_v9_3_btc_outcome
  ON signal_evaluations(probability_lgb_v9_3_btc, outcome)
  WHERE probability_lgb_v9_3_btc IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ws_v9_3_btc_outcome
  ON window_snapshots(probability_lgb_v9_3_btc, outcome)
  WHERE probability_lgb_v9_3_btc IS NOT NULL;

-- ── Verify ─────────────────────────────────────────────────────────────────
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_name IN ('signal_evaluations', 'window_snapshots')
  AND column_name IN (
    'probability_lgb_v9_2',
    'probability_lgb_v9_3_btc'
  )
ORDER BY table_name, column_name;
