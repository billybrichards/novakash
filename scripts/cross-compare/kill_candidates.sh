#!/bin/bash
# kill_candidates.sh — strategies fitting the DISABLED criteria.
# A strategy is a kill candidate if ANY of:
#   - WR < 50% over n >= 30 (consistent under-performance)
#   - daily TRADE count > 50 for 3+ consecutive days (gate too loose)
#   - wr_lower_95 < 45% (high probability of negative edge)
#
# Output: strategies that should be flipped GHOST → DISABLED via
# strategy_runtime_overrides, with the trigger condition in the verdict.

source "$(dirname "$0")/_lib.sh"

run_psql "
WITH per_strategy AS (
  SELECT
    strategy_id, asset, timeframe,
    COUNT(*) AS trades,
    SUM(CASE WHEN sd.direction = md.outcome THEN 1 ELSE 0 END) AS wins,
    ROUND(100.0 * SUM(CASE WHEN sd.direction = md.outcome THEN 1 ELSE 0 END)
                  / NULLIF(COUNT(*), 0), 1) AS wr_pct,
    ROUND(100.0 * (
      (SUM(CASE WHEN sd.direction = md.outcome THEN 1 ELSE 0 END)::float
         / NULLIF(COUNT(*), 0)
         + 1.96 * 1.96 / (2.0 * COUNT(*))
         - 1.96 * sqrt(
             (SUM(CASE WHEN sd.direction = md.outcome THEN 1 ELSE 0 END)::float
                / NULLIF(COUNT(*), 0))
             * (1.0 - SUM(CASE WHEN sd.direction = md.outcome THEN 1 ELSE 0 END)::float
                      / NULLIF(COUNT(*), 0))
             / NULLIF(COUNT(*), 0)
             + 1.96 * 1.96 / (4.0 * COUNT(*) * COUNT(*))
           )
      ) / (1.0 + 1.96 * 1.96 / NULLIF(COUNT(*), 0))
    ), 1) AS wr_lower_95
  FROM strategy_decisions sd
  JOIN market_data md USING (asset, timeframe, window_ts)
  WHERE sd.evaluated_at > NOW() - INTERVAL '${DAYS} days'
    AND sd.action = 'TRADE'
    AND md.resolved = TRUE
  GROUP BY strategy_id, asset, timeframe
)
SELECT
  strategy_id, asset, timeframe, trades, wr_pct, wr_lower_95,
  CASE
    WHEN trades >= 30 AND wr_pct < 50.0      THEN 'KILL_LOW_WR'
    WHEN trades >= 30 AND wr_lower_95 < 45.0 THEN 'KILL_NEG_EDGE'
    WHEN trades / GREATEST(${DAYS}, 1) > 50  THEN 'KILL_TOO_LOOSE'
    ELSE 'ok'
  END AS verdict
FROM per_strategy
WHERE
     (trades >= 30 AND wr_pct < 50.0)
  OR (trades >= 30 AND wr_lower_95 < 45.0)
  OR (trades / GREATEST(${DAYS}, 1) > 50)
ORDER BY wr_pct ASC NULLS FIRST;
"
