-- Wallet snapshots table for CLOB reconciler
-- Tracks USDC balance over time from direct Polymarket CLOB polling

CREATE TABLE IF NOT EXISTS wallet_snapshots (
    id BIGSERIAL PRIMARY KEY,
    balance_usdc NUMERIC(14, 4) NOT NULL,
    source VARCHAR(20) NOT NULL DEFAULT 'clob_reconciler',
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wallet_snapshots_time ON wallet_snapshots(recorded_at DESC);
