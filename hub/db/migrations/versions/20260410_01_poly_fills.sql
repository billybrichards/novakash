-- v11 Migration: poly_fills table
-- Purpose: Authoritative source-of-truth record of every Polymarket CLOB fill
--          for our proxy wallet, sourced from data-api.polymarket.com.
--
-- Design principles:
-- 1. **Append-only**: rows are INSERT-only. Never UPDATE fills — if the
--    on-chain data changes (impossible but defensively-coded), the old
--    row is preserved and a new one is inserted with a new verified_at.
-- 2. **Source-tagged**: `source` column identifies where the row came from
--    ('data-api', 'clob-api', 'on-chain', 'engine-reported'). This lets
--    us detect discrepancies between what the engine thought it did and
--    what actually happened on-chain.
-- 3. **Unique by transaction_hash**: the on-chain tx hash is the global
--    identifier. If the same tx shows up from multiple sources, we keep
--    the first (which should be data-api since it's most reliable).
-- 4. **Linkable**: `trade_bible_id` is a nullable FK. Orphan fills
--    (fills we received but never tracked as a trade) are preserved
--    with NULL — those are the multi-fill bug casualties.
-- 5. **Ground truth for analysis**: this table is the ONLY place analysis
--    queries should read when computing true P&L, true stake, true fills.
--    Never read trade_bible or trades for P&L — those are engine-side
--    records that can drift from reality.

CREATE TABLE IF NOT EXISTS poly_fills (
    id                  BIGSERIAL PRIMARY KEY,

    -- On-chain identifiers (authoritative)
    transaction_hash    TEXT NOT NULL,
    asset_token_id      TEXT NOT NULL,          -- Polymarket outcome token ID
    condition_id        TEXT,                   -- Market condition ID (links multiple fills to one window)
    market_slug         TEXT,                   -- e.g. 'btc-updown-5m-1775819700'

    -- Fill details (from Polymarket data-api)
    side                VARCHAR(8) NOT NULL CHECK (side IN ('BUY', 'SELL')),
    outcome             VARCHAR(16),            -- 'Up' or 'Down' (the side of this token)
    price               DOUBLE PRECISION NOT NULL,
    size                DOUBLE PRECISION NOT NULL,
    cost_usd            DOUBLE PRECISION NOT NULL GENERATED ALWAYS AS (price * size) STORED,
    fee_usd             DOUBLE PRECISION,       -- Polymarket fee (if known)

    -- Timing
    match_timestamp     BIGINT NOT NULL,        -- Unix epoch seconds (from chain)
    match_time_utc      TIMESTAMPTZ NOT NULL,

    -- Linkage (nullable — orphan fills have no engine trade)
    trade_bible_id      INTEGER REFERENCES trade_bible(id) ON DELETE SET NULL,
    clob_order_id       TEXT,                   -- Our order_id that produced this fill (may be NULL)

    -- Provenance (defensive tagging)
    source              VARCHAR(32) NOT NULL DEFAULT 'data-api',
    -- Values: 'data-api', 'clob-api', 'on-chain', 'engine-reported'

    verified_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Analysis flags (populated after insert via trigger or script)
    is_multi_fill       BOOLEAN,                -- True if this condition_id has multiple fills within 2min
    multi_fill_index    INTEGER,                -- 1, 2, 3 — which attempt this was
    multi_fill_total    INTEGER,                -- Total fills for this condition_id

    -- Raw payload for debugging + future reprocessing
    raw_payload         JSONB,

    CONSTRAINT poly_fills_unique_tx UNIQUE (transaction_hash)
);

CREATE INDEX IF NOT EXISTS idx_poly_fills_condition ON poly_fills (condition_id, match_time_utc DESC);
CREATE INDEX IF NOT EXISTS idx_poly_fills_slug ON poly_fills (market_slug, match_time_utc DESC);
CREATE INDEX IF NOT EXISTS idx_poly_fills_trade_bible ON poly_fills (trade_bible_id) WHERE trade_bible_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_poly_fills_match_time ON poly_fills (match_time_utc DESC);
CREATE INDEX IF NOT EXISTS idx_poly_fills_source ON poly_fills (source);

COMMENT ON TABLE poly_fills IS 'Authoritative fill record sourced from Polymarket data-api. Append-only.';
COMMENT ON COLUMN poly_fills.source IS 'Provenance tag: data-api|clob-api|on-chain|engine-reported';
COMMENT ON COLUMN poly_fills.is_multi_fill IS 'True if same condition_id has 2+ fills within 2min window — indicates multi-fill bug';
COMMENT ON COLUMN poly_fills.trade_bible_id IS 'NULL = orphan fill, engine never tracked this as a trade';
