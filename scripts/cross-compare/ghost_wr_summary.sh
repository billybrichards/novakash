#!/bin/bash
# ghost_wr_summary.sh — per-window deduped WR for every new ghost strategy
# plus current LIVE baselines, with Wilson 95% lower confidence bound.
#
# Usage:
#     bash ghost_wr_summary.sh             # last 7 days (default)
#     bash ghost_wr_summary.sh 14          # last 14 days
#     bash ghost_wr_summary.sh 3           # last 3 days
#
# Promotion rule: wr_lower_95 >= 60.0 AND trades >= 30 AND wr_pct >= 65.0
#                 → safe to flip GHOST → LIVE via strategy_runtime_overrides
#
# Wilson lower bound corrects for small-sample bias. A strategy at 80% WR
# on n=20 trades has wr_lower_95 ≈ 58% (don't promote on a lucky streak).
# At 80% WR on n=200 it has wr_lower_95 ≈ 75% (very safe to promote).

source "$(dirname "$0")/_lib.sh"

run_psql "
WITH dedup AS (
  SELECT strategy_id, asset, timeframe, window_ts,
         (array_agg(direction ORDER BY evaluated_at))[1] AS first_dir
  FROM strategy_decisions
  WHERE evaluated_at > NOW() - INTERVAL '${DAYS} days'
    AND action = 'TRADE'
    AND (
      strategy_id LIKE 'v\\_%'
      OR strategy_id IN (
        'v9_1_lgb_only','v9_lgb_only','v12_lgb_combo',
        'v15m_up_basic','v15m_gate','v8_champion_lgb_only'
      )
    )
  GROUP BY strategy_id, asset, timeframe, window_ts
)
SELECT
  d.strategy_id,
  d.asset,
  d.timeframe,
  COUNT(*) AS trades,
  SUM(CASE WHEN d.first_dir = md.outcome THEN 1 ELSE 0 END) AS wins,
  ROUND(100.0 * SUM(CASE WHEN d.first_dir = md.outcome THEN 1 ELSE 0 END)
        / NULLIF(COUNT(*), 0), 1) AS wr_pct,
  ROUND(100.0 * (
    (SUM(CASE WHEN d.first_dir = md.outcome THEN 1 ELSE 0 END)::float
       / NULLIF(COUNT(*), 0)
       + 1.96 * 1.96 / (2.0 * COUNT(*))
       - 1.96 * sqrt(
           (SUM(CASE WHEN d.first_dir = md.outcome THEN 1 ELSE 0 END)::float
              / NULLIF(COUNT(*), 0))
           * (1.0 - SUM(CASE WHEN d.first_dir = md.outcome THEN 1 ELSE 0 END)::float
                    / NULLIF(COUNT(*), 0))
           / NULLIF(COUNT(*), 0)
           + 1.96 * 1.96 / (4.0 * COUNT(*) * COUNT(*))
         )
    ) / (1.0 + 1.96 * 1.96 / NULLIF(COUNT(*), 0))
  ), 1) AS wr_lower_95,
  CASE
    WHEN COUNT(*) < 30 THEN 'soak'
    WHEN ROUND(100.0 * (
      (SUM(CASE WHEN d.first_dir = md.outcome THEN 1 ELSE 0 END)::float
         / NULLIF(COUNT(*), 0)
         + 1.96 * 1.96 / (2.0 * COUNT(*))
         - 1.96 * sqrt(
             (SUM(CASE WHEN d.first_dir = md.outcome THEN 1 ELSE 0 END)::float
                / NULLIF(COUNT(*), 0))
             * (1.0 - SUM(CASE WHEN d.first_dir = md.outcome THEN 1 ELSE 0 END)::float
                      / NULLIF(COUNT(*), 0))
             / NULLIF(COUNT(*), 0)
             + 1.96 * 1.96 / (4.0 * COUNT(*) * COUNT(*))
           )
      ) / (1.0 + 1.96 * 1.96 / NULLIF(COUNT(*), 0))
    ), 1) >= 60.0
      AND ROUND(100.0 * SUM(CASE WHEN d.first_dir = md.outcome THEN 1 ELSE 0 END)
                       / NULLIF(COUNT(*), 0), 1) >= 65.0
    THEN 'PROMOTE'
    WHEN ROUND(100.0 * SUM(CASE WHEN d.first_dir = md.outcome THEN 1 ELSE 0 END)
                       / NULLIF(COUNT(*), 0), 1) < 50.0
    THEN 'KILL'
    ELSE 'continue'
  END AS verdict
FROM dedup d
LEFT JOIN market_data md USING (asset, timeframe, window_ts)
WHERE md.resolved = TRUE
GROUP BY d.strategy_id, d.asset, d.timeframe
HAVING COUNT(*) >= 5
ORDER BY d.timeframe, d.asset, wr_pct DESC NULLS LAST;
"
