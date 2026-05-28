-- Migration: add probability_tickformer_v20 column + seed v20 strategy config
-- Date: 2026-05-28
-- PR: fix/tickformer-strategies-actually-fire
--
-- WHAT THIS DOES:
--   1. Adds probability_tickformer_v20 NUMERIC(10,6) to signal_evaluations
--      (idempotent — ADD COLUMN IF NOT EXISTS).
--   2. Seeds tickformer_v20_adaptive_early into strategy_configs so the
--      registry can list it before the engine boots (ON CONFLICT DO NOTHING).
--
-- DEPENDENCY:
--   The probability_tickformer_v20 column is written by the engine's
--   DBClient.update_signal_evaluations_tickformer() method (added in this PR).
--   The timesfm-service must also emit "probability_tickformer_v20" on
--   /v4/snapshot.timescales.5m for values to appear. Until that timesfm PR
--   lands, the column will be NULL — the v20 strategy emits
--   tickformer_v20_not_available SKIP (forward-compatible).
--
-- IDEMPOTENT: safe to re-run.
--
-- DOWN-MIGRATION (manual):
--   ALTER TABLE signal_evaluations DROP COLUMN IF EXISTS probability_tickformer_v20;
--   DELETE FROM strategy_configs WHERE strategy_id = 'tickformer_v20_adaptive_early';

-- ── 1. signal_evaluations column ─────────────────────────────────────────────
ALTER TABLE signal_evaluations
    ADD COLUMN IF NOT EXISTS probability_tickformer_v20 NUMERIC(10, 6);

COMMENT ON COLUMN signal_evaluations.probability_tickformer_v20 IS
    'TickFormer v20 adaptive early-entry P(UP) from timesfm-service. '
    'NULL until timesfm sister PR is deployed. '
    'Added by fix/tickformer-strategies-actually-fire (2026-05-28).';

-- ── 2. strategy_configs seed ─────────────────────────────────────────────────
INSERT INTO strategy_configs (
    strategy_id,
    version,
    mode,
    asset,
    timescale,
    config_yaml,
    gates_json,
    sizing_json,
    hooks_file,
    created_at,
    updated_at
) VALUES (
    'tickformer_v20_adaptive_early',
    '1.0.0',
    'SHADOW',
    'ANY',
    '5m',
    $$name: tickformer_v20_adaptive_early
strategy_id: tickformer_v20_adaptive_early
version: "1.0.0"
status: SHADOW
mode: SHADOW
asset: ANY
timescale: 5m
hooks_file: tickformer_v20_adaptive_early.py
pre_gate_hook: evaluate_tickformer_v20_adaptive_early
sizing:
  type: kelly
  fraction: 0.025
  max_collateral_pct: 0.025
gates:
  - type: chainlink_freshness
gate_params:
  shadow_only: 1
  up_threshold: 0.90
  down_threshold: 0.10
  eval_offset_remaining_min: 120
  eval_offset_remaining_max: 280
  min_consecutive_pass_ticks: 1
  entry_cap: 0.93
  entry_floor_up: 0.60
  entry_cap_down: 0.90
  collateral_pct: 0.025
  gtc_cap: 0.96
risk:
  max_position_usd: 5
  max_daily_loss_usd: 20
$$,
    '[{"type":"chainlink_freshness"}]'::jsonb,
    '{"type":"kelly","fraction":0.025,"max_collateral_pct":0.025,"max_position_usd":5}'::jsonb,
    'tickformer_v20_adaptive_early.py',
    NOW(),
    NOW()
)
ON CONFLICT (strategy_id, version) DO NOTHING;

-- ── 3. Verify ─────────────────────────────────────────────────────────────────
SELECT
    column_name,
    data_type,
    numeric_precision,
    numeric_scale
FROM information_schema.columns
WHERE table_name = 'signal_evaluations'
  AND column_name = 'probability_tickformer_v20';

SELECT strategy_id, version, mode, asset, timescale, hooks_file
FROM strategy_configs
WHERE strategy_id = 'tickformer_v20_adaptive_early';
