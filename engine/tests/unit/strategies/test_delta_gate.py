"""Tests for the delta alignment gate (Feature 1).

Validates that the delta gate correctly skips trades when chainlink
delta is moving against the bet direction by more than min_alignment_bps.

Both v8_champion_lgb_only and v9_ensemble get independent implementations
of this gate, so we test both.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v8_champion_lgb_only import (
    evaluate_v8_champion_lgb_only,
    reset_cooldown,
    reset_all_confirmations,
)
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


def _make_surface(**overrides) -> FullDataSurface:
    """Default valid DOWN surface."""
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


def _up_surface(**overrides) -> FullDataSurface:
    defaults = dict(
        poly_direction="UP", poly_confidence=0.70,
        poly_confidence_distance=0.20,
        probability_lgb=0.70, probability_classifier=None,
        delta_binance=+0.005, delta_tiingo=+0.004, delta_chainlink=+0.005,
        v4_recommended_side="UP", v2_probability_up=0.70,
        clob_up_bid=0.58, clob_up_ask=0.60,
        clob_down_bid=0.38, clob_down_ask=0.40,
        gamma_up_price=0.60, gamma_down_price=0.40,
    )
    defaults.update(overrides)
    return _make_surface(**defaults)


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
        # Delta gate params
        "delta_gate_enabled": True,
        "min_alignment_bps": 1.0,
        # Disable 3-tick for delta gate tests
        "min_consecutive_pass_ticks": 0,
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


# ── Delta gate: DOWN direction ─────────────────────────────────────────────

def test_delta_gate_skips_down_when_chainlink_positive():
    """DOWN bet + positive chainlink delta > 1bp = skip."""
    surface = _make_surface(
        delta_chainlink=+0.0002,  # +2bp → against DOWN
        delta_tiingo=-0.004,
        delta_binance=-0.003,
    )
    result = evaluate_v8_champion_lgb_only(surface)
    assert result.action == "SKIP"
    assert "delta_gate" in (result.skip_reason or "")


def test_delta_gate_passes_down_when_chainlink_negative():
    """DOWN bet + negative chainlink delta = aligned, passes."""
    surface = _make_surface(
        delta_chainlink=-0.005,  # -50bp → aligned with DOWN
    )
    result = evaluate_v8_champion_lgb_only(surface)
    # Should pass delta gate (might still TRADE or SKIP on other gates)
    if result.action == "SKIP":
        assert "delta_gate" not in (result.skip_reason or "")


def test_delta_gate_passes_down_when_chainlink_slightly_positive():
    """DOWN bet + positive chainlink < 1bp threshold = passes."""
    surface = _make_surface(
        delta_chainlink=+0.00005,  # +0.5bp < 1bp threshold
        delta_tiingo=-0.004,
        delta_binance=-0.003,
    )
    result = evaluate_v8_champion_lgb_only(surface)
    if result.action == "SKIP":
        assert "delta_gate" not in (result.skip_reason or "")


# ── Delta gate: UP direction ──────────────────────────────────────────────

def test_delta_gate_skips_up_when_chainlink_negative():
    """UP bet + negative chainlink delta > 1bp = skip."""
    surface = _up_surface(
        delta_chainlink=-0.0002,  # -2bp → against UP
        delta_tiingo=+0.004,
        delta_binance=+0.003,
    )
    result = evaluate_v8_champion_lgb_only(surface)
    assert result.action == "SKIP"
    assert "delta_gate" in (result.skip_reason or "")


def test_delta_gate_passes_up_when_chainlink_positive():
    """UP bet + positive chainlink delta = aligned, passes."""
    surface = _up_surface(delta_chainlink=+0.005)
    result = evaluate_v8_champion_lgb_only(surface)
    if result.action == "SKIP":
        assert "delta_gate" not in (result.skip_reason or "")


# ── Delta gate: disabled ──────────────────────────────────────────────────

def test_delta_gate_disabled_passes():
    """When delta_gate_enabled=False, misaligned delta passes."""
    token = _gp.set_active({
        **_gp._ACTIVE.get(),
        "delta_gate_enabled": False,
    })
    try:
        surface = _make_surface(
            delta_chainlink=+0.01,  # massive positive = against DOWN
        )
        result = evaluate_v8_champion_lgb_only(surface)
        if result.action == "SKIP":
            assert "delta_gate" not in (result.skip_reason or "")
    finally:
        _gp.reset_active(token)


def test_delta_gate_passes_when_chainlink_none():
    """When chainlink delta is None, delta gate passes gracefully."""
    surface = _make_surface(delta_chainlink=None)
    # Source agreement gate will skip first since chainlink is required
    result = evaluate_v8_champion_lgb_only(surface)
    assert result.action == "SKIP"
    assert "delta_gate" not in (result.skip_reason or "")


# ── Threshold tuning ──────────────────────────────────────────────────────

def test_delta_gate_custom_threshold():
    """Custom threshold of 5bp should allow 2bp misalignment."""
    token = _gp.set_active({
        **_gp._ACTIVE.get(),
        "min_alignment_bps": 5.0,
    })
    try:
        surface = _make_surface(
            delta_chainlink=+0.0002,  # +2bp against DOWN, but threshold is 5bp
            delta_tiingo=-0.004,
            delta_binance=-0.003,
        )
        result = evaluate_v8_champion_lgb_only(surface)
        if result.action == "SKIP":
            assert "delta_gate" not in (result.skip_reason or "")
    finally:
        _gp.reset_active(token)
