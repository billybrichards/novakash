-- Migration: add_eval_offset_to_window_snapshots.sql
-- Date: 2026-04-13
-- Purpose: Add eval_offset column to window_snapshots (missing from v8.0 migration)
--          This column is critical for tracking when evaluations occur within a window
--          (e.g., T-142, T-104, etc.) for proper audit trail and strategy analysis.
--
-- Background:
--   - write_window_snapshot() saves eval_offset (line 81-82 in db_client.py)
--   - GateContext includes eval_offset (line 80 in gates.py)
--   - v4_down_only_strategy uses eval_offset for T-90 to T-150 window timing
--   - Migration was accidentally omitted from add_v8_snapshot_columns.sql
--
-- Safe to run multiple times (ADD COLUMN IF NOT EXISTS)

ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS eval_offset INTEGER;

-- Add index for queries filtering by eval_offset (e.g., "show all evaluations at T-150")
CREATE INDEX IF NOT EXISTS idx_window_snapshots_eval_offset ON window_snapshots (eval_offset) WHERE eval_offset IS NOT NULL;

-- Verify the column exists
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'window_snapshots'
  AND column_name = 'eval_offset';

-- Show sample data with eval_offset
SELECT 
    window_ts,
    asset,
    timeframe,
    eval_offset,
    direction,
    delta_source,
    gate_failed,
    shadow_trade_direction
FROM window_snapshots
WHERE eval_offset IS NOT NULL
ORDER BY window_ts DESC
LIMIT 10;
