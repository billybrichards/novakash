-- =============================================================================
-- GHOST Shadow Audit v2 — Realistic Fills from ticks_clob
-- =============================================================================
-- Usage:
--   source scripts/cross-compare/_lib.sh
--   psql -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME \
--     -v audit_date="'2026-05-25'" \
--     -f scripts/analysis/shadow_audit_realistic_fills.sql
--
-- Set :audit_date to any date (YYYY-MM-DD string, single-quoted).
-- Optimized with 10s bucket join instead of LATERAL to avoid timeouts.
-- =============================================================================

\set ON_ERROR_STOP on
\timing on
SET statement_timeout = '300s';

-- Default date (override with -v audit_date="'YYYY-MM-DD'")
\if :{?audit_date}
\else
  \set audit_date '''2026-05-25'''
\endif

\echo '=================================================================='
\echo 'GHOST Shadow Audit v2 — Realistic Fills'
\echo 'Date: ' :audit_date
\echo '=================================================================='

WITH
-- ── Step 1: Dedupe decisions ──────────────────────────────────────────────────
deduped_decisions AS (
  SELECT DISTINCT ON (strategy_id, asset, window_ts)
    strategy_id,
    asset,
    window_ts,
    eval_offset,
    direction,
    -- eval_epoch: unix timestamp of when signal fires (seconds before window close)
    (window_ts + 300 - eval_offset) AS eval_epoch
  FROM strategy_decisions
  WHERE action = 'TRADE'
    AND evaluated_at::date = :audit_date
  ORDER BY strategy_id, asset, window_ts, eval_offset ASC
),

-- ── Step 2: Pre-aggregate ticks_clob into 10s buckets for the audit day ───────
-- This avoids the slow LATERAL per-row lookup (which caused 120s timeouts).
-- ticks_clob polls every ~10s, so 10s buckets give near-exact fill estimates.
clob_bucketed AS (
  SELECT
    asset,
    (EXTRACT(EPOCH FROM ts)::bigint / 10) * 10   AS bucket_10s,
    AVG(up_best_ask)                              AS up_ask,
    AVG(down_best_ask)                            AS down_ask
  FROM ticks_clob
  WHERE ts::date = :audit_date
    AND (up_best_ask IS NOT NULL OR down_best_ask IS NOT NULL)
  GROUP BY asset, (EXTRACT(EPOCH FROM ts)::bigint / 10) * 10
),

-- ── Step 3: Join decisions to nearest clob bucket (±10s tolerance) ───────────
clob_fills AS (
  SELECT
    d.strategy_id,
    d.asset,
    d.window_ts,
    d.eval_offset,
    d.direction,
    d.eval_epoch,
    -- Try exact bucket, then +10s, then -10s
    COALESCE(
      NULLIF(
        CASE UPPER(d.direction)
          WHEN 'UP'   THEN cb0.up_ask
          WHEN 'YES'  THEN cb0.up_ask
          WHEN 'DOWN' THEN cb0.down_ask
          WHEN 'NO'   THEN cb0.down_ask
        END, 0),
      NULLIF(
        CASE UPPER(d.direction)
          WHEN 'UP'   THEN cbp.up_ask
          WHEN 'YES'  THEN cbp.up_ask
          WHEN 'DOWN' THEN cbp.down_ask
          WHEN 'NO'   THEN cbp.down_ask
        END, 0),
      NULLIF(
        CASE UPPER(d.direction)
          WHEN 'UP'   THEN cbm.up_ask
          WHEN 'YES'  THEN cbm.up_ask
          WHEN 'DOWN' THEN cbm.down_ask
          WHEN 'NO'   THEN cbm.down_ask
        END, 0)
    ) AS clob_fill_price,
    CASE
      WHEN (cb0.up_ask IS NOT NULL OR cb0.down_ask IS NOT NULL) THEN 'clob'
      WHEN (cbp.up_ask IS NOT NULL OR cbp.down_ask IS NOT NULL) THEN 'clob'
      WHEN (cbm.up_ask IS NOT NULL OR cbm.down_ask IS NOT NULL) THEN 'clob'
      ELSE NULL
    END AS clob_source
  FROM deduped_decisions d
  LEFT JOIN clob_bucketed cb0
    ON cb0.asset = d.asset AND cb0.bucket_10s = (d.eval_epoch / 10) * 10
  LEFT JOIN clob_bucketed cbp
    ON cbp.asset = d.asset AND cbp.bucket_10s = (d.eval_epoch / 10) * 10 + 10
  LEFT JOIN clob_bucketed cbm
    ON cbm.asset = d.asset AND cbm.bucket_10s = (d.eval_epoch / 10) * 10 - 10
),

