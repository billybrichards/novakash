"""Unit tests for v9_5_eth_pure_lgb GHOST strategy.

Coverage:
- model-not-loaded SKIP (v9_5_eth_pure_model_not_loaded) — None probability
- defensive asset guard (SKIP on non-ETH surface — wrong_asset)
- eval_offset outside band [60, 210] (outside_eval_band)
- UP threshold gating: fires at 0.915, fires above, just below skips
- DOWN threshold gating: fires at 0.095, fires below, just above skips
- gate_params runtime override respected
- Metadata shape on TRADE includes probability + thresholds + strategy_id
- Consecutive ticks (min_consecutive_pass_ticks) gating
- Reads PURE column (probability_lgb_v9_5_eth_pure), NOT the blended one
  (probability_lgb_v9_5_eth) — the whole point of this strategy
- Cross-strategy independence: module-local _consec_state doesn't leak
  into v9_5_eth_raw_lgb / v9_2_eth_late_band_AB_blend

Test strategy: synthetic _make_surface() fixture; uses _gp.set_active /
_gp.reset_active (same pattern as test_v9_2_eth_late_band_AB_blend.py).
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_5_eth_pure_lgb import (
    evaluate_v9_5_eth_pure_lgb,
)
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# ── Surface factory ────────────────────────────────────────────────────────

def _make_surface(**overrides) -> FullDataSurface:
    """Default surface for v9_5_eth_pure_lgb tests.

    asset=ETH, eval_offset=120 (in band [60, 210]), hour_utc=12,
    probability_lgb_v9_5_eth_pure=0.93 (above the 0.915 UP threshold).
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
        probability_lgb_v9_5_eth_pure=0.93,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ── Gate-params helpers ────────────────────────────────────────────────────

