-- migrations/strategy_comparison.sql
--
-- Persistent strategy-comparison rollup table.
-- See docs/architecture/2026-05-01-strategy-comparison-system.md for design + rationale.
--
-- STATUS: NOT APPLIED. This is a design-PR migration. Apply via Montreal SSH after
-- review:
--
--   ssh -i $HOME/.ssh/novakash-montreal ubuntu@<montreal-ip> \
--     'sudo -u novakash bash -c "set -a; source /home/novakash/novakash/engine/.env; \
--      set +a; PSQL_URL=${DATABASE_URL/postgresql+asyncpg/postgresql}; \
--      psql \"$PSQL_URL\" -f /tmp/strategy_comparison.sql"'
--
-- Real-P&L math (see engine/domain/strategy_comparison/pnl_math.py — single source):
--   WIN:  real_pnl = (1 - fill_price) * (stake_usd / fill_price) - 0.072 * stake_usd
--   LOSS: real_pnl = -stake_usd

BEGIN;

CREATE TABLE IF NOT EXISTS strategy_comparison (
    snapshot_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strategy_id        TEXT        NOT NULL,
    asset              TEXT        NOT NULL DEFAULT 'BTC',
    timeframe          TEXT        NOT NULL DEFAULT '5m',
    window_period      TEXT        NOT NULL,    -- '1h' | '15h' | '24h' | '7d' | '30d'
    t_band             TEXT        NOT NULL,    -- 'all' | 'T-24-30' | 'T-31-60' | 'T-61-90' | 'T-91-120' | 'T-121-180' | 'T-181-240'
    direction_filter   TEXT        NOT NULL DEFAULT 'all',  -- 'all' | 'UP' | 'DOWN'
    regime_filter      TEXT        NOT NULL DEFAULT 'all',  -- 'all' | 'volatile_trend' | 'chop' | 'calm_trend' | 'risk_off'
    -- counts
    n_fires            INTEGER     NOT NULL,
    n_wins             INTEGER     NOT NULL,
    n_losses           INTEGER     NOT NULL,
    n_pending          INTEGER     NOT NULL,
    -- rates
    wr_pct             NUMERIC(5,2),
    wilson_low         NUMERIC(5,2),
    wilson_high        NUMERIC(5,2),
    -- fills
    avg_fill           NUMERIC(6,4),
    median_fill        NUMERIC(6,4),
    avg_stake_usd      NUMERIC(10,2),
    -- real P&L (wallet-truth math)
    real_net_pnl_usd   NUMERIC(12,2),
    real_pnl_per_fire  NUMERIC(10,2),
    daily_run_rate_usd NUMERIC(12,2),
    PRIMARY KEY (snapshot_at, strategy_id, window_period, t_band, direction_filter, regime_filter)
);

CREATE INDEX IF NOT EXISTS idx_strat_cmp_recent
    ON strategy_comparison (snapshot_at DESC, strategy_id);

CREATE INDEX IF NOT EXISTS idx_strat_cmp_strategy_period
    ON strategy_comparison (strategy_id, window_period, snapshot_at DESC);

CREATE INDEX IF NOT EXISTS idx_strat_cmp_leaderboard
    ON strategy_comparison (window_period, snapshot_at DESC, real_net_pnl_usd DESC);

-- Latest-only convenience view — most reads target this.
CREATE OR REPLACE VIEW v_strategy_comparison_latest AS
SELECT DISTINCT ON (strategy_id, window_period, t_band, direction_filter, regime_filter) *
FROM strategy_comparison
ORDER BY strategy_id, window_period, t_band, direction_filter, regime_filter, snapshot_at DESC;

-- Retention: drop snapshots older than 30 days. Run daily (cron / pg_cron / engine task).
-- Not auto-installed here; see scheduler module for scheduling decision.
--
-- DELETE FROM strategy_comparison WHERE snapshot_at < NOW() - INTERVAL '30 days';

COMMIT;
