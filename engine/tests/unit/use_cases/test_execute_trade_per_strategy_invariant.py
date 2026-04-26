"""Regression tests for the once-per-window-per-strategy invariant.

Context
-------
2026-04-26: v10_lgb_only filled 3x on the SAME window 1777234800
(20:21:12, 20:21:40, 20:22:11). Forensics:

* PR #390 added per-strategy lease keys and demoted the legacy
  ``was_traded`` check to ``elif`` (only runs when ``try_claim_trade``
  is absent). With the new repo it is never reached.
* The lease itself is in-flight protection only — 15s TTL, released on
  every fill (PR #391 added explicit release on early-return).
* So once a strategy filled, the row deleted, ~28s later the same
  strategy could acquire a fresh lease and fire FAK again.

Fix (audit #321): a NEW per-strategy terminal marker
(``strategy_window_fills``) backs ``has_filled(key, strategy_id)``,
checked BEFORE lease acquisition in :class:`ExecuteTradeUseCase`.

These tests exercise the wiring at the use-case boundary against
mocked WindowStateRepository, locking in:

  1. A strategy that already filled this window cannot fill again —
     the executor is never reached, the lease is never touched.
  2. Two different strategies CAN both fill the same window — they are
     independent, has_filled is keyed by strategy_id.
  3. Backfill compatibility: when has_filled returns False (the
     historical default before the marker existed), the trade
     proceeds normally and mark_traded threads strategy_id to record
     the marker for next time.
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from domain.value_objects import (
    ExecutionResult,
    RiskStatus,
    StrategyDecision,
    WindowKey,
    WindowMarket,
)


class _FakeClock:
    def __init__(self, start: float) -> None:
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += float(seconds)


def _decision(
    *,
    strategy_id: str = "v10_lgb_only",
    direction: str = "DOWN",
    metadata: Optional[dict] = None,
) -> StrategyDecision:
    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence="HIGH",
        confidence_score=0.75,
        entry_cap=0.65,
        collateral_pct=0.025,
        strategy_id=strategy_id,
        strategy_version="10.0.0",
        entry_reason="test",
        skip_reason=None,
        metadata=metadata or {"min_offset_sec": 30},
    )


def _market(window_ts: int, timeframe: str = "5m") -> WindowMarket:
    return WindowMarket(
        condition_id=f"0xcond-{window_ts}",
        up_token_id="0xUP",
        down_token_id="0xDOWN",
        market_slug=f"btc-updown-{timeframe}-{window_ts}",
        active=True,
    )


def _build_use_case(*, clock: _FakeClock):
    """ExecuteTradeUseCase with mocks. Mirror of the helper used by
    test_execute_trade_timing_recheck.py."""
    from use_cases.execute_trade import ExecuteTradeUseCase

    fill = ExecutionResult(
        success=True,
        order_id="paper-xyz",
        fill_price=0.55,
        fill_size=18.18,
        stake_usd=10.0,
        fee_usd=0.18,
        execution_mode="paper",
        fak_attempts=0,
        fak_prices=[],
        token_id="0xDOWN",
        execution_start=clock.now(),
        execution_end=clock.now() + 0.5,
    )

    risk = RiskStatus(
        current_bankroll=500.0,
        peak_bankroll=520.0,
        drawdown_pct=0.04,
        daily_pnl=5.0,
        consecutive_losses=0,
        paper_mode=True,
        kill_switch_active=False,
    )

    mock_poly = AsyncMock()
    mock_executor = AsyncMock()
    mock_executor.execute_order.return_value = fill
    mock_risk = MagicMock()
    mock_risk.get_status.return_value = risk
    mock_window_state = AsyncMock()
    mock_window_state.try_claim_trade.return_value = (True, "test-claim-id")
    mock_window_state.was_traded.return_value = False
    mock_window_state.has_filled.return_value = False
    mock_alerter = AsyncMock()
    mock_recorder = AsyncMock()

    uc = ExecuteTradeUseCase(
        polymarket=mock_poly,
        order_executor=mock_executor,
        risk_manager=mock_risk,
        window_state=mock_window_state,
        alerter=mock_alerter,
        trade_recorder=mock_recorder,
        clock=clock,
        paper_mode=True,
    )
    return uc, mock_executor, mock_window_state


# ─── Regression: same strategy cannot fill the same window twice ─────────


@pytest.mark.asyncio
async def test_strategy_cannot_fill_same_window_twice():
    """Smoking-gun regression for window 1777234800 / v10_lgb_only.

    has_filled returns True → must abort BEFORE acquiring the lease,
    BEFORE calling the executor. Failure reason is the new
    ``already_filled_this_window`` sentinel so log analysis can
    distinguish from in-flight ``already_traded``.
    """
    window_ts = 1_777_234_800  # the actual window from the 2026-04-26 incident
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 90)  # T-90, well within trading range

    uc, mock_executor, mock_window_state = _build_use_case(clock=clock)
    mock_window_state.has_filled.return_value = True
    decision = _decision(strategy_id="v10_lgb_only")
    market = _market(window_ts)

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )

    assert result.success is False
    assert result.failure_reason == "already_filled_this_window"
    # Hard invariants — these are what the bug violated:
    mock_executor.execute_order.assert_not_called()
    mock_window_state.try_claim_trade.assert_not_called()
    mock_window_state.clear_trade_claim.assert_not_called()
    # And we did consult the marker with the strategy_id from the decision:
    mock_window_state.has_filled.assert_called_once()
    call_kwargs = mock_window_state.has_filled.call_args
    # has_filled(window_key, strategy_id) — second positional or kwarg
    args = call_kwargs.args
    assert args[1] == "v10_lgb_only"


@pytest.mark.asyncio
async def test_different_strategies_can_fill_same_window():
    """Sibling strategies (v9_lgb_only, v10_lgb_only) MUST be allowed to
    each fill the same window. The marker is keyed by strategy_id, so
    has_filled for one returns True but for the other returns False.

    Critically, this is the property that motivated putting the marker
    in a NEW table (strategy_window_fills) instead of bolting strategy_id
    onto window_states which has UNIQUE (asset, window_ts, timeframe).
    """
    window_ts = 1_777_234_800
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 90)

    # Simulate a real per-strategy marker store: a set of strategy_ids
    # that have filled this specific window.
    filled: set[str] = set()

    async def _has_filled(key, strategy_id):
        return strategy_id in filled

    async def _mark_traded(key, order_id, strategy_id=None):
        if strategy_id:
            filled.add(strategy_id)

    uc, mock_executor, mock_window_state = _build_use_case(clock=clock)
    mock_window_state.has_filled.side_effect = _has_filled
    mock_window_state.mark_traded.side_effect = _mark_traded

    market = _market(window_ts)

    # v9 fills first
    r9 = await uc.execute(
        decision=_decision(strategy_id="v9_lgb_only"),
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert r9.success is True
    assert "v9_lgb_only" in filled
    assert "v10_lgb_only" not in filled  # critical: v10 not blocked

    # The use case enforces a 30s rate-limit guardrail between fills —
    # advance the clock so the second strategy isn't rate-limited.
    clock.advance(31.0)

    # v10 fills the SAME window — must succeed (independent strategy_id)
    r10 = await uc.execute(
        decision=_decision(strategy_id="v10_lgb_only"),
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert r10.success is True
    assert "v10_lgb_only" in filled

    # Both strategies reached the executor exactly once each.
    assert mock_executor.execute_order.call_count == 2


@pytest.mark.asyncio
async def test_has_filled_threads_strategy_id_to_mark_traded():
    """When has_filled returns False (no prior fill) and the trade
    succeeds, mark_traded MUST be called with strategy_id so the marker
    is written for next time. Without this, the very next eval tick
    would see has_filled=False again and fire another FAK.
    """
    window_ts = 1_777_234_800
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 90)

    uc, mock_executor, mock_window_state = _build_use_case(clock=clock)
    decision = _decision(strategy_id="v10_lgb_only")
    market = _market(window_ts)

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )

    assert result.success is True
    mock_executor.execute_order.assert_called_once()
    mock_window_state.mark_traded.assert_called_once()
    # Either positional (key, order_id, strategy_id) or kwargs.
    call = mock_window_state.mark_traded.call_args
    threaded_strategy = call.kwargs.get("strategy_id")
    if threaded_strategy is None and len(call.args) >= 3:
        threaded_strategy = call.args[2]
    assert threaded_strategy == "v10_lgb_only"


@pytest.mark.asyncio
async def test_has_filled_db_error_falls_through_to_lease():
    """Defensive: if has_filled raises (DB blip), we must fall through to
    the lease check rather than freezing the strategy. The 15s lease
    TTL is the backstop — a duplicate fill in that 15s is a worse-case
    cost than refusing to trade for the rest of the day on every
    transient DB error.
    """
    window_ts = 1_777_234_800
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 90)

    uc, mock_executor, mock_window_state = _build_use_case(clock=clock)
    mock_window_state.has_filled.side_effect = RuntimeError("connection lost")
    decision = _decision(strategy_id="v10_lgb_only")
    market = _market(window_ts)

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )

    # Trade still executes — lease provided in-flight protection.
    assert result.success is True
    mock_window_state.try_claim_trade.assert_called_once()
    mock_executor.execute_order.assert_called_once()
