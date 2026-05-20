"""Unit tests for v9_2_eth_raw_lgb GHOST strategy.

Coverage:
- model-not-loaded SKIP (v9_2_eth_model_not_loaded) — forward-compat path
- defensive asset guard (SKIP on non-ETH surface — wrong_asset)
- eval_offset outside band [60, 150] (outside_eval_band)
- conviction_below_threshold SKIP
- TRADE path: UP at 0.85, DOWN at 0.20 per hub note #550 recommendation
- gate_params runtime override respected
- Metadata shape on TRADE includes probability_lgb_v9_2_eth + thresholds
- Strategy is asset-restricted: same threshold passes for BTC surface SKIPs

Test strategy: synthetic _make_surface() fixture; uses _gp.set_active /
_gp.reset_active (same pattern as test_v9_2_iso_strategies.py).
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_2_eth_raw_lgb import evaluate_v9_2_eth_raw_lgb
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# ── Surface factory ────────────────────────────────────────────────────────

def _make_surface(**overrides) -> FullDataSurface:
    """Default surface for ETH strategy tests.

    asset=ETH, eval_offset=120 (in band [60,150]), hour_utc=12,
    probability_lgb_v9_2_eth=0.90 (above the 0.85 UP threshold).
    """
    defaults = dict(
        asset="ETH", timescale="5m",
        window_ts=1713009600,
        eval_offset=120, assembled_at=time.time(),
        current_price=3400.0, open_price=3380.0,
        delta_binance=0.005, delta_tiingo=0.004, delta_chainlink=0.005,
        delta_pct=0.005, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=0.003,
        v2_probability_up=0.80, v2_probability_raw=0.80,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=0.80, probability_classifier=None,
        ensemble_config={"mode": "blend"},
        v3_5m_composite=None, v3_15m_composite=None, v3_1h_composite=None,
        v3_4h_composite=None, v3_24h_composite=None, v3_48h_composite=None,
        v3_72h_composite=None, v3_1w_composite=None, v3_2w_composite=None,
        v3_sub_elm=None, v3_sub_cascade=None, v3_sub_taker=None,
        v3_sub_oi=None, v3_sub_funding=None, v3_sub_vpin=None,
        v3_sub_momentum=None,
        v4_regime="volatile_trend", v4_regime_confidence=0.85,
        v4_regime_persistence=0.9,
        v4_macro_bias="BULL", v4_macro_direction_gate="ALLOW_ALL",
        v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True, v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH", v4_conviction_score=0.85,
        poly_direction="UP", poly_trade_advised=True, poly_confidence=0.70,
        poly_confidence_distance=0.20, poly_timing="optimal",
        poly_max_entry_price=0.65, poly_reason="strong_signal",
        v4_recommended_side="UP", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.63, clob_up_ask=0.65, clob_down_bid=0.28,
        clob_down_ask=0.30, clob_implied_up=0.65,
        gamma_up_price=0.65, gamma_down_price=0.35,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0, cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=12, seconds_to_close=120,
        probability_lgb_v9_2_eth=0.90,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ── Gate-params helpers ────────────────────────────────────────────────────

_BASE_PARAMS: dict[str, Any] = {
    "up_threshold": 0.85,
    "down_threshold": 0.20,
    "eval_offset_min": 60,
    "eval_offset_max": 150,
    "min_consecutive_pass_ticks": 1,
    "expected_asset": "ETH",
    "entry_cap": 0.85,
    "collateral_pct": 0.025,
    "gtc_cap": 0.90,
}


@contextmanager
def _params(**extra):
    """Context manager that activates gate_params for the duration of a test."""
    p = dict(_BASE_PARAMS)
    p.update(extra)
    token = _gp.set_active(p)
    try:
        yield
    finally:
        _gp.reset_active(token)


@pytest.fixture(autouse=True)
def _clear_consec_state():
    """Reset consecutive-tick state and gate_params before/after each test."""
    from strategies.configs import v9_2_eth_raw_lgb
    v9_2_eth_raw_lgb._consec_state.clear()
    yield
    v9_2_eth_raw_lgb._consec_state.clear()


# ── Forward-compat: timesfm side not emitting yet ──────────────────────────

class TestForwardCompat:
    def test_model_not_loaded_skips_cleanly(self):
        """When the snapshot has no probability_lgb_v9_2_eth, the strategy
        SKIPs with a clear reason — no crash."""
        surface = _make_surface(probability_lgb_v9_2_eth=None)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_2_eth_model_not_loaded"
        # Decision still includes the strategy id even on SKIP.
        assert d.strategy_id == "v9_2_eth_raw_lgb"


# ── Defensive asset guard ──────────────────────────────────────────────────

class TestAssetGuard:
    def test_btc_surface_skips_even_with_high_prob(self):
        """ETH strategy refuses to fire on a BTC surface even at high
        conviction — defensive guard since the model is ETH-only."""
        surface = _make_surface(asset="BTC", probability_lgb_v9_2_eth=0.95)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_xrp_surface_skips(self):
        surface = _make_surface(asset="XRP", probability_lgb_v9_2_eth=0.95)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"


# ── Eval-offset band ───────────────────────────────────────────────────────

class TestEvalOffsetBand:
    def test_below_min_skips(self):
        surface = _make_surface(eval_offset=30)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_above_max_skips(self):
        """eval_offset=180 is OUT of the ETH band (tighter than BTC's 60-210)."""
        surface = _make_surface(eval_offset=180)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_at_min_60_fires(self):
        surface = _make_surface(eval_offset=60, probability_lgb_v9_2_eth=0.90)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_at_max_150_fires(self):
        surface = _make_surface(eval_offset=150, probability_lgb_v9_2_eth=0.90)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_none_eval_offset_skips(self):
        surface = _make_surface(eval_offset=None)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


# ── Threshold gating ───────────────────────────────────────────────────────

class TestThresholds:
    def test_up_fires_at_0_85(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.85)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.strategy_id == "v9_2_eth_raw_lgb"
        assert d.entry_reason == "v9_2_eth_raw_lgb_pass"

    def test_up_fires_above_0_85(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.95)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_down_fires_at_0_20(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.20)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_down_fires_below_0_20(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.10)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_between_thresholds_skips(self):
        # 0.50 is neither >= 0.85 nor <= 0.20
        surface = _make_surface(probability_lgb_v9_2_eth=0.50)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_just_below_up_skips(self):
        # 0.84 < 0.85 — below the UP threshold
        surface = _make_surface(probability_lgb_v9_2_eth=0.84)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_just_above_down_skips(self):
        # 0.21 > 0.20 — above the DOWN threshold
        surface = _make_surface(probability_lgb_v9_2_eth=0.21)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── Runtime override ───────────────────────────────────────────────────────

class TestRuntimeOverride:
    def test_runtime_override_lowers_up_threshold(self):
        """gate_params runtime override changes thresholds."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.75)
        # with default 0.85 threshold: SKIP
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "SKIP"
        # with overridden 0.70 threshold: TRADE
        with _params(up_threshold=0.70):
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_runtime_override_expands_eval_band(self):
        # surface at eval_offset=180 normally skips (above max 150)
        surface = _make_surface(eval_offset=180, probability_lgb_v9_2_eth=0.90)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "SKIP"
        with _params(eval_offset_max=210):
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "TRADE"


# ── Metadata shape ─────────────────────────────────────────────────────────

class TestMetadata:
    def test_metadata_contains_probability_on_trade(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.90)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "TRADE"
        assert "probability_lgb_v9_2_eth" in d.metadata
        assert d.metadata["probability_lgb_v9_2_eth"] == pytest.approx(0.90)
        assert d.metadata["up_threshold"] == pytest.approx(0.85)
        assert d.metadata["down_threshold"] == pytest.approx(0.20)
        assert d.metadata["eval_offset_min"] == 60
        assert d.metadata["eval_offset_max"] == 150
        assert d.metadata["asset"] == "ETH"

    def test_metadata_contains_sizing_on_trade(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.90)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "TRADE"
        assert d.metadata["entry_cap"] == pytest.approx(0.85)
        assert d.metadata["collateral_pct"] == pytest.approx(0.025)
        assert d.metadata["gtc_cap"] == pytest.approx(0.90)

    def test_confidence_score_is_high_at_0_90(self):
        # |0.90 - 0.5| * 2 = 0.80 >= 0.40 -> HIGH
        surface = _make_surface(probability_lgb_v9_2_eth=0.90)
        with _params():
            d = evaluate_v9_2_eth_raw_lgb(surface)
        assert d.action == "TRADE"
        assert d.confidence == "HIGH"
        assert d.confidence_score == pytest.approx(0.80)
