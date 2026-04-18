"""Tests for v5_fresh — the relaxed-gate sibling of v5_ensemble.

Two concerns:
  1. Registry loads v5_fresh + routes its gate_params to the shared
     v5_ensemble.evaluate_polymarket_ensemble hook (PR #267 plumbing).
  2. Decisions are stamped with strategy_id='v5_fresh' (not 'v5_ensemble').

Behavioural parity of the relaxed values themselves is covered by the
pre-existing v5_ensemble tests — those tests exercise the same code
path with env-fallback, and this strategy just loads different
gate_params from YAML. Re-testing every gate would duplicate coverage.
"""

from __future__ import annotations

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


def _make_surface(**overrides) -> FullDataSurface:
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713000000,
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=85000.0,
        delta_binance=-0.005, delta_tiingo=-0.004, delta_chainlink=-0.005,
        delta_pct=-0.004, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=-0.003,
        v2_probability_up=0.38, v2_probability_raw=0.36,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=None, probability_classifier=None, ensemble_config=None,
        v3_5m_composite=None, v3_15m_composite=None, v3_1h_composite=None,
        v3_4h_composite=None, v3_24h_composite=None, v3_48h_composite=None,
        v3_72h_composite=None, v3_1w_composite=None, v3_2w_composite=None,
        v3_sub_elm=None, v3_sub_cascade=None, v3_sub_taker=None,
        v3_sub_oi=None, v3_sub_funding=None, v3_sub_vpin=None,
        v3_sub_momentum=None,
        v4_regime="calm_trend", v4_regime_confidence=0.85,
        v4_regime_persistence=0.9,
        v4_macro_bias="BEAR", v4_macro_direction_gate="ALLOW_ALL",
        v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True, v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH", v4_conviction_score=0.85,
        poly_direction="DOWN", poly_trade_advised=True, poly_confidence=0.30,
        poly_confidence_distance=0.20, poly_timing="optimal",
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


def test_v5_fresh_loads_LIVE(registry):
    assert "v5_fresh" in registry.strategy_names
    cfg = registry.configs["v5_fresh"]
    assert cfg.mode == "LIVE"
    assert cfg.version == "5.3.0"
    assert cfg.timescale == "5m"


def test_v5_fresh_yaml_has_relaxed_knobs(registry):
    cfg = registry.configs["v5_fresh"]
    params = cfg.gate_params
    # Tier A3 — health relaxation
    assert params.get("health_gate") == "unsafe"
    # Tier A1 — model-disagreement gate ENABLED (v5_ensemble leaves at 0)
    assert params.get("ensemble_disagreement_threshold") == 0.20
    # Tier-0 knobs stay strict
    assert params.get("skip_calm") is True
    assert params.get("skip_stale_sources") is True
    assert params.get("require_tiingo_agree") is True
    # risk_off override STAYS DISABLED across all strategies (Billy's
    # 2026-04-17 kill, commit b094278). Re-enabling requires
    # counterfactual shadow data validation first.
    assert params.get("risk_off_override_enabled") is False


def test_v5_ensemble_stays_strict(registry):
    """Regression: v5_fresh's relaxations must NOT leak into v5_ensemble."""
    v5e = registry.configs["v5_ensemble"].gate_params
    assert v5e.get("health_gate") == "degraded"
    assert v5e.get("ensemble_disagreement_threshold") == 0.0
    # Override disabled in prod since 2026-04-17.
    assert v5e.get("risk_off_override_enabled") is False


def test_v4_fusion_override_disabled(registry):
    """Regression: v4_fusion yaml must match Montreal .env kill-switch."""
    v4 = registry.configs["v4_fusion"].gate_params
    assert v4.get("risk_off_override_enabled") is False


def test_v4_fusion_flipped_to_ghost(registry):
    assert registry.configs["v4_fusion"].mode == "GHOST"


def test_decision_stamped_with_v5_fresh_id(registry):
    surface = _make_surface(
        poly_direction="DOWN", poly_trade_advised=True,
        poly_confidence=0.30, poly_confidence_distance=0.20,
        poly_timing="optimal",
        delta_chainlink=-0.005, delta_tiingo=-0.004,
    )
    decision = registry._evaluate_one(
        "v5_fresh", registry.configs["v5_fresh"], surface
    )
    assert decision.strategy_id == "v5_fresh"
    assert decision.strategy_version == "5.3.0"


def test_relaxed_health_allows_degraded(registry):
    """v5_fresh with health_gate=unsafe lets a DEGRADED signal trade.
    Same surface on v5_ensemble (health_gate=degraded) would SKIP.

    Uses vpin=0.30 (below healthy band 0.50-0.85) which trips
    score_signal_health's 'vpin:low' amber → DEGRADED.
    """
    # Chainlink+tiingo both present + agree, so the only amber should
    # be vpin:low → DEGRADED (not UNSAFE).
    surface = _make_surface(
        poly_direction="DOWN", poly_trade_advised=True,
        poly_confidence=0.30, poly_confidence_distance=0.20,
        poly_timing="optimal",
        delta_chainlink=-0.005, delta_tiingo=-0.004,
        vpin=0.30,  # trips vpin:low amber
    )
    fresh = registry._evaluate_one(
        "v5_fresh", registry.configs["v5_fresh"], surface
    )
    ensemble = registry._evaluate_one(
        "v5_ensemble", registry.configs["v5_ensemble"], surface
    )
    assert fresh.action == "TRADE", (
        f"v5_fresh should trade on DEGRADED (health=unsafe); "
        f"got {fresh.action} skip_reason={fresh.skip_reason}"
    )
    assert ensemble.action == "SKIP", (
        f"v5_ensemble should skip on DEGRADED (health=degraded); "
        f"got {ensemble.action}"
    )
    assert "health_degraded" in (ensemble.skip_reason or "")
