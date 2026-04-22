"""Tests for v7_15m_sniper — multi-asset 15m classifier sniper.

Covers:
  - classifier_only_mode: ETH/SOL/XRP windows where the sister TimesFM
    model returns ``status=no_model`` (empty poly block). The hook must
    skip the poly-dependent gates and derive direction/conviction from
    ``surface.probability_classifier`` alone.
  - BTC regression: the full-stack path (poly block populated) must
    remain unchanged.
  - XRP yaml: exists + GHOST + asset/timescale correct.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.data_surface import DataSurfaceManager, FullDataSurface
from strategies.registry import StrategyRegistry

CONFIGS_DIR = str(Path(__file__).resolve().parents[3] / "strategies" / "configs")


def _make_surface(**overrides) -> FullDataSurface:
    """Default 15m surface — T-350 eval, high conviction, no skips.

    Base surface targets ETH classifier-only mode: poly block is absent
    (poly_trade_advised=None, poly_timing=None, poly_direction=None),
    probability_classifier=0.92 → UP direction, |0.92-0.5|=0.42 →
    classifier_strong bucket. VPIN floor cleared, sources agree UP.
    """
    defaults = dict(
        asset="ETH", timescale="15m", window_ts=1713010800,
        eval_offset=350, assembled_at=time.time(),
        current_price=3250.0, open_price=3200.0,
        delta_binance=0.015, delta_tiingo=0.015, delta_chainlink=0.015,
        delta_pct=0.015, delta_source="chainlink",
        vpin=0.50, regime="NORMAL", twap_delta=0.010,
        v2_probability_up=None, v2_probability_raw=None,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=None, probability_classifier=0.92,
        ensemble_config=None,
        v3_5m_composite=None, v3_15m_composite=None, v3_1h_composite=None,
        v3_4h_composite=None, v3_24h_composite=None, v3_48h_composite=None,
        v3_72h_composite=None, v3_1w_composite=None, v3_2w_composite=None,
        v3_sub_elm=None, v3_sub_cascade=None, v3_sub_taker=None,
        v3_sub_oi=None, v3_sub_funding=None, v3_sub_vpin=None,
        v3_sub_momentum=None,
        v4_regime=None, v4_regime_confidence=None,
        v4_regime_persistence=None,
        v4_macro_bias=None, v4_macro_direction_gate=None,
        v4_macro_size_modifier=None,
        v4_consensus_safe_to_trade=None, v4_consensus_agreement_score=None,
        v4_consensus_max_divergence_bps=None,
        v4_conviction=None, v4_conviction_score=None,
        # ── Poly block: ENTIRELY ABSENT (no_model signature) ──
        poly_direction=None, poly_trade_advised=None, poly_confidence=None,
        poly_confidence_distance=None, poly_timing=None,
        poly_max_entry_price=None, poly_reason=None,
        v4_recommended_side=None, v4_recommended_collateral_pct=None,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.60, clob_up_ask=0.62, clob_down_bid=0.38,
        clob_down_ask=0.40, clob_implied_up=0.61,
        gamma_up_price=0.60, gamma_down_price=0.40,
        cg_oi_usd=None, cg_funding_rate=None,
        cg_taker_buy_vol=None, cg_taker_sell_vol=None,
        cg_liq_total=None, cg_liq_long=None,
        cg_liq_short=None, cg_long_short_ratio=None,
        timesfm_expected_move_bps=None, timesfm_vol_forecast_bps=None,
        hour_utc=12, seconds_to_close=350,
        probability_classifier_inferred_at=time.time(),
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


@pytest.fixture
def registry():
    mgr = DataSurfaceManager(v4_base_url="http://fake")
    reg = StrategyRegistry(CONFIGS_DIR, mgr)
    reg.load_all()
    return reg


def _eval_eth(reg, surface):
    return reg._evaluate_one(
        "v7_15m_sniper_eth", reg.configs["v7_15m_sniper_eth"], surface
    )


def _eval_btc(reg, surface):
    return reg._evaluate_one(
        "v7_15m_sniper", reg.configs["v7_15m_sniper"], surface
    )


# ── Registry load sanity ───────────────────────────────────────────────────
def test_all_four_v7_strategies_registered(registry):
    for name in (
        "v7_15m_sniper",
        "v7_15m_sniper_eth",
        "v7_15m_sniper_sol",
        "v7_15m_sniper_xrp",
    ):
        assert name in registry.strategy_names, f"{name} missing"
        cfg = registry.configs[name]
        assert cfg.mode == "GHOST", f"{name} must be GHOST"
        assert cfg.timescale == "15m"
        assert cfg.version == "7.0.0"


def test_v7_xrp_yaml_exists_and_correct():
    """Regression guard — XRP yaml must be present with right asset + GHOST."""
    mgr = DataSurfaceManager(v4_base_url="http://fake")
    reg = StrategyRegistry(CONFIGS_DIR, mgr)
    reg.load_all()
    cfg = reg.configs.get("v7_15m_sniper_xrp")
    assert cfg is not None, "v7_15m_sniper_xrp yaml missing"
    assert cfg.asset == "XRP"
    assert cfg.mode == "GHOST"
    assert cfg.timescale == "15m"
    assert cfg.hooks_file == "v7_15m_sniper.py"


# ── Classifier-only mode (ETH/SOL/XRP — no_model) ──────────────────────────
def test_classifier_only_up_trade(registry):
    """ETH window, poly absent, p_classifier=0.92 → TRADE UP."""
    surface = _make_surface(probability_classifier=0.92)
    decision = _eval_eth(registry, surface)

    assert decision.action == "TRADE"
    assert decision.direction == "UP"
    assert decision.strategy_id == "v7_15m_sniper"  # shared hook id
    # Classifier-only mode flag present in metadata
    assert decision.metadata.get("classifier_only_mode") is True
    # Entry cap comes from YAML override (0.80), not poly_max_entry_price
    assert decision.entry_cap == 0.80
    # Gate trail records the classifier-only branch
    modes = [g for g in decision.metadata["gate_results"]
             if g.get("gate") == "classifier_only_mode"]
    assert modes and modes[0]["passed"] is True


def test_classifier_only_down_trade(registry):
    """ETH window, poly absent, p_classifier=0.15 → TRADE DOWN (|0.15-0.5|=0.35 ≥ 0.30)."""
    surface = _make_surface(
        probability_classifier=0.15,
        # Flip chainlink/tiingo to DOWN so source_agreement + oracle gates
        # don't skip. (skip_on_oracle_disagree defaults to False but the
        # agreement gate checks *between* sources, not trade direction.)
        delta_binance=-0.015, delta_tiingo=-0.015, delta_chainlink=-0.015,
        delta_pct=-0.015,
    )
    decision = _eval_eth(registry, surface)

    assert decision.action == "TRADE"
    assert decision.direction == "DOWN"
    assert decision.metadata.get("classifier_only_mode") is True


def test_classifier_only_mid_conviction_skips(registry):
    """ETH window, poly absent, p_classifier=0.55 → SKIP (|0.55-0.5|=0.05 < 0.30)."""
    surface = _make_surface(probability_classifier=0.55)
    decision = _eval_eth(registry, surface)

    assert decision.action == "SKIP"
    assert decision.skip_reason is not None
    assert "mid_conf" in decision.skip_reason


def test_classifier_only_does_not_skip_on_no_poly_advice(registry):
    """Regression: classifier_only_mode must NOT emit `trade_not_advised:no_poly_advice`.

    Before the fix, empty poly block → trade_not_advised skip on every
    non-BTC eval, making the whole shadow track useless.
    """
    surface = _make_surface(probability_classifier=0.92)
    decision = _eval_eth(registry, surface)

    # Must not skip with the no_poly_advice reason.
    assert decision.skip_reason != "trade_not_advised: no_poly_advice"
    # The trade_advised gate should pass in skipped/classifier_only form.
    trade_advised_gates = [
        g for g in decision.metadata["gate_results"]
        if g.get("gate") == "trade_advised"
    ]
    assert trade_advised_gates
    assert trade_advised_gates[0]["passed"] is True
    assert "classifier_only_mode" in trade_advised_gates[0]["reason"]


# ── BTC regression guard — full poly stack still wins through ──────────────
def test_btc_full_stack_still_trades(registry):
    """BTC window with populated poly block — unchanged v6-style path."""
    surface = _make_surface(
        asset="BTC",
        probability_classifier=0.85,  # strong UP
        poly_direction="UP",
        poly_trade_advised=True,
        poly_confidence=0.85,
        poly_confidence_distance=0.35,
        poly_timing="optimal",
        poly_max_entry_price=0.70,
        poly_reason="strong_signal",
        v4_regime="volatile_trend",
        v4_conviction="HIGH",
        v4_recommended_collateral_pct=0.025,
        delta_binance=0.005, delta_tiingo=0.005, delta_chainlink=0.005,
        delta_pct=0.005,
    )
    decision = _eval_btc(registry, surface)

    assert decision.action == "TRADE"
    assert decision.direction == "UP"
    # Full-stack path — classifier_only_mode flag is False (poly block present)
    assert decision.metadata.get("classifier_only_mode") is False
    # trade_advised gate should pass via the populated poly path, NOT the
    # classifier_only skip.
    trade_advised_gates = [
        g for g in decision.metadata["gate_results"]
        if g.get("gate") == "trade_advised"
    ]
    assert trade_advised_gates
    assert trade_advised_gates[0]["passed"] is True
    assert "classifier_only_mode" not in trade_advised_gates[0]["reason"]
