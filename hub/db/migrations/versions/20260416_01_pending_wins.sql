-- Engine-managed snapshot tables read by Hub /api/positions/snapshot.
-- Written from engine.infrastructure.runtime._send_position_snapshot()
-- on every 15-min cadence + post-sweep.
--
-- Pair with Task 8 (Hub endpoint) and Task 9 (engine writer) of
-- docs/superpowers/plans/2026-04-16-telegram-redemption-visibility.md
--
-- Idempotent: safe to run multiple times.

CREATE TABLE IF NOT EXISTS poly_pending_wins (
    condition_id     TEXT PRIMARY KEY,
    value            DOUBLE PRECISION NOT NULL,
    window_end_utc   TIMESTAMPTZ NOT NULL,
    observed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pending_wins_window_end
    ON poly_pending_wins (window_end_utc);

CREATE TABLE IF NOT EXISTS redeemer_state (
    id                              BIGSERIAL PRIMARY KEY,
    cooldown_active                 BOOLEAN NOT NULL,
    cooldown_remaining_seconds      INTEGER NOT NULL DEFAULT 0,
    cooldown_resets_at              TIMESTAMPTZ,
    cooldown_reason                 TEXT,
    daily_quota_limit               INTEGER NOT NULL,
    quota_used_today                INTEGER NOT NULL,
    observed_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_redeemer_state_observed
    ON redeemer_state (observed_at DESC);
