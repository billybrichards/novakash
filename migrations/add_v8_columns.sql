-- Migration: v8.0 tracking columns for window_snapshots
-- Date: 2026-04-06
-- Purpose: Clean before/after reconciliation, FOK tracking, gate auditing

-- Source tracking
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS delta_source VARCHAR(10);
-- 'tiingo' | 'chainlink' | 'binance'

-- FOK execution tracking
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS execution_mode VARCHAR(20);
-- 'fok_ladder' | 'gtc' | 'paper'
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS fok_attempts INT;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS fok_fill_step INT;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS clob_fill_price FLOAT;

-- Confidence tier
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS confidence_tier VARCHAR(10);
-- 'NONE' | 'LOW' | 'MODERATE' | 'HIGH' | 'DECISIVE'

-- Entry timing
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS entry_time_offset INT;
-- seconds before window close

-- Gate audit trail
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS gates_passed TEXT;
-- e.g. 'vpin,delta,cg,floor,cap,confidence'
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS gate_failed VARCHAR(20);
-- which gate stopped: 'vpin' | 'delta' | 'cg_veto' | 'floor' | 'cap' | NULL

-- Verify
SELECT column_name FROM information_schema.columns
WHERE table_name = 'window_snapshots'
  AND column_name IN ('delta_source','execution_mode','fok_attempts','fok_fill_step',
    'clob_fill_price','confidence_tier','entry_time_offset','gates_passed','gate_failed')
ORDER BY column_name;
