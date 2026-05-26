#!/bin/bash
# =============================================================================
# sweep_probabilities.sh — definitive probability column sweep (production method)
# =============================================================================
# Replaces broken haiku #1 + haiku #2 sweeps. Uses canonical methodology from
# RDS note #684 (the 4 traps). One source of truth for all probability sweeps.
#
# USAGE:
#   ./scripts/analysis/sweep_probabilities.sh [OPTIONS]
#
# OPTIONS:
#   --hours N            Lookback in hours (default 24)
#   --asset BTC|ETH|XRP  Filter to one asset (default: all)
#   --since-utc TS       Override start cutoff (e.g. '2026-05-25 13:51:00+00')
#   --until-utc TS       Override end cutoff
#   --min-n N            Min resolved fires for inclusion (default 10)
#   --min-wr N           Min WR % for inclusion (default 75)
#   --include-15m        Also sweep 15m classifier data from strategy_decisions
#   --csv FILE           Save output to CSV (also prints to stdout)
#   --quiet              Just CSV, no table output
#
# EXAMPLES:
#   # Standard 24h sweep across all assets, default filters
#   ./scripts/analysis/sweep_probabilities.sh
#
#   # Last 2h post-deploy ETH only
#   ./scripts/analysis/sweep_probabilities.sh --asset ETH --hours 2
#
#   # Pre/post deploy comparison for v9.5 ETH
#   ./scripts/analysis/sweep_probabilities.sh --asset ETH --since-utc '2026-05-25 13:51:00+00'
#
#   # Include 15m classifier data
#   ./scripts/analysis/sweep_probabilities.sh --hours 24 --include-15m
#
# METHODOLOGY (per RDS note #684):
#   1. DISTINCT ON (window_ts) ORDER BY window_ts, eval_offset DESC
#      = picks LARGEST eval_offset = EARLIEST in time = first qualifying tick
#      = matches production strategy fire behavior
#   2. Operating-point filter applied INSIDE the CTE (before DISTINCT ON)
#   3. LEFT JOIN to market_data + window_snapshots (COALESCE for backfilled truth)
#   4. Window-level dedup, NOT tick-level (which inflates counts)
#
# Connect via canonical lib (not .env which is stale Railway):
#   source scripts/cross-compare/_lib.sh
#
# =============================================================================

set -euo pipefail

# Defaults
HOURS=24
ASSET=""
SINCE_UTC=""
UNTIL_UTC=""
MIN_N=10
MIN_WR=75
INCLUDE_15M=0
CSV_FILE=""
QUIET=0

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --hours)        HOURS="$2"; shift 2 ;;
    --asset)        ASSET="$2"; shift 2 ;;
    --since-utc)    SINCE_UTC="$2"; shift 2 ;;
    --until-utc)    UNTIL_UTC="$2"; shift 2 ;;
    --min-n)        MIN_N="$2"; shift 2 ;;
    --min-wr)       MIN_WR="$2"; shift 2 ;;
    --include-15m)  INCLUDE_15M=1; shift ;;
    --csv)          CSV_FILE="$2"; shift 2 ;;
    --quiet)        QUIET=1; shift ;;
    -h|--help)      sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Build time filter clause
if [[ -n "$SINCE_UTC" ]]; then
  TIME_CLAUSE="evaluated_at >= '$SINCE_UTC'"
  [[ -n "$UNTIL_UTC" ]] && TIME_CLAUSE="$TIME_CLAUSE AND evaluated_at < '$UNTIL_UTC'"
else
  TIME_CLAUSE="evaluated_at > now() - interval '$HOURS hours'"
fi

# Asset filter
if [[ -n "$ASSET" ]]; then
  ASSET_LIST="('$ASSET')"
else
  ASSET_LIST="('BTC','ETH','XRP')"
fi

# Load DB credentials
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/cross-compare/_lib.sh" 2>/dev/null
export PGPASSWORD="$DB_PASS"

