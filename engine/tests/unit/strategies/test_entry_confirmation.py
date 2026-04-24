"""Tests for the 3-tick entry confirmation gate (Feature 2).

Validates that the entry confirmation state machine correctly:
  - Accumulates consecutive passing evals
  - Resets on any SKIP
  - Resets on direction change
  - Fires TRADE only after min_consecutive_pass_ticks reached
  - Is disabled when min_consecutive_pass_ticks=0

Both v8_champion_lgb_only and v9_ensemble get independent implementations.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v8_champion_lgb_only import (
    evaluate_v8_champion_lgb_only,
    check_confirmation,
    get_confirmation_count,
    reset_confirmation,
    reset_all_confirmations,
    reset_cooldown,
)
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


def _make_surface(**overrides) -> FullDataSurface:
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713010800,
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=85000.0,
        delta_binance=-0.005, delta_tiingo=-0.004, delta_chainlink=-0.005,
        delta_pct=-0.005, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=-0.003,
        v2_probability_up=0.30, v2_probability_raw=0.30,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=0.30, probability_classifier=None,
        ensemble_config={"mode": "blend"},
        v3_5m_composite=None, v3_15m_composite=None, v3_1h_composite=None,
        v3_4h_composite=None, v3_24h_composite=None, v3_48h_composite=None,
        v3_72h_composite=None, v3_1w_composite=None, v3_2w_composite=None,
        v3_sub_elm=None, v3_sub_cascade=None, v3_sub_taker=None,
        v3_sub_oi=None, v3_sub_funding=None, v3_sub_vpin=None,
        v3_sub_momentum=None,
        v4_regime="volatile_trend", v4_regime_confidence=0.85,
        v4_regime_persistence=0.9,
        v4_macro_bias="BEAR", v4_macro_direction_gate="ALLOW_ALL",
        v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True, v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH", v4_conviction_score=0.85,
        poly_direction="DOWN", poly_trade_advised=True, poly_confidence=0.30,
        poly_confidence_distance=0.20, poly_timing="optimal",
        poly_max_entry_price=0.60, poly_reason="strong_signal",
        v4_recommended_side="DOWN", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.38, clob_up_ask=0.40, clob_down_bid=0.53,
        clob_down_ask=0.55, clob_implied_up=0.40,
        gamma_up_price=0.40, gamma_down_price=0.60,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0, cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=12, seconds_to_close=120,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


@pytest.fixture(autouse=True)
def _bind_gate_params():
    params = {
        "min_offset_sec": 24,
        "max_offset_sec": 200,
        "tradeable_v4_regimes": ["volatile_trend", "chop", "risk_off", "calm_trend"],
        "block_down_vpin_regimes": ["TRANSITION"],
        "block_up_vpin_regimes": ["TRANSITION"],
        "lgb_dist_min_down": 0.10,
        "lgb_dist_min_up": 0.15,
        "fill_band_min": 0.00,
        "fill_band_max": 0.82,
        "up_min_fill_price": 0.55,
        "down_min_fill_price": 0.15,
        "blocked_utc_hours": [0, 1, 2, 3, 4, 5],
        "source_agreement_require_chainlink": True,
        "source_agreement_require_tiingo": True,
        "skip_on_oracle_disagree": True,
        "vpin_min": 0.40,
        "vpin_max": 1.0,
        "post_loss_cooldown_min": 20,
        "transition_strong_bypass_enabled": True,
        "transition_bypass_min_avg_pct_delta": 0.05,
        "transition_bypass_min_lgb_dist": 0.20,
        "delta_gate_enabled": True,
        "min_alignment_bps": 1.0,
        "min_consecutive_pass_ticks": 3,
    }
    token = _gp.set_active(params)
    reset_cooldown()
    reset_all_confirmations()
    try:
        yield
    finally:
        _gp.reset_active(token)
        reset_cooldown()
        reset_all_confirmations()


# ── Unit tests for the state machine ─────────────────────────────────────

def test_check_confirmation_accumulates():
    """Consecutive calls with same direction should accumulate."""
    assert not check_confirmation("test", 100, "DOWN")
    assert get_confirmation_count("test", 100) == 1
    assert not check_confirmation("test", 100, "DOWN")
    assert get_confirmation_count("test", 100) == 2
    assert check_confirmation("test", 100, "DOWN")
    assert get_confirmation_count("test", 100) == 3


def test_check_confirmation_resets_on_direction_change():
    """Direction change resets the counter."""
    check_confirmation("test", 100, "DOWN")
    check_confirmation("test", 100, "DOWN")
    assert get_confirmation_count("test", 100) == 2
    # Change direction
    check_confirmation("test", 100, "UP")
    assert get_confirmation_count("test", 100) == 1  # reset to 1


def test_reset_confirmation_clears():
    """Explicit reset clears the counter."""
    check_confirmation("test", 100, "DOWN")
    check_confirmation("test", 100, "DOWN")
    reset_confirmation("test", 100)
    assert get_confirmation_count("test", 100) == 0


def test_different_windows_independent():
    """Different window_ts keys are independent."""
    check_confirmation("test", 100, "DOWN")
    check_confirmation("test", 100, "DOWN")
    check_confirmation("test", 200, "DOWN")
    assert get_confirmation_count("test", 100) == 2
    assert get_confirmation_count("test", 200) == 1


# ── Integration tests with the evaluate hook ─────────────────────────────

def test_3tick_requires_3_passing_evals_for_trade():
    """With min_consecutive_pass_ticks=3, need 3 consecutive passing evals."""
    surface = _make_surface()

    # First eval: all gates pass but only 1/3 ticks
    result1 = evaluate_v8_champion_lgb_only(surface)
    assert result1.action == "SKIP"
    assert "entry_confirmation" in (result1.skip_reason or "")
    assert "1/3" in (result1.skip_reason or "")

    # Second eval: 2/3 ticks
    result2 = evaluate_v8_champion_lgb_only(surface)
    assert result2.action == "SKIP"
    assert "2/3" in (result2.skip_reason or "")

    # Third eval: 3/3 ticks → TRADE
    result3 = evaluate_v8_champion_lgb_only(surface)
    assert result3.action == "TRADE"


def test_3tick_resets_when_gate_fails():
    """If a gate fails between passing evals, counter resets."""
    surface = _make_surface()

    # First passing eval
    evaluate_v8_champion_lgb_only(surface)
    # Second passing eval
    evaluate_v8_champion_lgb_only(surface)

    # Fail a gate (e.g. delta gate by flipping chainlink positive)
    bad_surface = _make_surface(
        delta_chainlink=+0.001,  # against DOWN
        delta_tiingo=-0.004,
        delta_binance=-0.003,
    )
    result_skip = evaluate_v8_champion_lgb_only(bad_surface)
    assert result_skip.action == "SKIP"

    # Next passing eval should restart at 1/3
    result_after = evaluate_v8_champion_lgb_only(surface)
    assert result_after.action == "SKIP"
    assert "1/3" in (result_after.skip_reason or "")


def test_3tick_disabled_when_zero():
    """When min_consecutive_pass_ticks=0, trades on first pass."""
    token = _gp.set_active({
        **_gp._ACTIVE.get(),
        "min_consecutive_pass_ticks": 0,
    })
    try:
        surface = _make_surface()
        result = evaluate_v8_champion_lgb_only(surface)
        assert result.action == "TRADE"
    finally:
        _gp.reset_active(token)
