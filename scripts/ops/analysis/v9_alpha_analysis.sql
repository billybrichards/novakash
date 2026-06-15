-- v9_alpha_analysis.sql
-- ─────────────────────────────────────────────────────────────────
-- Definitive v9 LGB pure-signal alpha analysis.
-- Companion to pure_signal_analysis.sql (v10/v12 — Hub note #298/#304).
-- v9 has the longest LIVE history (weeks before being ghosted 2026-04-30) so it
-- is the best-sampled signal in the codebase.
--
-- Mining axes
-- -----------
--   1. T-band            T-24-60 / T-61-120 / T-121-180 / T-181-240
--   2. Direction         UP / DOWN
--   3. vpin_regime       CALM / NORMAL / TRANSITION / CASCADE
--   4. v4_regime         chop / calm_trend / volatile_trend / risk_off
--                          (sourced from window_evaluation_traces.surface_json
--                           because window_snapshots.regime is mostly NULL — only
--                           ~275 of 256k rows populated in 87h sample 2026-05-01)
--   5. Conviction band   dist_v9 = ABS(p_v9 - 0.5) bucketed:
--                          [0.10-0.15] / [0.15-0.20] / [0.20-0.30] / [0.30+]
--   6. 3-tick confirm    LAG-window: only counts a "fire" if 3 CONSECUTIVE
--                        eval_offset ticks at same window+direction passed
--                        the dist threshold. Mirrors engine's
--                        min_consecutive_pass_ticks=3 entry gate.
--   7. twap_gamma_agree  Boolean side-channel
--
-- Data sources
-- ------------
--   v9 probability    window_evaluation_traces.surface_json->>'probability_lgb'
--                       (signal_evaluations.probability_lgb does NOT exist —
--                        only probability_lgb_v12 lives there. v9 only in WET.)
--   ground truth      signal_evaluations.outcome (UP/DOWN)
--   fill price        signal_evaluations.clob_up_ask / clob_down_ask
--   vpin_regime       signal_evaluations.regime
--   v4_regime         window_evaluation_traces.surface_json->>'v4_regime'
--   twap_gamma_agree  signal_evaluations.twap_gamma_agree
--
-- Real P&L math (DB pnl_usd is broken — feedback_wallet_truth_authority.md):
--   stake = $7.50, fee = 7.2% of stake (Polymarket crypto)
--   WIN  = stake * (1 - fill) / fill - 0.072 * stake
--   LOSS = -stake
--
-- Per-window dedup
-- ----------------
-- Engine fires once per (window_ts, direction). For pure-signal accuracy we
-- take the LATEST eval_offset row per (window_ts, direction) since that is
-- closest to the engine's actual trade entry decision. (eval_offset =
-- seconds-to-close, so MAX eval_offset = earliest-in-window evaluation).
-- Identical convention to pure_signal_analysis.sql.
--
-- Convention: eval_offset = seconds-to-close (engine T-minus, verified via
-- live fires). T-181-240 = first minute of window; T-24-60 = last minute.
--
-- Wilson 95% CI is computed in SQL — no Python dependency.
--
-- Run from Montreal:
--   ssh ... 'sudo -u novakash bash -c "cd /home/novakash/novakash && \
--     set -a; source engine/.env; set +a; \
--     psql ${DATABASE_URL/postgresql+asyncpg/postgresql} \
--     -f scripts/ops/analysis/v9_alpha_analysis.sql"'
--
-- Change INTERVAL '87 hours' below to slice different windows.
-- ─────────────────────────────────────────────────────────────────

\pset format wrapped
\pset columns 240

\echo
\echo ============================================================
\echo == V9 LGB PURE-SIGNAL ALPHA ANALYSIS (87h)                ==
\echo ============================================================

