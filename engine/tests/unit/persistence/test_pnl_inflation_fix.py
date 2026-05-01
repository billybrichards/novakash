"""Regression tests for the FAK-ladder pnl-inflation fix (audit-task #331).

Three layers covered:

  1. ``Order`` dataclass — new ``fill_price`` field accepts an explicit
     volume-weighted-avg fill, defaults to ``None``, round-trips intact.

  2. Resolver (``OrderManager._resolve_from_polymarket`` /
     ``_determine_paper_outcome``) — payout uses ``order.fill_price`` when
     present, falls back to ``order.price`` only when fill_price is ``None``.
     Replays trade-id #6289 (v10_lgb_only): stake $15, first-rung price
     $0.34, avg fill $0.68, fill_size 22.06 → expected pnl ≈ $7.06, NOT
     ~$29 as the pre-fix code produced.

  3. ``PgTradeRepository.write_trade`` UPSERT — ``pnl_usd``, ``payout_usd``,
     ``resolved_at``, and ``outcome`` are all guarded with
     ``COALESCE(trades.X, EXCLUDED.X)`` so a late re-delivery (e.g.
     recovery, reconciler replay) cannot clobber an already-resolved row.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from domain.entities import Order, OrderStatus


# ─── Layer 1: Order dataclass ─────────────────────────────────────────────


def test_order_fill_price_defaults_to_none() -> None:
    """Existing call-sites that omit fill_price must keep working."""
    o = Order(
        order_id="0xdeadbeef",
        venue="polymarket",
        strategy="v9_lgb_only",
        direction="YES",
        price="0.34",
        stake_usd=15.0,
    )
    assert o.fill_price is None
    assert o.price == "0.34"


def test_order_fill_price_round_trips() -> None:
    """fill_price is a sister field — does not interfere with price."""
    o = Order(
        order_id="0xdeadbeef",
        venue="polymarket",
        strategy="v10_lgb_only",
        direction="YES",
        price="0.34",
        fill_price=0.68,
        stake_usd=15.0,
    )
    assert o.fill_price == pytest.approx(0.68)
    assert o.price == "0.34"


# ─── Layer 1: Resolver uses fill_price when present ───────────────────────


@pytest.mark.asyncio
async def test_paper_resolution_uses_fill_price_not_price() -> None:
    """Replays trade #6289 — pre-fix payout was ~$29, post-fix is ~$22."""
    from execution.order_manager import OrderManager

    om = OrderManager(db=None)

    order = Order(
        order_id="paper-fak-6289",
        venue="polymarket",
        strategy="v10_lgb_only",
        direction="YES",
        price="0.34",       # first ladder rung — what the pre-fix bug used
        fill_price=0.68,    # actual volume-weighted avg fill
        stake_usd=15.0,
        status=OrderStatus.OPEN,
        btc_entry_price=100_000.0,
        metadata={"window_open_price": 100_000.0},
        created_at=time.time() - 320,  # past window+buffer for 5min strat
    )

    # five_min_vpin codepath — bet was UP (YES), close > open → WIN.
    order.strategy = "five_min_vpin"
    result = await om._determine_paper_outcome(order, current_btc_price=100_500.0)
    assert result is not None
    outcome, payout = result
    assert outcome == "WIN"
    # 15 / 0.68 = 22.058 shares → payout ~= 22.06, NOT 15/0.34 = 44.12
    assert payout == pytest.approx(22.06, abs=0.05)


