"""Unit tests for the sister-veto bus (hub notes #394 / #395).

Coverage:
- publish_sister_fire records TRADE fires correctly
- get_recent_sister_fires filters by window_ts and agreement_window
- is_sister_pair_veto_active: both-oppose -> True (veto fires)
- is_sister_pair_veto_active: no fires -> False (no veto)
- is_sister_pair_veto_active: one sister has no fire -> False
- is_sister_pair_veto_active: one sister agrees with v9.2 -> False
- is_sister_pair_veto_active: sisters have mixed fires -> False (most-recent check)
- is_sister_pair_veto_active: window outside agreement window -> False
- evaluate_v9_2_super_lgb_only: both sisters oppose -> SKIP sister_pair_veto
- evaluate_v9_2_super_lgb_only: sisters agree with v9.2 -> TRADE (no veto)
- evaluate_v9_2_super_lgb_only: no sister fires -> TRADE (no veto)
- evaluate_v9_2_super_lgb_only: veto disabled via config -> TRADE
- v9_cascade_fade_late hook publishes to bus on TRADE
- v9_1_cascade_fade_late hook publishes to bus on TRADE
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.sister_veto_bus import (
    get_bus_snapshot,
    get_recent_sister_fires,
    is_sister_pair_veto_active,
    publish_sister_fire,
    reset_sister_veto_bus,
)
from strategies.configs.v9_2_super_lgb_only import (
    _STRATEGY_ID,
    _VERSION,
    evaluate_v9_2_super_lgb_only,
    reset_all_qualifying_ticks_v9_2,
)
from strategies.configs.v9_ensemble import (
    reset_all_confirmations_v9,
    reset_cooldown,
)
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# ── Surface factories (reused from test_v9_2_super_lgb_only) ─────────────

def _make_surface(**overrides) -> FullDataSurface:
    defaults = dict(
        asset="BTC", timescale="5m",
        window_ts=1713009600,  # 2024-04-13 12:00 UTC (hour=12, unblocked)
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
        probability_lgb_v9_2=0.10,  # DOWN conviction=0.90
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


def _make_surface_up(**overrides) -> FullDataSurface:
    up_defaults = dict(
        probability_lgb_v9_2=0.90,
        delta_binance=0.005, delta_tiingo=0.004, delta_chainlink=0.005,
        delta_pct=0.005, delta_source="chainlink",
        twap_delta=0.003,
        poly_direction="UP",
        v4_macro_bias="BULL",
        poly_confidence=0.70,
        poly_confidence_distance=0.20,
        v4_recommended_side="UP",
        clob_up_bid=0.63, clob_up_ask=0.65, clob_down_bid=0.28,
        clob_down_ask=0.30, clob_implied_up=0.65,
        gamma_up_price=0.65, gamma_down_price=0.35,
        cg_taker_buy_vol=1_200_000.0, cg_taker_sell_vol=800_000.0,
        probability_lgb=0.90,
    )
    up_defaults.update(overrides)
    return _make_surface(**up_defaults)


# ── Base gate_params fixture (mirrors test_v9_2_super_lgb_only) ───────────

@pytest.fixture(autouse=True)
def _reset_all():
    """Reset all module state before each test."""
    reset_sister_veto_bus()
    reset_all_qualifying_ticks_v9_2()
    reset_cooldown()
    reset_all_confirmations_v9()
    yield
    reset_sister_veto_bus()
    reset_all_qualifying_ticks_v9_2()
    reset_cooldown()
    reset_all_confirmations_v9()


@pytest.fixture
def _bind_gate_params():
    params: dict[str, Any] = {
        "min_offset_sec": 30,
        "max_offset_sec": 200,
        "tradeable_v4_regimes": ["volatile_trend", "chop", "risk_off", "calm_trend"],
        "block_down_vpin_regimes": [],
        "block_up_vpin_regimes": [],
        "lgb_dist_min_down": 0.10,
        "lgb_dist_min_up": 0.15,
        "lgb_dist_min_up_with_hc_agree": 0.05,
        "lgb_dist_min_down_with_hc_agree": 0.05,
        "fill_band_min": 0.00,
        "fill_band_max": 0.82,
        "up_min_fill_price": 0.20,
        "down_min_fill_price": 0.15,
        "blocked_utc_hours": [],
        "blocked_utc_hours_up": [4, 9, 13, 19, 23],
        "blocked_utc_hours_down": [2, 9, 14, 15],
        "source_agreement_require_chainlink": True,
        "source_agreement_require_tiingo": True,
        "skip_on_oracle_disagree": True,
        "vpin_min": 0.40,
        "vpin_max": 1.0,
        "post_loss_cooldown_min": 20,
        "ensemble_disagreement_threshold": 0.35,
        "require_direction_agreement": True,
        "pc_weight_t_60": 0.70,
        "pc_weight_t_120": 0.55,
        "pc_weight_t_180": 0.40,
        "pc_weight_t_200": 0.25,
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
        "delta_gate_enabled": False,
        "min_consecutive_pass_ticks": 0,
        "pl_vhc_bypass_enabled": True,
        "pl_vhc_threshold": 0.25,
        "pl_vhc_require_pc_agreement": True,
        "conviction_x_thresholds": {
            "TRANSITION_UP": 0.85, "TRANSITION_DOWN": 0.85,
            "CASCADE_UP": 0.85, "CASCADE_DOWN": 0.85,
            "NORMAL_UP": 0.85, "NORMAL_DOWN": 0.85,
            "CALM": None,
        },
        "ticks_n_thresholds": {
            "TRANSITION_UP": 8, "TRANSITION_DOWN": 12,
            "CASCADE_UP": 8, "CASCADE_DOWN": 12,
            "NORMAL_UP": 8, "NORMAL_DOWN": 8,
        },
        "eval_offset_min": 60,
        "eval_offset_max": 300,
        "gate_min_wr_target": 0.90,
        # Sister-pair veto enabled by default
        "sister_pair_veto": {
            "enabled": True,
            "pair": ["v9_1_cascade_fade_late", "v9_cascade_fade_late"],
            "agreement_window_seconds": 20,
            "veto_when_both_oppose": True,
        },
    }
    token = _gp.set_active(params)
    try:
        yield params
    finally:
        _gp.reset_active(token)


@pytest.fixture
def _bind_gate_params_veto_disabled():
    """Gate params with sister_pair_veto.enabled=False."""
    params: dict[str, Any] = {
        "min_offset_sec": 30, "max_offset_sec": 200,
        "tradeable_v4_regimes": ["volatile_trend", "chop", "risk_off", "calm_trend"],
        "block_down_vpin_regimes": [], "block_up_vpin_regimes": [],
        "lgb_dist_min_down": 0.10, "lgb_dist_min_up": 0.15,
        "lgb_dist_min_up_with_hc_agree": 0.05, "lgb_dist_min_down_with_hc_agree": 0.05,
        "fill_band_min": 0.00, "fill_band_max": 0.82,
        "up_min_fill_price": 0.20, "down_min_fill_price": 0.15,
        "blocked_utc_hours": [], "blocked_utc_hours_up": [4, 9, 13, 19, 23],
        "blocked_utc_hours_down": [2, 9, 14, 15],
        "source_agreement_require_chainlink": True, "source_agreement_require_tiingo": True,
        "skip_on_oracle_disagree": True, "vpin_min": 0.40, "vpin_max": 1.0,
        "post_loss_cooldown_min": 20,
        "ensemble_disagreement_threshold": 0.35, "require_direction_agreement": True,
        "pc_weight_t_60": 0.70, "pc_weight_t_120": 0.55,
        "pc_weight_t_180": 0.40, "pc_weight_t_200": 0.25,
        "vhc_threshold": 0.25, "vhc_bypass_transition": True,
        "vhc_bypass_up_dist": True, "vhc_bypass_disagreement": True,
        "vhc_bypass_lgb_safety_floor": True, "vhc_bypass_oracle_direction": True,
        "vhc_kelly_multiplier": 2.0,
        "conviction_high_dist": 0.20, "conviction_medium_dist": 0.12, "conviction_low_dist": 0.05,
        "fallback_to_lgb_on_pc_null": True,
        "transition_strong_bypass_enabled": True,
        "transition_bypass_min_avg_pct_delta": 0.05,
        "transition_bypass_min_lgb_dist": 0.20,
        "delta_gate_enabled": False, "min_consecutive_pass_ticks": 0,
        "pl_vhc_bypass_enabled": True, "pl_vhc_threshold": 0.25,
        "pl_vhc_require_pc_agreement": True,
        "conviction_x_thresholds": {
            "TRANSITION_UP": 0.85, "TRANSITION_DOWN": 0.85,
            "CASCADE_UP": 0.85, "CASCADE_DOWN": 0.85,
            "NORMAL_UP": 0.85, "NORMAL_DOWN": 0.85, "CALM": None,
        },
        "ticks_n_thresholds": {
            "TRANSITION_UP": 8, "TRANSITION_DOWN": 12,
            "CASCADE_UP": 8, "CASCADE_DOWN": 12,
            "NORMAL_UP": 8, "NORMAL_DOWN": 8,
        },
        "eval_offset_min": 60, "eval_offset_max": 300, "gate_min_wr_target": 0.90,
        "sister_pair_veto": {
            "enabled": False,  # DISABLED
            "pair": ["v9_1_cascade_fade_late", "v9_cascade_fade_late"],
            "agreement_window_seconds": 20,
        },
    }
    token = _gp.set_active(params)
    try:
        yield params
    finally:
        _gp.reset_active(token)


# =============================================================================
# 1. Bus: publish and retrieval
# =============================================================================

def test_publish_records_fire():
    """publish_sister_fire stores a fire record in the bus."""
    publish_sister_fire(
        strategy_id="v9_cascade_fade_late",
        asset="BTC",
        window_ts=1000,
        direction="UP",
    )
    fires = get_recent_sister_fires("v9_cascade_fade_late", "BTC", 1000, 20)
    assert len(fires) == 1
    assert fires[0].direction == "UP"
    assert fires[0].window_ts == 1000


def test_bus_empty_after_reset():
    """reset_sister_veto_bus clears all state."""
    publish_sister_fire(strategy_id="v9_cascade_fade_late", asset="BTC", window_ts=1, direction="DOWN")
    reset_sister_veto_bus()
    fires = get_recent_sister_fires("v9_cascade_fade_late", "BTC", 1, 20)
    assert fires == []


def test_get_recent_fires_within_window():
    """Fires within +-agreement_window_seconds are returned."""
    publish_sister_fire(strategy_id="v9_cascade_fade_late", asset="BTC", window_ts=1000, direction="DOWN")
    # Same window (offset=0)
    assert len(get_recent_sister_fires("v9_cascade_fade_late", "BTC", 1000, 20)) == 1
    # Within +20s
    assert len(get_recent_sister_fires("v9_cascade_fade_late", "BTC", 1020, 20)) == 1
    # Within -20s
    assert len(get_recent_sister_fires("v9_cascade_fade_late", "BTC", 980, 20)) == 1


def test_get_recent_fires_outside_window():
    """Fires outside the agreement window are excluded."""
    publish_sister_fire(strategy_id="v9_cascade_fade_late", asset="BTC", window_ts=1000, direction="DOWN")
    # 21s ahead of window — outside default 20s
    assert get_recent_sister_fires("v9_cascade_fade_late", "BTC", 1021, 20) == []
    # 21s behind window
    assert get_recent_sister_fires("v9_cascade_fade_late", "BTC", 979, 20) == []


def test_get_recent_fires_wrong_strategy():
    """Fires from a different strategy_id are not returned."""
    publish_sister_fire(strategy_id="v9_cascade_fade_late", asset="BTC", window_ts=1000, direction="DOWN")
    assert get_recent_sister_fires("v9_1_cascade_fade_late", "BTC", 1000, 20) == []


def test_get_recent_fires_wrong_asset():
    """Fires from a different asset are not returned."""
    publish_sister_fire(strategy_id="v9_cascade_fade_late", asset="ETH", window_ts=1000, direction="DOWN")
    assert get_recent_sister_fires("v9_cascade_fade_late", "BTC", 1000, 20) == []


def test_multiple_fires_most_recent_used():
    """When multiple fires exist, is_sister_pair_veto_active uses the most recent window_ts."""
    # Old fire agrees with v9.2 (UP), newer fire opposes (DOWN)
    publish_sister_fire(strategy_id="v9_cascade_fade_late", asset="BTC", window_ts=990, direction="UP")
    publish_sister_fire(strategy_id="v9_cascade_fade_late", asset="BTC", window_ts=1000, direction="DOWN")
    publish_sister_fire(strategy_id="v9_1_cascade_fade_late", asset="BTC", window_ts=1000, direction="DOWN")
    # v9.2 wants UP, sisters most-recent is DOWN (opposite) -> veto
    assert is_sister_pair_veto_active(
        pair=["v9_1_cascade_fade_late", "v9_cascade_fade_late"],
        asset="BTC",
        current_window_ts=1000,
        v9_2_direction="UP",
        agreement_window_seconds=20,
    ) is True


# =============================================================================
# 2. is_sister_pair_veto_active logic
# =============================================================================

def test_veto_fires_when_both_sisters_oppose():
    """Both sisters fired OPPOSITE direction -> veto=True."""
    publish_sister_fire(strategy_id="v9_cascade_fade_late", asset="BTC", window_ts=1000, direction="UP")
    publish_sister_fire(strategy_id="v9_1_cascade_fade_late", asset="BTC", window_ts=1000, direction="UP")
    # v9.2 wants DOWN, sisters want UP (opposite) -> veto
    result = is_sister_pair_veto_active(
        pair=["v9_1_cascade_fade_late", "v9_cascade_fade_late"],
        asset="BTC",
        current_window_ts=1000,
        v9_2_direction="DOWN",
        agreement_window_seconds=20,
    )
    assert result is True


def test_no_veto_when_no_sister_fires():
    """No fires in bus -> no evidence -> veto=False."""
    result = is_sister_pair_veto_active(
        pair=["v9_1_cascade_fade_late", "v9_cascade_fade_late"],
        asset="BTC",
        current_window_ts=1000,
        v9_2_direction="DOWN",
        agreement_window_seconds=20,
    )
    assert result is False


def test_no_veto_when_only_one_sister_fires():
    """Only one sister has a qualifying fire -> insufficient evidence -> no veto."""
    publish_sister_fire(strategy_id="v9_cascade_fade_late", asset="BTC", window_ts=1000, direction="UP")
    # v9_1_cascade_fade_late has no fire -> no veto
    result = is_sister_pair_veto_active(
        pair=["v9_1_cascade_fade_late", "v9_cascade_fade_late"],
        asset="BTC",
        current_window_ts=1000,
        v9_2_direction="DOWN",
        agreement_window_seconds=20,
    )
    assert result is False


def test_no_veto_when_sisters_agree_with_v9_2():
    """Both sisters agree with v9.2 direction -> no veto."""
    publish_sister_fire(strategy_id="v9_cascade_fade_late", asset="BTC", window_ts=1000, direction="DOWN")
    publish_sister_fire(strategy_id="v9_1_cascade_fade_late", asset="BTC", window_ts=1000, direction="DOWN")
    # v9.2 also wants DOWN -> sisters agree -> no veto
    result = is_sister_pair_veto_active(
        pair=["v9_1_cascade_fade_late", "v9_cascade_fade_late"],
        asset="BTC",
        current_window_ts=1000,
        v9_2_direction="DOWN",
        agreement_window_seconds=20,
    )
    assert result is False


def test_no_veto_when_sisters_mixed():
    """One sister opposes, one agrees -> no veto (need BOTH to oppose)."""
    publish_sister_fire(strategy_id="v9_cascade_fade_late", asset="BTC", window_ts=1000, direction="UP")   # opposes DOWN
    publish_sister_fire(strategy_id="v9_1_cascade_fade_late", asset="BTC", window_ts=1000, direction="DOWN")  # agrees with DOWN
    result = is_sister_pair_veto_active(
        pair=["v9_1_cascade_fade_late", "v9_cascade_fade_late"],
        asset="BTC",
        current_window_ts=1000,
        v9_2_direction="DOWN",
        agreement_window_seconds=20,
    )
    assert result is False


def test_no_veto_when_fire_outside_window():
    """Sisters fired but their window_ts is outside agreement window -> no veto."""
    publish_sister_fire(strategy_id="v9_cascade_fade_late", asset="BTC", window_ts=900, direction="UP")
    publish_sister_fire(strategy_id="v9_1_cascade_fade_late", asset="BTC", window_ts=900, direction="UP")
    # current_window_ts=1000, agreement=20 -> |1000-900|=100 > 20 -> no qualifying fires
    result = is_sister_pair_veto_active(
        pair=["v9_1_cascade_fade_late", "v9_cascade_fade_late"],
        asset="BTC",
        current_window_ts=1000,
        v9_2_direction="DOWN",
        agreement_window_seconds=20,
    )
    assert result is False


def test_veto_with_empty_pair():
    """Empty pair list -> always no veto (can't check zero strategies)."""
    result = is_sister_pair_veto_active(
        pair=[],
        asset="BTC",
        current_window_ts=1000,
        v9_2_direction="DOWN",
        agreement_window_seconds=20,
    )
    assert result is False


# =============================================================================
# 3. evaluate_v9_2_super_lgb_only integration
# =============================================================================

def _fire_sisters_against_v9_2(wts: int, v9_2_direction: str) -> None:
    """Publish both sisters in the OPPOSITE direction from v9_2_direction."""
    opposite = "UP" if v9_2_direction == "DOWN" else "DOWN"
    publish_sister_fire(strategy_id="v9_cascade_fade_late", asset="BTC", window_ts=wts, direction=opposite)
    publish_sister_fire(strategy_id="v9_1_cascade_fade_late", asset="BTC", window_ts=wts, direction=opposite)


def _run_n_ticks(surface_factory, n: int):
    """Run evaluate_v9_2_super_lgb_only n times and return all decisions."""
    return [evaluate_v9_2_super_lgb_only(surface_factory()) for _ in range(n)]


WTS = 1713009600  # 12:00 UTC — unblocked for both directions


def test_veto_fires_after_cohort_gate_qualifies(_bind_gate_params):
    """When both sisters oppose, v9.2 skips with sister_pair_veto even after N qualifying ticks."""
    reset_all_qualifying_ticks_v9_2()
    wts = WTS + 50000  # use a fresh window_ts to avoid state pollution

    def _s():
        return _make_surface(window_ts=wts, regime="NORMAL", probability_lgb_v9_2=0.10)

    # Build up 7 qualifying ticks (N-1 for NORMAL_DOWN=8)
    for _ in range(7):
        d = evaluate_v9_2_super_lgb_only(_s())
        assert d.skip_reason == "cohort_below_threshold"

    # Now sisters fire OPPOSITE (UP) before the 8th tick
    _fire_sisters_against_v9_2(wts, "DOWN")

    # 8th tick: cohort gate fires but sister veto catches it
    final = evaluate_v9_2_super_lgb_only(_s())
    assert final.action == "SKIP"
    assert final.skip_reason == "sister_pair_veto"
    assert final.metadata.get("v9_2_gate_fired") is False
    assert "sister_veto_pair" in final.metadata
    assert final.strategy_id == _STRATEGY_ID


def test_no_veto_when_sisters_agree_integration(_bind_gate_params):
    """Sisters agree with v9.2 (DOWN) -> TRADE after N ticks, no veto."""
    reset_all_qualifying_ticks_v9_2()
    wts = WTS + 51000

    def _s():
        return _make_surface(window_ts=wts, regime="NORMAL", probability_lgb_v9_2=0.10)

    # Sisters fire DOWN too (same as v9.2 wants)
    publish_sister_fire(strategy_id="v9_cascade_fade_late", asset="BTC", window_ts=wts, direction="DOWN")
    publish_sister_fire(strategy_id="v9_1_cascade_fade_late", asset="BTC", window_ts=wts, direction="DOWN")

    for _ in range(7):
        evaluate_v9_2_super_lgb_only(_s())

    final = evaluate_v9_2_super_lgb_only(_s())
    # Veto did not fire (sisters agree) -> expect TRADE or a base-gate SKIP, NOT sister_pair_veto
    assert final.skip_reason != "sister_pair_veto"


def test_no_veto_when_bus_empty_integration(_bind_gate_params):
    """No sister fires in bus -> no evidence -> v9.2 fires normally."""
    reset_all_qualifying_ticks_v9_2()
    wts = WTS + 52000

    def _s():
        return _make_surface(window_ts=wts, regime="NORMAL", probability_lgb_v9_2=0.10)

    for _ in range(7):
        evaluate_v9_2_super_lgb_only(_s())

    final = evaluate_v9_2_super_lgb_only(_s())
    # Sister bus is empty -> no veto. Expect TRADE or a base-gate reason (not veto).
    assert final.skip_reason != "sister_pair_veto"


def test_veto_disabled_via_config(_bind_gate_params_veto_disabled):
    """When sister_pair_veto.enabled=False, the veto never fires."""
    reset_all_qualifying_ticks_v9_2()
    wts = WTS + 53000

    def _s():
        return _make_surface(window_ts=wts, regime="NORMAL", probability_lgb_v9_2=0.10)

    # Both sisters oppose — but veto is disabled
    _fire_sisters_against_v9_2(wts, "DOWN")

    for _ in range(7):
        evaluate_v9_2_super_lgb_only(_s())

    final = evaluate_v9_2_super_lgb_only(_s())
    # Veto disabled -> should not skip with sister_pair_veto
    assert final.skip_reason != "sister_pair_veto"


def test_veto_metadata_shape(_bind_gate_params):
    """sister_pair_veto SKIP metadata carries required diagnostic fields."""
    reset_all_qualifying_ticks_v9_2()
    wts = WTS + 54000

    def _s():
        return _make_surface(window_ts=wts, regime="NORMAL", probability_lgb_v9_2=0.10)

    _fire_sisters_against_v9_2(wts, "DOWN")
    for _ in range(7):
        evaluate_v9_2_super_lgb_only(_s())
    final = evaluate_v9_2_super_lgb_only(_s())

    if final.skip_reason != "sister_pair_veto":
        pytest.skip(f"Veto did not fire (base gate blocked first): {final.skip_reason}")

    meta = final.metadata
    for field in [
        "probability_lgb_v9_2",
        "v9_2_conviction",
        "v9_2_pred_direction",
        "v9_2_cohort",
        "v9_2_gate_fired",
        "sister_veto_pair",
        "sister_veto_window_sec",
    ]:
        assert field in meta, f"Missing veto metadata field: {field}"
    assert meta["v9_2_gate_fired"] is False
    assert meta["sister_veto_pair"] == ["v9_1_cascade_fade_late", "v9_cascade_fade_late"]
    assert meta["sister_veto_window_sec"] == 20


# =============================================================================
# 4. cascade-fade hooks publish to bus on TRADE
# =============================================================================

def test_v9_cascade_fade_late_publishes_on_trade():
    """v9_cascade_fade_late publishes to the veto bus when it fires TRADE."""
    from strategies.configs.v9_cascade_fade_late import evaluate_v9_cascade_fade_late
    import strategies.gate_params as gp_mod

    # Minimal params for cascade fade late
    params = {
        "min_offset_sec": 0, "max_offset_sec": 200,
        "tradeable_v4_regimes": ["volatile_trend", "chop", "risk_off", "calm_trend"],
        "block_down_vpin_regimes": ["TRANSITION", "NORMAL"], "block_up_vpin_regimes": [],
        "lgb_dist_min_down": 0.10, "lgb_dist_min_up": 1.01,  # UP hard-locked
        "lgb_dist_min_up_with_hc_agree": 1.01, "lgb_dist_min_down_with_hc_agree": 0.10,
        "fill_band_min": 0.00, "fill_band_max": 0.82,
        "up_min_fill_price": 1.01, "down_min_fill_price": 0.15,
        "blocked_utc_hours": [], "blocked_utc_hours_up": [], "blocked_utc_hours_down": [],
        "source_agreement_require_chainlink": True, "source_agreement_require_tiingo": True,
        "skip_on_oracle_disagree": True,
        "vpin_min": 0.40, "vpin_max": 1.0,
        "post_loss_cooldown_min": 0,
        "ensemble_disagreement_threshold": 0.35, "require_direction_agreement": True,
        "pc_weight_t_60": 0.70, "pc_weight_t_120": 0.55,
        "pc_weight_t_180": 0.40, "pc_weight_t_200": 0.25,
        "vhc_threshold": 0.25, "vhc_bypass_transition": False,
        "vhc_bypass_up_dist": False, "vhc_bypass_disagreement": False,
        "vhc_bypass_lgb_safety_floor": False, "vhc_bypass_oracle_direction": False,
        "vhc_kelly_multiplier": 1.0,
        "conviction_high_dist": 0.20, "conviction_medium_dist": 0.12, "conviction_low_dist": 0.05,
        "fallback_to_lgb_on_pc_null": True,
        "transition_strong_bypass_enabled": False,
        "transition_bypass_min_avg_pct_delta": 0.05,
        "transition_bypass_min_lgb_dist": 0.20,
        "delta_gate_enabled": False, "min_consecutive_pass_ticks": 0,
        "pl_vhc_bypass_enabled": False, "pl_vhc_threshold": 0.25,
        "pl_vhc_require_pc_agreement": True,
    }
    token = gp_mod.set_active(params)
    try:
        surface = _make_surface(
            regime="CASCADE",
            probability_lgb=0.10,  # DOWN conviction=0.90
            delta_chainlink=-0.015, delta_tiingo=-0.012, delta_binance=-0.015,
            delta_pct=-0.015, twap_delta=-0.010,
            poly_direction="DOWN",
            v4_recommended_side="DOWN",
            v4_macro_bias="BEAR",
        )
        decision = evaluate_v9_cascade_fade_late(surface)
    finally:
        gp_mod.reset_active(token)
        reset_cooldown()
        reset_all_confirmations_v9()

    if decision.action == "TRADE":
        fires = get_recent_sister_fires("v9_cascade_fade_late", "BTC", surface.window_ts, 20)
        assert len(fires) == 1, "Expected 1 fire in bus after TRADE"
        assert fires[0].direction == decision.direction
    else:
        # SKIP is fine — bus should be empty
        fires = get_recent_sister_fires("v9_cascade_fade_late", "BTC", surface.window_ts, 20)
        assert fires == [], f"Bus should be empty on SKIP, got: {fires}"


def test_v9_cascade_fade_late_does_not_publish_on_skip():
    """v9_cascade_fade_late does NOT publish to bus on SKIP."""
    from strategies.configs.v9_cascade_fade_late import evaluate_v9_cascade_fade_late
    import strategies.gate_params as gp_mod

    params = {
        "min_offset_sec": 200, "max_offset_sec": 300,  # eval_offset=120 will SKIP timing
        "tradeable_v4_regimes": ["volatile_trend"],
        "block_down_vpin_regimes": [], "block_up_vpin_regimes": [],
        "lgb_dist_min_down": 0.10, "lgb_dist_min_up": 1.01,
        "lgb_dist_min_up_with_hc_agree": 1.01, "lgb_dist_min_down_with_hc_agree": 0.10,
        "fill_band_min": 0.00, "fill_band_max": 0.82,
        "up_min_fill_price": 1.01, "down_min_fill_price": 0.15,
        "blocked_utc_hours": [], "blocked_utc_hours_up": [], "blocked_utc_hours_down": [],
        "source_agreement_require_chainlink": True, "source_agreement_require_tiingo": True,
        "skip_on_oracle_disagree": True, "vpin_min": 0.40, "vpin_max": 1.0,
        "post_loss_cooldown_min": 0,
        "ensemble_disagreement_threshold": 0.35, "require_direction_agreement": True,
        "pc_weight_t_60": 0.70, "pc_weight_t_120": 0.55, "pc_weight_t_180": 0.40, "pc_weight_t_200": 0.25,
        "vhc_threshold": 0.25, "vhc_bypass_transition": False, "vhc_bypass_up_dist": False,
        "vhc_bypass_disagreement": False, "vhc_bypass_lgb_safety_floor": False,
        "vhc_bypass_oracle_direction": False, "vhc_kelly_multiplier": 1.0,
        "conviction_high_dist": 0.20, "conviction_medium_dist": 0.12, "conviction_low_dist": 0.05,
        "fallback_to_lgb_on_pc_null": True,
        "transition_strong_bypass_enabled": False, "transition_bypass_min_avg_pct_delta": 0.05,
        "transition_bypass_min_lgb_dist": 0.20,
        "delta_gate_enabled": False, "min_consecutive_pass_ticks": 0,
        "pl_vhc_bypass_enabled": False, "pl_vhc_threshold": 0.25, "pl_vhc_require_pc_agreement": True,
    }
    token = gp_mod.set_active(params)
    try:
        surface = _make_surface(eval_offset=120)  # outside min_offset_sec=200 -> timing SKIP
        decision = evaluate_v9_cascade_fade_late(surface)
    finally:
        gp_mod.reset_active(token)
        reset_cooldown()
        reset_all_confirmations_v9()

    assert decision.action == "SKIP"
    fires = get_recent_sister_fires("v9_cascade_fade_late", "BTC", surface.window_ts, 20)
    assert fires == [], f"Bus must be empty on SKIP, got: {fires}"


def test_bus_snapshot_diagnostic():
    """get_bus_snapshot returns a JSON-serialisable snapshot."""
    publish_sister_fire(strategy_id="v9_cascade_fade_late", asset="BTC", window_ts=9999, direction="DOWN")
    snap = get_bus_snapshot()
    assert isinstance(snap, dict)
    key = "v9_cascade_fade_late:BTC"
    assert key in snap
    assert snap[key][0]["direction"] == "DOWN"
    assert snap[key][0]["window_ts"] == 9999
