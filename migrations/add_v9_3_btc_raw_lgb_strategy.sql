-- Migration: seed v9_3_btc_raw_lgb strategy row in strategy_configs
-- Date: 2026-05-22
-- Purpose: Belt-and-braces seed for the new v9_3_btc_raw_lgb strategy
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
-- via runtime override or by Billy bumping the YAML version and re-deploying.
--
-- ON CONFLICT DO NOTHING — if the engine already seeded this (strategy_id,
-- version) tuple, leave the existing row untouched. The engine seeder uses
-- ON CONFLICT (strategy_id, version) DO UPDATE; this migration deliberately
-- uses DO NOTHING so it never clobbers later engine writes.
--
-- See also: migrations/add_probability_lgb_v9_3_btc_column.sql (peer migration
-- — adds the column the strategy reads from).
-- See also: migrations/add_v9_3_btc_tight_strategy.sql (sibling strategy).

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
    'v9_3_btc_raw_lgb',
    '1.0.0',
    'GHOST',
    'BTC',
    '5m',
    -- Raw YAML body — kept in sync with engine/strategies/configs/v9_3_btc_raw_lgb.yaml
    -- The engine's seed_registry_to_db() will re-UPSERT this from the
    -- canonical YAML file at boot, so this string is the belt-and-braces
    -- starting state only (matches the YAML byte-for-byte at PR time).
    $$name: v9_3_btc_raw_lgb
strategy_id: v9_3_btc_raw_lgb
version: "1.0.0"
status: GHOST
mode: GHOST
asset: BTC
timescale: 5m
hooks_file: v9_3_btc_raw_lgb.py
pre_gate_hook: evaluate_v9_3_btc_raw_lgb
sizing:
  type: kelly
  fraction: 0.025
  max_collateral_pct: 0.025
gates:
  - type: chainlink_freshness
gate_params:
  up_threshold: 0.72
  down_threshold: 0.20
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
    'v9_3_btc_raw_lgb.py',
    NOW(),
    NOW()
)
ON CONFLICT (strategy_id, version) DO NOTHING;

-- ── Verify ─────────────────────────────────────────────────────────────────
SELECT strategy_id, version, mode, asset, timescale, hooks_file
FROM strategy_configs
WHERE strategy_id = 'v9_3_btc_raw_lgb';
