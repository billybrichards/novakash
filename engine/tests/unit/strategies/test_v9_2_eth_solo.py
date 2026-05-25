"""Unit tests for v9_2_eth_solo GHOST strategy.

Coverage:
- model-not-loaded SKIP (v9_2_eth_model_not_loaded) — None probability
- defensive asset guard (SKIP on non-ETH surface — wrong_asset)
- eval_offset outside band [60, 180] (outside_eval_band)
- UP threshold gating: fires at p=0.80, fires above, just below skips
- DOWN threshold gating: fires at p=0.45, fires below, just above skips
- Neutral p=0.78 (between 0.45 and 0.80) → conviction_below_threshold
- Direction-aware fill-band gate:
    UP  + fill=0.85 (>= 0.60)  → TRADE
    UP  + fill=0.55 (< 0.60)   → SKIP fill_below_up_floor
    DOWN + fill=0.80 (< 0.90)  → TRADE
    DOWN + fill=0.91 (>= 0.90) → SKIP fill_above_down_cap
- None fill bypasses gate → TRADE
- gate_params runtime override respected
- Metadata shape on TRADE includes probability + threshold + strategy_id
- Consecutive ticks (min_consecutive_pass_ticks) gating
- Cross-strategy independence: module-local _consec_state doesn't leak
  into sibling strategies (v9_2_eth_late_band_AB_blend, v9_2_eth_raw_lgb)

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

from strategies.configs.v9_2_eth_solo import evaluate_v9_2_eth_solo
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# ── Surface factory ────────────────────────────────────────────────────────

def _make_surface(**overrides) -> FullDataSurface:
    """Default surface for v9_2_eth_solo tests.

    asset=ETH, eval_offset=120 (in band [60, 180]), hour_utc=12,
    probability_lgb_v9_2_eth=0.82 (above the 0.80 UP threshold).
    clob_implied_up=0.65 (above entry_floor_up=0.60).
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
        probability_lgb_v9_2_eth=0.82,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ── Gate-params helpers ────────────────────────────────────────────────────

