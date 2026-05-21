"""Unit tests for v9_2_eth_down_late GHOST strategy.

Coverage:
- model-not-loaded SKIP (v9_2_eth_model_not_loaded) — forward-compat path
- defensive asset guard (SKIP on non-ETH surface — wrong_asset)
- eval_offset outside band [60, 90] (outside_eval_band)
- conviction_below_threshold SKIP
- UP gate is DISABLED (impossible threshold 2.0) — even at p=0.99, no UP fire
- DOWN TRADE path at p <= 0.10 per hub note #550 late-band recommendation
- gate_params runtime override respected
- Metadata shape on TRADE includes probability_lgb_v9_2_eth + thresholds
- Strategy is asset-restricted: high-conviction BTC surface still SKIPs

Test strategy mirrors test_v9_2_eth_raw_lgb.py — synthetic _make_surface();
uses _gp.set_active / _gp.reset_active for runtime gate_params override.
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_2_eth_down_late import evaluate_v9_2_eth_down_late
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# ── Surface factory ────────────────────────────────────────────────────────

def _make_surface(**overrides) -> FullDataSurface:
    """Default surface for v9_2_eth_down_late tests.

    asset=ETH, eval_offset=75 (in band [60, 90]), hour_utc=12,
    probability_lgb_v9_2_eth=0.05 (below the 0.10 DOWN threshold).
    """
    defaults = dict(
        asset="ETH", timescale="5m",
        window_ts=1713009600,
        eval_offset=75, assembled_at=time.time(),
        current_price=3400.0, open_price=3380.0,
        delta_binance=0.005, delta_tiingo=0.004, delta_chainlink=0.005,
        delta_pct=0.005, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=0.003,
        v2_probability_up=0.20, v2_probability_raw=0.20,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=0.20, probability_classifier=None,
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
        clob_up_bid=0.28, clob_up_ask=0.30, clob_down_bid=0.63,
        clob_down_ask=0.65, clob_implied_up=0.30,
        gamma_up_price=0.30, gamma_down_price=0.70,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=400_000.0, cg_taker_sell_vol=1_600_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=0.8,
        timesfm_expected_move_bps=-50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=12, seconds_to_close=75,
        probability_lgb_v9_2_eth=0.05,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ── Gate-params helpers ────────────────────────────────────────────────────

_BASE_PARAMS: dict[str, Any] = {
    "up_threshold": 2.0,          # impossible — disables UP
    "down_threshold": 0.10,
    "eval_offset_min": 60,
    "eval_offset_max": 90,
    "min_consecutive_pass_ticks": 1,
    "expected_asset": "ETH",
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
    from strategies.configs import v9_2_eth_down_late
    v9_2_eth_down_late._consec_state.clear()
    yield
    v9_2_eth_down_late._consec_state.clear()


# ── Forward-compat: timesfm side not emitting yet ──────────────────────────

class TestForwardCompat:
    def test_model_not_loaded_skips_cleanly(self):
        surface = _make_surface(probability_lgb_v9_2_eth=None)
        with _params():
            d = evaluate_v9_2_eth_down_late(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_2_eth_model_not_loaded"
        assert d.strategy_id == "v9_2_eth_down_late"


# ── Defensive asset guard ──────────────────────────────────────────────────

class TestAssetGuard:
    def test_btc_surface_skips_even_with_low_prob(self):
        surface = _make_surface(asset="BTC", probability_lgb_v9_2_eth=0.02)
        with _params():
            d = evaluate_v9_2_eth_down_late(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_xrp_surface_skips(self):
        surface = _make_surface(asset="XRP", probability_lgb_v9_2_eth=0.02)
        with _params():
            d = evaluate_v9_2_eth_down_late(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"


# ── Eval-offset band [60, 90] — LATE WINDOW ───────────────────────────────

class TestEvalOffsetBand:
    def test_below_min_skips(self):
        """eval_offset=50 is OUT of the late band — too close to resolution."""
        surface = _make_surface(eval_offset=50)
        with _params():
            d = evaluate_v9_2_eth_down_late(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_above_max_skips(self):
        """eval_offset=120 is OUT — captured by v9_2_eth_raw_lgb instead."""
        surface = _make_surface(eval_offset=120)
        with _params():
            d = evaluate_v9_2_eth_down_late(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_at_min_60_fires(self):
        surface = _make_surface(eval_offset=60, probability_lgb_v9_2_eth=0.05)
        with _params():
            d = evaluate_v9_2_eth_down_late(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_at_max_90_fires(self):
        surface = _make_surface(eval_offset=90, probability_lgb_v9_2_eth=0.05)
        with _params():
            d = evaluate_v9_2_eth_down_late(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"


# ── UP gate disabled (impossible threshold) ────────────────────────────────

class TestUpGateDisabled:
    def test_extreme_high_prob_skips(self):
        """p=0.99 would fire UP at 0.85 threshold — but our UP threshold is 2.0,
        so UP is unreachable. Strategy SKIPs as conviction_below_threshold."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.99)
        with _params():
            d = evaluate_v9_2_eth_down_late(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_perfect_prob_skips(self):
        surface = _make_surface(probability_lgb_v9_2_eth=1.0)
        with _params():
            d = evaluate_v9_2_eth_down_late(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── DOWN threshold gating ──────────────────────────────────────────────────

class TestDownThresholds:
    def test_down_fires_at_0_10(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.10)
        with _params():
            d = evaluate_v9_2_eth_down_late(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"
        assert d.strategy_id == "v9_2_eth_down_late"
        assert d.entry_reason == "v9_2_eth_down_late_pass"

    def test_down_fires_below_0_10(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.05)
        with _params():
            d = evaluate_v9_2_eth_down_late(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_just_above_threshold_skips(self):
        """0.11 is barely above 0.10 — should NOT fire DOWN."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.11)
        with _params():
            d = evaluate_v9_2_eth_down_late(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_mid_range_skips(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.5)
        with _params():
            d = evaluate_v9_2_eth_down_late(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── Runtime gate_params override ──────────────────────────────────────────

class TestRuntimeOverride:
    def test_override_widens_band(self):
        """Allow runtime to widen the eval_offset band via gate_params."""
        surface = _make_surface(eval_offset=100, probability_lgb_v9_2_eth=0.05)
        with _params(eval_offset_max=120):
            d = evaluate_v9_2_eth_down_late(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_override_can_re_enable_up(self):
        """Sanity: if gate_params.up_threshold is overridden below 1.0, UP
        could re-fire (configured experiment). Otherwise UP is permanently off."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.95)
        with _params(up_threshold=0.90):
            d = evaluate_v9_2_eth_down_late(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"


# ── Metadata shape ─────────────────────────────────────────────────────────

class TestMetadata:
    def test_trade_metadata_includes_thresholds(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.05)
        with _params():
            d = evaluate_v9_2_eth_down_late(surface)
        assert d.action == "TRADE"
        m = d.metadata
        assert m["probability_lgb_v9_2_eth"] == 0.05
        assert m["up_threshold"] == 2.0
        assert m["down_threshold"] == 0.10
        assert m["eval_offset_min"] == 60
        assert m["eval_offset_max"] == 90
        assert m["asset"] == "ETH"
        assert m["consec_tick_count"] >= 1
