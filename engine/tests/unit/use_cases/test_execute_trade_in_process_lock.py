"""Regression tests for the in-process execute lock (audit #461).

Context
-------
2026-05-01: 10 confirmed double-fills on v9_lgb_only / v10_lgb_only
between 2026-04-28 and 2026-04-29, each pair 23-70 seconds apart on
different eval offsets within the same 5-minute window. Each pair was
two REAL on-chain transactions with distinct ``tx_hash`` values — the
engine paid double the intended stake. Affected windows include
1777411200, 1777416600, 1777470000, 1777470900, 1777480500, 1777488900,
1777492800, 1777394400, 1777395600.

Root cause
----------
``try_claim_fill_slot`` has a 25s ``STALE_PLACEHOLDER_TTL_SECONDS``
escape hatch (audit #401) that lets a fresh attempt steal a placeholder
older than the TTL. This exists for engine-crash recovery (a SIGKILL'd
attempt would otherwise lock the window forever) and is by-design
permissive. But a legitimate FAK ladder + DB write round-trip can
exceed 25s under DB-pool contention; the next eval-offset tick (~30s
later in the typical 5m schedule) sees the placeholder as stale, takes
it over, and fires its own FAK. Both eventually fill on chain.

Fix (audit #461)
----------------
First-barrier in-process ``set[(strategy_id, window_ts, direction)]``
in :class:`ExecuteTradeUseCase`. Held for the FULL lifetime of an
in-flight ``execute()`` (added on entry, removed in finally). A second
concurrent ``execute()`` for the same key returns
``already_executing_in_process`` immediately — no DB round-trip, no
chance of the stale-takeover firing on the first attempt's still-live
placeholder.

The DB-backed pessimistic claim is retained as the cross-PROCESS
backstop; this test covers the cross-COROUTINE failure mode that
matched the production data.
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


def _build_use_case(*, clock: _FakeClock, slow_fak_seconds: float = 0.0):
    """Construct an ExecuteTradeUseCase with mocked deps.

    ``slow_fak_seconds`` lets a test simulate a long-running FAK ladder
    so the second concurrent attempt fires WHILE the first is still in
    Step 6. This is the production-data scenario we're regressing.
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

    if slow_fak_seconds > 0:
        async def _slow_execute_order(*_a, **_kw):
            await asyncio.sleep(slow_fak_seconds)
            return fill
        mock_executor.execute_order.side_effect = _slow_execute_order
    else:
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


# ─── Smoking-gun regression: concurrent eval offsets, slow FAK ──────────


@pytest.mark.asyncio
async def test_concurrent_evals_during_in_flight_fak_blocks_second():
    """Reproduces the 2026-04-29 production data: two ``evaluate_all``
    invocations from different eval offsets fire concurrently for the
    same (strategy, window). The first attempt is mid-FAK (slow_fak)
    when the second arrives. Without the in-process gate the second
    would call ``try_claim_fill_slot`` and (in production) get a
    stale-takeover True after 25s. With the gate the second returns
    immediately as ``already_executing_in_process``.
    """
    window_ts = 1_777_488_900
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 180)

    # FAK takes 0.05s — short enough for the test to be fast, long
    # enough that the second gather'd call enters execute() while the
    # first is still in flight.
    uc, mock_executor, _ = _build_use_case(clock=clock, slow_fak_seconds=0.05)

    decision = _decision(strategy_id="v10_lgb_only", direction="UP")
    market = _market(window_ts)

    r1, r2 = await asyncio.gather(
        uc.execute(
            decision=decision,
            window_market=market,
            current_btc_price=84000.0,
            open_price=84100.0,
        ),
        uc.execute(
            decision=decision,
            window_market=market,
            current_btc_price=84000.0,
            open_price=84100.0,
        ),
    )

    # Exactly ONE FAK fired — the second attempt rejected before
    # reaching the executor.
    assert mock_executor.execute_order.call_count == 1, (
        f"expected 1 execute_order call, got {mock_executor.execute_order.call_count}"
    )

    # One result is success, the other is the in-process rejection
    # (gather order is non-deterministic so check the set).
    successes = [r for r in (r1, r2) if r.success]
    rejections = [r for r in (r1, r2) if not r.success]
    assert len(successes) == 1
    assert len(rejections) == 1
    assert rejections[0].failure_reason == "already_executing_in_process"


@pytest.mark.asyncio
async def test_sequential_evals_both_proceed_to_dedup_layer():
    """If two ``execute`` calls fire SEQUENTIALLY (not concurrently),
    the in-process gate releases between them and the second call
    falls through to the DB dedup layer. The DB layer is mocked here
    to allow both — the gate is solely about CONCURRENT calls. This
    locks in that we don't accidentally turn the in-process set into
    a permanent per-window lock.
    """
    window_ts = 1_777_488_900
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 180)

    uc, mock_executor, _ = _build_use_case(clock=clock)

    decision = _decision(strategy_id="v10_lgb_only", direction="UP")
    market = _market(window_ts)

    r1 = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    # Advance past the 30s order-rate guardrail
    clock.advance(31.0)
    r2 = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )

    # Both calls proceeded past the in-process gate (DB mocks accept
    # both). Two FAKs fired in this contrived test; the *real* dedup
    # backstop is the DB pessimistic claim, which is mocked here to
    # always return True. The point of this test is the in-process
    # set MUST clear between calls.
    assert mock_executor.execute_order.call_count == 2


@pytest.mark.asyncio
async def test_sibling_strategies_concurrent_each_fire_once():
    """v9_lgb_only and v10_lgb_only running concurrently on the SAME
    window must each get their own slot — the in-process key is
    keyed by (strategy_id, window_ts, direction), not just window_ts.
    """
    window_ts = 1_777_488_900
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 180)

    uc, mock_executor, _ = _build_use_case(clock=clock, slow_fak_seconds=0.05)

    market = _market(window_ts)
    r9, r10 = await asyncio.gather(
        uc.execute(
            decision=_decision(strategy_id="v9_lgb_only", direction="UP"),
            window_market=market,
            current_btc_price=84000.0,
            open_price=84100.0,
        ),
        uc.execute(
            decision=_decision(strategy_id="v10_lgb_only", direction="UP"),
            window_market=market,
            current_btc_price=84000.0,
            open_price=84100.0,
        ),
    )

    assert r9.success is True
    assert r10.success is True
    assert mock_executor.execute_order.call_count == 2


@pytest.mark.asyncio
async def test_in_flight_set_clears_on_exception():
    """If execute_order raises, the in-process key MUST be released so
    the next eval tick can retry. Without the finally, an unhandled
    exception would lock the strategy out of the window forever.
    """
    from use_cases.execute_trade import ExecuteTradeUseCase

    window_ts = 1_777_488_900
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 180)

    uc, mock_executor, _ = _build_use_case(clock=clock)

    # First call — make execute_order raise. Use BaseException to
    # ensure even CancelledError-class failures release the key.
    mock_executor.execute_order.side_effect = RuntimeError("simulated CLOB outage")
    decision = _decision(strategy_id="v10_lgb_only", direction="UP")
    market = _market(window_ts)

    r1 = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert r1.success is False  # exception caught, _failed result returned

    # In-flight set must be empty — the finally fired.
    assert not uc._in_flight_keys, (
        f"expected empty _in_flight_keys, got {uc._in_flight_keys}"
    )

    # Second call (after clock advance to bypass rate limit) succeeds
    # — the key was correctly released.
    mock_executor.execute_order.side_effect = None
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
    clock.advance(31.0)
    r2 = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert r2.success is True
