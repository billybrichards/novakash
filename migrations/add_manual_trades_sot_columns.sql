-- Migration: POLY-SOT — Polymarket CLOB source-of-truth columns for manual_trades
-- Date: 2026-04-11
-- Purpose: Make Polymarket CLOB the authoritative record for every operator
--          click on the Live Trade button. Mirrors the pattern margin_engine
--          uses with Binance/Hyperliquid where the exchange API is the SOT.
--
-- Columns added (all NULL by default — backfilled by the SOT reconciler loop):
--   polymarket_order_id              The CLOB order ID returned by the SDK
--   polymarket_confirmed_status      pending | matched | filled | cancelled | rejected
--   polymarket_confirmed_fill_price  Actual fill price reported by the CLOB
--   polymarket_confirmed_size        Actual filled size reported by the CLOB
--   polymarket_confirmed_at          Timestamp of the terminal status read
--   polymarket_last_verified_at      Last time the SOT reconciler queried Polymarket
--   sot_reconciliation_state         unreconciled | agrees | engine_optimistic | polymarket_only | diverged
--   sot_reconciliation_notes         Human-readable explanation of any divergence
--
-- Idempotent — re-running the migration is a no-op.
-- The existing `status` column remains the engine's local "what we think happened"
-- record; the new polymarket_confirmed_* columns are the authoritative record.

ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS polymarket_order_id TEXT;
ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS polymarket_confirmed_status TEXT;
ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS polymarket_confirmed_fill_price NUMERIC(18,6);
ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS polymarket_confirmed_size NUMERIC(18,6);
ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS polymarket_confirmed_at TIMESTAMPTZ;
ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS polymarket_last_verified_at TIMESTAMPTZ;
ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS sot_reconciliation_state TEXT;
ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS sot_reconciliation_notes TEXT;

-- Index on polymarket_order_id for fast reconciler lookups
CREATE INDEX IF NOT EXISTS idx_manual_trades_polymarket_order_id
    ON manual_trades(polymarket_order_id)
    WHERE polymarket_order_id IS NOT NULL;

-- Index on sot_reconciliation_state to make the dashboard query cheap
CREATE INDEX IF NOT EXISTS idx_manual_trades_sot_state
    ON manual_trades(sot_reconciliation_state)
    WHERE sot_reconciliation_state IS NOT NULL;

-- Verify
SELECT column_name FROM information_schema.columns
WHERE table_name = 'manual_trades'
  AND column_name IN (
      'polymarket_order_id',
      'polymarket_confirmed_status',
      'polymarket_confirmed_fill_price',
      'polymarket_confirmed_size',
      'polymarket_confirmed_at',
      'polymarket_last_verified_at',
      'sot_reconciliation_state',
      'sot_reconciliation_notes'
  )
ORDER BY column_name;
