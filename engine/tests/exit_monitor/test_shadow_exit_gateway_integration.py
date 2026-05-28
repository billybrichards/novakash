"""Integration test for ShadowExitGateway against a mocked pg connection.

Asserts the INSERT round-trip with the correct column values. Uses a mock
asyncpg pool so no real DB is needed.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from exit_monitor.adapters.shadow_exit_gateway import ShadowExitGateway
from exit_monitor.domain.shadow_trigger import ShadowTrigger


def _make_pool(execute_return=None, fetchrow_return=None):
    """Build a minimal mock asyncpg pool."""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=execute_return or "INSERT 0 1")
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)

    # asyncpg pool.acquire() is an async context manager
    pool = MagicMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


def _trigger() -> ShadowTrigger:
    return ShadowTrigger(
        decision_id=42,
        asset="BTC",
        window_ts=1_777_200_000,
        strategy_id="tickformer_v18_t180",
        side="UP",
        trigger_eval_offset=120,
        threshold=0.55,
        p_against=0.62,
        p_for=0.38,
        tickformer_model="v18",
        entry_p=0.88,
        entry_eval_offset=210,
    )


@pytest.mark.asyncio
async def test_insert_trigger_calls_execute_with_correct_values():
    """insert_trigger executes INSERT with all expected column values."""
    pool, conn = _make_pool()
    gw = ShadowExitGateway(db_pool=pool)
    trig = _trigger()

    await gw.insert_trigger(
        trigger=trig,
        clob_best_bid_held=0.01,
        clob_best_ask_held=0.09,
        clob_best_bid_against=0.92,
        clob_best_ask_against=0.98,
        clob_book_depth_usd=None,
    )

    conn.execute.assert_awaited_once()
    call_args = conn.execute.call_args[0]  # positional args: (sql, *values)
    sql = call_args[0]
    values = call_args[1:]

    assert "exit_monitor_shadow" in sql
    assert "INSERT INTO" in sql

    # Verify each value is in the tuple (order must match migration columns)
    assert 42 in values                      # decision_id
    assert "BTC" in values                   # asset
    assert 1_777_200_000 in values           # window_ts
    assert "tickformer_v18_t180" in values   # strategy_id
    assert "UP" in values                    # side
    assert 0.88 in values                    # entry_p
    assert 210 in values                     # entry_eval_offset
    assert 120 in values                     # trigger_eval_offset
    assert 0.55 in values                    # trigger_threshold
    assert 0.62 in values                    # p_against_at_trigger
    assert 0.38 in values                    # p_for_at_trigger
    assert "v18" in values                   # tickformer_model
    assert 0.01 in values                    # clob_best_bid_held
    assert 0.09 in values                    # clob_best_ask_held
    assert 0.92 in values                    # clob_best_bid_against
    assert 0.98 in values                    # clob_best_ask_against
    assert None in values                    # clob_book_depth_usd


@pytest.mark.asyncio
async def test_insert_trigger_swallows_db_exception():
    """insert_trigger swallows DB exceptions — never crashes caller."""
    pool, conn = _make_pool()
    conn.execute = AsyncMock(side_effect=Exception("pg connection lost"))
    gw = ShadowExitGateway(db_pool=pool)
    # Should not raise
    await gw.insert_trigger(
        trigger=_trigger(),
        clob_best_bid_held=None,
        clob_best_ask_held=None,
        clob_best_bid_against=None,
        clob_best_ask_against=None,
        clob_book_depth_usd=None,
    )


@pytest.mark.asyncio
async def test_insert_trigger_no_op_when_pool_is_none():
    """No-op (no exception) when pool is not yet available."""
    gw = ShadowExitGateway(db_pool=None, db_client=None)
    await gw.insert_trigger(
        trigger=_trigger(),
        clob_best_bid_held=None,
        clob_best_ask_held=None,
        clob_best_bid_against=None,
        clob_best_ask_against=None,
        clob_book_depth_usd=None,
    )  # no error


@pytest.mark.asyncio
async def test_backfill_outcome_calls_update():
    """backfill_outcome executes UPDATE with correct decision_id + outcome."""
    pool, conn = _make_pool(execute_return="UPDATE 2")
    gw = ShadowExitGateway(db_pool=pool)

    n = await gw.backfill_outcome(
        decision_id=42,
        realized_outcome="WIN",
        realized_pnl_held_to_close=0.15,
        realized_pnl_shadow_exit=-0.84,
    )

    conn.execute.assert_awaited_once()
    call_args = conn.execute.call_args[0]
    sql = call_args[0]
    assert "UPDATE exit_monitor_shadow" in sql
    assert "realized_outcome" in sql
    assert n == 2


@pytest.mark.asyncio
async def test_lazy_pool_from_db_client():
    """db_pool=None but db_client with ._pool resolves lazily."""
    pool, conn = _make_pool()
    db_client = MagicMock()
    db_client._pool = pool

    gw = ShadowExitGateway(db_pool=None, db_client=db_client)
    await gw.insert_trigger(
        trigger=_trigger(),
        clob_best_bid_held=0.01,
        clob_best_ask_held=None,
        clob_best_bid_against=None,
        clob_best_ask_against=None,
        clob_book_depth_usd=None,
    )
    conn.execute.assert_awaited_once()
