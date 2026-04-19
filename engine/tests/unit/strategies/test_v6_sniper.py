"""Tests for v6_sniper — bidirectional ensemble sniper.

Covers the 14 spec test cases:
  1-4  agree_strong bucket (accept + reject paths)
  5-8  pegged_path1 bucket (LGB-indifferent / agree / opposite / DOWN extreme)
  9    mid_conf block
  10   no_eval block (path1=None)
  11   vpin_min floor
  12   blocked_utc_hours
  13   source_agreement (chainlink/tiingo disagree)
  14   prefer_raw_probability → metadata records raw source

Strategy hooks live in ``engine/strategies/configs/v6_sniper.py`` and are
loaded via importlib by ``StrategyRegistry``. We exercise them through
the registry the same way ``test_v5_ensemble.py`` does so the real load
path is under test, not the function in isolation.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.data_surface import DataSurfaceManager, FullDataSurface
from strategies.registry import StrategyRegistry

CONFIGS_DIR = str(
    Path(__file__).resolve().parents[3] / "strategies" / "configs"
)


# ── Fixtures ────────────────────────────────────────────────────────────────
def _make_surface(**overrides) -> FullDataSurface:
    """Default surface that lands inside v6_sniper's accept window.

    Direction=DOWN, both models agree, |dist|=0.22 → agree_strong bucket.
    UTC hour=12 (not blocked). VPIN=0.55 (above floor). Both sources
    present and agree.
    """
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713010800,  # 12:00 UTC
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=85000.0,
        delta_binance=-0.005, delta_tiingo=-0.004, delta_chainlink=-0.005,
        delta_pct=-0.005, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=-0.003,
        v2_probability_up=0.28, v2_probability_raw=0.28,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        # Path 1 ensemble — both DOWN, |p_up - 0.5| = 0.22
        probability_lgb=0.30, probability_classifier=0.28,
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
        poly_direction="DOWN", poly_trade_advised=True, poly_confidence=0.28,
        poly_confidence_distance=0.22, poly_timing="optimal",
        poly_max_entry_price=0.65, poly_reason="strong_signal",
        v4_recommended_side="DOWN", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.46, clob_up_ask=0.48, clob_down_bid=0.52,
        clob_down_ask=0.54, clob_implied_up=0.47,
        gamma_up_price=0.45, gamma_down_price=0.55,
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
    return reg


def _evaluate(registry, surface):
    return registry._evaluate_one(
        "v6_sniper", registry.configs["v6_sniper"], surface
    )


# ── Registry load sanity ────────────────────────────────────────────────────
def test_v6_sniper_registered_as_live(registry):
    assert "v6_sniper" in registry.strategy_names
    cfg = registry.configs["v6_sniper"]
    assert cfg.mode == "LIVE"
    assert cfg.version == "6.0.1"
    assert cfg.timescale == "5m"


# ── #1-#4 agree_strong bucket ──────────────────────────────────────────────
def test_1_agree_strong_both_up_accepts(registry):
    """Both models agree UP + dist=0.25 -> ACCEPT."""
    surface = _make_surface(
        poly_direction="UP", poly_confidence=0.75,
        poly_confidence_distance=0.25,
        probability_lgb=0.72, probability_classifier=0.78,
        delta_chainlink=+0.005, delta_tiingo=+0.004, delta_binance=+0.005,
        v4_recommended_side="UP",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE", (
        f"expected TRADE, got {decision.action} skip_reason={decision.skip_reason}"
    )
    assert decision.direction == "UP"
    assert decision.metadata["conviction_bucket"] == "agree_strong"


def test_2_agree_strong_both_down_accepts(registry):
    """Both models agree DOWN + dist=0.22 -> ACCEPT."""
    surface = _make_surface(
        poly_direction="DOWN", poly_confidence=0.28,
        probability_lgb=0.30, probability_classifier=0.26,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE"
    assert decision.direction == "DOWN"
    assert decision.metadata["conviction_bucket"] == "agree_strong"


def test_3_agree_strong_models_disagree_rejects(registry):
    """Models disagree + dist=0.25 -> REJECT (fall through to mid_conf block).

    probability_up=0.25 (DOWN), LGB=0.70 (UP). LGB opposes trade direction
    AND is not pegged, so bucket is neither agree_strong nor pegged_path1
    → mid_conf_blocked.
    """
    surface = _make_surface(
        poly_direction="DOWN", poly_confidence=0.25,
        probability_lgb=0.70, probability_classifier=0.25,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"
    assert "mid_conf" in (decision.skip_reason or "")


def test_4_agree_strong_dist_too_low_rejects(registry):
    """Both agree UP but dist=0.15 -> below strong threshold → mid_conf block."""
    surface = _make_surface(
        poly_direction="UP", poly_confidence=0.65,
        poly_confidence_distance=0.15,
        probability_lgb=0.63, probability_classifier=0.68,
        delta_chainlink=+0.005, delta_tiingo=+0.004, delta_binance=+0.005,
        v4_recommended_side="UP",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"
    assert "mid_conf" in (decision.skip_reason or "")


# ── #5-#8 pegged_path1 bucket ──────────────────────────────────────────────
def test_5_pegged_path1_lgb_indifferent_accepts(registry):
    """path1=0.97 + LGB=0.52 (indifferent) -> ACCEPT (relaxed).

    |p_lgb - 0.5| = 0.02 < 0.10 opposite-block threshold, and even if LGB
    were technically "DOWN" it's not strong enough to block a pegged UP
    path1.
    """
    # probability_up = 0.97 → dist = 0.47 — also satisfies agree_strong if
    # models agreed, but LGB=0.52 (indifferent) means we fall into the
    # pegged_path1 branch which is checked FIRST by _classify_bucket.
    surface = _make_surface(
        poly_direction="UP", poly_confidence=0.97,
        probability_lgb=0.52, probability_classifier=0.97,
        delta_chainlink=+0.01, delta_tiingo=+0.009, delta_binance=+0.01,
        v4_recommended_side="UP",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE", (
        f"skip_reason={decision.skip_reason}"
    )
    assert decision.metadata["conviction_bucket"] == "pegged_path1"


def test_6_pegged_path1_lgb_agrees_accepts(registry):
    """path1=0.97 + LGB=0.65 (agrees) -> ACCEPT."""
    surface = _make_surface(
        poly_direction="UP", poly_confidence=0.97,
        probability_lgb=0.65, probability_classifier=0.97,
        delta_chainlink=+0.01, delta_tiingo=+0.009, delta_binance=+0.01,
        v4_recommended_side="UP",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE"
    assert decision.metadata["conviction_bucket"] == "pegged_path1"


def test_7_pegged_path1_lgb_strongly_opposite_rejects(registry):
    """path1=0.97 (UP) + LGB=0.30 (DOWN, |dist|=0.20 > 0.10) -> REJECT."""
    surface = _make_surface(
        poly_direction="UP", poly_confidence=0.97,
        probability_lgb=0.30, probability_classifier=0.97,
        delta_chainlink=+0.01, delta_tiingo=+0.009, delta_binance=+0.01,
        v4_recommended_side="UP",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"
    assert "pegged_path1_blocked_by_lgb" in (decision.skip_reason or "")


def test_8_pegged_path1_down_lgb_indifferent_accepts(registry):
    """path1=0.03 + LGB=0.48 (indifferent, DOWN side) -> ACCEPT (relaxed)."""
    surface = _make_surface(
        poly_direction="DOWN", poly_confidence=0.03,
        probability_lgb=0.48, probability_classifier=0.03,
        delta_chainlink=-0.01, delta_tiingo=-0.009, delta_binance=-0.01,
        v4_recommended_side="DOWN",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE"
    assert decision.direction == "DOWN"
    assert decision.metadata["conviction_bucket"] == "pegged_path1"


# ── #9 mid_conf block ──────────────────────────────────────────────────────
def test_9_mid_conf_blocked(registry):
    """path1=0.65, lgb=0.62, dist=0.14 -> REJECT (neither strong nor pegged)."""
    surface = _make_surface(
        poly_direction="UP", poly_confidence=0.64,
        poly_confidence_distance=0.14,
        probability_lgb=0.62, probability_classifier=0.65,
        delta_chainlink=+0.002, delta_tiingo=+0.002, delta_binance=+0.002,
        v4_recommended_side="UP",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"
    assert "mid_conf" in (decision.skip_reason or "")
    assert decision.metadata["conviction_bucket"] == "mid_conf_blocked"


# ── #10 no_eval block ──────────────────────────────────────────────────────
def test_10_no_eval_blocked_when_path1_none(registry):
    """path1=None -> REJECT on path1 freshness gate."""
    surface = _make_surface(
        poly_direction="DOWN", poly_confidence=0.28,
        probability_lgb=0.30, probability_classifier=None,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"
    assert "no_eval_blocked" in (decision.skip_reason or "")
    assert decision.metadata["conviction_bucket"] == "no_eval_blocked"


# ── #11 vpin_min ───────────────────────────────────────────────────────────
def test_11_vpin_below_floor_rejects(registry):
    """vpin=0.40 -> REJECT (below 0.45 floor)."""
    surface = _make_surface(vpin=0.40)
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"
    assert "vpin_too_low" in (decision.skip_reason or "")


# ── #12 blocked_utc_hours (v6.0.1: disabled by default) ────────────────────
def test_12_blocked_utc_hours_disabled_by_default(registry):
    """v6.0.1 sets blocked_utc_hours=[] in YAML (removed per Billy). A
    window at 07:30 UTC should NOT be skipped for time-of-day reasons
    anymore. Regression guard so a later PR that sets the YAML back to
    [7,8,9] shows up as a test diff rather than silent reactivation.
    """
    window_ts = int(
        _dt.datetime(2026, 4, 19, 7, 30, tzinfo=_dt.timezone.utc).timestamp()
    )
    surface = _make_surface(window_ts=window_ts, hour_utc=7)
    decision = _evaluate(registry, surface)
    # Default happy-path surface → should TRADE with no time-of-day skip.
    assert decision.action == "TRADE", (
        f"expected TRADE, got {decision.action} skip_reason={decision.skip_reason}"
    )
    # Sanity: skip_reason shouldn't contain "blocked_utc" for any reason.
    assert "blocked_utc" not in (decision.skip_reason or "")


# ── #13 source_agreement ───────────────────────────────────────────────────
def test_13_source_disagreement_rejects(registry):
    """chainlink UP, tiingo DOWN -> REJECT."""
    surface = _make_surface(
        delta_chainlink=+0.005, delta_tiingo=-0.004,
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"
    assert "source_disagreement" in (decision.skip_reason or "")


# ── #14 prefer_raw_probability ─────────────────────────────────────────────
def test_14_prefer_raw_records_raw_source(registry):
    """With prefer_raw_probability=true (YAML default), the hook reads raw
    poly_confidence and records read_probability_source='raw' in
    metadata.

    This test only verifies the raw path because the calibrated field
    (``surface.probability_up_calibrated``) is not yet on FullDataSurface
    — it arrives with engine-side PR #281. Until then, prefer_raw=false
    would fall back to raw anyway, so the test of mode A (raw) is the
    load-bearing case.
    """
    surface = _make_surface(
        poly_direction="UP", poly_confidence=0.75,
        probability_lgb=0.72, probability_classifier=0.78,
        delta_chainlink=+0.005, delta_tiingo=+0.004, delta_binance=+0.005,
        v4_recommended_side="UP",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE"
    assert decision.metadata["read_probability_source"] == "raw"
    assert decision.metadata["probability_raw"] == pytest.approx(0.75)
    # calibrated field absent today → logged as None.
    assert decision.metadata["probability_calibrated"] is None
    # Bucket carries the winning decision.
    assert decision.metadata["conviction_bucket"] == "agree_strong"


# ── Mode-flip regression (change 2 + 3) ────────────────────────────────────
def test_v5_ensemble_flipped_to_ghost(registry):
    assert registry.configs["v5_ensemble"].mode == "GHOST"


def test_v5_fresh_flipped_to_ghost(registry):
    assert registry.configs["v5_fresh"].mode == "GHOST"


def test_v4_fusion_flipped_to_live(registry):
    assert registry.configs["v4_fusion"].mode == "LIVE"


def test_v4_down_only_stays_ghost(registry):
    """Spec: do NOT touch v4_down_only — stays GHOST."""
    assert registry.configs["v4_down_only"].mode == "GHOST"


# ── v6.0.1 additions: risk_off regime pass + pegged boundary tests ─────────
def test_15_risk_off_regime_accepts(registry):
    """v6.0.1 adds risk_off to tradeable_v4_regimes. Regression test:
    a risk_off window with otherwise-passing gates should TRADE, not skip.
    """
    surface = _make_surface(v4_regime="risk_off")
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE", (
        f"expected TRADE, got {decision.action} skip_reason={decision.skip_reason}"
    )


def test_15b_chop_regime_still_rejected(registry):
    """chop stays blocked per Billy (only calm_trend / volatile_trend /
    risk_off in the allowlist)."""
    surface = _make_surface(v4_regime="chop")
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"
    assert "regime_not_tradeable" in (decision.skip_reason or "")


def test_16_pegged_high_boundary_just_inside(registry):
    """path1=0.91 + LGB=0.50 -> ACCEPT (just inside relaxed 0.90 threshold)."""
    surface = _make_surface(
        poly_direction="UP", poly_confidence=0.91,
        probability_lgb=0.50, probability_classifier=0.91,
        delta_chainlink=+0.01, delta_tiingo=+0.009, delta_binance=+0.01,
        v4_recommended_side="UP",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE", (
        f"skip_reason={decision.skip_reason}"
    )
    assert decision.metadata["conviction_bucket"] == "pegged_path1"


def test_17_pegged_high_boundary_just_outside(registry):
    """path1=0.89 + LGB=0.50 (indifferent) -> neither pegged nor agree_strong,
    falls into mid_conf block."""
    surface = _make_surface(
        poly_direction="UP", poly_confidence=0.70,
        poly_confidence_distance=0.20,
        probability_lgb=0.50, probability_classifier=0.89,
        delta_chainlink=+0.005, delta_tiingo=+0.004, delta_binance=+0.005,
        v4_recommended_side="UP",
    )
    decision = _evaluate(registry, surface)
    # 0.89 < 0.90 pegged threshold; LGB=0.50 means models don't agree on
    # direction with any force → agree_strong fails too. mid_conf fallback.
    assert decision.action == "SKIP"


def test_18_pegged_low_boundary_just_inside(registry):
    """path1=0.09 + LGB=0.50 (indifferent) -> ACCEPT (just inside 0.10 threshold)."""
    surface = _make_surface(
        poly_direction="DOWN", poly_confidence=0.09,
        probability_lgb=0.50, probability_classifier=0.09,
        delta_chainlink=-0.01, delta_tiingo=-0.009, delta_binance=-0.01,
        v4_recommended_side="DOWN",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE", (
        f"skip_reason={decision.skip_reason}"
    )
    assert decision.direction == "DOWN"
    assert decision.metadata["conviction_bucket"] == "pegged_path1"


def test_19_pegged_low_boundary_just_outside(registry):
    """path1=0.11 + LGB=0.45 + blended dist=0.05 -> neither pegged
    (path1 > 0.10) nor agree_strong (dist < 0.20) -> mid_conf block.
    """
    surface = _make_surface(
        poly_direction="DOWN", poly_confidence=0.45,
        poly_confidence_distance=0.05,
        probability_lgb=0.45, probability_classifier=0.11,
        delta_chainlink=-0.005, delta_tiingo=-0.004, delta_binance=-0.005,
        v4_recommended_side="DOWN",
    )
    decision = _evaluate(registry, surface)
    assert decision.action == "SKIP"


def test_20_entry_cap_override_applied(registry):
    """v6.0.1: entry_cap_override=0.85 in YAML overrides surface.poly_max_entry_price."""
    surface = _make_surface(poly_max_entry_price=0.65)  # surface default < override
    decision = _evaluate(registry, surface)
    assert decision.action == "TRADE"
    assert decision.entry_cap == pytest.approx(0.85), (
        f"expected 0.85, got {decision.entry_cap}"
    )
