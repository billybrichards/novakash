-- Add actual_direction column to window_snapshots
-- Used by shadow resolution to record what the oracle resolved (UP/DOWN)
-- Referenced by:
--   - engine/persistence/db_client.py:1265 (get_oracle_outcome)
--   - engine/strategies/orchestrator.py:1901 (send_window_resolution)
--   - engine/strategies/orchestrator.py:1941 (outcome alert data)

ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS actual_direction TEXT;

-- Index for efficient lookups
CREATE INDEX IF NOT EXISTS idx_ws_actual_direction
ON window_snapshots (window_ts, asset)
WHERE actual_direction IS NOT NULL;
