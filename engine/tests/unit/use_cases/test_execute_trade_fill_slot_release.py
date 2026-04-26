"""Regression tests for the fill-slot release leak (audit #398).

Context
-------
2026-04-26: THIRD smoking gun on the once-per-window invariant. After
the pessimistic fill-slot claim (#322 / PR #396) was deployed, window
1777240500 / v9_lgb_only left a 'pending' placeholder row in
strategy_window_fills with NO matching trade row — the strategy was
permanently locked out for the rest of the window. Manual DELETE was
the only recovery.

Root-cause forensics: the existing release paths only catch
``Exception``. ``asyncio.CancelledError`` (BaseException subclass since
Python 3.8) propagates THROUGH them — so any task cancellation between
``try_claim_fill_slot`` returning True and ``mark_traded`` succeeding
leaves the placeholder dangling.

Fix (audit #398): wrap the entire post-claim flow in ``try / finally``.
The ``committed`` flag is set ONLY after ``mark_traded`` succeeds; the
finally block releases the slot whenever ``committed=False`` regardless
of how the function exited.

These tests exercise the use-case boundary against a mocked
WindowStateRepository and lock in:

  1. FAK no-fill (success=False) → slot released as before (pre-existing
     release path still works).
  2. FAK book_error (auth/infra-style abort with success=False) → slot
     released as before.
  3. CancelledError mid-FAK → slot released via the finally backstop
     (THE 2026-04-26 bug).
  4. Successful fill → slot committed, NOT released.
"""

from __future__ import annotations

import asyncio
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
    """Construct an ExecuteTradeUseCase with mocked deps + an in-memory
    fill-slot tracker that mirrors the DB UNIQUE constraint.
    """
    from use_cases.execute_trade import ExecuteTradeUseCase

    fill_default = ExecutionResult(
        success=True,
        order_id="0x5285abc",
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
    mock_executor.execute_order.return_value = fill_default
    mock_risk = MagicMock()
    mock_risk.get_status.return_value = risk

    # In-memory fill-slot model. Track which (key, strategy_id) markers
    # are claimed; release_fill_slot removes them.
    claimed: set[str] = set()

    async def _try_claim_fill_slot(key, strategy_id):
        marker = f"{key.asset}/{key.window_ts}/{key.timeframe}/{strategy_id}"
        if marker in claimed:
            return False
        claimed.add(marker)
        return True

    async def _release_fill_slot(key, strategy_id):
        marker = f"{key.asset}/{key.window_ts}/{key.timeframe}/{strategy_id}"
        claimed.discard(marker)

    mock_window_state = AsyncMock()
    mock_window_state.try_claim_trade.return_value = (True, "test-claim-id")
    mock_window_state.was_traded.return_value = False
    mock_window_state.has_filled.return_value = False
    mock_window_state.try_claim_fill_slot.side_effect = _try_claim_fill_slot
    mock_window_state.release_fill_slot.side_effect = _release_fill_slot

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
    return uc, mock_executor, mock_window_state, claimed


# ─── #398 regression: FAK no-fill releases via existing path ─────────────


@pytest.mark.asyncio
async def test_release_on_fak_no_fill():
    """``success=False`` with a benign reason like ``fak_rfq_exhausted``
    must release the slot so the next eval tick within the window can
    re-attempt. Pre-existing path; the test exists to lock in the
    behaviour against further refactors.
    """
    window_ts = 1_777_240_500
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 180)

    uc, mock_executor, _ws, claimed = _build_use_case(clock=clock)

    mock_executor.execute_order.return_value = ExecutionResult(
        success=False,
        failure_reason="fak_rfq_exhausted; gtc_fallback_disabled",
        stake_usd=10.0,
        execution_mode="none",
        fak_attempts=2,
        fak_prices=[0.55, 0.50],
        token_id="0xDOWN",
        execution_start=clock.now(),
        execution_end=clock.now() + 0.5,
    )

    decision = _decision(strategy_id="v9_lgb_only")
    market = _market(window_ts)

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert result.success is False
    assert "BTC/1777240500/5m/v9_lgb_only" not in claimed


# ─── #398 regression: book_error releases via existing path ──────────────


@pytest.mark.asyncio
async def test_release_on_fak_book_error():
    """``success=False`` with a clob/book abort reason still releases
    the slot. Different code path inside the FAK ladder vs a benign
    no-fill but the use-case branch (``not result.success``) is shared.
    """
    window_ts = 1_777_240_500
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 180)

    uc, mock_executor, _ws, claimed = _build_use_case(clock=clock)

    mock_executor.execute_order.return_value = ExecutionResult(
        success=False,
        failure_reason="book_error: orderbook unavailable",
        stake_usd=10.0,
        execution_mode="none",
        fak_attempts=1,
        fak_prices=[0.55],
        token_id="0xDOWN",
        execution_start=clock.now(),
        execution_end=clock.now() + 0.5,
    )

    decision = _decision(strategy_id="v9_lgb_only")
    market = _market(window_ts)

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert result.success is False
    assert "BTC/1777240500/5m/v9_lgb_only" not in claimed


# ─── #398 regression THE BUG: CancelledError mid-FAK ─────────────────────


@pytest.mark.asyncio
async def test_release_on_cancelled_error_mid_fak():
    """SMOKING GUN regression. ``asyncio.CancelledError`` is a
    BaseException (not Exception) since Python 3.8. The pre-#398
    explicit ``except Exception`` paths could not catch it, so a
    cancellation mid-FAK would propagate UP through execute() with the
    placeholder still in the DB. Window was permanently locked out.

    With the finally-block backstop, the slot is released regardless
    of how execute() exits.
    """
    window_ts = 1_777_240_500
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 180)

    uc, mock_executor, _ws, claimed = _build_use_case(clock=clock)

    async def _cancel_mid_fak(*args, **kwargs):
        raise asyncio.CancelledError()

    mock_executor.execute_order.side_effect = _cancel_mid_fak

    decision = _decision(strategy_id="v9_lgb_only")
    market = _market(window_ts)

    with pytest.raises(asyncio.CancelledError):
        await uc.execute(
            decision=decision,
            window_market=market,
            current_btc_price=84000.0,
            open_price=84100.0,
        )

    # Slot was released via the finally block — strategy can retry
    # within the window on the next eval tick.
    assert "BTC/1777240500/5m/v9_lgb_only" not in claimed


# ─── #398 regression: success path preserves the slot ────────────────────


@pytest.mark.asyncio
async def test_success_path_does_not_release_slot():
    """On a successful fill, the slot must remain (mark_traded UPSERTs
    the placeholder to the real order_id). The finally backstop must
    NOT race a delete against the UPSERT.
    """
    window_ts = 1_777_240_500
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 180)

    uc, mock_executor, _ws, claimed = _build_use_case(clock=clock)
    # default mock returns success=True

    decision = _decision(strategy_id="v9_lgb_only")
    market = _market(window_ts)

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert result.success is True
    # Slot still held — represents the real (now-stamped) fill marker
    assert "BTC/1777240500/5m/v9_lgb_only" in claimed
