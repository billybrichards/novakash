"""Tests for v9_lgb_only — LGB-only fork of v9_ensemble.

Focused coverage for the audit-metadata mirroring (probability_lgb_prod)
and the LGB-only forced fallback identity.

Fixtures mirror ``test_v9_ensemble._make_surface`` with pc=None so the
hook delegates to the v8_lgb_only fallback (the documented LGB-only
behaviour). Surface helper is copied locally — do NOT import private
fixtures from the v9_ensemble test module.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_lgb_only import evaluate_v9_lgb_only
from strategies.configs.v9_ensemble import (
    reset_cooldown,
    reset_all_confirmations_v9,
)
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


def _make_surface(**overrides) -> FullDataSurface:
    """Default valid DOWN surface — pl=0.30 (DOWN), pc=None, all gates pass.

    Mirrors test_v9_ensemble._make_surface but with pc=None so the
    LGB-only fallback path is exercised. probability_lgb defaults to
    0.30 so the DOWN dist (0.20) clears the 0.10 floor.
    """
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713010800,  # 2024-04-13 12:00 UTC
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=85000.0,
        delta_binance=-0.005, delta_tiingo=-0.004, delta_chainlink=-0.005,
        delta_pct=-0.005, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=-0.003,
        v2_probability_up=0.30, v2_probability_raw=0.30,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=0.30, probability_classifier=None,
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
        poly_direction="DOWN", poly_trade_advised=True, poly_confidence=0.30,
        poly_confidence_distance=0.20, poly_timing="optimal",
        poly_max_entry_price=0.60, poly_reason="strong_signal",
        v4_recommended_side="DOWN", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.38, clob_up_ask=0.40, clob_down_bid=0.53,
        clob_down_ask=0.55, clob_implied_up=0.40,
        gamma_up_price=0.40, gamma_down_price=0.60,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0, cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=12, seconds_to_close=120,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


@pytest.fixture(autouse=True)
def _bind_gate_params():
    """Bind v9_lgb_only YAML defaults around each test.

    Same surface contract as v9_ensemble (the underlying gate stack)
    but with classifier-disabled / LGB-only-fallback semantics.
    """
    params = {
        "min_offset_sec": 30,
        "max_offset_sec": 200,
        "tradeable_v4_regimes": [
            "volatile_trend", "chop", "risk_off", "calm_trend",
        ],
        "block_down_vpin_regimes": ["TRANSITION"],
        "block_up_vpin_regimes": ["TRANSITION"],
        "lgb_dist_min_down": 0.10,
        "lgb_dist_min_up": 0.15,
        "lgb_dist_min_up_with_hc_agree": 0.05,
        "lgb_dist_min_down_with_hc_agree": 0.05,
        "fill_band_min": 0.00,
        "fill_band_max": 0.82,
        "up_min_fill_price": 0.55,
        "down_min_fill_price": 0.15,
        "blocked_utc_hours": [0, 1, 2, 3, 4, 5],
        "source_agreement_require_chainlink": True,
        "source_agreement_require_tiingo": True,
        "skip_on_oracle_disagree": True,
        "vpin_min": 0.40,
        "vpin_max": 1.0,
        "post_loss_cooldown_min": 20,
        # v9-specific
        "ensemble_disagreement_threshold": 0.25,
        "require_direction_agreement": True,
        "pc_weight_t_60": 0.55,
        "pc_weight_t_120": 0.50,
        "pc_weight_t_180": 0.45,
        "pc_weight_t_200": 0.35,
        "vhc_threshold": 0.25,
        "vhc_bypass_transition": True,
        "vhc_bypass_up_dist": True,
        "vhc_bypass_disagreement": True,
        "vhc_bypass_lgb_safety_floor": True,
        "vhc_bypass_oracle_direction": True,
        "vhc_kelly_multiplier": 2.0,
        "conviction_high_dist": 0.20,
        "conviction_medium_dist": 0.12,
        "conviction_low_dist": 0.05,
        "fallback_to_lgb_on_pc_null": True,
        "transition_strong_bypass_enabled": True,
        "transition_bypass_min_avg_pct_delta": 0.05,
        "transition_bypass_min_lgb_dist": 0.20,
        # Disable confirmation/delta noise for these unit checks
        "delta_gate_enabled": False,
        "min_consecutive_pass_ticks": 0,
        # PL VHC bypass — enabled by default
        "pl_vhc_bypass_enabled": True,
        "pl_vhc_threshold": 0.25,
        "pl_vhc_require_pc_agreement": True,
    }
    token = _gp.set_active(params)
    reset_cooldown()
    reset_all_confirmations_v9()
    try:
        yield
    finally:
        _gp.reset_active(token)
        reset_cooldown()
        reset_all_confirmations_v9()


# ── probability_lgb_prod mirroring ─────────────────────────────────────────
def test_probability_lgb_prod_mirrors_surface_value():
    """v9_lgb_only must write surface.probability_lgb to metadata as
    probability_lgb_prod for filter-parity with v10_lgb_only."""
    surface = _make_surface(probability_lgb=0.72)
    decision = evaluate_v9_lgb_only(surface)
    assert "probability_lgb_prod" in decision.metadata
    assert decision.metadata["probability_lgb_prod"] == pytest.approx(0.72, abs=1e-9)


def test_probability_lgb_prod_present_on_skip():
    """Audit field should be populated even on SKIP decisions so
    downstream filters always have it."""
    # eval_offset out of band → SKIP, but metadata should still mirror
    surface = _make_surface(probability_lgb=0.65, eval_offset=10)
    decision = evaluate_v9_lgb_only(surface)
    # The mirror is written regardless of action
    assert decision.metadata.get("probability_lgb_prod") == pytest.approx(0.65, abs=1e-9)


def test_lgb_only_forced_metadata_still_set():
    """Sanity: existing audit fields keep working."""
    surface = _make_surface(probability_lgb=0.30)
    decision = evaluate_v9_lgb_only(surface)
    assert decision.metadata.get("lgb_only_forced") is True
    assert decision.metadata.get("probability_classifier") is None


def test_strategy_identity_preserved():
    """Hook must relabel the decision as v9_lgb_only."""
    surface = _make_surface(probability_lgb=0.30)
    decision = evaluate_v9_lgb_only(surface)
    assert decision.strategy_id == "v9_lgb_only"
    assert decision.strategy_version == "9.1.0-lgb"
