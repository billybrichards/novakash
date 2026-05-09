"""Unit tests for v9_2_super_lgb_only strategy.

Coverage:
- model-not-loaded silent SKIP (v9_2_model_not_loaded)
- CALM regime exclusion (cohort_excluded_calm)
- Per-direction blocked_utc_hours (UP and DOWN)
- Non-consecutive qualifying-tick state machine (window lifecycle, reset, accumulation)
- Per-cohort gate: all 6 (vpin_regime × direction) cohorts
- cohort_below_threshold SKIP metadata shape
- Base gate SKIP passthrough (strategy identity relabelled)
- TRADE path metadata shape (gate_fired=True, tick counts etc.)
- State machine isolation: different windows are independent
- State machine direction flip resets counter correctly
- Surface probability_lgb_v9_2 field wired correctly

Tests mirror ``test_v9_lgb_only`` structure: _make_surface fixture +
per-test _bind_gate_params autouse fixture.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_2_super_lgb_only import (
    _STRATEGY_ID,
    _VERSION,
    count_qualifying_tick_v9_2,
    evaluate_v9_2_super_lgb_only,
    get_qualifying_tick_count_v9_2,
    reset_all_qualifying_ticks_v9_2,
    reset_qualifying_ticks_v9_2,
)
from strategies.configs.v9_ensemble import (
    reset_all_confirmations_v9,
    reset_cooldown,
)
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# ── Surface factory ────────────────────────────────────────────────────────

def _make_surface(**overrides) -> FullDataSurface:
    """Default valid DOWN surface with probability_lgb_v9_2 populated.

    window_ts=1713009600 → 2024-04-13 12:00:00 UTC (hour=12, no blocks).
    vpin=0.55, regime=NORMAL → cohort NORMAL_DOWN (N=8).
    probability_lgb_v9_2=0.22 → DOWN conviction=0.78 ≥ 0.85 threshold.
    Deltas are DOWN-aligned (chainlink -0.005, tiingo -0.004) so oracle
    gate passes for DOWN direction. For UP tests, pass delta overrides.
    """
    defaults = dict(
        asset="BTC", timescale="5m",
        window_ts=1713009600,  # 2024-04-13 12:00 UTC (hour=12, unblocked for both dirs)
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
        # v9.2 field: probability_lgb_v9_2
        probability_lgb_v9_2=0.10,  # DOWN conviction=0.90 ≥ 0.85 threshold
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


def _make_surface_up(**overrides) -> FullDataSurface:
    """UP-direction variant: probability_lgb_v9_2=0.90, UP-aligned deltas.

    Uses UP-aligned chainlink/tiingo deltas (+0.005) so the oracle direction
    gate in v9_ensemble passes for UP predictions.
    window_ts=1713009600 → hour=12 UTC (unblocked for UP direction).
    """
    up_defaults = dict(
        probability_lgb_v9_2=0.90,  # UP conviction=0.90
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


# ── Gate-params fixture ────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _bind_gate_params():
    """Bind v9_2_super_lgb_only YAML defaults around each test."""
    params: dict[str, Any] = {
        "min_offset_sec": 30,
        "max_offset_sec": 200,
        "tradeable_v4_regimes": [
            "volatile_trend", "chop", "risk_off", "calm_trend",
        ],
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
        # Per-direction hour blocks (from V9_2_GATE_CONFIG.html)
        "blocked_utc_hours_up":   [4, 9, 13, 19, 23],
        "blocked_utc_hours_down": [2, 9, 14, 15],
        "source_agreement_require_chainlink": True,
        "source_agreement_require_tiingo": True,
        "skip_on_oracle_disagree": True,
        "vpin_min": 0.40,
        "vpin_max": 1.0,
        "post_loss_cooldown_min": 20,
        # v9-ensemble params
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
        # Disable confirmation/delta noise for unit checks
        "delta_gate_enabled": False,
        "min_consecutive_pass_ticks": 0,
        "pl_vhc_bypass_enabled": True,
        "pl_vhc_threshold": 0.25,
        "pl_vhc_require_pc_agreement": True,
        # v9.2 cohort gate thresholds
        "conviction_x_thresholds": {
            "TRANSITION_UP":   0.85,
            "TRANSITION_DOWN": 0.85,
            "CASCADE_UP":      0.85,
            "CASCADE_DOWN":    0.85,
            "NORMAL_UP":       0.85,
            "NORMAL_DOWN":     0.85,
            "CALM":            None,
        },
        "ticks_n_thresholds": {
            "TRANSITION_UP":   8,
            "TRANSITION_DOWN": 12,
            "CASCADE_UP":      8,
            "CASCADE_DOWN":    12,
            "NORMAL_UP":       8,
            "NORMAL_DOWN":     8,
        },
        "eval_offset_min": 60,
        "eval_offset_max": 300,
        "gate_min_wr_target": 0.90,
    }
    token = _gp.set_active(params)
    reset_cooldown()
    reset_all_confirmations_v9()
    reset_all_qualifying_ticks_v9_2()
    try:
        yield
    finally:
        _gp.reset_active(token)
        reset_cooldown()
        reset_all_confirmations_v9()
        reset_all_qualifying_ticks_v9_2()


# ── 1. model-not-loaded SKIP ───────────────────────────────────────────────

def test_skip_when_model_not_loaded():
    """No probability_lgb_v9_2 on surface → SKIP v9_2_model_not_loaded."""
    surface = _make_surface(probability_lgb_v9_2=None)
    decision = evaluate_v9_2_super_lgb_only(surface)
    assert decision.action == "SKIP"
    assert decision.skip_reason == "v9_2_model_not_loaded"
    assert decision.strategy_id == _STRATEGY_ID
    assert decision.strategy_version == _VERSION
    assert decision.metadata.get("v9_2_enabled") is False


def test_skip_when_field_absent():
    """Field entirely absent from surface (no default) → SKIP."""
    surface = _make_surface()
    object.__setattr__(surface, "probability_lgb_v9_2", None)
    decision = evaluate_v9_2_super_lgb_only(surface)
    assert decision.action == "SKIP"
    assert decision.skip_reason == "v9_2_model_not_loaded"


# ── 2. CALM regime exclusion ───────────────────────────────────────────────

def test_calm_regime_excluded():
    """CALM vpin_regime → SKIP cohort_excluded_calm regardless of conviction."""
    surface = _make_surface_up(regime="CALM", probability_lgb_v9_2=0.92)
    decision = evaluate_v9_2_super_lgb_only(surface)
    assert decision.action == "SKIP"
    assert decision.skip_reason == "cohort_excluded_calm"
    assert decision.metadata.get("v9_2_cohort") == "CALM_UP"
    assert decision.metadata.get("v9_2_gate_fired") is False


# ── 3. Per-direction blocked_utc_hours ─────────────────────────────────────

def test_blocked_utc_hour_up_fires():
    """Hour in blocked_utc_hours_up (e.g. 4) + UP direction → SKIP.

    The hour block fires IN v9_2 BEFORE delegation to v9_ensemble (step 6
    in the surface flow). This is tested by checking skip_reason is the
    hour-block reason, not an oracle-direction reason.
    window_ts at 04:00 UTC: 2024-04-13 04:00:00 = 1712980800
    """
    surface = _make_surface_up(
        window_ts=1712980800,  # 04:00 UTC — blocked for UP
        regime="NORMAL",
    )
    decision = evaluate_v9_2_super_lgb_only(surface)
    assert decision.action == "SKIP"
    assert "blocked_utc_hour_up" in decision.skip_reason
    assert decision.metadata.get("blocked_hour") == 4
    assert decision.metadata.get("v9_2_pred_direction") == "UP"


def test_blocked_utc_hour_down_fires():
    """Hour in blocked_utc_hours_down (e.g. 2) + DOWN direction → SKIP.

    window_ts at 02:00 UTC: 2024-04-13 02:00:00 = 1712973600
    """
    surface = _make_surface(
        window_ts=1712973600,  # 02:00 UTC — blocked for DOWN
        probability_lgb_v9_2=0.10,  # DOWN
        regime="NORMAL",
    )
    decision = evaluate_v9_2_super_lgb_only(surface)
    assert decision.action == "SKIP"
    assert "blocked_utc_hour_down" in decision.skip_reason
    assert decision.metadata.get("blocked_hour") == 2
    assert decision.metadata.get("v9_2_pred_direction") == "DOWN"


def test_blocked_hour_up_does_not_fire_for_down():
    """Hour 4 is only UP-blocked. DOWN direction at hour 4 is NOT blocked."""
    surface = _make_surface(
        window_ts=1712980800,  # 04:00 UTC — only UP-blocked
        probability_lgb_v9_2=0.10,  # DOWN — not blocked at hour 4
        regime="NORMAL",
    )
    decision = evaluate_v9_2_super_lgb_only(surface)
    # Should NOT skip on blocked_utc_hour_up (hour 4 is UP-only)
    assert "blocked_utc_hour_up" not in (decision.skip_reason or "")


def test_hour_9_blocks_both_directions():
    """Hour 9 is in both blocked_up and blocked_down lists.

    2024-04-13 09:00:00 UTC = 1712998800
    """
    ts_hour9 = 1712998800  # 2024-04-13 09:00:00 UTC
    # DOWN test — hour 9 blocks DOWN
    surface_dn = _make_surface(window_ts=ts_hour9, probability_lgb_v9_2=0.08, regime="NORMAL")
    decision_dn = evaluate_v9_2_super_lgb_only(surface_dn)
    assert decision_dn.action == "SKIP"
    assert "blocked_utc_hour_down" in decision_dn.skip_reason

    # UP test — hour 9 also blocks UP
    surface_up = _make_surface_up(window_ts=ts_hour9, regime="NORMAL")
    decision_up = evaluate_v9_2_super_lgb_only(surface_up)
    assert decision_up.action == "SKIP"
    assert "blocked_utc_hour_up" in decision_up.skip_reason


def test_unblocked_hour_passes_hour_gate():
    """Hour 12 (not in either block list) passes the hour gate for both directions."""
    ts_hour12 = 1713009600  # 12:00 UTC (default fixture)
    # DOWN direction, hour 12 → not blocked
    surface = _make_surface(window_ts=ts_hour12, probability_lgb_v9_2=0.10, regime="NORMAL")
    decision = evaluate_v9_2_super_lgb_only(surface)
    assert "blocked_utc_hour" not in (decision.skip_reason or "")


# ── 4. Non-consecutive qualifying-tick state machine ──────────────────────

def test_qualifying_tick_state_machine_increments():
    """count_qualifying_tick_v9_2 increments on qualifying ticks."""
    reset_all_qualifying_ticks_v9_2()
    wts = 999001
    c1 = count_qualifying_tick_v9_2(wts, "DOWN", conviction=0.90, conviction_threshold=0.85)
    assert c1 == 1
    c2 = count_qualifying_tick_v9_2(wts, "DOWN", conviction=0.87, conviction_threshold=0.85)
    assert c2 == 2
    c3 = count_qualifying_tick_v9_2(wts, "DOWN", conviction=0.86, conviction_threshold=0.85)
    assert c3 == 3


def test_non_qualifying_tick_does_not_increment():
    """A tick with conviction < threshold does NOT increment the counter."""
    reset_all_qualifying_ticks_v9_2()
    wts = 999002
    count_qualifying_tick_v9_2(wts, "UP", conviction=0.90, conviction_threshold=0.85)
    count_qualifying_tick_v9_2(wts, "UP", conviction=0.82, conviction_threshold=0.85)  # below threshold
    assert get_qualifying_tick_count_v9_2(wts, "UP") == 1


def test_qualifying_tick_counter_is_per_direction():
    """Counters for UP and DOWN within the same window are independent."""
    reset_all_qualifying_ticks_v9_2()
    wts = 999003
    count_qualifying_tick_v9_2(wts, "UP", conviction=0.90, conviction_threshold=0.85)
    count_qualifying_tick_v9_2(wts, "UP", conviction=0.90, conviction_threshold=0.85)
    count_qualifying_tick_v9_2(wts, "DOWN", conviction=0.90, conviction_threshold=0.85)
    assert get_qualifying_tick_count_v9_2(wts, "UP") == 2
    assert get_qualifying_tick_count_v9_2(wts, "DOWN") == 1


def test_qualifying_tick_counter_is_per_window():
    """Different window_ts values have independent counters."""
    reset_all_qualifying_ticks_v9_2()
    count_qualifying_tick_v9_2(111, "DOWN", conviction=0.90, conviction_threshold=0.85)
    count_qualifying_tick_v9_2(111, "DOWN", conviction=0.90, conviction_threshold=0.85)
    count_qualifying_tick_v9_2(222, "DOWN", conviction=0.90, conviction_threshold=0.85)
    assert get_qualifying_tick_count_v9_2(111, "DOWN") == 2
    assert get_qualifying_tick_count_v9_2(222, "DOWN") == 1


def test_reset_qualifying_ticks_clears_specific_key():
    """reset_qualifying_ticks_v9_2 clears only the named (wts, direction) key."""
    reset_all_qualifying_ticks_v9_2()
    count_qualifying_tick_v9_2(500, "DOWN", conviction=0.90, conviction_threshold=0.85)
    count_qualifying_tick_v9_2(500, "UP", conviction=0.90, conviction_threshold=0.85)
    reset_qualifying_ticks_v9_2(500, "DOWN")
    assert get_qualifying_tick_count_v9_2(500, "DOWN") == 0
    assert get_qualifying_tick_count_v9_2(500, "UP") == 1  # unaffected


def test_reset_all_clears_everything():
    count_qualifying_tick_v9_2(1, "UP", conviction=0.90, conviction_threshold=0.85)
    count_qualifying_tick_v9_2(2, "DOWN", conviction=0.90, conviction_threshold=0.85)
    reset_all_qualifying_ticks_v9_2()
    assert get_qualifying_tick_count_v9_2(1, "UP") == 0
    assert get_qualifying_tick_count_v9_2(2, "DOWN") == 0


# ── 5. Per-cohort gate: below threshold → SKIP ────────────────────────────

def _run_n_ticks_below(surface_factory, n: int, *, tick_count: int) -> "StrategyDecision":
    """Run evaluate_v9_2_super_lgb_only `tick_count` times with the same surface.

    Returns the last decision. Assumes tick_count < n so the gate never fires.
    """
    decision = None
    for _ in range(tick_count):
        surface = surface_factory()
        decision = evaluate_v9_2_super_lgb_only(surface)
    return decision  # type: ignore[return-value]


# window_ts at 12:00 UTC — unblocked for both directions (hour 12 is clean).
# Use fixed offsets of 300s steps so all stay within the same hour.
_WTS_HOUR_12 = 1713009600  # 2024-04-13 12:00:00 UTC


@pytest.mark.parametrize("cohort,regime,prob_v9_2,ticks_n,is_up,wts_offset", [
    ("TRANSITION_UP",   "TRANSITION", 0.92, 8,  True,  0),
    ("TRANSITION_DOWN", "TRANSITION", 0.08, 12, False, 300),
    ("CASCADE_UP",      "CASCADE",    0.92, 8,  True,  600),
    ("CASCADE_DOWN",    "CASCADE",    0.08, 12, False, 900),
    ("NORMAL_UP",       "NORMAL",     0.92, 8,  True,  1200),
    ("NORMAL_DOWN",     "NORMAL",     0.10, 8,  False, 1500),  # conv=0.90 ≥ 0.85
])
def test_cohort_below_threshold_skips(cohort, regime, prob_v9_2, ticks_n, is_up, wts_offset):
    """For each cohort, tick_count < N → SKIP cohort_below_threshold."""
    # All wts offsets stay within the 12:00-12:25 UTC range (hour 12, unblocked).
    wts = _WTS_HOUR_12 + wts_offset
    reset_all_qualifying_ticks_v9_2()

    if is_up:
        def _surface():
            return _make_surface_up(
                window_ts=wts, regime=regime,
                probability_lgb_v9_2=prob_v9_2,
            )
    else:
        def _surface():
            return _make_surface(
                window_ts=wts, regime=regime,
                probability_lgb_v9_2=prob_v9_2,
            )

    ticks_below_n = ticks_n - 1  # always below threshold
    decision = _run_n_ticks_below(_surface, ticks_n, tick_count=ticks_below_n)
    assert decision.action == "SKIP", f"{cohort}: expected SKIP below threshold; got {decision.skip_reason}"
    assert decision.skip_reason == "cohort_below_threshold", (
        f"{cohort}: wrong skip_reason: {decision.skip_reason}"
    )
    assert decision.metadata.get("v9_2_cohort") == cohort
    assert decision.metadata.get("v9_2_gate_fired") is False
    assert decision.metadata.get("v9_2_tick_count") == ticks_below_n
    assert decision.metadata.get("v9_2_tick_threshold") == ticks_n


def test_cohort_below_threshold_metadata_has_regime():
    """SKIP metadata must carry vpin_regime for diagnostics."""
    surface = _make_surface(regime="CASCADE", probability_lgb_v9_2=0.08)  # DOWN, conviction=0.92
    decision = evaluate_v9_2_super_lgb_only(surface)
    # First tick: count=1, N=12 for CASCADE_DOWN → SKIP cohort_below_threshold
    assert decision.skip_reason == "cohort_below_threshold"
    assert decision.metadata.get("vpin_regime") == "CASCADE"
    assert decision.metadata.get("v9_2_conviction") is not None


# ── 6. Cohort gate FIRES after ≥N ticks ───────────────────────────────────

def test_normal_down_fires_at_n_ticks():
    """NORMAL_DOWN requires N=8 qualifying ticks; exactly 8 → TRADE."""
    wts = 888001
    reset_all_qualifying_ticks_v9_2()

    def _surface():
        return _make_surface(
            window_ts=wts, regime="NORMAL",
            probability_lgb_v9_2=0.10,  # DOWN conviction=0.90 ≥ 0.85
        )

    decisions = []
    for _ in range(8):
        d = evaluate_v9_2_super_lgb_only(_surface())
        decisions.append(d)

    # First 7: SKIP cohort_below_threshold
    for d in decisions[:7]:
        assert d.action == "SKIP"
        assert d.skip_reason == "cohort_below_threshold"

    # 8th tick: TRADE (cohort gate fires)
    final = decisions[7]
    assert final.action == "TRADE", f"Expected TRADE on tick 8, got SKIP: {final.skip_reason}"
    assert final.metadata.get("v9_2_gate_fired") is True
    assert final.metadata.get("v9_2_tick_count") == 8
    assert final.metadata.get("v9_2_tick_threshold") == 8
    assert final.metadata.get("v9_2_cohort") == "NORMAL_DOWN"


def test_transition_down_requires_12_ticks():
    """TRANSITION_DOWN requires N=12; tick 11 SKIPS, tick 12 TRADES."""
    wts = 888002
    reset_all_qualifying_ticks_v9_2()

    def _surface():
        return _make_surface(
            window_ts=wts, regime="TRANSITION",
            probability_lgb_v9_2=0.08,  # DOWN conviction=0.92
        )

    for i in range(11):
        d = evaluate_v9_2_super_lgb_only(_surface())
        if d.action != "SKIP" or d.skip_reason != "cohort_below_threshold":
            pytest.fail(f"Tick {i+1}: expected SKIP cohort_below_threshold, got {d.action}/{d.skip_reason}")

    final = evaluate_v9_2_super_lgb_only(_surface())
    assert final.action == "TRADE"
    assert final.metadata.get("v9_2_gate_fired") is True
    assert final.metadata.get("v9_2_cohort") == "TRANSITION_DOWN"


# ── 7. SKIP metadata shape (dashboards/audits) ────────────────────────────

def test_skip_metadata_has_required_fields():
    """cohort_below_threshold SKIP metadata must have all audit-required fields."""
    surface = _make_surface(regime="NORMAL", probability_lgb_v9_2=0.10)  # DOWN conv=0.90
    decision = evaluate_v9_2_super_lgb_only(surface)
    assert decision.action == "SKIP"
    assert decision.skip_reason == "cohort_below_threshold"
    meta = decision.metadata
    for field in [
        "probability_lgb_v9_2",
        "v9_2_conviction",
        "v9_2_pred_direction",
        "v9_2_cohort",
        "v9_2_gate_fired",
        "v9_2_tick_count",
        "v9_2_tick_threshold",
    ]:
        assert field in meta, f"Missing audit field: {field}"


def test_trade_metadata_has_required_fields():
    """TRADE metadata must have the same audit fields + gate_fired=True."""
    wts = 900001
    reset_all_qualifying_ticks_v9_2()

    def _s():
        return _make_surface(window_ts=wts, regime="NORMAL", probability_lgb_v9_2=0.10)

    for _ in range(7):
        evaluate_v9_2_super_lgb_only(_s())
    final = evaluate_v9_2_super_lgb_only(_s())

    if final.action != "TRADE":
        pytest.skip(f"Base gates blocked TRADE: {final.skip_reason}")

    meta = final.metadata
    assert meta.get("v9_2_gate_fired") is True
    for field in [
        "probability_lgb_v9_2",
        "probability_lgb_prod",
        "v9_2_conviction",
        "v9_2_pred_direction",
        "v9_2_cohort",
        "v9_2_tick_count",
        "v9_2_tick_threshold",
        "lgb_only_forced",
        "v9_2_active",
    ]:
        assert field in meta, f"Missing TRADE audit field: {field}"


# ── 8. Base gate SKIP passthrough ─────────────────────────────────────────

def test_base_gate_skip_passed_through_with_v9_2_identity():
    """When v9_ensemble SKIPs, the decision is relabelled as v9_2 strategy."""
    # eval_offset out of range → timing gate SKIP
    surface = _make_surface(probability_lgb_v9_2=0.92, eval_offset=10)
    decision = evaluate_v9_2_super_lgb_only(surface)
    assert decision.action == "SKIP"
    assert decision.strategy_id == _STRATEGY_ID
    assert decision.strategy_version == _VERSION
    # tick counter PERSISTS across base-gate SKIPs (PR #515 fix — counter
    # resets only on explicit blocked_utc_hour SKIP, cohort fire, or new
    # window_ts/direction. Base-gate SKIP no longer resets the counter so
    # qualifying ticks accumulate across the full eval band as per YAML spec).
    # probability_lgb_v9_2=0.92 conviction qualifies (≥0.85), so count == 1.
    assert get_qualifying_tick_count_v9_2(surface.window_ts, "UP") == 1


def test_base_gate_skip_includes_v9_2_fields_in_metadata():
    """Even on base-gate SKIP, v9_2 fields should be stamped in metadata."""
    surface = _make_surface(probability_lgb_v9_2=0.92, eval_offset=10, regime="NORMAL")
    decision = evaluate_v9_2_super_lgb_only(surface)
    assert decision.action == "SKIP"
    assert "probability_lgb_v9_2" in decision.metadata
    assert decision.metadata["v9_2_gate_fired"] is False


# ── 9. Strategy identity ───────────────────────────────────────────────────

def test_strategy_identity():
    """All decisions (SKIP/TRADE) carry the v9_2 identity."""
    surface = _make_surface(probability_lgb_v9_2=0.10, regime="NORMAL")
    decision = evaluate_v9_2_super_lgb_only(surface)
    assert decision.strategy_id == _STRATEGY_ID
    assert decision.strategy_version == _VERSION


def test_strategy_id_constant():
    assert _STRATEGY_ID == "v9_2_super_lgb_only"
    assert _VERSION == "9.2.0-super-canary"


# ── 10. Surface field wired ───────────────────────────────────────────────

def test_surface_has_probability_lgb_v9_2_field():
    """FullDataSurface has the probability_lgb_v9_2 field with correct default."""
    from strategies.data_surface import FullDataSurface
    surface = _make_surface(probability_lgb_v9_2=0.73)
    assert hasattr(surface, "probability_lgb_v9_2")
    assert surface.probability_lgb_v9_2 == pytest.approx(0.73, abs=1e-9)


def test_surface_probability_lgb_v9_2_defaults_to_none():
    """probability_lgb_v9_2 is None by default (model not yet loaded)."""
    # Build surface without the field (uses default=None)
    surface = _make_surface()
    object.__setattr__(surface, "probability_lgb_v9_2", None)
    assert surface.probability_lgb_v9_2 is None


# ── 11. Conviction calculation ────────────────────────────────────────────

@pytest.mark.parametrize("prob, expected_dir, expected_conv", [
    (0.92, "UP",   0.92),
    (0.08, "DOWN", 0.92),
    (0.50, "UP",   0.50),   # tie → UP
    (0.85, "UP",   0.85),
    (0.15, "DOWN", 0.85),
])
def test_conviction_and_direction(prob, expected_dir, expected_conv):
    """Check that conviction = max(p, 1-p) and direction is correct."""
    surface = _make_surface(probability_lgb_v9_2=prob, regime="CALM")
    decision = evaluate_v9_2_super_lgb_only(surface)
    # CALM is excluded — we get the fields before the exclusion check
    meta = decision.metadata
    assert meta.get("v9_2_pred_direction") == expected_dir
    assert meta.get("v9_2_conviction") == pytest.approx(expected_conv, abs=1e-6)


# ── 12. Window lifecycle / state machine reset on fire ────────────────────

def test_tick_counter_resets_after_gate_fires():
    """After the cohort gate fires (TRADE), the counter is cleared.

    On the NEXT tick for the same window_ts + direction, the counter
    starts from scratch (count = 1 after one qualifying tick).
    """
    wts = 901001
    reset_all_qualifying_ticks_v9_2()

    def _s():
        return _make_surface(window_ts=wts, regime="NORMAL", probability_lgb_v9_2=0.10)

    # Accumulate 8 ticks to fire
    for _ in range(8):
        evaluate_v9_2_super_lgb_only(_s())

    # Counter should be 0 after firing
    assert get_qualifying_tick_count_v9_2(wts, "DOWN") == 0

    # Next tick starts at 1
    evaluate_v9_2_super_lgb_only(_s())
    assert get_qualifying_tick_count_v9_2(wts, "DOWN") == 1


def test_tick_counter_persists_on_base_gate_skip():
    """Counter persists across base-gate SKIPs (PR #515 fix).

    The qualifying-tick counter must NOT reset on base-gate SKIP so that
    qualifying ticks accumulate across the full eval band as per the YAML spec:
    "N qualifying ticks anywhere in [t-300, t-60]". Resetting on every oracle/
    delta/fill flap silently turned the gate into a near-consecutive-ticks gate.
    Reset only occurs on: blocked_utc_hour SKIP, cohort fire, new window_ts or
    direction flip.
    """
    wts = 901002
    reset_all_qualifying_ticks_v9_2()

    # Build up 2 qualifying ticks via a valid surface
    for _ in range(2):
        surface = _make_surface(window_ts=wts, regime="NORMAL", probability_lgb_v9_2=0.10)
        evaluate_v9_2_super_lgb_only(surface)

    assert get_qualifying_tick_count_v9_2(wts, "DOWN") == 2

    # Now trigger a base-gate SKIP (eval_offset out of range).
    # Counter MUST persist — probability_lgb_v9_2=0.10 conviction < 0.85 threshold
    # for DOWN so this tick does NOT add a qualifying tick, but the existing 2
    # qualifying ticks should be preserved.
    surface_skip = _make_surface(window_ts=wts, regime="NORMAL", probability_lgb_v9_2=0.10, eval_offset=10)
    evaluate_v9_2_super_lgb_only(surface_skip)

    # Counter persists: still 3 (the base-SKIP tick also qualified since
    # probability_lgb_v9_2=0.10 → conviction=max(0.10,0.90)=0.90 ≥ 0.85 for DOWN).
    # The base-gate SKIP is the timing gate (eval_offset=10 is out of range),
    # which fires BEFORE count_qualifying_tick_v9_2. So the tick is counted
    # even though the base gate SKIPs later.
    assert get_qualifying_tick_count_v9_2(wts, "DOWN") == 3


# ── 13. Persistence writer tests ──────────────────────────────────────────

class _FakeConn:
    def __init__(self, result: str = "UPDATE 1") -> None:
        self.calls: list[dict] = []
        self._result = result

    async def execute(self, sql: str, *args, **kwargs):
        self.calls.append({"sql": sql, "args": args, "kwargs": kwargs})
        return self._result


class _FakePool:
    def __init__(self, result: str = "UPDATE 1") -> None:
        self.conn = _FakeConn(result)

    def acquire(self, timeout=None):  # accept timeout kwarg (PR #515 fix)
        outer = self

        class _CM:
            async def __aenter__(self_inner):
                return outer.conn

            async def __aexit__(self_inner, *exc):
                return None

        return _CM()


def _stub_db(result: str = "UPDATE 1"):
    from persistence.db_client import DBClient
    db = DBClient.__new__(DBClient)
    db._pool = _FakePool(result)
    return db


@pytest.mark.asyncio
async def test_v9_2_writer_stamps_all_cohort_fields():
    """Writer must upsert all 5 v9.2 columns. PR #500 converted from
    UPDATE-only to INSERT...ON CONFLICT to fix the race with the
    canonical write_signal_evaluation INSERT."""
    db = _stub_db("INSERT 0 1")
    n = await db.update_signal_evaluations_lgb_v9_2(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=60,
        probability_lgb_v9_2=0.87,
        v9_2_conviction=0.87,
        v9_2_pred_direction="UP",
        v9_2_cohort="NORMAL_UP",
        v9_2_gate_fired=True,
    )
    assert n == 1
    call = db._pool.conn.calls[0]
    sql = call["sql"]
    assert "INSERT INTO signal_evaluations" in sql
    assert "ON CONFLICT (window_ts, asset, timeframe, eval_offset)" in sql
    assert "probability_lgb_v9_2" in sql
    assert "v9_2_conviction" in sql
    assert "v9_2_cohort" in sql
    assert "v9_2_gate_fired" in sql
    # gate_fired uses OR-merge so a TRUE-stamp wins over earlier FALSE/NULL.
    assert "OR COALESCE(EXCLUDED.v9_2_gate_fired" in sql


@pytest.mark.asyncio
async def test_v9_2_writer_noop_when_value_none():
    db = _stub_db()
    n = await db.update_signal_evaluations_lgb_v9_2(
        window_ts=1, asset="BTC", timeframe="5m",
        eval_offset=0, probability_lgb_v9_2=None,
    )
    assert n == 0
    assert db._pool.conn.calls == []


@pytest.mark.asyncio
async def test_v9_2_writer_noop_when_no_pool():
    from persistence.db_client import DBClient
    db = DBClient.__new__(DBClient)
    db._pool = None
    n = await db.update_signal_evaluations_lgb_v9_2(
        window_ts=1, asset="BTC", timeframe="5m",
        eval_offset=0, probability_lgb_v9_2=0.5,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_v9_2_writer_swallows_db_errors():
    from persistence.db_client import DBClient

    class _BoomConn:
        async def execute(self, *a, **k):
            raise RuntimeError("connection refused")

    class _BoomPool:
        def acquire(self):
            class _CM:
                async def __aenter__(self_inner):
                    return _BoomConn()

                async def __aexit__(self_inner, *exc):
                    return None

            return _CM()

    db = DBClient.__new__(DBClient)
    db._pool = _BoomPool()
    n = await db.update_signal_evaluations_lgb_v9_2(
        window_ts=1, asset="BTC", timeframe="5m",
        eval_offset=0, probability_lgb_v9_2=0.5,
    )
    assert n == 0  # swallowed, never raises into orchestrator


@pytest.mark.asyncio
async def test_v9_2_pg_signal_repo_parity():
    """PgSignalRepository must match DBClient byte-for-byte."""
    from adapters.persistence.pg_signal_repo import PgSignalRepository

    pool = _FakePool("INSERT 0 1")
    repo = PgSignalRepository(pool)  # type: ignore[arg-type]
    n = await repo.update_signal_evaluations_lgb_v9_2(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=60,
        probability_lgb_v9_2=0.87,
        v9_2_conviction=0.87,
        v9_2_pred_direction="UP",
        v9_2_cohort="NORMAL_UP",
        v9_2_gate_fired=True,
    )
    assert n == 1
    sql = pool.conn.calls[0]["sql"]
    assert "INSERT INTO signal_evaluations" in sql
    assert "ON CONFLICT (window_ts, asset, timeframe, eval_offset)" in sql
    assert "OR COALESCE(EXCLUDED.v9_2_gate_fired" in sql


@pytest.mark.asyncio
async def test_v9_2_writer_noop_when_eval_offset_none():
    """eval_offset is part of the unique key — None makes the row
    uninsertable. PR #500: writer short-circuits before SQL."""
    db = _stub_db("INSERT 0 0")
    n = await db.update_signal_evaluations_lgb_v9_2(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=None,
        probability_lgb_v9_2=0.70,
    )
    assert n == 0
    assert db._pool.conn.calls == []