-- Reusable CTE: WET joined to SE with all axes.
DROP TABLE IF EXISTS v9_base;
CREATE TEMP TABLE v9_base AS
WITH wet AS (
    SELECT w.window_ts, w.eval_offset,
           (w.surface_json->>'probability_lgb')::float           AS p_v9,
           (w.surface_json->>'v4_regime')                        AS v4_regime
      FROM window_evaluation_traces w
     WHERE w.created_at > NOW() - INTERVAL '87 hours'
       AND w.asset='BTC' AND w.timeframe='5m'
       AND (w.surface_json->>'probability_lgb') IS NOT NULL
)
SELECT t.window_ts, t.eval_offset, t.p_v9, t.v4_regime,
       se.outcome,
       se.regime               AS vpin_regime,
       se.twap_gamma_agree,
       se.clob_up_ask, se.clob_down_ask,
       CASE WHEN t.p_v9 >= 0.5 THEN 'UP' ELSE 'DOWN' END AS dir,
       ABS(t.p_v9 - 0.5)       AS dist_v9,
       CASE WHEN t.eval_offset BETWEEN 24  AND 60   THEN 'T-24-60'
            WHEN t.eval_offset BETWEEN 61  AND 120  THEN 'T-61-120'
            WHEN t.eval_offset BETWEEN 121 AND 180  THEN 'T-121-180'
            WHEN t.eval_offset BETWEEN 181 AND 240  THEN 'T-181-240'
            ELSE 'other' END   AS tband,
       CASE WHEN t.p_v9 >= 0.5 THEN se.clob_up_ask
            ELSE se.clob_down_ask END AS fill
  FROM wet t
  JOIN signal_evaluations se
    ON se.window_ts = t.window_ts
   AND se.eval_offset = t.eval_offset
   AND se.asset = 'BTC' AND se.timeframe = '5m'
 WHERE se.outcome IN ('UP','DOWN');

CREATE INDEX ON v9_base (window_ts, dir, eval_offset DESC);

-- Per-window dedup (one entry per window/direction at conviction >= 0.10,
-- using LATEST eval_offset = earliest-in-window evaluation).
DROP TABLE IF EXISTS v9_fires;
CREATE TEMP TABLE v9_fires AS
SELECT DISTINCT ON (window_ts, dir) *
  FROM v9_base
 WHERE dist_v9 >= 0.10
   AND fill BETWEEN 0.05 AND 0.95
   AND tband != 'other'
 ORDER BY window_ts, dir, eval_offset DESC;

-- 3-tick confirmation: per (window_ts, dir, threshold), require >=3 consecutive
-- eval_offset rows passing dist >= threshold AND same direction. Take the
-- EARLIEST passing eval_offset of the run (closest to the engine entry latch).
DROP TABLE IF EXISTS v9_3tick;
CREATE TEMP TABLE v9_3tick AS
WITH ordered AS (
    SELECT b.*,
           LAG(eval_offset, 1) OVER w AS lag1,
           LAG(eval_offset, 2) OVER w AS lag2,
           LAG(dir, 1)         OVER w AS lag1_dir,
           LAG(dir, 2)         OVER w AS lag2_dir,
           LAG(dist_v9, 1)     OVER w AS lag1_dist,
           LAG(dist_v9, 2)     OVER w AS lag2_dist
      FROM v9_base b
      WHERE b.fill BETWEEN 0.05 AND 0.95
      WINDOW w AS (PARTITION BY window_ts, dir ORDER BY eval_offset DESC)
), confirmed AS (
    -- A row "confirms" if itself + previous 2 LAGged rows (also same window/dir)
    -- all have dist_v9 >= 0.10 (base threshold); we'll filter by stricter
    -- thresholds in downstream queries.
    SELECT *
      FROM ordered
     WHERE dist_v9 >= 0.10
       AND lag1_dist IS NOT NULL AND lag1_dist >= 0.10 AND lag1_dir = dir
       AND lag2_dist IS NOT NULL AND lag2_dist >= 0.10 AND lag2_dir = dir
       AND eval_offset < lag1 AND lag1 < lag2  -- monotonic descending offset
)
SELECT DISTINCT ON (window_ts, dir) *
  FROM confirmed
 WHERE tband != 'other'
 ORDER BY window_ts, dir, eval_offset DESC;

