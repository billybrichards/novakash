-- Migration: Add per-timescale macro bias map to macro_signals
-- Date: 2026-04-10
-- Description:
--   The macro-observer originally produced a single-horizon bias signal
--   (5-15 minute view) applied uniformly to every trade. The engine now
--   trades four horizons (5m / 15m / 1h / 4h) and needs per-timescale
--   macro gates so a bearish 5m chop doesn't block a valid 1h long thesis.
--
--   This migration adds a nullable JSONB column holding the per-horizon
--   map emitted by the LLM. The existing flat columns (bias, confidence,
--   direction_gate, threshold_modifier, size_modifier, override_active,
--   reasoning) stay populated from the new "overall" synthesis block so
--   every existing reader (dashboard, /v4/macro, hub/api/v58_monitor)
--   keeps working unchanged.
--
-- Backward-compat contract:
--   - NULL timescale_map is legal; it means the row was written by a
--     pre-Phase-2 observer build or the observer's fallback path that
--     didn't populate per-timescale output.
--   - Readers that want per-timescale bias should check for NULL and
--     fall back to applying the top-level overall bias to every
--     timescale (exact Phase 1 behavior).
--
-- Safe to run multiple times (IF NOT EXISTS).

-- ─── macro_signals per-timescale map ─────────────────────────────────────────
ALTER TABLE macro_signals ADD COLUMN IF NOT EXISTS timescale_map JSONB;

-- GIN index on the JSONB column so readers that filter on a specific
-- timescale's bias (e.g. "find all rows where timescale_map->'1h'->>'bias' = 'BULL'")
-- get an index-backed query plan instead of a full table scan.
CREATE INDEX IF NOT EXISTS idx_macro_signals_timescale_map
    ON macro_signals USING gin (timescale_map jsonb_path_ops);

-- Verify the column exists
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'macro_signals' AND column_name = 'timescale_map';
