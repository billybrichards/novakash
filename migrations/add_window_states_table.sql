-- Migration: add_window_states_table.sql
-- Phase 5 (CA-04): Single-owner window traded/resolved state table.
--
-- Replaces the triple in-memory dedup sets:
--   - FiveMinVPINStrategy._traded_windows
--   - CLOBReconciler._known_resolved
--   - Orchestrator._resolved_by_order_manager
--
-- Idempotent: safe to run multiple times.

CREATE TABLE IF NOT EXISTS window_states (
    window_key      VARCHAR(64)     PRIMARY KEY,
    asset           VARCHAR(10)     NOT NULL,
    window_ts       BIGINT          NOT NULL,
    duration_secs   INTEGER         NOT NULL DEFAULT 300,
    traded_at       TIMESTAMPTZ,
    traded_order_id TEXT,
    resolved_at     TIMESTAMPTZ,
    resolved_outcome TEXT,
    resolved_pnl_usd NUMERIC,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_window_states_traded_at
    ON window_states (traded_at) WHERE traded_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_window_states_resolved_at
    ON window_states (resolved_at) WHERE resolved_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_window_states_asset_ts
    ON window_states (asset, window_ts);
