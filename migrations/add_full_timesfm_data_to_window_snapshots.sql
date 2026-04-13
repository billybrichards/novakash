-- Migration: add_full_timesfm_data_to_window_snapshots.sql
-- Date: 2026-04-13
-- Purpose: Add ALL timesfm-repo v1/v2/v3/v4 data columns to window_snapshots
--          to ensure complete persistence for gate context and future strategy updates.
--
-- This migration captures the FULL data surface from v4_snapshot_assembler.py:
--   - v2: probability_up, probability_raw, model_version, quantiles, delta_bucket
--   - v3: composite, sub_signals (elm, cascade, taker, oi, funding, vpin, momentum)
--   - v3: regime, regime_confidence, regime_persistence, regime_transition
--   - v3: cascade_state (strength, signal), alignment (direction_agreement, timescales)
--   - v4: expected_move_bps, vol_forecast_bps, downside_var_bps_p10, upside_var_bps_p90
--   - v4: feature_freshness_ms, time_to_target_s
--   - v4: macro (per-timescale bias, confidence, direction_gate, size_modifier)
--   - v4: consensus (safe_to_trade, max_divergence_bps, source agreement scores)
--   - v4: events (max_impact_in_window, minutes_to_next_high_impact)
--   - v4: polymarket CLOB data (implied_prob, vig, imbalance)
--   - v4: orderflow (liq_pressure, forced_liquidations, taker_flow)
--   - v4: strategy recommendations (conviction, action, collateral_pct, sl_tp)
--
-- Safe to run multiple times (ADD COLUMN IF NOT EXISTS throughout)

-- ─── v2/v3 core predictions ──────────────────────────────────────────────
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS probability_up DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS probability_raw DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS model_version VARCHAR(50);
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS quantiles_p10 DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS quantiles_p25 DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS quantiles_p50 DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS quantiles_p75 DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS quantiles_p90 DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS delta_bucket INTEGER;

-- ─── v3 composite + sub-signals (current timescale) ──────────────────────
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS composite_v3 DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS sub_signal_elm DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS sub_signal_cascade DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS sub_signal_taker DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS sub_signal_oi DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS sub_signal_funding DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS sub_signal_vpin DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS sub_signal_momentum DOUBLE PRECISION;

-- ─── v3 multi-horizon data (all 9 timescales) ────────────────────────────
-- 5m horizon
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_5m_composite DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_5m_elm DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_5m_cascade DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_5m_taker DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_5m_oi DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_5m_funding DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_5m_vpin DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_5m_momentum DOUBLE PRECISION;

-- 15m horizon
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_15m_composite DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_15m_elm DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_15m_cascade DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_15m_taker DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_15m_oi DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_15m_funding DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_15m_vpin DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_15m_momentum DOUBLE PRECISION;

-- 1h horizon
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_1h_composite DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_1h_elm DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_1h_cascade DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_1h_taker DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_1h_oi DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_1h_funding DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_1h_vpin DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_1h_momentum DOUBLE PRECISION;

-- 4h horizon
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_4h_composite DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_4h_elm DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_4h_cascade DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_4h_taker DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_4h_oi DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_4h_funding DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_4h_vpin DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_4h_momentum DOUBLE PRECISION;

-- 24h horizon
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_24h_composite DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_24h_elm DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_24h_cascade DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_24h_taker DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_24h_oi DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_24h_funding DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_24h_vpin DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_24h_momentum DOUBLE PRECISION;

-- 48h horizon
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_48h_composite DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_48h_elm DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_48h_cascade DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_48h_taker DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_48h_oi DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_48h_funding DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_48h_vpin DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_48h_momentum DOUBLE PRECISION;

-- 72h horizon
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_72h_composite DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_72h_elm DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_72h_cascade DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_72h_taker DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_72h_oi DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_72h_funding DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_72h_vpin DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_72h_momentum DOUBLE PRECISION;

-- 1w horizon
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_1w_composite DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_1w_elm DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_1w_cascade DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_1w_taker DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_1w_oi DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_1w_funding DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_1w_vpin DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_1w_momentum DOUBLE PRECISION;

-- 2w horizon
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_2w_composite DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_2w_elm DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_2w_cascade DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_2w_taker DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_2w_oi DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_2w_funding DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_2w_vpin DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS v3_2w_momentum DOUBLE PRECISION;

-- ─── v3 regime classification ────────────────────────────────────────────
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS regime_confidence DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS regime_persistence DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS regime_transition JSONB;

-- ─── v3 cascade state ────────────────────────────────────────────────────
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS cascade_strength DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS cascade_signal DOUBLE PRECISION;

-- ─── v3 alignment ────────────────────────────────────────────────────────
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS alignment_direction_agreement DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS alignment_num_aligned INTEGER;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS alignment_details JSONB;

-- ─── v4 derived metrics ──────────────────────────────────────────────────
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS expected_move_bps DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS vol_forecast_bps DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS downside_var_bps_p10 DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS upside_var_bps_p90 DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS time_to_target_s INTEGER;

-- ─── v4 feature freshness ────────────────────────────────────────────────
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS feature_freshness_ms JSONB;

