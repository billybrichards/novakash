-- 20260520_01_trades_sub_fill_columns.sql
--
-- Layer 1 of the risk/exposure caps + sub-fill writer fix
-- (Hub note #554, branch fix/risk-exposure-caps-and-sub-fill-writer).
--
-- Problem
-- -------
-- A strategy can fire two CLOB orders inside the 25s STALE_PLACEHOLDER_TTL
-- window (mark_traded → SECONDARY_FILL race). Both fills happen on
-- Polymarket — but the existing UNIQUE(asset, window_ts, timeframe,
-- strategy_id) on strategy_window_fills only lets one trades-row land
-- with a real order_id. The second on-chain fill is real money on the
-- wallet that never gets a trades row, so DB pnl + dashboards see only
-- ONE fill while the wallet sees TWO. Smoking-gun evidence: 25-second
-- gap between 16:07:59 and 16:08:24 fills on window 1779293100.
--
-- Fix
-- ---
-- Add two columns to `trades` so the secondary fill can ALWAYS land:
--
--   is_secondary_fill BOOLEAN  -- true on rows recorded because the
--                                window_states row already had a real
--                                (non-'pending') order_id when this
--                                fill confirmed.
--   parent_trade_id   TEXT     -- the order_id of the first fill in
--                                the same (asset, window_ts, timeframe,
--                                strategy_id) bucket. NULL on the
--                                primary fill, set on the secondary
--                                fill. Lets us aggregate the pair when
--                                reporting effective stake / PnL.
--
-- Both columns are nullable / default false so existing rows are not
-- touched.  Existing UNIQUE constraint on trades.order_id is preserved
-- because each fill comes back from CLOB with its own order_id.
--
-- Idempotent — both ADD COLUMN statements are guarded by IF NOT EXISTS.
--
-- Applied by: Alembic / direct psql on RDS after PR merge.
-- DO NOT run in this PR — Billy decides when to apply.

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS is_secondary_fill BOOLEAN DEFAULT FALSE;

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS parent_trade_id TEXT;

COMMENT ON COLUMN trades.is_secondary_fill IS
    'TRUE for secondary fills produced when mark_traded detected an existing real order_id (SECONDARY_FILL). FALSE on the primary fill and on rows from before this migration.';

COMMENT ON COLUMN trades.parent_trade_id IS
    'order_id of the primary fill when this row is_secondary_fill=TRUE. NULL on primary fills and pre-migration rows.';

CREATE INDEX IF NOT EXISTS idx_trades_parent_trade_id
    ON trades(parent_trade_id)
    WHERE parent_trade_id IS NOT NULL;
