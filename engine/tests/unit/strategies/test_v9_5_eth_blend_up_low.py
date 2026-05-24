"""Unit tests for v9_5_eth_blend_up_low GHOST strategy (UP-LOW tier of the
v9.5 ETH BLEND ladder).

Coverage:
- model-not-loaded SKIP (v9_5_eth_model_not_loaded) — None probability
- defensive asset guard (SKIP on non-ETH surface — wrong_asset)
- eval_offset outside band [60, 240] (outside_eval_band)
- Band gating: [0.78, 0.82) — lower bound inclusive, upper exclusive
- DOWN signals NEVER fire (UP-only — even with extreme DOWN probs)
- Runtime override CANNOT flip the strategy to fire DOWN
- gate_params runtime override respected (band edges, eval offsets)
- Metadata shape on TRADE includes probability + thresholds + strategy_id
- Consecutive-tick (min_consecutive_pass_ticks) gating
- Cross-strategy independence: module-local _consec_state doesn't leak
  into sibling ladder tiers or v9_5_eth_raw_lgb

Pattern mirrors PR #595 test_v9_5_xrp_late_band_AB_blend.py.
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_5_eth_blend_up_low import (
    evaluate_v9_5_eth_blend_up_low,
)
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# ── Surface factory ────────────────────────────────────────────────────────

def _make_surface(**overrides) -> FullDataSurface:
    """Default ETH surface for v9_5_eth_blend_up_low tests.

    asset=ETH, eval_offset=120 (in band [60, 240]),
    probability_lgb_v9_5_eth=0.80 (inside the UP-LOW band [0.78, 0.82)).
    """
    defaults = dict(
        asset="ETH", timescale="5m",
        window_ts=1779000000,
        eval_offset=120, assembled_at=time.time(),
        current_price=2500.0, open_price=2495.0,
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
        probability_lgb_v9_5_eth=0.80,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ── Gate-params helpers ────────────────────────────────────────────────────

_BASE_PARAMS: dict[str, Any] = {
    "up_threshold": 0.78,
    "up_threshold_max": 0.82,
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
    from strategies.configs import v9_5_eth_blend_up_low
    v9_5_eth_blend_up_low._consec_state.clear()
    yield
    v9_5_eth_blend_up_low._consec_state.clear()


# ── Null probability handling ──────────────────────────────────────────────

class TestNullProbabilityHandling:
    def test_model_not_loaded_skips_cleanly(self):
        """When the snapshot has no probability_lgb_v9_5_eth, the strategy
        SKIPs with a clear reason — no crash."""
        surface = _make_surface(probability_lgb_v9_5_eth=None)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_5_eth_model_not_loaded"
        assert d.strategy_id == "v9_5_eth_blend_up_low"


# ── Defensive asset guard ──────────────────────────────────────────────────

class TestAssetGuard:
    def test_btc_surface_skips_even_with_in_band_prob(self):
        surface = _make_surface(asset="BTC", probability_lgb_v9_5_eth=0.80)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_xrp_surface_skips(self):
        surface = _make_surface(asset="XRP", probability_lgb_v9_5_eth=0.80)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_asset_check_runs_before_probability_check(self):
        surface = _make_surface(asset="BTC", probability_lgb_v9_5_eth=None)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"


# ── Eval-offset band ───────────────────────────────────────────────────────

class TestEvalOffsetBand:
    def test_below_min_skips(self):
        surface = _make_surface(eval_offset=59)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_above_max_skips(self):
        surface = _make_surface(eval_offset=241)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_at_min_60_fires(self):
        surface = _make_surface(eval_offset=60, probability_lgb_v9_5_eth=0.80)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_at_max_240_fires(self):
        surface = _make_surface(eval_offset=240, probability_lgb_v9_5_eth=0.80)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_none_eval_offset_skips(self):
        surface = _make_surface(eval_offset=None)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


# ── UP band gating (0.78 <= p < 0.82) ──────────────────────────────────────

class TestUpBand:
    def test_at_lower_bound_0_78_fires(self):
        """Lower bound is inclusive — exactly 0.78 should fire UP."""
        surface = _make_surface(probability_lgb_v9_5_eth=0.78)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.strategy_id == "v9_5_eth_blend_up_low"
        assert d.entry_reason == "v9_5_eth_blend_up_low_pass"

    def test_just_above_lower_bound_fires(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.79)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_just_below_lower_bound_skips(self):
        """0.7799 < 0.78 — below the band lower bound."""
        surface = _make_surface(probability_lgb_v9_5_eth=0.7799)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_conviction_band"

    def test_at_upper_bound_0_82_skips(self):
        """Upper bound is EXCLUSIVE — exactly 0.82 routes to up_mid, not here."""
        surface = _make_surface(probability_lgb_v9_5_eth=0.82)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_conviction_band"

    def test_above_upper_bound_skips(self):
        """0.85 is in up_mid's band, not up_low's."""
        surface = _make_surface(probability_lgb_v9_5_eth=0.85)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_conviction_band"

    def test_neutral_skips(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.50)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_conviction_band"


# ── No DOWN direction (UP-only ladder tier) ────────────────────────────────

class TestNoDownDirection:
    """UP-LOW tier is UP-only by design. DOWN signals must NEVER fire here."""

    def test_strong_down_skips(self):
        """Even very strong DOWN (p=0.05) must SKIP — UP-only strategy."""
        surface = _make_surface(probability_lgb_v9_5_eth=0.05)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_conviction_band"

    def test_in_down_low_band_skips(self):
        """A prob inside down_low's band (0.20) must SKIP for the UP tier."""
        surface = _make_surface(probability_lgb_v9_5_eth=0.20)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_conviction_band"

    def test_runtime_override_cannot_force_down(self):
        """Even injecting down_threshold via runtime override cannot make
        this UP-only strategy fire DOWN."""
        surface = _make_surface(probability_lgb_v9_5_eth=0.05)
        with _params(down_threshold=0.10, down_threshold_min=-1.0):
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        # The skip is due to band-check failure, not "wrong" direction logic.
        # The override is silently ignored — there's no DOWN codepath at all.
        assert d.skip_reason == "outside_conviction_band"


