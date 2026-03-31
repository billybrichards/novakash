"""
PnL Service

Business logic for P&L calculations and aggregations.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DailyPnL, Trade


class PnLService:
    """Computes P&L metrics from the trades table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_daily_pnl(self, days: int = 90) -> list[dict]:
        """
        Return daily P&L for the last N days.

        Reads from daily_pnl if available, otherwise computes from trades.
        Returns list of {date, pnl_usd, num_trades, win_rate}.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        result = await self._session.execute(
            select(DailyPnL)
            .where(DailyPnL.date >= cutoff)
            .order_by(DailyPnL.date)
        )
        rows = result.scalars().all()

        return [
            {
                "date": r.date.strftime("%Y-%m-%d") if hasattr(r.date, "strftime") else str(r.date),
                "pnl_usd": float(r.total_pnl or 0),
                "num_trades": r.num_trades,
                "win_rate": float(r.win_rate or 0),
            }
            for r in rows
        ]

    async def get_cumulative_pnl(self) -> list[dict]:
        """
        Return the cumulative equity curve as a time series.

        Returns list of {timestamp, pnl_usd, cumulative_pnl}.
        """
        result = await self._session.execute(
            select(Trade.resolved_at, Trade.pnl_usd)
            .where(Trade.pnl_usd.isnot(None))
            .where(Trade.resolved_at.isnot(None))
            .order_by(Trade.resolved_at)
        )
        rows = result.all()

        cumulative = 0.0
        curve = []
        for ts, pnl in rows:
            cumulative += float(pnl or 0)
            curve.append({
                "timestamp": ts.isoformat() if ts else None,
                "pnl_usd": float(pnl or 0),
                "cumulative_pnl": cumulative,
            })

        return curve

    async def get_pnl_by_strategy(self) -> list[dict]:
        """
        Return P&L broken down by strategy.

        Returns list of {strategy, total_pnl, num_trades, win_rate, avg_pnl}.
        """
        result = await self._session.execute(
            select(
                Trade.strategy,
                func.count(Trade.id).label("num_trades"),
                func.sum(Trade.pnl_usd).label("total_pnl"),
                func.avg(Trade.pnl_usd).label("avg_pnl"),
            )
            .where(Trade.pnl_usd.isnot(None))
            .group_by(Trade.strategy)
        )
        rows = result.all()

        data = []
        for row in rows:
            wins_result = await self._session.execute(
                select(func.count())
                .where(Trade.strategy == row.strategy)
                .where(Trade.outcome == "WIN")
            )
            wins = wins_result.scalar_one() or 0

            data.append({
                "strategy": row.strategy,
                "total_pnl": float(row.total_pnl or 0),
                "num_trades": row.num_trades or 0,
                "avg_pnl": float(row.avg_pnl or 0),
                "win_rate": wins / row.num_trades if row.num_trades else 0.0,
            })

        return data

    async def get_monthly_pnl(self) -> list[dict]:
        """
        Return monthly P&L summary.

        Returns list of {year, month, total_pnl, num_trades, win_rate}.
        """
        result = await self._session.execute(
            text("""
                SELECT
                    EXTRACT(YEAR  FROM resolved_at)::int AS year,
                    EXTRACT(MONTH FROM resolved_at)::int AS month,
                    SUM(pnl_usd)                          AS total_pnl,
                    COUNT(*)                               AS num_trades,
                    COUNT(*) FILTER (WHERE outcome = 'WIN') * 1.0 / NULLIF(COUNT(*), 0) AS win_rate
                FROM trades
                WHERE pnl_usd IS NOT NULL
                  AND resolved_at IS NOT NULL
                GROUP BY year, month
                ORDER BY year, month
            """)
        )
        rows = result.mappings().all()

        return [
            {
                "year": row["year"],
                "month": row["month"],
                "total_pnl": float(row["total_pnl"] or 0),
                "num_trades": row["num_trades"],
                "win_rate": float(row["win_rate"] or 0),
            }
            for row in rows
        ]
