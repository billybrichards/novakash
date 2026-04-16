"""Tests for the 5 production strategy configs -- evaluated against known inputs."""

import sys
import os
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from strategies.registry import StrategyRegistry
from strategies.data_surface import DataSurfaceManager, FullDataSurface

# Path to the actual configs directory
CONFIGS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "strategies", "configs")


def _make_surface(**overrides) -> FullDataSurface:
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713000000,
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=84000.0,
        delta_binance=0.005, delta_tiingo=0.004, delta_chainlink=0.005,
        delta_pct=0.004, delta_source="tiingo_rest_candle",
        vpin=0.45, regime="NORMAL", twap_delta=0.003,
        v2_probability_up=0.38, v2_probability_raw=0.36,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        v3_5m_composite=None, v3_15m_composite=None, v3_1h_composite=None,
        v3_4h_composite=None, v3_24h_composite=None, v3_48h_composite=None,
        v3_72h_composite=None, v3_1w_composite=None, v3_2w_composite=None,
        v3_sub_elm=None, v3_sub_cascade=None, v3_sub_taker=None,
        v3_sub_oi=None, v3_sub_funding=None, v3_sub_vpin=None, v3_sub_momentum=None,
        v4_regime="calm_trend", v4_regime_confidence=0.85, v4_regime_persistence=0.9,
        v4_macro_bias="BULL", v4_macro_direction_gate="ALLOW_ALL", v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True, v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH", v4_conviction_score=0.85,
        poly_direction="DOWN", poly_trade_advised=True, poly_confidence=0.38,
        poly_confidence_distance=0.12, poly_timing="optimal",
        poly_max_entry_price=0.65, poly_reason="strong_signal",
        v4_recommended_side="DOWN", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.46, clob_up_ask=0.48, clob_down_bid=0.52,
        clob_down_ask=0.54, clob_implied_up=0.47,
        gamma_up_price=0.45, gamma_down_price=0.55,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0, cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=12, seconds_to_close=120,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


@pytest.fixture
def registry():
    mgr = DataSurfaceManager(v4_base_url="http://fake")
    reg = StrategyRegistry(CONFIGS_DIR, mgr)
    reg.load_all()
    return reg


class TestV4DownOnly:
    def test_loads(self, registry):
        assert "v4_down_only" in registry.strategy_names
        assert registry.configs["v4_down_only"].mode == "GHOST"

    def test_trade_down_in_window(self, registry):
        surface = _make_surface(
            eval_offset=120, poly_direction="DOWN",
            poly_confidence_distance=0.12, poly_trade_advised=True,
            clob_down_ask=0.60,
        )
        decision = registry._evaluate_one(
            "v4_down_only", registry.configs["v4_down_only"], surface
        )
        assert decision.action == "TRADE"
        assert decision.direction == "DOWN"

    def test_skip_up_direction(self, registry):
        surface = _make_surface(
            eval_offset=120, poly_direction="UP",
            poly_confidence_distance=0.12, poly_trade_advised=True,
        )
        decision = registry._evaluate_one(
            "v4_down_only", registry.configs["v4_down_only"], surface
        )
        assert decision.action == "SKIP"
        assert "direction" in decision.skip_reason

    def test_skip_outside_timing(self, registry):
        surface = _make_surface(eval_offset=60)
        decision = registry._evaluate_one(
            "v4_down_only", registry.configs["v4_down_only"], surface
        )
        assert decision.action == "SKIP"
        assert "timing" in decision.skip_reason

    def test_skip_low_confidence(self, registry):
        surface = _make_surface(
            eval_offset=120, poly_direction="DOWN",
            poly_confidence_distance=0.05, poly_trade_advised=True,
        )
        decision = registry._evaluate_one(
            "v4_down_only", registry.configs["v4_down_only"], surface
        )
        assert decision.action == "SKIP"
        assert "confidence" in decision.skip_reason