# ── Runtime override ───────────────────────────────────────────────────────

class TestRuntimeOverride:
    def test_runtime_override_narrows_band(self):
        """Narrowing the band can exclude a previously-firing probability."""
        surface = _make_surface(probability_lgb_v9_5_eth=0.79)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "TRADE"
        # Narrow lower bound to 0.80 — 0.79 now out of band.
        with _params(up_threshold=0.80):
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_conviction_band"

    def test_runtime_override_widens_band(self):
        """Widening the band can include a previously-skipping probability."""
        surface = _make_surface(probability_lgb_v9_5_eth=0.83)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        # Push upper bound to 0.90 — 0.83 now in band.
        with _params(up_threshold_max=0.90):
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_runtime_override_narrows_eval_band(self):
        surface = _make_surface(eval_offset=100, probability_lgb_v9_5_eth=0.80)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "TRADE"
        with _params(eval_offset_min=120):
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


# ── Metadata shape ─────────────────────────────────────────────────────────

class TestMetadataShape:
    def test_metadata_contains_probability_on_trade(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.80)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "TRADE"
        assert d.metadata["probability_lgb_v9_5_eth"] == pytest.approx(0.80)
        assert d.metadata["up_threshold"] == pytest.approx(0.78)
        assert d.metadata["up_threshold_max"] == pytest.approx(0.82)
        assert d.metadata["eval_offset_min"] == 60
        assert d.metadata["eval_offset_max"] == 240
        assert d.metadata["asset"] == "ETH"
        assert d.metadata["strategy_id"] == "v9_5_eth_blend_up_low"
        assert d.metadata["strategy_version"] == "1.0.0"

    def test_metadata_contains_sizing_on_trade(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.80)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "TRADE"
        assert d.metadata["entry_cap"] == pytest.approx(0.93)
        assert d.metadata["collateral_pct"] == pytest.approx(0.025)
        assert d.metadata["gtc_cap"] == pytest.approx(0.96)

    def test_confidence_score_uses_distance_from_neutral(self):
        # p=0.80 -> |0.80 - 0.5| * 2 = 0.60 -> HIGH (>= 0.40)
        surface = _make_surface(probability_lgb_v9_5_eth=0.80)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "TRADE"
        assert d.confidence == "HIGH"
        assert d.confidence_score == pytest.approx(0.60)


# ── Consecutive ticks ──────────────────────────────────────────────────────

class TestConsecutiveTicks:
    def test_default_one_tick_fires_immediately(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.80)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.metadata["consec_tick_count"] == 1
        assert d.metadata["min_consecutive_pass_ticks"] == 1

    def test_runtime_override_requires_two_ticks(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.80)
        with _params(min_consecutive_pass_ticks=2):
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        assert "awaiting_consec_ticks" in (d.skip_reason or "")
        with _params(min_consecutive_pass_ticks=2):
            d2 = evaluate_v9_5_eth_blend_up_low(surface)
        assert d2.action == "TRADE"
        assert d2.direction == "UP"


# ── Cross-strategy independence ────────────────────────────────────────────

class TestCrossStrategyIndependence:
    """Each ladder tier owns its module-local _consec_state and must NOT
    share with sibling tiers or with v9_5_eth_raw_lgb. This is critical
    for the ladder design — overlapping conviction bands could otherwise
    pollute each other's consec counters."""

    def test_consec_state_isolated_from_up_mid(self):
        from strategies.configs import v9_5_eth_blend_up_low as up_low
        from strategies.configs import v9_5_eth_blend_up_mid as up_mid

        up_low._consec_state.clear()
        up_mid._consec_state.clear()
        assert up_low._consec_state is not up_mid._consec_state

        surface = _make_surface(probability_lgb_v9_5_eth=0.80)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "TRADE"
        assert len(up_low._consec_state) >= 1
        assert len(up_mid._consec_state) == 0

    def test_consec_state_isolated_from_up_high(self):
        from strategies.configs import v9_5_eth_blend_up_low as up_low
        from strategies.configs import v9_5_eth_blend_up_high as up_high

        up_low._consec_state.clear()
        up_high._consec_state.clear()
        assert up_low._consec_state is not up_high._consec_state

    def test_consec_state_isolated_from_down_low(self):
        from strategies.configs import v9_5_eth_blend_up_low as up_low
        from strategies.configs import v9_5_eth_blend_down_low as down_low

        up_low._consec_state.clear()
        down_low._consec_state.clear()
        assert up_low._consec_state is not down_low._consec_state

    def test_consec_state_isolated_from_v9_5_eth_raw_lgb(self):
        from strategies.configs import v9_5_eth_blend_up_low as up_low
        from strategies.configs import v9_5_eth_raw_lgb as raw_lgb

        up_low._consec_state.clear()
        raw_lgb._consec_state.clear()
        assert up_low._consec_state is not raw_lgb._consec_state

        surface = _make_surface(probability_lgb_v9_5_eth=0.80)
        with _params():
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "TRADE"
        assert len(up_low._consec_state) >= 1
        assert len(raw_lgb._consec_state) == 0
