#!/bin/bash
# v9_vs_v91_pair.sh — direct A/B comparison of every v9-anchored strategy
# vs its v9.1 mirror. Computes the pp lift of v9.1 over v9 PROD.
#
# Usage:  bash v9_vs_v91_pair.sh [days=7]
#
# Promotion rules:
#   - v91_lift_pp >= +3pp AND v91_trades >= 30 → flip v9.1 LIVE, demote v9
#   - v91_lift_pp in [-3, +3] → both stay ghost (or LIVE both as portfolio)
#   - v91_lift_pp <= -3pp → keep v9 LIVE, investigate v9.1 retrain

source "$(dirname "$0")/_lib.sh"

run_psql "
WITH per_strategy AS (
  SELECT strategy_id,
         COUNT(*) AS trades,
         ROUND(100.0 * COUNT(*) FILTER (WHERE sd.direction = md.outcome)
                       / NULLIF(COUNT(*), 0), 1) AS wr
  FROM strategy_decisions sd
  JOIN market_data md USING (asset, timeframe, window_ts)
  WHERE sd.evaluated_at > NOW() - INTERVAL '${DAYS} days'
    AND sd.action = 'TRADE'
    AND md.resolved = TRUE
    AND strategy_id IN (
      'v_v9_strong_up_btc_5m',     'v_v9_1_strong_up_btc_5m',
      'v_v9_strong_dn_btc_5m',     'v_v9_1_strong_dn_btc_5m',
      'v9_lgb_only',               'v9_1_lgb_only',
      'v9_cascade_fade_late',      'v9_1_cascade_fade_late'
    )
  GROUP BY strategy_id
),
paired AS (
  SELECT
    REPLACE(REPLACE(strategy_id, 'v_v9_1_', '##'), 'v_v9_', '') AS pair_key,
    REPLACE(REPLACE(strategy_id, 'v9_1_', '##'), 'v9_', '') AS pair_key_alt,
    strategy_id,
    trades,
    wr,
    strategy_id LIKE '%v9_1%' OR strategy_id LIKE '%v9_1\\_%' AS is_v91
  FROM per_strategy
)
SELECT
  COALESCE(NULLIF(pair_key, strategy_id), pair_key_alt) AS pair,
  MAX(CASE WHEN is_v91     THEN wr     END) AS v91_wr,
  MAX(CASE WHEN is_v91     THEN trades END) AS v91_trades,
  MAX(CASE WHEN NOT is_v91 THEN wr     END) AS v9_wr,
  MAX(CASE WHEN NOT is_v91 THEN trades END) AS v9_trades,
  ROUND(MAX(CASE WHEN is_v91 THEN wr END)
        - MAX(CASE WHEN NOT is_v91 THEN wr END), 1) AS v91_lift_pp,
  CASE
    WHEN MAX(CASE WHEN is_v91 THEN trades END) < 30
      OR MAX(CASE WHEN NOT is_v91 THEN trades END) < 30
    THEN 'soak'
    WHEN ROUND(MAX(CASE WHEN is_v91 THEN wr END)
               - MAX(CASE WHEN NOT is_v91 THEN wr END), 1) >= 3.0
    THEN 'PROMOTE_V91'
    WHEN ROUND(MAX(CASE WHEN is_v91 THEN wr END)
               - MAX(CASE WHEN NOT is_v91 THEN wr END), 1) <= -3.0
    THEN 'KEEP_V9'
    ELSE 'EQUIVALENT'
  END AS verdict
FROM paired
GROUP BY pair, pair_key_alt, strategy_id
ORDER BY pair;
"
