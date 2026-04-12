-- Strategy Decisions table (SP-05)
-- Stores ALL strategy evaluations (LIVE + GHOST) for the Strategy Lab.
-- One row per (strategy_id, asset, window_ts, eval_offset).

CREATE TABLE IF NOT EXISTS strategy_decisions (
    id              BIGSERIAL PRIMARY KEY,
    strategy_id     TEXT NOT NULL,           -- 'v10_gate', 'v4_fusion'
    strategy_version TEXT NOT NULL,          -- '10.5.3'
    asset           TEXT NOT NULL,           -- 'BTC'
    window_ts       BIGINT NOT NULL,         -- Unix epoch of window open
    timeframe       TEXT NOT NULL DEFAULT '5m',
    eval_offset     INTEGER,                 -- Seconds to close at evaluation time
    mode            TEXT NOT NULL,           -- 'LIVE' | 'GHOST'

    -- Decision
    action          TEXT NOT NULL,           -- 'TRADE' | 'SKIP' | 'ERROR'
    direction       TEXT,                    -- 'UP' | 'DOWN'
    confidence      TEXT,
    confidence_score DOUBLE PRECISION,
    entry_cap       DOUBLE PRECISION,
    collateral_pct  DOUBLE PRECISION,
    entry_reason    TEXT NOT NULL DEFAULT '',
    skip_reason     TEXT,

    -- Execution (filled post-trade for LIVE+TRADE only)
    executed        BOOLEAN NOT NULL DEFAULT false,
    order_id        TEXT,
    fill_price      DOUBLE PRECISION,
    fill_size       DOUBLE PRECISION,

    -- Audit
    metadata_json   JSONB NOT NULL DEFAULT '{}',
    evaluated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Dedup
    UNIQUE (strategy_id, asset, window_ts, eval_offset)
);

CREATE INDEX IF NOT EXISTS idx_sd_window ON strategy_decisions (asset, window_ts);
CREATE INDEX IF NOT EXISTS idx_sd_strategy ON strategy_decisions (strategy_id, evaluated_at);
