-- Migration: Auto-populate trade_bible from trades table
-- Date: 2026-04-08 (revised)
-- Purpose: trade_bible was created manually with a different schema than the
--          trigger expects. This migration:
--          1. Drops the old manually-created trade_bible table
--          2. Recreates with proper schema (trade_id UNIQUE, order_id UNIQUE)
--          3. Creates helper functions for config_version/eval_tier extraction
--          4. Creates trigger to auto-populate on trades INSERT/UPDATE
--          5. Backfills from existing resolved trades
--
-- The trade_bible derives config_version and eval_tier from
-- metadata->>'entry_reason', which follows these patterns:
--   v10_DUNE_<REGIME>_T<OFFSET>_<ORDER_TYPE>  (e.g. v10_DUNE_NORMAL_T120_FAK)
--   v9_<TIER>_T<OFFSET>_<ORDER_TYPE>          (e.g. v9_GOLDEN_T60_FAK)
--   v2.2_early_T<OFFSET>                      (e.g. v2.2_early_T240)
--   v2.2_confirmed_T<OFFSET>
--   v8_standard

-- ============================================================================
-- 1. Drop old table and recreate with proper schema
-- ============================================================================
DROP TRIGGER IF EXISTS trg_populate_trade_bible ON trades;
DROP TABLE IF EXISTS trade_bible CASCADE;