_BASE_PARAMS: dict[str, Any] = {
    "up_threshold": 0.80,
    "down_threshold": 0.45,
    "eval_offset_min": 60,
    "eval_offset_max": 180,
    "min_consecutive_pass_ticks": 1,
    "expected_asset": "ETH",
    "entry_cap": 0.93,
    "entry_floor_up": 0.60,
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
    from strategies.configs import v9_2_eth_solo
    v9_2_eth_solo._consec_state.clear()
    yield
    v9_2_eth_solo._consec_state.clear()


# ── Null probability handling ──────────────────────────────────────────────

class TestNullProbabilityHandling:
    def test_model_not_loaded_skips_cleanly(self):
        """When the snapshot has no probability_lgb_v9_2_eth, the strategy
        SKIPs with a clear reason — no crash."""
        surface = _make_surface(probability_lgb_v9_2_eth=None)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_2_eth_model_not_loaded"
        assert d.strategy_id == "v9_2_eth_solo"


# ── Defensive asset guard ──────────────────────────────────────────────────

class TestAssetGuard:
    def test_btc_surface_skips_even_with_high_prob(self):
        """ETH strategy refuses to fire on a BTC surface — defensive guard."""
        surface = _make_surface(asset="BTC", probability_lgb_v9_2_eth=0.95)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_xrp_surface_skips(self):
        surface = _make_surface(asset="XRP", probability_lgb_v9_2_eth=0.95)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_asset_check_runs_before_probability_check(self):
        """Asset check runs first — non-ETH with None prob still wrong_asset."""
        surface = _make_surface(asset="BTC", probability_lgb_v9_2_eth=None)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"


# ── Eval-offset band ───────────────────────────────────────────────────────

class TestEvalOffsetBand:
    def test_below_min_skips(self):
        """eval_offset=59 is below the band min of 60."""
        surface = _make_surface(eval_offset=59)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_above_max_skips(self):
        """eval_offset=181 is above the band max of 180."""
        surface = _make_surface(eval_offset=181)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_at_min_60_fires(self):
        surface = _make_surface(eval_offset=60, probability_lgb_v9_2_eth=0.82)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_at_max_180_fires(self):
        surface = _make_surface(eval_offset=180, probability_lgb_v9_2_eth=0.82)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_none_eval_offset_skips(self):
        surface = _make_surface(eval_offset=None)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_offset_outside_band_skips_even_with_high_prob(self):
        """eval_offset=240 is above the 180 band max — no fire."""
        surface = _make_surface(eval_offset=240, probability_lgb_v9_2_eth=0.95)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


# ── UP threshold gating ────────────────────────────────────────────────────

class TestUpThreshold:
    def test_up_fires_at_threshold_0_80(self):
        """Exactly at UP threshold 0.80 — fires UP."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.80)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.strategy_id == "v9_2_eth_solo"
        assert d.entry_reason == "v9_2_eth_solo_pass"

    def test_up_fires_at_0_81(self):
        """p=0.81 > 0.80 — fires UP (per RDS note #665 UP >=0.80 band)."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.81)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_up_fires_well_above_threshold(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.95)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_p_0_78_between_thresholds_skips(self):
        """p=0.78 is between 0.45 (DOWN) and 0.80 (UP) — no conviction."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.78)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_just_below_up_threshold_skips(self):
        """0.799 < 0.80 — below the UP threshold."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.799)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── DOWN threshold gating ──────────────────────────────────────────────────

class TestDownThreshold:
    def test_down_fires_at_threshold_0_45(self):
        """Exactly at DOWN threshold 0.45 — fires DOWN."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.45, clob_implied_up=0.80)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"
        assert d.strategy_id == "v9_2_eth_solo"
        assert d.entry_reason == "v9_2_eth_solo_pass"

    def test_down_fires_at_0_44(self):
        """p=0.44 < 0.45 — fires DOWN."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.44, clob_implied_up=0.80)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_down_fires_well_below_threshold(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.05, clob_implied_up=0.80)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_just_above_down_threshold_skips(self):
        """0.451 > 0.45 — above DOWN threshold, below UP threshold."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.451)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_neutral_0_50_skips(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.50)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── Direction-aware fill-band gate (RDS note #664) ─────────────────────────

class TestFillBandGate:
    """Deliverable tests from RDS note #665 specification:
    - UP fires at p=0.81 + fill 0.85 + offset in range → TRADE
    - UP fires at p=0.81 + fill 0.55 (below floor) → SKIP
    - DOWN fires at p=0.44 + fill 0.80 → TRADE
    - DOWN fires at p=0.44 + fill 0.91 (above cap) → SKIP
    """

    def test_up_fires_with_fill_0_85_above_floor(self):
        """UP at p=0.81 + fill=0.85 (>= 0.60) + offset in range → TRADE."""
        surface = _make_surface(
            probability_lgb_v9_2_eth=0.81,
            eval_offset=120,
            clob_implied_up=0.85,
        )
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_up_skips_with_fill_0_55_below_floor(self):
        """UP at p=0.81 + fill=0.55 (< 0.60 floor) → SKIP fill_below_up_floor."""
        surface = _make_surface(
            probability_lgb_v9_2_eth=0.81,
            eval_offset=120,
            clob_implied_up=0.55,
        )
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason is not None
        assert "fill_below_up_floor" in d.skip_reason
        assert "0.550" in d.skip_reason
        assert "0.600" in d.skip_reason

    def test_down_fires_with_fill_0_80_below_cap(self):
        """DOWN at p=0.44 + fill=0.80 (< 0.90 cap) → TRADE."""
        surface = _make_surface(
            probability_lgb_v9_2_eth=0.44,
            eval_offset=120,
            clob_implied_up=0.80,
        )
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_down_skips_with_fill_0_91_above_cap(self):
        """DOWN at p=0.44 + fill=0.91 (>= 0.90 cap) → SKIP fill_above_down_cap."""
        surface = _make_surface(
            probability_lgb_v9_2_eth=0.44,
            eval_offset=120,
            clob_implied_up=0.91,
        )
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason is not None
        assert "fill_above_down_cap" in d.skip_reason
        assert "0.910" in d.skip_reason
        assert "0.900" in d.skip_reason

    def test_none_fill_bypasses_gate(self):
        """When clob_implied_up is None, fill gate is skipped — TRADE."""
        surface = _make_surface(
            probability_lgb_v9_2_eth=0.82,
            clob_implied_up=None,
        )
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_up_floor_exact_boundary_inclusive(self):
        """UP at fill=0.60 exactly — boundary inclusive, TRADE."""
        surface = _make_surface(
            probability_lgb_v9_2_eth=0.82,
            clob_implied_up=0.60,
        )
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_down_cap_exact_boundary_exclusive(self):
        """DOWN at fill=0.90 — at cap boundary, SKIP (cap is exclusive >=)."""
        surface = _make_surface(
            probability_lgb_v9_2_eth=0.44,
            clob_implied_up=0.90,
        )
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason is not None
        assert "fill_above_down_cap" in d.skip_reason

    def test_default_permissive_allows_low_fill_on_up(self):
        """Without explicit gate params (defaults: 0.0/1.0), all fills pass."""
        surface = _make_surface(
            probability_lgb_v9_2_eth=0.82,
            clob_implied_up=0.10,
        )
        with _params(entry_floor_up=0.0, entry_cap_down=1.0):
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"


# ── Threshold edge: p=0.78 (not UP, not DOWN) ─────────────────────────────

class TestNeutralProbability:
    def test_p_0_78_neutral_zone_skips(self):
        """p=0.78 is between 0.45 and 0.80 — no conviction either way."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.78)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_p_0_46_neutral_zone_skips(self):
        """p=0.46 is between 0.45 and 0.80 — no conviction either way."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.46)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── Runtime override ───────────────────────────────────────────────────────

class TestRuntimeOverride:
    def test_runtime_override_raises_up_threshold(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.82)
        # Default 0.80 — TRADE.
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        # Raise to 0.90 — SKIP.
        with _params(up_threshold=0.90):
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_runtime_override_lowers_down_threshold(self):
        """Raising DOWN threshold makes a previously-passing prob skip."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.44, clob_implied_up=0.80)
        # Default 0.45 — TRADE.
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"
        # Tighten DOWN to 0.40 — p=0.44 is now > 0.40, SKIP.
        with _params(down_threshold=0.40):
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_runtime_override_narrows_eval_band(self):
        surface = _make_surface(eval_offset=170, probability_lgb_v9_2_eth=0.82)
        # Default band [60, 180] — TRADE.
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        # Narrow to [60, 150] — offset 170 now out of band.
        with _params(eval_offset_max=150):
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


# ── Metadata shape ─────────────────────────────────────────────────────────

class TestMetadataShape:
    def test_metadata_contains_probability_on_up_trade(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.82, eval_offset=120)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.metadata["probability_lgb_v9_2_eth"] == pytest.approx(0.82)
        assert d.metadata["up_threshold"] == pytest.approx(0.80)
        assert d.metadata["down_threshold"] == pytest.approx(0.45)
        assert d.metadata["eval_offset_min"] == 60
        assert d.metadata["eval_offset_max"] == 180
        assert d.metadata["eval_offset"] == 120
        assert d.metadata["asset"] == "ETH"
        assert d.metadata["strategy_id"] == "v9_2_eth_solo"
        assert d.metadata["strategy_version"] == "1.0.0"

    def test_metadata_contains_sizing_on_trade(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.82)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.metadata["entry_cap"] == pytest.approx(0.93)
        assert d.metadata["collateral_pct"] == pytest.approx(0.025)
        assert d.metadata["gtc_cap"] == pytest.approx(0.96)

    def test_confidence_score_uses_distance_from_neutral_up(self):
        # p=0.82 -> |0.82 - 0.5| * 2 = 0.64 -> HIGH (>= 0.40)
        surface = _make_surface(probability_lgb_v9_2_eth=0.82)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.confidence == "HIGH"
        assert d.confidence_score == pytest.approx(0.64)

    def test_confidence_score_uses_distance_from_neutral_down(self):
        # p=0.44 -> |0.44 - 0.5| * 2 = 0.12 -> MODERATE (< 0.40)
        surface = _make_surface(probability_lgb_v9_2_eth=0.44, clob_implied_up=0.80)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.confidence == "MODERATE"
        assert d.confidence_score == pytest.approx(0.12)

    def test_metadata_includes_direction_on_trade(self):
        surface = _make_surface(probability_lgb_v9_2_eth=0.82, eval_offset=120)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.metadata["direction"] == "UP"

    def test_metadata_includes_fill_price_when_available(self):
        surface = _make_surface(
            probability_lgb_v9_2_eth=0.82,
            clob_implied_up=0.65,
        )
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.metadata.get("fill_price") == pytest.approx(0.65)
        assert d.metadata.get("entry_floor_up") == pytest.approx(0.60)
        assert d.metadata.get("entry_cap_down") == pytest.approx(0.90)


# ── Consecutive ticks ──────────────────────────────────────────────────────

class TestConsecutiveTicks:
    def test_default_one_tick_fires_immediately(self):
        """min_consecutive_pass_ticks=1 (default) → one qualifying tick fires."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.82)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.metadata["consec_tick_count"] == 1
        assert d.metadata["min_consecutive_pass_ticks"] == 1

    def test_runtime_override_requires_two_ticks(self):
        """Runtime override min_consecutive_pass_ticks=2 → first tick SKIPs."""
        surface = _make_surface(probability_lgb_v9_2_eth=0.82)
        with _params(min_consecutive_pass_ticks=2):
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "SKIP"
        assert "awaiting_consec_ticks" in (d.skip_reason or "")
        # Second consecutive tick should TRADE.
        with _params(min_consecutive_pass_ticks=2):
            d2 = evaluate_v9_2_eth_solo(surface)
        assert d2.action == "TRADE"
        assert d2.direction == "UP"


# ── Cross-strategy independence ────────────────────────────────────────────

class TestCrossStrategyIndependence:
    """Module-local _consec_state must NOT leak to sibling strategies."""

    def test_consec_state_isolated_from_late_band(self):
        """Mutating v9_2_eth_solo._consec_state must NOT affect
        v9_2_eth_late_band_AB_blend._consec_state (separate module-locals).
        """
        from strategies.configs import v9_2_eth_solo as solo
        from strategies.configs import v9_2_eth_late_band_AB_blend as late_band

        solo._consec_state.clear()
        late_band._consec_state.clear()
        assert solo._consec_state is not late_band._consec_state

        # Fire solo — populates ITS state only.
        surface = _make_surface(probability_lgb_v9_2_eth=0.82)
        with _params():
            d = evaluate_v9_2_eth_solo(surface)
        assert d.action == "TRADE"
        assert len(solo._consec_state) >= 1
        # late_band's state remains untouched.
        assert len(late_band._consec_state) == 0

    def test_consec_state_isolated_from_raw_lgb(self):
        """Module-local state is isolated from v9_2_eth_raw_lgb."""
        from strategies.configs import v9_2_eth_solo as solo
        from strategies.configs import v9_2_eth_raw_lgb as raw_lgb

        solo._consec_state.clear()
        raw_lgb._consec_state.clear()
        assert solo._consec_state is not raw_lgb._consec_state

        surface = _make_surface(probability_lgb_v9_2_eth=0.82)
        with _params():
            evaluate_v9_2_eth_solo(surface)
        assert len(raw_lgb._consec_state) == 0
