-- /desk Phase 1 — operator manual-pick journal.
--
-- One row per (window_epoch, asset, timeframe). Operator picks UP/DOWN/SKIP
-- while a window is open, then the app joins against
-- strategy_decisions_resolved to score the operator's hit-rate vs strategy
-- hit-rate over the last N windows.
--
-- Idempotency: the endpoint does UPSERT — a second POST for the same
-- (window_epoch, asset, timeframe) updates pick+notes, so the operator can
-- flip their pick up until the T-00:10 client-side lockout. Enforced by a
-- UNIQUE constraint.
--
-- This file mirrors the inline-migration in hub/main.py lifespan. Railway
-- boots re-apply on every start; extra CREATE IF NOT EXISTS is a no-op.

CREATE TABLE IF NOT EXISTS desk_picks (
    id             BIGSERIAL PRIMARY KEY,
    window_epoch   BIGINT NOT NULL,
    asset          TEXT NOT NULL DEFAULT 'BTC',
    timeframe      TEXT NOT NULL DEFAULT '5m',
    pick           TEXT NOT NULL CHECK (pick IN ('UP','DOWN','SKIP')),
    t_remaining_s  INTEGER NOT NULL,
    notes          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_desk_picks_window_asset_tf
    ON desk_picks (window_epoch, asset, timeframe);

CREATE INDEX IF NOT EXISTS ix_desk_picks_window
    ON desk_picks (window_epoch DESC);

COMMENT ON TABLE desk_picks IS
    '/desk Phase 1 — operator play-along manual picks per 5m Polymarket window.';
