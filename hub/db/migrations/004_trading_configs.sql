-- ============================================================================
-- Migration 004 — Trading Configs System
-- Paper and Live are INDEPENDENT modes — both can run simultaneously
-- ============================================================================

-- Trading configs table
CREATE TABLE IF NOT EXISTS trading_configs (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    description TEXT,
    config JSONB NOT NULL,
    mode VARCHAR(16) NOT NULL DEFAULT 'paper',  -- 'paper' or 'live'
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

-- Add mode to trades table (paper | live)
ALTER TABLE trades ADD COLUMN IF NOT EXISTS mode VARCHAR(16) DEFAULT 'paper';
CREATE INDEX IF NOT EXISTS idx_trades_mode ON trades(mode);

-- Extend system_state to track both engines independently
-- paper_enabled and live_enabled are independent booleans
ALTER TABLE system_state ADD COLUMN IF NOT EXISTS paper_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE system_state ADD COLUMN IF NOT EXISTS live_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE system_state ADD COLUMN IF NOT EXISTS active_paper_config_id INTEGER;
ALTER TABLE system_state ADD COLUMN IF NOT EXISTS active_live_config_id INTEGER;
