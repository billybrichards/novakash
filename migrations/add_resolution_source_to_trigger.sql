-- Migration: tag trade_bible.resolution_source from the populate trigger
-- Date: 2026-04-10
-- Purpose:
--   The `trade_bible.resolution_source` column has existed in the schema
--   for some time but was never populated by the `populate_trade_bible()`
--   trigger. New live-engine resolutions therefore landed with
--   resolution_source = NULL, making it impossible for the sitrep to
--   distinguish them from startup backfills or orphan reconciler catches.
--
--   This migration REPLACES the trigger function with a version that
--   stamps `resolution_source = 'trigger'` on every live-engine resolution,
--   and preserves an existing non-NULL source on CONFLICT so the
--   reconciler's explicit 'orphan_resolved' / 'backfill' tags (applied
--   after the trigger fires with a direct UPDATE) win the race.
--
--   This is a NON-DESTRUCTIVE migration — it does NOT drop the table or
--   reset existing rows. The old 873 'backfill' / 202 'trigger' / 29
--   'position_monitor' / 17 'trades_table' rows are preserved as-is.
--
--   Paired with code changes in:
--     engine/reconciliation/reconciler.py  (writes 'orphan_resolved' / 'backfill')
--     engine/strategies/orchestrator.py    (sitrep filters + labels)
--
-- To apply:
--   psql "$DATABASE_URL" -f migrations/add_resolution_source_to_trigger.sql

-- ============================================================================
-- Replace the populate_trade_bible trigger function (no table drops).
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

    -- Upsert into trade_bible (conflict on trade_id for updates).
    --
    -- resolution_source = 'trigger' marks this row as a live-engine
    -- resolution (via the AFTER UPDATE trigger on trades.outcome).
    -- The reconciler's orphan/backfill paths overwrite this value with
    -- their own source AFTER the trigger fires via direct UPDATE
    -- statements on trade_bible, and the ON CONFLICT clause below uses
    -- COALESCE(trade_bible.resolution_source, EXCLUDED.resolution_source)
    -- so the tagged value sticks on subsequent updates to the same row.
    INSERT INTO trade_bible (
        trade_id, order_id, config_version, eval_tier,
        direction, trade_outcome, entry_price, pnl_usd,
        stake_usd, payout_usd, is_live, execution_mode,
        entry_reason, token_id, vpin_at_entry, delta_pct,
        window_ts, created_at, resolved_at, resolution_source
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
        NEW.resolved_at,
        'trigger'
    )
    ON CONFLICT (trade_id) DO UPDATE SET
        order_id          = EXCLUDED.order_id,
        trade_outcome     = EXCLUDED.trade_outcome,
        pnl_usd           = EXCLUDED.pnl_usd,
        payout_usd        = EXCLUDED.payout_usd,
        resolved_at       = EXCLUDED.resolved_at,
        is_live           = EXCLUDED.is_live,
        execution_mode    = EXCLUDED.execution_mode,
        entry_price       = EXCLUDED.entry_price,
        stake_usd         = EXCLUDED.stake_usd,
        config_version    = COALESCE(EXCLUDED.config_version, trade_bible.config_version),
        eval_tier         = COALESCE(EXCLUDED.eval_tier, trade_bible.eval_tier),
        entry_reason      = COALESCE(EXCLUDED.entry_reason, trade_bible.entry_reason),
        token_id          = COALESCE(EXCLUDED.token_id, trade_bible.token_id),
        vpin_at_entry     = COALESCE(EXCLUDED.vpin_at_entry, trade_bible.vpin_at_entry),
        delta_pct         = COALESCE(EXCLUDED.delta_pct, trade_bible.delta_pct),
        window_ts         = COALESCE(EXCLUDED.window_ts, trade_bible.window_ts),
        -- Preserve an existing tagged source (orphan_resolved / backfill)
        -- over this trigger's default 'trigger' value. Without this
        -- COALESCE the reconciler's explicit tagging (set in an UPDATE
        -- that happens AFTER this trigger fires) would get clobbered the
        -- next time the trades row was touched.
        resolution_source = COALESCE(trade_bible.resolution_source, EXCLUDED.resolution_source);

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- The existing trigger definition is unchanged and still references
-- populate_trade_bible() — CREATE OR REPLACE FUNCTION rewires it
-- automatically. No need to DROP/CREATE the trigger itself.
