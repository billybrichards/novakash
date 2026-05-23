"""Regression tests for the new redemption helpers on PgTradeRepository
(Hub #586).

Two new methods cover the auto-redeem reconciler:

  - ``find_unresolved_live_trades_for_slug`` — read query that returns
    the trade rows the redemption reconciler should stamp.
  - ``stamp_redemption_win`` — single atomic UPDATE with the WIN +
    redeemed flags, guarded by ``WHERE outcome IS NULL AND redeemed =
    FALSE`` so racing reconciler passes cannot double-stamp.

These tests pin the WHERE guard, the column list, and the parameter
binding order. They use a fake asyncpg pool so they run in milliseconds
without RDS.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from adapters.persistence.pg_trade_repo import PgTradeRepository


class _FakeConn:
    """Records the SQL + params an asyncpg connection would have run."""

    def __init__(self, execute_returns: str = "UPDATE 1", fetch_returns=None):
        self.execute_calls: list[dict[str, Any]] = []
        self.fetch_calls: list[dict[str, Any]] = []
        self._execute_returns = execute_returns
        self._fetch_returns = fetch_returns or []

    async def execute(self, sql, *args, **kwargs):
        self.execute_calls.append(
            {"sql": sql, "args": args, "kwargs": kwargs}
        )
        return self._execute_returns

    async def fetch(self, sql, *args, **kwargs):
        self.fetch_calls.append(
            {"sql": sql, "args": args, "kwargs": kwargs}
        )
        return self._fetch_returns


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        outer = self

        class _CM:
            async def __aenter__(self_inner):
                return outer.conn

            async def __aexit__(self_inner, *exc):
                return None

        return _CM()


def _make_repo(conn) -> PgTradeRepository:
    return PgTradeRepository(_FakePool(conn))


# ── find_unresolved_live_trades_for_slug ───────────────────────────────


@pytest.mark.asyncio
async def test_find_unresolved_passes_slug_and_direction_filter():
    """When winning_direction is supplied, the SQL filters on direction."""
    conn = _FakeConn(fetch_returns=[
        {"id": 9135, "direction": "NO"},
        {"id": 9136, "direction": "NO"},
    ])
    repo = _make_repo(conn)
    rows = await repo.find_unresolved_live_trades_for_slug(
        "btc-updown-5m-1779543300", winning_direction="NO"
    )
    assert len(rows) == 2
    call = conn.fetch_calls[0]
    assert call["args"][0] == "btc-updown-5m-1779543300"
    assert call["args"][1] == "NO"
    sql = call["sql"]
    assert "outcome IS NULL" in sql
    assert "is_live = TRUE" in sql
    assert "order_id NOT LIKE 'paper-%'" in sql
    assert "direction = $2" in sql


@pytest.mark.asyncio
async def test_find_unresolved_without_direction_returns_both_sides():
    """No direction filter → caller can see all unresolved trades on slug."""
    conn = _FakeConn(fetch_returns=[
        {"id": 9111, "direction": "YES"},
        {"id": 9112, "direction": "NO"},
    ])
    repo = _make_repo(conn)
    rows = await repo.find_unresolved_live_trades_for_slug(
        "btc-updown-5m-1779543300"
    )
    assert len(rows) == 2
    call = conn.fetch_calls[0]
    assert call["args"][0] == "btc-updown-5m-1779543300"
    assert len(call["args"]) == 1  # no direction arg
    assert "direction = $2" not in call["sql"]


@pytest.mark.asyncio
async def test_find_unresolved_returns_empty_on_db_error():
    """Caller treats empty as 'skip this event' — never raises."""
    conn = _FakeConn()
    conn.fetch_calls = []  # type: ignore[assignment]

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated DB error")

    conn.fetch = boom  # type: ignore[assignment]
    repo = _make_repo(conn)
    rows = await repo.find_unresolved_live_trades_for_slug(
        "btc-updown-5m-1779543300", winning_direction="NO"
    )
    assert rows == []


@pytest.mark.asyncio
async def test_find_unresolved_empty_slug_short_circuit():
    """Empty slug returns [] without touching the DB."""
    conn = _FakeConn(fetch_returns=[{"id": 9135}])
    repo = _make_repo(conn)
    rows = await repo.find_unresolved_live_trades_for_slug("")
    assert rows == []
    assert len(conn.fetch_calls) == 0


# ── stamp_redemption_win ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stamp_redemption_win_writes_all_columns_in_order():
    """One UPDATE; outcome+status+payout+pnl+redeemed all in one shot."""
    conn = _FakeConn(execute_returns="UPDATE 1")
    repo = _make_repo(conn)
    ok = await repo.stamp_redemption_win(
        trade_id=9135,
        payout_usd=3.9,
        pnl_usd=0.35,
        redemption_tx="0xcb1ed7b5",
        redeemed_at_epoch=1779543638,
    )
    assert ok is True
    assert len(conn.execute_calls) == 1
    sql = conn.execute_calls[0]["sql"]
    # Required guards in the WHERE clause
    assert "outcome IS NULL" in sql
    assert "redeemed = FALSE" in sql
    assert "is_live = TRUE" in sql
    # All columns set in one update
    assert "outcome       = 'WIN'" in sql
    assert "status        = 'RESOLVED_WIN'" in sql
    assert "redeemed      = TRUE" in sql
    assert "payout_usd    = $1" in sql
    assert "pnl_usd       = $2" in sql
    args = conn.execute_calls[0]["args"]
    assert args[0] == 3.9        # payout_usd
    assert args[1] == 0.35       # pnl_usd
    assert args[4] == "0xcb1ed7b5"  # redemption_tx
    assert args[5] == 9135       # trade_id


@pytest.mark.asyncio
async def test_stamp_redemption_win_returns_false_when_guard_rejects():
    """UPDATE 0 → the WHERE guard didn't match → return False."""
    conn = _FakeConn(execute_returns="UPDATE 0")
    repo = _make_repo(conn)
    ok = await repo.stamp_redemption_win(
        trade_id=9135,
        payout_usd=3.9,
        pnl_usd=0.35,
        redemption_tx="0xcb1ed7b5",
        redeemed_at_epoch=1779543638,
    )
    assert ok is False


@pytest.mark.asyncio
async def test_stamp_redemption_win_returns_false_on_db_error():
    """A DB-level error must not crash the reconciler loop."""
    conn = _FakeConn()

    async def boom(*args, **kwargs):
        raise RuntimeError("connection reset")

    conn.execute = boom  # type: ignore[assignment]
    repo = _make_repo(conn)
    ok = await repo.stamp_redemption_win(
        trade_id=9135,
        payout_usd=3.9,
        pnl_usd=0.35,
        redemption_tx="0xcb1ed7b5",
        redeemed_at_epoch=1779543638,
    )
    assert ok is False


@pytest.mark.asyncio
async def test_stamp_redemption_win_no_pool_is_noop():
    repo = PgTradeRepository.__new__(PgTradeRepository)
    repo._pool = None  # type: ignore[attr-defined]
    ok = await repo.stamp_redemption_win(
        trade_id=9135,
        payout_usd=3.9,
        pnl_usd=0.35,
        redemption_tx="0xcb1ed7b5",
        redeemed_at_epoch=1779543638,
    )
    assert ok is False
