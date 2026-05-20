"""Regression tests for the STRICT (strategy_id, window_ts, direction)
HARD lock added at Step -0.5 of ExecuteTradeUseCase._execute_locked.

Driver incident: 2026-05-20 ETH `v9_2_eth_raw_lgb` fired DOWN 3x on window
1779312000 in 40 seconds, -$21.91 net. Every TTL-based dedup mechanism
(15s lease / 25s placeholder / 30s order-interval) cleared between fires.

The new gate queries the trades table directly via
TradeRepository.has_fill_for_strategy_window_direction(...). This is the
canonical artefact of "a fill happened" and survives every TTL/marker
failure mode. See `tasks/dedup_audit_2026-05-20.md` for the full audit.

These tests MUST fail without Step -0.5 in execute_trade.py and pass with
it. They run with pure mocks — no I/O.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from typing import Optional

from domain.value_objects import (
    ExecutionResult,
    RiskStatus,
    StrategyDecision,
    WindowKey,
    WindowMarket,
)


def _decision(strategy_id: str = "v9_2_eth_raw_lgb", direction: str = "DOWN") -> StrategyDecision:
    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence=0.85,
        confidence_score=0.40,
        strategy_id=strategy_id,
        strategy_version="9.2",
        entry_cap=0.86,
        collateral_pct=0.025,
        gtc_cap=0.86,
        entry_reason=f"{strategy_id}_pass",
        skip_reason=None,
        metadata={"min_offset_sec": 30, "max_offset_sec": 150},
    )


def _market(window_ts: int = 1779312000, asset: str = "ETH") -> WindowMarket:
    return WindowMarket(
        market_slug=f"{asset.lower()}-updown-5m-{window_ts}",
        up_token_id="0xUP_TOK",
        down_token_id="0xDOWN_TOK",
        asset=asset,
        window_ts=window_ts,
        condition_id=f"0xcond_{window_ts}",
    )


class _Clock:
    def __init__(self, t: float = 1779311880.0):
        self._t = t

    def now(self) -> float:
        return self._t


def _risk() -> RiskStatus:
    return RiskStatus(
        current_bankroll=100.0, peak_bankroll=100.0, drawdown_pct=0.0,
        daily_pnl=0.0, consecutive_losses=0, paper_mode=False,
        kill_switch_active=False,
    )


def _fill_result() -> ExecutionResult:
    return ExecutionResult(
        success=True, fill_price=0.78, fill_size=7.85, fee_usd=0.44,
        order_id="ord-1", token_id="0xDOWN_TOK",
        execution_mode="fak", strategy_id="v9_2_eth_raw_lgb",
        direction="DOWN", stake_usd=6.12,
    )


def _build_uc(*, has_fill_result: bool = False, trade_repo_raises: bool = False):
    """Build use case with a trade_repo mock whose
    has_fill_for_strategy_window_direction returns ``has_fill_result`` —
    or raises if ``trade_repo_raises``."""
    from use_cases.execute_trade import ExecuteTradeUseCase

    trade_repo = MagicMock()
    if trade_repo_raises:
        trade_repo.has_fill_for_strategy_window_direction = AsyncMock(
            side_effect=RuntimeError("simulated pool exhaustion"),
        )
    else:
        trade_repo.has_fill_for_strategy_window_direction = AsyncMock(
            return_value=has_fill_result,
        )

    mock_poly = AsyncMock()
    mock_executor = AsyncMock()
    mock_executor.execute_order = AsyncMock(return_value=_fill_result())
    mock_risk = MagicMock()
    mock_risk.get_status = MagicMock(return_value=_risk())
    mock_ws = AsyncMock()
    mock_ws.was_traded = AsyncMock(return_value=False)
    mock_ws.has_filled = AsyncMock(return_value=False)
    mock_ws.try_claim_trade = AsyncMock(return_value=(True, "claim-1"))
    mock_ws.try_claim_fill_slot = AsyncMock(return_value=True)
    mock_ws.release_fill_slot = AsyncMock()
    mock_ws.clear_trade_claim = AsyncMock()
    mock_ws.mark_traded = AsyncMock()
    mock_alerter = AsyncMock()
    mock_recorder = AsyncMock()

    uc = ExecuteTradeUseCase(
        polymarket=mock_poly,
        order_executor=mock_executor,
        risk_manager=mock_risk,
        window_state=mock_ws,
        alerter=mock_alerter,
        trade_recorder=mock_recorder,
        clock=_Clock(),
        paper_mode=False,
        trade_repo=trade_repo,
    )
    return uc, {"trade_repo": trade_repo, "executor": mock_executor, "ws": mock_ws}


# ─── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hard_lock_blocks_when_trade_already_exists():
    """Reproducer for 2026-05-20 incident. Trade row exists for
    (v9_2_eth_raw_lgb, 1779312000, DOWN). Second attempt MUST be blocked
    with skip_reason='already_fired_this_window_direction'.
    """
    uc, mocks = _build_uc(has_fill_result=True)

    result = await uc.execute(
        decision=_decision(direction="DOWN"),
        window_market=_market(),
        current_btc_price=2500.0,
        open_price=2500.0,
    )

    assert result.success is False
    assert result.failure_reason == "already_fired_this_window_direction"
    # Executor MUST NOT have been called — gate blocks before any order placement
    mocks["executor"].execute_order.assert_not_called()


@pytest.mark.asyncio
async def test_hard_lock_allows_when_no_prior_fill():
    """No trade row exists → execute proceeds normally."""
    uc, mocks = _build_uc(has_fill_result=False)

    result = await uc.execute(
        decision=_decision(direction="DOWN"),
        window_market=_market(),
        current_btc_price=2500.0,
        open_price=2500.0,
    )

    # Either success or fail-for-some-other-reason — but NOT the hard-lock
    assert result.failure_reason != "already_fired_this_window_direction"
    # has_fill_for_strategy_window_direction was queried
    mocks["trade_repo"].has_fill_for_strategy_window_direction.assert_awaited_once()


@pytest.mark.asyncio
async def test_hard_lock_per_direction_independence():
    """DOWN fired → DOWN cell locked. UP on the SAME window must still
    be allowed (different direction = different lock cell).
    """
    from use_cases.execute_trade import ExecuteTradeUseCase

    # Mock returns True for DOWN only
    async def selective(*, strategy_id, window_ts, direction, **kw):
        return direction == "DOWN"

    uc, mocks = _build_uc(has_fill_result=False)
    mocks["trade_repo"].has_fill_for_strategy_window_direction = selective

    # DOWN: blocked
    r_down = await uc.execute(
        decision=_decision(direction="DOWN"), window_market=_market(),
        current_btc_price=2500.0, open_price=2500.0,
    )
    assert r_down.failure_reason == "already_fired_this_window_direction"

    # UP same window: NOT blocked by hard lock
    r_up = await uc.execute(
        decision=_decision(direction="UP"), window_market=_market(),
        current_btc_price=2500.0, open_price=2500.0,
    )
    assert r_up.failure_reason != "already_fired_this_window_direction"


@pytest.mark.asyncio
async def test_hard_lock_cross_strategy_independence():
    """Strategy A fired → strategy A's cell locked. Strategy B on the same
    window MUST still fire (different strategy_id = different cell).
    """
    async def per_strategy(*, strategy_id, **kw):
        return strategy_id == "v9_2_eth_raw_lgb"

    uc, mocks = _build_uc(has_fill_result=False)
    mocks["trade_repo"].has_fill_for_strategy_window_direction = per_strategy

    r_a = await uc.execute(
        decision=_decision(strategy_id="v9_2_eth_raw_lgb", direction="DOWN"),
        window_market=_market(),
        current_btc_price=2500.0, open_price=2500.0,
    )
    assert r_a.failure_reason == "already_fired_this_window_direction"

    r_b = await uc.execute(
        decision=_decision(strategy_id="v9_2_v12_combo", direction="DOWN"),
        window_market=_market(),
        current_btc_price=2500.0, open_price=2500.0,
    )
    assert r_b.failure_reason != "already_fired_this_window_direction"


@pytest.mark.asyncio
async def test_hard_lock_db_error_fails_closed():
    """DB error during has_fill query → BLOCK the trade (fail-closed).
    Better to skip one legitimate fire than risk a $22 repeat incident.
    """
    uc, _ = _build_uc(trade_repo_raises=True)

    result = await uc.execute(
        decision=_decision(direction="DOWN"),
        window_market=_market(),
        current_btc_price=2500.0,
        open_price=2500.0,
    )
    assert result.success is False
    assert result.failure_reason == "hard_lock_error_fail_closed"


@pytest.mark.asyncio
async def test_hard_lock_called_with_correct_params():
    """Verify the gate passes the right (strategy_id, window_ts, direction,
    asset, timeframe, is_live) tuple to the repo."""
    uc, mocks = _build_uc(has_fill_result=False)

    await uc.execute(
        decision=_decision(strategy_id="v9_2_eth_raw_lgb", direction="DOWN"),
        window_market=_market(window_ts=1779312000, asset="ETH"),
        current_btc_price=2500.0, open_price=2500.0,
    )

    call = mocks["trade_repo"].has_fill_for_strategy_window_direction.await_args
    assert call is not None
    kwargs = call.kwargs
    assert kwargs["strategy_id"] == "v9_2_eth_raw_lgb"
    assert int(kwargs["window_ts"]) == 1779312000
    assert kwargs["direction"] == "DOWN"
    assert kwargs["asset"] == "ETH"
    assert kwargs["timeframe"] == "5m"
    assert kwargs["is_live"] is True  # paper_mode=False in build


@pytest.mark.asyncio
async def test_hard_lock_paper_mode_passes_is_live_false():
    """In paper_mode=True, the gate must query with is_live=False so paper
    and live trade domains are independent."""
    from use_cases.execute_trade import ExecuteTradeUseCase

    trade_repo = MagicMock()
    trade_repo.has_fill_for_strategy_window_direction = AsyncMock(return_value=False)
    mock_poly = AsyncMock()
    mock_executor = AsyncMock()
    mock_executor.execute_order = AsyncMock(return_value=_fill_result())
    mock_risk = MagicMock()
    mock_risk.get_status = MagicMock(return_value=_risk())
    mock_ws = AsyncMock()
    mock_ws.was_traded = AsyncMock(return_value=False)
    mock_ws.has_filled = AsyncMock(return_value=False)
    mock_ws.try_claim_trade = AsyncMock(return_value=(True, "claim-2"))
    mock_ws.try_claim_fill_slot = AsyncMock(return_value=True)
    mock_ws.release_fill_slot = AsyncMock()
    mock_ws.clear_trade_claim = AsyncMock()
    mock_ws.mark_traded = AsyncMock()
    mock_alerter = AsyncMock()
    mock_recorder = AsyncMock()

    uc = ExecuteTradeUseCase(
        polymarket=mock_poly,
        order_executor=mock_executor,
        risk_manager=mock_risk,
        window_state=mock_ws,
        alerter=mock_alerter,
        trade_recorder=mock_recorder,
        clock=_Clock(),
        paper_mode=True,
        trade_repo=trade_repo,
    )
    await uc.execute(
        decision=_decision(),
        window_market=_market(),
        current_btc_price=2500.0,
        open_price=2500.0,
    )
    call = trade_repo.has_fill_for_strategy_window_direction.await_args
    assert call.kwargs["is_live"] is False
