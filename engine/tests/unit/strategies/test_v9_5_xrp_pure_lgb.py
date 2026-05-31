"""Unit tests for v9_5_xrp_pure_lgb GHOST strategy.

Coverage:
- model-not-loaded SKIP (v9_5_xrp_pure_model_not_loaded) — None probability
- defensive asset guard (SKIP on non-XRP surface — wrong_asset)
- eval_offset outside band [60, 180] (outside_eval_band)
- UP threshold gating: fires at 0.92, fires above, just below skips
- DOWN threshold gating: fires at 0.06, fires below, just above skips
- conviction_below_threshold: mid-range probability (e.g. 0.50)
- gate_params runtime override respected (up_threshold, down_threshold,
  eval_offset_min/max)
- Metadata shape on TRADE includes probability, thresholds, eval_offset,
  strategy_id, strategy_version, and window_ts
- Consecutive ticks: default min_consecutive_pass_ticks=2 gating
  (1 tick → SKIP awaiting_consec_ticks, 2 ticks → TRADE)
- Reads probability_lgb_v9_5_xrp_pure column, NOT the blended
  probability_lgb_v9_5_xrp column
- Cross-strategy independence: module-local _consec_state doesn't leak
  into v9_5_xrp_blend / v9_5_xrp_down_solo / v9_5_xrp_up_solo

Test strategy: synthetic _make_surface() fixture; uses _gp.set_active /
_gp.reset_active (same pattern as test_v9_5_xrp_down_solo.py and
test_v9_5_eth_pure_lgb.py).
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_5_xrp_pure_lgb import evaluate_v9_5_xrp_pure_lgb
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# ── Surface factory ────────────────────────────────────────────────────────

def _make_surface(**overrides) -> FullDataSurface:
    """Default surface for v9_5_xrp_pure_lgb tests.

    asset=XRP, eval_offset=120 (in band [60, 180]),
    window_ts=1713009600,
    probability_lgb_v9_5_xrp_pure=0.94 (above the 0.92 UP threshold).
    clob_down_ask=0.70 (below entry_cap_down=0.85 — DOWN fires through).
    """
    defaults = dict(
        asset="XRP", timescale="5m",
        window_ts=1713009600,
        eval_offset=120, assembled_at=time.time(),
        current_price=0.62, open_price=0.61,
        delta_binance=0.008, delta_tiingo=0.007, delta_chainlink=0.008,
        delta_pct=0.008, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=0.005,
        v2_probability_up=0.80, v2_probability_raw=0.80,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=0.80, probability_classifier=None,
        ensemble_config={"mode": "pure"},
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
        clob_up_bid=0.60, clob_up_ask=0.62, clob_down_bid=0.28,
        clob_down_ask=0.70, clob_implied_up=0.62,
        gamma_up_price=0.62, gamma_down_price=0.35,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0, cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=12, seconds_to_close=120,
        probability_lgb_v9_5_xrp_pure=0.94,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ── Gate-params helpers ────────────────────────────────────────────────────

_BASE_PARAMS: dict[str, Any] = {
    "up_threshold": 0.92,
    "down_threshold": 0.06,
    "eval_offset_min": 60,
    "eval_offset_max": 180,
    "min_consecutive_pass_ticks": 2,
    "expected_asset": "XRP",
    "entry_cap": 0.90,
    "entry_cap_up": 0.92,
    "entry_cap_down": 0.85,
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
    from strategies.configs import v9_5_xrp_pure_lgb
    v9_5_xrp_pure_lgb._consec_state.clear()
    yield
    v9_5_xrp_pure_lgb._consec_state.clear()


# ── Null probability handling ──────────────────────────────────────────────

class TestNullProbabilityHandling:
    def test_model_not_loaded_skips_cleanly(self):
        """When probability_lgb_v9_5_xrp_pure is None, strategy SKIPs with
        a clear reason — no crash."""
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=None)
        with _params():
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_5_xrp_pure_model_not_loaded"
        assert d.strategy_id == "v9_5_xrp_pure_lgb"

    def test_model_not_loaded_returns_zero_confidence(self):
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=None)
        with _params():
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.confidence_score == 0.0
        assert d.entry_cap == 0.0
        assert d.collateral_pct == 0.0


# ── PURE vs blend column ───────────────────────────────────────────────────

class TestReadsPureColumn:
    """Strategy reads PURE column (probability_lgb_v9_5_xrp_pure), NOT blend."""

    def test_blend_high_pure_none_skips(self):
        """probability_lgb_v9_5_xrp (blend) high but PURE None → SKIP.

        Proves the strategy reads the PURE column, not the blend.
        """
        surface = _make_surface(
            probability_lgb_v9_5_xrp_pure=None,
            probability_lgb_v9_5_xrp=0.99,
        )
        with _params():
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_5_xrp_pure_model_not_loaded"

    def test_pure_high_blend_none_fires(self):
        """PURE high and blend None → TRADE UP (with 2-tick gate, need 2 calls)."""
        surface = _make_surface(
            probability_lgb_v9_5_xrp_pure=0.95,
            probability_lgb_v9_5_xrp=None,
        )
        with _params():
            # First tick → awaiting
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert "awaiting_consec_ticks" in (d.skip_reason or "")
        with _params():
            # Second tick → TRADE
            d2 = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d2.action == "TRADE"
        assert d2.direction == "UP"


# ── Defensive asset guard ──────────────────────────────────────────────────

class TestAssetGuard:
    def test_btc_surface_skips_even_with_high_prob(self):
        """XRP PURE strategy refuses to fire on a BTC surface."""
        surface = _make_surface(asset="BTC", probability_lgb_v9_5_xrp_pure=0.97)
        with _params():
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_eth_surface_skips(self):
        surface = _make_surface(asset="ETH", probability_lgb_v9_5_xrp_pure=0.97)
        with _params():
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_sol_surface_skips(self):
        surface = _make_surface(asset="SOL", probability_lgb_v9_5_xrp_pure=0.97)
        with _params():
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_asset_check_runs_before_probability_check(self):
        """Asset check runs first — non-XRP with None prob still wrong_asset."""
        surface = _make_surface(asset="BTC", probability_lgb_v9_5_xrp_pure=None)
        with _params():
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_xrp_asset_passes_guard(self):
        """Explicit XRP passes the asset guard."""
        surface = _make_surface(asset="XRP", probability_lgb_v9_5_xrp_pure=0.95)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.skip_reason != "wrong_asset"


# ── Eval-offset band ───────────────────────────────────────────────────────

class TestEvalOffsetBand:
    def test_below_min_skips(self):
        """eval_offset=59 is below the band min of 60."""
        surface = _make_surface(eval_offset=59)
        with _params():
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_above_max_skips(self):
        """eval_offset=181 is above the band max of 180."""
        surface = _make_surface(eval_offset=181)
        with _params():
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_at_min_60_fires(self):
        surface = _make_surface(eval_offset=60, probability_lgb_v9_5_xrp_pure=0.95)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_at_max_180_fires(self):
        surface = _make_surface(eval_offset=180, probability_lgb_v9_5_xrp_pure=0.95)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_240s_tail_skipped(self):
        """eval_offset=240 outside the [60, 180] band — SKIPs."""
        surface = _make_surface(eval_offset=240, probability_lgb_v9_5_xrp_pure=0.95)
        with _params():
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_none_eval_offset_skips(self):
        surface = _make_surface(eval_offset=None)
        with _params():
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


# ── UP threshold gating ────────────────────────────────────────────────────

class TestUpThreshold:
    def test_up_fires_at_default_threshold_0_92(self):
        """Exactly at default UP threshold 0.92 — fires UP (with 1-tick gate)."""
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=0.92)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.strategy_id == "v9_5_xrp_pure_lgb"
        assert d.entry_reason == "v9_5_xrp_pure_lgb_pass"

    def test_up_fires_well_above_threshold(self):
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=0.98)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_just_below_up_threshold_skips(self):
        """0.919 < 0.92 (and > 0.06) — conviction_below_threshold."""
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=0.919)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── DOWN threshold gating ──────────────────────────────────────────────────

class TestDownThreshold:
    def test_down_fires_at_default_threshold_0_06(self):
        """Exactly at default DOWN threshold 0.06 — fires DOWN (with 1-tick gate)."""
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=0.06)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_down_fires_well_below_threshold(self):
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=0.02)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_just_above_down_threshold_skips(self):
        """0.061 > 0.06 (and < 0.92) — conviction_below_threshold."""
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=0.061)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_neutral_skips(self):
        """p=0.50 sits in the dead zone — conviction_below_threshold."""
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=0.50)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── Runtime override ───────────────────────────────────────────────────────

class TestRuntimeOverride:
    def test_runtime_override_raises_up_threshold(self):
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=0.93)
        # Default 0.92 — TRADE (1-tick).
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"
        # Raise to 0.98 — SKIP.
        with _params(min_consecutive_pass_ticks=1, up_threshold=0.98):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_runtime_override_lowers_down_threshold(self):
        """Lowering DOWN threshold (toward 0) makes a borderline DOWN skip."""
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=0.05)
        # Default 0.06 — TRADE DOWN (0.05 <= 0.06).
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"
        # Lower to 0.03 — 0.05 > 0.03 → SKIP.
        with _params(min_consecutive_pass_ticks=1, down_threshold=0.03):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_runtime_override_narrows_eval_band(self):
        surface = _make_surface(eval_offset=160, probability_lgb_v9_5_xrp_pure=0.95)
        # Default band [60, 180] — TRADE.
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"
        # Narrow to [60, 120] — 160 now out of band.
        with _params(min_consecutive_pass_ticks=1, eval_offset_max=120):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


# ── Metadata shape ─────────────────────────────────────────────────────────

class TestMetadataShape:
    def test_metadata_contains_probability_on_trade_up(self):
        surface = _make_surface(
            probability_lgb_v9_5_xrp_pure=0.94, window_ts=1713009600
        )
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.metadata["probability_lgb_v9_5_xrp_pure"] == pytest.approx(0.94)
        assert d.metadata["up_threshold"] == pytest.approx(0.92)
        assert d.metadata["down_threshold"] == pytest.approx(0.06)
        assert d.metadata["eval_offset_min"] == 60
        assert d.metadata["eval_offset_max"] == 180
        assert d.metadata["asset"] == "XRP"
        assert d.metadata["strategy_id"] == "v9_5_xrp_pure_lgb"
        assert d.metadata["strategy_version"] == "1.0.0"

    def test_metadata_contains_window_ts_on_trade(self):
        """window_ts must be present in metadata on TRADE — required for audit."""
        surface = _make_surface(
            probability_lgb_v9_5_xrp_pure=0.94, window_ts=1713009600
        )
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"
        assert "window_ts" in d.metadata
        assert d.metadata["window_ts"] == 1713009600

    def test_metadata_contains_window_ts_on_skip(self):
        """window_ts also present on SKIP reason (outside_eval_band example)."""
        surface = _make_surface(
            probability_lgb_v9_5_xrp_pure=0.94, window_ts=1713009600,
            eval_offset=200,
        )
        with _params():
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"
        assert d.metadata.get("window_ts") == 1713009600

    def test_metadata_contains_sizing_on_trade(self):
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=0.94)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.metadata["entry_cap"] == pytest.approx(0.90)
        assert d.metadata["collateral_pct"] == pytest.approx(0.025)
        assert d.metadata["gtc_cap"] == pytest.approx(0.96)

    def test_confidence_score_up_uses_distance_from_neutral(self):
        # p=0.94 -> |0.94 - 0.5| * 2 = 0.88 -> HIGH (>= 0.40)
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=0.94)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.confidence == "HIGH"
        assert d.confidence_score == pytest.approx(0.88)

    def test_confidence_score_down_uses_distance_from_neutral(self):
        # p=0.04 -> |0.04 - 0.5| * 2 = 0.92 -> HIGH
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=0.04)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"
        assert d.confidence == "HIGH"
        assert d.confidence_score == pytest.approx(0.92)

    def test_metadata_contains_eval_offset_on_trade(self):
        surface = _make_surface(
            eval_offset=120, probability_lgb_v9_5_xrp_pure=0.94
        )
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.metadata["eval_offset"] == 120


# ── Consecutive ticks ──────────────────────────────────────────────────────

class TestConsecutiveTicks:
    def test_default_two_ticks_first_skips(self):
        """Default min_consecutive_pass_ticks=2 — first qualifying tick SKIPs."""
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=0.94)
        with _params():
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert "awaiting_consec_ticks" in (d.skip_reason or "")
        assert d.metadata["consec_tick_count"] == 1
        assert d.metadata["min_consecutive_pass_ticks"] == 2

    def test_default_two_ticks_second_fires(self):
        """Second consecutive qualifying tick on same window fires TRADE."""
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=0.94)
        with _params():
            d1 = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d1.action == "SKIP"
        with _params():
            d2 = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d2.action == "TRADE"
        assert d2.direction == "UP"
        assert d2.metadata["consec_tick_count"] == 2

    def test_runtime_override_one_tick_fires_immediately(self):
        """min_consecutive_pass_ticks=1 override → single qualifying tick fires."""
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=0.94)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.metadata["consec_tick_count"] == 1
        assert d.metadata["min_consecutive_pass_ticks"] == 1

    def test_two_ticks_down_fires(self):
        """DOWN side also requires 2 consecutive ticks (same default)."""
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=0.04)
        with _params():
            d1 = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d1.action == "SKIP"
        assert "awaiting_consec_ticks" in (d1.skip_reason or "")
        with _params():
            d2 = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d2.action == "TRADE"
        assert d2.direction == "DOWN"


# ── Cross-strategy independence ────────────────────────────────────────────

class TestCrossStrategyIndependence:
    """Module-local _consec_state must NOT leak to sibling XRP strategies."""

    def test_consec_state_isolated_from_v9_5_xrp_down_solo(self):
        """Mutating v9_5_xrp_pure_lgb._consec_state must NOT affect
        v9_5_xrp_down_solo._consec_state.
        """
        from strategies.configs import v9_5_xrp_pure_lgb as pure_lgb
        from strategies.configs import v9_5_xrp_down_solo as down_solo

        pure_lgb._consec_state.clear()
        down_solo._consec_state.clear()
        assert pure_lgb._consec_state is not down_solo._consec_state

        # Fire pure_lgb (UP, 1-tick override) — populates ITS state only.
        surface = _make_surface(probability_lgb_v9_5_xrp_pure=0.95)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"
        assert len(pure_lgb._consec_state) >= 1
        # down_solo's state remains untouched.
        assert len(down_solo._consec_state) == 0

    def test_consec_state_isolated_from_v9_5_xrp_blend(self):
        """Independence from v9_5_xrp_blend."""
        from strategies.configs import v9_5_xrp_pure_lgb as pure_lgb
        from strategies.configs import v9_5_xrp_blend as blend

        pure_lgb._consec_state.clear()
        blend._consec_state.clear()
        assert pure_lgb._consec_state is not blend._consec_state

        surface = _make_surface(probability_lgb_v9_5_xrp_pure=0.95)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"
        assert len(pure_lgb._consec_state) >= 1
        assert len(blend._consec_state) == 0
