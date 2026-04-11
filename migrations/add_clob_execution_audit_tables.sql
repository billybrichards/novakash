-- =============================================================================
-- CLOB Execution Audit Log — Complete Order Book + Execution Tracking
-- =============================================================================
-- Purpose: Debug FOK ladder behavior, CLOB pricing, order fills
-- Location: Montreal VPS only (Polymarket geo-blocked elsewhere)
-- Created: 2026-04-07
-- =============================================================================

-- Main execution log: every FOK attempt, GTC placement, fill, kill
CREATE TABLE IF NOT EXISTS clob_execution_log (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Window context
    asset TEXT NOT NULL,
    timeframe TEXT NOT NULL DEFAULT '5m',
    window_ts BIGINT NOT NULL,
    outcome TEXT NOT NULL,  -- 'UP' or 'DOWN'
    
    -- Token IDs
    token_id TEXT NOT NULL,
    
    -- Decision context
    direction TEXT NOT NULL,  -- 'BUY' (we buy YES or NO)
    strategy TEXT,
    eval_offset INT,
    
    -- Order parameters
    target_price NUMERIC(10,6),
    target_size NUMERIC(18,8),
    max_price NUMERIC(10,6),  -- FOK cap
    min_price NUMERIC(10,6),  -- FOK floor
    
    -- CLOB state at execution time
    clob_best_ask NUMERIC(10,6),
    clob_best_bid NUMERIC(10,6),
    clob_spread NUMERIC(10,6),
    clob_mid NUMERIC(10,6),
    
    -- Execution mode
    execution_mode TEXT NOT NULL,  -- 'FOK' or 'GTC'
    
    -- FOK ladder state (NULL for GTC)
    fok_attempt_num INT,
    fok_max_attempts INT,
    fok_ladder_step_price NUMERIC(10,6),
    
    -- Result
    status TEXT NOT NULL,  -- 'submitted', 'filled', 'killed', 'timeout', 'error'
    fill_price NUMERIC(10,6),
    fill_size NUMERIC(18,8),
    fill_pct NUMERIC(5,2),  -- Percentage filled
    
    -- Order IDs
    order_id TEXT,
    transaction_hash TEXT,
    
    -- Error details
    error_code TEXT,
    error_message TEXT,
    
    -- Latency
    latency_ms INT,
    
    -- Metadata (JSONB for flexibility)
    metadata JSONB DEFAULT '{}'::jsonb,
    
    -- Indexes for fast queries
    CONSTRAINT clob_exec_log_window_idx UNIQUE (window_ts, outcome, ts, execution_mode, fok_attempt_num)
);

CREATE INDEX IF NOT EXISTS idx_clob_exec_log_ts ON clob_execution_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_clob_exec_log_window ON clob_execution_log(window_ts, outcome);
CREATE INDEX IF NOT EXISTS idx_clob_exec_log_order_id ON clob_execution_log(order_id);
CREATE INDEX IF NOT EXISTS idx_clob_exec_log_status ON clob_execution_log(status);

-- FOK ladder attempt history (one row per attempt in the ladder)
CREATE TABLE IF NOT EXISTS fok_ladder_attempts (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Links to main execution log
    execution_log_id BIGINT REFERENCES clob_execution_log(id),
    
    -- Attempt details
    attempt_num INT NOT NULL,
    attempt_price NUMERIC(10,6) NOT NULL,
    attempt_size NUMERIC(18,8) NOT NULL,
    
    -- CLOB state at this attempt
    clob_best_ask NUMERIC(10,6),
    clob_best_bid NUMERIC(10,6),
    
    -- Result
    status TEXT NOT NULL,  -- 'attempted', 'filled', 'killed', 'timeout'
    fill_size NUMERIC(18,8),
    fill_price NUMERIC(10,6),
    
    -- Error details
    error_message TEXT,
    
    -- Latency
    attempt_duration_ms INT,
    
    UNIQUE (execution_log_id, attempt_num)
);

CREATE INDEX IF NOT EXISTS idx_fok_ladder_exec_id ON fok_ladder_attempts(execution_log_id);

