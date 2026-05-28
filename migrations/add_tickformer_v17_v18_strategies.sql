-- Migration: seed tickformer_v17_sniper + tickformer_v18_t180 strategies
-- Date: 2026-05-28
-- Purpose: Belt-and-braces seed for the new tickformer_v17_sniper and
--          tickformer_v18_t180 SHADOW strategies ahead of engine boot.
--          Sister to add_tickformer_v16_pure_strategy.sql. The canonical
--          write path is StrategyRegistry.seed_registry_to_db() which
--          UPSERTs every loaded YAML config; this migration just
--          guarantees the rows exist in SHADOW mode so /api/strategies
--          can list them before the engine has booted against this DB.
--
-- IMPORTANT — DEFAULTS SHADOW. Per feedback_no_auto_promote.md, Billy
-- promotes every strategy by hand. Both strategies ALSO have a runtime
-- gate_params.shadow_only=1 kill switch which forces SKIP-with-record
-- even if mode is mis-set to LIVE. Two independent layers of "no live
-- trades" — both must be flipped before any real fire emits.
--
-- DEPENDENCY: timesfm sister PR (magic-model branch) — adds
--   probability_tickformer_v17 (NUMERIC) and
--   probability_tickformer_v18 (NUMERIC) columns to signal_evaluations
--   + emits them on /v4/snapshot.timescales.5m. Forward-compatible:
--   if the columns don't exist yet, the engine surface short-circuits
--   to probability_tickformer_v1[78]=None → strategy emits a clean
--   tickformer_v1[78]_not_available SKIP.
--
-- CONVICTION TIERS:
--   v17 sniper — thr 0.85 / rem 60-140  -> ~94-98% WR (low density)
--   v18 t180   — thr 0.90 / rem 60-220  -> ~92% WR (recommended)
--
-- ON CONFLICT DO NOTHING — leave existing engine-seeded rows untouched.
--
-- DOWN-MIGRATION (manual):
--   DELETE FROM strategy_configs
--    WHERE strategy_id IN ('tickformer_v17_sniper', 'tickformer_v18_t180')
--      AND version = '1.0.0';

-- ---------------------------------------------------------------------
-- tickformer_v17_sniper (precision sniper)
-- ---------------------------------------------------------------------
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
    'tickformer_v17_sniper',
    '1.0.0',
    'SHADOW',
    'ANY',
    '5m',
    $$name: tickformer_v17_sniper
strategy_id: tickformer_v17_sniper
version: "1.0.0"
status: SHADOW
mode: SHADOW
asset: ANY
timescale: 5m
hooks_file: tickformer_v17_sniper.py
pre_gate_hook: evaluate_tickformer_v17_sniper
sizing:
  type: kelly
  fraction: 0.025
  max_collateral_pct: 0.025
gates:
  - type: chainlink_freshness
gate_params:
  shadow_only: 1
  up_threshold: 0.85
  down_threshold: 0.15
  eval_offset_remaining_min: 60
  eval_offset_remaining_max: 140
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
    'tickformer_v17_sniper.py',
    NOW(),
    NOW()
)
ON CONFLICT (strategy_id, version) DO NOTHING;

-- ---------------------------------------------------------------------
-- tickformer_v18_t180 (balanced t-180, recommended)
-- ---------------------------------------------------------------------
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
    'tickformer_v18_t180',
    '1.0.0',
    'SHADOW',
    'ANY',
    '5m',
    $$name: tickformer_v18_t180
strategy_id: tickformer_v18_t180
version: "1.0.0"
status: SHADOW
mode: SHADOW
asset: ANY
timescale: 5m
hooks_file: tickformer_v18_t180.py
pre_gate_hook: evaluate_tickformer_v18_t180
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
  eval_offset_remaining_min: 60
  eval_offset_remaining_max: 220
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
    'tickformer_v18_t180.py',
    NOW(),
    NOW()
)
ON CONFLICT (strategy_id, version) DO NOTHING;

-- Verify
SELECT strategy_id, version, mode, asset, timescale, hooks_file
FROM strategy_configs
WHERE strategy_id IN ('tickformer_v17_sniper', 'tickformer_v18_t180')
ORDER BY strategy_id;
