"""Unit tests for v9_5_xrp_blend GHOST strategy.

Coverage:
- model-not-loaded SKIP (v9_5_xrp_model_not_loaded) — forward-compat path
- defensive asset guard (SKIP on non-XRP surface — wrong_asset)
- eval_offset outside band [60, 240] (outside_eval_band)
- conviction_below_threshold SKIP
- TRADE path: UP at 0.82, DOWN at 0.20 per walk-forward CV operating point
- gate_params runtime override respected
- Metadata shape on TRADE includes probability_lgb_v9_5_xrp + thresholds
- Strategy is asset-restricted: high prob on BTC/ETH SKIPs

Test strategy: synthetic _make_surface() fixture; uses _gp.set_active /
_gp.reset_active (same pattern as test_v9_3_btc_raw_lgb.py).
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_5_xrp_blend import evaluate_v9_5_xrp_blend
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# ── Surface factory ────────────────────────────────────────────────────────

def _make_surface(**overrides) -> FullDataSurface:
    """Default surface for XRP strategy tests.

    asset=XRP, eval_offset=120 (in band [60, 240]), hour_utc=12,
    probability_lgb_v9_5_xrp=0.85 (above the 0.82 UP threshold).
    """
    defaults = dict(
        asset="XRP", timescale="5m",
        window_ts=1779000000,
        eval_offset=120, assembled_at=time.time(),
        current_price=0.55, open_price=0.548,
        delta_binance=0.005, delta_tiingo=0.004, delta_chainlink=0.005,
        delta_pct=0.005, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=0.003,
        v2_probability_up=0.85, v2_probability_raw=0.85,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=0.85, probability_classifier=None,
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
        hour_utc=12, seconds_to_close=180,
        probability_lgb_v9_5_xrp=0.85,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ── Gate-params helpers ────────────────────────────────────────────────────

_BASE_PARAMS: dict[str, Any] = {
    "up_threshold": 0.82,
    "down_threshold": 0.20,
    "eval_offset_min": 60,
    "eval_offset_max": 240,
    "min_consecutive_pass_ticks": 1,
    "expected_asset": "XRP",
    "entry_cap": 0.85,
    "collateral_pct": 0.025,
    "gtc_cap": 0.90,
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
    from strategies.configs import v9_5_xrp_blend
    v9_5_xrp_blend._consec_state.clear()
    yield
    v9_5_xrp_blend._consec_state.clear()


# ── Forward-compat: timesfm side not emitting yet ──────────────────────────

class TestForwardCompat:
    def test_model_not_loaded_skips_cleanly(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=None)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_5_xrp_model_not_loaded"
        assert d.strategy_id == "v9_5_xrp_blend"


# ── Defensive asset guard ──────────────────────────────────────────────────

class TestAssetGuard:
    def test_btc_surface_skips_even_with_high_prob(self):
        surface = _make_surface(asset="BTC", probability_lgb_v9_5_xrp=0.85)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_eth_surface_skips(self):
        surface = _make_surface(asset="ETH", probability_lgb_v9_5_xrp=0.85)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"


# ── Eval-offset band ───────────────────────────────────────────────────────

class TestEvalOffsetBand:
    def test_below_min_skips(self):
        surface = _make_surface(eval_offset=50)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason.startswith("outside_eval_band")

    def test_above_max_skips(self):
        surface = _make_surface(eval_offset=250)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason.startswith("outside_eval_band")

    def test_at_min_60_fires(self):
        surface = _make_surface(eval_offset=60, probability_lgb_v9_5_xrp=0.85)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_at_max_240_fires(self):
        surface = _make_surface(eval_offset=240, probability_lgb_v9_5_xrp=0.85)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_none_eval_offset_skips(self):
        surface = _make_surface(eval_offset=None)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason.startswith("outside_eval_band")


# ── Threshold gating ───────────────────────────────────────────────────────

class TestThresholds:
    def test_up_fires_at_0_82(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.82)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.strategy_id == "v9_5_xrp_blend"
        assert d.entry_reason == "v9_5_xrp_blend_pass"

    def test_up_fires_well_above_0_82(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.95)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_down_fires_at_0_20(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.20)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_down_fires_below_0_20(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.05)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_between_thresholds_skips(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.50)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_just_below_up_skips(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.81)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_just_above_down_skips(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.21)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── Runtime override ───────────────────────────────────────────────────────

class TestRuntimeOverride:
    def test_runtime_override_raises_up_threshold(self):
        """gate_params runtime override changes thresholds."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.85)
        # with default 0.82 threshold: TRADE
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "TRADE"
        # with overridden 0.95 threshold: SKIP (0.85 < 0.95)
        with _params(up_threshold=0.95):
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_runtime_override_narrows_eval_band(self):
        surface = _make_surface(eval_offset=80, probability_lgb_v9_5_xrp=0.85)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "TRADE"
        with _params(eval_offset_min=120):
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason.startswith("outside_eval_band")


# ── Metadata shape ─────────────────────────────────────────────────────────

class TestMetadata:
    def test_metadata_contains_probability_on_trade(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.85)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "TRADE"
        assert "probability_lgb_v9_5_xrp" in d.metadata
        assert d.metadata["probability_lgb_v9_5_xrp"] == pytest.approx(0.85)
        assert d.metadata["up_threshold"] == pytest.approx(0.82)
        assert d.metadata["down_threshold"] == pytest.approx(0.20)
        assert d.metadata["eval_offset_min"] == 60
        assert d.metadata["eval_offset_max"] == 240
        assert d.metadata["asset"] == "XRP"

    def test_metadata_contains_sizing_on_trade(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.85)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "TRADE"
        assert d.metadata["entry_cap"] == pytest.approx(0.85)
        assert d.metadata["collateral_pct"] == pytest.approx(0.025)
        assert d.metadata["gtc_cap"] == pytest.approx(0.90)

    def test_confidence_score_at_0_85(self):
        # |0.85 - 0.5| * 2 = 0.70 >= 0.40 -> HIGH
        surface = _make_surface(probability_lgb_v9_5_xrp=0.85)
        with _params():
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "TRADE"
        assert d.confidence == "HIGH"
        assert d.confidence_score == pytest.approx(0.70)
