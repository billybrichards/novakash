#!/bin/bash
# =============================================================================
# shadow_pnl_clob.sh — CLOB-aware GHOST shadow PnL (sister script to sweep_probabilities.sh)
# =============================================================================
# Computes shadow PnL for GHOST (or any) strategies by joining
# strategy_decisions to ticks_clob (actual CLOB asks at decision time) and
# signal_evaluations.outcome (canonical truth). This is meaningfully more
# accurate than assuming fill = entry_cap because:
#   - If CLOB ask > entry_cap at decision time, the FAK ladder would NOT
#     fill → no trade → no PnL.
#   - If CLOB ask ≤ entry_cap, the actual fill is the ask, not the cap.
#   - Wins pay (5/ask - 5 - fee). Losses cost (5 + fee = 5.36). Naive
#     methodology overstates wins (assumes fill at cap, fewer shares) AND
#     ignores fill-failure rate.
#
# Discovered 2026-05-28: naive shadow PnL pointed at multiple "winning"
# GHOSTs that have 0-10% real fill rates under CLOB realities, and
# pointed away from several strats whose actual fills land much cheaper
# than their conservative entry_cap suggests. See RDS note (TODO: file).
#
# USAGE:
#   ./scripts/analysis/shadow_pnl_clob.sh [OPTIONS]
#
# OPTIONS:
#   --hours N            Lookback in hours (default 24)
#   --since-utc TS       Override start cutoff
#   --until-utc TS       Override end cutoff
#   --mode MODE          Filter strategies by mode (GHOST|LIVE|all, default GHOST)
#   --strats LIST        Comma-separated strategy_ids (overrides --mode)
#   --stake USD          Stake per trade (default 5.00)
#   --fee PCT            Fee multiplier (default 0.072 — Polymarket crypto)
#   --min-n N            Minimum resolved decisions for inclusion (default 20)
#   --csv FILE           Save CSV (also prints to stdout)
#   --quiet              CSV only, no table output
#
# EXAMPLES:
#   # Default — all GHOST strats over 24h
#   ./scripts/analysis/shadow_pnl_clob.sh
#
#   # Compare 24h shadow for specific strats
#   ./scripts/analysis/shadow_pnl_clob.sh --strats v9_1_meta_kelly,v9_2_eth_solo,v4_fusion
#
#   # 7d view across LIVE strats
#   ./scripts/analysis/shadow_pnl_clob.sh --hours 168 --mode LIVE
#
# METHODOLOGY:
#   1. Dedup strategy_decisions per (strategy_id, asset, window_ts) keeping
#      the EARLIEST decision (largest eval_offset).
#   2. Join to ticks_clob: find the latest CLOB tick at-or-before the
#      decision's evaluated_at, for the same (asset, window_ts, 5m).
#      Pull up_best_ask (direction=UP) or down_best_ask (direction=DOWN).
#   3. Join to signal_evaluations for the canonical outcome.
#   4. Simulate fill: if clob_ask <= entry_cap, fill at clob_ask; else NULL
#      (no fill, no PnL).
#   5. PnL: WIN = stake/fill - stake - stake*fee; LOSS = -(stake + stake*fee).
#
# Connect via canonical lib (not .env which is stale Railway):
#   source scripts/cross-compare/_lib.sh
#
# =============================================================================

set -euo pipefail

# Defaults
HOURS=24
SINCE_UTC=""
UNTIL_UTC=""
MODE="GHOST"
STRATS=""
STAKE=5.0
FEE=0.072
MIN_N=20
CSV_FILE=""
QUIET=0

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --hours)        HOURS="$2"; shift 2 ;;
    --since-utc)    SINCE_UTC="$2"; shift 2 ;;
    --until-utc)    UNTIL_UTC="$2"; shift 2 ;;
    --mode)         MODE="$2"; shift 2 ;;
    --strats)       STRATS="$2"; shift 2 ;;
    --stake)        STAKE="$2"; shift 2 ;;
    --fee)          FEE="$2"; shift 2 ;;
    --min-n)        MIN_N="$2"; shift 2 ;;
    --csv)          CSV_FILE="$2"; shift 2 ;;
    --quiet)        QUIET=1; shift ;;
    -h|--help)      sed -n '2,55p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Build time filter
if [[ -n "$SINCE_UTC" ]]; then
  TIME_CLAUSE="sd.evaluated_at >= '$SINCE_UTC'"
  [[ -n "$UNTIL_UTC" ]] && TIME_CLAUSE="$TIME_CLAUSE AND sd.evaluated_at < '$UNTIL_UTC'"
else
  TIME_CLAUSE="sd.evaluated_at > now() - interval '$HOURS hours'"
fi

# Build strategy filter
if [[ -n "$STRATS" ]]; then
  # explicit strats list
  STRAT_FILTER="sd.strategy_id IN ('$(echo "$STRATS" | sed "s/,/','/g")')"
elif [[ "$MODE" == "all" ]]; then
  STRAT_FILTER="TRUE"
else
  STRAT_FILTER="sd.strategy_id IN (SELECT strategy_id FROM strategy_runtime_overrides WHERE mode='$MODE')"
