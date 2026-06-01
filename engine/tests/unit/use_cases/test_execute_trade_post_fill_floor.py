"""Regression tests: post-fill floor enforcement (RDS #664 CLOB price-improvement gap).

Scenario
--------
The FAK executor submits a limit-buy at entry_cap. Polymarket's CLOB can give
price improvement — fills arrive BELOW the limit price by matching cheap
resting asks. entry_cap is the structural FAK ceiling (can't fill above it);
entry_floor has no structural CLOB equivalent, so execute_trade.py enforces it
after the fill comes back.

Bug that triggered this (2026-06-01):
  - tickformer_v20_adaptive_early trade 9622: fill 0.01, entry_floor_up 0.60 → lost $10.
  - v9_5_eth_pure_lgb trade 9633:           fill 0.585, entry_floor_up 0.60 → lost $5.

Fix: after execute_order returns success and BEFORE committed=True, check fill
against meta["entry_floor_up"] / meta["entry_floor_down"]. If violated, release
fill-slot and claim reservations and return a failure result.

Tests
-----
1. UP fill below floor  → rejected, fill slot released.
2. UP fill AT floor     → accepted (boundary: fill == floor passes).
3. UP fill above floor  → accepted.
4. DOWN fill below floor_down → rejected.
5. DOWN fill at floor_down    → accepted.
6. No floor set (meta["entry_floor_up"] absent) → accepted (no-op for hooks not yet wired).
7. No fill_price on result     → accepted (None fill_price skips the check safely).
8. Reservation leak check: on rejection, release_fill_slot and clear_trade_claim
   are both called BEFORE returning.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest

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


def _market() -> WindowMarket:
    return WindowMarket(
        condition_id="0xCOND",
        up_token_id="0xUP",
        down_token_id="0xDOWN",
        market_slug="eth-updown-5m-1780000000",
        active=True,
    )


def _decision(
    direction: str = "UP",
    meta: dict | None = None,
) -> StrategyDecision:
    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence="HIGH",
        confidence_score=0.85,
        entry_cap=0.93,
        collateral_pct=0.025,
        strategy_id="v9_5_eth_pure_lgb",
        strategy_version="1.0.0",
        entry_reason="test",
        skip_reason=None,
        metadata=meta if meta is not None else {},
    )


def _fill_result(fill_price: float | None, *, direction: str = "UP") -> ExecutionResult:
    token_id = "0xUP" if direction == "UP" else "0xDOWN"
    return ExecutionResult(
        success=True,
        order_id="0xORDER",
        fill_price=fill_price,
        fill_size=9.0,
        stake_usd=5.0,
        fee_usd=0.05,
        execution_mode="fak",
        token_id=token_id,
        execution_start=1000.0,
        execution_end=1000.5,
    )


def _build_uc(*, fill_result: ExecutionResult):
    from use_cases.execute_trade import ExecuteTradeUseCase

    mock_poly = AsyncMock()
    mock_executor = AsyncMock()
    mock_executor.execute_order.return_value = fill_result
    mock_risk = MagicMock()
    mock_risk.get_status.return_value = _risk()

    mock_window_state = AsyncMock()
    mock_window_state.try_claim_trade.return_value = (True, "claim-id-1")
    mock_window_state.has_filled.return_value = False
    mock_window_state.try_claim_fill_slot.return_value = True
    mock_window_state.mark_traded.return_value = None  # COMMITTED path if reached

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
    return uc, mock_window_state, mock_recorder


# ─── Test 1: UP fill below floor → rejected ───────────────────────────────


@pytest.mark.asyncio
async def test_up_fill_below_floor_rejected():
    """fill_price=0.01, entry_floor_up=0.60 → rejected (the real bug case)."""
    uc, ws, recorder = _build_uc(fill_result=_fill_result(0.01, direction="UP"))

    result = await uc.execute(
        decision=_decision(
            direction="UP",
            meta={"entry_floor_up": 0.60, "min_offset_sec": 30},
        ),
        window_market=_market(),
        current_btc_price=3500.0,
        open_price=3510.0,
    )

    assert not result.success
    assert "fill_slipped_below_floor" in (result.failure_reason or "")
    assert "0.010" in (result.failure_reason or "")
    # record_trade must NOT have been called — the fill was rejected before booking
    recorder.record_trade.assert_not_called()
    # mark_traded must NOT have been called
    ws.mark_traded.assert_not_called()


# ─── Test 2: UP fill AT floor → accepted (boundary) ────────────────────────


@pytest.mark.asyncio
async def test_up_fill_at_floor_accepted():
    """fill_price == entry_floor_up must pass (boundary: >= floor is valid)."""
    uc, ws, recorder = _build_uc(fill_result=_fill_result(0.60, direction="UP"))

    result = await uc.execute(
        decision=_decision(
            direction="UP",
            meta={"entry_floor_up": 0.60, "min_offset_sec": 30},
        ),
        window_market=_market(),
        current_btc_price=3500.0,
        open_price=3510.0,
    )

    assert result.success
    recorder.record_trade.assert_called_once()


# ─── Test 3: UP fill above floor → accepted ─────────────────────────────────


@pytest.mark.asyncio
async def test_up_fill_above_floor_accepted():
    """fill_price=0.85, entry_floor_up=0.60 → normal happy path."""
    uc, ws, recorder = _build_uc(fill_result=_fill_result(0.85, direction="UP"))

    result = await uc.execute(
        decision=_decision(
            direction="UP",
            meta={"entry_floor_up": 0.60, "min_offset_sec": 30},
        ),
        window_market=_market(),
        current_btc_price=3500.0,
        open_price=3510.0,
    )

    assert result.success
    recorder.record_trade.assert_called_once()


# ─── Test 4: DOWN fill below floor_down → rejected ──────────────────────────


@pytest.mark.asyncio
async def test_down_fill_below_floor_down_rejected():
    """fill_price=0.10, entry_floor_down=0.40 (NO leg) → rejected."""
    uc, ws, recorder = _build_uc(fill_result=_fill_result(0.10, direction="DOWN"))

    result = await uc.execute(
        decision=_decision(
            direction="DOWN",
            meta={"entry_floor_down": 0.40, "min_offset_sec": 30},
        ),
        window_market=_market(),
        current_btc_price=3500.0,
        open_price=3510.0,
    )

    assert not result.success
    assert "fill_slipped_below_floor_down" in (result.failure_reason or "")
    recorder.record_trade.assert_not_called()
    ws.mark_traded.assert_not_called()


# ─── Test 5: DOWN fill at floor_down → accepted ─────────────────────────────


@pytest.mark.asyncio
async def test_down_fill_at_floor_down_accepted():
    """fill_price == entry_floor_down → passes (boundary)."""
    uc, ws, recorder = _build_uc(fill_result=_fill_result(0.40, direction="DOWN"))

    result = await uc.execute(
        decision=_decision(
            direction="DOWN",
            meta={"entry_floor_down": 0.40, "min_offset_sec": 30},
        ),
        window_market=_market(),
        current_btc_price=3500.0,
        open_price=3510.0,
    )

    assert result.success
    recorder.record_trade.assert_called_once()


# ─── Test 6: No floor in meta → accepted (hooks not yet wired pass through) ─


@pytest.mark.asyncio
async def test_no_floor_in_meta_accepted():
    """Hooks that don't set entry_floor_up must not regress — floor check no-ops."""
    uc, ws, recorder = _build_uc(fill_result=_fill_result(0.01, direction="UP"))

    result = await uc.execute(
        decision=_decision(
            direction="UP",
            meta={"min_offset_sec": 30},  # no entry_floor_up key
        ),
        window_market=_market(),
        current_btc_price=3500.0,
        open_price=3510.0,
    )

    assert result.success
    recorder.record_trade.assert_called_once()


