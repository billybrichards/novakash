"""Unit tests for v9_5_eth_blend_down_high GHOST strategy (DOWN-HIGH floor tier).

Band gating: (-1.0, 0.12] — effectively p <= 0.12 since calibrated
probability never goes negative. Strongest-conviction DOWN region of the
ladder.

Pattern mirrors test_v9_5_eth_blend_down_low.py.
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_5_eth_blend_down_high import (
    evaluate_v9_5_eth_blend_down_high,
)
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


def _make_surface(**overrides) -> FullDataSurface:
    """Default ETH surface — probability 0.08 (inside floor band)."""
    defaults = dict(
        asset="ETH", timescale="5m",
        window_ts=1779000000,
        eval_offset=120, assembled_at=time.time(),
        current_price=2500.0, open_price=2495.0,
        delta_binance=0.005, delta_tiingo=0.004, delta_chainlink=0.005,
        delta_pct=0.005, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=0.003,
        v2_probability_up=0.08, v2_probability_raw=0.08,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=0.08, probability_classifier=None,
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
        poly_direction="DOWN", poly_trade_advised=True, poly_confidence=0.70,
        poly_confidence_distance=0.20, poly_timing="optimal",
        poly_max_entry_price=0.65, poly_reason="strong_signal",
        v4_recommended_side="DOWN", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.30, clob_up_ask=0.32, clob_down_bid=0.66,
        clob_down_ask=0.68, clob_implied_up=0.31,
        gamma_up_price=0.31, gamma_down_price=0.69,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0, cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=12, seconds_to_close=120,
        probability_lgb_v9_5_eth=0.08,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


_BASE_PARAMS: dict[str, Any] = {
    "down_threshold": 0.12,
    "down_threshold_min": -1.0,
    "eval_offset_min": 60,
    "eval_offset_max": 240,
    "min_consecutive_pass_ticks": 1,
    "expected_asset": "ETH",
    "entry_cap": 0.93,
    "collateral_pct": 0.025,
    "gtc_cap": 0.96,
}


@contextmanager
def _params(**extra):
    p = dict(_BASE_PARAMS)
    p.update(extra)
    token = _gp.set_active(p)
    try:
        yield
    finally:
        _gp.reset_active(token)


@pytest.fixture(autouse=True)
def _clear_consec_state():
    from strategies.configs import v9_5_eth_blend_down_high
    v9_5_eth_blend_down_high._consec_state.clear()
    yield
    v9_5_eth_blend_down_high._consec_state.clear()


class TestNullProbabilityHandling:
    def test_model_not_loaded_skips_cleanly(self):
        surface = _make_surface(probability_lgb_v9_5_eth=None)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_5_eth_model_not_loaded"
        assert d.strategy_id == "v9_5_eth_blend_down_high"


class TestAssetGuard:
    def test_btc_surface_skips(self):
        surface = _make_surface(asset="BTC", probability_lgb_v9_5_eth=0.08)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_xrp_surface_skips(self):
        surface = _make_surface(asset="XRP", probability_lgb_v9_5_eth=0.08)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_asset_check_runs_before_probability_check(self):
        surface = _make_surface(asset="BTC", probability_lgb_v9_5_eth=None)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"


class TestEvalOffsetBand:
    def test_below_min_skips(self):
        surface = _make_surface(eval_offset=59)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_above_max_skips(self):
        surface = _make_surface(eval_offset=241)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_at_min_60_fires(self):
        surface = _make_surface(eval_offset=60, probability_lgb_v9_5_eth=0.08)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_at_max_240_fires(self):
        surface = _make_surface(eval_offset=240, probability_lgb_v9_5_eth=0.08)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_none_eval_offset_skips(self):
        surface = _make_surface(eval_offset=None)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


class TestDownBand:
    """Floor band: (-1.0, 0.12]. Effectively p <= 0.12."""

    def test_at_upper_bound_0_12_fires(self):
        """Upper bound INCLUSIVE — exactly 0.12 fires DOWN."""
        surface = _make_surface(probability_lgb_v9_5_eth=0.12)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"
        assert d.entry_reason == "v9_5_eth_blend_down_high_pass"

    def test_at_p_0_05_fires(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.05)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_at_p_0_0_fires(self):
        """Edge — calibrated p=0.0 (rare but technically valid) fires."""
        surface = _make_surface(probability_lgb_v9_5_eth=0.0)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_just_above_upper_bound_skips(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.1201)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_conviction_band"

    def test_in_down_mid_band_skips(self):
        """0.15 is in down_mid territory."""
        surface = _make_surface(probability_lgb_v9_5_eth=0.15)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_conviction_band"

    def test_neutral_skips(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.50)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_conviction_band"


class TestNoUpDirection:
    def test_strong_up_skips(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.95)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_conviction_band"

    def test_runtime_override_cannot_force_up(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.95)
        with _params(up_threshold=0.80, up_threshold_max=2.0):
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_conviction_band"


class TestRuntimeOverride:
    def test_runtime_override_lowers_upper_bound(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.10)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "TRADE"
        with _params(down_threshold=0.08):
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "SKIP"

    def test_runtime_override_can_set_floor(self):
        """Override can clamp the floor, even though defaults are unbounded."""
        surface = _make_surface(probability_lgb_v9_5_eth=0.02)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "TRADE"
        with _params(down_threshold_min=0.05):
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "SKIP"

    def test_runtime_override_narrows_eval_band(self):
        surface = _make_surface(eval_offset=100, probability_lgb_v9_5_eth=0.08)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "TRADE"
        with _params(eval_offset_min=120):
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


class TestMetadataShape:
    def test_metadata_contains_probability_on_trade(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.08)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "TRADE"
        assert d.metadata["probability_lgb_v9_5_eth"] == pytest.approx(0.08)
        assert d.metadata["down_threshold"] == pytest.approx(0.12)
        assert d.metadata["down_threshold_min"] == pytest.approx(-1.0)
        assert d.metadata["eval_offset_min"] == 60
        assert d.metadata["eval_offset_max"] == 240
        assert d.metadata["asset"] == "ETH"
        assert d.metadata["strategy_id"] == "v9_5_eth_blend_down_high"
        assert d.metadata["strategy_version"] == "1.0.0"

    def test_metadata_contains_sizing_on_trade(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.08)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "TRADE"
        assert d.metadata["entry_cap"] == pytest.approx(0.93)
        assert d.metadata["collateral_pct"] == pytest.approx(0.025)
        assert d.metadata["gtc_cap"] == pytest.approx(0.96)

    def test_confidence_score_uses_distance_from_neutral(self):
        # p=0.08 -> |0.08 - 0.5| * 2 = 0.84
        surface = _make_surface(probability_lgb_v9_5_eth=0.08)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "TRADE"
        assert d.confidence == "HIGH"
        assert d.confidence_score == pytest.approx(0.84)


class TestConsecutiveTicks:
    def test_default_one_tick_fires_immediately(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.08)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"
        assert d.metadata["consec_tick_count"] == 1

    def test_runtime_override_requires_two_ticks(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.08)
        with _params(min_consecutive_pass_ticks=2):
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "SKIP"
        assert "awaiting_consec_ticks" in (d.skip_reason or "")
        with _params(min_consecutive_pass_ticks=2):
            d2 = evaluate_v9_5_eth_blend_down_high(surface)
        assert d2.action == "TRADE"
        assert d2.direction == "DOWN"


class TestCrossStrategyIndependence:
    def test_consec_state_isolated_from_down_low(self):
        from strategies.configs import v9_5_eth_blend_down_high as down_high
        from strategies.configs import v9_5_eth_blend_down_low as down_low

        down_high._consec_state.clear()
        down_low._consec_state.clear()
        assert down_high._consec_state is not down_low._consec_state

        surface = _make_surface(probability_lgb_v9_5_eth=0.08)
        with _params():
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "TRADE"
        assert len(down_high._consec_state) >= 1
        assert len(down_low._consec_state) == 0

    def test_consec_state_isolated_from_down_mid(self):
        from strategies.configs import v9_5_eth_blend_down_high as down_high
        from strategies.configs import v9_5_eth_blend_down_mid as down_mid

        down_high._consec_state.clear()
        down_mid._consec_state.clear()
        assert down_high._consec_state is not down_mid._consec_state

    def test_consec_state_isolated_from_up_high(self):
        from strategies.configs import v9_5_eth_blend_down_high as down_high
        from strategies.configs import v9_5_eth_blend_up_high as up_high

        down_high._consec_state.clear()
        up_high._consec_state.clear()
        assert down_high._consec_state is not up_high._consec_state

    def test_consec_state_isolated_from_v9_5_eth_raw_lgb(self):
        from strategies.configs import v9_5_eth_blend_down_high as down_high
        from strategies.configs import v9_5_eth_raw_lgb as raw_lgb

        down_high._consec_state.clear()
        raw_lgb._consec_state.clear()
        assert down_high._consec_state is not raw_lgb._consec_state
