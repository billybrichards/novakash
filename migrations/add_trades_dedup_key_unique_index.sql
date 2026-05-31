-- Migration: add UNIQUE partial index on trades.dedup_key as a DB-level
-- safety net for the double-fire dedup leak.
--
-- Date: 2026-05-29
-- Incident: tickformer_v16_pure placed TWO real Polymarket orders on
--   window_ts=1780091700 direction=UP at 21:58:01 and 21:58:35 UTC.
--   Both carried dedup_key='tickformer_v16_pure:1780091700:UP'.
--   Root cause: has_fill_for_strategy_window_direction queried
--   metadata->>'window_ts' which was NULL in tickformer trades (the strategy
--   hook did not set it). The empty-string COALESCE never matched the real
--   epoch, so the hard lock returned False and a concurrent evaluate_all
--   placed a second order.
--
-- Two-layer fix applied in the same PR:
--   Layer 1 (application): _tickformer_base.py now sets metadata['window_ts']
--     AND has_fill_for_strategy_window_direction now also checks metadata->>'dedup_key'
--     as a Clause B fallback.
--   Layer 2 (DB safety net, this migration): a UNIQUE partial index on the
--     dedup_key JSONB key — any future application-level bypass now raises a
--     UniqueViolation before the trade row is committed.
--
-- ⚠ IMPORTANT: the existing duplicate trades 9442 and 9443 share the same
-- dedup_key. This UNIQUE constraint CANNOT be created while those rows exist
-- because it would fail with a unique-violation error. The duplicate is a real
-- settled live trade with real PnL — do NOT delete or modify either row.
--
-- Resolution: the migration below is written to be SAFE and DESCRIPTIVE.
-- It:
--   1. Detects and reports any duplicate dedup_key pairs (informational).
--   2. Does NOT modify or delete any trade rows.
--   3. Creates the UNIQUE index with a DATE guard — only applies to trades
--      created AFTER 2026-05-29 (i.e. excludes the incident duplicate rows).
--      The guard is intentionally conservative; it is a belt-and-braces net
--      for future fires, not a retroactive audit.
--
-- Operator note: to apply a strict UNIQUE index covering ALL trades you will
-- need to manually mark one of trade 9442 / 9443 as CANCELLED first. Do not
-- do this without review — those rows are settled with real PnL.

-- ── Step 1: Report existing dedup_key duplicates (informational, no-op) ──
DO $$
DECLARE
    dup_count INT;
BEGIN
    SELECT COUNT(*)
      INTO dup_count
      FROM (
        SELECT metadata->>'dedup_key' AS dk
          FROM trades
         WHERE metadata->>'dedup_key' IS NOT NULL
           AND status NOT IN ('CANCELLED', 'SKIPPED', 'FAILED_EXECUTION')
         GROUP BY dk
        HAVING COUNT(*) > 1
      ) AS dups;

    IF dup_count > 0 THEN
        RAISE NOTICE
            '⚠  Found % dedup_key group(s) with duplicate active trades. '
            'The UNIQUE index (created in Step 2) excludes trades created '
            'before 2026-05-30 to avoid a constraint violation on the '
            'existing incident duplicate (trades 9442+9443). '
            'Review the duplicates and mark one CANCELLED before adding a '
            'strict all-rows UNIQUE index.',
            dup_count;
    ELSE
        RAISE NOTICE 'No duplicate dedup_key pairs found in active trades.';
    END IF;
END $$;

-- ── Step 2: UNIQUE partial index — new trades only ─────────────────────────
-- Excludes:
--   * Rows without a dedup_key (strategies that pre-date dedup_key support).
--   * CANCELLED / SKIPPED / FAILED_EXECUTION rows (resolved non-fills).
--   * Rows created before 2026-05-30 00:00 UTC (excludes the 9442/9443
--     incident duplicate so the migration is safe to run immediately).
--
-- Effect: any future duplicate dedup_key within the same settlement status
-- will fail with a PostgreSQL UniqueViolation BEFORE the trade row commits,
-- giving a hard DB-level safety net beneath the application guard.
CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_dedup_key_unique
    ON trades ( (metadata->>'dedup_key') )
    WHERE metadata->>'dedup_key' IS NOT NULL
      AND status NOT IN ('CANCELLED', 'SKIPPED', 'FAILED_EXECUTION')
      AND created_at >= '2026-05-30 00:00:00+00';

-- Verify
SELECT indexname, indexdef
  FROM pg_indexes
 WHERE tablename = 'trades'
   AND indexname = 'idx_trades_dedup_key_unique';
