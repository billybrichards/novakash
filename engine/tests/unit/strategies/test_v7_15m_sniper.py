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

from strategies.configs.v7_15m_sniper import (
    check_confirmation_v7,
    evaluate_polymarket_15m_sniper,
    get_confirmation_count_v7,
    reset_confirmation_v7,
    reset_all_confirmations_v7,
)
from strategies import gate_params as _gp
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
    # Reset v7 confirmation state so each test starts clean.
    reset_all_confirmations_v7()
    yield reg
    reset_all_confirmations_v7()


def _eval_eth(reg, surface):
    return reg._evaluate_one(
        "v7_15m_sniper_eth", reg.configs["v7_15m_sniper_eth"], surface
    )


def _eval_btc(reg, surface):
    return reg._evaluate_one(
        "v7_15m_sniper", reg.configs["v7_15m_sniper"], surface
    )


def _eval_eth_trade(reg, surface):
    """Helper to drive through the 3-tick confirmation and return the TRADE decision."""
    result = None
    for _ in range(3):
        result = _eval_eth(reg, surface)
    return result


def _eval_btc_trade(reg, surface):
    """Helper to drive through the 3-tick confirmation and return the TRADE decision."""
    result = None
    for _ in range(3):
        result = _eval_btc(reg, surface)
    return result


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
    """ETH window, poly absent, p_classifier=0.92 → TRADE UP (after 3-tick confirmation)."""
    surface = _make_surface(probability_classifier=0.92)
    decision = _eval_eth_trade(registry, surface)

    assert decision.action == "TRADE"
    assert decision.direction == "UP"
    assert decision.strategy_id == "v7_15m_sniper_eth"  # registry patches to config name
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
    decision = _eval_eth_trade(registry, surface)

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
    decision = _eval_eth_trade(registry, surface)

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
    decision = _eval_btc_trade(registry, surface)

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


# ── 3-tick entry confirmation ──────────────────────────────────────────────

@pytest.fixture(autouse=False)
def _v7_gate_params_3tick():
    """Bind gate_params with min_consecutive_pass_ticks=3 for 3-tick tests."""
    params = {
        "min_offset_sec": 300,
        "max_offset_sec": 400,
        "bucket_abs_dist_strong": 0.30,
        "bucket_path1_extreme_high": 0.90,
        "bucket_path1_extreme_low": 0.10,
        "path1_max_age_s": 120,
        "path1_skip_on_null": True,
        "vpin_min": 0.45,
        "source_agreement_require_chainlink": True,
        "source_agreement_require_tiingo": True,
        "skip_on_oracle_disagree": False,
        "health_gate": "off",
        "skip_stale_sources": True,
        "blocked_utc_hours": [],
        "tradeable_v4_regimes": ["calm_trend", "volatile_trend", "risk_off", "chop"],
        "entry_cap_override": 0.80,
        "v7_risk_off_override_enabled": True,
        "v7_risk_off_override_buckets": ["classifier_strong", "pegged_classifier"],
        "high_vpin_bypass_enabled": True,
        "high_vpin_bypass_threshold": 0.60,
        "high_vpin_bypass_buckets": ["classifier_strong", "pegged_classifier"],
        "min_consecutive_pass_ticks": 3,
    }
    token = _gp.set_active(params)
    reset_all_confirmations_v7()
    try:
        yield
    finally:
        _gp.reset_active(token)
        reset_all_confirmations_v7()


# ── Unit tests: state machine in isolation ────────────────────────────────

def test_v7_check_confirmation_accumulates():
    """Consecutive calls with same direction accumulate toward threshold."""
    reset_all_confirmations_v7()
    assert not check_confirmation_v7("v7_15m_sniper", "ETH", 1000, "UP")
    assert get_confirmation_count_v7("v7_15m_sniper", "ETH", 1000) == 1
    assert not check_confirmation_v7("v7_15m_sniper", "ETH", 1000, "UP")
    assert get_confirmation_count_v7("v7_15m_sniper", "ETH", 1000) == 2
    assert check_confirmation_v7("v7_15m_sniper", "ETH", 1000, "UP")
    assert get_confirmation_count_v7("v7_15m_sniper", "ETH", 1000) == 3
    reset_all_confirmations_v7()


def test_v7_check_confirmation_resets_on_direction_change():
    """Direction change resets the counter back to 1."""
    reset_all_confirmations_v7()
    check_confirmation_v7("v7_15m_sniper", "ETH", 1000, "UP")
    check_confirmation_v7("v7_15m_sniper", "ETH", 1000, "UP")
    assert get_confirmation_count_v7("v7_15m_sniper", "ETH", 1000) == 2
    # Direction flips — counter resets
    check_confirmation_v7("v7_15m_sniper", "ETH", 1000, "DOWN")
    assert get_confirmation_count_v7("v7_15m_sniper", "ETH", 1000) == 1
    reset_all_confirmations_v7()


def test_v7_window_flip_resets_counter():
    """When window_ts changes, the old counter does not carry over."""
    reset_all_confirmations_v7()
    check_confirmation_v7("v7_15m_sniper", "ETH", 1000, "UP")
    check_confirmation_v7("v7_15m_sniper", "ETH", 1000, "UP")
    # New window — fresh start
    result = check_confirmation_v7("v7_15m_sniper", "ETH", 1900, "UP")
    assert not result  # 1/3, not enough
    assert get_confirmation_count_v7("v7_15m_sniper", "ETH", 1900) == 1
    assert get_confirmation_count_v7("v7_15m_sniper", "ETH", 1000) == 2  # old unaffected
    reset_all_confirmations_v7()


