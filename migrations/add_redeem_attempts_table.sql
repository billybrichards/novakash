-- Migration: add_redeem_attempts_table.sql
-- PR D: Track every Builder Relayer redeem attempt so we can skip
-- condition_ids that fail repeatedly (prevents hot-loop on a stuck
-- position that keeps burning relayer quota on every sweep).
--
-- Pair with:
--   - engine/adapters/persistence/pg_redeem_attempts.py
--   - Redeemer.redeem_position() uses recent_failures(condition_id, 24h)
--     to skip after N consecutive failures.
--
-- Idempotent: safe to run multiple times.

CREATE TABLE IF NOT EXISTS redeem_attempts (
    id              BIGSERIAL       PRIMARY KEY,
    condition_id    VARCHAR(66)     NOT NULL,
    attempted_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    outcome         VARCHAR(16)     NOT NULL,  -- SUCCESS | FAILED | COOLDOWN
    tx_hash         TEXT,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_redeem_attempts_condition_time
    ON redeem_attempts (condition_id, attempted_at DESC);

CREATE INDEX IF NOT EXISTS idx_redeem_attempts_outcome_time
    ON redeem_attempts (outcome, attempted_at DESC);
