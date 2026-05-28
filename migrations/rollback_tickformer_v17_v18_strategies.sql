-- Migration: ROLLBACK / DOWN — tickformer SHADOW strategies
-- Date: 2026-05-28
-- Pair: add_tickformer_v17_v18_strategies.sql (UP)
--       add_tickformer_v16_pure_strategy.sql  (UP, earlier PR)
--
-- Purpose: idempotent hard-rollback of the tickformer_v* SHADOW
--          strategy_configs rows. Removes ALL THREE sibling rows
--          (v16_pure, v17_sniper, v18_t180) so a single rollback
--          drops the whole magic-model family at once. The engine's
--          YAML auto-loader will re-seed them in SHADOW mode at
--          next boot UNLESS the corresponding YAML files are also
--          removed in the same release (see
--          docs/TICKFORMER_GHOST_PROTOCOL.md "Hard rollback").
--
-- USAGE:
--   psql $DATABASE_URL -f migrations/rollback_tickformer_v17_v18_strategies.sql
--
-- IDEMPOTENT: re-running is a no-op (DELETE WHERE the rows are
--             already gone). Safe to apply multiple times during
--             a chaos test.
--
-- NOT A SOFT ROLLBACK: this DELETES the rows. To suspend trading
--             without losing the row history, instead UPDATE mode
--             to SHADOW (see docs/TICKFORMER_GHOST_PROTOCOL.md
--             "Soft rollback").
--
-- RUNTIME OVERRIDE CLEANUP: also clears any
--             strategy_runtime_overrides row that targets the
--             same strategy_ids so that a re-applied UP migration
--             does not inherit stale runtime gate_params (e.g. a
--             tier=TIER_B override left over from an aborted GHOST
--             promotion attempt).

BEGIN;

DELETE FROM strategy_configs
 WHERE strategy_id IN (
     'tickformer_v16_pure',
     'tickformer_v17_sniper',
     'tickformer_v18_t180'
 );

-- strategy_runtime_overrides may not exist in every environment
-- yet (cluster-version drift). Wrap in a DO block so a missing
-- table is non-fatal.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_name = 'strategy_runtime_overrides'
    ) THEN
        DELETE FROM strategy_runtime_overrides
         WHERE strategy_id IN (
             'tickformer_v16_pure',
             'tickformer_v17_sniper',
             'tickformer_v18_t180'
         );
    END IF;
END $$;

COMMIT;