def test_v7_asset_isolation():
    """ETH and BTC counters are independent even on the same window_ts."""
    reset_all_confirmations_v7()
    check_confirmation_v7("v7_15m_sniper", "ETH", 1000, "UP")
    check_confirmation_v7("v7_15m_sniper", "ETH", 1000, "UP")
    check_confirmation_v7("v7_15m_sniper", "BTC", 1000, "UP")
    assert get_confirmation_count_v7("v7_15m_sniper", "ETH", 1000) == 2
    assert get_confirmation_count_v7("v7_15m_sniper", "BTC", 1000) == 1
    reset_all_confirmations_v7()


# ── Integration tests with the evaluate hook ──────────────────────────────

def test_3tick_requires_3_passing_evals(_v7_gate_params_3tick, registry):
    """With min_consecutive_pass_ticks=3, TRADE only fires on the 3rd consecutive pass."""
    surface = _make_surface(probability_classifier=0.92)

    result1 = _eval_eth(registry, surface)
    assert result1.action == "SKIP"
    assert result1.skip_reason is not None
    assert "entry_confirmation" in result1.skip_reason
    assert "1/3" in result1.skip_reason

    result2 = _eval_eth(registry, surface)
    assert result2.action == "SKIP"
    assert "2/3" in result2.skip_reason

    result3 = _eval_eth(registry, surface)
    assert result3.action == "TRADE"
    assert result3.direction == "UP"


def test_3tick_resets_on_direction_change(_v7_gate_params_3tick, registry):
    """Pass-pass (UP)-then-DOWN: direction change resets counter, need 3 more for DOWN."""
    surface_up = _make_surface(probability_classifier=0.92)  # UP direction
    _eval_eth(registry, surface_up)  # tick 1 UP → count=1
    _eval_eth(registry, surface_up)  # tick 2 UP → count=2

    # Flip to DOWN — direction change resets the counter
    surface_down = _make_surface(
        probability_classifier=0.08,   # DOWN: |0.08-0.5|=0.42 ≥ 0.30
        delta_chainlink=-0.015, delta_tiingo=-0.015, delta_binance=-0.015,
        delta_pct=-0.015,
    )
    result1 = _eval_eth(registry, surface_down)  # count resets to 1
    assert result1.action == "SKIP"
    assert "1/3" in (result1.skip_reason or "")

    result2 = _eval_eth(registry, surface_down)  # count=2
    assert result2.action == "SKIP"
    assert "2/3" in (result2.skip_reason or "")

    result3 = _eval_eth(registry, surface_down)  # count=3 → TRADE
    assert result3.action == "TRADE"
    assert result3.direction == "DOWN"


def test_3tick_window_rollover(_v7_gate_params_3tick, registry):
    """2 passing ticks on window N; window flips; need 3 fresh ticks on window N+1."""
    surface_w1 = _make_surface(probability_classifier=0.92, window_ts=1713010800)
    _eval_eth(registry, surface_w1)  # tick 1
    _eval_eth(registry, surface_w1)  # tick 2

    surface_w2 = _make_surface(probability_classifier=0.92, window_ts=1713011700)
    result1 = _eval_eth(registry, surface_w2)
    assert result1.action == "SKIP"
    assert "1/3" in (result1.skip_reason or "")

    _eval_eth(registry, surface_w2)  # tick 2 on new window
    result3 = _eval_eth(registry, surface_w2)  # tick 3
    assert result3.action == "TRADE"


def test_3tick_disabled_fires_immediately():
    """When min_consecutive_pass_ticks=0, trades on the first passing tick.

    Calls the hook directly (bypassing registry) so we can control gate_params.
    """
    reset_all_confirmations_v7()
    # Full param set needed by the hook
    params = {
        "min_offset_sec": 300,
        "max_offset_sec": 400,
        "bucket_abs_dist_strong": 0.30,
        "bucket_path1_extreme_high": 0.90,
        "bucket_path1_extreme_low": 0.10,
        "path1_max_age_s": 120,
        "path1_skip_on_null": True,
        "vpin_min": 0.45,
        "source_agreement_require_chainlink": True,
        "source_agreement_require_tiingo": True,
        "skip_on_oracle_disagree": False,
        "health_gate": "off",
        "skip_stale_sources": True,
        "blocked_utc_hours": [],
        "tradeable_v4_regimes": ["calm_trend", "volatile_trend", "risk_off", "chop"],
        "entry_cap_override": 0.80,
        "v7_risk_off_override_enabled": True,
        "v7_risk_off_override_buckets": ["classifier_strong", "pegged_classifier"],
        "high_vpin_bypass_enabled": True,
        "high_vpin_bypass_threshold": 0.60,
        "high_vpin_bypass_buckets": ["classifier_strong", "pegged_classifier"],
        "min_consecutive_pass_ticks": 0,   # disabled
    }
    token = _gp.set_active(params)
    try:
        surface = _make_surface(probability_classifier=0.92)
        result = evaluate_polymarket_15m_sniper(surface)
        assert result.action == "TRADE"
    finally:
        _gp.reset_active(token)
        reset_all_confirmations_v7()
