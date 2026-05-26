-- =================================================================================
-- Ghost threshold sweep — 0.02 granularity, production-realistic methodology
-- =================================================================================
-- Covers all major probability columns × UP/DOWN × 25 thresholds per direction.
-- Production-realistic per RDS note #684:
--   - DISTINCT ON (window_ts) ORDER BY eval_offset DESC (first qualifying tick)
--   - LEFT JOIN to market_data + window_snapshots (COALESCE for backfilled truth)
--   - eval_offset BETWEEN 60 AND 180
--
-- USAGE:
--   source /home/billyrichards/bbrdev1/novakash/novakashmain/scripts/cross-compare/_lib.sh 2>/dev/null
--   export PGPASSWORD="$DB_PASS"
--   psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
--     -P pager=off -f /home/billyrichards/bbrdev1/novakash/novakashmain/scripts/analysis/ghost_threshold_sweep.sql
--
-- To change lookback: edit the `interval '24 hours'` strings below (5 places, one per asset).
-- To filter results: add HAVING clauses to the final SELECT (e.g., HAVING wr >= 80 AND resolved >= 30).
-- =================================================================================

SET statement_timeout = '180s';

\echo
\echo === GHOST THRESHOLD SWEEP — 0.02 granularity, last 24h, production method ===
\echo

WITH
-- BTC columns (asset = 'BTC')
ff_btc AS (
  SELECT * FROM (
    SELECT DISTINCT ON (window_ts) 'v9_2'           AS col, window_ts, probability_lgb_v9_2::float          AS p FROM signal_evaluations
      WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v9_2 IS NOT NULL          AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC
  ) UNION ALL SELECT * FROM (
    SELECT DISTINCT ON (window_ts) 'v9_2_pure'      AS col, window_ts, probability_lgb_v9_2_pure::float      AS p FROM signal_evaluations
      WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v9_2_pure IS NOT NULL     AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC
  ) UNION ALL SELECT * FROM (
    SELECT DISTINCT ON (window_ts) 'v9_2_post_iso'  AS col, window_ts, probability_lgb_v9_2_post_iso::float  AS p FROM signal_evaluations
      WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v9_2_post_iso IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC
  ) UNION ALL SELECT * FROM (
    SELECT DISTINCT ON (window_ts) 'v9_3_btc'       AS col, window_ts, probability_lgb_v9_3_btc::float       AS p FROM signal_evaluations
      WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v9_3_btc IS NOT NULL      AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC
  ) UNION ALL SELECT * FROM (
    SELECT DISTINCT ON (window_ts) 'v9_3_btc_pure'  AS col, window_ts, probability_lgb_v9_3_btc_pure::float  AS p FROM signal_evaluations
      WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v9_3_btc_pure IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC
  ) UNION ALL SELECT * FROM (
    SELECT DISTINCT ON (window_ts) 'v12'            AS col, window_ts, probability_lgb_v12::float            AS p FROM signal_evaluations
      WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v12 IS NOT NULL           AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC
  ) UNION ALL SELECT * FROM (
    SELECT DISTINCT ON (window_ts) 'v12_pure'       AS col, window_ts, probability_lgb_v12_pure::float       AS p FROM signal_evaluations
      WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v12_pure IS NOT NULL      AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC
  ) UNION ALL SELECT * FROM (
    SELECT DISTINCT ON (window_ts) 'v12_meta_gate'  AS col, window_ts, probability_v12_meta_gate::float      AS p FROM signal_evaluations
      WHERE asset='BTC' AND timeframe='5m' AND probability_v12_meta_gate IS NOT NULL     AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC
  ) UNION ALL SELECT * FROM (
    SELECT DISTINCT ON (window_ts) 'v9_2_meta_gate' AS col, window_ts, probability_v9_2_meta_gate::float     AS p FROM signal_evaluations
      WHERE asset='BTC' AND timeframe='5m' AND probability_v9_2_meta_gate IS NOT NULL    AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC
  ) UNION ALL SELECT * FROM (
    SELECT DISTINCT ON (window_ts) 'v9_1'           AS col, window_ts, probability_lgb_v9_1::float           AS p FROM signal_evaluations
      WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v9_1 IS NOT NULL          AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC
  ) UNION ALL SELECT * FROM (
    SELECT DISTINCT ON (window_ts) 'v2_meta_gate'   AS col, window_ts, probability_v2_meta_gate::float       AS p FROM signal_evaluations
      WHERE asset='BTC' AND timeframe='5m' AND probability_v2_meta_gate IS NOT NULL      AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC
  )
),
-- ETH columns
ff_eth AS (
  SELECT * FROM (
    SELECT DISTINCT ON (window_ts) 'v9_2_eth'       AS col, window_ts, probability_lgb_v9_2_eth::float       AS p FROM signal_evaluations
      WHERE asset='ETH' AND timeframe='5m' AND probability_lgb_v9_2_eth IS NOT NULL      AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC
  ) UNION ALL SELECT * FROM (
    SELECT DISTINCT ON (window_ts) 'v9_5_eth'       AS col, window_ts, probability_lgb_v9_5_eth::float       AS p FROM signal_evaluations
      WHERE asset='ETH' AND timeframe='5m' AND probability_lgb_v9_5_eth IS NOT NULL      AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC
  ) UNION ALL SELECT * FROM (
    SELECT DISTINCT ON (window_ts) 'v9_5_eth_pure'  AS col, window_ts, probability_lgb_v9_5_eth_pure::float  AS p FROM signal_evaluations
      WHERE asset='ETH' AND timeframe='5m' AND probability_lgb_v9_5_eth_pure IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC
  )
),
-- XRP columns
ff_xrp AS (
  SELECT * FROM (
    SELECT DISTINCT ON (window_ts) 'v9_2_xrp'       AS col, window_ts, probability_lgb_v9_2_xrp::float       AS p FROM signal_evaluations
      WHERE asset='XRP' AND timeframe='5m' AND probability_lgb_v9_2_xrp IS NOT NULL      AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC
  ) UNION ALL SELECT * FROM (
    SELECT DISTINCT ON (window_ts) 'v9_5_xrp'       AS col, window_ts, probability_lgb_v9_5_xrp::float       AS p FROM signal_evaluations
      WHERE asset='XRP' AND timeframe='5m' AND probability_lgb_v9_5_xrp IS NOT NULL      AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC
  )
),
-- Join each to its asset's truth (COALESCE market_data + window_snapshots)
gw_btc AS (
  SELECT ff.col, ff.window_ts, ff.p, COALESCE(md.outcome, ws_o) AS truth
    FROM ff_btc ff
    LEFT JOIN market_data md ON md.window_ts=ff.window_ts AND md.asset='BTC' AND md.timeframe='5m'
    LEFT JOIN LATERAL (SELECT outcome AS ws_o FROM window_snapshots ws WHERE ws.window_ts=ff.window_ts AND ws.asset='BTC' AND ws.timeframe='5m' AND outcome IS NOT NULL LIMIT 1) wsx ON TRUE
),
gw_eth AS (
  SELECT ff.col, ff.window_ts, ff.p, COALESCE(md.outcome, ws_o) AS truth
    FROM ff_eth ff
    LEFT JOIN market_data md ON md.window_ts=ff.window_ts AND md.asset='ETH' AND md.timeframe='5m'
    LEFT JOIN LATERAL (SELECT outcome AS ws_o FROM window_snapshots ws WHERE ws.window_ts=ff.window_ts AND ws.asset='ETH' AND ws.timeframe='5m' AND outcome IS NOT NULL LIMIT 1) wsx ON TRUE
),
gw_xrp AS (
  SELECT ff.col, ff.window_ts, ff.p, COALESCE(md.outcome, ws_o) AS truth
    FROM ff_xrp ff
    LEFT JOIN market_data md ON md.window_ts=ff.window_ts AND md.asset='XRP' AND md.timeframe='5m'
    LEFT JOIN LATERAL (SELECT outcome AS ws_o FROM window_snapshots ws WHERE ws.window_ts=ff.window_ts AND ws.asset='XRP' AND ws.timeframe='5m' AND outcome IS NOT NULL LIMIT 1) wsx ON TRUE
),
all_gw AS (
  SELECT 'BTC' AS asset, * FROM gw_btc
  UNION ALL SELECT 'ETH', * FROM gw_eth
  UNION ALL SELECT 'XRP', * FROM gw_xrp
)
-- UP sweep at 0.02 granularity from 0.50 to 1.00
SELECT asset, col,
       'UP' AS direction,
       thr,
       COUNT(*) FILTER (WHERE p >= thr) AS fires,
       COUNT(*) FILTER (WHERE p >= thr AND truth IN ('UP','DOWN')) AS resolved,
       COUNT(*) FILTER (WHERE p >= thr AND truth='UP') AS wins,
       ROUND(100.0*COUNT(*) FILTER (WHERE p >= thr AND truth='UP')::numeric
             / NULLIF(COUNT(*) FILTER (WHERE p >= thr AND truth IN ('UP','DOWN')),0), 1) AS wr_pct
  FROM all_gw
  CROSS JOIN LATERAL (SELECT generate_series::float/100 AS thr FROM generate_series(50, 100, 2)) AS t
 GROUP BY asset, col, thr
