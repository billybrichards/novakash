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
    mode         VARCHAR(16) DEFAULT 'paper',
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
    paper_enabled BOOLEAN DEFAULT TRUE,
    live_enabled BOOLEAN DEFAULT FALSE,
    active_paper_config_id INTEGER,
    active_live_config_id INTEGER,
    CONSTRAINT single_row CHECK (id = 1)
);

INSERT INTO system_state (id, state)
VALUES (1, '{}')
ON CONFLICT (id) DO NOTHING;


-- Trading Configs
CREATE TABLE IF NOT EXISTS trading_configs (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    description TEXT,
    config JSONB NOT NULL,
    mode VARCHAR(16) NOT NULL DEFAULT 'paper',
    is_active BOOLEAN DEFAULT FALSE,
    is_approved BOOLEAN DEFAULT FALSE,
    approved_at TIMESTAMPTZ,
    approved_by VARCHAR(64),
    parent_id INTEGER REFERENCES trading_configs(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trading_configs_active ON trading_configs(is_active, mode);
CREATE INDEX IF NOT EXISTS idx_trading_configs_name ON trading_configs(name);


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


-- Audit Tasks (Agent Ops + audit checklist)
CREATE TABLE IF NOT EXISTS audit_tasks_dev (
    id                BIGSERIAL PRIMARY KEY,
    task_key          VARCHAR(64),
    task_type         VARCHAR(64) NOT NULL,
    source            VARCHAR(64),
    title             TEXT NOT NULL,
    status            VARCHAR(24) NOT NULL DEFAULT 'OPEN',
    severity          VARCHAR(16),
    category          VARCHAR(64),
    priority          INTEGER NOT NULL DEFAULT 0,
    dedupe_key        TEXT,
    payload           JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by        VARCHAR(64),
    updated_by        VARCHAR(64),
    claimed_by        VARCHAR(64),
    claimed_at        TIMESTAMPTZ,
    claim_expires_at  TIMESTAMPTZ,
    started_at        TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ,
    canceled_at       TIMESTAMPTZ,
    last_heartbeat_at TIMESTAMPTZ,
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    status_reason     TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS audit_tasks_dev_dedupe_key_uq
    ON audit_tasks_dev (dedupe_key) WHERE dedupe_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS audit_tasks_dev_status_priority_idx
    ON audit_tasks_dev (status, priority DESC, created_at ASC);

CREATE INDEX IF NOT EXISTS audit_tasks_dev_claim_expires_idx
    ON audit_tasks_dev (claim_expires_at);

CREATE INDEX IF NOT EXISTS audit_tasks_dev_claimed_by_idx
    ON audit_tasks_dev (claimed_by, status);

CREATE INDEX IF NOT EXISTS audit_tasks_dev_updated_at_idx
    ON audit_tasks_dev (updated_at DESC);

CREATE INDEX IF NOT EXISTS audit_tasks_dev_type_created_idx
    ON audit_tasks_dev (task_type, created_at DESC);
