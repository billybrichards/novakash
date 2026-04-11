"""
Dashboard Service

Business logic for assembling the dashboard payload.
Reads from trades, signals, and system_state tables.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DailyPnL, Signal, SystemState, Trade


class DashboardService:
    """Assembles dashboard data from multiple DB sources."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_dashboard_data(self) -> dict:
        """
        Full dashboard payload.

        Returns:
            {
                summary: {...},
                vpin: {...},
                cascade: {...},
                recent_trades: [...],
                arb_opportunities: [...],
                system: {...},
            }
        """
        summary = await self.get_summary()
        system = await self._get_system_state()
        vpin = await self._get_latest_signal("vpin")
        cascade = await self._get_latest_signal("cascade")
        recent_trades = await self._get_recent_trades(limit=10)

        return {
            "summary": summary,
            "vpin": vpin,
            "cascade": cascade,
            "recent_trades": recent_trades,
            "system": system,
        }

    async def get_summary(self) -> dict:
        """
        Lightweight top-bar stats.

        Returns bankroll, daily_pnl, total_pnl, win_rate, open_trades.
        """
        # System state (bankroll, etc.)
        state_result = await self._session.execute(
            select(SystemState).where(SystemState.id == 1)
        )
        state = state_result.scalar_one_or_none()
        engine_state: dict = state.state if state and state.state else {}

        # Trade stats
        stats_result = await self._session.execute(
            select(
                func.count(Trade.id).label("total"),
                func.sum(Trade.pnl_usd).label("total_pnl"),
            ).where(Trade.pnl_usd.isnot(None))
        )
        row = stats_result.one()

        wins_result = await self._session.execute(
            select(func.count()).where(Trade.outcome == "WIN")
        )
        wins = wins_result.scalar_one() or 0
        total = row.total or 0

        # Daily P&L
        today_result = await self._session.execute(
            select(func.coalesce(func.sum(Trade.pnl_usd), 0)).where(
                Trade.resolved_at >= datetime.now(timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
            )
        )
        daily_pnl = float(today_result.scalar_one() or 0)

        # Open trades
        open_result = await self._session.execute(
            select(func.count()).where(Trade.status.in_(["PENDING", "OPEN"]))
        )
        open_trades = open_result.scalar_one() or 0

        # Bankroll: use wallet_balance_usdc from engine state (or current_balance as fallback)
        bankroll = engine_state.get("wallet_balance_usdc") or engine_state.get("current_balance")
        
        return {
            "bankroll": bankroll,
            "daily_pnl": daily_pnl,
            "total_pnl": float(row.total_pnl or 0),
            "win_rate": wins / total if total > 0 else 0.0,
            "open_trades": open_trades,
            "kill_switch_active": engine_state.get("kill_switch_active", False),
            "paper_mode": engine_state.get("paper_mode", False),
        }

    # ─── Private Helpers ──────────────────────────────────────────────────────

    async def _get_system_state(self) -> dict:
        result = await self._session.execute(
            select(SystemState).where(SystemState.id == 1)
        )
        state = result.scalar_one_or_none()
        if state is None:
            return {"status": "offline"}
        return {
            "status": "online",
            "data": state.state,
            "updated_at": state.updated_at.isoformat() if state.updated_at else None,
        }

    async def _get_latest_signal(self, signal_type: str) -> dict | None:
        from sqlalchemy import desc

        result = await self._session.execute(
            select(Signal)
            .where(Signal.signal_type == signal_type)
            .order_by(desc(Signal.created_at))
            .limit(1)
        )
        sig = result.scalar_one_or_none()
        if sig is None:
            return None
        return {
            **sig.payload,
            "timestamp": sig.created_at.isoformat(),
        }

    async def _get_recent_trades(self, limit: int = 10) -> list[dict]:
        from sqlalchemy import desc

        result = await self._session.execute(
            select(Trade).order_by(desc(Trade.created_at)).limit(limit)
        )
        trades = result.scalars().all()
        return [
            {
                "id": t.id,
                "strategy": t.strategy,
                "market_slug": t.market_slug,
                "direction": t.direction,
                "stake_usd": float(t.stake_usd) if t.stake_usd else None,
                "pnl_usd": float(t.pnl_usd) if t.pnl_usd else None,
                "outcome": t.outcome,
                "status": t.status,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in trades
        ]
