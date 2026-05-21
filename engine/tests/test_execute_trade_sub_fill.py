"""Sub-fill writer regression test (Hub #554, 2026-05-20).

Scenario reconstructed from the 25 s gap between 16:07:59 and 16:08:24
fills on window 1779293100:

  1. Strategy fires FAK at T-X, fills as primary on Polymarket.
  2. Engine's strategy_window_fills row keeps the placeholder past the
     25 s STALE_PLACEHOLDER_TTL window because the DB pool is
     saturated.
  3. Next eval tick steals the stale placeholder, fires a SECOND FAK
     that also fills on Polymarket — same (asset, window_ts,
     timeframe, strategy_id).
  4. The second mark_traded sees an EXISTING real order_id in the
     row → returns WriteOutcome.SECONDARY_FILL.
  5. ExecuteTradeUseCase.Step 8a must call record_trade again with
     ``is_secondary_fill=True`` + ``parent_trade_id=<this call's
     order_id>`` so the second fill lands a tagged row.

This test asserts step 5.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from domain.ports import WriteOutcome
from domain.value_objects import (
    ExecutionResult,
    RiskStatus,
    StrategyDecision,
    WindowMarket,
)


class _FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self._t = t

    def now(self) -> float:
        return self._t


def _decision() -> StrategyDecision:
    return StrategyDecision(
        action="TRADE",
        direction="DOWN",
        confidence="HIGH",
        confidence_score=0.75,
        entry_cap=0.55,
        collateral_pct=0.025,
        strategy_id="v9_2_v12_combo",
        strategy_version="1.0.0",
        entry_reason="v9_2_v12_combo_T80_DOWN_secondary_fill_test",
        skip_reason=None,
        metadata={"min_offset_sec": 30, "eval_offset": 80},
    )


def _market() -> WindowMarket:
    return WindowMarket(
        condition_id="0xCONDITION",
        up_token_id="0xUP",
        down_token_id="0xDOWN",
        market_slug="btc-updown-5m-1779293100",
        active=True,
    )


def _fill_result() -> ExecutionResult:
    return ExecutionResult(
        success=True,
        order_id="0xSECONDARY_FILL_ORDER_ID",
        fill_price=0.55,
        fill_size=9.09,
        stake_usd=5.0,
        fee_usd=0.09,
        execution_mode="fak",
        token_id="0xDOWN",
        execution_start=1000.0,
        execution_end=1000.5,
    )


def _risk() -> RiskStatus:
    return RiskStatus(
        current_bankroll=500.0,
        peak_bankroll=520.0,
        drawdown_pct=0.04,
        daily_pnl=5.0,
        consecutive_losses=0,
        paper_mode=False,
        kill_switch_active=False,
    )


@pytest.mark.asyncio
async def test_mark_traded_secondary_fill_triggers_second_record_trade():
    """mark_traded → SECONDARY_FILL must trigger a second record_trade call
    flagged with is_secondary_fill=True + parent_trade_id."""
    from use_cases.execute_trade import ExecuteTradeUseCase

    # mocks
    mock_poly = AsyncMock()
    mock_executor = AsyncMock()
    mock_executor.execute_order.return_value = _fill_result()
    mock_risk = MagicMock()
    mock_risk.get_status.return_value = _risk()

    mock_window_state = AsyncMock()
    mock_window_state.try_claim_trade.return_value = (True, "claim-id-1")
    mock_window_state.has_filled.return_value = False
    mock_window_state.try_claim_fill_slot.return_value = True
    # THE bug under test — mark_traded returns SECONDARY_FILL because the
    # row already had a different real order_id from a prior fill.
    mock_window_state.mark_traded.return_value = WriteOutcome.SECONDARY_FILL

    mock_alerter = AsyncMock()
    mock_alerter.send_strategy_trade_alert = AsyncMock()
    mock_alerter.send_fill_confirmed = AsyncMock()

    mock_recorder = AsyncMock()

    uc = ExecuteTradeUseCase(
        polymarket=mock_poly,
        order_executor=mock_executor,
        risk_manager=mock_risk,
        window_state=mock_window_state,
        alerter=mock_alerter,
        trade_recorder=mock_recorder,
        clock=_FakeClock(),
        paper_mode=False,  # live so secondary-fill record uses real path
    )

    result = await uc.execute(
        decision=_decision(),
        window_market=_market(),
        current_btc_price=84231.0,
        open_price=84331.0,
    )

    # The trade itself succeeded — fill happened on CLOB.
    assert result.success
    assert result.order_id == "0xSECONDARY_FILL_ORDER_ID"

    # record_trade was called TWICE:
    #   1. Step 7  — primary record (default is_secondary_fill=False).
    #   2. Step 8a — sub-fill writer (is_secondary_fill=True).
    assert mock_recorder.record_trade.call_count == 2

    # Inspect the second call — must carry the secondary tag + parent.
    second_call = mock_recorder.record_trade.call_args_list[1]
    assert second_call.kwargs.get("is_secondary_fill") is True
    assert (
        second_call.kwargs.get("parent_trade_id")
        == "0xSECONDARY_FILL_ORDER_ID"
    )

    # First call (Step 7) had defaults — no is_secondary_fill kwarg set.
    first_call = mock_recorder.record_trade.call_args_list[0]
    assert first_call.kwargs.get("is_secondary_fill", False) is False
    assert first_call.kwargs.get("parent_trade_id") is None


@pytest.mark.asyncio
async def test_mark_traded_committed_does_not_trigger_secondary_record():
    """Sanity: a normal COMMITTED fill must NOT trigger a second record_trade."""
    from use_cases.execute_trade import ExecuteTradeUseCase

    mock_poly = AsyncMock()
    mock_executor = AsyncMock()
    mock_executor.execute_order.return_value = _fill_result()
    mock_risk = MagicMock()
    mock_risk.get_status.return_value = _risk()

    mock_window_state = AsyncMock()
    mock_window_state.try_claim_trade.return_value = (True, "claim-id-1")
    mock_window_state.has_filled.return_value = False
    mock_window_state.try_claim_fill_slot.return_value = True
    mock_window_state.mark_traded.return_value = WriteOutcome.COMMITTED

    mock_alerter = AsyncMock()
    mock_alerter.send_strategy_trade_alert = AsyncMock()
    mock_alerter.send_fill_confirmed = AsyncMock()

    mock_recorder = AsyncMock()

    uc = ExecuteTradeUseCase(
        polymarket=mock_poly,
        order_executor=mock_executor,
        risk_manager=mock_risk,
        window_state=mock_window_state,
        alerter=mock_alerter,
        trade_recorder=mock_recorder,
        clock=_FakeClock(),
        paper_mode=False,
    )

    result = await uc.execute(
        decision=_decision(),
        window_market=_market(),
        current_btc_price=84231.0,
        open_price=84331.0,
    )

    assert result.success
    # Only the primary record_trade — no sub-fill writer call.
    assert mock_recorder.record_trade.call_count == 1
    only_call = mock_recorder.record_trade.call_args_list[0]
    assert only_call.kwargs.get("is_secondary_fill", False) is False