\echo
\echo == Sample sizes ==
SELECT 'v9_base' AS rel, COUNT(*) FROM v9_base
UNION ALL SELECT 'v9_fires (1-tick dist>=0.10)', COUNT(*) FROM v9_fires
UNION ALL SELECT 'v9_3tick (3-tick dist>=0.10)', COUNT(*) FROM v9_3tick
UNION ALL SELECT 'distinct windows', COUNT(DISTINCT window_ts) FROM v9_base;

-- ─────────────────────────────────────────────────────────────────
-- (A) Top alpha cells: T-band x direction x vpin_regime
-- ─────────────────────────────────────────────────────────────────
\echo
\echo == (A) v9 1-tick: tband x dir x vpin_regime (n>=10, ranked by Wilson_low * sqrt(n)) ==
WITH agg AS (
    SELECT tband, dir, vpin_regime,
           COUNT(*) AS n,
           SUM(CASE WHEN dir = outcome THEN 1 ELSE 0 END) AS wins,
           AVG(fill) AS avg_fill,
           SUM(CASE WHEN dir = outcome THEN ((1 - fill) * (7.50 / fill) - 0.072 * 7.50)
                    ELSE -7.50 END) AS real_pnl
      FROM v9_fires
     WHERE vpin_regime IS NOT NULL
     GROUP BY 1,2,3
), wilson AS (
    SELECT *,
           wins::float/n AS wr,
           -- Wilson 95% lower bound
           ((wins::float/n) + (1.96*1.96)/(2.0*n)
              - 1.96 * SQRT(((wins::float/n)*(1.0-wins::float/n) + (1.96*1.96)/(4.0*n)) / n))
              / (1.0 + (1.96*1.96)/n) AS w_low,
           ((wins::float/n) + (1.96*1.96)/(2.0*n)
              + 1.96 * SQRT(((wins::float/n)*(1.0-wins::float/n) + (1.96*1.96)/(4.0*n)) / n))
              / (1.0 + (1.96*1.96)/n) AS w_high
      FROM agg
)
SELECT tband, dir, vpin_regime,
       n, wins,
       ROUND((wr*100)::numeric, 1) AS wr,
       ROUND((w_low*100)::numeric, 1) AS w_low,
       ROUND((w_high*100)::numeric, 1) AS w_high,
       ROUND(avg_fill::numeric, 3) AS avg_fill,
       ROUND(real_pnl::numeric, 2) AS real_pnl,
       ROUND((w_low * SQRT(n))::numeric, 2) AS quality
  FROM wilson
 WHERE n >= 10
 ORDER BY quality DESC LIMIT 30;

-- ─────────────────────────────────────────────────────────────────
-- (B) Conviction ladder per (T-band x dir x vpin_regime)
-- ─────────────────────────────────────────────────────────────────
\echo
\echo == (B) Conviction-band ladder (dist tier x tband x dir x vpin_regime, n>=10) ==
WITH banded AS (
    SELECT tband, dir, vpin_regime,
           CASE WHEN dist_v9 >= 0.30 THEN '4-dist>=0.30'
                WHEN dist_v9 >= 0.20 THEN '3-dist[0.20-0.30)'
                WHEN dist_v9 >= 0.15 THEN '2-dist[0.15-0.20)'
                ELSE                       '1-dist[0.10-0.15)' END AS conv_band,
           outcome, fill
      FROM v9_fires
     WHERE vpin_regime IS NOT NULL
), agg AS (
    SELECT tband, dir, vpin_regime, conv_band,
           COUNT(*) AS n,
           SUM(CASE WHEN dir = outcome THEN 1 ELSE 0 END) AS wins,
           AVG(fill) AS avg_fill,
           SUM(CASE WHEN dir = outcome THEN ((1 - fill) * (7.50 / fill) - 0.072 * 7.50)
                    ELSE -7.50 END) AS real_pnl
      FROM banded GROUP BY 1,2,3,4
)
SELECT tband, dir, vpin_regime, conv_band, n, wins,
       ROUND((100.0 * wins / n)::numeric, 1) AS wr,
       ROUND(avg_fill::numeric, 3) AS avg_fill,
       ROUND(real_pnl::numeric, 2) AS real_pnl
  FROM agg WHERE n >= 10
 ORDER BY tband, dir, vpin_regime, conv_band;

