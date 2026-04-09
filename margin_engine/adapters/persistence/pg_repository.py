"""
PostgreSQL position repository — persists margin positions to Railway DB.

Uses asyncpg directly (same pattern as the engine's db_client.py).
Table: margin_positions (created by migration).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from margin_engine.domain.entities.position import Position
from margin_engine.domain.ports import PositionRepository
from margin_engine.domain.value_objects import (
    ExitReason,
    Money,
    PositionState,
    Price,
    StopLevel,
    TradeSide,
)

logger = logging.getLogger(__name__)

UPSERT_SQL = """
INSERT INTO margin_positions
    (id, asset, side, state, leverage,
     entry_price, notional, collateral,
     stop_loss_price, take_profit_price,
     exit_price, exit_reason, realised_pnl,
     opened_at, closed_at,
     entry_signal_score, entry_timescale,
     entry_order_id, exit_order_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19)
ON CONFLICT (id) DO UPDATE SET
    state = $4,
    exit_price = $11,
    exit_reason = $12,
    realised_pnl = $13,
    closed_at = $15,
    exit_order_id = $19
"""

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS margin_positions (
    id TEXT PRIMARY KEY,
    asset TEXT NOT NULL DEFAULT 'BTC',
    side TEXT NOT NULL,
    state TEXT NOT NULL,
    leverage INT NOT NULL DEFAULT 5,
    entry_price REAL,
    notional REAL,
    collateral REAL,
    stop_loss_price REAL,
    take_profit_price REAL,
    exit_price REAL,
    exit_reason TEXT,
    realised_pnl REAL DEFAULT 0,
    opened_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    entry_signal_score REAL,
    entry_timescale TEXT,
    entry_order_id TEXT,
    exit_order_id TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_margin_pos_state ON margin_positions(state);
CREATE INDEX IF NOT EXISTS idx_margin_pos_opened ON margin_positions(opened_at);
"""


class PgPositionRepository(PositionRepository):
    """asyncpg-backed position repository."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def ensure_table(self) -> None:
        """Create table if it doesn't exist."""
        async with self._pool.acquire() as conn:
            await conn.execute(CREATE_TABLE_SQL)
        logger.info("margin_positions table ensured")

    async def save(self, position: Position) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                UPSERT_SQL,
                position.id,
                position.asset,
                position.side.value,
                position.state.value,
                position.leverage,
                position.entry_price.value if position.entry_price else None,
                position.notional.amount if position.notional else None,
                position.collateral.amount if position.collateral else None,
                position.stop_loss.price if position.stop_loss else None,
                position.take_profit.price if position.take_profit else None,
                position.exit_price.value if position.exit_price else None,
                position.exit_reason.value if position.exit_reason else None,
                position.realised_pnl,
                _ts(position.opened_at),
                _ts(position.closed_at),
                position.entry_signal_score,
                position.entry_timescale,
                position.entry_order_id,
                position.exit_order_id,
            )

    async def get_open_positions(self) -> list[Position]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM margin_positions WHERE state = 'OPEN' ORDER BY opened_at"
            )
        return [self._row_to_position(r) for r in rows]

    async def get_by_id(self, position_id: str) -> Optional[Position]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM margin_positions WHERE id = $1", position_id
            )
        return self._row_to_position(row) if row else None

    async def get_closed_today(self) -> list[Position]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM margin_positions WHERE state = 'CLOSED' AND closed_at >= CURRENT_DATE ORDER BY closed_at"
            )
        return [self._row_to_position(r) for r in rows]

    @staticmethod
    def _row_to_position(row) -> Position:
        p = Position(
            id=row["id"],
            asset=row["asset"],
            side=TradeSide(row["side"]),
            state=PositionState(row["state"]),
            leverage=row["leverage"],
            entry_signal_score=row["entry_signal_score"] or 0.0,
            entry_timescale=row["entry_timescale"] or "5m",
            entry_order_id=row["entry_order_id"],
            exit_order_id=row["exit_order_id"],
            realised_pnl=row["realised_pnl"] or 0.0,
        )
        if row["entry_price"]:
            p.entry_price = Price(value=row["entry_price"])
        if row["notional"]:
            p.notional = Money.usd(row["notional"])
        if row["collateral"]:
            p.collateral = Money.usd(row["collateral"])
        if row["stop_loss_price"]:
            p.stop_loss = StopLevel(price=row["stop_loss_price"])
        if row["take_profit_price"]:
            p.take_profit = StopLevel(price=row["take_profit_price"])
        if row["exit_price"]:
            p.exit_price = Price(value=row["exit_price"])
        if row["exit_reason"]:
            p.exit_reason = ExitReason(row["exit_reason"])
        if row["opened_at"]:
            p.opened_at = row["opened_at"].timestamp()
        if row["closed_at"]:
            p.closed_at = row["closed_at"].timestamp()
        return p


def _ts(epoch: float) -> Optional[datetime]:
    if epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc)