_BASE_PARAMS: dict[str, Any] = {
    "up_threshold": 0.915,
    "down_threshold": 0.095,
    "eval_offset_min": 60,
    "eval_offset_max": 210,
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
    from strategies.configs import v9_5_eth_pure_lgb
    v9_5_eth_pure_lgb._consec_state.clear()
    yield
    v9_5_eth_pure_lgb._consec_state.clear()


# ── Null probability handling ──────────────────────────────────────────────

class TestNullProbabilityHandling:
    def test_model_not_loaded_skips_cleanly(self):
        """When the snapshot has no probability_lgb_v9_5_eth_pure, the strategy
        SKIPs with a clear reason — no crash."""
        surface = _make_surface(probability_lgb_v9_5_eth_pure=None)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_5_eth_pure_model_not_loaded"
        assert d.strategy_id == "v9_5_eth_pure_lgb"


# ── PURE vs blend column ───────────────────────────────────────────────────

class TestReadsPureColumn:
    """The whole point of this strategy: reads PURE column, NOT blend."""

    def test_blend_high_pure_none_skips(self):
        """probability_lgb_v9_5_eth (blend) high but PURE None → SKIP.

        This proves the strategy reads the PURE column. If it accidentally
        read the blend column, it would fire here.
        """
        surface = _make_surface(
            probability_lgb_v9_5_eth_pure=None,
            probability_lgb_v9_5_eth=0.99,  # blend value high (irrelevant)
        )
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_5_eth_pure_model_not_loaded"

    def test_pure_high_blend_none_fires(self):
        """PURE high and blend None → TRADE.

        Confirms the strategy doesn't *require* the blend column either —
        it only cares about the PURE column.
        """
        surface = _make_surface(
            probability_lgb_v9_5_eth_pure=0.95,
            probability_lgb_v9_5_eth=None,
        )
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"


# ── Defensive asset guard ──────────────────────────────────────────────────

class TestAssetGuard:
    def test_btc_surface_skips_even_with_high_prob(self):
        """ETH strategy refuses to fire on a BTC surface — defensive guard."""
        surface = _make_surface(asset="BTC", probability_lgb_v9_5_eth_pure=0.97)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_xrp_surface_skips(self):
        surface = _make_surface(asset="XRP", probability_lgb_v9_5_eth_pure=0.97)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_asset_check_runs_before_probability_check(self):
        """Asset check runs first — non-ETH with None prob still wrong_asset."""
        surface = _make_surface(asset="BTC", probability_lgb_v9_5_eth_pure=None)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"


# ── Eval-offset band ───────────────────────────────────────────────────────

class TestEvalOffsetBand:
    def test_below_min_skips(self):
        """eval_offset=59 is below the band min of 60."""
        surface = _make_surface(eval_offset=59)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason.startswith("outside_eval_band")

    def test_above_max_skips(self):
        """eval_offset=211 is above the band max of 210 (drops weak Δ=240s tail)."""
        surface = _make_surface(eval_offset=211)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason.startswith("outside_eval_band")

    def test_at_min_60_fires(self):
        surface = _make_surface(eval_offset=60, probability_lgb_v9_5_eth_pure=0.95)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_at_max_210_fires(self):
        surface = _make_surface(eval_offset=210, probability_lgb_v9_5_eth_pure=0.95)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_240s_tail_skipped(self):
        """eval_offset=240 (the weak tail dropped by max=210) — SKIPs."""
        surface = _make_surface(eval_offset=240, probability_lgb_v9_5_eth_pure=0.95)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason.startswith("outside_eval_band")

    def test_none_eval_offset_skips(self):
        surface = _make_surface(eval_offset=None)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason.startswith("outside_eval_band")


# ── UP threshold gating ────────────────────────────────────────────────────

class TestUpThreshold:
    def test_up_fires_at_cv_threshold_0_915(self):
        """Exactly at CV threshold 0.915 — fires UP."""
        surface = _make_surface(probability_lgb_v9_5_eth_pure=0.915)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.strategy_id == "v9_5_eth_pure_lgb"
        assert d.entry_reason == "v9_5_eth_pure_lgb_pass"

    def test_up_fires_well_above_threshold(self):
        surface = _make_surface(probability_lgb_v9_5_eth_pure=0.98)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_just_below_threshold_skips(self):
        """0.914 < 0.915 — below the UP threshold (and above the DOWN one)."""
        surface = _make_surface(probability_lgb_v9_5_eth_pure=0.914)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── DOWN threshold gating ──────────────────────────────────────────────────

class TestDownThreshold:
    def test_down_fires_at_cv_threshold_0_095(self):
        """Exactly at CV threshold 0.095 — fires DOWN."""
        surface = _make_surface(probability_lgb_v9_5_eth_pure=0.095)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_down_fires_well_below_threshold(self):
        surface = _make_surface(probability_lgb_v9_5_eth_pure=0.02)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_just_above_down_skips(self):
        """0.096 > 0.095 — above the DOWN threshold (and below UP)."""
        surface = _make_surface(probability_lgb_v9_5_eth_pure=0.096)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_neutral_skips(self):
        """p=0.50 sits in the dead zone — SKIPs."""
        surface = _make_surface(probability_lgb_v9_5_eth_pure=0.50)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── Runtime override ───────────────────────────────────────────────────────

class TestRuntimeOverride:
    def test_runtime_override_raises_up_threshold(self):
        surface = _make_surface(probability_lgb_v9_5_eth_pure=0.93)
        # Default 0.915 — TRADE.
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"
        # Raise to 0.98 — SKIP.
        with _params(up_threshold=0.98):
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_runtime_override_lowers_down_threshold(self):
        """Lowering DOWN threshold (toward 0) makes a borderline DOWN skip."""
        surface = _make_surface(probability_lgb_v9_5_eth_pure=0.09)
        # Default 0.095 — TRADE DOWN (0.09 <= 0.095).
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"
        # Lower to 0.05 — 0.09 > 0.05 → SKIP.
        with _params(down_threshold=0.05):
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_runtime_override_narrows_eval_band(self):
        surface = _make_surface(eval_offset=200, probability_lgb_v9_5_eth_pure=0.95)
        # Default band [60, 210] — TRADE.
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"
        # Narrow to [60, 120] — 200 now out of band.
        with _params(eval_offset_max=120):
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason.startswith("outside_eval_band")


# ── Metadata shape ─────────────────────────────────────────────────────────

class TestMetadataShape:
    def test_metadata_contains_probability_on_trade(self):
        surface = _make_surface(probability_lgb_v9_5_eth_pure=0.93)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.metadata["probability_lgb_v9_5_eth_pure"] == pytest.approx(0.93)
        assert d.metadata["up_threshold"] == pytest.approx(0.915)
        assert d.metadata["down_threshold"] == pytest.approx(0.095)
        assert d.metadata["eval_offset_min"] == 60
        assert d.metadata["eval_offset_max"] == 210
        assert d.metadata["asset"] == "ETH"
        assert d.metadata["strategy_id"] == "v9_5_eth_pure_lgb"
        assert d.metadata["strategy_version"] == "1.0.0"

    def test_metadata_contains_sizing_on_trade(self):
        surface = _make_surface(probability_lgb_v9_5_eth_pure=0.93)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.metadata["entry_cap"] == pytest.approx(0.93)
        assert d.metadata["collateral_pct"] == pytest.approx(0.025)
        assert d.metadata["gtc_cap"] == pytest.approx(0.96)

    def test_confidence_score_uses_distance_from_neutral(self):
        # p=0.93 -> |0.93 - 0.5| * 2 = 0.86 -> HIGH (>= 0.40)
        surface = _make_surface(probability_lgb_v9_5_eth_pure=0.93)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.confidence == "HIGH"
        assert d.confidence_score == pytest.approx(0.86)

    def test_metadata_includes_eval_offset_on_trade(self):
        surface = _make_surface(eval_offset=120, probability_lgb_v9_5_eth_pure=0.93)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.metadata["eval_offset"] == 120


# ── Consecutive ticks ──────────────────────────────────────────────────────

class TestConsecutiveTicks:
    def test_default_one_tick_fires_immediately(self):
        """min_consecutive_pass_ticks=1 (default) → one qualifying tick fires."""
        surface = _make_surface(probability_lgb_v9_5_eth_pure=0.93)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.metadata["consec_tick_count"] == 1
        assert d.metadata["min_consecutive_pass_ticks"] == 1

    def test_runtime_override_requires_two_ticks(self):
        """Runtime override min_consecutive_pass_ticks=2 → first tick SKIPs."""
        surface = _make_surface(probability_lgb_v9_5_eth_pure=0.93)
        with _params(min_consecutive_pass_ticks=2):
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "SKIP"
        assert "awaiting_consec_ticks" in (d.skip_reason or "")
        # Second consecutive tick should TRADE.
        with _params(min_consecutive_pass_ticks=2):
            d2 = evaluate_v9_5_eth_pure_lgb(surface)
        assert d2.action == "TRADE"
        assert d2.direction == "UP"


# ── Cross-strategy independence ────────────────────────────────────────────

class TestCrossStrategyIndependence:
    """Module-local _consec_state must NOT leak to sibling strategies."""

    def test_consec_state_isolated_from_v9_5_eth_blend(self):
        """Mutating v9_5_eth_pure_lgb._consec_state must NOT affect
        v9_5_eth_blend (the blend sibling, née v9_5_eth_raw_lgb)._consec_state.
        """
        from strategies.configs import v9_5_eth_pure_lgb as pure
        from strategies.configs import v9_5_eth_blend as blend

        # Sanity: clear both.
        pure._consec_state.clear()
        blend._consec_state.clear()
        assert pure._consec_state is not blend._consec_state

        # Fire this strategy — populates ITS state only.
        surface = _make_surface(probability_lgb_v9_5_eth_pure=0.95)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"
        assert len(pure._consec_state) >= 1
        # blend's state remains untouched.
        assert len(blend._consec_state) == 0

    def test_consec_state_isolated_from_v9_2_eth_late_band_AB_blend(self):
        """Independence from v9_2_eth_late_band_AB_blend (a different op-point
        ETH sibling)."""
        from strategies.configs import v9_5_eth_pure_lgb as pure
        from strategies.configs import v9_2_eth_late_band_AB_blend as late_band

        pure._consec_state.clear()
        late_band._consec_state.clear()
        assert pure._consec_state is not late_band._consec_state

        surface = _make_surface(probability_lgb_v9_5_eth_pure=0.95)
        with _params():
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"
        assert len(pure._consec_state) >= 1
        assert len(late_band._consec_state) == 0
