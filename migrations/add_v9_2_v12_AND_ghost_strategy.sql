-- Migration: seed v9_2_v12_AND_ghost strategy row in strategy_configs
-- Date: 2026-05-24
-- Purpose: Belt-and-braces seed for the new v9_2_v12_AND_ghost strategy
--          ahead of the engine boot. The canonical write path is
--          StrategyRegistry.seed_registry_to_db() which runs on engine
--          startup and UPSERTs every loaded YAML config. This migration
--          ensures the row exists in GHOST mode even if the engine hasn't
--          booted yet against this DB, so the hub /api/strategies endpoint
--          can list it immediately.
--
-- IMPORTANT — DEFAULTS GHOST. Per feedback_no_auto_promote.md, Billy
-- promotes every strategy by hand. This migration MUST NOT default to
-- LIVE. If the engine seeder later re-upserts with the YAML state (which
-- is also GHOST in this PR), the row stays GHOST. Any LIVE flip happens
-- via runtime override or by Billy bumping the YAML version and
-- re-deploying.
--
-- ON CONFLICT DO NOTHING — if the engine already seeded this (strategy_id,
-- version) tuple, leave the existing row untouched. The engine seeder uses
-- ON CONFLICT (strategy_id, version) DO UPDATE; this migration deliberately
-- uses DO NOTHING so it never clobbers later engine writes.
--
-- AUDIT OPERATING POINT (RDS note #618):
--   UP   when p_v9_2 >= 0.725 AND p_v12 >= 0.750  -> 92.1% WR on n=369
--                                                    (Wilson LB 88.9%)
--   DOWN when p_v9_2 <= 0.250 AND p_v12 <= 0.200  -> 81.7% WR on n=202
--                                                    (Wilson LB 75.8%)
--
-- NO new probability columns needed — both probability_lgb_v9_2 and
-- probability_lgb_v12 already exist in signal_evaluations and are populated
-- by the LIVE v9.2 and v12 scorers.

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
    'v9_2_v12_AND_ghost',
    '1.0.0',
    'GHOST',
    'BTC',
    '5m',
    -- Raw YAML body — kept in sync with engine/strategies/configs/v9_2_v12_AND_ghost.yaml
    -- The engine's seed_registry_to_db() will re-UPSERT this from the
    -- canonical YAML file at boot, so this string is the belt-and-braces
    -- starting state only (matches the YAML byte-for-byte at PR time).
    $$name: v9_2_v12_AND_ghost
strategy_id: v9_2_v12_AND_ghost
version: "1.0.0"
status: GHOST
mode: GHOST
asset: BTC
timescale: 5m
hooks_file: v9_2_v12_AND_ghost.py
pre_gate_hook: evaluate_v9_2_v12_AND_ghost
sizing:
  type: kelly
  fraction: 0.025
  max_collateral_pct: 0.025
gates:
  - type: chainlink_freshness
gate_params:
  up_v92_threshold: 0.725
  up_v12_threshold: 0.750
  down_v92_threshold: 0.25
  down_v12_threshold: 0.20
  eval_offset_min: 60
  eval_offset_max: 210
  min_consecutive_pass_ticks: 1
  expected_asset: BTC
  entry_cap: 0.85
  collateral_pct: 0.025
  gtc_cap: 0.90
risk:
  max_position_usd: 5
  max_daily_loss_usd: 20
$$,
    '[{"type":"chainlink_freshness"}]'::jsonb,
    '{"type":"kelly","fraction":0.025,"max_collateral_pct":0.025,"max_position_usd":5}'::jsonb,
    'v9_2_v12_AND_ghost.py',
    NOW(),
    NOW()
)
ON CONFLICT (strategy_id, version) DO NOTHING;

-- Verify
SELECT strategy_id, version, mode, asset, timescale, hooks_file
FROM strategy_configs
WHERE strategy_id = 'v9_2_v12_AND_ghost';
