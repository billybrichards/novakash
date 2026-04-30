"""Unit tests for comparison_shadow — Option B meta-strategy hook.

Covers all 6 signal sources + skip paths + metadata enrichment. The hook
itself is pure: a function of (surface, gate_params). No DB, no global
state beyond the contextvar in ``strategies.gate_params``.

Hard contract under test:
  * Signal field selection by ``gate_params.signal_source``.
  * SKIP when the requested source is None.
  * SKIP when ``|p - 0.5| < shadow_conviction_floor`` (default 0.10).
  * TRADE label otherwise (mode=GHOST in YAML stops actual orders).
  * ``metadata.signal_source`` and ``metadata.probability_used`` always
    present so SQL joins on ``strategy_decisions`` work.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

# Make ``engine/`` importable when pytest is invoked from repo root.
_ENGINE = str(Path(__file__).resolve().parents[3])
if _ENGINE not in sys.path:
    sys.path.insert(0, _ENGINE)

# Set required env vars before any engine imports trigger Settings().
os.environ.setdefault(
    "DATABASE_URL", "postgresql://test:test@localhost:5432/test"
)

from strategies import gate_params as _gp
from strategies.configs.comparison_shadow import evaluate_comparison_shadow
from strategies.data_surface import FullDataSurface


def _make_surface(**overrides) -> FullDataSurface:
    """Minimal-but-complete FullDataSurface for hook tests.

    All required (non-default) fields are filled; tests pass overrides
    for the probability fields they care about.
    """
    defaults = dict(
        asset="BTC",
        timescale="5m",
        window_ts=1713000000,
        eval_offset=120,
        assembled_at=time.time(),
        current_price=84500.0,
        open_price=84000.0,
        delta_binance=0.005,
        delta_tiingo=0.004,
        delta_chainlink=0.005,
        delta_pct=0.004,
        delta_source="tiingo_rest_candle",
        vpin=0.55,
        regime="NORMAL",
        twap_delta=0.003,
        v2_probability_up=0.55,
        v2_probability_raw=0.54,
        v2_quantiles_p10=None,
        v2_quantiles_p50=None,
        v2_quantiles_p90=None,
        probability_lgb=None,
        probability_classifier=None,
        ensemble_config=None,
        v3_5m_composite=None, v3_15m_composite=None, v3_1h_composite=None,
        v3_4h_composite=None, v3_24h_composite=None, v3_48h_composite=None,
        v3_72h_composite=None, v3_1w_composite=None, v3_2w_composite=None,
        v3_sub_elm=None, v3_sub_cascade=None, v3_sub_taker=None,
        v3_sub_oi=None, v3_sub_funding=None, v3_sub_vpin=None,
        v3_sub_momentum=None,
        v4_regime="calm_trend",
        v4_regime_confidence=0.85,
        v4_regime_persistence=0.9,
        v4_macro_bias="BULL",
        v4_macro_direction_gate="ALLOW_ALL",
        v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True,
        v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH",
        v4_conviction_score=0.85,
        poly_direction="UP",
        poly_trade_advised=True,
        poly_confidence=0.55,
        poly_confidence_distance=0.05,
        poly_timing="optimal",
        poly_max_entry_price=0.65,
        poly_reason="strong_signal",
        v4_recommended_side="UP",
        v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None,
        v4_quantiles=None,
        clob_up_bid=0.46, clob_up_ask=0.48,
        clob_down_bid=0.52, clob_down_ask=0.54,
        clob_implied_up=0.47,
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


# ── Tests ────────────────────────────────────────────────────────────────────

def test_skip_when_signal_unavailable():
    """probability_lgb=None with signal_source=probability_lgb → SKIP."""
    surface = _make_surface(probability_lgb=None)
    token = _gp.set_active({
        "signal_source": "probability_lgb",
        "shadow_strategy_id": "v4_fusion_compare_v9",
        "shadow_conviction_floor": 0.10,
    })
    try:
        decision = evaluate_comparison_shadow(surface)
    finally:
        _gp.reset_active(token)

    assert decision.action == "SKIP"
    assert decision.skip_reason is not None
    assert "signal_source_unavailable" in decision.skip_reason
    assert "probability_lgb" in decision.skip_reason
    assert decision.metadata["signal_source"] == "probability_lgb"
    assert decision.metadata["probability_used"] is None
    assert decision.metadata["shadow_mode"] is True
    assert decision.strategy_id == "v4_fusion_compare_v9"


def test_skip_below_conviction_floor():
    """p=0.55 (dist=0.05) below floor 0.10 → SKIP with below_floor reason."""
    surface = _make_surface(probability_lgb=0.55)
    token = _gp.set_active({
        "signal_source": "probability_lgb",
        "shadow_strategy_id": "v4_fusion_compare_v9",
        "shadow_conviction_floor": 0.10,
    })
    try:
        decision = evaluate_comparison_shadow(surface)
    finally:
        _gp.reset_active(token)

    assert decision.action == "SKIP"
    assert decision.skip_reason is not None
    assert "below_floor" in decision.skip_reason
    assert decision.metadata["signal_source"] == "probability_lgb"
    assert decision.metadata["probability_used"] == pytest.approx(0.55)
    assert decision.metadata["distance"] == pytest.approx(0.05)


def test_trade_action_emitted_above_floor():
    """p=0.80 (dist=0.30 well above 0.10 floor) → TRADE with direction=UP."""
    surface = _make_surface(probability_lgb=0.80)
    token = _gp.set_active({
        "signal_source": "probability_lgb",
        "shadow_strategy_id": "v4_fusion_compare_v9",
        "shadow_conviction_floor": 0.10,
    })
    try:
        decision = evaluate_comparison_shadow(surface)
    finally:
        _gp.reset_active(token)

    assert decision.action == "TRADE"
    assert decision.direction == "UP"
    assert decision.metadata["signal_source"] == "probability_lgb"
    assert decision.metadata["probability_used"] == pytest.approx(0.80)
    assert decision.metadata["distance"] == pytest.approx(0.30)
    assert decision.metadata["shadow_mode"] is True
    assert decision.strategy_id == "v4_fusion_compare_v9"
    assert decision.strategy_version == "1.0.0-shadow"
    # confidence_score is the normalized distance (0..1).
    assert decision.confidence_score == pytest.approx(0.60)


def test_signal_source_v9():
    """signal_source=probability_lgb reads surface.probability_lgb."""
    surface = _make_surface(
        probability_lgb=0.75,
        probability_lgb_v10=0.10,  # would FAIL conviction if accidentally used
    )
    token = _gp.set_active({
        "signal_source": "probability_lgb",
        "shadow_strategy_id": "v4_fusion_compare_v9",
        "shadow_conviction_floor": 0.10,
    })
    try:
        decision = evaluate_comparison_shadow(surface)
    finally:
        _gp.reset_active(token)

    assert decision.action == "TRADE"
    assert decision.direction == "UP"
    assert decision.metadata["probability_used"] == pytest.approx(0.75)


def test_signal_source_v10():
    """signal_source=probability_lgb_v10 reads surface.probability_lgb_v10."""
    surface = _make_surface(
        probability_lgb=0.10,  # decoy — should NOT be picked
        probability_lgb_v10=0.78,
    )
    token = _gp.set_active({
        "signal_source": "probability_lgb_v10",
        "shadow_strategy_id": "v4_fusion_compare_v10",
        "shadow_conviction_floor": 0.10,
    })
    try:
        decision = evaluate_comparison_shadow(surface)
    finally:
        _gp.reset_active(token)

    assert decision.action == "TRADE"
    assert decision.direction == "UP"
    assert decision.metadata["probability_used"] == pytest.approx(0.78)
    assert decision.metadata["signal_source"] == "probability_lgb_v10"


def test_signal_source_v12_unavailable_skip():
    """signal_source=probability_lgb_v12 with surface.probability_lgb_v12=None → SKIP."""
    surface = _make_surface(
        probability_lgb=0.75,  # plenty of conviction — but v12 isn't wired
        probability_lgb_v12=None,
    )
    token = _gp.set_active({
        "signal_source": "probability_lgb_v12",
        "shadow_strategy_id": "v4_fusion_compare_v12",
        "shadow_conviction_floor": 0.10,
    })
    try:
        decision = evaluate_comparison_shadow(surface)
    finally:
        _gp.reset_active(token)

    assert decision.action == "SKIP"
    assert "signal_source_unavailable" in (decision.skip_reason or "")
    assert "probability_lgb_v12" in (decision.skip_reason or "")
    assert decision.metadata["signal_source"] == "probability_lgb_v12"
    assert decision.metadata["probability_used"] is None


def test_signal_source_blend():
    """signal_source=probability_up returns (lgb + classifier) / 2 when both present."""
    # lgb=0.80, clf=0.60 → blend = 0.70 → dist 0.20 → TRADE UP
    surface = _make_surface(
        probability_lgb=0.80,
        probability_classifier=0.60,
    )
    token = _gp.set_active({
        "signal_source": "probability_up",
        "shadow_strategy_id": "v4_fusion_compare_blend",
        "shadow_conviction_floor": 0.10,
    })
    try:
        decision = evaluate_comparison_shadow(surface)
    finally:
        _gp.reset_active(token)

    assert decision.action == "TRADE"
    assert decision.direction == "UP"
    assert decision.metadata["probability_used"] == pytest.approx(0.70)
    assert decision.metadata["signal_source"] == "probability_up"


def test_metadata_includes_signal_source_and_value():
    """Every decision's metadata must contain signal_source + probability_used.

    Iterates SKIP-unavailable, SKIP-below-floor, and TRADE paths so we
    catch any branch that forgot to populate the comparison metadata.
    """
    cases = [
        # (overrides, gate_params, expected_action, expected_p_used)
        (
            {"probability_lgb": None},
            {"signal_source": "probability_lgb",
             "shadow_strategy_id": "v4_fusion_compare_v9",
             "shadow_conviction_floor": 0.10},
            "SKIP",
            None,
        ),
        (
            {"probability_lgb": 0.55},  # dist=0.05 < 0.10
            {"signal_source": "probability_lgb",
             "shadow_strategy_id": "v4_fusion_compare_v9",
             "shadow_conviction_floor": 0.10},
            "SKIP",
            0.55,
        ),
        (
            {"probability_lgb": 0.85},  # dist=0.35 → TRADE
            {"signal_source": "probability_lgb",
             "shadow_strategy_id": "v4_fusion_compare_v9",
             "shadow_conviction_floor": 0.10},
            "TRADE",
            0.85,
        ),
    ]

    for overrides, params, expected_action, expected_p in cases:
        surface = _make_surface(**overrides)
        token = _gp.set_active(params)
        try:
            decision = evaluate_comparison_shadow(surface)
        finally:
            _gp.reset_active(token)

        assert decision.action == expected_action, (
            f"action mismatch for {overrides}: got {decision.action}"
        )
        assert "signal_source" in decision.metadata
        assert "probability_used" in decision.metadata
        assert decision.metadata["signal_source"] == params["signal_source"]
        if expected_p is None:
            assert decision.metadata["probability_used"] is None
        else:
            assert decision.metadata["probability_used"] == pytest.approx(
                expected_p
            )
