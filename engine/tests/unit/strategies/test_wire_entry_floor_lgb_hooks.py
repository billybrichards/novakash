"""Regression tests: entry_floor_up / entry_floor_down gate wired into 10 hooks.

Bug: Several strategy hooks never called _gp.get_float("entry_floor_up", ...) so
runtime overrides of entry_floor_up were silently ignored — sub-floor fills could
be approved at decision time. The 2026-06-01 trades (9622, 9633) demonstrated
the consequence.

Fix (PR #2): Wire the fill-band gate block (copied from v9_2_eth_late_band_AB_blend)
into all 10 unwired hooks. The block reads entry_floor_up / entry_cap_down /
entry_floor_down from gate_params, propagates them into decision metadata, and
skips if the decision-time fill would violate a floor.

Tests in this file verify:
  1. Each hook SKIPs on a sub-floor decision-time fill (entry_floor_up overridden).
  2. Each hook does NOT skip when no floor is configured (default 0.0 = permissive).
  3. The skip populates meta with fill_price, entry_floor_up keys.
  4. Hooks that fire UP-only still skip on sub-floor fill (UP direction).
  5. Hooks that fire DOWN-only skip when entry_cap_down is overridden to below fill.
  6. entry_floor_down gate works for DOWN-side fills on bidirectional hooks.

Strategy hooks under test:
  v9_5_eth_pure_lgb, v9_5_eth_blend,
  v9_5_eth_blend_up_low, v9_5_eth_blend_up_mid, v9_5_eth_blend_up_high,
  v9_5_eth_blend_down_low, v9_5_eth_blend_down_mid, v9_5_eth_blend_down_high,
  v9_5_xrp_blend, v9_5_xrp_tight_blend, v9_5_xrp_pure_lgb
"""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# ─── Surface factories ─────────────────────────────────────────────────────


