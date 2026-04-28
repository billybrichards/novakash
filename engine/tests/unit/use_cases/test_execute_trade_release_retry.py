"""Tests for the failure-path release retry (PR fix/clob-wallet-and-fak-retry).

Context
-------
2026-04-27: User-reported "FAK fails -> ~minute wait before retry". The
60s ``STALE_PLACEHOLDER_TTL_SECONDS`` was the worst-case safety net, but
the real cause of the perceived delay was timing out the
``release_fill_slot`` DELETE under DB pool saturation. When the DELETE
times out, ``slot_released`` stays False; the next eval tick observes
the leaked 'pending' placeholder via ``has_filled`` and is blocked until
TTL expiry.

Fix
----
1. Lower ``STALE_PLACEHOLDER_TTL_SECONDS`` from 60s -> 25s. Still ≥
   FAK_LADDER_MAX_ELAPSED_S (20s) so we never steal an in-flight attempt.
2. On the failure path, retry the DELETE once when the first attempt
   times out. Transient pool blips (the common case) recover instantly;
   a true outage falls through to the (now 25s) TTL.

These tests verify (2) at the use-case boundary.
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


def _build_use_case_with_release_calls(
    *,
    clock: _FakeClock,
    release_behaviour,
):
    """Build use case where release_fill_slot calls follow ``release_behaviour``.

    ``release_behaviour`` is a list of callables. Each call to
    release_fill_slot pops the next callable and awaits it.
    """
    from use_cases.execute_trade import ExecuteTradeUseCase

    fail_result = ExecutionResult(
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
    mock_executor.execute_order.return_value = fail_result
    mock_risk = MagicMock()
    mock_risk.get_status.return_value = risk

    release_call_count = {"n": 0}

    async def _release(*args, **kwargs):
        idx = release_call_count["n"]
        release_call_count["n"] += 1
        if idx >= len(release_behaviour):
            return  # default no-op for finally backstop, etc.
        await release_behaviour[idx]()

    mock_window_state = AsyncMock()
    mock_window_state.try_claim_trade.return_value = (True, "test-claim-id")
    mock_window_state.was_traded.return_value = False
    mock_window_state.has_filled.return_value = False
    mock_window_state.try_claim_fill_slot.return_value = True
    mock_window_state.release_fill_slot.side_effect = _release

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
    return uc, mock_window_state, release_call_count


@pytest.mark.asyncio
async def test_release_retry_recovers_from_transient_timeout(monkeypatch):
    """First DELETE times out (DB pool blip), retry succeeds within
    DB_AWAIT_TIMEOUT_S. The strategy unblocks immediately — no 60s wait.
    """
    # Tighten the timeout so the test runs fast; behaviour is identical.
    monkeypatch.setenv("EXECUTE_TRADE_DB_TIMEOUT_S", "0.1")
    # Reload use_cases.execute_trade so DB_AWAIT_TIMEOUT_S picks up env.
    import importlib
    import use_cases.execute_trade as et
    importlib.reload(et)

    window_ts = 1_777_240_500
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 180)

    async def _hang_then_die():
        # Hang past the timeout so wait_for raises asyncio.TimeoutError.
        await asyncio.sleep(10)

    async def _quick_ok():
        return None

    uc, ws, counts = _build_use_case_with_release_calls(
        clock=clock,
        release_behaviour=[_hang_then_die, _quick_ok],
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
    # Two release attempts: original timed out, retry succeeded.
    assert counts["n"] == 2


@pytest.mark.asyncio
async def test_release_retry_falls_through_on_repeated_timeout(monkeypatch):
    """If both DELETE attempts time out, fall through to the TTL safety
    net — the test verifies we attempt twice and don't crash. The next
    eval tick relies on STALE_PLACEHOLDER_TTL_SECONDS to recover.
    """
    monkeypatch.setenv("EXECUTE_TRADE_DB_TIMEOUT_S", "0.05")
    import importlib
    import use_cases.execute_trade as et
    importlib.reload(et)

    window_ts = 1_777_240_500
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 180)

    async def _hang():
        await asyncio.sleep(10)

    uc, ws, counts = _build_use_case_with_release_calls(
        clock=clock,
        release_behaviour=[_hang, _hang],
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
    # Two timeouts then the finally backstop is short-circuited because
    # ``slot_released`` stays False but the `if not slot_claimed or
    # slot_released` early-return is False — the finally backstop will
    # try a third release. Allow >=2 to keep the test resilient against
    # the finally-backstop also firing.
    assert counts["n"] >= 2


def test_stale_placeholder_ttl_is_25_seconds():
    """Pin the lowered TTL constant. 25s = FAK ladder hard cap (20s) +
    5s buffer. Tightens the worst-case retry wait from 60s -> 25s.
    """
    from adapters.persistence.pg_window_repo import PgWindowRepository
    assert PgWindowRepository.STALE_PLACEHOLDER_TTL_SECONDS == 25
