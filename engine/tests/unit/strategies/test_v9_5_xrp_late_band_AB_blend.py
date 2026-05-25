"""Unit tests for v9_5_xrp_late_band_AB_blend GHOST strategy.

Coverage:
- model-not-loaded SKIP (v9_5_xrp_model_not_loaded) — None probability
- defensive asset guard (SKIP on non-XRP surface — wrong_asset)
- eval_offset outside band [90, 150] (outside_eval_band)
- UP threshold gating: fires at 0.91, fires above, just below skips
- DOWN signals NEVER fire (UP-only — no usable DOWN band per RDS note #629)
- gate_params runtime override respected
- Metadata shape on TRADE includes probability + threshold + strategy_id
- Consecutive ticks (min_consecutive_pass_ticks) gating
- Cross-strategy independence: module-local _consec_state doesn't leak
  into v9_5_xrp_raw_lgb

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

from strategies.configs.v9_5_xrp_late_band_AB_blend import (
    evaluate_v9_5_xrp_late_band_AB_blend,
)
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# ── Surface factory ────────────────────────────────────────────────────────

def _make_surface(**overrides) -> FullDataSurface:
    """Default surface for v9_5_xrp_late_band_AB_blend tests.

    asset=XRP, eval_offset=120 (in band [90, 150]), hour_utc=12,
    probability_lgb_v9_5_xrp=0.93 (above the 0.91 UP threshold).
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
        hour_utc=12, seconds_to_close=120,
        probability_lgb_v9_5_xrp=0.93,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ── Gate-params helpers ────────────────────────────────────────────────────

