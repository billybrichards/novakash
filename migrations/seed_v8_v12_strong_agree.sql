-- Migration: seed v8_v12_strong_agree strategy as GHOST
-- Date: 2026-05-01
-- Purpose: Register v8_v12_strong_agree (v8_champion gate stack + strong v9/v12
--          LGB agreement gate) into strategy_configs so the engine picks it up
--          on next boot. Mode = GHOST (shadow only — no real money).
--
-- Thesis (Hub note #298): v8_champion_lgb_only + v12 strong-agreement gate
-- (UP both p_v9 & p_v12 >= 0.65; DOWN both <= 0.35) lifted backtest WR from
-- 64% -> 98.7% (n=1,182). This claim was derived from a backward-looking
-- signal-health study and needs out-of-sample validation before any LIVE flip.
--
-- The engine auto-seeds strategy_configs from YAML on every boot
-- (see strategies/registry.py::seed_registry_to_db). This file exists so the
-- operator can apply the seed manually without an engine restart, and to
-- document the GHOST-mode constraint at the SQL layer.
--
-- Idempotent — ON CONFLICT updates existing rows. Safe to re-run.
--
-- Apply via:
--   ssh ... 'sudo -u novakash bash -c "set -a; source ...env; set +a; \
--       psql ... -f /tmp/seed_v8_v12_strong_agree.sql"'

INSERT INTO strategy_configs (
    strategy_id,
    version,
    mode,
    asset,
    timescale,
    config_yaml,
    gates_json,
    sizing_json,
    hooks_file
) VALUES (
    'v8_v12_strong_agree',
    '1.0.0',
    'GHOST',
    'BTC',
    '5m',
    -- config_yaml is the verbatim contents of
    -- engine/strategies/configs/v8_v12_strong_agree.yaml. The engine reseeds
    -- this from disk on every boot, so the value here is only authoritative
    -- between manual apply and next engine restart.
    $YAML$
name: v8_v12_strong_agree
version: "1.0.0"
mode: GHOST
asset: BTC
timescale: 5m
gates: []
gate_params:
  min_offset_sec: 24
  max_offset_sec: 200
  tradeable_v4_regimes: [volatile_trend, chop, risk_off, calm_trend]
  block_down_vpin_regimes: [TRANSITION]
  block_up_vpin_regimes: [TRANSITION]
  use_classifier_bucket: false
  lgb_dist_min_down: 0.10
  lgb_dist_min_up: 0.15
  fill_band_min: 0.00
  fill_band_max: 0.82
  up_min_fill_price: 0.55
  up_require_both_buckets: false
  down_min_fill_price: 0.15
  blocked_utc_hours: [0, 1, 2, 3, 4, 5]
  source_agreement_require_chainlink: true
  source_agreement_require_tiingo: true
  skip_on_oracle_disagree: true
  vpin_min: 0.40
  vpin_max: 1.0
  post_loss_cooldown_min: 20
  transition_strong_bypass_enabled: true
  transition_bypass_min_avg_pct_delta: 0.05
  transition_bypass_min_lgb_dist: 0.20
  delta_gate_enabled: true
  min_alignment_bps: 1.0
  min_consecutive_pass_ticks: 3
  exit_monitor_enabled: true
  exit_shadow_mode: true
  exit_eval_start_offset: 48
  exit_eval_end_offset: 30
  exit_max_retries: 1
  exit_retry_timeout_seconds: 5
  exit_mark_min_pct: 0.3145
  exit_mark_ticks: 6
  primary_signal_source: lgb_v9_v12_strong_agree
  prefer_raw_probability: true
  combo_strong_agree_up_min: 0.65
  combo_strong_agree_down_max: 0.35
sizing:
  type: custom
  custom_hook: clob_sizing
  fraction: 0.025
  max_collateral_pct: 0.058
  schedule:
    - { threshold: 0.55, modifier: 2.0, label: "high_conv_full" }
    - { threshold: 0.35, modifier: 1.2, label: "mid" }
    - { threshold: 0.25, modifier: 1.0, label: "low" }
    - { threshold: 0.0,  modifier: 0.0, label: "skip" }
hooks_file: v8_v12_strong_agree.py
pre_gate_hook: evaluate_v8_v12_strong_agree
$YAML$,
    '[]'::jsonb,
    -- sizing_json copied from v12_lgb_combo (clob_sizing schedule).
    $JSON${
      "type": "custom",
      "custom_hook": "clob_sizing",
      "fraction": 0.025,
      "max_collateral_pct": 0.058,
      "schedule": [
        {"threshold": 0.55, "modifier": 2.0, "label": "high_conv_full"},
        {"threshold": 0.35, "modifier": 1.2, "label": "mid"},
        {"threshold": 0.25, "modifier": 1.0, "label": "low"},
        {"threshold": 0.0,  "modifier": 0.0, "label": "skip"}
      ]
    }$JSON$::jsonb,
    'v8_v12_strong_agree.py'
)
ON CONFLICT (strategy_id, version) DO UPDATE SET
    mode        = EXCLUDED.mode,
    asset       = EXCLUDED.asset,
    timescale   = EXCLUDED.timescale,
    config_yaml = EXCLUDED.config_yaml,
    gates_json  = EXCLUDED.gates_json,
    sizing_json = EXCLUDED.sizing_json,
    hooks_file  = EXCLUDED.hooks_file,
    updated_at  = NOW();

-- ── Verify ─────────────────────────────────────────────────────────────────
SELECT strategy_id, version, mode, asset, timescale, hooks_file, updated_at
FROM strategy_configs
WHERE strategy_id = 'v8_v12_strong_agree';