def _eth_surface(**overrides) -> FullDataSurface:
    """Minimal ETH surface for the v9_5 ETH strategy tests."""
    defaults = dict(
        asset="ETH", timescale="5m",
        window_ts=1780000000,
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
        clob_down_ask=0.30, clob_implied_up=0.50,  # set low so floor tests are unambiguous
        gamma_up_price=0.65, gamma_down_price=0.35,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0, cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=12, seconds_to_close=120,
        # ETH probability columns
        probability_lgb_v9_5_eth=0.97,
        probability_lgb_v9_5_eth_pure=0.93,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


def _xrp_surface(**overrides) -> FullDataSurface:
    """Minimal XRP surface for the v9_5 XRP strategy tests."""
    base = _eth_surface(
        asset="XRP",
        clob_implied_up=0.50,
        probability_lgb_v9_5_xrp=0.90,
        probability_lgb_v9_5_xrp_pure=0.93,
    )
    # Apply overrides on top
    for k, v in overrides.items():
        object.__setattr__(base, k, v)
    return base


@contextmanager
def _params(params: dict):
    token = _gp.set_active(params)
    try:
        yield
    finally:
        _gp.reset_active(token)


# ─── v9_5_eth_pure_lgb ───────────────────────────────────────────────────────


class TestV95EthPureLgbFloorGate:
    """Fill-band gate is wired into v9_5_eth_pure_lgb."""

    def setup_method(self):
        from strategies.configs import v9_5_eth_pure_lgb
        v9_5_eth_pure_lgb._consec_state.clear()

    def test_up_fill_below_floor_skips(self):
        """entry_floor_up=0.60 overridden, clob_implied_up=0.50 → SKIP."""
        from strategies.configs.v9_5_eth_pure_lgb import evaluate_v9_5_eth_pure_lgb
        surface = _eth_surface(clob_implied_up=0.50)
        with _params({
            "expected_asset": "ETH", "up_threshold": 0.915, "down_threshold": 0.095,
            "eval_offset_min": 60, "eval_offset_max": 210,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.93, "collateral_pct": 0.025, "gtc_cap": 0.96,
            "entry_floor_up": 0.60,
        }):
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "SKIP"
        assert "fill_below_up_floor" in (d.skip_reason or "")

    def test_up_fill_above_floor_trades(self):
        """clob_implied_up=0.75, entry_floor_up=0.60 → TRADE (no regression)."""
        from strategies.configs.v9_5_eth_pure_lgb import evaluate_v9_5_eth_pure_lgb
        surface = _eth_surface(clob_implied_up=0.75)
        with _params({
            "expected_asset": "ETH", "up_threshold": 0.915, "down_threshold": 0.095,
            "eval_offset_min": 60, "eval_offset_max": 210,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.93, "collateral_pct": 0.025, "gtc_cap": 0.96,
            "entry_floor_up": 0.60,
        }):
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"

    def test_no_floor_no_regression(self):
        """Default floor=0.0 → TRADE even when fill is low."""
        from strategies.configs.v9_5_eth_pure_lgb import evaluate_v9_5_eth_pure_lgb
        surface = _eth_surface(clob_implied_up=0.01)
        with _params({
            "expected_asset": "ETH", "up_threshold": 0.915, "down_threshold": 0.095,
            "eval_offset_min": 60, "eval_offset_max": 210,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.93, "collateral_pct": 0.025, "gtc_cap": 0.96,
            # no entry_floor_up → default 0.0
        }):
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"

    def test_floor_propagated_to_meta(self):
        """entry_floor_up must appear in decision metadata for execute_trade."""
        from strategies.configs.v9_5_eth_pure_lgb import evaluate_v9_5_eth_pure_lgb
        surface = _eth_surface(clob_implied_up=0.75)
        with _params({
            "expected_asset": "ETH", "up_threshold": 0.915, "down_threshold": 0.095,
            "eval_offset_min": 60, "eval_offset_max": 210,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.93, "collateral_pct": 0.025, "gtc_cap": 0.96,
            "entry_floor_up": 0.55,
        }):
            d = evaluate_v9_5_eth_pure_lgb(surface)
        assert d.action == "TRADE"
        assert d.metadata.get("entry_floor_up") == 0.55
        assert "fill_price" in d.metadata


# ─── v9_5_eth_blend ──────────────────────────────────────────────────────────


class TestV95EthBlendFloorGate:
    def setup_method(self):
        from strategies.configs import v9_5_eth_blend
        v9_5_eth_blend._consec_state.clear()

    def test_floor_blocks_sub_floor_fill(self):
        from strategies.configs.v9_5_eth_blend import evaluate_v9_5_eth_blend
        surface = _eth_surface(clob_implied_up=0.50, probability_lgb_v9_5_eth=0.97)
        with _params({
            "expected_asset": "ETH", "up_threshold": 0.96, "down_threshold": 0.04,
            "eval_offset_min": 60, "eval_offset_max": 240,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.85, "collateral_pct": 0.025, "gtc_cap": 0.90,
            "entry_floor_up": 0.60,
        }):
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "SKIP"
        assert "fill_below_up_floor" in (d.skip_reason or "")

    def test_no_floor_no_regression(self):
        from strategies.configs.v9_5_eth_blend import evaluate_v9_5_eth_blend
        surface = _eth_surface(clob_implied_up=0.01, probability_lgb_v9_5_eth=0.97)
        with _params({
            "expected_asset": "ETH", "up_threshold": 0.96, "down_threshold": 0.04,
            "eval_offset_min": 60, "eval_offset_max": 240,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.85, "collateral_pct": 0.025, "gtc_cap": 0.90,
        }):
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "TRADE"


# ─── v9_5_eth_blend_up_low ───────────────────────────────────────────────────


class TestV95EthBlendUpLowFloorGate:
    def setup_method(self):
        from strategies.configs import v9_5_eth_blend_up_low
        v9_5_eth_blend_up_low._consec_state.clear()

    def test_floor_blocks_sub_floor_fill(self):
        from strategies.configs.v9_5_eth_blend_up_low import evaluate_v9_5_eth_blend_up_low
        # UP-only: p_eth in [0.78, 0.82) triggers UP direction
        surface = _eth_surface(clob_implied_up=0.50, probability_lgb_v9_5_eth=0.80)
        with _params({
            "expected_asset": "ETH", "up_threshold": 0.78, "up_threshold_max": 0.82,
            "eval_offset_min": 60, "eval_offset_max": 240,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.93, "collateral_pct": 0.025, "gtc_cap": 0.96,
            "entry_floor_up": 0.60,
        }):
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "SKIP"
        assert "fill_below_up_floor" in (d.skip_reason or "")

    def test_no_floor_no_regression(self):
        from strategies.configs.v9_5_eth_blend_up_low import evaluate_v9_5_eth_blend_up_low
        surface = _eth_surface(clob_implied_up=0.01, probability_lgb_v9_5_eth=0.80)
        with _params({
            "expected_asset": "ETH", "up_threshold": 0.78, "up_threshold_max": 0.82,
            "eval_offset_min": 60, "eval_offset_max": 240,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.93, "collateral_pct": 0.025, "gtc_cap": 0.96,
        }):
            d = evaluate_v9_5_eth_blend_up_low(surface)
        assert d.action == "TRADE"


# ─── v9_5_eth_blend_up_mid ───────────────────────────────────────────────────


class TestV95EthBlendUpMidFloorGate:
    def setup_method(self):
        from strategies.configs import v9_5_eth_blend_up_mid
        v9_5_eth_blend_up_mid._consec_state.clear()

    def test_floor_blocks_sub_floor_fill(self):
        from strategies.configs.v9_5_eth_blend_up_mid import evaluate_v9_5_eth_blend_up_mid
        surface = _eth_surface(clob_implied_up=0.50, probability_lgb_v9_5_eth=0.85)
        with _params({
            "expected_asset": "ETH",
            "up_threshold": 0.82, "up_threshold_max": 0.90,
            "eval_offset_min": 60, "eval_offset_max": 240,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.93, "collateral_pct": 0.025, "gtc_cap": 0.96,
            "entry_floor_up": 0.60,
        }):
            d = evaluate_v9_5_eth_blend_up_mid(surface)
        assert d.action == "SKIP"
        assert "fill_below_up_floor" in (d.skip_reason or "")

    def test_no_floor_no_regression(self):
        from strategies.configs.v9_5_eth_blend_up_mid import evaluate_v9_5_eth_blend_up_mid
        surface = _eth_surface(clob_implied_up=0.01, probability_lgb_v9_5_eth=0.85)
        with _params({
            "expected_asset": "ETH",
            "up_threshold": 0.82, "up_threshold_max": 0.90,
            "eval_offset_min": 60, "eval_offset_max": 240,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.93, "collateral_pct": 0.025, "gtc_cap": 0.96,
        }):
            d = evaluate_v9_5_eth_blend_up_mid(surface)
        assert d.action == "TRADE"


# ─── v9_5_eth_blend_up_high ──────────────────────────────────────────────────


class TestV95EthBlendUpHighFloorGate:
    def setup_method(self):
        from strategies.configs import v9_5_eth_blend_up_high
        v9_5_eth_blend_up_high._consec_state.clear()

    def test_floor_blocks_sub_floor_fill(self):
        from strategies.configs.v9_5_eth_blend_up_high import evaluate_v9_5_eth_blend_up_high
        surface = _eth_surface(clob_implied_up=0.50, probability_lgb_v9_5_eth=0.95)
        with _params({
            "expected_asset": "ETH",
            "up_threshold": 0.90, "up_threshold_max": 2.0,
            "eval_offset_min": 60, "eval_offset_max": 240,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.93, "collateral_pct": 0.025, "gtc_cap": 0.96,
            "entry_floor_up": 0.60,
        }):
            d = evaluate_v9_5_eth_blend_up_high(surface)
        assert d.action == "SKIP"
        assert "fill_below_up_floor" in (d.skip_reason or "")

    def test_no_floor_no_regression(self):
        from strategies.configs.v9_5_eth_blend_up_high import evaluate_v9_5_eth_blend_up_high
        surface = _eth_surface(clob_implied_up=0.01, probability_lgb_v9_5_eth=0.95)
        with _params({
            "expected_asset": "ETH",
            "up_threshold": 0.90, "up_threshold_max": 2.0,
            "eval_offset_min": 60, "eval_offset_max": 240,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.93, "collateral_pct": 0.025, "gtc_cap": 0.96,
        }):
            d = evaluate_v9_5_eth_blend_up_high(surface)
        assert d.action == "TRADE"


# ─── v9_5_eth_blend_down_low ─────────────────────────────────────────────────


class TestV95EthBlendDownLowFloorGate:
    def setup_method(self):
        from strategies.configs import v9_5_eth_blend_down_low
        v9_5_eth_blend_down_low._consec_state.clear()

    def test_entry_cap_down_blocks_high_down_fill(self):
        """DOWN-only: entry_cap_down override blocks a high YES price (low NO price)."""
        from strategies.configs.v9_5_eth_blend_down_low import evaluate_v9_5_eth_blend_down_low
        # DOWN fires when p_eth in (0.18, 0.22]. clob_implied_up=0.95 → entry_cap_down
        # default 1.0 allows it. Override entry_cap_down=0.90 → fill 0.95 >= 0.90 → SKIP.
        surface = _eth_surface(clob_implied_up=0.95, probability_lgb_v9_5_eth=0.20)
        with _params({
            "expected_asset": "ETH",
            "down_threshold": 0.22, "down_threshold_min": 0.18,
            "eval_offset_min": 60, "eval_offset_max": 240,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.93, "collateral_pct": 0.025, "gtc_cap": 0.96,
            "entry_cap_down": 0.90,
        }):
            d = evaluate_v9_5_eth_blend_down_low(surface)
        assert d.action == "SKIP"
        assert "fill_above_down_cap" in (d.skip_reason or "")

    def test_no_cap_no_regression(self):
        from strategies.configs.v9_5_eth_blend_down_low import evaluate_v9_5_eth_blend_down_low
        surface = _eth_surface(clob_implied_up=0.50, probability_lgb_v9_5_eth=0.20)
        with _params({
            "expected_asset": "ETH",
            "down_threshold": 0.22, "down_threshold_min": 0.18,
            "eval_offset_min": 60, "eval_offset_max": 240,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.93, "collateral_pct": 0.025, "gtc_cap": 0.96,
        }):
            d = evaluate_v9_5_eth_blend_down_low(surface)
        assert d.action == "TRADE"


# ─── v9_5_eth_blend_down_mid ─────────────────────────────────────────────────


class TestV95EthBlendDownMidFloorGate:
    def setup_method(self):
        from strategies.configs import v9_5_eth_blend_down_mid
        v9_5_eth_blend_down_mid._consec_state.clear()

    def test_entry_cap_down_blocks_high_down_fill(self):
        from strategies.configs.v9_5_eth_blend_down_mid import evaluate_v9_5_eth_blend_down_mid
        surface = _eth_surface(clob_implied_up=0.95, probability_lgb_v9_5_eth=0.10)
        with _params({
            "expected_asset": "ETH",
            "down_threshold": 0.14, "down_threshold_min": 0.08,
            "eval_offset_min": 60, "eval_offset_max": 240,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.93, "collateral_pct": 0.025, "gtc_cap": 0.96,
            "entry_cap_down": 0.90,
        }):
            d = evaluate_v9_5_eth_blend_down_mid(surface)
        assert d.action == "SKIP"
        assert "fill_above_down_cap" in (d.skip_reason or "")

    def test_no_cap_no_regression(self):
        from strategies.configs.v9_5_eth_blend_down_mid import evaluate_v9_5_eth_blend_down_mid
        surface = _eth_surface(clob_implied_up=0.50, probability_lgb_v9_5_eth=0.10)
        with _params({
            "expected_asset": "ETH",
            "down_threshold": 0.14, "down_threshold_min": 0.08,
            "eval_offset_min": 60, "eval_offset_max": 240,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.93, "collateral_pct": 0.025, "gtc_cap": 0.96,
        }):
            d = evaluate_v9_5_eth_blend_down_mid(surface)
        assert d.action == "TRADE"


# ─── v9_5_eth_blend_down_high ────────────────────────────────────────────────


class TestV95EthBlendDownHighFloorGate:
    def setup_method(self):
        from strategies.configs import v9_5_eth_blend_down_high
        v9_5_eth_blend_down_high._consec_state.clear()

    def test_entry_cap_down_blocks_high_down_fill(self):
        from strategies.configs.v9_5_eth_blend_down_high import evaluate_v9_5_eth_blend_down_high
        surface = _eth_surface(clob_implied_up=0.95, probability_lgb_v9_5_eth=0.05)
        with _params({
            "expected_asset": "ETH",
            "down_threshold": 0.12, "down_threshold_min": -1.0,
            "eval_offset_min": 60, "eval_offset_max": 240,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.93, "collateral_pct": 0.025, "gtc_cap": 0.96,
            "entry_cap_down": 0.90,
        }):
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "SKIP"
        assert "fill_above_down_cap" in (d.skip_reason or "")

    def test_no_cap_no_regression(self):
        from strategies.configs.v9_5_eth_blend_down_high import evaluate_v9_5_eth_blend_down_high
        surface = _eth_surface(clob_implied_up=0.50, probability_lgb_v9_5_eth=0.05)
        with _params({
            "expected_asset": "ETH",
            "down_threshold": 0.12, "down_threshold_min": -1.0,
            "eval_offset_min": 60, "eval_offset_max": 240,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.93, "collateral_pct": 0.025, "gtc_cap": 0.96,
        }):
            d = evaluate_v9_5_eth_blend_down_high(surface)
        assert d.action == "TRADE"


# ─── v9_5_xrp_blend ──────────────────────────────────────────────────────────


class TestV95XrpBlendFloorGate:
    def setup_method(self):
        from strategies.configs import v9_5_xrp_blend
        v9_5_xrp_blend._consec_state.clear()

    def test_floor_blocks_sub_floor_up_fill(self):
        from strategies.configs.v9_5_xrp_blend import evaluate_v9_5_xrp_blend
        surface = _xrp_surface(clob_implied_up=0.50, probability_lgb_v9_5_xrp=0.90)
        with _params({
            "expected_asset": "XRP", "up_threshold": 0.82, "down_threshold": 0.20,
            "eval_offset_min": 60, "eval_offset_max": 240,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.85, "collateral_pct": 0.025, "gtc_cap": 0.90,
            "entry_floor_up": 0.60,
        }):
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "SKIP"
        assert "fill_below_up_floor" in (d.skip_reason or "")

    def test_no_floor_no_regression(self):
        from strategies.configs.v9_5_xrp_blend import evaluate_v9_5_xrp_blend
        surface = _xrp_surface(clob_implied_up=0.01, probability_lgb_v9_5_xrp=0.90)
        with _params({
            "expected_asset": "XRP", "up_threshold": 0.82, "down_threshold": 0.20,
            "eval_offset_min": 60, "eval_offset_max": 240,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.85, "collateral_pct": 0.025, "gtc_cap": 0.90,
        }):
            d = evaluate_v9_5_xrp_blend(surface)
        assert d.action == "TRADE"


# ─── v9_5_xrp_tight_blend ────────────────────────────────────────────────────


class TestV95XrpTightBlendFloorGate:
    def setup_method(self):
        from strategies.configs import v9_5_xrp_tight_blend
        v9_5_xrp_tight_blend._consec_state.clear()

    def test_floor_blocks_sub_floor_up_fill(self):
        from strategies.configs.v9_5_xrp_tight_blend import evaluate_v9_5_xrp_tight_blend
        surface = _xrp_surface(
            clob_implied_up=0.50, probability_lgb_v9_5_xrp=0.97,
            eval_offset=180,
        )
        with _params({
            "expected_asset": "XRP", "up_threshold": 0.95, "down_threshold": 0.05,
            "eval_offset_min": 120, "eval_offset_max": 240,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.95, "collateral_pct": 0.025, "gtc_cap": 0.95,
            "entry_floor_up": 0.60,
        }):
            d = evaluate_v9_5_xrp_tight_blend(surface)
        assert d.action == "SKIP"
        assert "fill_below_up_floor" in (d.skip_reason or "")

    def test_no_floor_no_regression(self):
        from strategies.configs.v9_5_xrp_tight_blend import evaluate_v9_5_xrp_tight_blend
        surface = _xrp_surface(
            clob_implied_up=0.01, probability_lgb_v9_5_xrp=0.97,
            eval_offset=180,
        )
        with _params({
            "expected_asset": "XRP", "up_threshold": 0.95, "down_threshold": 0.05,
            "eval_offset_min": 120, "eval_offset_max": 240,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.95, "collateral_pct": 0.025, "gtc_cap": 0.95,
        }):
            d = evaluate_v9_5_xrp_tight_blend(surface)
        assert d.action == "TRADE"


# ─── v9_5_xrp_pure_lgb ───────────────────────────────────────────────────────


class TestV95XrpPureLgbFloorGate:
    def setup_method(self):
        from strategies.configs import v9_5_xrp_pure_lgb
        v9_5_xrp_pure_lgb._consec_state.clear()

    def test_up_fill_below_floor_skips(self):
        """entry_floor_up=0.60 overridden, clob_up_ask=0.50 → SKIP."""
        from strategies.configs.v9_5_xrp_pure_lgb import evaluate_v9_5_xrp_pure_lgb
        surface = _xrp_surface(clob_up_ask=0.50, probability_lgb_v9_5_xrp_pure=0.95)
        with _params({
            "expected_asset": "XRP", "up_threshold": 0.92, "down_threshold": 0.06,
            "eval_offset_min": 60, "eval_offset_max": 180,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.90, "collateral_pct": 0.025, "gtc_cap": 0.96,
            "entry_cap_up": 0.92, "entry_cap_down": 0.85,
            "entry_floor_up": 0.60,
        }):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert "fill_below_up_floor" in (d.skip_reason or "")

    def test_no_floor_no_regression(self):
        from strategies.configs.v9_5_xrp_pure_lgb import evaluate_v9_5_xrp_pure_lgb
        surface = _xrp_surface(clob_up_ask=0.50, probability_lgb_v9_5_xrp_pure=0.95)
        with _params({
            "expected_asset": "XRP", "up_threshold": 0.92, "down_threshold": 0.06,
            "eval_offset_min": 60, "eval_offset_max": 180,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.90, "collateral_pct": 0.025, "gtc_cap": 0.96,
            "entry_cap_up": 0.92, "entry_cap_down": 0.85,
            # no entry_floor_up → default 0.0
        }):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "TRADE"

    def test_down_fill_below_floor_down_skips(self):
        """DOWN direction: entry_floor_down=0.40, clob_down_ask=0.20 → SKIP."""
        from strategies.configs.v9_5_xrp_pure_lgb import evaluate_v9_5_xrp_pure_lgb
        surface = _xrp_surface(clob_down_ask=0.20, probability_lgb_v9_5_xrp_pure=0.03)
        with _params({
            "expected_asset": "XRP", "up_threshold": 0.92, "down_threshold": 0.06,
            "eval_offset_min": 60, "eval_offset_max": 180,
            "min_consecutive_pass_ticks": 1,
            "entry_cap": 0.90, "collateral_pct": 0.025, "gtc_cap": 0.96,
            "entry_cap_up": 0.92, "entry_cap_down": 0.85,
            "entry_floor_down": 0.40,
        }):
            d = evaluate_v9_5_xrp_pure_lgb(surface)
        assert d.action == "SKIP"
        assert "fill_below_down_floor" in (d.skip_reason or "")
