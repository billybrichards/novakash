-- Migration: Add Macro Observer tables and columns
-- Date: 2026-04-06
-- Description: Creates macro_signals and macro_events tables,
--              adds macro + price columns to window_snapshots.
--              Safe to run multiple times (IF NOT EXISTS / IF NOT EXISTS).

-- ─── macro_signals ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS macro_signals (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT now(),

    -- Signal output
    bias VARCHAR(8) NOT NULL,               -- BULL / BEAR / NEUTRAL
    confidence INT NOT NULL,                -- 0-100
    direction_gate VARCHAR(12) NOT NULL,    -- ALLOW_ALL / SKIP_DOWN / SKIP_UP
    threshold_modifier FLOAT NOT NULL DEFAULT 1.0,   -- 0.5-1.5x on delta thresholds
    size_modifier FLOAT NOT NULL DEFAULT 1.0,        -- 0.5-1.5x on bet sizing
    override_active BOOL NOT NULL DEFAULT false,     -- true only at confidence >= 80
    reasoning TEXT,

    -- Inputs logged for analysis / debugging
    oracle_up_ratio_1h FLOAT,       -- % UP in last 12 resolved windows (1h)
    oracle_up_ratio_4h FLOAT,       -- % UP in last 48 resolved windows (4h)
    btc_delta_1h FLOAT,
    btc_delta_4h FLOAT,
    btc_delta_15m FLOAT,
    coinbase_price FLOAT,
    kraken_price FLOAT,
    binance_price FLOAT,
    exchange_spread_usd FLOAT,      -- coinbase - binance
    funding_rate FLOAT,
    top_trader_long_pct FLOAT,
    taker_buy_ratio FLOAT,
    oi_delta_1h FLOAT,
    vpin_current FLOAT,
    recent_spike BOOL DEFAULT false,
    upcoming_event TEXT,            -- e.g. "US CPI (HIGH) in 42 min"

    -- Raw data for replay / debugging
    raw_payload JSONB,
    raw_response JSONB,

    -- Cost tracking
    input_tokens INT,
    output_tokens INT,
    latency_ms INT,
    cost_usd FLOAT
);

CREATE INDEX IF NOT EXISTS idx_macro_signals_created_at ON macro_signals (created_at DESC);

-- ─── macro_events ─────────────────────────────────────────────────────────────
-- Economic calendar — pre-load weekly Fed/CPI/FOMC events
CREATE TABLE IF NOT EXISTS macro_events (
    id SERIAL PRIMARY KEY,
    event_time TIMESTAMPTZ NOT NULL,
    event_name TEXT NOT NULL,
    impact VARCHAR(10) NOT NULL DEFAULT 'MEDIUM',   -- LOW / MEDIUM / HIGH / EXTREME
    actual_value TEXT,
    forecast_value TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_macro_events_event_time ON macro_events (event_time);

-- ─── window_snapshots additions ───────────────────────────────────────────────
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS macro_bias VARCHAR(8);
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS macro_confidence INT;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS macro_override_active BOOL;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS macro_signal_id INT;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS coinbase_price FLOAT;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS exchange_spread_usd FLOAT;

-- Verify
SELECT 'macro_signals' as table_name, COUNT(*) as rows FROM macro_signals
UNION ALL
SELECT 'macro_events', COUNT(*) FROM macro_events;

SELECT column_name FROM information_schema.columns
WHERE table_name = 'window_snapshots'
  AND column_name IN ('macro_bias','macro_confidence','macro_override_active','macro_signal_id','coinbase_price','exchange_spread_usd')
ORDER BY column_name;