-- ── Step 4: Add market_data fallback + outcome ────────────────────────────────
final_fills AS (
  SELECT
    cf.strategy_id,
    cf.asset,
    cf.window_ts,
    cf.eval_offset,
    cf.direction,
    cf.clob_fill_price,
    md.outcome,
    md.resolved,
    COALESCE(
      cf.clob_fill_price,
      CASE UPPER(cf.direction)
        WHEN 'UP'   THEN md.up_price
        WHEN 'YES'  THEN md.up_price
        WHEN 'DOWN' THEN md.down_price
        WHEN 'NO'   THEN md.down_price
      END
    ) AS fill_price,
    CASE WHEN cf.clob_source IS NOT NULL THEN 'clob' ELSE 'market_data' END AS fill_source
  FROM clob_fills cf
  LEFT JOIN market_data md
    ON md.asset = cf.asset
    AND md.window_ts = cf.window_ts
    AND md.timeframe = '5m'
),

-- ── Step 5: Per-fire PnL ──────────────────────────────────────────────────────
per_fire_pnl AS (
  SELECT
    strategy_id,
    asset,
    window_ts,
    eval_offset,
    direction,
    fill_price,
    fill_source,
    outcome,
    resolved,
    CASE
      WHEN resolved = true AND outcome IS NOT NULL THEN
        CASE
          WHEN (UPPER(direction) IN ('UP', 'YES') AND outcome = 'UP')   THEN 'WIN'
          WHEN (UPPER(direction) IN ('DOWN', 'NO') AND outcome = 'DOWN') THEN 'WIN'
          ELSE 'LOSS'
        END
      ELSE 'FLAT'
    END AS result,
    -- $25 stake PnL
    -- WIN:  stake * (1/fill - 1 - fee)   (fee = 7.2%)
    -- LOSS: -stake
    -- FLAT: 0
    CASE
      WHEN resolved = true AND outcome IS NOT NULL AND fill_price > 0 THEN
        CASE
          WHEN (UPPER(direction) IN ('UP', 'YES') AND outcome = 'UP')   THEN
            25.0 * (1.0 / fill_price - 1.0 - 0.072)
          WHEN (UPPER(direction) IN ('DOWN', 'NO') AND outcome = 'DOWN') THEN
            25.0 * (1.0 / fill_price - 1.0 - 0.072)
          ELSE -25.0
        END
      ELSE 0.0
    END AS pnl_25,
    -- $5 stake PnL
    CASE
      WHEN resolved = true AND outcome IS NOT NULL AND fill_price > 0 THEN
        CASE
          WHEN (UPPER(direction) IN ('UP', 'YES') AND outcome = 'UP')   THEN
            5.0 * (1.0 / fill_price - 1.0 - 0.072)
          WHEN (UPPER(direction) IN ('DOWN', 'NO') AND outcome = 'DOWN') THEN
            5.0 * (1.0 / fill_price - 1.0 - 0.072)
          ELSE -5.0
        END
      ELSE 0.0
    END AS pnl_5
  FROM final_fills
)

-- ── Step 6: Aggregate per strategy ────────────────────────────────────────────
SELECT
  strategy_id,
  COUNT(*)                                                                AS fires,
  SUM(CASE WHEN result = 'WIN'  THEN 1 ELSE 0 END)                       AS wins,
  SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END)                       AS losses,
  SUM(CASE WHEN result = 'FLAT' THEN 1 ELSE 0 END)                       AS flats,
  ROUND(
    100.0 * SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END)
    / NULLIF(SUM(CASE WHEN result IN ('WIN','LOSS') THEN 1 ELSE 0 END), 0),
    1
  )                                                                       AS wr_pct,
  ROUND(AVG(fill_price)::numeric, 3)                                      AS avg_fill,
  ROUND(MIN(fill_price)::numeric, 3)                                      AS min_fill,
  ROUND(MAX(fill_price)::numeric, 3)                                      AS max_fill,
  ROUND(SUM(pnl_25)::numeric, 2)                                          AS shadow_pnl_25,
  ROUND(SUM(pnl_5)::numeric, 2)                                           AS shadow_pnl_5,
  SUM(CASE WHEN fill_source = 'clob'        THEN 1 ELSE 0 END)            AS clob_hits,
  SUM(CASE WHEN fill_source = 'market_data' THEN 1 ELSE 0 END)            AS md_fallbacks
FROM per_fire_pnl
GROUP BY strategy_id
ORDER BY shadow_pnl_5 DESC;
