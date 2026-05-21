"""Unit tests for v9_2_iso_volmatch / v9_2_iso_expand / v9_2_iso_strict.

Coverage:
- model-not-loaded SKIP (v9_2_post_iso_not_loaded)
- Blocked UTC hours 09 and 15 (blocked_utc_hour_N)
- eval_offset outside band [60, 210] (outside_eval_band)
- conviction_below_threshold SKIP
- TRADE path: UP and DOWN for each variant
- Gate thresholds are correct per spec:
    volmatch: UP >= 0.797, DOWN <= 0.098
    expand:   UP >= 0.72,  DOWN <= 0.20
    strict:   UP >= 0.85,  DOWN <= 0.12
- gate_params runtime override respected
- Metadata shape on TRADE includes probability_lgb_v9_2_post_iso

Test strategy: synthetic _make_surface() fixture; autouse _bind_gate_params
uses _gp.set_active/_gp.reset_active (same pattern as test_v9_2_super_lgb_only).
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_2_iso_volmatch import evaluate_v9_2_iso_volmatch
from strategies.configs.v9_2_iso_expand import evaluate_v9_2_iso_expand
from strategies.configs.v9_2_iso_strict import evaluate_v9_2_iso_strict
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# ── Surface factory ────────────────────────────────────────────────────────

def _make_surface(**overrides) -> FullDataSurface:
    """Default surface for iso strategy tests.

    eval_offset=120 (in band), hour_utc=12 (not blocked),
    probability_lgb_v9_2_post_iso=0.90 (above all UP thresholds).
    """
    defaults = dict(
        asset="BTC", timescale="5m",
        window_ts=1713009600,
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=85000.0,
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
        probability_lgb_v9_2=0.80,
        probability_lgb_v9_2_post_iso=0.90,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ── Gate-params helpers ────────────────────────────────────────────────────

_BASE_PARAMS: dict[str, Any] = {
    "eval_offset_min": 60,
    "eval_offset_max": 210,
    "min_consecutive_pass_ticks": 1,
    "blocked_hours_utc": [9, 15],
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
    from strategies.configs import v9_2_iso_volmatch, v9_2_iso_expand, v9_2_iso_strict
    v9_2_iso_volmatch._consec_state.clear()
    v9_2_iso_expand._consec_state.clear()
    v9_2_iso_strict._consec_state.clear()
    yield
    v9_2_iso_volmatch._consec_state.clear()
    v9_2_iso_expand._consec_state.clear()
    v9_2_iso_strict._consec_state.clear()


# ── v9_2_iso_volmatch (thresholds: UP >= 0.797, DOWN <= 0.098) ────────────

class TestV92IsoVolmatch:
    """Tests for the sanity-check baseline variant."""

    def test_not_loaded_skips(self):
        surface = _make_surface(probability_lgb_v9_2_post_iso=None)
        with _params(up_threshold=0.797, down_threshold=0.098):
            d = evaluate_v9_2_iso_volmatch(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_2_post_iso_not_loaded"

    def test_blocked_hour_9_skips(self):
        surface = _make_surface(hour_utc=9, probability_lgb_v9_2_post_iso=0.90)
        with _params(up_threshold=0.797, down_threshold=0.098):
            d = evaluate_v9_2_iso_volmatch(surface)
        assert d.action == "SKIP"
        assert "blocked_utc_hour_9" in d.skip_reason

    def test_blocked_hour_15_skips(self):
        surface = _make_surface(hour_utc=15, probability_lgb_v9_2_post_iso=0.90)
        with _params(up_threshold=0.797, down_threshold=0.098):
            d = evaluate_v9_2_iso_volmatch(surface)
        assert d.action == "SKIP"
        assert "blocked_utc_hour_15" in d.skip_reason

    def test_outside_eval_band_skips(self):
        surface = _make_surface(eval_offset=250, probability_lgb_v9_2_post_iso=0.90)
        with _params(up_threshold=0.797, down_threshold=0.098):
            d = evaluate_v9_2_iso_volmatch(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_conviction_below_threshold_skips(self):
        # 0.50 is neither >= 0.797 nor <= 0.098
        surface = _make_surface(probability_lgb_v9_2_post_iso=0.50)
        with _params(up_threshold=0.797, down_threshold=0.098):
            d = evaluate_v9_2_iso_volmatch(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_up_trade_fires_at_0_797(self):
        surface = _make_surface(probability_lgb_v9_2_post_iso=0.797)
        with _params(up_threshold=0.797, down_threshold=0.098):
            d = evaluate_v9_2_iso_volmatch(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.strategy_id == "v9_2_iso_volmatch"
        assert d.metadata["probability_lgb_v9_2_post_iso"] == pytest.approx(0.797)

    def test_up_trade_fires_above_0_797(self):
        surface = _make_surface(probability_lgb_v9_2_post_iso=0.95)
        with _params(up_threshold=0.797, down_threshold=0.098):
            d = evaluate_v9_2_iso_volmatch(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_down_trade_fires_at_0_098(self):
        surface = _make_surface(probability_lgb_v9_2_post_iso=0.098)
        with _params(up_threshold=0.797, down_threshold=0.098):
            d = evaluate_v9_2_iso_volmatch(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"
        assert d.strategy_id == "v9_2_iso_volmatch"

    def test_between_thresholds_skips(self):
        surface = _make_surface(probability_lgb_v9_2_post_iso=0.50)
        with _params(up_threshold=0.797, down_threshold=0.098):
            d = evaluate_v9_2_iso_volmatch(surface)
        assert d.action == "SKIP"

    def test_raw_0_72_skips_volmatch(self):
        # 0.72 < 0.797 — below volmatch UP threshold
        surface = _make_surface(probability_lgb_v9_2_post_iso=0.72)
        with _params(up_threshold=0.797, down_threshold=0.098):
            d = evaluate_v9_2_iso_volmatch(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── v9_2_iso_expand (thresholds: UP >= 0.72, DOWN <= 0.20) ───────────────

class TestV92IsoExpand:
    """Tests for the headline thesis variant."""

    def test_not_loaded_skips(self):
        surface = _make_surface(probability_lgb_v9_2_post_iso=None)
        with _params(up_threshold=0.72, down_threshold=0.20):
            d = evaluate_v9_2_iso_expand(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_2_post_iso_not_loaded"

    def test_up_fires_at_0_72(self):
        surface = _make_surface(probability_lgb_v9_2_post_iso=0.72)
        with _params(up_threshold=0.72, down_threshold=0.20):
            d = evaluate_v9_2_iso_expand(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.strategy_id == "v9_2_iso_expand"

    def test_down_fires_at_0_20(self):
        surface = _make_surface(probability_lgb_v9_2_post_iso=0.20)
        with _params(up_threshold=0.72, down_threshold=0.20):
            d = evaluate_v9_2_iso_expand(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_conviction_below_0_72_skips(self):
        surface = _make_surface(probability_lgb_v9_2_post_iso=0.65)
        with _params(up_threshold=0.72, down_threshold=0.20):
            d = evaluate_v9_2_iso_expand(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_expand_fires_where_volmatch_would_skip(self):
        # 0.72 < 0.797: expand fires at 0.72, volmatch threshold is 0.797
        surface = _make_surface(probability_lgb_v9_2_post_iso=0.72)
        with _params(up_threshold=0.72, down_threshold=0.20):
            d = evaluate_v9_2_iso_expand(surface)
        assert d.action == "TRADE"

    def test_blocked_hours_respected(self):
        for h in [9, 15]:
            surface = _make_surface(hour_utc=h, probability_lgb_v9_2_post_iso=0.90)
            with _params(up_threshold=0.72, down_threshold=0.20):
                d = evaluate_v9_2_iso_expand(surface)
            assert d.action == "SKIP", f"Expected SKIP at hour {h}"

    def test_metadata_contains_iso_probability(self):
        surface = _make_surface(probability_lgb_v9_2_post_iso=0.90)
        with _params(up_threshold=0.72, down_threshold=0.20):
            d = evaluate_v9_2_iso_expand(surface)
        assert d.action == "TRADE"
        assert "probability_lgb_v9_2_post_iso" in d.metadata
        assert d.metadata["probability_lgb_v9_2_post_iso"] == pytest.approx(0.90)


# ── v9_2_iso_strict (thresholds: UP >= 0.85, DOWN <= 0.12) ───────────────

class TestV92IsoStrict:
    """Tests for the high-conviction variant."""

    def test_not_loaded_skips(self):
        surface = _make_surface(probability_lgb_v9_2_post_iso=None)
        with _params(up_threshold=0.85, down_threshold=0.12):
            d = evaluate_v9_2_iso_strict(surface)
        assert d.action == "SKIP"

    def test_up_fires_at_0_85(self):
        surface = _make_surface(probability_lgb_v9_2_post_iso=0.85)
        with _params(up_threshold=0.85, down_threshold=0.12):
            d = evaluate_v9_2_iso_strict(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.strategy_id == "v9_2_iso_strict"

    def test_up_does_not_fire_below_0_85(self):
        # iso_expand would fire at 0.80 (>= 0.72), strict does not
        surface = _make_surface(probability_lgb_v9_2_post_iso=0.80)
        with _params(up_threshold=0.85, down_threshold=0.12):
            d = evaluate_v9_2_iso_strict(surface)
        assert d.action == "SKIP"

    def test_down_fires_at_0_12(self):
        surface = _make_surface(probability_lgb_v9_2_post_iso=0.12)
        with _params(up_threshold=0.85, down_threshold=0.12):
            d = evaluate_v9_2_iso_strict(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_down_does_not_fire_at_0_20(self):
        # iso_expand fires at 0.20 DOWN, strict threshold is 0.12
        surface = _make_surface(probability_lgb_v9_2_post_iso=0.20)
        with _params(up_threshold=0.85, down_threshold=0.12):
            d = evaluate_v9_2_iso_strict(surface)
        assert d.action == "SKIP"

    def test_blocked_hours_respected(self):
        surface = _make_surface(hour_utc=9, probability_lgb_v9_2_post_iso=0.90)
        with _params(up_threshold=0.85, down_threshold=0.12):
            d = evaluate_v9_2_iso_strict(surface)
        assert d.action == "SKIP"

    def test_runtime_override_threshold(self):
        """gate_params runtime override changes thresholds."""
        surface = _make_surface(probability_lgb_v9_2_post_iso=0.75)
        # with default 0.85 threshold: SKIP
        with _params(up_threshold=0.85, down_threshold=0.12):
            d = evaluate_v9_2_iso_strict(surface)
        assert d.action == "SKIP"
        # with overridden 0.70 threshold: TRADE
        with _params(up_threshold=0.70, down_threshold=0.12):
            d = evaluate_v9_2_iso_strict(surface)
        assert d.action == "TRADE"

    def test_entry_reason_correct(self):
        surface = _make_surface(probability_lgb_v9_2_post_iso=0.90)
        with _params(up_threshold=0.85, down_threshold=0.12):
            d = evaluate_v9_2_iso_strict(surface)
        assert d.action == "TRADE"
        assert d.entry_reason == "v9_2_iso_strict_pass"
