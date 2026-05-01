-- Backfill signal_evaluations.probability_lgb_v12 from window_snapshots.
--
-- Audit-task #332. Companion to the writer fix that wires
-- StrategyRegistry → DBClient.update_signal_evaluations_lgb_v12 going
-- forward. This script repairs historical rows that were written before
-- the fix landed.
--
-- Source of truth: window_snapshots.probability_lgb_v12 (already
-- populated since PR #438 / migration add_probability_lgb_v12.sql).
-- Join key: (window_ts, asset, timeframe). signal_evaluations carries
-- multiple eval_offset rows per (window_ts, asset, timeframe) — we
-- broadcast the same v12 value across all of them, mirroring how
-- window_snapshots stores one row per (window_ts, asset, timeframe).
--
-- Idempotent: COALESCE preserves any value already populated.
-- Safe to run multiple times. Best run on RDS (Hub-side) since local
-- Mac is forbidden from polymarket per project memory.
--
-- Verification before/after:
--   SELECT count(*) FILTER (WHERE probability_lgb_v12 IS NOT NULL)::float
--          / NULLIF(count(*),0) AS pct_populated
--   FROM signal_evaluations
--   WHERE evaluated_at > NOW() - INTERVAL '7 days';

BEGIN;

WITH source AS (
    SELECT window_ts, asset, timeframe, probability_lgb_v12
    FROM window_snapshots
    WHERE probability_lgb_v12 IS NOT NULL
)
UPDATE signal_evaluations se
   SET probability_lgb_v12 = src.probability_lgb_v12
  FROM source src
 WHERE se.window_ts = src.window_ts
   AND se.asset     = src.asset
   AND se.timeframe = src.timeframe
   AND se.probability_lgb_v12 IS NULL;

-- Report rows touched
SELECT
    count(*) FILTER (WHERE probability_lgb_v12 IS NOT NULL) AS populated_now,
    count(*) AS total_rows,
    round(
        100.0 * count(*) FILTER (WHERE probability_lgb_v12 IS NOT NULL)
              / NULLIF(count(*), 0)::numeric,
        2
    ) AS pct_populated
FROM signal_evaluations
WHERE evaluated_at > NOW() - INTERVAL '14 days';

COMMIT;
