-- Migration: seed v9_3_btc_tight strategy row in strategy_configs
-- Date: 2026-05-22
-- Purpose: Belt-and-braces seed for the new v9_3_btc_tight strategy
--          ahead of the engine boot. Sibling of v9_3_btc_raw_lgb — both
--          read the same probability_lgb_v9_3_btc column but at different
--          operating points (tight reads p>=0.935/p<=0.065 on early-window
--          subsegment).
--
-- IMPORTANT — DEFAULTS GHOST. Per feedback_no_auto_promote.md, Billy
-- promotes every strategy by hand. This migration MUST NOT default to
-- LIVE. Any LIVE flip happens via runtime override or by Billy bumping
-- the YAML version and re-deploying.
--
-- ON CONFLICT DO NOTHING — if the engine already seeded this (strategy_id,
-- version) tuple, leave the existing row untouched.
--
-- See also: migrations/add_probability_lgb_v9_3_btc_column.sql (peer migration
-- — adds the column the strategy reads from).
-- See also: migrations/add_v9_3_btc_raw_lgb_strategy.sql (sibling strategy).

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
    'v9_3_btc_tight',
    '1.0.0',
    'GHOST',
    'BTC',
    '5m',
    -- Raw YAML body — kept in sync with engine/strategies/configs/v9_3_btc_tight.yaml
    $$name: v9_3_btc_tight
strategy_id: v9_3_btc_tight
version: "1.0.0"
status: GHOST
mode: GHOST
asset: BTC
timescale: 5m
hooks_file: v9_3_btc_tight.py
pre_gate_hook: evaluate_v9_3_btc_tight
sizing:
  type: kelly
  fraction: 0.025
  max_collateral_pct: 0.025
gates:
  - type: chainlink_freshness
gate_params:
  up_threshold: 0.935
  down_threshold: 0.065
  eval_offset_min: 20
  eval_offset_max: 170
  min_consecutive_pass_ticks: 1
  expected_asset: BTC
  entry_cap: 0.935
  collateral_pct: 0.025
  gtc_cap: 0.935
risk:
  max_position_usd: 5
  max_daily_loss_usd: 20
$$,
    '[{"type":"chainlink_freshness"}]'::jsonb,
    '{"type":"kelly","fraction":0.025,"max_collateral_pct":0.025,"max_position_usd":5}'::jsonb,
    'v9_3_btc_tight.py',
    NOW(),
    NOW()
)
ON CONFLICT (strategy_id, version) DO NOTHING;

-- ── Verify ─────────────────────────────────────────────────────────────────
SELECT strategy_id, version, mode, asset, timescale, hooks_file
FROM strategy_configs
WHERE strategy_id = 'v9_3_btc_tight';
