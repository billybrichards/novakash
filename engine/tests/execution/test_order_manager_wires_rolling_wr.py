"""Verify that OrderManager calls RollingWRMonitor.on_trade_resolved
after every WIN/LOSS resolution (audits #379 + #385, 2026-05-06).

Uses a mock RollingWRMonitor so no DB dependency is needed.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from domain.entities import Order, OrderStatus
from execution.order_manager import OrderManager
from services.rolling_wr_monitor import ResolvedTrade


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_order(
    order_id: str = "test-order-1",
    strategy: str = "v12_lgb_combo",
    direction: str = "YES",
    stake_usd: float = 5.0,
    price: str = "0.70",
    fill_price: float = 0.71,
    metadata: dict | None = None,
) -> Order:
    return Order(
        order_id=order_id,
        venue="polymarket",
        strategy=strategy,
        direction=direction,
        price=price,
        stake_usd=stake_usd,
        fill_price=fill_price,
        status=OrderStatus.OPEN,
        created_at=time.time() - 300,
        metadata=metadata or {"eval_offset": 88, "vpin_regime": "CASCADE"},
        window_seconds=300,
        market_id="btc-up-1777658700",
    )


# ── tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rolling_wr_monitor_called_on_win():
    """on_trade_resolved is called when a trade resolves WIN."""
    mock_monitor = MagicMock()
    mock_monitor.on_trade_resolved = AsyncMock(return_value=None)

    om = OrderManager(
        db=None,
        paper_mode=True,
        rolling_wr_monitor=mock_monitor,
    )

    order = _make_order()
    await om.register_order(order)

    resolved = await om.resolve_order(order.order_id, "WIN", payout_usd=7.14)
    assert resolved.outcome == "WIN"

    mock_monitor.on_trade_resolved.assert_awaited_once()
    call_arg = mock_monitor.on_trade_resolved.call_args[0][0]
    assert isinstance(call_arg, ResolvedTrade)
    assert call_arg.strategy_id == "v12_lgb_combo"
    assert call_arg.direction == "UP"
    assert call_arg.is_win is True


@pytest.mark.asyncio
async def test_rolling_wr_monitor_called_on_loss():
    """on_trade_resolved is called when a trade resolves LOSS."""
    mock_monitor = MagicMock()
    mock_monitor.on_trade_resolved = AsyncMock(return_value=None)

    om = OrderManager(
        db=None,
        paper_mode=True,
        rolling_wr_monitor=mock_monitor,
    )

    order = _make_order(direction="NO", price="0.32")
    await om.register_order(order)

    await om.resolve_order(order.order_id, "LOSS", payout_usd=0.0)

    mock_monitor.on_trade_resolved.assert_awaited_once()
    call_arg = mock_monitor.on_trade_resolved.call_args[0][0]
    assert call_arg.is_win is False
    assert call_arg.direction == "DOWN"


@pytest.mark.asyncio
async def test_rolling_wr_monitor_direction_mapping():
    """YES → UP, NO → DOWN when building ResolvedTrade."""
    mock_monitor = MagicMock()
    mock_monitor.on_trade_resolved = AsyncMock(return_value=None)

    om = OrderManager(db=None, paper_mode=True, rolling_wr_monitor=mock_monitor)

    # YES direction
    o_yes = _make_order(order_id="yes-1", direction="YES")
    await om.register_order(o_yes)
    await om.resolve_order("yes-1", "WIN", payout_usd=7.0)
    call_yes = mock_monitor.on_trade_resolved.call_args_list[-1][0][0]
    assert call_yes.direction == "UP"

    # NO direction
    o_no = _make_order(order_id="no-1", direction="NO")
    await om.register_order(o_no)
    await om.resolve_order("no-1", "WIN", payout_usd=7.0)
    call_no = mock_monitor.on_trade_resolved.call_args_list[-1][0][0]
    assert call_no.direction == "DOWN"


@pytest.mark.asyncio
async def test_rolling_wr_monitor_none_does_not_raise():
    """OrderManager works normally when rolling_wr_monitor is None (default)."""
    om = OrderManager(db=None, paper_mode=True)
    order = _make_order()
    await om.register_order(order)
    resolved = await om.resolve_order(order.order_id, "WIN", payout_usd=7.0)
    assert resolved.outcome == "WIN"


@pytest.mark.asyncio
async def test_rolling_wr_monitor_exception_does_not_crash_resolution():
    """If on_trade_resolved raises, resolve_order still completes."""
    mock_monitor = MagicMock()
    mock_monitor.on_trade_resolved = AsyncMock(
        side_effect=RuntimeError("monitor exploded")
    )

    om = OrderManager(db=None, paper_mode=True, rolling_wr_monitor=mock_monitor)
    order = _make_order()
    await om.register_order(order)

    # Must not raise even though monitor raises
    resolved = await om.resolve_order(order.order_id, "LOSS", payout_usd=0.0)
    assert resolved.outcome == "LOSS"
    mock_monitor.on_trade_resolved.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolved_trade_carries_metadata():
    """eval_offset and regime are correctly extracted from order metadata."""
    mock_monitor = MagicMock()
    mock_monitor.on_trade_resolved = AsyncMock(return_value=None)

    om = OrderManager(db=None, paper_mode=True, rolling_wr_monitor=mock_monitor)
    order = _make_order(
        metadata={
            "eval_offset": 88,
            "vpin_regime": "volatile_trend",
            "window_ts": 1777658700,
        }
    )
    await om.register_order(order)
    await om.resolve_order(order.order_id, "WIN", payout_usd=7.0)

    rt: ResolvedTrade = mock_monitor.on_trade_resolved.call_args[0][0]
    assert rt.eval_offset == 88
    assert rt.regime == "volatile_trend"
    assert rt.fill_price == pytest.approx(0.71)


@pytest.mark.asyncio
async def test_resolve_order_not_called_for_expired():
    """EXPIRED orders (skipped, not WIN/LOSS) don't call on_trade_resolved
    via resolve_order — they go through a different code path."""
    mock_monitor = MagicMock()
    mock_monitor.on_trade_resolved = AsyncMock(return_value=None)

    om = OrderManager(db=None, paper_mode=True, rolling_wr_monitor=mock_monitor)

    # resolve_order only accepts WIN/LOSS — EXPIRED is handled differently
    # in poll_resolutions (status mutation, not resolve_order call).
    # So on_trade_resolved must NOT be called for non-WIN/LOSS outcomes.
    order = _make_order()
    await om.register_order(order)

    with pytest.raises(ValueError):
        await om.resolve_order(order.order_id, "EXPIRED", payout_usd=0.0)

    mock_monitor.on_trade_resolved.assert_not_awaited()
