-- Migration 20260417_03 — strategy_configs table (Option C.1)
--
-- Mirrors engine/strategies/configs/*.yaml into the database so the hub
-- can serve /api/strategies without needing the engine/ directory on the
-- hub host (the AWS deploy tree does NOT ship engine/; see the PR #253
-- rsync-workaround fix for the filesystem-only version of this).
--
-- Write path: the engine's StrategyRegistry.seed_registry_to_db() calls
-- an idempotent UPSERT here at startup after load_all() completes. The
-- composite primary key (strategy_id, version) means re-seeding the same
-- shipping version is a no-op; bumping version in YAML lands a new row
-- and updated_at on the old row is untouched — full history preserved.
--
-- Read path: hub /api/strategies prefers this table; falls back to the
-- filesystem resolver from PR #253 only when the table is empty (fresh
-- cluster, engine never booted, etc.). See hub/api/strategies.py.
--
-- Governance: hub has SELECT-only on this table. ALL writes go through
-- the engine's seed path. This keeps the "engine owns decisions" invariant
-- clean and prevents the UI from becoming an auto-promotion surface —
-- see feedback_no_auto_model_promotion.md.
--
-- Why not reuse trading_configs? That table is for operator-managed
-- runtime toggles (bet_fraction, caps, paper/live activation). This
-- table is for the strategy CATALOG — what strategies exist, their
-- gate pipeline structure, their shipped defaults. Different lifecycle,
-- different write authority, different key shape.

CREATE TABLE IF NOT EXISTS strategy_configs (
    strategy_id   VARCHAR(64)  NOT NULL,
    version       VARCHAR(32)  NOT NULL,
    mode          VARCHAR(16)  NOT NULL,                        -- LIVE | GHOST | DISABLED
    asset         VARCHAR(16),
    timescale     VARCHAR(16),
    config_yaml   TEXT         NOT NULL,                        -- raw YAML source of truth
    gates_json    JSONB,                                        -- parsed gates[] (denorm, may be null for hook-only strategies)
    sizing_json   JSONB,                                        -- parsed sizing{}
    hooks_file    VARCHAR(256),
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (strategy_id, version)
);

CREATE INDEX IF NOT EXISTS idx_strategy_configs_strategy
    ON strategy_configs (strategy_id);
CREATE INDEX IF NOT EXISTS idx_strategy_configs_mode
    ON strategy_configs (mode)
    WHERE mode IN ('LIVE', 'GHOST');  -- DISABLED filtered at source
CREATE INDEX IF NOT EXISTS idx_strategy_configs_updated
    ON strategy_configs (updated_at DESC);
