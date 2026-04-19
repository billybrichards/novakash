"""PR 4 fixes — three small UX / spam-control changes on the registry
trade-attempt card path.

1. ``ensemble_disagreement`` skip_reason shows lgb + path1 values and
   their predicted directions, not just the magnitude of their delta.
2. An execute-trade result with ``failure_reason == "already_traded"``
   classifies as ``SKIPPED_COOLDOWN`` (sibling strategy won the window
   claim) rather than ``FAILED_EXECUTION`` (real failure).
3. ``_fire_trade_attempt_card`` caps emission at
   ``self._attempt_card_cap`` per ``(strategy, window_ts, outcome)``
   tuple so retries at later eval offsets don't flood TG.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from strategies.data_surface import DataSurfaceManager, FullDataSurface
from strategies.registry import StrategyConfig, StrategyRegistry

CONFIGS_DIR = str(
    Path(__file__).resolve().parents[3] / "strategies" / "configs"
)


# ─── helpers ────────────────────────────────────────────────────────────


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


class _FakeAlerter:
    """Captures send_trade_attempt_result kwargs into a list."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_trade_attempt_result(self, **kwargs) -> None:
        self.calls.append(kwargs)


def _fake_exec_result(success: bool, failure_reason: str = None) -> Any:
    class R:
        pass

    r = R()
    r.success = success
    r.failure_reason = failure_reason
    r.fill_price = 0.374 if success else None
    r.stake_usd = 1.92 if success else None
    r.order_id = "0xabc" if success else None
    r.execution_mode = "fak" if success else "none"
    return r


@pytest.fixture
def registry_with_alerter():
    mgr = DataSurfaceManager(v4_base_url="http://fake")
    alerter = _FakeAlerter()
    reg = StrategyRegistry(CONFIGS_DIR, mgr, alerter=alerter)
    reg.load_all()
    return reg, alerter


# ─── Fix 1: ensemble_disagreement skip reason carries direction info ────


def test_ensemble_disagreement_skip_reason_shows_directions(registry_with_alerter):
    reg, _ = registry_with_alerter
    # Force the disagreement gate on for this test via gate_params.
    # p_lgb=0.18 → DOWN, p_classifier=0.53 → UP, |Δ|=0.35 > 0.20 threshold.
    reg.configs["v5_fresh"].gate_params["ensemble_disagreement_threshold"] = 0.20
    surface = _make_surface(
        poly_direction="DOWN",
        poly_trade_advised=True,
        poly_confidence=0.30,
        poly_confidence_distance=0.20,
        poly_timing="optimal",
        probability_lgb=0.18,
        probability_classifier=0.53,
        ensemble_config={"mode": "blend"},
    )
    decision = reg._evaluate_one(
        "v5_fresh", reg.configs["v5_fresh"], surface
    )
    assert decision.action == "SKIP"
    reason = decision.skip_reason or ""
    # The improved format surfaces both models + their predicted sides.
    assert "lgb=0.18(DOWN)" in reason, reason
    assert "path1=0.53(UP)" in reason, reason
    assert "|Δ|=0.350" in reason, reason


# ─── Fix 2: already_traded classifies as SKIPPED_COOLDOWN ───────────────


@pytest.mark.asyncio
async def test_already_traded_classifies_as_skipped_cooldown(registry_with_alerter):
    reg, alerter = registry_with_alerter
    # Build a TRADE decision — doesn't matter which strategy for this unit.
    from domain.value_objects import StrategyDecision

    dec = StrategyDecision(
        action="TRADE", direction="UP",
        confidence="HIGH", confidence_score=0.5,
        entry_cap=0.60, collateral_pct=0.10,
        strategy_id="v5_fresh", strategy_version="5.3.0",
        entry_reason="test", skip_reason=None,
        metadata={},
    )
    exec_result = _fake_exec_result(success=False, failure_reason="already_traded")
    await reg._fire_trade_attempt_card(
        strategy="v5_fresh",
        window_ts=1776523800,
        decision=dec,
        execution_result=exec_result,
        timeframe="5m",
    )
    assert len(alerter.calls) == 1
    assert alerter.calls[0]["outcome"] == "SKIPPED_COOLDOWN"


