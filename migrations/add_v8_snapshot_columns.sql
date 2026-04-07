-- Migration: v8.0 window_snapshots + trades columns
-- Date: 2026-04-06
-- Purpose: Add engine metadata, gate audit trail, and shadow trade fields to
--          window_snapshots; add CLOB execution metadata to trades.
-- Safe to run multiple times (ADD COLUMN IF NOT EXISTS throughout).

-- ── window_snapshots: v8.0 fields ──────────────────────────────────────────

-- Engine version tag (e.g. 'v8.0')
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS engine_version VARCHAR(10);

-- Which price source drove the delta signal ('tiingo' | 'chainlink' | 'binance' | 'consensus')
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS delta_source VARCHAR(10);

-- Human-readable confidence bucket ('NONE' | 'LOW' | 'MODERATE' | 'HIGH' | 'DECISIVE')
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS confidence_tier VARCHAR(10);

-- Comma-separated list of gates that passed (e.g. 'vpin,delta,cg,floor,cap,confidence')
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS gates_passed TEXT;

-- Name of the gate that blocked a trade, NULL if all passed (e.g. 'vpin' | 'delta' | 'cg_veto')
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS gate_failed VARCHAR(20);

-- Implied direction from delta even when no trade was placed ('UP' | 'DOWN')
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS shadow_trade_direction VARCHAR(4);

-- Gamma price for the implied direction at evaluation time
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS shadow_trade_entry_price DOUBLE PRECISION;

-- ── trades: v8.0 CLOB execution fields ─────────────────────────────────────

-- Engine version that placed the trade
ALTER TABLE trades ADD COLUMN IF NOT EXISTS engine_version VARCHAR(10);

-- CLOB order ID returned by Polymarket (0x-prefixed hex)
ALTER TABLE trades ADD COLUMN IF NOT EXISTS clob_order_id VARCHAR(128);

-- Actual fill price from CLOB execution
ALTER TABLE trades ADD COLUMN IF NOT EXISTS fill_price DOUBLE PRECISION;

-- Actual fill size (shares) from CLOB execution
ALTER TABLE trades ADD COLUMN IF NOT EXISTS fill_size DOUBLE PRECISION;

-- Execution mode: 'fok_ladder' | 'gtc' | 'paper'
ALTER TABLE trades ADD COLUMN IF NOT EXISTS execution_mode VARCHAR(20);

-- Live/paper flag (mirrors existing paper mode logic)
ALTER TABLE trades ADD COLUMN IF NOT EXISTS is_live BOOLEAN DEFAULT FALSE;

-- ── Verify ──────────────────────────────────────────────────────────────────
SELECT column_name
FROM information_schema.columns
WHERE table_name = 'window_snapshots'
  AND column_name IN (
    'engine_version','delta_source','confidence_tier',
    'gates_passed','gate_failed',
    'shadow_trade_direction','shadow_trade_entry_price'
  )
ORDER BY column_name;

SELECT column_name
FROM information_schema.columns
WHERE table_name = 'trades'
  AND column_name IN (
    'engine_version','clob_order_id','fill_price','fill_size','execution_mode','is_live'
  )
ORDER BY column_name;