-- ─────────────────────────────────────────────────────────────────
-- (C) 3-tick confirmation impact (1-tick fires vs 3-tick fires)
-- ─────────────────────────────────────────────────────────────────
\echo
\echo == (C) 1-tick vs 3-tick confirmation comparison ==
WITH onetick AS (
    SELECT tband, dir, vpin_regime,
           COUNT(*) AS n,
           SUM(CASE WHEN dir=outcome THEN 1 ELSE 0 END) AS wins,
           AVG(fill) AS avg_fill,
           SUM(CASE WHEN dir=outcome THEN ((1-fill)*(7.50/fill)-0.072*7.50)
                    ELSE -7.50 END) AS real_pnl
      FROM v9_fires WHERE vpin_regime IS NOT NULL
     GROUP BY 1,2,3
), threetick AS (
    SELECT tband, dir, vpin_regime,
           COUNT(*) AS n,
           SUM(CASE WHEN dir=outcome THEN 1 ELSE 0 END) AS wins,
           AVG(fill) AS avg_fill,
           SUM(CASE WHEN dir=outcome THEN ((1-fill)*(7.50/fill)-0.072*7.50)
                    ELSE -7.50 END) AS real_pnl
      FROM v9_3tick WHERE vpin_regime IS NOT NULL
     GROUP BY 1,2,3
)
SELECT
    COALESCE(o.tband, t.tband) AS tband,
    COALESCE(o.dir, t.dir) AS dir,
    COALESCE(o.vpin_regime, t.vpin_regime) AS vpin_regime,
    o.n AS n_1t, o.wins AS w_1t,
    ROUND((100.0 * o.wins / NULLIF(o.n,0))::numeric, 1) AS wr_1t,
    ROUND(o.real_pnl::numeric, 2) AS pnl_1t,
    t.n AS n_3t, t.wins AS w_3t,
    ROUND((100.0 * t.wins / NULLIF(t.n,0))::numeric, 1) AS wr_3t,
    ROUND(t.real_pnl::numeric, 2) AS pnl_3t,
    ROUND(((100.0*t.wins/NULLIF(t.n,0)) - (100.0*o.wins/NULLIF(o.n,0)))::numeric, 1) AS wr_delta_pp
  FROM onetick o FULL OUTER JOIN threetick t USING (tband, dir, vpin_regime)
 WHERE COALESCE(o.n, 0) >= 10 OR COALESCE(t.n, 0) >= 10
 ORDER BY pnl_1t DESC NULLS LAST LIMIT 30;

-- ─────────────────────────────────────────────────────────────────
-- (D) v4_regime axis (sourced from surface_json — overcomes ws.regime gap)
-- ─────────────────────────────────────────────────────────────────
\echo
\echo == (D) v4_regime x dir x vpin_regime (n>=10) ==
WITH agg AS (
    SELECT v4_regime, dir, vpin_regime,
           COUNT(*) AS n,
           SUM(CASE WHEN dir=outcome THEN 1 ELSE 0 END) AS wins,
           AVG(fill) AS avg_fill,
           SUM(CASE WHEN dir=outcome THEN ((1-fill)*(7.50/fill)-0.072*7.50)
                    ELSE -7.50 END) AS real_pnl
      FROM v9_fires
     WHERE vpin_regime IS NOT NULL AND v4_regime IS NOT NULL
     GROUP BY 1,2,3
)
SELECT v4_regime, dir, vpin_regime, n, wins,
       ROUND((100.0*wins/n)::numeric, 1) AS wr,
       ROUND(avg_fill::numeric, 3) AS avg_fill,
       ROUND(real_pnl::numeric, 2) AS real_pnl
  FROM agg WHERE n >= 10
 ORDER BY real_pnl DESC LIMIT 25;

