-- Migration: seed tickformer_v18_golden strategy config
-- Date: 2026-05-29
-- PR: fix/tickformer-threshold-recal-2026-05-28
--
-- WHAT THIS DOES:
--   Seeds tickformer_v18_golden into strategy_configs so the registry
--   can list it before the engine boots (ON CONFLICT DO NOTHING).
--
--   No new column is needed — this strategy re-uses the existing
--   probability_tickformer_v18 column (added by
--   add_tickformer_v17_v18_strategies.sql). Dependency: that migration
--   must have run before this one.
--
-- BACKGROUND (hub notes #728/#729, 7d BTC 5m sweep):
--   Live golden pocket: prob 0.80-0.85 × eval_offset_remaining 120-179s
--   → n=13, WR 100%.
--   After RECAL 2026-05-28, tickformer_v18_t180 tightened its band to
--   60-119s (sniper pocket). v18_golden covers the mid-conviction
--   early-window band that v18_t180 no longer captures.
--
-- DEFAULTS — SHADOW MODE. Per feedback_no_auto_promote.md, Billy promotes
-- every strategy by hand. shadow_only=1 kill switch enforces SKIP-with-
-- record even if mode is mis-set. Two independent layers before any live fire.
--
-- IDEMPOTENT: safe to re-run (ON CONFLICT DO NOTHING).
--
-- DOWN-MIGRATION (manual):
--   DELETE FROM strategy_configs
--    WHERE strategy_id = 'tickformer_v18_golden'
--      AND version = '1.0.0';

-- ── strategy_configs seed ─────────────────────────────────────────────────────
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
    'tickformer_v18_golden',
    '1.0.0',
    'SHADOW',
    'ANY',
    '5m',
    $$name: tickformer_v18_golden
strategy_id: tickformer_v18_golden
version: "1.0.0"
status: SHADOW
mode: SHADOW
asset: ANY
timescale: 5m
hooks_file: tickformer_v18_golden.py
pre_gate_hook: evaluate_tickformer_v18_golden
sizing:
  type: kelly
  fraction: 0.025
  max_collateral_pct: 0.025
gates:
  - type: chainlink_freshness
gate_params:
  shadow_only: 1
  mutex_group: tickformer
  up_threshold: 0.80
  down_threshold: 0.20
  eval_offset_remaining_min: 120
  eval_offset_remaining_max: 179
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
    'tickformer_v18_golden.py',
    NOW(),
    NOW()
)
ON CONFLICT (strategy_id, version) DO NOTHING;

-- ── Verify ────────────────────────────────────────────────────────────────────
SELECT strategy_id, version, mode, asset, timescale, hooks_file
FROM strategy_configs
WHERE strategy_id = 'tickformer_v18_golden';
