-- Migration: seed tickformer_v16_pure strategy row in strategy_configs
-- Date: 2026-05-28
-- Purpose: Belt-and-braces seed for the new tickformer_v16_pure SHADOW
--          strategy ahead of the engine boot. The canonical write path
--          is StrategyRegistry.seed_registry_to_db() which UPSERTs every
--          loaded YAML config; this migration just guarantees the row
--          exists in SHADOW mode so /api/strategies can list it before
--          the engine has booted against this DB.
--
-- IMPORTANT — DEFAULTS SHADOW. Per feedback_no_auto_promote.md, Billy
-- promotes every strategy by hand. This strategy ALSO has a runtime
-- gate_params.shadow_only=1 kill switch which forces SKIP-with-record
-- even if mode is mis-set to LIVE. Two independent layers of "no live
-- trades" — both must be flipped before any real fire emits.
--
-- DEPENDENCY: timesfm sister PR (magic-model branch) — adds
--   probability_tickformer_v16 (NUMERIC) and tickformer_trade_signal
--   (text: UP/DOWN/HOLD) columns to signal_evaluations + emits them on
--   /v4/snapshot.timescales.5m + new GET /v5/probability and
--   GET /v5/trade_signal endpoints. This migration is forward-compatible:
--   if the columns don't exist yet, the engine surface short-circuits to
--   probability_tickformer_v16=None → strategy emits a clean
--   tickformer_v16_not_available SKIP.
--
-- CONVICTION TIERS (offline backtest, v16 spec):
--   Tier A — thr 0.85, eval_offset_remaining 60s   -> ~95% WR
--   Tier B — thr 0.90, eval_offset_remaining 60s   -> ~96% WR
--   Tier C — thr 0.85, eval_offset_remaining 180s  -> ~94% WR (default)
--   Tier D — thr 0.85, eval_offset_remaining 220s  -> ~88% WR (dream)
--
-- ON CONFLICT DO NOTHING — leave existing engine-seeded row untouched.

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
    'tickformer_v16_pure',
    '1.0.0',
    'SHADOW',
    'ANY',
    '5m',
    -- Raw YAML body — kept in sync with
    -- engine/strategies/configs/tickformer_v16_pure.yaml. Engine
    -- seed_registry_to_db() will re-UPSERT this from the canonical YAML
    -- at boot, so this string is the belt-and-braces starting state only.
    $$name: tickformer_v16_pure
strategy_id: tickformer_v16_pure
version: "1.0.0"
status: SHADOW
mode: SHADOW
asset: ANY
timescale: 5m
hooks_file: tickformer_v16_pure.py
pre_gate_hook: evaluate_tickformer_v16_pure
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
    'tickformer_v16_pure.py',
    NOW(),
    NOW()
)
ON CONFLICT (strategy_id, version) DO NOTHING;

-- Verify
SELECT strategy_id, version, mode, asset, timescale, hooks_file
FROM strategy_configs
WHERE strategy_id = 'tickformer_v16_pure';