HAVING COUNT(*) FILTER (WHERE p >= thr AND truth IN ('UP','DOWN')) >= 10
   AND ROUND(100.0*COUNT(*) FILTER (WHERE p >= thr AND truth='UP')::numeric
             / NULLIF(COUNT(*) FILTER (WHERE p >= thr AND truth IN ('UP','DOWN')),0), 1) >= 75
UNION ALL
-- DOWN sweep at 0.02 granularity from 0.00 to 0.50
SELECT asset, col,
       'DOWN' AS direction,
       thr,
       COUNT(*) FILTER (WHERE p <= thr) AS fires,
       COUNT(*) FILTER (WHERE p <= thr AND truth IN ('UP','DOWN')) AS resolved,
       COUNT(*) FILTER (WHERE p <= thr AND truth='DOWN') AS wins,
       ROUND(100.0*COUNT(*) FILTER (WHERE p <= thr AND truth='DOWN')::numeric
             / NULLIF(COUNT(*) FILTER (WHERE p <= thr AND truth IN ('UP','DOWN')),0), 1) AS wr_pct
  FROM all_gw
  CROSS JOIN LATERAL (SELECT generate_series::float/100 AS thr FROM generate_series(0, 50, 2)) AS t
 GROUP BY asset, col, thr
HAVING COUNT(*) FILTER (WHERE p <= thr AND truth IN ('UP','DOWN')) >= 10
   AND ROUND(100.0*COUNT(*) FILTER (WHERE p <= thr AND truth='DOWN')::numeric
             / NULLIF(COUNT(*) FILTER (WHERE p <= thr AND truth IN ('UP','DOWN')),0), 1) >= 75
ORDER BY wr_pct DESC, fires DESC;