CREATE TABLE trade_bible (
    id              SERIAL PRIMARY KEY,
    trade_id        INTEGER NOT NULL UNIQUE REFERENCES trades(id) ON DELETE CASCADE,
    order_id        VARCHAR(64) NOT NULL UNIQUE,
    config_version  VARCHAR(16),         -- 'v10', 'v9.0', 'v8.0', 'v2.2'
    eval_tier       VARCHAR(32),         -- 'DUNE_NORMAL', 'GOLDEN', 'EARLY_CASCADE', etc.
    direction       VARCHAR(8),          -- 'UP' / 'DOWN'
    trade_outcome   VARCHAR(8),          -- 'WIN' / 'LOSS' / 'PUSH'
    entry_price     NUMERIC(10, 6),
    pnl_usd         NUMERIC(12, 4),
    stake_usd       NUMERIC(12, 4),
    payout_usd      NUMERIC(12, 4),
    is_live         BOOLEAN DEFAULT FALSE,
    execution_mode  VARCHAR(20),         -- 'FAK' / 'GTC' / 'paper'
    entry_reason    TEXT,                -- full entry_reason string
    token_id        TEXT,                -- Polymarket token ID
    vpin_at_entry   DOUBLE PRECISION,
    delta_pct       DOUBLE PRECISION,
    window_ts       BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    bible_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_trade_bible_config   ON trade_bible(config_version);
CREATE INDEX idx_trade_bible_tier     ON trade_bible(eval_tier);
CREATE INDEX idx_trade_bible_outcome  ON trade_bible(trade_outcome);
CREATE INDEX idx_trade_bible_is_live  ON trade_bible(is_live);
CREATE INDEX idx_trade_bible_resolved ON trade_bible(resolved_at DESC);

-- ============================================================================
-- 2. Helper function: extract config_version from entry_reason
-- ============================================================================
CREATE OR REPLACE FUNCTION extract_config_version(entry_reason TEXT)
RETURNS VARCHAR(16) AS $$
BEGIN
    IF entry_reason IS NULL THEN
        RETURN NULL;
    ELSIF entry_reason LIKE 'v10_%' THEN
        RETURN 'v10';
    ELSIF entry_reason LIKE 'v9_%' THEN
        RETURN 'v9.0';
    ELSIF entry_reason LIKE 'v2.2_%' THEN
        RETURN 'v2.2';
    ELSIF entry_reason LIKE 'v8_%' THEN
        RETURN 'v8.0';
    ELSE
        RETURN 'unknown';
    END IF;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- ============================================================================
-- 3. Helper function: extract eval_tier from entry_reason
-- ============================================================================
CREATE OR REPLACE FUNCTION extract_eval_tier(entry_reason TEXT)
RETURNS VARCHAR(32) AS $$
DECLARE
    parts TEXT[];
BEGIN
    IF entry_reason IS NULL THEN
        RETURN NULL;
    END IF;

    -- v10_DUNE_<REGIME>_T<OFFSET>_<ORDER_TYPE>
    -- Extract the regime part (e.g., NORMAL, CASCADE, HIGH, LOW)
    IF entry_reason LIKE 'v10_DUNE_%' THEN
        -- Split by underscore, tier is parts[3] (1-indexed: v10, DUNE, <REGIME>, T120, FAK)
        parts := string_to_array(entry_reason, '_');
        IF array_length(parts, 1) >= 3 THEN
            RETURN 'DUNE_' || parts[3];
        END IF;
        RETURN 'DUNE';
    END IF;

    -- v9_<TIER>_T<OFFSET>_<ORDER_TYPE>
    -- Tier examples: GOLDEN, EARLY_CASCADE, EARLY
    IF entry_reason LIKE 'v9_%' THEN
        parts := string_to_array(entry_reason, '_');
        IF array_length(parts, 1) >= 2 THEN
            -- Handle multi-word tiers like EARLY_CASCADE
            -- v9_EARLY_CASCADE_T60_FAK -> tier = EARLY_CASCADE
            -- v9_GOLDEN_T60_FAK -> tier = GOLDEN
            IF array_length(parts, 1) >= 4 AND parts[3] !~ '^T[0-9]' THEN
                -- Multi-word tier: e.g., v9_EARLY_CASCADE_T60_FAK
                RETURN parts[2] || '_' || parts[3];
            ELSE
                RETURN parts[2];
            END IF;
        END IF;
        RETURN 'v9_unknown';
    END IF;

    -- v2.2_early_T<OFFSET> or v2.2_confirmed_T<OFFSET>
    IF entry_reason LIKE 'v2.2_%' THEN
        IF entry_reason LIKE '%early%' THEN
            RETURN 'v2_early';
        ELSIF entry_reason LIKE '%confirmed%' THEN
            RETURN 'v2_confirmed';
        END IF;
        RETURN 'v2_unknown';
    END IF;

    -- v8_standard or v8_confirmed or v8_early
    IF entry_reason LIKE 'v8_%' THEN
        parts := string_to_array(entry_reason, '_');
        IF array_length(parts, 1) >= 2 THEN
            RETURN 'v8_' || parts[2];
        END IF;
        RETURN 'v8_standard';
    END IF;

    RETURN entry_reason;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- ============================================================================
-- 4. Trigger function: auto-populate trade_bible on trades INSERT/UPDATE
-- ============================================================================
CREATE OR REPLACE FUNCTION populate_trade_bible()
RETURNS TRIGGER AS $$
DECLARE
    v_entry_reason TEXT;
    v_token_id TEXT;
    v_vpin DOUBLE PRECISION;
    v_delta DOUBLE PRECISION;
    v_window_ts BIGINT;
BEGIN
    -- Only populate when the trade has a resolved outcome
    IF NEW.outcome IS NULL THEN
        RETURN NEW;
    END IF;

    -- Extract fields from metadata JSONB
    v_entry_reason := NEW.metadata->>'entry_reason';
    v_token_id     := NEW.metadata->>'token_id';

    BEGIN
        v_window_ts := (NEW.metadata->>'window_ts')::BIGINT;
    EXCEPTION WHEN OTHERS THEN
        v_window_ts := NULL;
    END;

    BEGIN
        v_vpin := (NEW.metadata->>'vpin')::DOUBLE PRECISION;
    EXCEPTION WHEN OTHERS THEN
        v_vpin := NULL;
    END;

    BEGIN
        v_delta := (NEW.metadata->>'delta_pct')::DOUBLE PRECISION;
    EXCEPTION WHEN OTHERS THEN
        v_delta := NULL;
    END;

    -- Upsert into trade_bible (conflict on trade_id for updates)
    INSERT INTO trade_bible (
        trade_id, order_id, config_version, eval_tier,
        direction, trade_outcome, entry_price, pnl_usd,
        stake_usd, payout_usd, is_live, execution_mode,
        entry_reason, token_id, vpin_at_entry, delta_pct,
        window_ts, created_at, resolved_at
    ) VALUES (
        NEW.id,
        NEW.order_id,
        extract_config_version(v_entry_reason),
        extract_eval_tier(v_entry_reason),
        NEW.direction,
        NEW.outcome,
        NEW.entry_price,
        NEW.pnl_usd,
        NEW.stake_usd,
        NEW.payout_usd,
        COALESCE(NEW.is_live, FALSE),
        NEW.execution_mode,
        v_entry_reason,
        v_token_id,
        v_vpin,
        v_delta,
        v_window_ts,
        NEW.created_at,
        NEW.resolved_at
    )
    ON CONFLICT (trade_id) DO UPDATE SET
        order_id       = EXCLUDED.order_id,
        trade_outcome  = EXCLUDED.trade_outcome,
        pnl_usd        = EXCLUDED.pnl_usd,
        payout_usd     = EXCLUDED.payout_usd,
        resolved_at    = EXCLUDED.resolved_at,
        is_live        = EXCLUDED.is_live,
        execution_mode = EXCLUDED.execution_mode,
        entry_price    = EXCLUDED.entry_price,
        stake_usd      = EXCLUDED.stake_usd,
        config_version = COALESCE(EXCLUDED.config_version, trade_bible.config_version),
        eval_tier      = COALESCE(EXCLUDED.eval_tier, trade_bible.eval_tier),
        entry_reason   = COALESCE(EXCLUDED.entry_reason, trade_bible.entry_reason),
        token_id       = COALESCE(EXCLUDED.token_id, trade_bible.token_id),
        vpin_at_entry  = COALESCE(EXCLUDED.vpin_at_entry, trade_bible.vpin_at_entry),
        delta_pct      = COALESCE(EXCLUDED.delta_pct, trade_bible.delta_pct),
        window_ts      = COALESCE(EXCLUDED.window_ts, trade_bible.window_ts);

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- 5. Attach trigger to trades table
-- ============================================================================
CREATE TRIGGER trg_populate_trade_bible
    AFTER INSERT OR UPDATE OF outcome, pnl_usd, resolved_at, status
    ON trades
    FOR EACH ROW
    EXECUTE FUNCTION populate_trade_bible();

-- ============================================================================
-- 6. Backfill: populate trade_bible from all existing resolved trades
-- ============================================================================
INSERT INTO trade_bible (
    trade_id, order_id, config_version, eval_tier,
    direction, trade_outcome, entry_price, pnl_usd,
    stake_usd, payout_usd, is_live, execution_mode,
    entry_reason, token_id, vpin_at_entry, delta_pct,
    window_ts, created_at, resolved_at
)
SELECT
    t.id,
    t.order_id,
    extract_config_version(t.metadata->>'entry_reason'),
    extract_eval_tier(t.metadata->>'entry_reason'),
    t.direction,
    t.outcome,
    t.entry_price,
    t.pnl_usd,
    t.stake_usd,
    t.payout_usd,
    COALESCE(t.is_live, FALSE),
    t.execution_mode,
    t.metadata->>'entry_reason',
    t.metadata->>'token_id',
    (t.metadata->>'vpin')::DOUBLE PRECISION,
    (t.metadata->>'delta_pct')::DOUBLE PRECISION,
    (t.metadata->>'window_ts')::BIGINT,
    t.created_at,
    t.resolved_at
FROM trades t
WHERE t.outcome IS NOT NULL
ON CONFLICT (trade_id) DO NOTHING;

-- ============================================================================
-- 7. Verification queries (run after migration)
-- ============================================================================

-- Count by config version and tier
SELECT config_version, eval_tier, count(*),
       count(*) FILTER (WHERE trade_outcome = 'WIN') as wins,
       count(*) FILTER (WHERE trade_outcome = 'LOSS') as losses,
       ROUND(SUM(pnl_usd)::numeric, 2) as total_pnl
FROM trade_bible
WHERE is_live = true
GROUP BY config_version, eval_tier
ORDER BY config_version, eval_tier;

-- Total backfilled
SELECT count(*) as total_bible_entries,
       count(*) FILTER (WHERE is_live = true) as live_entries,
       count(*) FILTER (WHERE config_version = 'v10') as v10_entries
FROM trade_bible;
