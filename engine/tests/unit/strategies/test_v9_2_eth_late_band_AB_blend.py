"""Unit tests for v9_2_eth_late_band_AB GHOST strategy.

Coverage:
- model-not-loaded SKIP (v9_2_eth_model_not_loaded) — None probability
- defensive asset guard (SKIP on non-ETH surface — wrong_asset)
- eval_offset outside band [60, 90] (outside_eval_band)
- UP threshold gating: fires at 0.82, fires above, just below skips
- DOWN signals NEVER fire (UP-only strategy per RDS note #627)
- gate_params runtime override respected
- Metadata shape on TRADE includes probability + threshold + strategy_id
- Consecutive ticks (min_consecutive_pass_ticks) gating
- Cross-strategy independence: module-local _consec_state doesn't leak
  into v9_2_eth_raw_lgb

Test strategy: synthetic _make_surface() fixture; uses _gp.set_active /
_gp.reset_active (same pattern as test_v9_2_v12_AND_ghost.py).
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_2_eth_late_band_AB import (
    evaluate_v9_2_eth_late_band_AB,
)
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# ── Surface factory ────────────────────────────────────────────────────────

def _make_surface(**overrides) -> FullDataSurface:
    """Default surface for v9_2_eth_late_band_AB tests.

    asset=ETH, eval_offset=75 (in band [60, 90]), hour_utc=12,
    probability_lgb_v9_2_eth=0.85 (above the 0.82 UP threshold).
    """
    defaults = dict(
        asset="ETH", timescale="5m",
        window_ts=1713009600,
        eval_offset=75, assembled_at=time.time(),
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
        hour_utc=12, seconds_to_close=75,
        probability_lgb_v9_2_eth=0.85,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ── Gate-params helpers ────────────────────────────────────────────────────

_BASE_PARAMS: dict[str, Any] = {
    "up_threshold": 0.82,
    "eval_offset_min": 60,
    "eval_offset_max": 90,
    "min_consecutive_pass_ticks": 1,
    "expected_asset": "ETH",
    "entry_cap": 0.93,
    "collateral_pct": 0.025,
    "gtc_cap": 0.93,
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
    from strategies.configs import v9_2_eth_late_band_AB
    v9_2_eth_late_band_AB._consec_state.clear()
    yield
    v9_2_eth_late_band_AB._consec_state.clear()


# ── Null probability handling ──────────────────────────────────────────────

class TestNullProbabilityHandling:
    def test_model_not_loaded_skips_cleanly(self):
        """When the snapshot has no probability_lgb_v9_2_eth, the strategy
        SKIPs with a clear reason — no crash."""
        surface = _make_surface(probability_lgb_v9_2_eth=None)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_2_eth_model_not_loaded"
        assert d.strategy_id == "v9_2_eth_late_band_AB"


# ── Defensive asset guard ──────────────────────────────────────────────────

class TestAssetGuard:
    def test_btc_surface_skips_even_with_high_prob(self):
        """ETH strategy refuses to fire on a BTC surface — defensive guard."""
        surface = _make_surface(asset="BTC", probability_lgb_v9_2_eth=0.95)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_xrp_surface_skips(self):
        surface = _make_surface(asset="XRP", probability_lgb_v9_2_eth=0.95)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_asset_check_runs_before_probability_check(self):
        """Asset check runs first — non-ETH with None prob still wrong_asset."""
        surface = _make_surface(asset="BTC", probability_lgb_v9_2_eth=None)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"


# ── Eval-offset band ───────────────────────────────────────────────────────

class TestEvalOffsetBand:
    def test_below_min_skips(self):
        """eval_offset=59 is below the late-band min of 60."""
        surface = _make_surface(eval_offset=59)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_above_max_skips(self):
        """eval_offset=91 is above the late-band max of 90."""
        surface = _make_surface(eval_offset=91)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_at_min_60_fires(self):
        surface = _make_surface(eval_offset=60, probability_lgb_v9_2_eth=0.85)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_at_max_90_fires(self):
        surface = _make_surface(eval_offset=90, probability_lgb_v9_2_eth=0.85)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_none_eval_offset_skips(self):
        surface = _make_surface(eval_offset=None)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_well_below_band_skips(self):
        """eval_offset=200 (e.g. early window) is way out of the late band."""
        surface = _make_surface(eval_offset=200)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


# ── UP threshold gating ────────────────────────────────────────────────────

class TestUpThreshold:
    def test_up_fires_at_audit_threshold_0_82(self):
        """Exactly at audit threshold 0.82 — fires UP."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.82)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.strategy_id == "v9_2_eth_late_band_AB"
        assert d.entry_reason == "v9_2_eth_late_band_AB_pass"

    def test_up_fires_well_above_threshold(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.95)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_just_below_threshold_skips(self):
        """0.819 < 0.82 — below the UP threshold."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.819)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_neutral_skips(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.50)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── No DOWN direction ──────────────────────────────────────────────────────

class TestNoDownDirection:
    """UP-only strategy. DOWN signals should NEVER fire — audit n=5 too thin."""

    def test_strong_down_skips(self):
        """Even very strong DOWN (p=0.05) must SKIP — UP-only strategy."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.05)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_moderate_down_skips(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.20)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_runtime_override_cannot_force_down(self):
        """Even runtime overrides can't make this fire DOWN — UP-only logic."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.05)
        # Try to set a DOWN threshold via override — should be ignored.
        with _params(down_threshold=0.10):
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── Runtime override ───────────────────────────────────────────────────────

class TestRuntimeOverride:
    def test_runtime_override_raises_up_threshold(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.85)
        # Default 0.82 — TRADE.
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "TRADE"
        # Raise to 0.90 — SKIP.
        with _params(up_threshold=0.90):
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_runtime_override_lowers_up_threshold(self):
        """Lowering threshold lets a previously-skipping prob through."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.80)
        # Default 0.82 — SKIP.
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "SKIP"
        # Lower to 0.78 — TRADE.
        with _params(up_threshold=0.78):
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_runtime_override_narrows_eval_band(self):
        surface = _make_surface(eval_offset=70, probability_lgb_v9_2_eth=0.85)
        # Default band [60, 90] — TRADE.
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "TRADE"
        # Narrow to [80, 90] — 70 now out of band.
        with _params(eval_offset_min=80):
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