-- ─── v4 macro (per-timescale) ────────────────────────────────────────────
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS macro_bias VARCHAR(20);
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS macro_confidence INTEGER;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS macro_direction_gate VARCHAR(30);
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS macro_size_modifier DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS macro_threshold_modifier DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS macro_reasoning TEXT;

-- ─── v4 consensus ────────────────────────────────────────────────────────
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS consensus_safe_to_trade BOOLEAN;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS consensus_max_divergence_bps DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS consensus_mean_divergence_bps DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS consensus_agreement_score DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS consensus_sources JSONB;

-- ─── v4 events ───────────────────────────────────────────────────────────
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS events_max_impact VARCHAR(20);
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS events_minutes_to_next_high_impact DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS events_upcoming JSONB;

-- ─── v4 polymarket CLOB ──────────────────────────────────────────────────
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS clob_implied_up DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS clob_implied_down DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS clob_vig DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS clob_imbalance DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS clob_up_spread DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS clob_down_spread DOUBLE PRECISION;

-- ─── v4 orderflow ────────────────────────────────────────────────────────
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS orderflow_liq_pressure VARCHAR(30);
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS orderflow_forced_long_liq_usd DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS orderflow_forced_short_liq_usd DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS orderflow_taker_buy_volume DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS orderflow_taker_sell_volume DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS orderflow_taker_flow_imbalance DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS orderflow_funding_rate DOUBLE PRECISION;

-- ─── v4 strategy recommendations ─────────────────────────────────────────
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS strategy_conviction VARCHAR(20);
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS strategy_conviction_score DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS strategy_action VARCHAR(20);  -- 'TRADE' | 'SKIP' | 'LONG' | 'SHORT' | 'UP' | 'DOWN'
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS strategy_collateral_pct DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS strategy_sl_pct DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS strategy_tp_pct DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS strategy_max_hold_s INTEGER;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS strategy_reason TEXT;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS strategy_recommendation_json JSONB;

-- ─── v4 vol state ────────────────────────────────────────────────────────
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS vol_state_range_pct DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS vol_state_std_pct DOUBLE PRECISION;
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS vol_state_regime_band VARCHAR(20);

-- ─── eval_offset (missing from v8 migration) ─────────────────────────────
ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS eval_offset INTEGER;

-- Create index for eval_offset queries
CREATE INDEX IF NOT EXISTS idx_window_snapshots_eval_offset ON window_snapshots (eval_offset) WHERE eval_offset IS NOT NULL;

-- ─── Verify columns exist ────────────────────────────────────────────────
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'window_snapshots'
  AND column_name IN (
    -- v2/v3 core
    'probability_up','probability_raw','model_version','quantiles_p10','quantiles_p25',
    'quantiles_p50','quantiles_p75','quantiles_p90','delta_bucket',
    -- v3 composite
    'composite_v3','sub_signal_elm','sub_signal_cascade','sub_signal_taker',
    'sub_signal_oi','sub_signal_funding','sub_signal_vpin','sub_signal_momentum',
    -- v3 regime
    'regime_confidence','regime_persistence','regime_transition',
    -- v3 cascade
    'cascade_strength','cascade_signal',
    -- v3 alignment
    'alignment_direction_agreement','alignment_num_aligned','alignment_details',
    -- v4 derived
    'expected_move_bps','vol_forecast_bps','downside_var_bps_p10','upside_var_bps_p90',
    'time_to_target_s',
    -- v4 feature freshness
    'feature_freshness_ms',
    -- v4 macro
    'macro_bias','macro_confidence','macro_direction_gate','macro_size_modifier',
    'macro_threshold_modifier','macro_reasoning',
    -- v4 consensus
    'consensus_safe_to_trade','consensus_max_divergence_bps','consensus_mean_divergence_bps',
    'consensus_agreement_score','consensus_sources',
    -- v4 events
    'events_max_impact','events_minutes_to_next_high_impact','events_upcoming',
    -- v4 clob
    'clob_implied_up','clob_implied_down','clob_vig','clob_imbalance',
    'clob_up_spread','clob_down_spread',
    -- v4 orderflow
    'orderflow_liq_pressure','orderflow_forced_long_liq_usd','orderflow_forced_short_liq_usd',
    'orderflow_taker_buy_volume','orderflow_taker_sell_volume','orderflow_taker_flow_imbalance',
    'orderflow_funding_rate',
    -- v4 strategy
    'strategy_conviction','strategy_conviction_score','strategy_action',
    'strategy_collateral_pct','strategy_sl_pct','strategy_tp_pct','strategy_max_hold_s',
    'strategy_reason','strategy_recommendation_json',
    -- v4 vol state
    'vol_state_range_pct','vol_state_std_pct','vol_state_regime_band',
    -- eval_offset
    'eval_offset'
  )
ORDER BY column_name;

-- Show sample data
SELECT 
    window_ts,
    asset,
    timeframe,
    eval_offset,
    probability_up,
    composite_v3,
    regime,
    macro_bias,
    consensus_safe_to_trade,
    strategy_action,
    strategy_conviction
FROM window_snapshots
WHERE eval_offset IS NOT NULL
ORDER BY window_ts DESC
LIMIT 5;