# =============================================================================
# Build the unified sweep SQL
# =============================================================================
SQL=$(cat <<SQLEOF
SET statement_timeout = '180s';

-- Per-column ff CTEs (asset-aware)
WITH
ff_btc AS (
  SELECT * FROM (SELECT DISTINCT ON (window_ts) 'BTC' AS asset, 'v9_1' AS col, window_ts, probability_lgb_v9_1::float AS p FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v9_1 IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND $TIME_CLAUSE ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'BTC', 'v9_2', window_ts, probability_lgb_v9_2::float FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v9_2 IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND $TIME_CLAUSE ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'BTC', 'v9_2_pure', window_ts, probability_lgb_v9_2_pure::float FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v9_2_pure IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND $TIME_CLAUSE ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'BTC', 'v9_2_post_iso', window_ts, probability_lgb_v9_2_post_iso::float FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v9_2_post_iso IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND $TIME_CLAUSE ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'BTC', 'v9_3_btc', window_ts, probability_lgb_v9_3_btc::float FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v9_3_btc IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND $TIME_CLAUSE ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'BTC', 'v9_3_btc_pure', window_ts, probability_lgb_v9_3_btc_pure::float FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v9_3_btc_pure IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND $TIME_CLAUSE ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'BTC', 'v12', window_ts, probability_lgb_v12::float FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v12 IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND $TIME_CLAUSE ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'BTC', 'v12_pure', window_ts, probability_lgb_v12_pure::float FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_lgb_v12_pure IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND $TIME_CLAUSE ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'BTC', 'v12_meta_gate', window_ts, probability_v12_meta_gate::float FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_v12_meta_gate IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND $TIME_CLAUSE ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'BTC', 'v9_2_meta_gate', window_ts, probability_v9_2_meta_gate::float FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_v9_2_meta_gate IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND $TIME_CLAUSE ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'BTC', 'v2_meta_gate', window_ts, probability_v2_meta_gate::float FROM signal_evaluations WHERE asset='BTC' AND timeframe='5m' AND probability_v2_meta_gate IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND $TIME_CLAUSE ORDER BY window_ts, eval_offset DESC) x
),
ff_eth AS (
  SELECT * FROM (SELECT DISTINCT ON (window_ts) 'ETH' AS asset, 'v9_2_eth' AS col, window_ts, probability_lgb_v9_2_eth::float AS p FROM signal_evaluations WHERE asset='ETH' AND timeframe='5m' AND probability_lgb_v9_2_eth IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND $TIME_CLAUSE ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'ETH', 'v9_5_eth', window_ts, probability_lgb_v9_5_eth::float FROM signal_evaluations WHERE asset='ETH' AND timeframe='5m' AND probability_lgb_v9_5_eth IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND $TIME_CLAUSE ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'ETH', 'v9_5_eth_pure', window_ts, probability_lgb_v9_5_eth_pure::float FROM signal_evaluations WHERE asset='ETH' AND timeframe='5m' AND probability_lgb_v9_5_eth_pure IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND $TIME_CLAUSE ORDER BY window_ts, eval_offset DESC) x
),
ff_xrp AS (
  SELECT * FROM (SELECT DISTINCT ON (window_ts) 'XRP' AS asset, 'v9_5_xrp' AS col, window_ts, probability_lgb_v9_5_xrp::float AS p FROM signal_evaluations WHERE asset='XRP' AND timeframe='5m' AND probability_lgb_v9_5_xrp IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND $TIME_CLAUSE ORDER BY window_ts, eval_offset DESC) x
  UNION ALL SELECT * FROM (SELECT DISTINCT ON (window_ts) 'XRP', 'v9_2_xrp', window_ts, probability_lgb_v9_2_xrp::float FROM signal_evaluations WHERE asset='XRP' AND timeframe='5m' AND probability_lgb_v9_2_xrp IS NOT NULL AND eval_offset BETWEEN 60 AND 180 AND $TIME_CLAUSE ORDER BY window_ts, eval_offset DESC) x
),
all_ff AS (
  SELECT * FROM ff_btc WHERE 'BTC' IN $ASSET_LIST
  UNION ALL SELECT * FROM ff_eth WHERE 'ETH' IN $ASSET_LIST
  UNION ALL SELECT * FROM ff_xrp WHERE 'XRP' IN $ASSET_LIST
),
gw AS (
  SELECT ff.asset, ff.col, ff.window_ts, ff.p,
         COALESCE(md.outcome, ws_o) AS truth
    FROM all_ff ff
    LEFT JOIN market_data md ON md.window_ts=ff.window_ts AND md.asset=ff.asset AND md.timeframe='5m'
    LEFT JOIN LATERAL (
      SELECT outcome AS ws_o FROM window_snapshots ws
       WHERE ws.window_ts=ff.window_ts AND ws.asset=ff.asset AND ws.timeframe='5m' AND outcome IS NOT NULL LIMIT 1
    ) wsx ON TRUE
)
-- UP sweep at 0.02 granularity
SELECT asset, col, 'UP' AS direction, ROUND(thr::numeric, 2) AS thr,
       COUNT(*) FILTER (WHERE p >= thr) AS fires,
       COUNT(*) FILTER (WHERE p >= thr AND truth IN ('UP','DOWN')) AS resolved,
       COUNT(*) FILTER (WHERE p >= thr AND truth='UP') AS wins,
       ROUND(100.0*COUNT(*) FILTER (WHERE p >= thr AND truth='UP')::numeric
             / NULLIF(COUNT(*) FILTER (WHERE p >= thr AND truth IN ('UP','DOWN')),0), 1) AS wr_pct
  FROM gw CROSS JOIN (SELECT generate_series(50,100,2)::float/100 AS thr) t
 GROUP BY asset, col, thr
HAVING COUNT(*) FILTER (WHERE p >= thr AND truth IN ('UP','DOWN')) >= $MIN_N
   AND ROUND(100.0*COUNT(*) FILTER (WHERE p >= thr AND truth='UP')::numeric
             / NULLIF(COUNT(*) FILTER (WHERE p >= thr AND truth IN ('UP','DOWN')),0), 1) >= $MIN_WR
UNION ALL
-- DOWN sweep at 0.02 granularity
SELECT asset, col, 'DOWN', ROUND(thr::numeric, 2),
       COUNT(*) FILTER (WHERE p <= thr) AS fires,
       COUNT(*) FILTER (WHERE p <= thr AND truth IN ('UP','DOWN')) AS resolved,
       COUNT(*) FILTER (WHERE p <= thr AND truth='DOWN') AS wins,
       ROUND(100.0*COUNT(*) FILTER (WHERE p <= thr AND truth='DOWN')::numeric
             / NULLIF(COUNT(*) FILTER (WHERE p <= thr AND truth IN ('UP','DOWN')),0), 1) AS wr_pct
  FROM gw CROSS JOIN (SELECT generate_series(0,50,2)::float/100 AS thr) t
 GROUP BY asset, col, thr
HAVING COUNT(*) FILTER (WHERE p <= thr AND truth IN ('UP','DOWN')) >= $MIN_N
   AND ROUND(100.0*COUNT(*) FILTER (WHERE p <= thr AND truth='DOWN')::numeric
             / NULLIF(COUNT(*) FILTER (WHERE p <= thr AND truth IN ('UP','DOWN')),0), 1) >= $MIN_WR
ORDER BY wr_pct DESC, fires DESC;
SQLEOF
)