# ── Metadata shape ─────────────────────────────────────────────────────────

class TestMetadataShape:
    def test_metadata_contains_probability_on_trade(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.85)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "TRADE"
        assert d.metadata["probability_lgb_v9_2_eth"] == pytest.approx(0.85)
        assert d.metadata["up_threshold"] == pytest.approx(0.82)
        assert d.metadata["eval_offset_min"] == 60
        assert d.metadata["eval_offset_max"] == 90
        assert d.metadata["asset"] == "ETH"
        assert d.metadata["strategy_id"] == "v9_2_eth_late_band_AB"
        assert d.metadata["strategy_version"] == "1.0.0"

    def test_metadata_contains_sizing_on_trade(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.85)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "TRADE"
        assert d.metadata["entry_cap"] == pytest.approx(0.93)
        assert d.metadata["collateral_pct"] == pytest.approx(0.025)
        assert d.metadata["gtc_cap"] == pytest.approx(0.93)

    def test_confidence_score_uses_distance_from_neutral(self):
        # p=0.85 -> |0.85 - 0.5| * 2 = 0.70 -> HIGH (>= 0.40)
        surface = _make_surface(probability_lgb_v9_2_eth=0.85)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "TRADE"
        assert d.confidence == "HIGH"
        assert d.confidence_score == pytest.approx(0.70)

    def test_metadata_includes_eval_offset_on_trade(self):
        surface = _make_surface(eval_offset=75, probability_lgb_v9_2_eth=0.85)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "TRADE"
        assert d.metadata["eval_offset"] == 75


# ── Consecutive ticks ──────────────────────────────────────────────────────

class TestConsecutiveTicks:
    def test_default_one_tick_fires_immediately(self):
        """min_consecutive_pass_ticks=1 (default) → one qualifying tick fires."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.85)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.metadata["consec_tick_count"] == 1
        assert d.metadata["min_consecutive_pass_ticks"] == 1

    def test_runtime_override_requires_two_ticks(self):
        """Runtime override min_consecutive_pass_ticks=2 → first tick SKIPs."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.85)
        with _params(min_consecutive_pass_ticks=2):
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "SKIP"
        assert "awaiting_consec_ticks" in (d.skip_reason or "")
        # Second consecutive tick should TRADE.
        with _params(min_consecutive_pass_ticks=2):
            d2 = evaluate_v9_2_eth_late_band_AB(surface)
        assert d2.action == "TRADE"
        assert d2.direction == "UP"


# ── Cross-strategy independence ────────────────────────────────────────────

class TestCrossStrategyIndependence:
    """Module-local _consec_state must NOT leak to sibling strategies."""

    def test_consec_state_isolated_from_v9_2_eth_raw_lgb(self):
        """Mutating v9_2_eth_late_band_AB._consec_state must NOT affect
        v9_2_eth_raw_lgb._consec_state (they're separate module-locals).
        """
        from strategies.configs import v9_2_eth_late_band_AB as late_band
        from strategies.configs import v9_2_eth_raw_lgb as raw_lgb

        # Sanity: clear both.
        late_band._consec_state.clear()
        raw_lgb._consec_state.clear()
        assert late_band._consec_state is not raw_lgb._consec_state

        # Fire this strategy — populates ITS state only.
        surface = _make_surface(probability_lgb_v9_2_eth=0.85)
        with _params():
            d = evaluate_v9_2_eth_late_band_AB(surface)
        assert d.action == "TRADE"
        assert len(late_band._consec_state) >= 1
        # raw_lgb's state remains untouched.
        assert len(raw_lgb._consec_state) == 0
