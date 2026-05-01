-- v15m_up_basic LIVE monitoring queries
-- Run on RDS (novakash-pg-prod) directly, or via Hub psql proxy.
--
-- Usage:
--   PGPASSWORD=$RDS_PWD psql -h novakash-pg-prod.cpmisy2asv71.ca-central-1.rds.amazonaws.com \
--     -U postgres -d novakash -f scripts/v15m_up_basic_monitor.sql
--
-- Promoted LIVE 2026-05-01 18:41:32 UTC via DB override (audit #343, PR #453, hub note #311).
-- Revert: see scripts/v15m_up_basic_revert.sql

\echo '=== Override row state ==='
SELECT strategy_id, mode, updated_at, updated_by, updated_reason
FROM strategy_runtime_overrides
WHERE strategy_id = 'v15m_up_basic';

\echo ''
\echo '=== Decisions since LIVE promotion ==='
SELECT
    mode,
    action,
    COUNT(*) AS n,
    SUM(CASE WHEN action = 'TRADE' THEN 1 ELSE 0 END) AS trades,
    MIN(evaluated_at) AS first,
    MAX(evaluated_at) AS last
FROM strategy_decisions
WHERE strategy_id = 'v15m_up_basic'
  AND evaluated_at > '2026-05-01 18:41:32+00'
GROUP BY mode, action
ORDER BY mode, action;

\echo ''
\echo '=== LIVE TRADE decisions (the ones that move money) ==='
SELECT
    evaluated_at,
    window_ts,
    direction,
    confidence,
    confidence_score,
    entry_cap,
    executed,
    order_id,
    fill_price,
    fill_size
FROM strategy_decisions
WHERE strategy_id = 'v15m_up_basic'
  AND mode = 'LIVE'
  AND action = 'TRADE'
  AND evaluated_at > '2026-05-01 18:41:32+00'
ORDER BY evaluated_at DESC
LIMIT 20;

\echo ''
\echo '=== Skip-reason breakdown post-LIVE (last 4h) ==='
SELECT skip_reason, COUNT(*) AS n
FROM strategy_decisions
WHERE strategy_id = 'v15m_up_basic'
  AND mode = 'LIVE'
  AND action = 'SKIP'
  AND evaluated_at > NOW() - INTERVAL '4 hours'
GROUP BY skip_reason
ORDER BY n DESC
LIMIT 15;

\echo ''
\echo '=== Hour-of-day TRADE distribution (since LIVE) — confirms session_hours gate firing ==='
SELECT
    EXTRACT(HOUR FROM TO_TIMESTAMP(window_ts))::int AS hour_utc,
    COUNT(*) FILTER (WHERE action = 'TRADE') AS trades,
    COUNT(*) FILTER (WHERE action = 'SKIP' AND skip_reason LIKE 'session_hours%') AS hour_blocked,
    COUNT(*) AS total
FROM strategy_decisions
WHERE strategy_id = 'v15m_up_basic'
  AND mode = 'LIVE'
  AND evaluated_at > '2026-05-01 18:41:32+00'
GROUP BY hour_utc
ORDER BY hour_utc;

\echo ''
\echo '=== Trades placed (cross-join trades table) ==='
SELECT
    t.id,
    t.created_at,
    t.market_slug,
    t.direction,
    t.entry_price,
    t.stake_usd,
    t.fee_usd,
    t.outcome,
    t.pnl_usd
FROM trades t
WHERE t.strategy_id = 'v15m_up_basic'
  AND t.created_at > '2026-05-01 18:41:32+00'
ORDER BY t.created_at DESC
LIMIT 50;

\echo ''
\echo '=== Rolling kill criteria check ==='
WITH live_trades AS (
    SELECT
        id,
        created_at,
        outcome,
        pnl_usd,
        ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rn_desc,
        ROW_NUMBER() OVER (ORDER BY created_at) AS rn_asc
    FROM trades
    WHERE strategy_id = 'v15m_up_basic'
      AND created_at > '2026-05-01 18:41:32+00'
      AND outcome IN ('WIN', 'LOSS')
),
last_50 AS (
    SELECT * FROM live_trades WHERE rn_desc <= 50
),
consec_losses AS (
    -- Most recent N consecutive losses
    SELECT COUNT(*) AS n_consec
    FROM (
        SELECT outcome,
               SUM(CASE WHEN outcome != 'LOSS' THEN 1 ELSE 0 END) OVER (ORDER BY created_at DESC) AS grp
        FROM live_trades
    ) sub
    WHERE grp = 0 AND outcome = 'LOSS'
)
SELECT
    (SELECT COUNT(*) FROM live_trades) AS total_resolved,
    (SELECT COUNT(*) FILTER (WHERE outcome = 'WIN') FROM last_50) AS last50_wins,
    (SELECT COUNT(*) FILTER (WHERE outcome = 'LOSS') FROM last_50) AS last50_losses,
    (SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE outcome = 'WIN')::numeric / NULLIF(COUNT(*), 0), 2) FROM last_50) AS last50_wr_pct,
    (SELECT COALESCE(SUM(pnl_usd), 0) FROM live_trades) AS cumulative_pnl,
    (SELECT MIN(pnl_usd) FROM live_trades) AS worst_single_trade_pnl,
    (SELECT n_consec FROM consec_losses) AS current_consec_losses,
    -- Kill criteria flags
    CASE
        WHEN (SELECT COUNT(*) FROM last_50) >= 50 AND
             (SELECT 100.0 * COUNT(*) FILTER (WHERE outcome = 'WIN')::numeric / COUNT(*) FROM last_50) < 65
        THEN 'KILL: rolling 50-trade WR < 65%'
        WHEN (SELECT SUM(pnl_usd) FROM live_trades) < -25
        THEN 'KILL: cumulative PnL < -$25'
        WHEN (SELECT n_consec FROM consec_losses) >= 3
        THEN 'KILL: 3 consecutive losses'
        WHEN (SELECT MIN(pnl_usd) FROM live_trades) < -5
        THEN 'KILL: single trade loss > $5'
        ELSE 'OK'
    END AS kill_status;
