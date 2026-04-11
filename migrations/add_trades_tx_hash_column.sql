-- Migration: POLY-SOT-d — polymarket_tx_hash column for on-chain SOT proof
-- Date: 2026-04-11
-- Purpose: The existing polymarket_order_id column stores the Polymarket CLOB
--          order ID (useful for the forward path where the CLOB /get_order
--          endpoint is still fresh). POLY-SOT-d switches the reconciler to
--          use poly_fills as the authoritative source of truth instead — and
--          poly_fills identifies fills by their on-chain Polygon transaction
--          hash, not by the CLOB order ID. We add a new column so that once
--          the reconciler matches a trades row to a poly_fills row, we can
--          stamp the cryptographic proof (the tx hash) back onto the trade.
--
--          This supersedes POLY-SOT Phase 1 and POLY-SOT-b/c for the
--          reconciler hot path: the CLOB API has a short retention window
--          and returns empty for trades older than a few days, which was
--          producing false-positive engine_optimistic tags. The poly_fills
--          table has no retention window — it's populated by the
--          poly_fills_reconciler worker from data-api.polymarket.com and is
--          append-only. See docs/AUDIT_PROGRESS.md 2026-04-11 POLY-SOT-d
--          entry for the full reasoning + production data distribution.
--
-- Columns added:
--   polymarket_tx_hash  TEXT  On-chain Polygon transaction hash that
--                             cryptographically proves the fill. NULL until
--                             the reconciler matches the trade row against
--                             a poly_fills row.
--
-- Indexes added:
--   idx_trades_tx_hash         Partial index on non-NULL tx hashes for the
--                              hub "show me the tx hash for this trade"
--                              endpoint + on-chain audit queries.
--   idx_manual_trades_tx_hash  Same shape on manual_trades.
--
-- Additive only. Idempotent — safe to re-run. The existing columns from
-- add_trades_sot_columns.sql and add_manual_trades_sot_columns.sql are
-- preserved unchanged; polymarket_order_id stays populated for the forward
-- path, and polymarket_tx_hash is the new on-chain proof.

ALTER TABLE trades        ADD COLUMN IF NOT EXISTS polymarket_tx_hash TEXT;
ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS polymarket_tx_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_trades_tx_hash
    ON trades(polymarket_tx_hash)
    WHERE polymarket_tx_hash IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_manual_trades_tx_hash
    ON manual_trades(polymarket_tx_hash)
    WHERE polymarket_tx_hash IS NOT NULL;

-- Verify
SELECT column_name FROM information_schema.columns
WHERE table_name IN ('trades', 'manual_trades')
  AND column_name = 'polymarket_tx_hash'
ORDER BY table_name;
