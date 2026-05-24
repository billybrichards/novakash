-- Migration: seed v9_5_eth_pure_lgb strategy row in strategy_configs
-- Date: 2026-05-24
-- Purpose: Belt-and-braces seed for the new v9_5_eth_pure_lgb strategy
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
-- WALK-FORWARD CV (5×4d, 16d OOF, 22.66d corpus, 67-feature schema):
--   UP   p ≥ 0.915 → 90.3% WR (n=1330, Wilson LB 88.6%), ~83.1 fires/day
--   DOWN p ≤ 0.095 → 90.4% WR (n=1416, Wilson LB 88.7%), ~88.5 fires/day
-- eval_offset band [60, 210] drops the weak Δ=240s tail.
--
-- WHY PURE (vs the existing blend): the LIVE app/v2_scorer.py:blend_ensemble
-- path mixes the v9.5 LGB head with the TimesFM HF classifier, but the
-- classifier saturates at ~0.84 which caps the blend at ~0.92 (RDS notes
-- #631, #632). PURE reads the un-blended LGB+iso output so the model's own
-- high-conviction tail is preserved. CV shows ~10× more fires than blend at
-- the same 90% WR target.
--
-- Companion blend strategy (renamed in this same PR):
--   v9_5_eth_blend (née v9_5_eth_raw_lgb)
--
-- NEW probability column needed — probability_lgb_v9_5_eth_pure (added by
-- migrations/add_probability_lgb_v9_5_eth_pure.sql in this same PR).

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
    'v9_5_eth_pure_lgb',
    '1.0.0',
    'GHOST',
    'ETH',
    '5m',
    -- Raw YAML body — kept in sync with engine/strategies/configs/v9_5_eth_pure_lgb.yaml
    -- The engine's seed_registry_to_db() will re-UPSERT this from the
    -- canonical YAML file at boot, so this string is the belt-and-braces
    -- starting state only (matches the YAML byte-for-byte at PR time).
    $$name: v9_5_eth_pure_lgb
strategy_id: v9_5_eth_pure_lgb
version: "1.0.0"
status: GHOST
mode: GHOST
asset: ETH
timescale: 5m
hooks_file: v9_5_eth_pure_lgb.py
pre_gate_hook: evaluate_v9_5_eth_pure_lgb
sizing:
  type: kelly
  fraction: 0.025
  max_collateral_pct: 0.025
gates:
  - type: chainlink_freshness
gate_params:
  up_threshold: 0.915
  down_threshold: 0.095
  eval_offset_min: 60
  eval_offset_max: 210
  min_consecutive_pass_ticks: 1
  expected_asset: ETH
  entry_cap: 0.93
  collateral_pct: 0.025
  gtc_cap: 0.96
risk:
  max_position_usd: 5
  max_daily_loss_usd: 20
$$,
    '[{"type":"chainlink_freshness"}]'::jsonb,
    '{"type":"kelly","fraction":0.025,"max_collateral_pct":0.025,"max_position_usd":5}'::jsonb,
    'v9_5_eth_pure_lgb.py',
    NOW(),
    NOW()
)
ON CONFLICT (strategy_id, version) DO NOTHING;

-- Verify
SELECT strategy_id, version, mode, asset, timescale, hooks_file
FROM strategy_configs
WHERE strategy_id = 'v9_5_eth_pure_lgb';
