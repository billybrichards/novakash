-- Task #196 — Redeemer exponential backoff on 429.
-- Extends the redeemer_state snapshot table with backoff visibility so the
-- Hub /api/positions/snapshot endpoint + FE PositionSnapshotBar can render
-- why the redeemer is skipping ticks even when the server-reported cooldown
-- appears inactive.
--
-- Pair with engine.execution.redeemer._trip_backoff() and
-- engine.persistence.db_client.upsert_redeemer_state (new kwargs).
--
-- Idempotent: safe to run multiple times.

ALTER TABLE redeemer_state
    ADD COLUMN IF NOT EXISTS backoff_active             BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS backoff_remaining_seconds  INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS consecutive_429_count      INTEGER NOT NULL DEFAULT 0;
