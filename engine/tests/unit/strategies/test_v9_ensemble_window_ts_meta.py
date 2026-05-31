"""Regression guard: evaluate_v9_ensemble TRADE path must include window_ts.

Context (2026-05-31):
  PR #634 fixed the same bug in _tickformer_base.py (Layer 1a).
  Audit post-#634 found 13 strategies downstream of evaluate_v9_ensemble
  had the identical omission: the TRADE-path metadata dict did not include
  'window_ts', so has_fill_for_strategy_window_direction (Clause A,
  COALESCE(metadata->>'window_ts', '')) returned '' and the hard lock
  silently allowed duplicate orders through.

This test MUST fail if the window_ts line is removed from v9_ensemble.py.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_ensemble import (
    evaluate_v9_ensemble,
    reset_cooldown,
    reset_all_confirmations_v9,
)
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


def _make_trade_surface(**overrides) -> FullDataSurface:
    """Minimal valid surface that passes all v9_ensemble gates → TRADE.

    Uses the same defaults as test_v9_ensemble._make_surface but pared to
    the fields that gate logic actually reads.  DOWN direction surface.
    """
    defaults = dict(
        asset="BTC",
        timescale="5m",
        window_ts=1780091700,  # arbitrary fixed epoch
        eval_offset=120,
        assembled_at=time.time(),
        current_price=84500.0,
        open_price=85000.0,
        delta_binance=-0.005,
        delta_tiingo=-0.004,
        delta_chainlink=-0.005,
        delta_pct=-0.005,
        delta_source="chainlink",
        vpin=0.55,
        regime="NORMAL",
        twap_delta=-0.003,
        v2_probability_up=0.30,
        v2_probability_raw=0.30,
        v2_quantiles_p10=None,
        v2_quantiles_p50=None,
        v2_quantiles_p90=None,
        probability_lgb=0.30,
        probability_classifier=0.30,
        ensemble_config={"mode": "blend"},
        v3_5m_composite=None,
        v3_15m_composite=None,
        v3_1h_composite=None,
        v3_4h_composite=None,
        v3_24h_composite=None,
        v3_48h_composite=None,
        v3_72h_composite=None,
        v3_1w_composite=None,
        v3_2w_composite=None,
        v3_sub_elm=None,
        v3_sub_cascade=None,
        v3_sub_taker=None,
        v3_sub_oi=None,
        v3_sub_funding=None,
        v3_sub_vpin=None,
        v3_sub_momentum=None,
        v4_regime="volatile_trend",
        v4_regime_confidence=0.85,
        v4_regime_persistence=0.9,
        v4_macro_bias="BEAR",
        v4_macro_direction_gate="ALLOW_ALL",
        v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True,
        v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH",
        v4_conviction_score=0.85,
        poly_direction="DOWN",
        poly_trade_advised=True,
        poly_confidence=0.30,
        poly_confidence_distance=0.20,
        poly_timing="optimal",
        poly_max_entry_price=0.60,
        poly_reason="strong_signal",
        v4_recommended_side="DOWN",
        v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None,
        v4_quantiles=None,
        clob_up_bid=0.38,
        clob_up_ask=0.40,
        clob_down_bid=0.53,
        clob_down_ask=0.55,
        clob_implied_up=0.40,
        gamma_up_price=0.40,
        gamma_down_price=0.60,
        cg_oi_usd=50_000_000.0,
        cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0,
        cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0,
        cg_liq_long=300_000.0,
        cg_liq_short=200_000.0,
        cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0,
        timesfm_vol_forecast_bps=80.0,
        hour_utc=12,
        seconds_to_close=120,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


_GATE_PARAMS = {
    # v8 shared
    "min_offset_sec": 30,
    "max_offset_sec": 200,
    "tradeable_v4_regimes": ["volatile_trend", "chop", "risk_off", "calm_trend"],
    "block_down_vpin_regimes": ["TRANSITION"],
    "block_up_vpin_regimes": ["TRANSITION"],
    "lgb_dist_min_down": 0.10,
    "lgb_dist_min_up": 0.15,
    "lgb_dist_min_up_with_hc_agree": 0.05,
    "lgb_dist_min_down_with_hc_agree": 0.05,
    "fill_band_min": 0.00,
    "fill_band_max": 0.82,
    "up_min_fill_price": 0.55,
    "down_min_fill_price": 0.15,
    "blocked_utc_hours": [],
    "source_agreement_require_chainlink": True,
    "source_agreement_require_tiingo": True,
    "skip_on_oracle_disagree": True,
    "vpin_min": 0.40,
    "vpin_max": 1.0,
    "post_loss_cooldown_min": 20,
    # v9 specific
    "ensemble_disagreement_threshold": 0.25,
    "require_direction_agreement": True,
    "pc_weight_t_60": 0.55,
    "pc_weight_t_120": 0.50,
    "pc_weight_t_180": 0.45,
    "pc_weight_t_200": 0.35,
    "vhc_threshold": 0.25,
    "vhc_bypass_transition": True,
    "vhc_bypass_up_dist": True,
    "vhc_bypass_disagreement": True,
    "vhc_bypass_lgb_safety_floor": True,
    "vhc_bypass_oracle_direction": True,
    "vhc_kelly_multiplier": 2.0,
    "conviction_high_dist": 0.20,
    "conviction_medium_dist": 0.12,
    "conviction_low_dist": 0.05,
    "fallback_to_lgb_on_pc_null": True,
    "transition_strong_bypass_enabled": True,
    "transition_bypass_min_avg_pct_delta": 0.05,
    "transition_bypass_min_lgb_dist": 0.20,
    "delta_gate_enabled": False,
    "min_consecutive_pass_ticks": 0,
    "pl_vhc_bypass_enabled": True,
    "pl_vhc_threshold": 0.25,
    "pl_vhc_require_pc_agreement": True,
}


def test_v9_ensemble_trade_decision_includes_window_ts():
    """TRADE-path metadata must contain 'window_ts'.

    Regression guard: before this fix, evaluate_v9_ensemble's TRADE return
    built a 25-key metadata dict but omitted 'window_ts'.  The pre-trade
    hard lock (has_fill_for_strategy_window_direction) queries
    metadata->>'window_ts' via COALESCE which collapses NULL to '' — never
    matching the real epoch — so the lock returned False and allowed
    duplicate orders across concurrent evaluate_all calls.

    This reproduces for all 13 downstream strategies
    (v10_lgb_only, v9_lgb_only, v12_lgb_combo, ...) because they all
    delegate to evaluate_v9_ensemble under the hood.
    """
    surface = _make_trade_surface(window_ts=1780091700)
    token = _gp.set_active(_GATE_PARAMS)
    reset_cooldown()
    reset_all_confirmations_v9()
    try:
        decision = evaluate_v9_ensemble(surface)
    finally:
        _gp.reset_active(token)
        reset_cooldown()
        reset_all_confirmations_v9()

    assert decision.action == "TRADE", (
        f"Expected TRADE but got {decision.action} "
        f"(skip_reason={decision.skip_reason!r}).  "
        "Check that the surface passes all gates."
    )
    assert "window_ts" in (decision.metadata or {}), (
        "metadata['window_ts'] is missing from the v9_ensemble TRADE decision. "
        "This is the same root cause as the 2026-05-29 double-fire incident "
        "fixed in PR #634 for _tickformer_base.py. "
        "Add  \"window_ts\": getattr(surface, \"window_ts\", None)  "
        "to the TRADE-path metadata dict in v9_ensemble.py."
    )
    assert decision.metadata["window_ts"] == surface.window_ts, (
        f"metadata['window_ts']={decision.metadata['window_ts']!r} "
        f"!= surface.window_ts={surface.window_ts!r}"
    )
