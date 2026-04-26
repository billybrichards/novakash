"""Regression tests for the pessimistic fill-slot claim (audit #322).

Context
-------
2026-04-26: SECOND smoking gun on the once-per-window invariant. After
the per-strategy filled-marker (#321) was deployed, v10_lgb_only STILL
filled SIX times on window 1777238100 (PID 838090, 21:18:24–21:20:56):

  * Step 0.5 ``has_filled()`` check observed False on every attempt —
    the marker is only written AFTER FAK confirms in Step 8, but the
    FAK ladder takes ~2 minutes per attempt.
  * Per-strategy lease (15s TTL) auto-expired between eval ticks while
    the previous attempt's FAK was still in flight. Each new attempt
    found a clean lease to acquire.
  * All six FAKs eventually confirmed; only the FIRST ``mark_traded``
    INSERT succeeded — the other five silently lost on the UNIQUE
    constraint AFTER the trades were already booked. Money on the
    table, marker absent.

Fix (audit #322): pessimistically claim ``strategy_window_fills`` with
a placeholder (``order_id = 'pending'``) BEFORE firing FAK. UNIQUE
constraint on (asset, window_ts, timeframe, strategy_id) ensures only
one concurrent attempt wins. ``has_filled()`` now observes the
placeholder row and short-circuits before the lease is even checked.

These tests exercise the wiring at the use-case boundary against a
mocked WindowStateRepository, locking in:

  1. Three concurrent attempts on the same (window, strategy) →
     exactly one fires FAK, the other two return
     ``already_claimed_this_window`` and never reach the executor.
  2. Sibling strategies remain independent — v9 and v10 BOTH win their
     respective slots on the same window.
  3. On FAK no-fill the slot is released so the next eval tick can
     retry within the same window.
  4. On execution_error (raised exception) the slot is released so the
     window doesn't get permanently locked out.
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
    """Construct an ExecuteTradeUseCase with mocked deps. Mirrors the
    helper in test_execute_trade_per_strategy_invariant.py.
    """
    from use_cases.execute_trade import ExecuteTradeUseCase

    fill = ExecutionResult(
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
    mock_executor.execute_order.return_value = fill
    mock_risk = MagicMock()
    mock_risk.get_status.return_value = risk

    mock_window_state = AsyncMock()
    mock_window_state.try_claim_trade.return_value = (True, "test-claim-id")
    mock_window_state.was_traded.return_value = False
    mock_window_state.has_filled.return_value = False
    mock_window_state.try_claim_fill_slot.return_value = True

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


# ─── Smoking-gun regression: 3 concurrent attempts → exactly 1 FAK ──────


@pytest.mark.asyncio
async def test_three_concurrent_attempts_only_one_fires_fak():
    """Exact reproduction of the 2026-04-26 v10_lgb_only / window
    1777238100 race. Three back-to-back eval ticks (lease auto-expires
    between each) — only the first wins the pessimistic fill-slot
    claim, the other two return ``already_claimed_this_window`` and
    never reach the executor.

    Failure mode this test would catch: if the pessimistic claim were
    moved AFTER ``execute_order`` (or removed entirely), the executor
    would be called 3 times and the assertion would catch 3 calls.
    """
    window_ts = 1_777_238_100  # the actual window from the smoking gun
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 180)

    uc, mock_executor, mock_window_state = _build_use_case(clock=clock)

    # Single in-process slot — exactly mirrors the DB UNIQUE constraint
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

    mock_window_state.try_claim_fill_slot.side_effect = _try_claim_fill_slot
    mock_window_state.release_fill_slot.side_effect = _release_fill_slot

    decision = _decision(strategy_id="v10_lgb_only")
    market = _market(window_ts)

    results = []
    # Three eval ticks — same window, same strategy. The 30s rate-limit
    # guardrail forces us to advance the clock between attempts; in the
    # production race the same 30s elapsed naturally between eval ticks.
    for i in range(3):
        r = await uc.execute(
            decision=decision,
            window_market=market,
            current_btc_price=84000.0,
            open_price=84100.0,
        )
        results.append(r)
        clock.advance(31.0)

    # Exactly one FAK fired
    assert mock_executor.execute_order.call_count == 1, (
        f"expected 1 execute_order call, got {mock_executor.execute_order.call_count}"
    )

    # First attempt won — successful fill
    assert results[0].success is True

    # Attempts 2 and 3 were rejected with the pessimistic-lock sentinel
    assert results[1].success is False
    assert results[1].failure_reason == "already_claimed_this_window"
    assert results[2].success is False
    assert results[2].failure_reason == "already_claimed_this_window"

    # The slot remains claimed (no release on success path) so future
    # attempts in the same window stay blocked.
    assert "BTC/1777238100/5m/v10_lgb_only" in claimed


@pytest.mark.asyncio
async def test_sibling_strategies_each_win_own_slot():
    """v9_lgb_only and v10_lgb_only BOTH legitimately fill the same
    window. The slot is keyed by strategy_id so they don't contend.
    """
    window_ts = 1_777_238_100
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 180)

    uc, mock_executor, mock_window_state = _build_use_case(clock=clock)

    claimed: set[str] = set()

    async def _try_claim_fill_slot(key, strategy_id):
        marker = f"{key.asset}/{key.window_ts}/{key.timeframe}/{strategy_id}"
        if marker in claimed:
            return False
        claimed.add(marker)
        return True

    mock_window_state.try_claim_fill_slot.side_effect = _try_claim_fill_slot

    market = _market(window_ts)

    r9 = await uc.execute(
        decision=_decision(strategy_id="v9_lgb_only"),
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert r9.success is True

    clock.advance(31.0)

    r10 = await uc.execute(
        decision=_decision(strategy_id="v10_lgb_only"),
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert r10.success is True

    # Both strategies reached the executor exactly once each
    assert mock_executor.execute_order.call_count == 2
    # And both slots are held (independent rows)
    assert "BTC/1777238100/5m/v9_lgb_only" in claimed
    assert "BTC/1777238100/5m/v10_lgb_only" in claimed


@pytest.mark.asyncio
async def test_fak_no_fill_releases_slot_for_retry():
    """If FAK comes back unfilled (empty book, taker wouldn't cross),
    the slot MUST be released so the next eval tick can re-attempt
    within the same window. Without this, a single ``gtc_unfilled``
    would lock out the strategy for the rest of the window — the
    opposite failure mode of the original bug.
    """
    window_ts = 1_777_238_100
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 180)

    uc, mock_executor, mock_window_state = _build_use_case(clock=clock)

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

    mock_window_state.try_claim_fill_slot.side_effect = _try_claim_fill_slot
    mock_window_state.release_fill_slot.side_effect = _release_fill_slot

    # First attempt: FAK comes back unfilled
    no_fill = ExecutionResult(
        success=False,
        order_id=None,
        fill_price=0.0,
        fill_size=0.0,
        stake_usd=10.0,
        fee_usd=0.0,
        execution_mode="paper",
        fak_attempts=2,
        fak_prices=[0.55, 0.50],
        token_id="0xDOWN",
        execution_start=clock.now(),
        execution_end=clock.now() + 0.5,
        failure_reason="fak_rfq_exhausted",
    )
    mock_executor.execute_order.return_value = no_fill

    market = _market(window_ts)
    decision = _decision(strategy_id="v10_lgb_only")

    r1 = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert r1.success is False
    # Slot was released — set is empty
    assert "BTC/1777238100/5m/v10_lgb_only" not in claimed

    # Second attempt within the same window can claim again and proceed
    clock.advance(31.0)
    fill = ExecutionResult(
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
    mock_executor.execute_order.return_value = fill

    r2 = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert r2.success is True
    assert mock_executor.execute_order.call_count == 2


@pytest.mark.asyncio
async def test_execution_error_releases_slot():
    """If the executor raises (real infra error, not a benign no-fill),
    the slot is released so a transient error doesn't permanently lock
    the window. The 30s rate limit + circuit breaker provide
    independent protection against runaway retries.
    """
    window_ts = 1_777_238_100
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 180)

    uc, mock_executor, mock_window_state = _build_use_case(clock=clock)

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

    mock_window_state.try_claim_fill_slot.side_effect = _try_claim_fill_slot
    mock_window_state.release_fill_slot.side_effect = _release_fill_slot

    mock_executor.execute_order.side_effect = RuntimeError("CLOB unreachable")

    decision = _decision(strategy_id="v10_lgb_only")
    market = _market(window_ts)

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert result.success is False
    assert (result.failure_reason or "").startswith("execution_error")
    # Slot was released
    assert "BTC/1777238100/5m/v10_lgb_only" not in claimed


@pytest.mark.asyncio
async def test_claim_fail_does_not_call_executor():
    """When ``try_claim_fill_slot`` returns False (we lost the race
    against another in-process attempt or another engine instance), we
    must NOT call ``execute_order``. This is the hard invariant the
    smoking gun violated.
    """
    window_ts = 1_777_238_100
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 180)

    uc, mock_executor, mock_window_state = _build_use_case(clock=clock)
    mock_window_state.try_claim_fill_slot.return_value = False

    decision = _decision(strategy_id="v10_lgb_only")
    market = _market(window_ts)

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert result.success is False
    assert result.failure_reason == "already_claimed_this_window"
    mock_executor.execute_order.assert_not_called()
    # And the lease (Step 1) was released so it doesn't TTL-lock siblings
    mock_window_state.clear_trade_claim.assert_called_once()
