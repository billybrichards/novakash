"""Unit tests for v9_3_btc_down_solo GHOST strategy.

Coverage:
- model-not-loaded SKIP (v9_3_btc_model_not_loaded) — None probability
- defensive asset guard (SKIP on non-BTC surface — wrong_asset)
- eval_offset outside band [60, 180] (outside_eval_band)
- DOWN threshold gating: fires at p=0.50, fires below, just above skips
- No UP fires (conviction_below_threshold for all p > down_threshold)
- Direction-aware fill-band gate:
    DOWN + fill=0.80 (< 0.90)  → TRADE
    DOWN + fill=0.91 (>= 0.90) → SKIP fill_above_down_cap
    DOWN + fill=0.90 (exactly) → SKIP (cap is >= exclusive)
    None fill bypasses gate     → TRADE
- gate_params runtime override respected
- Metadata shape on TRADE includes probability + threshold + strategy_id
- Consecutive ticks (min_consecutive_pass_ticks) gating
- Cross-strategy independence: module-local _consec_state doesn't leak
  into sibling strategies (v9_3_btc_blend, v9_3_btc_tight_blend)

Test strategy: synthetic _make_surface() fixture; uses _gp.set_active /
_gp.reset_active (same pattern as test_v9_2_eth_solo.py).
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_3_btc_down_solo import evaluate_v9_3_btc_down_solo
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# ── Surface factory ────────────────────────────────────────────────────────

def _make_surface(**overrides) -> FullDataSurface:
    """Default surface for v9_3_btc_down_solo tests.

    asset=BTC, eval_offset=120 (in band [60, 180]),
    probability_lgb_v9_3_btc=0.40 (below the 0.50 DOWN threshold).
    clob_implied_up=0.75 (below entry_cap_down=0.90 — DOWN fires through).
    """
    defaults = dict(
        asset="BTC", timescale="5m",
        window_ts=1713009600,
        eval_offset=120, assembled_at=time.time(),
        current_price=68000.0, open_price=67800.0,
        delta_binance=0.003, delta_tiingo=0.003, delta_chainlink=0.003,
        delta_pct=0.003, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=0.002,
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
        clob_down_ask=0.30, clob_implied_up=0.75,
        gamma_up_price=0.65, gamma_down_price=0.35,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0, cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=12, seconds_to_close=120,
        probability_lgb_v9_3_btc=0.40,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ── Gate-params helpers ────────────────────────────────────────────────────

_BASE_PARAMS: dict[str, Any] = {
    "down_threshold": 0.50,
    "eval_offset_min": 60,
    "eval_offset_max": 180,
    "min_consecutive_pass_ticks": 1,
    "expected_asset": "BTC",
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
    from strategies.configs import v9_3_btc_down_solo
    v9_3_btc_down_solo._consec_state.clear()
    yield
    v9_3_btc_down_solo._consec_state.clear()


# ── Null probability handling ──────────────────────────────────────────────

class TestNullProbabilityHandling:
    def test_model_not_loaded_skips_cleanly(self):
        """When the snapshot has no probability_lgb_v9_3_btc, the strategy
        SKIPs with a clear reason — no crash."""
        surface = _make_surface(probability_lgb_v9_3_btc=None)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_3_btc_model_not_loaded"
        assert d.strategy_id == "v9_3_btc_down_solo"


# ── Defensive asset guard ──────────────────────────────────────────────────

class TestAssetGuard:
    def test_eth_surface_skips_even_with_low_prob(self):
        """BTC strategy refuses to fire on an ETH surface — defensive guard."""
        surface = _make_surface(asset="ETH", probability_lgb_v9_3_btc=0.10)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_xrp_surface_skips(self):
        surface = _make_surface(asset="XRP", probability_lgb_v9_3_btc=0.10)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_asset_check_runs_before_probability_check(self):
        """Asset check runs first — non-BTC with None prob still wrong_asset."""
        surface = _make_surface(asset="ETH", probability_lgb_v9_3_btc=None)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"


# ── Eval-offset band ───────────────────────────────────────────────────────

class TestEvalOffsetBand:
    def test_below_min_skips(self):
        """eval_offset=59 is below the band min of 60."""
        surface = _make_surface(eval_offset=59)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_above_max_skips(self):
        """eval_offset=181 is above the band max of 180."""
        surface = _make_surface(eval_offset=181)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_at_min_60_fires(self):
        surface = _make_surface(eval_offset=60, probability_lgb_v9_3_btc=0.40)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_at_max_180_fires(self):
        surface = _make_surface(eval_offset=180, probability_lgb_v9_3_btc=0.40)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_none_eval_offset_skips(self):
        surface = _make_surface(eval_offset=None)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_offset_outside_band_skips_even_with_low_prob(self):
        """eval_offset=240 is above the 180 band max — no fire."""
        surface = _make_surface(eval_offset=240, probability_lgb_v9_3_btc=0.10)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


# ── DOWN threshold gating ──────────────────────────────────────────────────

class TestDownThreshold:
    def test_down_fires_at_threshold_0_50(self):
        """Exactly at DOWN threshold 0.50 — fires DOWN."""
        surface = _make_surface(probability_lgb_v9_3_btc=0.50)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"
        assert d.strategy_id == "v9_3_btc_down_solo"
        assert d.entry_reason == "v9_3_btc_down_solo_pass"

    def test_down_fires_at_0_49(self):
        """p=0.49 < 0.50 — fires DOWN."""
        surface = _make_surface(probability_lgb_v9_3_btc=0.49)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_down_fires_well_below_threshold(self):
        surface = _make_surface(probability_lgb_v9_3_btc=0.10)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_just_above_down_threshold_skips(self):
        """0.501 > 0.50 — above DOWN threshold, skips."""
        surface = _make_surface(probability_lgb_v9_3_btc=0.501)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_neutral_0_60_skips(self):
        surface = _make_surface(probability_lgb_v9_3_btc=0.60)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── No UP fires (DOWN-only strategy) ──────────────────────────────────────

class TestNoUpFires:
    def test_high_prob_does_not_fire_up(self):
        """p=0.90 is above 0.50 — no UP direction, skips."""
        surface = _make_surface(probability_lgb_v9_3_btc=0.90)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"
        assert d.direction is None

    def test_p_0_72_does_not_fire_up(self):
        """p=0.72 — the v9_3_btc_blend UP threshold, but DOWN-only skips."""
        surface = _make_surface(probability_lgb_v9_3_btc=0.72)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_p_0_99_skips(self):
        """p=0.99 maximum — still skips in DOWN-only strategy."""
        surface = _make_surface(probability_lgb_v9_3_btc=0.99)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── Direction-aware fill-band gate (RDS note #664) ─────────────────────────

class TestFillBandGate:
    def test_down_fires_with_fill_0_80_below_cap(self):
        """DOWN at p=0.40 + fill=0.80 (< 0.90 cap) → TRADE."""
        surface = _make_surface(
            probability_lgb_v9_3_btc=0.40,
            eval_offset=120,
            clob_implied_up=0.80,
        )
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_down_skips_with_fill_0_91_above_cap(self):
        """DOWN at p=0.40 + fill=0.91 (>= 0.90 cap) → SKIP fill_above_down_cap."""
        surface = _make_surface(
            probability_lgb_v9_3_btc=0.40,
            eval_offset=120,
            clob_implied_up=0.91,
        )
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason is not None
        assert "fill_above_down_cap" in d.skip_reason
        assert "0.910" in d.skip_reason
        assert "0.900" in d.skip_reason

    def test_down_cap_exact_boundary_exclusive(self):
        """DOWN at fill=0.90 — at cap boundary, SKIP (cap is exclusive >=)."""
        surface = _make_surface(
            probability_lgb_v9_3_btc=0.40,
            eval_offset=120,
            clob_implied_up=0.90,
        )
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason is not None
        assert "fill_above_down_cap" in d.skip_reason

    def test_none_fill_bypasses_gate(self):
        """When clob_implied_up is None, fill gate is skipped — TRADE."""
        surface = _make_surface(
            probability_lgb_v9_3_btc=0.40,
            clob_implied_up=None,
        )
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_default_permissive_cap_allows_high_fill(self):
        """With entry_cap_down=1.0, all fills pass (no cap filtering)."""
        surface = _make_surface(
            probability_lgb_v9_3_btc=0.40,
            clob_implied_up=0.95,
        )
        with _params(entry_cap_down=1.0):
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"


# ── Runtime override ───────────────────────────────────────────────────────

class TestRuntimeOverride:
    def test_runtime_override_tightens_down_threshold(self):
        surface = _make_surface(probability_lgb_v9_3_btc=0.45)
        # Default 0.50 — TRADE.
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "TRADE"
        # Tighten to 0.40 — p=0.45 is now > 0.40, SKIP.
        with _params(down_threshold=0.40):
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_runtime_override_narrows_eval_band(self):
        surface = _make_surface(eval_offset=170, probability_lgb_v9_3_btc=0.40)
        # Default band [60, 180] — TRADE.
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "TRADE"
        # Narrow to [60, 150] — offset 170 now out of band.
        with _params(eval_offset_max=150):
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"


# ── Metadata shape ─────────────────────────────────────────────────────────

class TestMetadataShape:
    def test_metadata_contains_probability_on_down_trade(self):
        surface = _make_surface(probability_lgb_v9_3_btc=0.40, eval_offset=120)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "TRADE"
        assert d.metadata["probability_lgb_v9_3_btc"] == pytest.approx(0.40)
        assert d.metadata["down_threshold"] == pytest.approx(0.50)
        assert d.metadata["eval_offset_min"] == 60
        assert d.metadata["eval_offset_max"] == 180
        assert d.metadata["eval_offset"] == 120
        assert d.metadata["asset"] == "BTC"
        assert d.metadata["strategy_id"] == "v9_3_btc_down_solo"
        assert d.metadata["strategy_version"] == "1.0.0"

    def test_metadata_contains_sizing_on_trade(self):
        surface = _make_surface(probability_lgb_v9_3_btc=0.40)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "TRADE"
        assert d.metadata["entry_cap"] == pytest.approx(0.93)
        assert d.metadata["collateral_pct"] == pytest.approx(0.025)
        assert d.metadata["gtc_cap"] == pytest.approx(0.96)

    def test_confidence_score_uses_distance_from_neutral(self):
        # p=0.40 -> |0.40 - 0.5| * 2 = 0.20 -> MODERATE (< 0.40)
        surface = _make_surface(probability_lgb_v9_3_btc=0.40)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "TRADE"
        assert d.confidence == "MODERATE"
        assert d.confidence_score == pytest.approx(0.20)

    def test_confidence_high_for_extreme_prob(self):
        # p=0.10 -> |0.10 - 0.5| * 2 = 0.80 -> HIGH (>= 0.40)
        surface = _make_surface(probability_lgb_v9_3_btc=0.10)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "TRADE"
        assert d.confidence == "HIGH"
        assert d.confidence_score == pytest.approx(0.80)

    def test_metadata_includes_direction_on_trade(self):
        surface = _make_surface(probability_lgb_v9_3_btc=0.40, eval_offset=120)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "TRADE"
        assert d.metadata["direction"] == "DOWN"

    def test_metadata_includes_fill_price_and_cap_when_available(self):
        surface = _make_surface(
            probability_lgb_v9_3_btc=0.40,
            clob_implied_up=0.75,
        )
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "TRADE"
        assert d.metadata.get("fill_price") == pytest.approx(0.75)
        assert d.metadata.get("entry_cap_down") == pytest.approx(0.90)


# ── Consecutive ticks ──────────────────────────────────────────────────────

class TestConsecutiveTicks:
    def test_default_one_tick_fires_immediately(self):
        """min_consecutive_pass_ticks=1 (default) → one qualifying tick fires."""
        surface = _make_surface(probability_lgb_v9_3_btc=0.40)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"
        assert d.metadata["consec_tick_count"] == 1
        assert d.metadata["min_consecutive_pass_ticks"] == 1

    def test_runtime_override_requires_two_ticks(self):
        """Runtime override min_consecutive_pass_ticks=2 → first tick SKIPs."""
        surface = _make_surface(probability_lgb_v9_3_btc=0.40)
        with _params(min_consecutive_pass_ticks=2):
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "SKIP"
        assert "awaiting_consec_ticks" in (d.skip_reason or "")
        # Second consecutive tick should TRADE.
        with _params(min_consecutive_pass_ticks=2):
            d2 = evaluate_v9_3_btc_down_solo(surface)
        assert d2.action == "TRADE"
        assert d2.direction == "DOWN"


# ── Cross-strategy independence ────────────────────────────────────────────

class TestCrossStrategyIndependence:
    """Module-local _consec_state must NOT leak to sibling strategies."""

    def test_consec_state_isolated_from_btc_blend(self):
        """Mutating v9_3_btc_down_solo._consec_state must NOT affect
        v9_3_btc_blend._consec_state (separate module-locals).
        """
        from strategies.configs import v9_3_btc_down_solo as down_solo
        from strategies.configs import v9_3_btc_blend as btc_blend

        down_solo._consec_state.clear()
        btc_blend._consec_state.clear()
        assert down_solo._consec_state is not btc_blend._consec_state

        surface = _make_surface(probability_lgb_v9_3_btc=0.40)
        with _params():
            d = evaluate_v9_3_btc_down_solo(surface)
        assert d.action == "TRADE"
        assert len(down_solo._consec_state) >= 1
        # btc_blend's state remains untouched.
        assert len(btc_blend._consec_state) == 0

    def test_consec_state_isolated_from_tight_blend(self):
        """Module-local state is isolated from v9_3_btc_tight_blend."""
        from strategies.configs import v9_3_btc_down_solo as down_solo
        from strategies.configs import v9_3_btc_tight_blend as tight

        down_solo._consec_state.clear()
        tight._consec_state.clear()
        assert down_solo._consec_state is not tight._consec_state

        surface = _make_surface(probability_lgb_v9_3_btc=0.40)
        with _params():
            evaluate_v9_3_btc_down_solo(surface)
        assert len(tight._consec_state) == 0
