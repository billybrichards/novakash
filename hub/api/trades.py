"""
Trades API Routes

GET /api/trades           — paginated list with filters
GET /api/trades/{id}      — single trade detail
GET /api/trades/stats     — aggregate stats (win rate, avg PnL, etc.)
"""

from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session
from db.models import Trade

router = APIRouter()


@router.get("/trades")
async def list_trades(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    strategy: Optional[str] = Query(None),
    outcome: Optional[str] = Query(None),
    market_slug: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return paginated list of trades.

    Filters:
      - strategy: "sub_dollar_arb" | "vpin_cascade"
      - outcome:  "WIN" | "LOSS" | "PUSH"
      - market_slug: filter by specific market
    """
    from sqlalchemy import select, func, desc

    query = select(Trade).order_by(desc(Trade.created_at))

    if strategy:
        query = query.where(Trade.strategy == strategy)
    if outcome:
        query = query.where(Trade.outcome == outcome)
    if market_slug:
        query = query.where(Trade.market_slug == market_slug)

    # Total count
    count_result = await session.execute(
        select(func.count()).select_from(query.subquery())
    )
    total = count_result.scalar_one()

    # Paginated results
    offset = (page - 1) * page_size
    result = await session.execute(query.offset(offset).limit(page_size))
    trades = result.scalars().all()

    return {
        "trades": [_trade_to_dict(t) for t in trades],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


@router.get("/trades/stats")
async def get_trade_stats(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return aggregate statistics:
      total_trades, wins, losses, win_rate, total_pnl, avg_pnl, best_trade, worst_trade
    """
    from sqlalchemy import select, func

    result = await session.execute(
        select(
            func.count(Trade.id).label("total"),
            func.sum(Trade.pnl_usd).label("total_pnl"),
            func.avg(Trade.pnl_usd).label("avg_pnl"),
            func.max(Trade.pnl_usd).label("best"),
            func.min(Trade.pnl_usd).label("worst"),
        ).where(Trade.pnl_usd.isnot(None))
    )
    row = result.one()

    wins_result = await session.execute(
        select(func.count()).where(Trade.outcome == "WIN")
    )
    wins = wins_result.scalar_one()

    total = row.total or 0
    return {
        "total_trades": total,
        "wins": wins,
        "losses": total - wins,
        "win_rate": wins / total if total > 0 else 0.0,
        "total_pnl": float(row.total_pnl or 0),
        "avg_pnl": float(row.avg_pnl or 0),
        "best_trade": float(row.best or 0),
        "worst_trade": float(row.worst or 0),
    }


@router.get("/trades/{trade_id}")
async def get_trade(
    trade_id: int,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Return a single trade by database ID."""
    from sqlalchemy import select
    from fastapi import HTTPException

    result = await session.execute(select(Trade).where(Trade.id == trade_id))
    trade = result.scalar_one_or_none()

    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")

    return _trade_to_dict(trade)


def _trade_to_dict(trade: Trade) -> dict:
    return {
        "id": trade.id,
        "order_id": trade.order_id,
        "strategy": trade.strategy,
        "strategy_id": trade.strategy_id,
        "strategy_version": trade.strategy_version,
        "venue": trade.venue,
        "market_slug": trade.market_slug,
        "direction": trade.direction,
        "entry_price": float(trade.entry_price) if trade.entry_price else None,
        "stake_usd": float(trade.stake_usd) if trade.stake_usd else None,
        "fee_usd": float(trade.fee_usd) if trade.fee_usd else None,
        "status": trade.status,
        "outcome": trade.outcome,
        "pnl_usd": float(trade.pnl_usd) if trade.pnl_usd else None,
        "created_at": trade.created_at.isoformat() if trade.created_at else None,
        "resolved_at": trade.resolved_at.isoformat() if trade.resolved_at else None,
    }
