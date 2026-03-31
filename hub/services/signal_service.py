"""
Signal Service

Business logic for querying and aggregating trading signals.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Signal


class SignalService:
    """Query helpers for signal data."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_latest_vpin(self) -> Optional[dict]:
        """Return the most recent VPIN signal payload."""
        return await self._get_latest("vpin")

    async def get_latest_cascade(self) -> Optional[dict]:
        """Return the most recent cascade state signal."""
        return await self._get_latest("cascade")

    async def get_latest_arb(self) -> Optional[dict]:
        """Return the most recent arb opportunity signal."""
        return await self._get_latest("arb")

    async def get_latest_regime(self) -> Optional[dict]:
        """Return the most recent market regime signal."""
        return await self._get_latest("regime")

    async def get_vpin_series(self, limit: int = 200) -> list[dict]:
        """
        Return time-series VPIN values for charting.

        Returns list of {value, cascade_threshold_crossed, timestamp}.
        """
        from sqlalchemy import desc

        result = await self._session.execute(
            select(Signal)
            .where(Signal.signal_type == "vpin")
            .order_by(desc(Signal.created_at))
            .limit(limit)
        )
        signals = result.scalars().all()

        return [
            {
                "value": s.payload.get("value"),
                "cascade_threshold_crossed": s.payload.get("cascade_threshold_crossed"),
                "informed_threshold_crossed": s.payload.get("informed_threshold_crossed"),
                "timestamp": s.created_at.isoformat(),
            }
            for s in reversed(signals)  # Chronological order
        ]

    async def get_cascade_transitions(self, limit: int = 50) -> list[dict]:
        """
        Return cascade FSM state transitions.

        Returns list of {from_state, state, direction, vpin, timestamp}.
        """
        from sqlalchemy import desc

        result = await self._session.execute(
            select(Signal)
            .where(Signal.signal_type == "cascade")
            .order_by(desc(Signal.created_at))
            .limit(limit)
        )
        signals = result.scalars().all()

        return [
            {
                "state": s.payload.get("state"),
                "direction": s.payload.get("direction"),
                "vpin": s.payload.get("vpin"),
                "oi_delta_pct": s.payload.get("oi_delta_pct"),
                "timestamp": s.created_at.isoformat(),
            }
            for s in signals
        ]

    async def get_signal_counts(self, since_hours: int = 24) -> dict:
        """
        Return counts of each signal type in the last N hours.

        Useful for the system health dashboard.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

        result = await self._session.execute(
            select(
                Signal.signal_type,
                func.count(Signal.id).label("count"),
            )
            .where(Signal.created_at >= cutoff)
            .group_by(Signal.signal_type)
        )
        rows = result.all()

        return {row.signal_type: row.count for row in rows}

    # ─── Private ──────────────────────────────────────────────────────────────

    async def _get_latest(self, signal_type: str) -> Optional[dict]:
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
