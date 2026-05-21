"""Unit tests for v_eth_15m_classifier_strict + the ConfidenceGate
classifier-source wiring (Hub #546).

Coverage:
- The new hook (probability_classifier-driven):
    * SKIP when probability_classifier is None
    * SKIP when conviction below threshold (prob=0.60, conf_dist=0.10)
    * TRADE UP when prob above threshold (prob=0.80)
    * TRADE DOWN when prob below threshold (prob=0.15)
    * SKIP when eval_offset outside band
    * SKIP until consecutive-tick confirmation reached
- The ConfidenceGate wiring fix:
    * Pin that source="classifier" reads surface.probability_classifier
      (not poly_confidence_distance / v2_probability_up).
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v_eth_15m_classifier_strict import (
    evaluate_v_eth_15m_classifier_strict,
)
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface
from strategies.gates.confidence import ConfidenceGate


# ── Surface factory ────────────────────────────────────────────────────────


def _make_surface(**overrides) -> FullDataSurface:
    """Default surface for classifier strict tests.

    eval_offset=300 (in band 60-450 for 15m), hour_utc=12 (not blocked),
    probability_classifier=0.80 (well above 0.75 UP threshold).
    """
    defaults = dict(
        asset="ETH", timescale="15m",
        window_ts=1713009600,
        eval_offset=300, assembled_at=time.time(),
        current_price=2500.0, open_price=2495.0,
        delta_binance=0.002, delta_tiingo=0.002, delta_chainlink=0.002,
        delta_pct=0.002, delta_source="chainlink",
        vpin=0.45, regime="NORMAL", twap_delta=0.0015,
        v2_probability_up=None, v2_probability_raw=None,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=None,
        probability_classifier=0.80,
        ensemble_config=None,
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
        # NOTE: poly fields all None — 15m snapshots typically don't ship
        # the polymarket_live_recommended_outcome block. This is the bug
        # case for the legacy ConfidenceGate.
        poly_direction=None, poly_trade_advised=None, poly_confidence=None,
        poly_confidence_distance=None, poly_timing=None,
        poly_max_entry_price=None, poly_reason=None,
        v4_recommended_side=None, v4_recommended_collateral_pct=None,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=None, clob_up_ask=None, clob_down_bid=None,
        clob_down_ask=None, clob_implied_up=None,
        gamma_up_price=None, gamma_down_price=None,
        cg_oi_usd=None, cg_funding_rate=None,
        cg_taker_buy_vol=None, cg_taker_sell_vol=None,
        cg_liq_total=None, cg_liq_long=None, cg_liq_short=None,
        cg_long_short_ratio=None,
        timesfm_expected_move_bps=None, timesfm_vol_forecast_bps=None,
        hour_utc=12, seconds_to_close=300,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ── Gate-params helper ─────────────────────────────────────────────────────


_BASE_PARAMS: dict[str, Any] = {
    "conf_distance_min": 0.25,
    "eval_offset_min": 60,
    "eval_offset_max": 450,
    "min_consecutive_pass_ticks": 2,
    "blocked_hours_utc": [],
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
    from strategies.configs import v_eth_15m_classifier_strict as mod
    mod._consec_state.clear()
    yield
    mod._consec_state.clear()


# ── Hook tests ─────────────────────────────────────────────────────────────


class TestVEth15mClassifierStrictHook:
    def test_skip_when_prob_classifier_unavailable(self):
        surface = _make_surface(probability_classifier=None)
        with _params():
            d = evaluate_v_eth_15m_classifier_strict(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "probability_classifier_not_available"

    def test_skip_when_conf_below_threshold(self):
        # prob=0.60 → conf_dist=0.10 < 0.25 min
        surface = _make_surface(probability_classifier=0.60)
        with _params():
            d = evaluate_v_eth_15m_classifier_strict(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"
        assert d.metadata["conf_distance"] == pytest.approx(0.10)

    def test_fire_up_when_prob_above_threshold(self):
        # prob=0.80 → conf_dist=0.30 >= 0.25; first tick is awaiting,
        # second tick fires.
        surface = _make_surface(probability_classifier=0.80)
        with _params():
            d1 = evaluate_v_eth_15m_classifier_strict(surface)
            assert d1.action == "SKIP"
            assert "awaiting_consec_ticks" in d1.skip_reason
            d2 = evaluate_v_eth_15m_classifier_strict(surface)
        assert d2.action == "TRADE"
        assert d2.direction == "UP"
        assert d2.strategy_id == "v_eth_15m_classifier_strict"
        assert d2.metadata["probability_classifier"] == pytest.approx(0.80)
        assert d2.metadata["consec_tick_count"] == 2

    def test_fire_down_when_prob_below_threshold(self):
        # prob=0.15 → conf_dist=0.35 >= 0.25
        surface = _make_surface(probability_classifier=0.15)
        with _params():
            evaluate_v_eth_15m_classifier_strict(surface)  # tick 1
            d = evaluate_v_eth_15m_classifier_strict(surface)  # tick 2
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_blocked_eval_offset(self):
        # eval_offset=30 < min=60
        surface = _make_surface(eval_offset=30, probability_classifier=0.80)
        with _params():
            d = evaluate_v_eth_15m_classifier_strict(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

        # eval_offset=500 > max=450
        surface2 = _make_surface(eval_offset=500, probability_classifier=0.80)
        with _params():
            d2 = evaluate_v_eth_15m_classifier_strict(surface2)
        assert d2.action == "SKIP"
        assert d2.skip_reason == "outside_eval_band"

    def test_consec_tick_requirement(self):
        # With min_consecutive_pass_ticks=3, first two ticks SKIP, third fires.
        surface = _make_surface(probability_classifier=0.80)
        with _params(min_consecutive_pass_ticks=3):
            d1 = evaluate_v_eth_15m_classifier_strict(surface)
            d2 = evaluate_v_eth_15m_classifier_strict(surface)
            d3 = evaluate_v_eth_15m_classifier_strict(surface)
        assert d1.action == "SKIP" and "awaiting_consec_ticks" in d1.skip_reason
        assert d2.action == "SKIP" and "awaiting_consec_ticks" in d2.skip_reason
        assert d3.action == "TRADE"
        assert d3.metadata["consec_tick_count"] == 3

    def test_blocked_utc_hour(self):
        surface = _make_surface(
            hour_utc=15, probability_classifier=0.80
        )
        with _params(blocked_hours_utc=[15]):
            d = evaluate_v_eth_15m_classifier_strict(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "blocked_utc_hour_15"


# ── ConfidenceGate wiring fix tests (Hub #546) ─────────────────────────────


class TestConfidenceGateClassifierSource:
    """Pin that source='classifier' reads surface.probability_classifier."""

    def test_classifier_source_passes_when_above_min_dist(self):
        # prob=0.80 → dist=0.30 >= 0.18
        surface = _make_surface(probability_classifier=0.80)
        gate = ConfidenceGate(min_dist=0.18, source="classifier")
        r = gate.evaluate(surface)
        assert r.passed is True
        assert r.data["source"] == "classifier"
        assert r.data["distance"] == pytest.approx(0.30)

    def test_classifier_source_skips_when_below_min_dist(self):
        # prob=0.60 → dist=0.10 < 0.18
        surface = _make_surface(probability_classifier=0.60)
        gate = ConfidenceGate(min_dist=0.18, source="classifier")
        r = gate.evaluate(surface)
        assert r.passed is False
        assert "0.100 < min=0.18" in r.reason

    def test_classifier_source_unavailable_reason_when_none(self):
        surface = _make_surface(probability_classifier=None)
        gate = ConfidenceGate(min_dist=0.18, source="classifier")
        r = gate.evaluate(surface)
        assert r.passed is False
        assert r.reason == "probability_classifier not available"

    def test_classifier_source_ignores_poly_when_classifier_none(self):
        # Even if poly_confidence_distance is set, classifier source ignores it.
        surface = _make_surface(
            probability_classifier=None,
            poly_confidence_distance=0.40,
        )
        gate = ConfidenceGate(min_dist=0.18, source="classifier")
        r = gate.evaluate(surface)
        assert r.passed is False
        assert "probability_classifier not available" in r.reason

    def test_legacy_poly_source_unchanged(self):
        # Default constructor (no source kwarg) preserves pre-#546 behaviour.
        surface = _make_surface(
            probability_classifier=0.80,  # ignored by legacy path
            poly_confidence_distance=0.20,
        )
        gate = ConfidenceGate(min_dist=0.18)
        r = gate.evaluate(surface)
        assert r.passed is True
        assert r.data["source"] == "poly"
        assert r.data["distance"] == pytest.approx(0.20)

    def test_legacy_poly_source_falls_back_to_v2_prob_up(self):
        # poly missing → fall back to v2_probability_up (legacy behaviour).
        surface = _make_surface(
            probability_classifier=0.80,
            poly_confidence_distance=None,
            v2_probability_up=0.70,
        )
        gate = ConfidenceGate(min_dist=0.18)
        r = gate.evaluate(surface)
        assert r.passed is True
        assert r.data["source"] == "poly"
        # dist = |0.70 - 0.50| = 0.20
        assert r.data["distance"] == pytest.approx(0.20)

    def test_invalid_source_raises(self):
        with pytest.raises(ValueError):
            ConfidenceGate(min_dist=0.10, source="banana")