# =============================================================================
# 15m classifier sweep (separate — data lives in strategy_decisions.metadata_json)
# =============================================================================
SQL_15M=$(cat <<'SQLEOF'
SET statement_timeout = '180s';

-- 15m classifier strategies emit probability_classifier into metadata_json.
-- Window-level dedup + LEFT JOIN to market_data for outcome.
WITH ff AS (
  SELECT DISTINCT ON (sd.strategy_id, sd.timeframe,
                      COALESCE((md_join.window_ts)::text, sd.evaluated_at::text))
         sd.strategy_id,
         sd.timeframe,
         w.window_ts,
         (sd.metadata->>'probability_classifier')::float AS p_classifier,
         sd.metadata->>'asset' AS asset
    FROM strategy_decisions sd
    LEFT JOIN window_snapshots w ON w.id = sd.window_id
    LEFT JOIN LATERAL (SELECT 1 AS dummy, w.window_ts) md_join ON TRUE
   WHERE sd.timeframe = '15m'
     AND sd.strategy_id LIKE '%15m_classifier%'
     AND sd.action = 'TRADE'
     AND sd.evaluated_at > now() - interval '24 hours'
     AND sd.metadata ? 'probability_classifier'
     AND (sd.metadata->>'probability_classifier')::float IS NOT NULL
   ORDER BY sd.strategy_id, sd.timeframe, COALESCE((md_join.window_ts)::text, sd.evaluated_at::text), sd.evaluated_at DESC
),
gw AS (
  SELECT ff.*,
         COALESCE(md.outcome, ws_o) AS truth
    FROM ff
    LEFT JOIN market_data md ON md.window_ts=ff.window_ts AND md.asset=ff.asset AND md.timeframe='15m'
    LEFT JOIN LATERAL (
      SELECT outcome AS ws_o FROM window_snapshots ws
       WHERE ws.window_ts=ff.window_ts AND ws.asset=ff.asset AND ws.timeframe='15m' AND outcome IS NOT NULL LIMIT 1
    ) wsx ON TRUE
)
SELECT strategy_id,
       asset,
       ROUND(thr::numeric, 2) AS conf,
       COUNT(*) FILTER (WHERE p_classifier >= thr) AS fires,
       COUNT(*) FILTER (WHERE p_classifier >= thr AND truth IN ('UP','DOWN')) AS resolved,
       COUNT(*) FILTER (WHERE p_classifier >= thr AND truth='UP') AS up_wins,
       COUNT(*) FILTER (WHERE p_classifier >= thr AND truth='DOWN') AS down_wins,
       ROUND(100.0*COUNT(*) FILTER (WHERE p_classifier >= thr AND truth='UP')::numeric
             / NULLIF(COUNT(*) FILTER (WHERE p_classifier >= thr AND truth IN ('UP','DOWN')),0), 1) AS up_wr_pct
  FROM gw CROSS JOIN (VALUES (0.15),(0.20),(0.25),(0.30),(0.35),(0.40),(0.45),(0.50)) AS t(thr)
 WHERE asset IS NOT NULL
 GROUP BY strategy_id, asset, thr
HAVING COUNT(*) FILTER (WHERE p_classifier >= thr AND truth IN ('UP','DOWN')) >= 10
 ORDER BY strategy_id, asset, conf;
SQLEOF
)

# =============================================================================
# Execute
# =============================================================================
if [[ $QUIET -eq 0 ]]; then
  echo "==============================================================="
  echo "=== sweep_probabilities.sh — production-realistic methodology ==="
  echo "==============================================================="
  echo "Time clause:  $TIME_CLAUSE"
  echo "Asset filter: $ASSET_LIST"
  echo "Min n:        $MIN_N"
  echo "Min WR%:      $MIN_WR"
  echo "Include 15m:  $INCLUDE_15M"
  echo ""
  echo "--- 5m probability columns (signal_evaluations) ---"
fi

OUTPUT=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -P pager=off -c "$SQL")

if [[ $QUIET -eq 0 ]]; then
  echo "$OUTPUT"
fi

if [[ -n "$CSV_FILE" ]]; then
  psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" --csv -c "$SQL" > "$CSV_FILE"
  echo "Saved CSV: $CSV_FILE"
fi

if [[ $INCLUDE_15M -eq 1 ]]; then
  echo ""
  echo "--- 15m classifier (strategy_decisions.metadata_json.probability_classifier) ---"
  psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -P pager=off -c "$SQL_15M"
fi
