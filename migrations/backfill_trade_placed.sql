-- Backfill trade_placed for historical windows
--
-- Bug: Since April 2 revert, _execute_trade() never called update_window_trade_placed().
-- Result: 803 windows have trades in the trades table but trade_placed=FALSE in window_snapshots.
-- Fix: Set trade_placed=TRUE for any window that has a matching trade.
--
-- This uses the same metadata JSON join pattern as the /v58/outcomes endpoint.
-- Safe to run multiple times (idempotent).

BEGIN;

UPDATE window_snapshots ws
SET trade_placed = TRUE
FROM trades t
WHERE t.strategy = 'five_min_vpin'
  AND (t.metadata::json->>'window_ts')::bigint = ws.window_ts
  AND ws.timeframe = '5m'
  AND (ws.trade_placed = FALSE OR ws.trade_placed IS NULL);

COMMIT;
