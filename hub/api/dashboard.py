"""
Dashboard API Routes

GET /api/dashboard         — full dashboard data
GET /api/dashboard/summary — lightweight stat summary
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session
from services.dashboard_service import DashboardService

router = APIRouter()


@router.get("/dashboard")
async def get_dashboard(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return the full dashboard payload including:
      - Current bankroll and PnL metrics
      - Latest VPIN value
      - Active cascade state
      - Open arb opportunities count
      - Recent trades (last 10)
    """
    svc = DashboardService(session)
    return await svc.get_dashboard_data()


@router.get("/dashboard/summary")
async def get_dashboard_summary(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return a lightweight stat summary suitable for the top bar:
      - bankroll, daily_pnl, total_pnl, win_rate, open_trades
    """
    svc = DashboardService(session)
    return await svc.get_summary()
