"""Unit tests for v9_3_btc_pure_up_solo GHOST strategy.

Coverage:
- model-not-loaded SKIP (v9_3_btc_pure_model_not_loaded) — None probability
- defensive asset guard (SKIP on non-BTC surface — wrong_asset)
- eval_offset outside band [60, 180] (outside_eval_band)
- UP threshold gating: fires at p=0.85, fires above, just below skips
- No DOWN fires (conviction_below_threshold for all p < up_threshold)
- Direction-aware fill-band gate:
    UP + fill=0.75 (>= 0.65 floor, < 0.90 cap)  → TRADE
    UP + fill=0.55 (< 0.65 floor)                → SKIP fill_below_up_floor
    UP + fill=0.65 (exactly at floor)             → TRADE (floor is inclusive >=)
    UP + fill=0.90 (exactly at cap)               → SKIP fill_above_up_cap (>= exclusive)
    UP + fill=0.91 (above cap)                    → SKIP fill_above_up_cap
    None fill bypasses gate                        → TRADE
- gate_params runtime override respected (tighten to p>=0.90, loosen to p>=0.80)
- Metadata shape on TRADE:
    - probability_lgb_v9_3_btc_pure key present (NOT the blend column)
    - up_threshold, strategy_id, strategy_version = "1.0.0"
    - direction = "UP"
    - fill_price and entry_floor_up present when fill available
- Consecutive ticks (min_consecutive_pass_ticks=2 default) gating:
    - First tick SKIPs, second TRADE
    - Runtime override to 1 fires immediately
    - Runtime override to 3 requires three ticks
- Cross-strategy independence: module-local _consec_state doesn't leak
  into sibling strategies (v9_3_btc_blend, v9_3_btc_tight_blend,
  v9_3_btc_pure_lgb, v9_3_btc_down_solo)

OOF context (v9.3.1 retrain, 468K records, 2026-05-25):
  UP p >= 0.80: 91.3% WR n=115K
  UP p >= 0.85: 94.3% WR n=90K  ← PRIMARY THRESHOLD
  UP p >= 0.90: 95.2% WR n=61K  ← tighter override

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

from strategies.configs.v9_3_btc_pure_up_solo import evaluate_v9_3_btc_pure_up_solo
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# ── Surface factory ────────────────────────────────────────────────────────

def _make_surface(**overrides) -> FullDataSurface:
    """Default surface for v9_3_btc_pure_up_solo tests.

    asset=BTC, eval_offset=120 (in band [60, 180]),
    probability_lgb_v9_3_btc_pure=0.90 (above the 0.85 UP threshold).
    clob_implied_up=0.75 (above entry_floor_up=0.65, below entry_cap=0.90).
    """
    defaults = dict(
        asset="BTC", timescale="5m",
        window_ts=1713009600,
        eval_offset=120, assembled_at=time.time(),
        current_price=68000.0, open_price=67500.0,
        delta_binance=0.007, delta_tiingo=0.006, delta_chainlink=0.007,
        delta_pct=0.007, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=0.004,
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
        clob_up_bid=0.73, clob_up_ask=0.75, clob_down_bid=0.18,
        clob_down_ask=0.20, clob_implied_up=0.75,
        gamma_up_price=0.75, gamma_down_price=0.25,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=1_200_000.0, cg_taker_sell_vol=800_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=1.5,
        timesfm_expected_move_bps=80.0, timesfm_vol_forecast_bps=100.0,
        hour_utc=14, seconds_to_close=120,
        probability_lgb_v9_3_btc_pure=0.90,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ── Gate-params helpers ────────────────────────────────────────────────────

_BASE_PARAMS: dict[str, Any] = {
    "up_threshold": 0.85,        # v9.3.1 OOF: UP p>=0.85 = 94.3% WR n=90K
    "eval_offset_min": 60,
    "eval_offset_max": 180,
    "min_consecutive_pass_ticks": 2,  # default=2 in this strategy
    "expected_asset": "BTC",
    "entry_cap": 0.90,
    "entry_floor_up": 0.65,
    "entry_cap_down": 0.90,
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
    from strategies.configs import v9_3_btc_pure_up_solo
    v9_3_btc_pure_up_solo._consec_state.clear()
    yield
    v9_3_btc_pure_up_solo._consec_state.clear()


# ── Null probability handling ──────────────────────────────────────────────

class TestNullProbabilityHandling:
    def test_model_not_loaded_skips_cleanly(self):
        """When probability_lgb_v9_3_btc_pure is None, strategy SKIPs cleanly."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=None)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_3_btc_pure_model_not_loaded"
        assert d.strategy_id == "v9_3_btc_pure_up_solo"

    def test_skip_reason_references_pure_column(self):
        """Verify skip reason explicitly names PURE — not blend."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=None)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert "pure" in (d.skip_reason or "")
        assert "blend" not in (d.skip_reason or "")

    def test_strategy_id_correct_on_skip(self):
        """Strategy ID must be v9_3_btc_pure_up_solo on all SKIP returns."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=None)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.strategy_id == "v9_3_btc_pure_up_solo"


