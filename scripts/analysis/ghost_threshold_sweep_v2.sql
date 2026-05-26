-- =================================================================================
-- Ghost threshold sweep v2 — 0.02 granularity, production-realistic methodology
-- =================================================================================
-- Extends ghost_threshold_sweep.sql with:
--   1. All 16 columns including v9_1 (was missing in v1)
--   2. Meta-gate columns swept independently (semantics note below)
--   3. v9.5 ETH pre/post 13:51 UTC 2026-05-25 deploy split
--   4. Lower HAVING threshold: n>=10 (was n>=10 in UP block, same in DOWN)
--   5. Shows all thresholds with wr>=50 for diagnostic; filter to >=75 for promotions
--
-- META-GATE SEMANTICS (v12_meta_gate, v9_2_meta_gate, v2_meta_gate):
--   These columns are NOT raw LGB outputs. They are computed server-side as a
--   product/conjunction of multiple underlying model probabilities. Specifically:
--     - probability_v12_meta_gate: gating function over probability_lgb_v12
--       (requires v12 conviction AND gate >= threshold). Observed range [0.46, 1.00],
--       median 0.97 — nearly always above 0.85. A sweep on this column is unreliable
--       because the column is already pre-gated; sweeping it like a raw probability
--       conflates the gate with the underlying signal.
--     - probability_v9_2_meta_gate: conjunction of v9.2 + meta agreement. Range [0.01, 0.95],
--       median 0.60 — bimodal. Sweepable for DOWN direction (p <= 0.50 fires ~61% WR).
--     - probability_v2_meta_gate: conjunction of TimesFM v2 + meta. Range [0.19, 0.98],
--       median 0.85 — strongly right-skewed. No DOWN fires (never goes below 0.19).
--       All rows fire UP regardless of threshold. Uninformative sweep.
--   RECOMMENDATION: Do not sweep meta-gate columns as standalone thresholds. Use them
--   as additional filter layers on top of underlying LGB columns (as the strategy hooks do).
--
-- COLUMN PROVENANCE (from engine/strategies/data_surface.py):
--   PURE = LGB+isotonic only, no TimesFM HF classifier blend
--   BLEND = LGB raw output blended with TimesFM HF classifier (caps at ~0.82-0.93)
--   META-COMPOSITE = server-computed conjunction/product of multiple model outputs
--   Column                       | Type            | Asset | Lines (data_surface.py)
--   probability_lgb_v9_1         | blend           | BTC   | 226-227
--   probability_lgb_v9_2         | blend           | BTC   | 235
--   probability_lgb_v9_2_pure    | pure            | BTC   | 350
--   probability_lgb_v9_2_post_iso| blend+iso       | BTC   | 244 (isotonic layer 2 on blend)
--   probability_lgb_v9_3_btc     | blend           | BTC   | 314
--   probability_lgb_v9_3_btc_pure| pure            | BTC   | 349
--   probability_lgb_v12          | blend           | BTC   | 220
--   probability_lgb_v12_pure     | pure            | BTC   | 351
--   probability_v12_meta_gate    | meta-composite  | BTC   | 375-377 (v12 + gate)
--   probability_v9_2_meta_gate   | meta-composite  | BTC   | 375-377 (v9.2 + gate)
--   probability_v2_meta_gate     | meta-composite  | BTC   | 375-377 (v2 + gate)
--   probability_lgb_v9_2_eth     | blend           | ETH   | 256
--   probability_lgb_v9_5_eth     | blend           | ETH   | 273
--   probability_lgb_v9_5_eth_pure| pure            | ETH   | 298
--   probability_lgb_v9_2_xrp     | blend           | XRP   | N/A (0 rows, not deployed)
--   probability_lgb_v9_5_xrp     | blend           | XRP   | 371
--
-- USAGE:
--   source /home/billyrichards/bbrdev1/novakash/novakashmain/scripts/cross-compare/_lib.sh 2>/dev/null
--   export PGPASSWORD="$DB_PASS"
--   psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
--     -P pager=off -f /home/billyrichards/bbrdev1/novakash/novakashmain/scripts/analysis/ghost_threshold_sweep_v2.sql
--
-- TRAPS AVOIDED (per note #684):
--   1. DISTINCT ON (window_ts) ORDER BY window_ts, eval_offset DESC — first qualifying tick
--   2. eval_offset filter INSIDE the CTE (not outside)
--   3. LEFT JOIN to market_data (not INNER JOIN)
--   4. COALESCE(md.outcome, ws_o) for backfilled truth coverage
-- =================================================================================

SET statement_timeout = '180s';

\echo
\echo === GHOST THRESHOLD SWEEP v2 — all 16 cols, 0.02 granularity, last 24h ===
\echo

-- ─────────────────────────────────────────────────────────────────────────────
-- SECTION 1: FULL SWEEP — all columns × UP/DOWN × WR >= 75%, n >= 10
-- ─────────────────────────────────────────────────────────────────────────────

\echo --- SECTION 1: Top pockets (WR >= 75%, n >= 10) ---

WITH
ff_btc AS (
  SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v9_1' AS col, window_ts, probability_lgb_v9_1::float AS p FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v9_1 IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v9_2' AS col, window_ts, probability_lgb_v9_2::float AS p FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v9_2 IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v9_2_pure' AS col, window_ts, probability_lgb_v9_2_pure::float AS p FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v9_2_pure IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v9_2_post_iso' AS col, window_ts, probability_lgb_v9_2_post_iso::float AS p FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v9_2_post_iso IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v9_3_btc' AS col, window_ts, probability_lgb_v9_3_btc::float AS p FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v9_3_btc IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v9_3_btc_pure' AS col, window_ts, probability_lgb_v9_3_btc_pure::float AS p FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v9_3_btc_pure IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v12' AS col, window_ts, probability_lgb_v12::float AS p FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v12 IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v12_pure' AS col, window_ts, probability_lgb_v12_pure::float AS p FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v12_pure IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v12_meta_gate' AS col, window_ts, probability_v12_meta_gate::float AS p FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_v12_meta_gate IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v9_2_meta_gate' AS col, window_ts, probability_v9_2_meta_gate::float AS p FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_v9_2_meta_gate IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v2_meta_gate' AS col, window_ts, probability_v2_meta_gate::float AS p FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_v2_meta_gate IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC) x
),
ff_eth AS (
  SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v9_2_eth' AS col, window_ts, probability_lgb_v9_2_eth::float AS p FROM signal_evaluations WHERE asset='ETH' AND timeframe='5m' AND probability_lgb_v9_2_eth IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v9_5_eth' AS col, window_ts, probability_lgb_v9_5_eth::float AS p FROM signal_evaluations WHERE asset='ETH' AND timeframe='5m' AND probability_lgb_v9_5_eth IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v9_5_eth_pure' AS col, window_ts, probability_lgb_v9_5_eth_pure::float AS p FROM signal_evaluations WHERE asset='ETH' AND timeframe='5m' AND probability_lgb_v9_5_eth_pure IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC) x
),
ff_xrp AS (
  SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v9_5_xrp' AS col, window_ts, probability_lgb_v9_5_xrp::float AS p FROM signal_evaluations WHERE asset='XRP' AND timeframe='5m' AND probability_lgb_v9_5_xrp IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at > now() - interval '24 hours' ORDER BY window_ts, eval_offset DESC) x
),
gw_btc AS (SELECT ff.col, ff.window_ts, ff.p, COALESCE(md.outcome, ws_o) AS truth FROM ff_btc ff LEFT JOIN market_data md ON md.window_ts=ff.window_ts AND md.asset='BTC' AND md.timeframe='5m' LEFT JOIN LATERAL (SELECT outcome AS ws_o FROM window_snapshots ws WHERE ws.window_ts=ff.window_ts AND ws.asset='BTC' AND ws.timeframe='5m' AND outcome IS NOT NULL LIMIT 1) wsx ON TRUE),
gw_eth AS (SELECT ff.col, ff.window_ts, ff.p, COALESCE(md.outcome, ws_o) AS truth FROM ff_eth ff LEFT JOIN market_data md ON md.window_ts=ff.window_ts AND md.asset='ETH' AND md.timeframe='5m' LEFT JOIN LATERAL (SELECT outcome AS ws_o FROM window_snapshots ws WHERE ws.window_ts=ff.window_ts AND ws.asset='ETH' AND ws.timeframe='5m' AND outcome IS NOT NULL LIMIT 1) wsx ON TRUE),
gw_xrp AS (SELECT ff.col, ff.window_ts, ff.p, COALESCE(md.outcome, ws_o) AS truth FROM ff_xrp ff LEFT JOIN market_data md ON md.window_ts=ff.window_ts AND md.asset='XRP' AND md.timeframe='5m' LEFT JOIN LATERAL (SELECT outcome AS ws_o FROM window_snapshots ws WHERE ws.window_ts=ff.window_ts AND ws.asset='XRP' AND ws.timeframe='5m' AND outcome IS NOT NULL LIMIT 1) wsx ON TRUE),
all_gw AS (SELECT 'BTC' AS asset, * FROM gw_btc UNION ALL SELECT 'ETH', * FROM gw_eth UNION ALL SELECT 'XRP', * FROM gw_xrp)
SELECT asset, col, 'UP' AS direction, thr, fires, resolved, wins, wr_pct
  FROM (
    SELECT asset, col, thr,
           COUNT(*) FILTER (WHERE p >= thr) AS fires,
           COUNT(*) FILTER (WHERE p >= thr AND truth IN ('UP','DOWN')) AS resolved,
           COUNT(*) FILTER (WHERE p >= thr AND truth='UP') AS wins,
           ROUND(100.0*COUNT(*) FILTER (WHERE p >= thr AND truth='UP')::numeric / NULLIF(COUNT(*) FILTER (WHERE p >= thr AND truth IN ('UP','DOWN')),0), 1) AS wr_pct
      FROM all_gw CROSS JOIN (SELECT generate_series::float/100 AS thr FROM generate_series(50,100,2)) t
     GROUP BY asset, col, thr
    HAVING COUNT(*) FILTER (WHERE p >= thr AND truth IN ('UP','DOWN')) >= 10
  ) x WHERE wr_pct >= 75
UNION ALL
SELECT asset, col, 'DOWN', thr, fires, resolved, wins, wr_pct
  FROM (
    SELECT asset, col, thr,
           COUNT(*) FILTER (WHERE p <= thr) AS fires,
           COUNT(*) FILTER (WHERE p <= thr AND truth IN ('UP','DOWN')) AS resolved,
           COUNT(*) FILTER (WHERE p <= thr AND truth='DOWN') AS wins,
           ROUND(100.0*COUNT(*) FILTER (WHERE p <= thr AND truth='DOWN')::numeric / NULLIF(COUNT(*) FILTER (WHERE p <= thr AND truth IN ('UP','DOWN')),0), 1) AS wr_pct
      FROM all_gw CROSS JOIN (SELECT generate_series::float/100 AS thr FROM generate_series(0,50,2)) t
     GROUP BY asset, col, thr
    HAVING COUNT(*) FILTER (WHERE p <= thr AND truth IN ('UP','DOWN')) >= 10
  ) x WHERE wr_pct >= 75
ORDER BY wr_pct DESC, fires DESC;


-- ─────────────────────────────────────────────────────────────────────────────
-- SECTION 2: v9.5 ETH PRE vs POST 13:51 UTC 2026-05-25 comparison
-- ─────────────────────────────────────────────────────────────────────────────

\echo
\echo --- SECTION 2: v9.5 ETH pre/post 13:51 UTC 2026-05-25 deploy comparison (n>=5) ---

WITH
ff_pre AS (
  SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v9_5_eth_blend_PRE' AS col, window_ts, probability_lgb_v9_5_eth::float AS p FROM signal_evaluations WHERE asset='ETH' AND timeframe='5m' AND probability_lgb_v9_5_eth IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at BETWEEN now() - interval '24 hours' AND '2026-05-25 13:51:00+00' ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v9_5_eth_pure_PRE' AS col, window_ts, probability_lgb_v9_5_eth_pure::float AS p FROM signal_evaluations WHERE asset='ETH' AND timeframe='5m' AND probability_lgb_v9_5_eth_pure IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at BETWEEN now() - interval '24 hours' AND '2026-05-25 13:51:00+00' ORDER BY window_ts, eval_offset DESC) x
),
ff_post AS (
  SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v9_5_eth_blend_POST' AS col, window_ts, probability_lgb_v9_5_eth::float AS p FROM signal_evaluations WHERE asset='ETH' AND timeframe='5m' AND probability_lgb_v9_5_eth IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at >= '2026-05-25 13:51:00+00' ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'v9_5_eth_pure_POST' AS col, window_ts, probability_lgb_v9_5_eth_pure::float AS p FROM signal_evaluations WHERE asset='ETH' AND timeframe='5m' AND probability_lgb_v9_5_eth_pure IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND evaluated_at >= '2026-05-25 13:51:00+00' ORDER BY window_ts, eval_offset DESC) x
),
all_ff AS (SELECT * FROM ff_pre UNION ALL SELECT * FROM ff_post),
gw AS (
  SELECT ff.col, ff.window_ts, ff.p, COALESCE(md.outcome, ws_o) AS truth
    FROM all_ff ff
    LEFT JOIN market_data md ON md.window_ts=ff.window_ts AND md.asset='ETH' AND md.timeframe='5m'
    LEFT JOIN LATERAL (SELECT outcome AS ws_o FROM window_snapshots ws WHERE ws.window_ts=ff.window_ts AND ws.asset='ETH' AND ws.timeframe='5m' AND outcome IS NOT NULL LIMIT 1) wsx ON TRUE
)
SELECT col, 'UP' AS dir, thr, fires, resolved, wins, wr
  FROM (
    SELECT col, thr,
           COUNT(*) FILTER (WHERE p >= thr) AS fires,
           COUNT(*) FILTER (WHERE p >= thr AND truth IN ('UP','DOWN')) AS resolved,
           COUNT(*) FILTER (WHERE p >= thr AND truth='UP') AS wins,
           ROUND(100.0*COUNT(*) FILTER (WHERE p >= thr AND truth='UP')::numeric / NULLIF(COUNT(*) FILTER (WHERE p >= thr AND truth IN ('UP','DOWN')),0), 1) AS wr
      FROM gw CROSS JOIN (SELECT generate_series::float/100 AS thr FROM generate_series(50,100,2)) t
     GROUP BY col, thr HAVING COUNT(*) FILTER (WHERE p >= thr AND truth IN ('UP','DOWN')) >= 5
  ) x WHERE wr >= 65
UNION ALL
SELECT col, 'DOWN', thr, fires, resolved, wins, wr
  FROM (
    SELECT col, thr,
           COUNT(*) FILTER (WHERE p <= thr) AS fires,
           COUNT(*) FILTER (WHERE p <= thr AND truth IN ('UP','DOWN')) AS resolved,
           COUNT(*) FILTER (WHERE p <= thr AND truth='DOWN') AS wins,
           ROUND(100.0*COUNT(*) FILTER (WHERE p <= thr AND truth='DOWN')::numeric / NULLIF(COUNT(*) FILTER (WHERE p <= thr AND truth IN ('UP','DOWN')),0), 1) AS wr
      FROM gw CROSS JOIN (SELECT generate_series::float/100 AS thr FROM generate_series(0,50,2)) t
     GROUP BY col, thr HAVING COUNT(*) FILTER (WHERE p <= thr AND truth IN ('UP','DOWN')) >= 5
  ) x WHERE wr >= 65
ORDER BY col, dir, wr DESC, fires DESC;
