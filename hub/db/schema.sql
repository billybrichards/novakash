-- ============================================================================
-- BTC-Trader Hub — Full PostgreSQL Schema
-- ============================================================================

-- Users
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    username        VARCHAR(64) NOT NULL UNIQUE,
    hashed_password VARCHAR(256) NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);


-- Trades (bets placed by the engine)
CREATE TABLE IF NOT EXISTS trades (
    id           SERIAL PRIMARY KEY,
    order_id     VARCHAR(64) NOT NULL UNIQUE,
    strategy     VARCHAR(64) NOT NULL,
    venue        VARCHAR(32) NOT NULL,
    market_slug  VARCHAR(128) NOT NULL,
    direction    VARCHAR(8) NOT NULL,               -- YES | NO | ARB
    entry_price  NUMERIC(10, 6),
    stake_usd    NUMERIC(12, 4),
    fee_usd      NUMERIC(10, 6),
    status       VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    outcome      VARCHAR(8),                        -- WIN | LOSS | PUSH
    payout_usd   NUMERIC(12, 4),
    pnl_usd      NUMERIC(12, 4),
    metadata     JSONB DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_trades_order_id    ON trades(order_id);
CREATE INDEX IF NOT EXISTS idx_trades_strategy    ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_market_slug ON trades(market_slug);
CREATE INDEX IF NOT EXISTS idx_trades_status      ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_created_at  ON trades(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_resolved_at ON trades(resolved_at DESC);


-- Signals (VPIN, cascade, arb, regime snapshots)
CREATE TABLE IF NOT EXISTS signals (
    id          SERIAL PRIMARY KEY,
    signal_type VARCHAR(32) NOT NULL,     -- vpin | cascade | arb | regime
    payload     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signals_type       ON signals(signal_type);
CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals(created_at DESC);


-- Daily P&L (pre-aggregated for fast charting)
CREATE TABLE IF NOT EXISTS daily_pnl (
    id                  SERIAL PRIMARY KEY,
    date                DATE NOT NULL UNIQUE,
    total_pnl           NUMERIC(12, 4),
    num_trades          INTEGER NOT NULL DEFAULT 0,
    wins                INTEGER NOT NULL DEFAULT 0,
    losses              INTEGER NOT NULL DEFAULT 0,
    win_rate            FLOAT,
    bankroll_end        NUMERIC(12, 2),
    strategy_breakdown  JSONB DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_daily_pnl_date ON daily_pnl(date DESC);


-- System State (single-row heartbeat from the engine)
CREATE TABLE IF NOT EXISTS system_state (
    id         INTEGER PRIMARY KEY DEFAULT 1,
    state      JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT single_row CHECK (id = 1)
);

INSERT INTO system_state (id, state)
VALUES (1, '{}')
ON CONFLICT (id) DO NOTHING;


-- Backtest Runs
CREATE TABLE IF NOT EXISTS backtest_runs (
    id            SERIAL PRIMARY KEY,
    strategy      VARCHAR(64) NOT NULL,
    start_date    TIMESTAMPTZ,
    end_date      TIMESTAMPTZ,
    total_pnl     NUMERIC(12, 4),
    num_trades    INTEGER,
    win_rate      FLOAT,
    sharpe_ratio  FLOAT,
    max_drawdown  FLOAT,
    params        JSONB DEFAULT '{}',
    trades_json   JSONB DEFAULT '[]',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_backtest_strategy   ON backtest_runs(strategy);
CREATE INDEX IF NOT EXISTS idx_backtest_created_at ON backtest_runs(created_at DESC);
