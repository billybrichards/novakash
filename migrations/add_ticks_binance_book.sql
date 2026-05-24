-- Migration: ticks_binance_book
--
-- Adds the canonical store for Binance depth20 book snapshots, the data
-- source for the four `binance_depth_imbalance_*` + `binance_spread_pct`
-- features wired into V5FeatureBody by PR #582.
--
-- Schema mirrors `engine/persistence/tick_recorder.py:ensure_tables`
-- (idempotent CREATE there too — the migration is committed as SQL for
-- ops review; production engines auto-create on boot).
--
-- Indexed `(asset, ts DESC)` so the feature emitter's ASOF lookup is a
-- single seek per scoring call.
--
-- Migration source: `feat/binance-depth-feed` (follow-up to PR #582).
-- Sequence number: pick whatever the next migration slot is at apply time.

CREATE TABLE IF NOT EXISTS ticks_binance_book (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ  NOT NULL,
    asset           VARCHAR(10)  NOT NULL,
    last_update_id  BIGINT,

    -- Top of book
    best_bid        FLOAT8,
    best_ask        FLOAT8,
    best_bid_qty    FLOAT8,
    best_ask_qty    FLOAT8,

    -- Derived
    mid             FLOAT8,
    spread_pct      FLOAT8,

    -- ±1% / ±5% of mid summed depth
    bid_depth_1pct  FLOAT8,
    ask_depth_1pct  FLOAT8,
    bid_depth_5pct  FLOAT8,
    ask_depth_5pct  FLOAT8,

    -- Full top-20 levels (for any future re-derivation)
    bids_top20      JSONB,
    asks_top20      JSONB,

    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ticks_binance_book_ts
    ON ticks_binance_book (ts DESC);
CREATE INDEX IF NOT EXISTS idx_ticks_binance_book_asset_ts
    ON ticks_binance_book (asset, ts DESC);