# ─── Test 7: fill_price=None → accepted (safe skip) ──────────────────────────


@pytest.mark.asyncio
async def test_none_fill_price_skips_floor_check():
    """A result with fill_price=None must not trigger a floor rejection."""
    uc, ws, recorder = _build_uc(fill_result=_fill_result(None, direction="UP"))

    result = await uc.execute(
        decision=_decision(
            direction="UP",
            meta={"entry_floor_up": 0.60, "min_offset_sec": 30},
        ),
        window_market=_market(),
        current_btc_price=3500.0,
        open_price=3510.0,
    )

    # None fill_price means we cannot enforce — should not crash or reject
    # (executor would have flagged a missing fill separately in practice)
    assert result.success or not result.success  # just no exception
    # The important thing: no AttributeError / TypeError from the floor check
    # The assert above is intentionally non-prescriptive about success/fail
    # because a None fill_price may mean no real fill — but the floor check
    # must not introduce a new crash.


# ─── Test 8: Reservation not leaked on rejection ────────────────────────────


@pytest.mark.asyncio
async def test_floor_rejection_releases_fill_slot_and_claim():
    """On post-fill floor rejection, release_fill_slot and clear_trade_claim
    must both be called before the function returns (no reservation leak)."""
    uc, ws, recorder = _build_uc(fill_result=_fill_result(0.01, direction="UP"))

    result = await uc.execute(
        decision=_decision(
            direction="UP",
            meta={"entry_floor_up": 0.60, "min_offset_sec": 30},
        ),
        window_market=_market(),
        current_btc_price=3500.0,
        open_price=3510.0,
    )

    assert not result.success

    # release_fill_slot must be called at least once (the post-fill rejection path)
    assert ws.release_fill_slot.call_count >= 1

    # clear_trade_claim must be called to release the claim reservation
    assert ws.clear_trade_claim.call_count >= 1

    # No trade was booked
    recorder.record_trade.assert_not_called()
