-- ============================================================
-- Audit #396 review fix 2 — phase discriminator on ticks_v2_probability
-- ============================================================
--
-- Two call sites in engine/strategies/five_min_vpin.py write to
-- ticks_v2_probability within the same _evaluate_window invocation:
--
--   1. pre-eval feature snapshot (`phase = 'pre_eval'`)
--   2. decision-path snapshot   (`phase = 'decision'`)
--
-- Without a discriminator, analytics see "duplicate" rows for the same
-- (asset, seconds_to_close, ts ~equal) tuple. The phase column lets
-- consumers filter or aggregate as needed.
--
-- Idempotent — safe to re-apply. Existing rows (none on RDS yet, but on
-- Railway there are 5.6M) get the default 'decision' which preserves their
-- backward-compatible reading.
-- ============================================================

ALTER TABLE ticks_v2_probability
    ADD COLUMN IF NOT EXISTS phase TEXT NOT NULL DEFAULT 'decision';

-- Convenience index — analytics commonly filter by phase + ts.
CREATE INDEX IF NOT EXISTS idx_ticks_v2_prob_phase_ts
    ON ticks_v2_probability (phase, ts DESC);
