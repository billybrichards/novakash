-- Backfill window_snapshots CLOB columns from clob_book_snapshots.
--
-- Audit-task #338. Companion to the writer fix that wires CLOB prices into
-- write_window_snapshot going forward. This script repairs historical rows
-- written before the fix landed.
--
-- Source of truth: clob_book_snapshots (populated by the CLOB feed).
-- Join key: match on the nearest clob_book_snapshots row within 60 seconds
-- of each window_ts (CLOB feed polls every 10 s so the closest row is
-- within one poll interval).
--
-- Derived fields computed here mirror the engine derivation:
--   clob_implied_up  = clob_up_bid          (bid = market-implied probability)
--   clob_fill_price  = (up_ask + down_ask) / 2
--   clob_imbalance   = NULL (requires size data not stored per-row)
--
-- Idempotent: COALESCE preserves any value already populated.
-- Safe to run multiple times.
--
-- Verification before/after:
--   SELECT count(*) FILTER (WHERE clob_up_bid IS NOT NULL)::float
--          / NULLIF(count(*), 0) AS pct_clob_populated
--   FROM window_snapshots
--   WHERE created_at > NOW() - INTERVAL '7 days';

BEGIN;

WITH nearest_clob AS (
    -- For each window snapshot, find the nearest clob_book_snapshots row
    -- within 60 seconds (CLOB polls every 10 s).
    SELECT DISTINCT ON (ws.window_ts, ws.asset)
        ws.window_ts,
        ws.asset,
        cbs.up_best_bid   AS clob_up_bid,
        cbs.up_best_ask   AS clob_up_ask,
        cbs.down_best_bid AS clob_down_bid,
        cbs.down_best_ask AS clob_down_ask
    FROM window_snapshots ws
    JOIN clob_book_snapshots cbs
      ON cbs.asset = ws.asset
     AND cbs.fetched_at BETWEEN to_timestamp(ws.window_ts) - INTERVAL '60 seconds'
                             AND to_timestamp(ws.window_ts) + INTERVAL '60 seconds'
    WHERE ws.clob_up_bid IS NULL
      AND cbs.up_best_bid IS NOT NULL
    ORDER BY ws.window_ts, ws.asset, ABS(EXTRACT(EPOCH FROM (cbs.fetched_at - to_timestamp(ws.window_ts))))
)
UPDATE window_snapshots ws
   SET clob_up_bid     = COALESCE(ws.clob_up_bid,    nc.clob_up_bid),
       clob_up_ask     = COALESCE(ws.clob_up_ask,    nc.clob_up_ask),
       clob_down_bid   = COALESCE(ws.clob_down_bid,  nc.clob_down_bid),
       clob_down_ask   = COALESCE(ws.clob_down_ask,  nc.clob_down_ask),
       clob_implied_up = COALESCE(ws.clob_implied_up, nc.clob_up_bid),
       clob_fill_price = COALESCE(ws.clob_fill_price,
                                  CASE
                                    WHEN nc.clob_up_ask IS NOT NULL AND nc.clob_down_ask IS NOT NULL
                                    THEN (nc.clob_up_ask + nc.clob_down_ask) / 2
                                  END)
  FROM nearest_clob nc
 WHERE ws.window_ts = nc.window_ts
   AND ws.asset     = nc.asset;

-- Report rows touched
SELECT
    count(*) FILTER (WHERE clob_up_bid IS NOT NULL)   AS rows_with_clob,
    count(*) FILTER (WHERE clob_implied_up IS NOT NULL) AS rows_with_implied_up,
    count(*) FILTER (WHERE clob_fill_price IS NOT NULL) AS rows_with_fill_price,
    count(*) AS total_rows,
    round(
        100.0 * count(*) FILTER (WHERE clob_up_bid IS NOT NULL)
              / NULLIF(count(*), 0)::numeric,
        2
    ) AS pct_clob_populated
FROM window_snapshots
WHERE created_at > NOW() - INTERVAL '14 days';

COMMIT;