@pytest.mark.asyncio
async def test_real_execution_failure_still_classifies_as_failed(
    registry_with_alerter,
):
    reg, alerter = registry_with_alerter
    from domain.value_objects import StrategyDecision

    dec = StrategyDecision(
        action="TRADE", direction="UP",
        confidence="HIGH", confidence_score=0.5,
        entry_cap=0.60, collateral_pct=0.10,
        strategy_id="v5_fresh", strategy_version="5.3.0",
        entry_reason="test", skip_reason=None,
        metadata={},
    )
    # Real failure — e.g. price_cap_violated.
    exec_result = _fake_exec_result(
        success=False, failure_reason="price_cap_violated"
    )
    await reg._fire_trade_attempt_card(
        strategy="v5_fresh",
        window_ts=1776523800,
        decision=dec,
        execution_result=exec_result,
        timeframe="5m",
    )
    assert alerter.calls[-1]["outcome"] == "FAILED_EXECUTION"


# ─── Fix 3: per-(strategy, window, outcome) card cap ────────────────────


@pytest.mark.asyncio
async def test_attempt_card_cap_suppresses_repeat_failures(registry_with_alerter):
    reg, alerter = registry_with_alerter
    reg._attempt_card_cap = 2
    from domain.value_objects import StrategyDecision

    dec = StrategyDecision(
        action="TRADE", direction="UP",
        confidence="HIGH", confidence_score=0.5,
        entry_cap=0.60, collateral_pct=0.10,
        strategy_id="v5_fresh", strategy_version="5.3.0",
        entry_reason="test", skip_reason=None,
        metadata={},
    )
    exec_result = _fake_exec_result(success=False, failure_reason="price_cap_violated")
    for _ in range(5):
        await reg._fire_trade_attempt_card(
            strategy="v5_fresh",
            window_ts=1776523800,
            decision=dec,
            execution_result=exec_result,
            timeframe="5m",
        )
    # Cap=2 → first two fire, remaining three suppressed.
    assert len(alerter.calls) == 2


@pytest.mark.asyncio
async def test_cap_is_per_window_not_global(registry_with_alerter):
    reg, alerter = registry_with_alerter
    reg._attempt_card_cap = 1
    from domain.value_objects import StrategyDecision

    dec = StrategyDecision(
        action="TRADE", direction="UP",
        confidence="HIGH", confidence_score=0.5,
        entry_cap=0.60, collateral_pct=0.10,
        strategy_id="v5_fresh", strategy_version="5.3.0",
        entry_reason="test", skip_reason=None,
        metadata={},
    )
    exec_result = _fake_exec_result(success=False, failure_reason="price_cap_violated")
    # Window A: 1 allowed, extras suppressed.
    for _ in range(3):
        await reg._fire_trade_attempt_card(
            strategy="v5_fresh", window_ts=100, decision=dec,
            execution_result=exec_result, timeframe="5m",
        )
    # Window B: fresh cap.
    for _ in range(3):
        await reg._fire_trade_attempt_card(
            strategy="v5_fresh", window_ts=200, decision=dec,
            execution_result=exec_result, timeframe="5m",
        )
    # 1 per window × 2 windows = 2 cards total.
    assert len(alerter.calls) == 2
    assert {c["window_ts"] for c in alerter.calls} == {100, 200}


@pytest.mark.asyncio
async def test_cap_is_per_outcome_fills_never_suppressed(registry_with_alerter):
    """A FILLED card must always fire even after N prior FAILED_EXECUTION
    cards on the same (strategy, window) — different outcome tuple, and
    success is one-shot anyway.
    """
    reg, alerter = registry_with_alerter
    reg._attempt_card_cap = 1
    from domain.value_objects import StrategyDecision

    dec = StrategyDecision(
        action="TRADE", direction="UP",
        confidence="HIGH", confidence_score=0.5,
        entry_cap=0.60, collateral_pct=0.10,
        strategy_id="v5_fresh", strategy_version="5.3.0",
        entry_reason="test", skip_reason=None,
        metadata={},
    )
    fail = _fake_exec_result(success=False, failure_reason="price_cap_violated")
    # 3 fails on same window — cap=1 means 1 FAILED card, 2 suppressed.
    for _ in range(3):
        await reg._fire_trade_attempt_card(
            strategy="v5_fresh", window_ts=100, decision=dec,
            execution_result=fail, timeframe="5m",
        )
    # Then a fill succeeds — must fire.
    success = _fake_exec_result(success=True)
    await reg._fire_trade_attempt_card(
        strategy="v5_fresh", window_ts=100, decision=dec,
        execution_result=success, timeframe="5m",
    )
    outcomes = [c["outcome"] for c in alerter.calls]
    assert outcomes.count("FILLED") == 1
    assert outcomes.count("FAILED_EXECUTION") == 1
