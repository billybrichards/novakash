"""Tests for v6_sniper v6.1.0 — fill-aware sizing + mid-range double-bucket gate.

Data-driven from Hub note #198 (25 trades today, 13W/5L/7 OPEN, +$22.83):
  * Fill 0.60-0.70 bucket = 100% WR (5/5) — pegged_path1 peak, size UP (2.5x).
  * Fill 0.35-0.60 bucket = 60% WR / near-zero net PnL after 7.2% Polymarket
    fees — break-even zone, tighten with BOTH-buckets requirement.

Covers 6 spec test cases:
  1. mid_range_fill_blocks_when_only_one_bucket
  2. mid_range_fill_allows_when_both_buckets
  3. high_fill_bypasses_mid_range_gate
  4. sizing_rung_fill_band_selects_correctly
  5. sizing_rung_fill_band_falls_through_when_outside
  6. version_bumped
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.data_surface import DataSurfaceManager, FullDataSurface
from strategies.registry import SizingResult, StrategyRegistry

CONFIGS_DIR = str(
    Path(__file__).resolve().parents[3] / "strategies" / "configs"
)
V6_YAML = Path(CONFIGS_DIR) / "v6_sniper.yaml"


def _make_surface(**overrides) -> FullDataSurface:
    """Base surface: v6.1.0 accept-path, DOWN direction, high-fill band.

    Defaults put the surface in the agree_strong bucket with fill=0.65
    (above mid-range) so subclasses can flip just the knob they're testing.
    """
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713010800,
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=85000.0,
        delta_binance=-0.005, delta_tiingo=-0.004, delta_chainlink=-0.005,
        delta_pct=-0.005, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=-0.003,
        v2_probability_up=0.25, v2_probability_raw=0.25,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=0.30, probability_classifier=0.25,
        ensemble_config={"mode": "blend"},
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
        poly_direction="DOWN", poly_trade_advised=True, poly_confidence=0.25,
        poly_confidence_distance=0.25, poly_timing="optimal",
        poly_max_entry_price=0.65, poly_reason="strong_signal",
        v4_recommended_side="DOWN", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        # Default: high-fill band (0.65 > mid_range_fill_max=0.60).
        clob_up_bid=0.33, clob_up_ask=0.35, clob_down_bid=0.63,
        clob_down_ask=0.65, clob_implied_up=0.34,
        gamma_up_price=0.35, gamma_down_price=0.65,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0, cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=12, seconds_to_close=120,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


@pytest.fixture
def registry():
    mgr = DataSurfaceManager(v4_base_url="http://fake")
    reg = StrategyRegistry(CONFIGS_DIR, mgr)
    reg.load_all()
    # v6.1.2 introduced Tier-1 gates (require_lgb_aligned, max_head_disagreement,
    # min_confidence_score) that reject many v6.1.0 fixtures (dist=0.25,
    # lgb=0.30). These tests cover v6.1.0's fill-aware sizing + mid-range
    # double-bucket logic, which is independent of Tier-1, so we disable
    # the v6.1.2 gates at fixture scope. v6.1.2 behaviour itself is pinned
    # by ``test_v6_sniper_v6_1_2.py``.
    cfg = reg.configs["v6_sniper"]
    cfg.gate_params["require_lgb_aligned"] = False
    cfg.gate_params["max_head_disagreement"] = 0.0
    cfg.gate_params["min_confidence_score"] = 0.0
    return reg


def _evaluate(registry, surface):
    return registry._evaluate_one(
        "v6_sniper", registry.configs["v6_sniper"], surface
    )


# ── Test 1 ──────────────────────────────────────────────────────────────────
def test_mid_range_fill_blocks_when_only_one_bucket(registry):
    """fill=0.50 in mid-range + agree_strong (dist=0.25) but NOT pegged
    (path1=0.50) → SKIP with reason mid_range_insufficient_conviction.
    """
    surface = _make_surface(
        # agree_strong: LGB + poly agree DOWN, dist=0.25.
        poly_direction="DOWN", poly_confidence=0.25,
        probability_lgb=0.30, probability_classifier=0.50,
        # fill in the break-even zone
        clob_down_ask=0.50, clob_up_ask=0.50,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP", (
        f"expected SKIP, got {decision.action} skip_reason={decision.skip_reason}"
    )
    assert "mid_range_insufficient_conviction" in (decision.skip_reason or ""), (
        f"expected mid_range_insufficient_conviction in skip_reason, "
        f"got {decision.skip_reason}"
    )
    gate_names = [g.get("gate") for g in decision.metadata.get("gate_results", [])]
    assert "mid_range_both_buckets" in gate_names


# ── Test 2 ──────────────────────────────────────────────────────────────────
def test_mid_range_fill_allows_when_both_buckets(registry):
    """fill=0.50 in mid-range + BOTH agree_strong (dist=0.25) AND
    pegged_path1 (path1=0.93) → TRADE.
    """
    surface = _make_surface(
        poly_direction="DOWN", poly_confidence=0.07,
        poly_confidence_distance=0.43,
        probability_lgb=0.10, probability_classifier=0.07,
        # fill in the break-even zone
        clob_down_ask=0.50, clob_up_ask=0.50,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE", (
        f"expected TRADE, got {decision.action} skip_reason={decision.skip_reason}"
    )
    # mid_range_both_buckets gate was exercised and passed.
    gate_names = [g.get("gate") for g in decision.metadata.get("gate_results", [])]
    gate_reasons = {
        g.get("gate"): (g.get("passed"), g.get("reason"))
        for g in decision.metadata.get("gate_results", [])
    }
    assert "mid_range_both_buckets" in gate_names
    passed, reason = gate_reasons["mid_range_both_buckets"]
    assert passed is True
    assert "agree_strong=True" in reason and "pegged=True" in reason


# ── Test 3 ──────────────────────────────────────────────────────────────────
def test_high_fill_bypasses_mid_range_gate(registry):
    """fill=0.65 (above mid_max=0.60) + agree_strong only (path1=0.50)
    → TRADE. High-fill surface bypasses the double-bucket requirement.
    """
    surface = _make_surface(
        poly_direction="DOWN", poly_confidence=0.25,
        probability_lgb=0.30, probability_classifier=0.50,
        # fill above mid-range cap
        clob_down_ask=0.65, clob_up_ask=0.35,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE", (
        f"expected TRADE, got {decision.action} skip_reason={decision.skip_reason}"
    )
    gate_reasons = {
        g.get("gate"): (g.get("passed"), g.get("reason"))
        for g in decision.metadata.get("gate_results", [])
    }
    assert "mid_range_both_buckets" in gate_reasons
    passed, reason = gate_reasons["mid_range_both_buckets"]
    assert passed is True
    # Confirm the gate saw a high fill (> mid_max=0.60) and didn't trip.
    assert "agree_strong=True" in reason and "pegged=False" in reason


# ── Test 4 ──────────────────────────────────────────────────────────────────
def test_sizing_rung_fill_band_selects_correctly(registry):
    """clob_down_ask=0.68 + pegged_path1 → selects the high_conv_fills_0.60+
    rung (2.5x modifier) because fill_band [0.60, 0.75] contains 0.68.
    """
    from strategies.configs.v6_sniper import clob_sizing

    surface = _make_surface(
        poly_direction="DOWN", poly_confidence=0.05,
        probability_lgb=0.10, probability_classifier=0.05,
        clob_down_ask=0.68, clob_up_ask=0.32,
    )
    seed = SizingResult(fraction=0.025, max_collateral_pct=0.08)
    result = clob_sizing(surface, seed)
    assert result.size_modifier == pytest.approx(2.5), (
        f"expected 2.5x, got {result.size_modifier} label={result.label}"
    )
    assert result.label == "high_conv_fills_0.60+", (
        f"expected high_conv_fills_0.60+, got {result.label}"
    )


# ── Test 5 ──────────────────────────────────────────────────────────────────
def test_sizing_rung_fill_band_falls_through_when_outside(registry):
    """clob_down_ask=0.45 (below fill_band 0.60) → first rung skipped,
    falls through to high_conv_TBD (2.0x, no fill_band gate).
    Since 0.45 >= 0.35, the mid_conv_downsized rung is NOT the first match:
    the second rung (threshold 0.55) requires ask >= 0.55, so we fall
    through to mid_conv_downsized (threshold 0.35, 1.0x).

    This test documents the actual behaviour: the threshold comparison
    governs selection once fill_band is satisfied (or absent).
    """
    from strategies.configs.v6_sniper import clob_sizing

    surface = _make_surface(
        poly_direction="DOWN", poly_confidence=0.05,
        probability_lgb=0.10, probability_classifier=0.05,
        clob_down_ask=0.45, clob_up_ask=0.55,
    )
    seed = SizingResult(fraction=0.025, max_collateral_pct=0.08)
    result = clob_sizing(surface, seed)
    # 0.45 is below fill_band [0.60, 0.75] AND below threshold 0.55, so
    # first two rungs skipped. 0.45 >= 0.35 → mid_conv_downsized (1.0x).
    assert result.label == "mid_conv_downsized", (
        f"expected mid_conv_downsized (fell through fill_band and "
        f"high_conv threshold), got {result.label}"
    )
    assert result.size_modifier == pytest.approx(1.0)


# ── Test 6 ──────────────────────────────────────────────────────────────────
def test_version_bumped():
    """YAML version must be at least 6.1.0 (forward-compatible).

    This test pins the v6.1.0 mid-range fill gate changes. v6.1.1 and later
    versions layer on top without removing any of the assertions in this
    file, so we accept any 6.1.x version here.
    """
    data = yaml.safe_load(V6_YAML.read_text())
    version = data["version"]
    assert version.startswith("6.1."), f"expected 6.1.x, got {version}"
