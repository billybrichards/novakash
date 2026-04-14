"""Tests for PreTradeGate use case."""
import pytest
import time
from unittest.mock import AsyncMock, MagicMock
from use_cases.pre_trade_gate import PreTradeGate


def _make_gate(*, already_executed=False, balance=44.0, db_error=False):
    guard = MagicMock()
    if db_error:
        guard.has_executed = AsyncMock(side_effect=Exception("db down"))
    else:
        guard.has_executed = AsyncMock(return_value=already_executed)
    guard.mark_executed = AsyncMock()

    wallet = MagicMock()
    wallet.get_live_balance = AsyncMock(return_value=balance)

    return PreTradeGate(guard=guard, wallet=wallet)


@pytest.mark.asyncio
async def test_passes_all_checks():
    gate = _make_gate()
    result = await gate.check(
        strategy_id="v4_fusion", window_ts=1713000000,
        clob_price=0.43, clob_price_ts=time.time() - 5,
        proposed_stake=3.08,
    )
    assert result.approved is True
    assert result.live_bankroll == 44.0


@pytest.mark.asyncio
async def test_blocks_duplicate_window():
    gate = _make_gate(already_executed=True)
    result = await gate.check(
        strategy_id="v4_fusion", window_ts=1713000000,
        clob_price=0.43, clob_price_ts=time.time() - 5,
        proposed_stake=3.08,
    )
    assert result.approved is False
    assert "dedup" in result.reason


@pytest.mark.asyncio
async def test_blocks_stale_clob_price():
    gate = _make_gate()
    result = await gate.check(
        strategy_id="v4_fusion", window_ts=1713000000,
        clob_price=0.43, clob_price_ts=time.time() - 60,  # 60s old
        proposed_stake=3.08,
    )
    assert result.approved is False
    assert "clob_stale" in result.reason


@pytest.mark.asyncio
async def test_blocks_none_clob_price():
    gate = _make_gate()
    result = await gate.check(
        strategy_id="v4_fusion", window_ts=1713000000,
        clob_price=None, clob_price_ts=time.time(),
        proposed_stake=3.08,
    )
    assert result.approved is False
    assert "clob_stale" in result.reason


@pytest.mark.asyncio
async def test_blocks_empty_wallet():
    gate = _make_gate(balance=2.0)
    result = await gate.check(
        strategy_id="v4_fusion", window_ts=1713000000,
        clob_price=0.43, clob_price_ts=time.time() - 5,
        proposed_stake=3.08,
    )
    assert result.approved is False
    assert "bankroll" in result.reason


@pytest.mark.asyncio
async def test_blocks_oversized_stake():
    gate = _make_gate(balance=10.0)
    result = await gate.check(
        strategy_id="v4_fusion", window_ts=1713000000,
        clob_price=0.43, clob_price_ts=time.time() - 5,
        proposed_stake=9.0,  # > 25% of $10 wallet
    )
    assert result.approved is False
    assert "bankroll" in result.reason


@pytest.mark.asyncio
async def test_fail_closed_on_db_error():
    gate = _make_gate(db_error=True)
    result = await gate.check(
        strategy_id="v4_fusion", window_ts=1713000000,
        clob_price=0.43, clob_price_ts=time.time() - 5,
        proposed_stake=3.08,
    )
    assert result.approved is False
    assert "dedup" in result.reason