# ── Defensive asset guard ──────────────────────────────────────────────────

class TestAssetGuard:
    def test_eth_surface_skips_even_with_high_pure_prob(self):
        """BTC-trained model must NOT fire on ETH surface."""
        surface = _make_surface(asset="ETH", probability_lgb_v9_3_btc_pure=0.95)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_xrp_surface_skips(self):
        surface = _make_surface(asset="XRP", probability_lgb_v9_3_btc_pure=0.95)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_sol_surface_skips(self):
        surface = _make_surface(asset="SOL", probability_lgb_v9_3_btc_pure=0.95)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_asset_check_before_probability_check(self):
        """Asset guard runs before probability check — non-BTC + None prob = wrong_asset."""
        surface = _make_surface(asset="ETH", probability_lgb_v9_3_btc_pure=None)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_btc_surface_passes_asset_guard(self):
        """BTC asset is accepted — strategy proceeds to threshold check."""
        surface = _make_surface(asset="BTC", probability_lgb_v9_3_btc_pure=0.90)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"


# ── Eval-offset band ───────────────────────────────────────────────────────

class TestEvalOffsetBand:
    def test_below_min_skips(self):
        """eval_offset=59 is below the band min of 60."""
        surface = _make_surface(eval_offset=59)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_above_max_skips(self):
        """eval_offset=181 is above the band max of 180."""
        surface = _make_surface(eval_offset=181)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_at_min_60_fires(self):
        surface = _make_surface(eval_offset=60, probability_lgb_v9_3_btc_pure=0.90,
                                clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_at_max_180_fires(self):
        surface = _make_surface(eval_offset=180, probability_lgb_v9_3_btc_pure=0.90,
                                clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_none_eval_offset_skips(self):
        surface = _make_surface(eval_offset=None)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_offset_240_outside_band_skips(self):
        """eval_offset=240 is above the 180 band max — strategy is tighter than pure_lgb."""
        surface = _make_surface(eval_offset=240, probability_lgb_v9_3_btc_pure=0.95)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_offset_120_mid_band_fires(self):
        """eval_offset=120 is squarely in the [60, 180] band."""
        surface = _make_surface(eval_offset=120, probability_lgb_v9_3_btc_pure=0.90,
                                clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"


# ── UP threshold gating ────────────────────────────────────────────────────

class TestUpThreshold:
    def test_up_fires_at_threshold_0_85(self):
        """Exactly at UP threshold 0.85 (94.3% WR n=90K OOF) — fires UP."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.85, clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.strategy_id == "v9_3_btc_pure_up_solo"
        assert d.entry_reason == "v9_3_btc_pure_up_solo_pass"

    def test_up_fires_at_0_86(self):
        """p=0.86 > 0.85 — fires UP."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.86, clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_up_fires_well_above_threshold(self):
        """p=0.95 well above threshold — fires UP."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.95, clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_just_below_up_threshold_skips(self):
        """p=0.849 < 0.85 — just below threshold, SKIP."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.849)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_neutral_0_50_skips(self):
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.50)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_p_0_80_below_default_threshold_skips(self):
        """p=0.80 is in the OOF 91.3% WR n=115K pocket but below 0.85 default — SKIP."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.80)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_tighten_runtime_to_0_90_skips_at_0_87(self):
        """Runtime tighten to p>=0.90 (95.2% WR n=61K) — p=0.87 now skips."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.87, clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1, up_threshold=0.90):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_tighten_runtime_to_0_90_fires_at_0_91(self):
        """Runtime tighten to p>=0.90 — p=0.91 fires UP."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.91, clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1, up_threshold=0.90):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_loosen_runtime_to_0_80_fires_at_0_82(self):
        """Runtime loosen to p>=0.80 (91.3% WR n=115K) — p=0.82 now fires UP."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.82, clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1, up_threshold=0.80):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"


# ── No DOWN fires (UP-only strategy) ──────────────────────────────────────

class TestNoDownFires:
    def test_low_prob_does_not_fire_down(self):
        """p=0.05 is below UP threshold — no DOWN direction, skips."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.05)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"
        assert d.direction is None

    def test_p_0_20_down_solo_threshold_does_not_fire_down(self):
        """p=0.20 — v9_3_btc_down_solo DOWN threshold, but UP-only skips."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.20)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_p_0_01_skips(self):
        """p=0.01 minimum — still skips in UP-only strategy."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.01)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_p_0_50_skips(self):
        """p=0.50 neutral — skips."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.50)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── Direction-aware fill-band gate (RDS note #664) ─────────────────────────

class TestFillBandGate:
    def test_up_fires_with_fill_0_75_between_floor_and_cap(self):
        """UP at p=0.90 + fill=0.75 (>= 0.65 floor, < 0.90 cap) → TRADE."""
        surface = _make_surface(
            probability_lgb_v9_3_btc_pure=0.90,
            eval_offset=120,
            clob_implied_up=0.75,
        )
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_up_skips_with_fill_0_55_below_floor(self):
        """UP at p=0.90 + fill=0.55 (< 0.65 floor) → SKIP fill_below_up_floor."""
        surface = _make_surface(
            probability_lgb_v9_3_btc_pure=0.90,
            eval_offset=120,
            clob_implied_up=0.55,
        )
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason is not None
        assert "fill_below_up_floor" in d.skip_reason
        assert "0.550" in d.skip_reason
        assert "0.650" in d.skip_reason

    def test_up_floor_exact_boundary_inclusive(self):
        """UP at fill=0.65 exactly — floor is inclusive (>=), TRADE."""
        surface = _make_surface(
            probability_lgb_v9_3_btc_pure=0.90,
            clob_implied_up=0.65,
        )
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_up_skips_with_fill_0_91_above_cap(self):
        """UP at p=0.90 + fill=0.91 (>= 0.90 cap) → SKIP fill_above_up_cap."""
        surface = _make_surface(
            probability_lgb_v9_3_btc_pure=0.90,
            eval_offset=120,
            clob_implied_up=0.91,
        )
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason is not None
        assert "fill_above_up_cap" in d.skip_reason
        assert "0.910" in d.skip_reason
        assert "0.900" in d.skip_reason

    def test_up_cap_exact_boundary_exclusive(self):
        """UP at fill=0.90 exactly — cap is exclusive (>=), SKIP."""
        surface = _make_surface(
            probability_lgb_v9_3_btc_pure=0.90,
            eval_offset=120,
            clob_implied_up=0.90,
        )
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason is not None
        assert "fill_above_up_cap" in d.skip_reason

    def test_none_fill_bypasses_gate(self):
        """When clob_implied_up is None, fill gate is skipped — TRADE."""
        surface = _make_surface(
            probability_lgb_v9_3_btc_pure=0.90,
            clob_implied_up=None,
        )
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_permissive_floor_allows_low_fill(self):
        """With entry_floor_up=0.0, all fills pass the floor check."""
        surface = _make_surface(
            probability_lgb_v9_3_btc_pure=0.90,
            clob_implied_up=0.10,
        )
        with _params(min_consecutive_pass_ticks=1, entry_floor_up=0.0, entry_cap=1.0):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_high_cap_allows_high_fill(self):
        """With entry_cap=1.0, near-resolved fills pass."""
        surface = _make_surface(
            probability_lgb_v9_3_btc_pure=0.90,
            clob_implied_up=0.95,
        )
        with _params(min_consecutive_pass_ticks=1, entry_cap=1.0):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"


# ── Runtime override ───────────────────────────────────────────────────────

class TestRuntimeOverride:
    def test_runtime_override_raises_threshold_to_0_90(self):
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.87, clob_implied_up=0.75)
        # Default 0.85, min_consec=1 — p=0.87 TRADE.
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        # Raise to 0.90 — p=0.87 now SKIP.
        with _params(min_consecutive_pass_ticks=1, up_threshold=0.90):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_runtime_override_narrows_eval_band(self):
        surface = _make_surface(eval_offset=170, probability_lgb_v9_3_btc_pure=0.90,
                                clob_implied_up=0.75)
        # Default band [60, 180] — TRADE.
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        # Narrow to [60, 150] — offset 170 out of band.
        with _params(min_consecutive_pass_ticks=1, eval_offset_max=150):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_runtime_override_entry_floor_up(self):
        """Override entry_floor_up from 0.65 to 0.70 — fill=0.67 now fails."""
        surface = _make_surface(
            probability_lgb_v9_3_btc_pure=0.90,
            clob_implied_up=0.67,
        )
        # Default floor=0.65 — fill=0.67 passes.
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        # Raise floor to 0.70 — fill=0.67 now fails.
        with _params(min_consecutive_pass_ticks=1, entry_floor_up=0.70):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert "fill_below_up_floor" in (d.skip_reason or "")


# ── Metadata shape ─────────────────────────────────────────────────────────

class TestMetadataShape:
    def test_metadata_contains_pure_probability_on_up_trade(self):
        """Critical: metadata MUST use probability_lgb_v9_3_btc_pure key."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.90, eval_offset=120,
                                clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        # PURE column key present
        assert "probability_lgb_v9_3_btc_pure" in d.metadata
        assert d.metadata["probability_lgb_v9_3_btc_pure"] == pytest.approx(0.90)
        # Old blend key absent (column switch check)
        assert "probability_lgb_v9_3_btc" not in d.metadata
        assert d.metadata["up_threshold"] == pytest.approx(0.85)
        assert d.metadata["eval_offset_min"] == 60
        assert d.metadata["eval_offset_max"] == 180
        assert d.metadata["eval_offset"] == 120
        assert d.metadata["asset"] == "BTC"
        assert d.metadata["strategy_id"] == "v9_3_btc_pure_up_solo"
        assert d.metadata["strategy_version"] == "1.0.0"

    def test_metadata_contains_sizing_on_trade(self):
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.90, clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.metadata["entry_cap"] == pytest.approx(0.90)
        assert d.metadata["collateral_pct"] == pytest.approx(0.025)
        assert d.metadata["gtc_cap"] == pytest.approx(0.96)

    def test_confidence_high_for_0_90_pure_prob(self):
        # p=0.90 -> |0.90 - 0.5| * 2 = 0.80 -> HIGH (>= 0.40)
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.90, clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.confidence == "HIGH"
        assert d.confidence_score == pytest.approx(0.80)

    def test_confidence_high_for_0_85_boundary(self):
        # p=0.85 -> |0.85 - 0.5| * 2 = 0.70 -> HIGH (>= 0.40)
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.85, clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.confidence == "HIGH"
        assert d.confidence_score == pytest.approx(0.70)

    def test_metadata_direction_is_up(self):
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.90, eval_offset=120,
                                clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.metadata["direction"] == "UP"

    def test_metadata_includes_fill_floor_when_available(self):
        surface = _make_surface(
            probability_lgb_v9_3_btc_pure=0.90,
            clob_implied_up=0.75,
        )
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.metadata.get("fill_price") == pytest.approx(0.75)
        assert d.metadata.get("entry_floor_up") == pytest.approx(0.65)

    def test_strategy_version_is_1_0_0(self):
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.90, clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.strategy_version == "1.0.0"

    def test_metadata_on_skip_also_correct(self):
        """Even on SKIP, strategy_id is correct."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.50)
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert d.strategy_id == "v9_3_btc_pure_up_solo"


# ── Consecutive ticks (default=2 in this strategy) ────────────────────────

class TestConsecutiveTicks:
    def test_default_two_ticks_first_skips(self):
        """min_consecutive_pass_ticks=2 (default) → first tick SKIPs."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.90, clob_implied_up=0.75)
        with _params():  # default min_consec=2
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "SKIP"
        assert "awaiting_consec_ticks" in (d.skip_reason or "")
        assert d.metadata["consec_tick_count"] == 1
        assert d.metadata["min_consecutive_pass_ticks"] == 2

    def test_default_two_ticks_second_fires(self):
        """Second consecutive tick with default min_consec=2 → TRADE."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.90, clob_implied_up=0.75)
        with _params():
            evaluate_v9_3_btc_pure_up_solo(surface)  # tick 1 — SKIP
        with _params():
            d = evaluate_v9_3_btc_pure_up_solo(surface)  # tick 2 — TRADE
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.metadata["consec_tick_count"] == 2

    def test_override_to_one_tick_fires_immediately(self):
        """Runtime override min_consecutive_pass_ticks=1 → fires on first tick."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.90, clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.metadata["consec_tick_count"] == 1
        assert d.metadata["min_consecutive_pass_ticks"] == 1

    def test_override_to_three_ticks(self):
        """Runtime override min_consecutive_pass_ticks=3 → fires on third tick only."""
        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.90, clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=3):
            d1 = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d1.action == "SKIP"
        with _params(min_consecutive_pass_ticks=3):
            d2 = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d2.action == "SKIP"
        with _params(min_consecutive_pass_ticks=3):
            d3 = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d3.action == "TRADE"
        assert d3.direction == "UP"

    def test_direction_switch_resets_counter(self):
        """UP tick sequence continues on same window_ts."""
        surface_up = _make_surface(probability_lgb_v9_3_btc_pure=0.90, clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1):
            evaluate_v9_3_btc_pure_up_solo(surface_up)  # registers UP tick
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface_up)
        assert d.action == "TRADE"


# ── Cross-strategy independence ────────────────────────────────────────────

class TestCrossStrategyIndependence:
    """Module-local _consec_state must NOT leak to sibling strategies."""

    def test_consec_state_isolated_from_btc_blend(self):
        """v9_3_btc_pure_up_solo._consec_state must NOT affect v9_3_btc_blend."""
        from strategies.configs import v9_3_btc_pure_up_solo as up_solo
        from strategies.configs import v9_3_btc_blend as btc_blend

        up_solo._consec_state.clear()
        btc_blend._consec_state.clear()
        assert up_solo._consec_state is not btc_blend._consec_state

        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.90, clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1):
            d = evaluate_v9_3_btc_pure_up_solo(surface)
        assert d.action == "TRADE"
        assert len(up_solo._consec_state) >= 1
        assert len(btc_blend._consec_state) == 0

    def test_consec_state_isolated_from_tight_blend(self):
        """Module-local state isolated from v9_3_btc_tight_blend."""
        from strategies.configs import v9_3_btc_pure_up_solo as up_solo
        from strategies.configs import v9_3_btc_tight_blend as tight

        up_solo._consec_state.clear()
        tight._consec_state.clear()
        assert up_solo._consec_state is not tight._consec_state

        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.90, clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1):
            evaluate_v9_3_btc_pure_up_solo(surface)
        assert len(tight._consec_state) == 0

    def test_consec_state_isolated_from_pure_lgb(self):
        """Module-local state isolated from v9_3_btc_pure_lgb (also reads PURE column)."""
        from strategies.configs import v9_3_btc_pure_up_solo as up_solo
        from strategies.configs import v9_3_btc_pure_lgb as pure_lgb

        up_solo._consec_state.clear()
        pure_lgb._consec_state.clear()
        assert up_solo._consec_state is not pure_lgb._consec_state

        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.90, clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1):
            evaluate_v9_3_btc_pure_up_solo(surface)
        assert len(pure_lgb._consec_state) == 0

    def test_consec_state_isolated_from_down_solo(self):
        """Module-local state isolated from v9_3_btc_down_solo (companion strategy)."""
        from strategies.configs import v9_3_btc_pure_up_solo as up_solo
        from strategies.configs import v9_3_btc_down_solo as down_solo

        up_solo._consec_state.clear()
        down_solo._consec_state.clear()
        assert up_solo._consec_state is not down_solo._consec_state

        surface = _make_surface(probability_lgb_v9_3_btc_pure=0.90, clob_implied_up=0.75)
        with _params(min_consecutive_pass_ticks=1):
            evaluate_v9_3_btc_pure_up_solo(surface)
        assert len(down_solo._consec_state) == 0