class TestV4UpBasic:
    def test_loads(self, registry):
        assert "v4_up_basic" in registry.strategy_names
        assert registry.configs["v4_up_basic"].mode == "GHOST"

    def test_trade_up_in_window(self, registry):
        surface = _make_surface(
            eval_offset=120, poly_direction="UP",
            poly_confidence_distance=0.16,  # min_dist raised to 0.15 on 2026-04-14
            v2_probability_up=0.62,
        )
        decision = registry._evaluate_one(
            "v4_up_basic", registry.configs["v4_up_basic"], surface
        )
        assert decision.action == "TRADE"
        assert decision.direction == "UP"

    def test_skip_down(self, registry):
        surface = _make_surface(
            eval_offset=120, poly_direction="DOWN",
            poly_confidence_distance=0.12,
        )
        decision = registry._evaluate_one(
            "v4_up_basic", registry.configs["v4_up_basic"], surface
        )
        assert decision.action == "SKIP"

    def test_wider_timing_than_down(self, registry):
        """v4_up_basic accepts T-65, which v4_down_only rejects."""
        surface = _make_surface(
            eval_offset=65, poly_direction="UP",
            poly_confidence_distance=0.16,  # min_dist raised to 0.15 on 2026-04-14
        )
        decision = registry._evaluate_one(
            "v4_up_basic", registry.configs["v4_up_basic"], surface
        )
        assert decision.action == "TRADE"


class TestV4UpAsian:
    def test_loads(self, registry):
        assert "v4_up_asian" in registry.strategy_names

    def test_trade_asian_hours(self, registry):
        surface = _make_surface(
            eval_offset=120, poly_direction="UP",
            poly_confidence_distance=0.15, hour_utc=0,
        )
        decision = registry._evaluate_one(
            "v4_up_asian", registry.configs["v4_up_asian"], surface
        )
        assert decision.action == "TRADE"

    def test_skip_non_asian_hours(self, registry):
        surface = _make_surface(
            eval_offset=120, poly_direction="UP",
            poly_confidence_distance=0.15, hour_utc=12,
        )
        decision = registry._evaluate_one(
            "v4_up_asian", registry.configs["v4_up_asian"], surface
        )
        assert decision.action == "SKIP"
        assert "session_hours" in decision.skip_reason

    def test_skip_high_confidence(self, registry):
        """max_dist=0.20 should reject dist=0.25."""
        surface = _make_surface(
            eval_offset=120, poly_direction="UP",
            poly_confidence_distance=0.25, hour_utc=0,
        )
        decision = registry._evaluate_one(
            "v4_up_asian", registry.configs["v4_up_asian"], surface
        )
        assert decision.action == "SKIP"
        assert "confidence" in decision.skip_reason


class TestV4Fusion:
    def test_loads(self, registry):
        assert "v4_fusion" in registry.strategy_names
        assert registry.configs["v4_fusion"].pre_gate_hook == "evaluate_polymarket_v2"

    def test_trade_poly_v2(self, registry):
        surface = _make_surface(
            poly_direction="DOWN", poly_trade_advised=True,
            poly_confidence=0.38, poly_confidence_distance=0.12,
            poly_timing="optimal",
            # Chainlink must agree with trade direction (it IS the resolution oracle)
            delta_chainlink=-0.005,
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "TRADE"
        assert decision.direction == "DOWN"

    def test_skip_early_timing(self, registry):
        surface = _make_surface(poly_timing="early")
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "SKIP"
        assert "timing" in decision.skip_reason

    def test_skip_low_confidence(self, registry):
        surface = _make_surface(
            poly_direction="DOWN", poly_confidence_distance=0.08,
            poly_timing="optimal", poly_trade_advised=True,
            poly_confidence=0.42,
        )
        decision = registry._evaluate_one(
            "v4_fusion", registry.configs["v4_fusion"], surface
        )
        assert decision.action == "SKIP"
        assert "0.12" in decision.skip_reason


class TestV10Gate:
    def test_loads(self, registry):
        assert "v10_gate" in registry.strategy_names
        assert registry.configs["v10_gate"].post_gate_hook == "classify_confidence"

    def test_trade_all_gates_pass(self, registry):
        surface = _make_surface(
            eval_offset=120,
            delta_tiingo=-0.001, delta_chainlink=-0.001, delta_binance=-0.001,
            delta_pct=-0.001,
            poly_direction="DOWN",
            cg_taker_buy_vol=800, cg_taker_sell_vol=1200,
            poly_confidence_distance=0.15,
            clob_down_bid=0.53, clob_down_ask=0.535,
            v2_probability_up=0.35,
            cg_liq_total=100_000.0,
        )
        decision = registry._evaluate_one(
            "v10_gate", registry.configs["v10_gate"], surface
        )
        assert decision.action == "TRADE"

    def test_skip_low_delta(self, registry):
        surface = _make_surface(
            eval_offset=120, delta_pct=0.0001,
            delta_tiingo=0.0001, delta_chainlink=0.0001,
        )
        decision = registry._evaluate_one(
            "v10_gate", registry.configs["v10_gate"], surface
        )
        assert decision.action == "SKIP"
        assert "delta_magnitude" in decision.skip_reason
