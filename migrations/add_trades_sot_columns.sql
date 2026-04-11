-- Migration: POLY-SOT-b — Polymarket CLOB source-of-truth columns for `trades`
-- Date: 2026-04-11
-- Purpose: Extend the source-of-truth pattern from POLY-SOT (PR #62) so that
--          automatic engine trades — not just operator manual trades — are
--          re-verified against Polymarket on every reconciliation pass.
--
-- Columns added (all NULL by default — backfilled forward by the SOT
-- reconciler loop, and historically by engine/scripts/backfill_sot_reconciliation.py):
--   polymarket_order_id              The CLOB order ID returned by the SDK.
--                                    `trades` already has a `clob_order_id`
--                                    column (from the v8 migration), but we
--                                    add this dedicated field so the SOT
--                                    reconciler can write back through the
--                                    same code path it uses for manual_trades
--                                    without depending on the v8 column.
--                                    The orchestrator's write path will
--                                    populate both for at least one release.
--   polymarket_confirmed_status      pending | matched | filled | cancelled | rejected
--   polymarket_confirmed_fill_price  Actual fill price reported by the CLOB
--   polymarket_confirmed_size        Actual filled size reported by the CLOB
--   polymarket_confirmed_at          Timestamp of the terminal status read
--   polymarket_last_verified_at      Last time the SOT reconciler queried Polymarket
--   sot_reconciliation_state         unreconciled | agrees | engine_optimistic | polymarket_only | diverged | no_order_id
--   sot_reconciliation_notes         Human-readable explanation of any divergence
--
-- Idempotent — re-running the migration is a no-op. Mirrors the
-- migrations/add_manual_trades_sot_columns.sql migration byte-for-byte
-- except for the table name.

ALTER TABLE trades ADD COLUMN IF NOT EXISTS polymarket_order_id TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS polymarket_confirmed_status TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS polymarket_confirmed_fill_price NUMERIC(18,6);
ALTER TABLE trades ADD COLUMN IF NOT EXISTS polymarket_confirmed_size NUMERIC(18,6);
ALTER TABLE trades ADD COLUMN IF NOT EXISTS polymarket_confirmed_at TIMESTAMPTZ;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS polymarket_last_verified_at TIMESTAMPTZ;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS sot_reconciliation_state TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS sot_reconciliation_notes TEXT;

-- Index on polymarket_order_id for fast reconciler lookups
CREATE INDEX IF NOT EXISTS idx_trades_polymarket_order_id
    ON trades(polymarket_order_id)
    WHERE polymarket_order_id IS NOT NULL;

-- Index on sot_reconciliation_state to make the dashboard query cheap
CREATE INDEX IF NOT EXISTS idx_trades_sot_state
    ON trades(sot_reconciliation_state)
    WHERE sot_reconciliation_state IS NOT NULL;

-- Verify
SELECT column_name FROM information_schema.columns
WHERE table_name = 'trades'
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