@pytest.mark.asyncio
async def test_paper_resolution_falls_back_to_price_when_fill_price_none() -> None:
    """Single-fill / paper rows that never recorded fill_price must still
    resolve — the resolver falls back to the submission price."""
    from execution.order_manager import OrderManager

    om = OrderManager(db=None)

    order = Order(
        order_id="paper-fak-fallback",
        venue="polymarket",
        strategy="five_min_vpin",
        direction="YES",
        price="0.50",
        fill_price=None,  # paper / pre-fix legacy row
        stake_usd=10.0,
        status=OrderStatus.OPEN,
        btc_entry_price=100_000.0,
        metadata={"window_open_price": 100_000.0},
        created_at=time.time() - 320,
    )

    result = await om._determine_paper_outcome(order, current_btc_price=100_500.0)
    assert result is not None
    outcome, payout = result
    assert outcome == "WIN"
    # 10 / 0.50 = 20.0 shares
    assert payout == pytest.approx(20.0, abs=0.01)


# ─── Layer 2: write_trade UPSERT pnl-clobber guard ────────────────────────


class _RecordingConn:
    """asyncpg connection stub that records every execute() call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        return "INSERT 0 1"


class _RecordingPool:
    def __init__(self) -> None:
        self.conn = _RecordingConn()

    def acquire(self):
        outer = self

        class _CM:
            async def __aenter__(self_inner):
                return outer.conn

            async def __aexit__(self_inner, *exc):
                return None

        return _CM()


@pytest.mark.asyncio
async def test_write_trade_upsert_locks_resolved_state_fields() -> None:
    """The ON CONFLICT branch must use COALESCE(trades.X, EXCLUDED.X) for
    pnl_usd, payout_usd, resolved_at and outcome — otherwise a recovery /
    replay write_trade can re-stamp pnl on an already-resolved row.
    """
    from adapters.persistence.pg_trade_repo import PgTradeRepository

    pool = _RecordingPool()
    repo = PgTradeRepository(pool)  # type: ignore[arg-type]

    order = Order(
        order_id="0xfeedbeef",
        venue="polymarket",
        strategy="v9_lgb_only",
        direction="YES",
        price="0.50",
        fill_price=0.68,
        stake_usd=15.0,
        status=OrderStatus.RESOLVED_WIN,
        outcome="WIN",
        payout_usd=22.06,
        pnl_usd=7.06,
        resolved_at=time.time(),
        market_id="btc-up-5m-1234567890",
        metadata={"execution_mode": "fak"},
    )

    await repo.write_trade(order)

    assert pool.conn.calls, "expected at least one execute() call"
    sql = pool.conn.calls[0][0]

    # Lock fields — these must all be guarded by COALESCE(trades.X, ...).
    assert "pnl_usd          = COALESCE(trades.pnl_usd, EXCLUDED.pnl_usd)" in sql
    assert "payout_usd       = COALESCE(trades.payout_usd, EXCLUDED.payout_usd)" in sql
    assert "resolved_at      = COALESCE(trades.resolved_at, EXCLUDED.resolved_at)" in sql
    assert "outcome          = COALESCE(trades.outcome, EXCLUDED.outcome)" in sql

    # status must NOT be guarded — forward transitions
    # (OPEN → FILLED → RESOLVED_*) are valid and required by recovery.
    assert "status           = EXCLUDED.status" in sql


@pytest.mark.asyncio
async def test_write_trade_open_row_writes_all_fields_normally() -> None:
    """First write of an OPEN trade — none of the COALESCE guards should
    bite because trades.X is NULL on insert."""
    from adapters.persistence.pg_trade_repo import PgTradeRepository

    pool = _RecordingPool()
    repo = PgTradeRepository(pool)  # type: ignore[arg-type]

    order = Order(
        order_id="0xnewopen",
        venue="polymarket",
        strategy="v9_lgb_only",
        direction="YES",
        price="0.50",
        stake_usd=15.0,
        status=OrderStatus.OPEN,
        market_id="btc-up-5m-1234567890",
        metadata={"execution_mode": "fak"},
    )

    await repo.write_trade(order)
    assert pool.conn.calls
    # ON CONFLICT branch is present (idempotent insert) but the INSERT
    # itself is what carries the row through the first time.
    sql = pool.conn.calls[0][0]
    assert "INSERT INTO trades" in sql
    assert "ON CONFLICT (order_id) DO UPDATE" in sql
