"""
Backtest API Routes

GET /api/backtest/runs        — list all backtest runs
GET /api/backtest/runs/{id}   — single backtest run detail
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session
from db.models import BacktestRun

router = APIRouter()


@router.get("/backtest/runs")
async def list_backtest_runs(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return all backtest runs ordered by most recent first.

    Each run includes:
      - strategy name, date range, total_pnl, win_rate, sharpe, max_drawdown
    """
    result = await session.execute(
        select(BacktestRun).order_by(desc(BacktestRun.created_at))
    )
    runs = result.scalars().all()

    return {
        "runs": [_run_to_dict(r) for r in runs]
    }


@router.get("/backtest/runs/{run_id}")
async def get_backtest_run(
    run_id: int,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Return a single backtest run with full detail including trade-level breakdown."""
    result = await session.execute(
        select(BacktestRun).where(BacktestRun.id == run_id)
    )
    run = result.scalar_one_or_none()

    if run is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")

    return _run_to_dict(run, include_trades=True)


def _run_to_dict(run: BacktestRun, include_trades: bool = False) -> dict:
    data = {
        "id": run.id,
        "strategy": run.strategy,
        "start_date": run.start_date.isoformat() if run.start_date else None,
        "end_date": run.end_date.isoformat() if run.end_date else None,
        "total_pnl": float(run.total_pnl) if run.total_pnl else None,
        "num_trades": run.num_trades,
        "win_rate": float(run.win_rate) if run.win_rate else None,
        "sharpe_ratio": float(run.sharpe_ratio) if run.sharpe_ratio else None,
        "max_drawdown": float(run.max_drawdown) if run.max_drawdown else None,
        "params": run.params,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }
    if include_trades and run.trades_json:
        data["trades"] = run.trades_json
    return data
