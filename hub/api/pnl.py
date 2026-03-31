"""
PnL API Routes

GET /api/pnl/daily        — daily P&L bar chart data
GET /api/pnl/cumulative   — cumulative equity curve
GET /api/pnl/by-strategy  — P&L broken down by strategy
GET /api/pnl/monthly      — monthly P&L table
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session
from services.pnl_service import PnLService

router = APIRouter()


@router.get("/pnl/daily")
async def get_daily_pnl(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return daily P&L for the last 90 days.

    Returns list of {date, pnl_usd, num_trades, win_rate}.
    """
    svc = PnLService(session)
    return {"data": await svc.get_daily_pnl(days=90)}


@router.get("/pnl/cumulative")
async def get_cumulative_pnl(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return the cumulative equity curve (running sum of P&L over time).

    Returns list of {timestamp, cumulative_pnl, bankroll_estimate}.
    """
    svc = PnLService(session)
    return {"data": await svc.get_cumulative_pnl()}


@router.get("/pnl/by-strategy")
async def get_pnl_by_strategy(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return P&L breakdown per strategy.

    Returns list of {strategy, total_pnl, num_trades, win_rate, avg_pnl}.
    """
    svc = PnLService(session)
    return {"data": await svc.get_pnl_by_strategy()}


@router.get("/pnl/monthly")
async def get_monthly_pnl(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return monthly P&L summary table.

    Returns list of {year, month, total_pnl, num_trades, win_rate}.
    """
    svc = PnLService(session)
    return {"data": await svc.get_monthly_pnl()}
