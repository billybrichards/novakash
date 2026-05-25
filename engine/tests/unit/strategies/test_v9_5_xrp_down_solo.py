"""Unit tests for v9_5_xrp_down_solo GHOST strategy.

Coverage:
- model-not-loaded SKIP (v9_5_xrp_model_not_loaded) — None probability
- defensive asset guard (SKIP on non-XRP surface — wrong_asset)
- eval_offset outside band [60, 180] (outside_eval_band)
- DOWN threshold gating: fires at p=0.10, fires below, just above skips
- No UP fires (conviction_below_threshold for all p > down_threshold)
- Direction-aware fill-band gate:
    DOWN + fill=0.70 (<= 0.85 cap) → TRADE
    DOWN + fill=0.90 (> 0.85 cap)  → SKIP fill_above_down_cap
    DOWN + fill=0.85 (exactly)     → TRADE (cap is inclusive <=)
    None fill bypasses gate          → TRADE
- gate_params runtime override respected
- Metadata shape on TRADE includes probability + threshold + strategy_id
- Consecutive ticks (min_consecutive_pass_ticks=2 default) gating
- Cross-strategy independence: module-local _consec_state doesn't leak
  into sibling strategies (v9_5_xrp_up_solo, v9_5_xrp_blend, etc.)
- Confidence score computed from distance-from-neutral for DOWN direction
- OOF threshold boundary: p=0.15 skips at default 0.10, fires at override 0.15
- Asset guard runs before probability check

Test strategy: synthetic _make_surface() fixture; uses _gp.set_active /
_gp.reset_active (same pattern as test_v9_5_xrp_up_solo.py).
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_5_xrp_down_solo import evaluate_v9_5_xrp_down_solo
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# ── Surface factory ────────────────────────────────────────────────────────

def _make_surface(**overrides) -> FullDataSurface:
    """Default surface for v9_5_xrp_down_solo tests.

    asset=XRP, eval_offset=120 (in band [60, 180]),
    probability_lgb_v9_5_xrp=0.08 (below the 0.10 DOWN threshold).
    clob_down_ask=0.70 (below entry_cap_down=0.85 — DOWN fires through).
    """
    defaults = dict(
        asset="XRP", timescale="5m",
        window_ts=1713009600,
        eval_offset=120, assembled_at=time.time(),
        current_price=0.62, open_price=0.61,
        delta_binance=-0.008, delta_tiingo=-0.007, delta_chainlink=-0.008,
        delta_pct=-0.008, delta_source="chainlink",
        vpin=0.60, regime="NORMAL", twap_delta=-0.005,
        v2_probability_up=0.15, v2_probability_raw=0.15,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=0.15, probability_classifier=None,
        ensemble_config={"mode": "pure"},
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
        poly_max_entry_price=0.30, poly_reason="strong_signal",
        v4_recommended_side="DOWN", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.22, clob_up_ask=0.25, clob_down_bid=0.68,
        clob_down_ask=0.70, clob_implied_up=0.25,
        gamma_up_price=0.25, gamma_down_price=0.70,
        cg_oi_usd=50_000_000.0, cg_funding_rate=-0.0001,
        cg_taker_buy_vol=500_000.0, cg_taker_sell_vol=1_500_000.0,
        cg_liq_total=800_000.0, cg_liq_long=600_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=0.8,
        timesfm_expected_move_bps=-50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=12, seconds_to_close=120,
        probability_lgb_v9_5_xrp=0.08,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ── Gate-params helpers ────────────────────────────────────────────────────

_BASE_PARAMS: dict[str, Any] = {
    "down_threshold": 0.10,
    "eval_offset_min": 60,
    "eval_offset_max": 180,
    "min_consecutive_pass_ticks": 2,
    "expected_asset": "XRP",
    "entry_cap": 0.90,
    "entry_floor_up": 0.70,
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
    from strategies.configs import v9_5_xrp_down_solo
    v9_5_xrp_down_solo._consec_state.clear()
    yield
    v9_5_xrp_down_solo._consec_state.clear()


# ── Null probability handling ──────────────────────────────────────────────

class TestNullProbabilityHandling:
    def test_model_not_loaded_skips_cleanly(self):
        """When the snapshot has no probability_lgb_v9_5_xrp, the strategy
        SKIPs with a clear reason — no crash."""
        surface = _make_surface(probability_lgb_v9_5_xrp=None)
        with _params():
            # Tick 1 — would normally await consec, but model check runs first.
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_5_xrp_model_not_loaded"
        assert d.strategy_id == "v9_5_xrp_down_solo"

    def test_model_not_loaded_returns_zero_confidence(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=None)
        with _params():
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.confidence_score == 0.0
        assert d.entry_cap == 0.0
        assert d.collateral_pct == 0.0


# ── Defensive asset guard ──────────────────────────────────────────────────

class TestAssetGuard:
    def test_btc_surface_skips_even_with_low_prob(self):
        """XRP DOWN strategy refuses to fire on a BTC surface."""
        surface = _make_surface(asset="BTC", probability_lgb_v9_5_xrp=0.05)
        with _params():
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_eth_surface_skips(self):
        surface = _make_surface(asset="ETH", probability_lgb_v9_5_xrp=0.05)
        with _params():
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_sol_surface_skips(self):
        surface = _make_surface(asset="SOL", probability_lgb_v9_5_xrp=0.05)
        with _params():
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_asset_check_runs_before_probability_check(self):
        """Asset check runs first — non-XRP with None prob still wrong_asset."""
        surface = _make_surface(asset="BTC", probability_lgb_v9_5_xrp=None)
        with _params():
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_xrp_asset_passes_guard(self):
        """Explicit XRP passes the asset guard (precondition for other tests)."""
        surface = _make_surface(asset="XRP", probability_lgb_v9_5_xrp=0.08)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.skip_reason != "wrong_asset"


# ── Eval-offset band ───────────────────────────────────────────────────────

class TestEvalOffsetBand:
    def test_below_min_skips(self):
        """eval_offset=59 is below the band min of 60."""
        surface = _make_surface(eval_offset=59)
        with _params():
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_above_max_skips(self):
        """eval_offset=181 is above the band max of 180."""
        surface = _make_surface(eval_offset=181)
        with _params():
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_at_min_60_fires(self):
        surface = _make_surface(eval_offset=60, probability_lgb_v9_5_xrp=0.08)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_at_max_180_fires(self):
        surface = _make_surface(eval_offset=180, probability_lgb_v9_5_xrp=0.08)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_none_eval_offset_skips(self):
        surface = _make_surface(eval_offset=None)
        with _params():
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_offset_240_skips_even_with_low_prob(self):
        """eval_offset=240 is above 180 band max — no fire."""
        surface = _make_surface(eval_offset=240, probability_lgb_v9_5_xrp=0.01)
        with _params():
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


# ── DOWN threshold gating ──────────────────────────────────────────────────

class TestDownThreshold:
    def test_down_fires_at_threshold_0_10(self):
        """Exactly at DOWN threshold 0.10 — fires DOWN."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.10)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"
        assert d.strategy_id == "v9_5_xrp_down_solo"
        assert d.entry_reason == "v9_5_xrp_down_solo_pass"

    def test_down_fires_at_0_08(self):
        """p=0.08 < 0.10 — fires DOWN."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.08)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_down_fires_well_below_threshold(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.01)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_just_above_down_threshold_skips(self):
        """0.101 > 0.10 — above the DOWN threshold, skips."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.101)
        with _params():
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_neutral_0_50_skips(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.50)
        with _params():
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_oof_0_15_skips_at_default_threshold(self):
        """p=0.15 is the alternative OOF threshold — skips at default 0.10."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.15)
        with _params():
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_oof_0_15_fires_with_override_threshold(self):
        """p=0.15 fires when down_threshold overridden to 0.15 (90.6% WR n=48K pocket)."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.15)
        with _params(down_threshold=0.15, min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"


# ── No UP fires (DOWN-only strategy) ──────────────────────────────────────

class TestNoUpFires:
    def test_high_prob_does_not_fire_up(self):
        """p=0.90 is above DOWN threshold — no UP direction, skips."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.90)
        with _params():
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"
        assert d.direction is None

    def test_p_0_82_does_not_fire_up(self):
        """p=0.82 — v9_5_xrp_up_solo UP threshold, but DOWN-only skips."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.82)
        with _params():
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_p_0_99_skips(self):
        """p=0.99 maximum — still skips in DOWN-only strategy."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.99)
        with _params():
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── Direction-aware fill-band gate (RDS note #664) ─────────────────────────

class TestFillBandGate:
    def test_down_fires_with_fill_0_70_below_cap(self):
        """DOWN at p=0.08 + fill=0.70 (<= 0.85 cap) → TRADE."""
        surface = _make_surface(
            probability_lgb_v9_5_xrp=0.08,
            eval_offset=120,
            clob_down_ask=0.70,
        )
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_down_skips_with_fill_0_90_above_cap(self):
        """DOWN at p=0.08 + fill=0.90 (> 0.85 cap) → SKIP fill_above_down_cap."""
        surface = _make_surface(
            probability_lgb_v9_5_xrp=0.08,
            eval_offset=120,
            clob_down_ask=0.90,
        )
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason is not None
        assert "fill_above_down_cap" in d.skip_reason
        assert "0.900" in d.skip_reason
        assert "0.850" in d.skip_reason

    def test_down_cap_exact_boundary_inclusive(self):
        """DOWN at fill=0.85 exactly — boundary inclusive, TRADE."""
        surface = _make_surface(
            probability_lgb_v9_5_xrp=0.08,
            clob_down_ask=0.85,
        )
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_none_fill_bypasses_gate(self):
        """When clob_down_ask is None, fill gate is skipped — TRADE."""
        surface = _make_surface(
            probability_lgb_v9_5_xrp=0.08,
            clob_down_ask=None,
        )
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_permissive_cap_1_0_allows_all_fills(self):
        """With entry_cap_down=1.0, all fills pass (no cap filtering)."""
        surface = _make_surface(
            probability_lgb_v9_5_xrp=0.08,
            clob_down_ask=0.98,
        )
        with _params(entry_cap_down=1.0, min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"


# ── Runtime override ───────────────────────────────────────────────────────

class TestRuntimeOverride:
    def test_runtime_override_lowers_down_threshold(self):
        """p=0.08 fires at default 0.10; raising threshold to 0.06 causes SKIP."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.08)
        # Default 0.10 — would TRADE (needs consec=1 for test speed).
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        # Tighten to 0.06 — p=0.08 now above threshold, SKIP.
        with _params(down_threshold=0.06, min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_runtime_override_widens_to_0_15(self):
        """Override down_threshold=0.15 allows the 90.6% WR n=48K OOF pocket."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.13)
        # Default 0.10 — p=0.13 > 0.10, SKIP.
        with _params():
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        # Widen to 0.15 — p=0.13 <= 0.15, TRADE.
        with _params(down_threshold=0.15, min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_runtime_override_narrows_eval_band(self):
        surface = _make_surface(eval_offset=170, probability_lgb_v9_5_xrp=0.08)
        # Default band [60, 180] — TRADE (1 tick for speed).
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        # Narrow to [60, 150] — offset 170 now out of band.
        with _params(eval_offset_max=150):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


# ── Metadata shape ─────────────────────────────────────────────────────────

class TestMetadataShape:
    def test_metadata_contains_probability_on_down_trade(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.08, eval_offset=120)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.metadata["probability_lgb_v9_5_xrp"] == pytest.approx(0.08)
        assert d.metadata["down_threshold"] == pytest.approx(0.10)
        assert d.metadata["eval_offset_min"] == 60
        assert d.metadata["eval_offset_max"] == 180
        assert d.metadata["eval_offset"] == 120
        assert d.metadata["asset"] == "XRP"
        assert d.metadata["strategy_id"] == "v9_5_xrp_down_solo"
        assert d.metadata["strategy_version"] == "1.0.0"

    def test_metadata_contains_sizing_on_trade(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.08)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.metadata["entry_cap"] == pytest.approx(0.90)
        assert d.metadata["collateral_pct"] == pytest.approx(0.025)
        assert d.metadata["gtc_cap"] == pytest.approx(0.96)

    def test_confidence_score_uses_distance_from_neutral_down(self):
        # p=0.08 -> |0.08 - 0.5| * 2 = 0.84 -> HIGH (>= 0.40)
        surface = _make_surface(probability_lgb_v9_5_xrp=0.08)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.confidence == "HIGH"
        assert d.confidence_score == pytest.approx(0.84)

    def test_confidence_score_at_threshold_0_10(self):
        # p=0.10 -> |0.10 - 0.5| * 2 = 0.80 -> HIGH
        surface = _make_surface(probability_lgb_v9_5_xrp=0.10)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.confidence == "HIGH"
        assert d.confidence_score == pytest.approx(0.80)

    def test_metadata_includes_direction_on_trade(self):
        surface = _make_surface(probability_lgb_v9_5_xrp=0.08, eval_offset=120)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.metadata["direction"] == "DOWN"

    def test_metadata_includes_fill_price_and_cap_when_available(self):
        surface = _make_surface(
            probability_lgb_v9_5_xrp=0.08,
            clob_down_ask=0.72,
        )
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.metadata.get("fill_price") == pytest.approx(0.72)
        assert d.metadata.get("entry_cap_down") == pytest.approx(0.85)

    def test_skip_metadata_includes_probability(self):
        """Even on SKIP, metadata should include available probability."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.50, eval_offset=120)
        with _params():
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert d.metadata["probability_lgb_v9_5_xrp"] == pytest.approx(0.50)


# ── Consecutive ticks (default=2 for DOWN) ─────────────────────────────────

class TestConsecutiveTicks:
    def test_default_two_ticks_first_awaits(self):
        """min_consecutive_pass_ticks=2 (default) → first tick SKIPs."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.08)
        with _params():
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "SKIP"
        assert "awaiting_consec_ticks" in (d.skip_reason or "")
        assert d.metadata["consec_tick_count"] == 1
        assert d.metadata["min_consecutive_pass_ticks"] == 2

    def test_default_two_ticks_second_fires(self):
        """Second consecutive qualifying tick fires TRADE."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.08)
        with _params():
            _ = evaluate_v9_5_xrp_down_solo(surface)  # tick 1 — skip
            d = evaluate_v9_5_xrp_down_solo(surface)  # tick 2 — trade
        assert d.action == "TRADE"
        assert d.direction == "DOWN"
        assert d.metadata["consec_tick_count"] == 2

    def test_override_to_one_tick_fires_immediately(self):
        """Runtime override min_consecutive_pass_ticks=1 → fires on first tick."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.08)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"
        assert d.metadata["consec_tick_count"] == 1

    def test_consec_count_in_skip_metadata(self):
        """SKIP while awaiting consec ticks still reports count in metadata."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.08)
        with _params(min_consecutive_pass_ticks=3):
            d1 = evaluate_v9_5_xrp_down_solo(surface)
            d2 = evaluate_v9_5_xrp_down_solo(surface)
        assert d1.metadata["consec_tick_count"] == 1
        assert d2.metadata["consec_tick_count"] == 2
        assert d1.action == "SKIP"
        assert d2.action == "SKIP"

    def test_three_ticks_fires_on_third(self):
        """min_consecutive_pass_ticks=3 → fires on third consecutive tick."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.08)
        with _params(min_consecutive_pass_ticks=3):
            _ = evaluate_v9_5_xrp_down_solo(surface)
            _ = evaluate_v9_5_xrp_down_solo(surface)
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"
        assert d.metadata["consec_tick_count"] == 3


# ── Cross-strategy independence ────────────────────────────────────────────

class TestCrossStrategyIndependence:
    """Module-local _consec_state must NOT leak to sibling strategies."""

    def test_consec_state_isolated_from_xrp_up_solo(self):
        """Mutating v9_5_xrp_down_solo._consec_state must NOT affect
        v9_5_xrp_up_solo._consec_state (separate module-locals).
        """
        from strategies.configs import v9_5_xrp_down_solo as down_solo
        from strategies.configs import v9_5_xrp_up_solo as up_solo

        down_solo._consec_state.clear()
        up_solo._consec_state.clear()
        assert down_solo._consec_state is not up_solo._consec_state

        surface = _make_surface(probability_lgb_v9_5_xrp=0.08)
        with _params():
            # Tick 1 on down_solo — populates its state.
            evaluate_v9_5_xrp_down_solo(surface)
        assert len(down_solo._consec_state) >= 1
        # up_solo's state remains untouched.
        assert len(up_solo._consec_state) == 0

    def test_consec_state_isolated_from_xrp_blend(self):
        """Module-local state is isolated from v9_5_xrp_blend."""
        from strategies.configs import v9_5_xrp_down_solo as down_solo
        from strategies.configs import v9_5_xrp_blend as xrp_blend

        down_solo._consec_state.clear()
        xrp_blend._consec_state.clear()
        assert down_solo._consec_state is not xrp_blend._consec_state

        surface = _make_surface(probability_lgb_v9_5_xrp=0.08)
        with _params():
            evaluate_v9_5_xrp_down_solo(surface)
        assert len(xrp_blend._consec_state) == 0

    def test_strategy_id_is_down_solo_not_up_solo(self):
        """Ensure strategy_id is v9_5_xrp_down_solo, not the sister strategy."""
        surface = _make_surface(probability_lgb_v9_5_xrp=0.08)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_5_xrp_down_solo(surface)
        assert d.strategy_id == "v9_5_xrp_down_solo"
        assert d.strategy_id != "v9_5_xrp_up_solo"
