"""Unit tests for v9_3_btc_tight GHOST strategy (high-precision corner).

Coverage:
- model-not-loaded SKIP (v9_3_btc_model_not_loaded) — forward-compat path
- defensive asset guard (SKIP on non-BTC surface — wrong_asset)
- eval_offset outside band [20, 170] (outside_eval_band)
- conviction_below_threshold SKIP at the tight thresholds (0.935/0.065)
- TRADE path: UP at 0.935, DOWN at 0.065 per walk-forward CV
- gate_params runtime override respected
- Metadata shape on TRADE includes probability_lgb_v9_3_btc + thresholds
- Co-exists with v9_3_btc_raw_lgb (different module state)

Test strategy: synthetic _make_surface() fixture.
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_3_btc_tight import evaluate_v9_3_btc_tight
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


def _make_surface(**overrides) -> FullDataSurface:
    """Default surface: asset=BTC, eval_offset=100 (in band [20, 170]),
    probability_lgb_v9_3_btc=0.95 (above 0.935 tight UP threshold).
    """
    defaults = dict(
        asset="BTC", timescale="5m",
        window_ts=1713009600,
        eval_offset=100, assembled_at=time.time(),
        current_price=110000.0, open_price=109500.0,
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
        hour_utc=12, seconds_to_close=200,
        probability_lgb_v9_3_btc=0.95,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


_BASE_PARAMS: dict[str, Any] = {
    "up_threshold": 0.935,
    "down_threshold": 0.065,
    "eval_offset_min": 20,
    "eval_offset_max": 170,
    "min_consecutive_pass_ticks": 1,
    "expected_asset": "BTC",
    "entry_cap": 0.935,
    "collateral_pct": 0.025,
    "gtc_cap": 0.935,
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
    from strategies.configs import v9_3_btc_tight
    v9_3_btc_tight._consec_state.clear()
    yield
    v9_3_btc_tight._consec_state.clear()


class TestForwardCompat:
    def test_model_not_loaded_skips_cleanly(self):
        surface = _make_surface(probability_lgb_v9_3_btc=None)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_3_btc_model_not_loaded"
        assert d.strategy_id == "v9_3_btc_tight"


class TestAssetGuard:
    def test_eth_surface_skips(self):
        surface = _make_surface(asset="ETH", probability_lgb_v9_3_btc=0.95)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_xrp_surface_skips(self):
        surface = _make_surface(asset="XRP", probability_lgb_v9_3_btc=0.95)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"


class TestEvalOffsetBand:
    def test_below_min_skips(self):
        # eval_offset=10 is below band min 20 (early-window band)
        surface = _make_surface(eval_offset=10)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_above_max_skips(self):
        # eval_offset=180 is above band max 170 (late-window — tight doesn't fire)
        surface = _make_surface(eval_offset=180)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_at_min_20_fires(self):
        surface = _make_surface(eval_offset=20, probability_lgb_v9_3_btc=0.95)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_at_max_170_fires(self):
        surface = _make_surface(eval_offset=170, probability_lgb_v9_3_btc=0.95)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_late_window_skips(self):
        """eval_offset=200 (late window) is OUT — tight is early-window only."""
        surface = _make_surface(eval_offset=200, probability_lgb_v9_3_btc=0.95)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


class TestThresholds:
    def test_up_fires_at_0_935(self):
        surface = _make_surface(probability_lgb_v9_3_btc=0.935)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.entry_reason == "v9_3_btc_tight_pass"

    def test_up_fires_well_above_threshold(self):
        surface = _make_surface(probability_lgb_v9_3_btc=0.99)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_down_fires_at_0_065(self):
        surface = _make_surface(probability_lgb_v9_3_btc=0.065)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_down_fires_below_threshold(self):
        surface = _make_surface(probability_lgb_v9_3_btc=0.01)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_just_below_up_skips(self):
        # 0.93 < 0.935 — tight UP doesn't fire even at high raw conviction
        surface = _make_surface(probability_lgb_v9_3_btc=0.93)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_just_above_down_skips(self):
        # 0.07 > 0.065 — tight DOWN doesn't fire
        surface = _make_surface(probability_lgb_v9_3_btc=0.07)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_raw_lgb_threshold_does_not_fire_on_tight(self):
        """v9_3_btc_raw_lgb would fire at p=0.72, but tight requires p>=0.935.

        Confirms the two sibling strategies operate at different thresholds
        on the same probability column.
        """
        surface = _make_surface(probability_lgb_v9_3_btc=0.72)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


class TestRuntimeOverride:
    def test_runtime_override_widens_band(self):
        surface = _make_surface(eval_offset=180, probability_lgb_v9_3_btc=0.95)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "SKIP"
        with _params(eval_offset_max=210):
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "TRADE"

    def test_runtime_override_lowers_up_threshold(self):
        surface = _make_surface(probability_lgb_v9_3_btc=0.90)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "SKIP"
        with _params(up_threshold=0.85):
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"


class TestMetadata:
    def test_metadata_contains_probability_on_trade(self):
        surface = _make_surface(probability_lgb_v9_3_btc=0.95)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "TRADE"
        assert d.metadata["probability_lgb_v9_3_btc"] == pytest.approx(0.95)
        assert d.metadata["up_threshold"] == pytest.approx(0.935)
        assert d.metadata["down_threshold"] == pytest.approx(0.065)
        assert d.metadata["eval_offset_min"] == 20
        assert d.metadata["eval_offset_max"] == 170
        assert d.metadata["asset"] == "BTC"

    def test_metadata_contains_sizing_on_trade(self):
        surface = _make_surface(probability_lgb_v9_3_btc=0.95)
        with _params():
            d = evaluate_v9_3_btc_tight(surface)
        assert d.action == "TRADE"
        assert d.metadata["entry_cap"] == pytest.approx(0.935)
        assert d.metadata["collateral_pct"] == pytest.approx(0.025)
        assert d.metadata["gtc_cap"] == pytest.approx(0.935)
