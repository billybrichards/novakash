-- Migration: add calibrated_probability_tickformer_v{16,17,18,20} BTC sibling
-- columns to signal_evaluations.
--
-- Date: 2026-06-03
-- Owner PR (timesfm): #180. Engine consumer: novakash PR #654.
--
-- Why this lives in BOTH repos:
--   timesfm PR #180 is the canonical owner because it emits the values.
--   The engine's update_signal_evaluations_tickformer writer hardcodes the
--   four column names into INSERT...ON CONFLICT — if these columns are
--   missing at deploy time, EVERY tickformer SE write fails with a
--   parse-time error (entire statement rejected by Postgres), breaking the
--   raw probability_tickformer_v{N} persistence as collateral. This file
--   exists so the engine deploy is not blocked on cross-repo coordination.
--
-- Idempotent + additive: ADD COLUMN IF NOT EXISTS. Safe to run multiple times.
-- The timesfm migration applies the same DDL — duplicate apply is a no-op.
--
-- v17 column is created but never populated by the scorer — the offline 30d
-- iso fit degraded its held-out Brier/ECE, so no iso fit is packaged for it.
-- Column exists for surface symmetry only (so future strategies can read all
-- four uniformly).

ALTER TABLE signal_evaluations
    ADD COLUMN IF NOT EXISTS calibrated_probability_tickformer_v16 NUMERIC(10, 8),
    ADD COLUMN IF NOT EXISTS calibrated_probability_tickformer_v17 NUMERIC(10, 8),
    ADD COLUMN IF NOT EXISTS calibrated_probability_tickformer_v18 NUMERIC(10, 8),
    ADD COLUMN IF NOT EXISTS calibrated_probability_tickformer_v20 NUMERIC(10, 8);

-- Verify
SELECT column_name, data_type
  FROM information_schema.columns
 WHERE table_name = 'signal_evaluations'
   AND column_name LIKE 'calibrated_probability_tickformer_%'
 ORDER BY column_name;