-- ─────────────────────────────────────────────────────────────────
-- (E) Head-to-head with v10 and v12 on CASCADE x DOWN x T-181-240
-- ─────────────────────────────────────────────────────────────────
\echo
\echo == (E) CASCADE x DOWN x T-181-240 head-to-head v9 vs v10 vs v12 ==
WITH wet AS (
    SELECT w.window_ts, w.eval_offset,
           (w.surface_json->>'probability_lgb')::float       AS p_v9,
           (w.surface_json->>'probability_lgb_v10')::float   AS p_v10,
           (w.surface_json->>'probability_lgb_v12')::float   AS p_v12
      FROM window_evaluation_traces w
     WHERE w.created_at > NOW() - INTERVAL '87 hours'
       AND w.asset='BTC' AND w.timeframe='5m'
), joined AS (
    SELECT t.*, se.outcome, se.regime AS vpin_regime,
           se.clob_up_ask, se.clob_down_ask,
           CASE WHEN t.eval_offset BETWEEN 181 AND 240 THEN 'T-181-240'
                ELSE 'other' END AS tband
      FROM wet t
      JOIN signal_evaluations se ON se.window_ts=t.window_ts AND se.eval_offset=t.eval_offset
       AND se.asset='BTC' AND se.timeframe='5m'
     WHERE se.outcome IN ('UP','DOWN')
), fires AS (
    SELECT 'v9'  AS sig, window_ts, tband, vpin_regime, eval_offset, outcome,
           CASE WHEN p_v9>=0.5 THEN 'UP' ELSE 'DOWN' END AS dir,
           CASE WHEN p_v9>=0.5 THEN clob_up_ask ELSE clob_down_ask END AS fill
      FROM joined WHERE p_v9 IS NOT NULL AND ABS(p_v9-0.5) >= 0.10
    UNION ALL
    SELECT 'v10', window_ts, tband, vpin_regime, eval_offset, outcome,
           CASE WHEN p_v10>=0.5 THEN 'UP' ELSE 'DOWN' END,
           CASE WHEN p_v10>=0.5 THEN clob_up_ask ELSE clob_down_ask END
      FROM joined WHERE p_v10 IS NOT NULL AND ABS(p_v10-0.5) >= 0.10
    UNION ALL
    SELECT 'v12', window_ts, tband, vpin_regime, eval_offset, outcome,
           CASE WHEN p_v12>=0.5 THEN 'UP' ELSE 'DOWN' END,
           CASE WHEN p_v12>=0.5 THEN clob_up_ask ELSE clob_down_ask END
      FROM joined WHERE p_v12 IS NOT NULL AND ABS(p_v12-0.5) >= 0.10
), deduped AS (
    SELECT DISTINCT ON (sig, window_ts, dir) *
      FROM fires WHERE fill BETWEEN 0.05 AND 0.95
       AND tband='T-181-240' AND vpin_regime='CASCADE' AND dir='DOWN'
     ORDER BY sig, window_ts, dir, eval_offset DESC
)
SELECT sig, COUNT(*) AS n,
       SUM(CASE WHEN dir=outcome THEN 1 ELSE 0 END) AS wins,
       ROUND(100.0*SUM(CASE WHEN dir=outcome THEN 1 ELSE 0 END)/COUNT(*)::numeric, 1) AS wr,
       ROUND(AVG(fill)::numeric, 3) AS avg_fill,
       ROUND(SUM(CASE WHEN dir=outcome THEN ((1-fill)*(7.50/fill)-0.072*7.50)
                      ELSE -7.50 END)::numeric, 2) AS real_pnl
  FROM deduped GROUP BY 1 ORDER BY 1;

