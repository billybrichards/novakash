-- Migration: ROLLBACK / DOWN — tickformer_v20_adaptive_early SHADOW
-- Date: 2026-05-28
-- Pair: add_tickformer_v20_strategy.sql (UP)
--
-- Purpose: idempotent hard-rollback of the tickformer_v20_adaptive_early
--          strategy_configs row. The engine's YAML auto-loader will
--          re-seed it in SHADOW mode at next boot UNLESS the
--          corresponding YAML file is also removed in the same release
--          (see docs/TICKFORMER_GHOST_PROTOCOL.md "Hard rollback").
--
-- USAGE:
--   psql $DATABASE_URL -f migrations/rollback_tickformer_v20_strategy.sql
--
-- IDEMPOTENT: re-running is a no-op (DELETE WHERE the row is already
--             gone). Safe to apply multiple times during a chaos test.
--
-- NOT A SOFT ROLLBACK: this DELETES the row. To suspend trading
--             without losing the row history, instead UPDATE mode to
--             SHADOW (see docs/TICKFORMER_GHOST_PROTOCOL.md "Soft
--             rollback").
--
-- RUNTIME OVERRIDE CLEANUP: also clears any strategy_runtime_overrides
--             row that targets the same strategy_id so that a
--             re-applied UP migration does not inherit stale runtime
--             gate_params (e.g. a tier override left over from an
--             aborted GHOST promotion attempt).

BEGIN;

DELETE FROM strategy_configs
 WHERE strategy_id = 'tickformer_v20_adaptive_early';

-- strategy_runtime_overrides may not exist in every environment yet
-- (cluster-version drift). Wrap in a DO block so a missing table is
-- non-fatal.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_name = 'strategy_runtime_overrides'
    ) THEN
        DELETE FROM strategy_runtime_overrides
         WHERE strategy_id = 'tickformer_v20_adaptive_early';
    END IF;
END $$;

COMMIT;