_BASE_PARAMS: dict[str, Any] = {
    "up_threshold": 0.91,
    "eval_offset_min": 90,
    "eval_offset_max": 150,
    "min_consecutive_pass_ticks": 1,
    "expected_asset": "XRP",
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
    from strategies.configs import v9_5_xrp_late_band_AB_blend
    v9_5_xrp_late_band_AB_blend._consec_state.clear()
    yield
    v9_5_xrp_late_band_AB_blend._consec_state.clear()


# ── Null probability handling ──────────────────────────────────────────────

class TestNullProbabilityHandling:
    def test_model_not_loaded_skips_cleanly(self):
        """When the snapshot has no probability_lgb_v9_5_xrp, the strategy
        SKIPs with a clear reason — no crash."""
        surface = _make_surface(probability_lgb_v9_5_xrp=None)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_5_xrp_model_not_loaded"
        assert d.strategy_id == "v9_5_xrp_late_band_AB_blend"


# ── Defensive asset guard ──────────────────────────────────────────────────

class TestAssetGuard:
    def test_btc_surface_skips_even_with_high_prob(self):
        """XRP strategy refuses to fire on a BTC surface — defensive guard."""
        surface = _make_surface(asset="BTC", probability_lgb_v9_5_xrp=0.97)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_eth_surface_skips(self):
        surface = _make_surface(asset="ETH", probability_lgb_v9_5_xrp=0.97)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_asset_check_runs_before_probability_check(self):
        """Asset check runs first — non-XRP with None prob still wrong_asset."""
        surface = _make_surface(asset="BTC", probability_lgb_v9_5_xrp=None)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"


# ── Eval-offset band ───────────────────────────────────────────────────────

class TestEvalOffsetBand:
    def test_below_min_skips(self):
        """eval_offset=89 is below the late-band min of 90."""
        surface = _make_surface(eval_offset=89)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_above_max_skips(self):
        """eval_offset=151 is above the late-band max of 150."""
        surface = _make_surface(eval_offset=151)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_at_min_90_fires(self):
        surface = _make_surface(eval_offset=90, probability_lgb_v9_5_xrp=0.93)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_at_max_150_fires(self):
        surface = _make_surface(eval_offset=150, probability_lgb_v9_5_xrp=0.93)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_none_eval_offset_skips(self):
        surface = _make_surface(eval_offset=None)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_well_below_band_skips(self):
        """eval_offset=240 (e.g. early window) is way out of the late band."""
        surface = _make_surface(eval_offset=240)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


# ── UP threshold gating ────────────────────────────────────────────────────

class TestUpThreshold:
    def test_up_fires_at_audit_threshold_0_91(self):
        """Exactly at audit threshold 0.91 — fires UP."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.91)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.strategy_id == "v9_5_xrp_late_band_AB_blend"
        assert d.entry_reason == "v9_5_xrp_late_band_AB_blend_pass"

    def test_up_fires_well_above_threshold(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.98)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_just_below_threshold_skips(self):
        """0.909 < 0.91 — below the UP threshold."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.909)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_neutral_skips(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.50)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── No DOWN direction ──────────────────────────────────────────────────────

class TestNoDownDirection:
    """UP-only strategy. DOWN signals should NEVER fire — RDS note #629
    explicit: 'no usable DOWN band yet'."""

    def test_strong_down_skips(self):
        """Even very strong DOWN (p=0.05) must SKIP — UP-only strategy."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.05)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_moderate_down_skips(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.20)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_runtime_override_cannot_force_down(self):
        """Even runtime overrides can't make this fire DOWN — UP-only logic."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.05)
        with _params(down_threshold=0.10):
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── Runtime override ───────────────────────────────────────────────────────

class TestRuntimeOverride:
    def test_runtime_override_raises_up_threshold(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.93)
        # Default 0.91 — TRADE.
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "TRADE"
        # Raise to 0.95 — SKIP.
        with _params(up_threshold=0.95):
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_runtime_override_lowers_up_threshold(self):
        """Lowering threshold lets a previously-skipping prob through."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.85)
        # Default 0.91 — SKIP.
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "SKIP"
        # Lower to 0.80 — TRADE.
        with _params(up_threshold=0.80):
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_runtime_override_narrows_eval_band(self):
        surface = _make_surface(eval_offset=100, probability_lgb_v9_5_xrp=0.93)
        # Default band [90, 150] — TRADE.
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "TRADE"
        # Narrow to [120, 150] — 100 now out of band.
        with _params(eval_offset_min=120):
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


# ── Metadata shape ─────────────────────────────────────────────────────────

class TestMetadataShape:
    def test_metadata_contains_probability_on_trade(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.93)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "TRADE"
        assert d.metadata["probability_lgb_v9_5_xrp"] == pytest.approx(0.93)
        assert d.metadata["up_threshold"] == pytest.approx(0.91)
        assert d.metadata["eval_offset_min"] == 90
        assert d.metadata["eval_offset_max"] == 150
        assert d.metadata["asset"] == "XRP"
        assert d.metadata["strategy_id"] == "v9_5_xrp_late_band_AB_blend"
        assert d.metadata["strategy_version"] == "1.0.0"

    def test_metadata_contains_sizing_on_trade(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.93)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "TRADE"
        assert d.metadata["entry_cap"] == pytest.approx(0.93)
        assert d.metadata["collateral_pct"] == pytest.approx(0.025)
        assert d.metadata["gtc_cap"] == pytest.approx(0.93)

    def test_confidence_score_uses_distance_from_neutral(self):
        # p=0.93 -> |0.93 - 0.5| * 2 = 0.86 -> HIGH (>= 0.40)
        surface = _make_surface(probability_lgb_v9_5_xrp=0.93)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "TRADE"
        assert d.confidence == "HIGH"
        assert d.confidence_score == pytest.approx(0.86)

    def test_metadata_includes_eval_offset_on_trade(self):
        surface = _make_surface(eval_offset=120, probability_lgb_v9_5_xrp=0.93)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "TRADE"
        assert d.metadata["eval_offset"] == 120


# ── Consecutive ticks ──────────────────────────────────────────────────────

class TestConsecutiveTicks:
    def test_default_one_tick_fires_immediately(self):
        """min_consecutive_pass_ticks=1 (default) → one qualifying tick fires."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.93)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.metadata["consec_tick_count"] == 1
        assert d.metadata["min_consecutive_pass_ticks"] == 1

    def test_runtime_override_requires_two_ticks(self):
        """Runtime override min_consecutive_pass_ticks=2 → first tick SKIPs."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.93)
        with _params(min_consecutive_pass_ticks=2):
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "SKIP"
        assert "awaiting_consec_ticks" in (d.skip_reason or "")
        # Second consecutive tick should TRADE.
        with _params(min_consecutive_pass_ticks=2):
            d2 = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d2.action == "TRADE"
        assert d2.direction == "UP"


# ── Cross-strategy independence ────────────────────────────────────────────

class TestCrossStrategyIndependence:
    """Module-local _consec_state must NOT leak to sibling strategies."""

    def test_consec_state_isolated_from_v9_5_xrp_blend(self):
        """Mutating v9_5_xrp_late_band_AB_blend._consec_state must NOT affect
        v9_5_xrp_blend (née v9_5_xrp_raw_lgb)._consec_state (separate
        module-locals).
        """
        from strategies.configs import v9_5_xrp_late_band_AB_blend as late_band
        from strategies.configs import v9_5_xrp_blend as blend

        # Sanity: clear both.
        late_band._consec_state.clear()
        blend._consec_state.clear()
        assert late_band._consec_state is not blend._consec_state

        # Fire this strategy — populates ITS state only.
        surface = _make_surface(probability_lgb_v9_5_xrp=0.93)
        with _params():
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "TRADE"
        assert len(late_band._consec_state) >= 1
        # blend's state remains untouched.
        assert len(blend._consec_state) == 0


# ── Direction-aware fill-band gate (RDS note #664, 2026-05-25) ─────────────


class TestDirectionAwareFillBandGate:
    """entry_floor_up=0.60 blocks UP fires at fill < 0.60.
    entry_cap_down=0.90 blocks DOWN fires at fill >= 0.90.
    XRP late-band is UP-only so only the UP gate applies in practice;
    the DOWN gate is a no-op but must remain permissive (default 1.0).
    """

    def test_up_fill_055_below_floor_skips(self):
        """UP fire at fill=0.55 < 0.60 — SKIP."""
        surface = _make_surface(
            probability_lgb_v9_5_xrp=0.93,
            clob_implied_up=0.55,
        )
        with _params(entry_floor_up=0.60, entry_cap_down=0.90):
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason is not None
        assert "fill_below_up_floor" in d.skip_reason
        assert "0.550" in d.skip_reason
        assert "0.600" in d.skip_reason

    def test_up_fill_at_060_exact_trades(self):
        """UP fire at fill=0.60 exactly — boundary inclusive, TRADE."""
        surface = _make_surface(
            probability_lgb_v9_5_xrp=0.93,
            clob_implied_up=0.60,
        )
        with _params(entry_floor_up=0.60, entry_cap_down=0.90):
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_up_fill_above_floor_trades(self):
        """UP fire at fill=0.65 — inside safe zone, TRADE."""
        surface = _make_surface(
            probability_lgb_v9_5_xrp=0.93,
            clob_implied_up=0.65,
        )
        with _params(entry_floor_up=0.60, entry_cap_down=0.90):
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_up_fill_092_above_090_still_trades(self):
        """UP at fill=0.92 must TRADE — 100% WR zone; direction-aware gate does NOT block."""
        surface = _make_surface(
            probability_lgb_v9_5_xrp=0.93,
            clob_implied_up=0.92,
        )
        with _params(entry_floor_up=0.60, entry_cap_down=0.90):
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_none_clob_implied_up_bypasses_gate(self):
        """When clob_implied_up is None, fill gate is skipped — TRADE."""
        surface = _make_surface(
            probability_lgb_v9_5_xrp=0.93,
            clob_implied_up=None,
        )
        with _params(entry_floor_up=0.60, entry_cap_down=0.90):
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_default_permissive_params_allow_all_fills(self):
        """Without explicit gate params (defaults: 0.0/1.0), all fills pass."""
        surface = _make_surface(
            probability_lgb_v9_5_xrp=0.93,
            clob_implied_up=0.10,
        )
        with _params():  # no entry_floor_up / entry_cap_down
            d = evaluate_v9_5_xrp_late_band_AB_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