fi

# Calculate fee in dollar terms for SQL (5.0 * 0.072 = 0.36)
FEE_DOLLARS=$(awk "BEGIN {print $STAKE * $FEE}")

# Load DB credentials
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/cross-compare/_lib.sh" 2>/dev/null
export PGPASSWORD="$DB_PASS"

# =============================================================================
# Build SQL
# =============================================================================
SQL=$(cat <<SQLEOF
SET statement_timeout = '180s';

WITH shadow AS (
  SELECT DISTINCT ON (sd.strategy_id, sd.asset, sd.window_ts)
    sd.strategy_id, sd.direction, sd.entry_cap, sd.asset, sd.window_ts,
    sd.evaluated_at
  FROM strategy_decisions sd
  WHERE sd.action='TRADE' AND $TIME_CLAUSE AND $STRAT_FILTER
  ORDER BY sd.strategy_id, sd.asset, sd.window_ts, sd.eval_offset DESC
),
with_clob AS (
  SELECT s.*,
    (SELECT CASE WHEN s.direction='UP' THEN c.up_best_ask ELSE c.down_best_ask END
     FROM ticks_clob c
     WHERE c.asset=s.asset AND c.window_ts=s.window_ts AND c.timeframe='5m'
       AND c.ts <= s.evaluated_at
     ORDER BY c.ts DESC LIMIT 1) AS clob_ask,
    (SELECT outcome FROM signal_evaluations se
     WHERE se.asset=s.asset AND se.window_ts=s.window_ts AND se.timeframe='5m' AND se.outcome IS NOT NULL LIMIT 1) AS truth
  FROM shadow s
),
modeled AS (
  SELECT *,
    -- Simulated fill: at clob_ask if affordable, else NULL (no fill).
    CASE WHEN clob_ask IS NOT NULL AND clob_ask <= COALESCE(entry_cap, 0.85)
         THEN clob_ask ELSE NULL END AS sim_fill,
    -- CLOB-aware PnL — counts only filled trades.
    CASE WHEN clob_ask IS NULL OR clob_ask > COALESCE(entry_cap, 0.85) THEN NULL
         WHEN truth=direction THEN (${STAKE}/clob_ask) - ${STAKE} - ${FEE_DOLLARS}
         WHEN truth IS NOT NULL THEN -(${STAKE} + ${FEE_DOLLARS}) END AS clob_pnl,
    -- Naive PnL — assumes fill = entry_cap (overstates fill rate + misprices fill).
    CASE WHEN truth=direction THEN (${STAKE}/COALESCE(entry_cap, 0.85)) - ${STAKE} - ${FEE_DOLLARS}
         WHEN truth IS NOT NULL THEN -(${STAKE} + ${FEE_DOLLARS}) END AS naive_pnl
  FROM with_clob
)
SELECT strategy_id,
  COUNT(*) AS decisions,
  COUNT(*) FILTER (WHERE truth IS NOT NULL) AS resolved,
  COUNT(*) FILTER (WHERE sim_fill IS NOT NULL AND truth IS NOT NULL) AS filled,
  ROUND(100.0*COUNT(*) FILTER (WHERE sim_fill IS NOT NULL AND truth IS NOT NULL)
        / NULLIF(COUNT(*) FILTER (WHERE truth IS NOT NULL),0), 1) AS fill_rate_pct,
  ROUND(100.0*COUNT(*) FILTER (WHERE truth=direction)
        / NULLIF(COUNT(*) FILTER (WHERE truth IN ('UP','DOWN')),0), 1) AS wr_pct,
  ROUND(AVG(clob_ask) FILTER (WHERE sim_fill IS NOT NULL)::numeric, 3) AS avg_clob_fill,
  ROUND(AVG(entry_cap)::numeric, 3) AS avg_cap,
  ROUND(SUM(clob_pnl)::numeric, 2) AS clob_pnl,
  ROUND(SUM(naive_pnl)::numeric, 2) AS naive_pnl
FROM modeled
GROUP BY strategy_id
HAVING COUNT(*) FILTER (WHERE truth IS NOT NULL) >= ${MIN_N}
ORDER BY clob_pnl DESC NULLS LAST;
SQLEOF
)

# =============================================================================
# Execute
# =============================================================================
if [[ $QUIET -eq 0 ]]; then
  echo "==============================================================="
  echo "=== shadow_pnl_clob.sh — CLOB-aware GHOST shadow PnL ==="
  echo "==============================================================="
  echo "Time clause:   $TIME_CLAUSE"
  echo "Strat filter:  $STRAT_FILTER"
  echo "Stake:         \$$STAKE"
  echo "Fee:           $FEE ($FEE_DOLLARS USD per trade)"
  echo "Min n:         $MIN_N (resolved decisions)"
  echo ""
fi

OUTPUT=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -P pager=off -c "$SQL")

if [[ $QUIET -eq 0 ]]; then
  echo "$OUTPUT"
fi

if [[ -n "$CSV_FILE" ]]; then
  psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" --csv -c "$SQL" > "$CSV_FILE"
  echo "Saved CSV: $CSV_FILE"
fi
