-- Migration: seed tickformer_v20_adaptive_early strategy
-- Date: 2026-05-28
-- Purpose: Belt-and-braces seed for the new tickformer_v20_adaptive_early
--          SHADOW strategy ahead of engine boot. Sister to
--          add_tickformer_v17_v18_strategies.sql. The canonical write
--          path is StrategyRegistry.seed_registry_to_db() which UPSERTs
--          every loaded YAML config; this migration just guarantees the
--          row exists in SHADOW mode so /api/strategies can list it
--          before the engine has booted against this DB.
--
-- IMPORTANT — DEFAULTS SHADOW. Per feedback_no_auto_promote.md, Billy
-- promotes every strategy by hand. The strategy ALSO has a runtime
-- gate_params.shadow_only=1 kill switch which forces SKIP-with-record
-- even if mode is mis-set to LIVE. Two independent layers of "no live
-- trades" — both must be flipped before any real fire emits.
--
-- DEPENDENCY: timesfm sister PR (magic-model branch, NOT YET OPENED at
--   this migration's authoring) — adds probability_tickformer_v20
--   (NUMERIC) column to signal_evaluations + emits it on
--   /v4/snapshot.timescales.5m. Forward-compatible: if the column does
--   not exist yet, the engine surface short-circuits to
--   probability_tickformer_v20=None → strategy emits a clean
--   tickformer_v20_not_available SKIP.
--
-- CONVICTION TIERS (from RDS hub note #726):
--   thr 0.85 K=6 -> 227 fires, WR 85.9%, 32.6 trades/day
--   thr 0.88 K=6 -> 156 fires, WR 87.8%, 22.5 trades/day
--   thr 0.90 K=6 -> 111 fires, WR 91.0%, 15.9 trades/day (RECOMMENDED)
--   thr 0.92 K=6 ->  61 fires, WR 90.2%,  8.7 trades/day
--
-- DO NOT raise up_threshold above 0.90 — WR saturates per the
-- backtest; higher thresholds only halve volume without WR benefit.
--
-- ON CONFLICT DO NOTHING — leave existing engine-seeded rows untouched.
--
-- DOWN-MIGRATION (manual): see rollback_tickformer_v20_strategy.sql

-- ---------------------------------------------------------------------
-- tickformer_v20_adaptive_early (adaptive-K-aware, recommended thr 0.90)
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
  mutex_group: tickformer
  up_threshold: 0.90
  down_threshold: 0.10
  eval_offset_remaining_min: 60
  eval_offset_remaining_max: 240
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

-- Verify
SELECT strategy_id, version, mode, asset, timescale, hooks_file
FROM strategy_configs
WHERE strategy_id = 'tickformer_v20_adaptive_early'
ORDER BY strategy_id;
