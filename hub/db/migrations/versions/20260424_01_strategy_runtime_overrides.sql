-- Migration 20260424_01 — strategy_runtime_overrides table (audit #291)
--
-- Closes hub note #224: "trading_configs is global; no per-strategy runtime
-- override surface; flipping v8_champion GHOST→LIVE requires a YAML PR +
-- rsync + engine restart (~15-30 min operator cycle)."
--
-- This table provides a DB-backed OVERRIDE layer that sits ABOVE YAML in the
-- config-resolution order. Engine polls the table every 30s (see
-- ``engine/strategies/runtime_override.py``) and applies overrides to the
-- StrategyConfig used inside ``_evaluate_one`` — so a PATCH to the hub API
-- takes effect within ~35s with no engine restart, no redeploy, no rsync.
--
-- Layering (same-lexer semantics as ``reference_config_layering.md``):
--   YAML defaults → env vars → DB ``strategy_runtime_overrides`` → final
--
-- Write path: hub ``PATCH /api/strategies/{id}/override``. Auth-gated
-- (JWT, same as /api/system/*). Engine is READ-ONLY on this table — it
-- never writes back, which keeps "engine owns decisions" clean and lets
-- the operator surface be the single source of truth for overrides.
--
-- Read path: engine cache refreshed every 30s, plus on startup, plus
-- on-demand via the hub-triggered refresh hook (future work — for now
-- the 30s TTL is the floor on cycle time).
--
-- NULL semantics:
--   - ``mode = NULL``  → inherit YAML mode
--   - ``params = NULL`` → inherit YAML gate_params verbatim
--   - Row absent → no override at all (identical to both fields NULL)
-- Storing a row with both fields NULL is legal but useless; the engine
-- treats it as a no-op. Operators wanting to clear an override should
-- DELETE the row (see ``DELETE /api/strategies/{id}/override``).
--
-- Governance + audit trail:
--   - ``updated_by`` = TokenData.username (who did the flip)
--   - ``updated_reason`` = free-form operator note ("smoke test", "v8 green
--     in shadow; promoting", etc.)
--   - ``updated_at`` — auto-set by DEFAULT NOW(), maintained by UPSERT path
-- Full row history is NOT kept in this table by design; git log of the
-- YAML file plus the audit-task thread provides the "why" narrative,
-- while this table is the authoritative current-runtime-state.
--
-- Why not extend ``strategy_configs``? That table is the engine-owned
-- shipping CATALOG (engine SEEDS rows). This is an operator-owned
-- OVERRIDE. Different write authority, different lifecycle, different
-- revert semantics (delete-row = revert vs history-preserving). Keeping
-- them separate keeps the "engine owns catalog, hub owns runtime" line
-- crisp — see feedback_no_auto_model_promotion.md.

CREATE TABLE IF NOT EXISTS strategy_runtime_overrides (
    strategy_id     VARCHAR(64)  NOT NULL PRIMARY KEY,
    mode            VARCHAR(16),                              -- LIVE | GHOST | DISABLED | NULL (inherit YAML)
    params          JSONB,                                    -- partial override of gate_params; NULL = inherit
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_by      VARCHAR(64),                              -- auth username who set this
    updated_reason  TEXT,                                     -- free-form operator note
    CONSTRAINT strategy_runtime_overrides_mode_check
        CHECK (mode IS NULL OR mode IN ('LIVE', 'GHOST', 'DISABLED'))
);

CREATE INDEX IF NOT EXISTS idx_strategy_runtime_overrides_updated_at
    ON strategy_runtime_overrides (updated_at DESC);
