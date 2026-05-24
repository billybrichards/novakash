"""Unit tests for v9_2_v12_AND_ghost GHOST strategy.

Coverage:
- model-not-loaded SKIP for each of v9.2 / v12 (None probability)
- defensive asset guard (SKIP on non-BTC surface — wrong_asset)
- eval_offset outside band [60, 210] (outside_eval_band)
- conviction_below_threshold SKIP when only one side passes
- TRADE path: UP at (0.725, 0.750), DOWN at (0.25, 0.20)
- gate_params runtime override respected
- Metadata shape on TRADE includes both probabilities + thresholds
- NULL probability handling skips evaluation cleanly

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

from strategies.configs.v9_2_v12_AND_ghost import evaluate_v9_2_v12_AND_ghost
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# ── Surface factory ────────────────────────────────────────────────────────

def _make_surface(**overrides) -> FullDataSurface:
    """Default surface for BTC AND-filter ensemble tests.

    asset=BTC, eval_offset=120 (in band [60, 210]),
    probability_lgb_v9_2=0.80, probability_lgb_v12=0.80
    (both above audit UP thresholds 0.725/0.750).
    """
    defaults = dict(
        asset="BTC", timescale="5m",
        window_ts=1713009600,
        eval_offset=120, assembled_at=time.time(),
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
        hour_utc=12, seconds_to_close=180,
        probability_lgb_v9_2=0.80,
        probability_lgb_v12=0.80,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ── Gate-params helpers ────────────────────────────────────────────────────

_BASE_PARAMS: dict[str, Any] = {
    "up_v92_threshold": 0.725,
    "up_v12_threshold": 0.750,
    "down_v92_threshold": 0.25,
    "down_v12_threshold": 0.20,
    "eval_offset_min": 60,
    "eval_offset_max": 210,
    "min_consecutive_pass_ticks": 1,
    "expected_asset": "BTC",
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
    from strategies.configs import v9_2_v12_AND_ghost
    v9_2_v12_AND_ghost._consec_state.clear()
    yield
    v9_2_v12_AND_ghost._consec_state.clear()


# ── Forward-compat / NULL probability handling ─────────────────────────────

class TestNullProbabilityHandling:
    def test_v9_2_none_skips_cleanly(self):
        surface = _make_surface(probability_lgb_v9_2=None)
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_2_model_not_loaded"
        assert d.strategy_id == "v9_2_v12_AND_ghost"

    def test_v12_none_skips_cleanly(self):
        surface = _make_surface(probability_lgb_v12=None)
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v12_model_not_loaded"

    def test_both_none_skips_on_v9_2_first(self):
        surface = _make_surface(
            probability_lgb_v9_2=None, probability_lgb_v12=None
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        # v9.2 check fires first
        assert d.skip_reason == "v9_2_model_not_loaded"


# ── Defensive asset guard ──────────────────────────────────────────────────

class TestAssetGuard:
    def test_eth_surface_skips_even_with_high_probs(self):
        surface = _make_surface(
            asset="ETH",
            probability_lgb_v9_2=0.80, probability_lgb_v12=0.80,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_xrp_surface_skips(self):
        surface = _make_surface(
            asset="XRP",
            probability_lgb_v9_2=0.80, probability_lgb_v12=0.80,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"


# ── Eval-offset band ───────────────────────────────────────────────────────

class TestEvalOffsetBand:
    def test_below_min_skips(self):
        surface = _make_surface(eval_offset=50)
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_above_max_skips(self):
        surface = _make_surface(eval_offset=220)
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_at_min_60_fires(self):
        surface = _make_surface(eval_offset=60)
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_at_max_210_fires(self):
        surface = _make_surface(eval_offset=210)
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_none_eval_offset_skips(self):
        surface = _make_surface(eval_offset=None)
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


# ── UP-side threshold gating ───────────────────────────────────────────────

class TestUpThresholds:
    def test_up_fires_when_both_at_audit_thresholds(self):
        # Exactly at audit op-point (0.725 / 0.750).
        surface = _make_surface(
            probability_lgb_v9_2=0.725, probability_lgb_v12=0.750,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.strategy_id == "v9_2_v12_AND_ghost"
        assert d.entry_reason == "v9_2_v12_AND_ghost_pass"

    def test_up_fires_well_above_thresholds(self):
        surface = _make_surface(
            probability_lgb_v9_2=0.95, probability_lgb_v12=0.92,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_up_skips_when_only_v9_2_above_threshold(self):
        # v9.2 strong UP, v12 below 0.750
        surface = _make_surface(
            probability_lgb_v9_2=0.85, probability_lgb_v12=0.70,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_up_skips_when_only_v12_above_threshold(self):
        # v12 strong UP, v9.2 below 0.725
        surface = _make_surface(
            probability_lgb_v9_2=0.70, probability_lgb_v12=0.85,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_up_skips_just_below_v92_threshold(self):
        surface = _make_surface(
            probability_lgb_v9_2=0.724, probability_lgb_v12=0.80,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_up_skips_just_below_v12_threshold(self):
        surface = _make_surface(
            probability_lgb_v9_2=0.80, probability_lgb_v12=0.749,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── DOWN-side threshold gating ─────────────────────────────────────────────

class TestDownThresholds:
    def test_down_fires_when_both_at_audit_thresholds(self):
        # Exactly at audit DOWN op-point (0.25 / 0.20).
        surface = _make_surface(
            probability_lgb_v9_2=0.25, probability_lgb_v12=0.20,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_down_fires_well_below_thresholds(self):
        surface = _make_surface(
            probability_lgb_v9_2=0.05, probability_lgb_v12=0.08,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_down_skips_when_only_v9_2_below_threshold(self):
        surface = _make_surface(
            probability_lgb_v9_2=0.15, probability_lgb_v12=0.30,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_down_skips_when_only_v12_below_threshold(self):
        surface = _make_surface(
            probability_lgb_v9_2=0.30, probability_lgb_v12=0.15,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_down_skips_just_above_v92_threshold(self):
        surface = _make_surface(
            probability_lgb_v9_2=0.251, probability_lgb_v12=0.15,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_down_skips_just_above_v12_threshold(self):
        surface = _make_surface(
            probability_lgb_v9_2=0.20, probability_lgb_v12=0.201,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── Neutral / disagree ─────────────────────────────────────────────────────

class TestNeutralAndDisagree:
    def test_both_neutral_skips(self):
        surface = _make_surface(
            probability_lgb_v9_2=0.50, probability_lgb_v12=0.50,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_disagree_v9_2_up_v12_down_skips(self):
        # v9.2 says UP strong, v12 says DOWN strong — disagreement.
        surface = _make_surface(
            probability_lgb_v9_2=0.85, probability_lgb_v12=0.10,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_disagree_v9_2_down_v12_up_skips(self):
        surface = _make_surface(
            probability_lgb_v9_2=0.10, probability_lgb_v12=0.85,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── Runtime override ───────────────────────────────────────────────────────

class TestRuntimeOverride:
    def test_runtime_override_raises_v92_up_threshold(self):
        """gate_params runtime override changes thresholds."""
        surface = _make_surface(
            probability_lgb_v9_2=0.80, probability_lgb_v12=0.80,
        )
        # default thresholds: TRADE
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "TRADE"
        # raise v9.2 UP threshold above 0.80: SKIP
        with _params(up_v92_threshold=0.90):
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_runtime_override_raises_v12_up_threshold(self):
        surface = _make_surface(
            probability_lgb_v9_2=0.80, probability_lgb_v12=0.80,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "TRADE"
        with _params(up_v12_threshold=0.95):
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_runtime_override_narrows_eval_band(self):
        surface = _make_surface(eval_offset=80)
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "TRADE"
        with _params(eval_offset_min=120):
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


# ── Metadata shape ─────────────────────────────────────────────────────────

class TestMetadata:
    def test_metadata_contains_both_probabilities_on_trade(self):
        surface = _make_surface(
            probability_lgb_v9_2=0.80, probability_lgb_v12=0.80,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "TRADE"
        assert d.metadata["probability_lgb_v9_2"] == pytest.approx(0.80)
        assert d.metadata["probability_lgb_v12"] == pytest.approx(0.80)
        assert d.metadata["up_v92_threshold"] == pytest.approx(0.725)
        assert d.metadata["up_v12_threshold"] == pytest.approx(0.750)
        assert d.metadata["down_v92_threshold"] == pytest.approx(0.25)
        assert d.metadata["down_v12_threshold"] == pytest.approx(0.20)
        assert d.metadata["eval_offset_min"] == 60
        assert d.metadata["eval_offset_max"] == 210
        assert d.metadata["asset"] == "BTC"

    def test_metadata_contains_sizing_on_trade(self):
        surface = _make_surface(
            probability_lgb_v9_2=0.80, probability_lgb_v12=0.80,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "TRADE"
        assert d.metadata["entry_cap"] == pytest.approx(0.85)
        assert d.metadata["collateral_pct"] == pytest.approx(0.025)
        assert d.metadata["gtc_cap"] == pytest.approx(0.90)

    def test_confidence_score_uses_stronger_signal(self):
        # v9.2 = 0.80 -> |0.80 - 0.5| * 2 = 0.60
        # v12  = 0.85 -> |0.85 - 0.5| * 2 = 0.70 (stronger)
        # max -> 0.70, HIGH (>= 0.40)
        surface = _make_surface(
            probability_lgb_v9_2=0.80, probability_lgb_v12=0.85,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "TRADE"
        assert d.confidence == "HIGH"
        assert d.confidence_score == pytest.approx(0.70)


# ── Cross-strategy independence ────────────────────────────────────────────

class TestCrossStrategyIndependence:
    def test_v9_2_v12_combo_threshold_does_not_fire_AND_ghost(self):
        """The LIVE v9_2_v12_combo fires at p_v9_2 >= 0.75 AND p_v12 >= 0.70.
        At (0.75, 0.70), v9.2 >= 0.725 (yes) but v12 < 0.750 (no), so
        v9_2_v12_AND_ghost should NOT fire — different op-point.
        """
        surface = _make_surface(
            probability_lgb_v9_2=0.75, probability_lgb_v12=0.70,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_AND_ghost_threshold_does_fire_at_audit_op_point(self):
        """Audit op-point (0.725, 0.750) — both at exact thresholds — fires."""
        surface = _make_surface(
            probability_lgb_v9_2=0.725, probability_lgb_v12=0.750,
        )
        with _params():
            d = evaluate_v9_2_v12_AND_ghost(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
