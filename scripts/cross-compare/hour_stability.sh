#!/bin/bash
# hour_stability.sh — verify the BTC 15m hour blocklist [0,1,4,10,12,16,18,22]
# from sweep 2b is still holding in recent data. Detects regime shifts.
#
# Reading the output:
#   - "STILL_DEAD" rows: hour is in the blocklist AND has WR < 50% (all good)
#   - "REGIME_SHIFT" rows: hour was dead in the historical sweep but now
#     shows WR >= 65% — consider removing from blocklist
#   - "NEW_DEAD" rows: hour was tradable historically but now <50% — consider
#     adding to blocklist for new strategies

source "$(dirname "$0")/_lib.sh"

# Historical dead hours per sweep 2b
DEAD_HOURS_HIST="0,1,4,10,12,16,18,22"

run_psql "
WITH dedup AS (
  SELECT EXTRACT(HOUR FROM TO_TIMESTAMP(window_ts))::int AS hour_utc,
         window_ts,
         BOOL_OR(direction = (SELECT outcome FROM market_data md
                              WHERE md.window_ts = sd.window_ts
                                AND md.asset = sd.asset
                                AND md.timeframe = sd.timeframe
                              LIMIT 1)) AS won
  FROM strategy_decisions sd
  WHERE evaluated_at > NOW() - INTERVAL '${DAYS} days'
    AND action = 'TRADE'
    AND asset = 'BTC' AND timeframe = '15m'
  GROUP BY hour_utc, window_ts
)
SELECT
  hour_utc,
  COUNT(*) AS unique_windows,
  ROUND(100.0 * SUM(CASE WHEN won THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) AS wr_pct,
  CASE
    WHEN hour_utc IN (${DEAD_HOURS_HIST/,/, })
      AND ROUND(100.0 * SUM(CASE WHEN won THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) < 50.0
    THEN 'STILL_DEAD'
    WHEN hour_utc IN (${DEAD_HOURS_HIST/,/, })
      AND ROUND(100.0 * SUM(CASE WHEN won THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) >= 65.0
    THEN 'REGIME_SHIFT'
    WHEN hour_utc NOT IN (${DEAD_HOURS_HIST/,/, })
      AND ROUND(100.0 * SUM(CASE WHEN won THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) < 50.0
      AND COUNT(*) >= 5
    THEN 'NEW_DEAD'
    ELSE 'stable'
  END AS verdict
FROM dedup
GROUP BY hour_utc
HAVING COUNT(*) >= 3
ORDER BY hour_utc;
"