-- Comprehensive CLOB snapshot log (every poll, not just during execution)
CREATE TABLE IF NOT EXISTS clob_book_snapshots (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Window context
    asset TEXT NOT NULL,
    timeframe TEXT NOT NULL DEFAULT '5m',
    window_ts BIGINT NOT NULL,
    
    -- Token IDs
    up_token_id TEXT,
    down_token_id TEXT,
    
    -- UP token book
    up_best_bid NUMERIC(10,6),
    up_best_ask NUMERIC(10,6),
    up_bid_depth NUMERIC(18,8),  -- Total size at best bid
    up_ask_depth NUMERIC(18,8),  -- Total size at best ask
    
    -- DOWN token book
    down_best_bid NUMERIC(10,6),
    down_best_ask NUMERIC(10,6),
    down_bid_depth NUMERIC(18,8),
    down_ask_depth NUMERIC(18,8),
    
    -- Derived metrics
    up_spread NUMERIC(10,6),
    down_spread NUMERIC(10,6),
    mid_price NUMERIC(10,6),
    
    -- Book depth (top 5 levels)
    up_bids_top5 JSONB,  -- [{"price": 0.55, "size": 100}, ...]
    up_asks_top5 JSONB,
    down_bids_top5 JSONB,
    down_asks_top5 JSONB,
    
    UNIQUE (window_ts, up_token_id, down_token_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_clob_snapshots_ts ON clob_book_snapshots(ts DESC);
CREATE INDEX IF NOT EXISTS idx_clob_snapshots_window ON clob_book_snapshots(window_ts);

-- Order submission audit (all orders, not just CLOB)
CREATE TABLE IF NOT EXISTS order_audit_log (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Order details
    order_id TEXT UNIQUE NOT NULL,
    direction TEXT NOT NULL,  -- 'YES' or 'NO'
    token_id TEXT NOT NULL,
    
    -- Pricing
    price NUMERIC(10,6) NOT NULL,
    size NUMERIC(18,8) NOT NULL,
    stake_usd NUMERIC(10,2) NOT NULL,
    
    -- Execution
    execution_mode TEXT NOT NULL,  -- 'FOK', 'GTC', 'GTD'
    status TEXT NOT NULL,  -- 'submitted', 'filled', 'cancelled', 'expired', 'rejected'
    
    -- Fill details
    fill_price NUMERIC(10,6),
    fill_size NUMERIC(18,8),
    fill_time TIMESTAMPTZ,
    
    -- Window context
    asset TEXT,
    window_ts BIGINT,
    outcome TEXT,
    eval_offset INT,
    
    -- Error details
    error_code TEXT,
    error_message TEXT,
    
    -- CLOB state at submission
    clob_best_ask NUMERIC(10,6),
    clob_best_bid NUMERIC(10,6),
    
    -- Metadata
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_order_audit_order_id ON order_audit_log(order_id);
CREATE INDEX IF NOT EXISTS idx_order_audit_ts ON order_audit_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_order_audit_window ON order_audit_log(window_ts);
CREATE INDEX IF NOT EXISTS idx_order_audit_status ON order_audit_log(status);

-- =============================================================================
-- Helper function: log FOK ladder execution
-- =============================================================================
CREATE OR REPLACE FUNCTION log_fok_execution(
    p_asset TEXT,
    p_timeframe TEXT,
    p_window_ts BIGINT,
    p_outcome TEXT,
    p_token_id TEXT,
    p_direction TEXT,
    p_strategy TEXT,
    p_eval_offset INT,
    p_target_price NUMERIC,
    p_target_size NUMERIC,
    p_max_price NUMERIC,
    p_min_price NUMERIC,
    p_clob_ask NUMERIC,
    p_clob_bid NUMERIC,
    p_status TEXT,
    p_fill_price NUMERIC,
    p_fill_size NUMERIC,
    p_order_id TEXT,
    p_error_message TEXT,
    p_metadata JSONB
) RETURNS BIGINT AS $$
DECLARE
    v_exec_id BIGINT;
BEGIN
    INSERT INTO clob_execution_log (
        asset, timeframe, window_ts, outcome, token_id,
        direction, strategy, eval_offset,
        target_price, target_size, max_price, min_price,
        clob_best_ask, clob_best_bid,
        execution_mode, status,
        fill_price, fill_size, order_id, error_message, metadata
    ) VALUES (
        p_asset, p_timeframe, p_window_ts, p_outcome, p_token_id,
        p_direction, p_strategy, p_eval_offset,
        p_target_price, p_target_size, p_max_price, p_min_price,
        p_clob_ask, p_clob_bid,
        'FOK', p_status,
        p_fill_price, p_fill_size, p_order_id, p_error_message, p_metadata
    ) RETURNING id INTO v_exec_id;
    
    RETURN v_exec_id;
END;
$$ LANGUAGE plpgsql;

-- =============================================================================
-- Example queries for debugging
-- =============================================================================

-- FOK ladder attempts that killed (no fill)
SELECT 
    el.ts, el.window_ts, el.outcome,
    el.target_price, el.clob_best_ask,
    COUNT(fla.id) as attempts,
    MAX(fla.fill_size) as max_fill
FROM clob_execution_log el
LEFT JOIN fok_ladder_attempts fla ON fla.execution_log_id = el.id
WHERE el.execution_mode = 'FOK' AND el.status = 'killed'
GROUP BY el.id
ORDER BY el.ts DESC
LIMIT 20;

-- CLOB vs target price for all FOK attempts
SELECT 
    ts, window_ts, outcome,
    target_price, clob_best_ask,
    ROUND((clob_best_ask - target_price) / target_price * 100, 2) as price_gap_pct,
    status, fill_size
FROM clob_execution_log
WHERE execution_mode = 'FOK'
ORDER BY ts DESC
LIMIT 50;

-- Order fill rate by execution mode
SELECT 
    execution_mode,
    COUNT(*) as total_orders,
    COUNT(CASE WHEN status = 'filled' THEN 1 END) as filled,
    ROUND(100.0 * COUNT(CASE WHEN status = 'filled' THEN 1 END) / COUNT(*), 2) as fill_rate_pct
FROM clob_execution_log
WHERE ts > NOW() - INTERVAL '24 hours'
GROUP BY execution_mode;