-- ─────────────────────────────────────────────────────────────────
-- (F) twap_gamma_agree side-channel (does it lift WR?)
-- ─────────────────────────────────────────────────────────────────
\echo
\echo == (F) twap_gamma_agree gate impact on top vpin_regime cells ==
WITH agg AS (
    SELECT tband, dir, vpin_regime,
           COALESCE(twap_gamma_agree::text, 'unknown') AS twap_agree,
           COUNT(*) AS n,
           SUM(CASE WHEN dir=outcome THEN 1 ELSE 0 END) AS wins,
           AVG(fill) AS avg_fill,
           SUM(CASE WHEN dir=outcome THEN ((1-fill)*(7.50/fill)-0.072*7.50)
                    ELSE -7.50 END) AS real_pnl
      FROM v9_fires WHERE vpin_regime IS NOT NULL
     GROUP BY 1,2,3,4
)
SELECT tband, dir, vpin_regime, twap_agree, n, wins,
       ROUND((100.0*wins/n)::numeric, 1) AS wr,
       ROUND(avg_fill::numeric, 3) AS avg_fill,
       ROUND(real_pnl::numeric, 2) AS real_pnl
  FROM agg WHERE n >= 10
 ORDER BY real_pnl DESC LIMIT 25;

-- ─────────────────────────────────────────────────────────────────
-- (G) Top 10 v9 alpha cells (4-axis: tband x dir x vpin_regime x conv_band)
-- ─────────────────────────────────────────────────────────────────
\echo
\echo == (G) TOP 10 v9 ALPHA CELLS — 4-axis quality ranking ==
WITH banded AS (
    SELECT tband, dir, vpin_regime,
           CASE WHEN dist_v9 >= 0.30 THEN '4-dist>=0.30'
                WHEN dist_v9 >= 0.20 THEN '3-dist[0.20-0.30)'
                WHEN dist_v9 >= 0.15 THEN '2-dist[0.15-0.20)'
                ELSE                       '1-dist[0.10-0.15)' END AS conv_band,
           outcome, fill
      FROM v9_fires WHERE vpin_regime IS NOT NULL
), agg AS (
    SELECT tband, dir, vpin_regime, conv_band,
           COUNT(*) AS n,
           SUM(CASE WHEN dir=outcome THEN 1 ELSE 0 END) AS wins,
           AVG(fill) AS avg_fill,
           SUM(CASE WHEN dir=outcome THEN ((1-fill)*(7.50/fill)-0.072*7.50)
                    ELSE -7.50 END) AS real_pnl
      FROM banded GROUP BY 1,2,3,4
), wilson AS (
    SELECT *,
           wins::float/n AS wr,
           ((wins::float/n) + (1.96*1.96)/(2.0*n)
              - 1.96 * SQRT(((wins::float/n)*(1.0-wins::float/n) + (1.96*1.96)/(4.0*n)) / n))
              / (1.0 + (1.96*1.96)/n) AS w_low
      FROM agg WHERE n >= 10
)
SELECT tband, dir, vpin_regime, conv_band, n, wins,
       ROUND((wr*100)::numeric, 1) AS wr,
       ROUND((w_low*100)::numeric, 1) AS w_low,
       ROUND(avg_fill::numeric, 3) AS avg_fill,
       ROUND(real_pnl::numeric, 2) AS real_pnl,
       ROUND((w_low * SQRT(n))::numeric, 2) AS quality
  FROM wilson
 ORDER BY quality DESC LIMIT 10;

\echo
\echo == END ==
