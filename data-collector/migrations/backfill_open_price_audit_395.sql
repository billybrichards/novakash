-- ============================================================
-- Audit #395 — Backfill market_data.open_price (NULL since PR #464)
-- ============================================================
--
-- ROLLOUT INSTRUCTIONS
-- --------------------
-- 1. Deploy the collector.py writer fix (PR that includes this file) to Railway.
--    New windows will be populated by the live writer from that point forward.
--
-- 2. For historical NULL rows (≈9000 rows accumulated since PR #464 / 2026-05-02):
--    Run the PRIMARY backfill (Step 2) — a one-shot Python script on the
--    collector container that re-fetches Gamma for each NULL slug. Estimated
--    runtime: ~38 min at 4 req/s rate limit.
--
-- 3. After the Python script finishes, run Step 3 for any remaining NULLs
--    where the Gamma event 404s or eventMetadata is absent (old archived events).
--    This uses ticks_chainlink as a lower-fidelity fallback.
--
-- 4. Verify with Step 4 query that null counts are near zero.
--
-- DO NOT execute this file directly against prod without reading all steps.
-- Step 3a is DDL (ALTER TABLE). Steps 0/1/4 are read-only diagnostic queries.
-- ============================================================

-- ============================================================
-- STEP 0: Sanity check current NULL counts
-- ============================================================

SELECT
    timeframe,
    COUNT(*) FILTER (WHERE open_price IS NULL) AS null_open,
    COUNT(*) FILTER (WHERE open_price IS NOT NULL) AS has_open,
    COUNT(*) FILTER (WHERE close_price IS NOT NULL) AS has_close,
    COUNT(*) AS total,
    MIN(window_ts) AS oldest,
    MAX(window_ts) AS newest
FROM market_data
WHERE collected_at > NOW() - INTERVAL '7 days'
GROUP BY timeframe
ORDER BY timeframe;

-- ============================================================
-- STEP 1: List NULL rows that need backfill (resolved windows only)
-- ============================================================
-- Engine consumers only need open_price for *resolved* windows when
-- looking up historical signals. Currently-active windows will be filled
-- by the patched writer once the new code deploys.

SELECT window_ts, asset, timeframe, market_slug
FROM market_data
WHERE open_price IS NULL
  AND resolved = TRUE
  AND collected_at > NOW() - INTERVAL '14 days'
ORDER BY window_ts DESC
LIMIT 100;  -- preview; full set is ~9000 rows

-- ============================================================
-- STEP 2: PRIMARY backfill — script-driven Gamma re-fetch
-- ============================================================
-- Owner: data-collector container. New file:
--   data-collector/backfill_open_price.py
--
-- Pseudocode:
--   for row in SELECT window_ts, asset, timeframe, market_slug
--              FROM market_data
--              WHERE open_price IS NULL
--                AND resolved = TRUE
--                AND collected_at > NOW() - INTERVAL '30 days'
--              ORDER BY window_ts DESC:
--       slug = row.market_slug
--       resp = await gamma.GET /events?slug={slug}
--       ptb  = resp[0].eventMetadata.priceToBeat
--               OR (slug for window_ts - duration).eventMetadata.finalPrice
--       if ptb:
--           UPDATE market_data SET open_price = ptb
--             WHERE window_ts = row.window_ts
--               AND asset = row.asset
--               AND timeframe = row.timeframe;
--
-- Run on Montreal (RDS access) once after PR merges. Single shot.

-- ============================================================
-- STEP 3: FALLBACK backfill — chainlink ticks (if Gamma backfill leaves gaps)
-- ============================================================
-- For any rows the Gamma-driven script CANNOT fill (event 404s,
-- eventMetadata absent on very old archived windows, etc.) we fall
-- back to ticks_chainlink. NOT canonical (mean delta ~$14.68 vs Streams)
-- but better than NULL for low-fidelity historical analysis. Mark these
-- with a sentinel so analytical queries can exclude them.

-- 3a) Add a source column if it doesn't exist (one-time DDL):
ALTER TABLE market_data
    ADD COLUMN IF NOT EXISTS open_price_source TEXT DEFAULT NULL;

-- 3b) Backfill from chainlink ticks (only for assets with chainlink coverage):
WITH boundary_ticks AS (
    SELECT DISTINCT ON (md.window_ts, md.asset, md.timeframe)
        md.window_ts,
        md.asset,
        md.timeframe,
        tc.price_usd::DOUBLE PRECISION AS chainlink_open
    FROM market_data md
    JOIN ticks_chainlink tc
      ON tc.asset = md.asset
     AND tc.captured_at >= TO_TIMESTAMP(md.window_ts) - INTERVAL '5 seconds'
     AND tc.captured_at <= TO_TIMESTAMP(md.window_ts) + INTERVAL '15 seconds'
    WHERE md.open_price IS NULL
      AND md.resolved = TRUE
      AND md.collected_at > NOW() - INTERVAL '30 days'
    ORDER BY md.window_ts, md.asset, md.timeframe,
             ABS(EXTRACT(EPOCH FROM tc.captured_at - TO_TIMESTAMP(md.window_ts))) ASC
)
UPDATE market_data md
SET open_price = bt.chainlink_open,
    open_price_source = 'chainlink_polygon_backfill'
FROM boundary_ticks bt
WHERE md.window_ts = bt.window_ts
  AND md.asset = bt.asset
  AND md.timeframe = bt.timeframe
  AND md.open_price IS NULL;

-- ============================================================
-- STEP 4: Verify
-- ============================================================
SELECT
    timeframe,
    open_price_source,
    COUNT(*) FILTER (WHERE open_price IS NULL) AS still_null,
    COUNT(*) FILTER (WHERE open_price IS NOT NULL) AS filled,
    COUNT(*) AS total
FROM market_data
WHERE collected_at > NOW() - INTERVAL '7 days'
GROUP BY timeframe, open_price_source
ORDER BY timeframe, open_price_source NULLS FIRST;
