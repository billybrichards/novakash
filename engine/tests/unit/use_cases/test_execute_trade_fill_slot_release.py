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


# ─── 2026-04-26 smoking gun: cancel AFTER fill, BEFORE mark_traded ────────


@pytest.mark.asyncio
async def test_cancel_after_fill_before_mark_traded_preserves_slot():
    """Window 1777245000 / v10_lgb_only: a real CLOB FAK fill returned
    success=True, then ``window_signal_dropped_oldest`` cancelled the
    in-flight execute_trade ~1.9s later — somewhere between
    record_trade and mark_traded. With ``committed = True`` only set
    AFTER mark_traded, the CancelledError propagated through both
    inner ``except Exception`` blocks (BaseException is not caught) and
    hit the outer finally with committed=False. The placeholder was
    deleted, a concurrent eval tick won try_claim_fill_slot, and we
    double-filled.

    Fix: set ``committed = True`` immediately after ``execute_order``
    returns success — the fill is real on CLOB at that point, so the
    placeholder must be preserved regardless of what happens to
    record_trade / mark_traded (including BaseException task drop).
    """
    window_ts = 1_777_245_000
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 222)  # mirrors live offset 222

    uc, mock_executor, mock_window_state, claimed = _build_use_case(clock=clock)
    # mock_executor returns success=True from default fixture

    # Cancel inside record_trade — happens AFTER execute_order returned
    # success but BEFORE the (pre-fix) committed=True assignment.
    async def _cancel_during_record(*args, **kwargs):
        raise asyncio.CancelledError()

    uc._recorder.record_trade.side_effect = _cancel_during_record

    decision = _decision(strategy_id="v10_lgb_only")
    market = _market(window_ts)

    with pytest.raises(asyncio.CancelledError):
        await uc.execute(
            decision=decision,
            window_market=market,
            current_btc_price=78311.84,
            open_price=78300.0,
        )

    # CRITICAL: slot is preserved. The fill is real on CLOB; deleting
    # the placeholder would let the next eval tick double-fill.
    assert "BTC/1777245000/5m/v10_lgb_only" in claimed


# ─── Orphan-GTC fix (2026-05-16 09:48 incident, audit #432) ───────────────


@pytest.mark.asyncio
async def test_orphan_gtc_cancelled_on_rollback():
    """When execute_order returns a gtc_resting result but the outer flow
    then bails before commit (e.g. CancelledError mid-record_trade or
    similar), the orphan GTC must be cancelled on CLOB.

    Pre-fix: the resting order sat live until window close, then filled
    30-60s later at $0.90 as a phantom outflow (full $56 bleed on the
    2026-05-16 09:48 UTC incident).

    Post-fix: the finally_uncommitted path calls polymarket.cancel_order
    with the returned order_id.
    """
    window_ts = 1_777_658_700
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 60)

    uc, mock_executor, _ws, claimed = _build_use_case(clock=clock)

    # Executor placed a resting GTC successfully.
    mock_executor.execute_order.return_value = ExecutionResult(
        success=True,
        order_id="0xORPHAN-GTC",
        fill_price=None,
        fill_size=None,
        stake_usd=10.0,
        fee_usd=0.0,
        execution_mode="gtc_resting",
        fak_attempts=4,
        fak_prices=[0.85, 0.87, 0.89, 0.92],
        token_id="0xDOWN",
        execution_start=clock.now(),
        execution_end=clock.now() + 0.5,
    )

    # Cancellation lands inside record_trade — between execute_order
    # returning success and committed=True being set the second time.
    # (For gtc_resting, success=True so committed=True fires at the
    # first commit point, BUT we still want to validate cancellation
    # behaviour if a future regression reverses that ordering. Use the
    # outer registry side-effect: raise via mark_traded which the inner
    # try/except DOES swallow, so we instead trigger via the
    # try_claim_fill_slot succeeding then forcing a BaseException via
    # record_trade.)
    async def _cancel_during_record(*args, **kwargs):
        raise asyncio.CancelledError()

    uc._recorder.record_trade.side_effect = _cancel_during_record

    # Wire a cancel_order tracker onto the polymarket mock.
    cancel_calls: list[str] = []

    async def _cancel_order(order_id):
        cancel_calls.append(order_id)
        return True

    uc._polymarket.cancel_order = _cancel_order

    decision = _decision(strategy_id="v9_2_v12_combo")
    market = _market(window_ts)

    # Replace the AsyncMock executor with a real fake so attribute
    # access in the finally block (cancel_active_gtc) routes to our
    # registry-aware coroutine, not an auto-generated AsyncMock.
    class _FakeExecutorWithRegistry:
        def __init__(self):
            self._active_gtc: dict = {}

        async def execute_order(self, **kwargs):
            # Simulate executor having registered an active GTC then
            # being cancelled mid-poll. Mirrors what _try_gtc does after
            # place_order returns but before the poll loop completes.
            self._active_gtc[
                ("v9_2_v12_combo", "0xDOWN", "NO")
            ] = {
                "order_id": "0xORPHAN-GTC",
                "placed_at": clock.now(),
                "close_ts": float(close_ts),
            }
            raise asyncio.CancelledError()

        async def cancel_active_gtc(self, strategy_id, token_id, side):
            entry = self._active_gtc.pop((strategy_id, token_id, side), None)
            if entry:
                await _cancel_order(entry["order_id"])
                return True
            return False

    uc._executor = _FakeExecutorWithRegistry()
    uc._recorder.record_trade.side_effect = None  # reset

    with pytest.raises(asyncio.CancelledError):
        await uc.execute(
            decision=decision,
            window_market=market,
            current_btc_price=84000.0,
            open_price=84100.0,
        )

    # The orphan GTC must have been cancelled via the registry hook.
    assert "0xORPHAN-GTC" in cancel_calls, (
        f"cancel_order must have been called for the orphan GTC; got {cancel_calls}"
    )


@pytest.mark.asyncio
async def test_cancel_during_mark_traded_preserves_slot():
    """Same root cause as above but the cancellation lands inside
    ``mark_traded`` (the UPSERT under DB pool saturation). Pre-fix this
    also slipped past the inner ``except Exception`` and finally
    released the slot. Post-fix the committed flag was already set
    after ``execute_order`` returned success, so finally is a no-op.
    """
    window_ts = 1_777_245_000
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 222)

    uc, mock_executor, mock_window_state, claimed = _build_use_case(clock=clock)

    async def _cancel_during_mark(*args, **kwargs):
        raise asyncio.CancelledError()

    mock_window_state.mark_traded.side_effect = _cancel_during_mark

    decision = _decision(strategy_id="v10_lgb_only")
    market = _market(window_ts)

    with pytest.raises(asyncio.CancelledError):
        await uc.execute(
            decision=decision,
            window_market=market,
            current_btc_price=78311.84,
            open_price=78300.0,
        )

    assert "BTC/1777245000/5m/v10_lgb_only" in claimed
