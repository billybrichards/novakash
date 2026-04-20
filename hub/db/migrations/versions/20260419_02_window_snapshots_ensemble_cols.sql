-- 2026-04-19 (PR 02) — window_snapshots: v5_ensemble probability surface.
--
-- Unblocks historical counterfactual WR analysis (p_lgb alone vs
-- p_classifier alone vs ensemble p_up) across browser reloads and
-- across operators. Previously the three ensemble components lived
-- only in-memory on the timesfm box (`http://16.52.14.182:8080/v4/snapshot`
-- per-timescale block: `probability_up`, `probability_lgb`,
-- `probability_classifier`, `ensemble_config`) and in a session-only
-- FE ring buffer (`frontend/src/components/monitor/ComparativeWRCard.jsx`).
-- Zero historical retention → cannot evaluate which component drives WR.
--
-- Why explicit new columns (not reuse of `probability_up` /
-- `probability_raw` / `v2_probability_up`):
--   * Those columns have ambiguous history across v2/v3/v4 and were
--     NULL in 300/300 resolved rows sampled on 2026-04-19 (no writer).
--   * `ensemble_*` names are unambiguous tomorrow. Explicit > clever.
--
-- Intended consumers:
--   * scripts/ops/shadow_analysis.py (reference_shadow_analysis.md)
--   * future hub endpoint `/api/v5-ensemble/wr-comparison` (follow-up PR)
--   * follow-up FE wire-up of `ComparativeWRCard` (follow-up PR)
--
-- Additive + idempotent. All columns nullable — rows written before
-- this migration stay valid. Rows written while v5_ensemble is in a
-- fallback state (classifier head unavailable) carry NULLs for the
-- components that weren't produced.

ALTER TABLE window_snapshots
    ADD COLUMN IF NOT EXISTS ensemble_p_up          DOUBLE PRECISION;
ALTER TABLE window_snapshots
    ADD COLUMN IF NOT EXISTS ensemble_p_lgb         DOUBLE PRECISION;
ALTER TABLE window_snapshots
    ADD COLUMN IF NOT EXISTS ensemble_p_classifier  DOUBLE PRECISION;
ALTER TABLE window_snapshots
    ADD COLUMN IF NOT EXISTS ensemble_mode          VARCHAR(32);
ALTER TABLE window_snapshots
    ADD COLUMN IF NOT EXISTS ensemble_disagreement  DOUBLE PRECISION;
ALTER TABLE window_snapshots
    ADD COLUMN IF NOT EXISTS ensemble_model_version TEXT;

COMMENT ON COLUMN window_snapshots.ensemble_p_up          IS
    'v5_ensemble final blended p_up at eval time (null when snapshot missing/malformed).';
COMMENT ON COLUMN window_snapshots.ensemble_p_lgb         IS
    'v5_ensemble LGB component, pre-blend (null when ensemble disabled on timesfm side).';
COMMENT ON COLUMN window_snapshots.ensemble_p_classifier  IS
    'v5_ensemble Path 1 classifier component, pre-blend (null when classifier head unavailable).';
COMMENT ON COLUMN window_snapshots.ensemble_mode          IS
    'ensemble_config.mode: blend | fallback_lgb_only | disabled (null when config absent).';
COMMENT ON COLUMN window_snapshots.ensemble_disagreement  IS
    '|p_lgb - p_classifier| at eval time (null when one or both components missing).';
COMMENT ON COLUMN window_snapshots.ensemble_model_version IS
    'Classifier head version string (populated even in fallback when timesfm reports it).';
