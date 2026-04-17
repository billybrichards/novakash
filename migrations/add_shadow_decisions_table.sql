-- Migration: add_shadow_decisions_table.sql
-- Phase D (TG Narrative Refactor — plans/serialized-drifting-clover.md).
--
-- Persists every strategy decision (LIVE + GHOST) per window so the
-- post-resolve shadow report can reconstruct what each strategy saw
-- without re-evaluating. Audit field ``mode`` records what the strategy
-- WAS at eval time, not what it is now — stable after YAML flips.
--
-- Idempotent: safe to run multiple times.

CREATE TABLE IF NOT EXISTS shadow_decisions (
    id                BIGSERIAL PRIMARY KEY,
    window_id         TEXT            NOT NULL,            -- e.g. "BTC-1712345678"
    timeframe         TEXT            NOT NULL,            -- '5m' | '15m'
    strategy_id       TEXT            NOT NULL,            -- e.g. 'v4_fusion'
    strategy_version  TEXT            NOT NULL,
    mode              TEXT            NOT NULL,            -- 'LIVE' | 'GHOST' at eval time
    action            TEXT            NOT NULL,            -- 'TRADE' | 'SKIP' | 'ERROR'
    direction         TEXT,                                -- 'UP' | 'DOWN' | NULL
    confidence        TEXT,                                -- 'HIGH' | 'MODERATE' | 'LOW' | 'NONE' | NULL
    confidence_score  DOUBLE PRECISION,
    entry_reason      TEXT,
    skip_reason       TEXT,
    gate_results      JSONB           DEFAULT '[]'::jsonb,
    metadata          JSONB           DEFAULT '{}'::jsonb,
    evaluated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shadow_decisions_window_strategy UNIQUE (window_id, strategy_id)
);

CREATE INDEX IF NOT EXISTS ix_shadow_decisions_window
    ON shadow_decisions (window_id, timeframe);

CREATE INDEX IF NOT EXISTS ix_shadow_decisions_strategy_time
    ON shadow_decisions (strategy_id, evaluated_at DESC);

CREATE INDEX IF NOT EXISTS ix_shadow_decisions_evaluated_at
    ON shadow_decisions (evaluated_at DESC);

COMMENT ON TABLE  shadow_decisions           IS 'Per-window strategy decision audit for shadow/live comparison reports.';
COMMENT ON COLUMN shadow_decisions.mode      IS 'LIVE or GHOST at eval time — do NOT update if YAML mode later changes.';
COMMENT ON COLUMN shadow_decisions.metadata  IS 'Free-form JSON: stake_usdc, regime, surface digest, etc.';
