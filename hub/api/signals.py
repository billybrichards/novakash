"""
Signals API Routes

GET /api/signals/vpin    — latest VPIN readings
GET /api/signals/cascade — cascade detector state history
GET /api/signals/arb     — recent arb opportunities detected
GET /api/signals/regime  — market regime classification
"""

from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session
from db.models import Signal

router = APIRouter()


@router.get("/signals/vpin")
async def get_vpin_signals(
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return the most recent VPIN signal readings.

    Returns list of {value, informed_threshold_crossed, cascade_threshold_crossed, timestamp}.
    """
    result = await session.execute(
        select(Signal)
        .where(Signal.signal_type == "vpin")
        .order_by(desc(Signal.created_at))
        .limit(limit)
    )
    signals = result.scalars().all()

    return {
        "signals": [
            {
                "value": s.payload.get("value"),
                "informed_threshold_crossed": s.payload.get("informed_threshold_crossed"),
                "cascade_threshold_crossed": s.payload.get("cascade_threshold_crossed"),
                "buckets_filled": s.payload.get("buckets_filled"),
                "timestamp": s.created_at.isoformat(),
            }
            for s in signals
        ]
    }


@router.get("/signals/cascade")
async def get_cascade_signals(
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Return cascade FSM state transition history."""
    result = await session.execute(
        select(Signal)
        .where(Signal.signal_type == "cascade")
        .order_by(desc(Signal.created_at))
        .limit(limit)
    )
    signals = result.scalars().all()

    return {
        "signals": [
            {
                "state": s.payload.get("state"),
                "direction": s.payload.get("direction"),
                "vpin": s.payload.get("vpin"),
                "oi_delta_pct": s.payload.get("oi_delta_pct"),
                "liq_volume_usd": s.payload.get("liq_volume_usd"),
                "timestamp": s.created_at.isoformat(),
            }
            for s in signals
        ]
    }


@router.get("/signals/arb")
async def get_arb_signals(
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Return recent arbitrage opportunity detections."""
    result = await session.execute(
        select(Signal)
        .where(Signal.signal_type == "arb")
        .order_by(desc(Signal.created_at))
        .limit(limit)
    )
    signals = result.scalars().all()

    return {
        "signals": [
            {
                "market_slug": s.payload.get("market_slug"),
                "yes_price": s.payload.get("yes_price"),
                "no_price": s.payload.get("no_price"),
                "combined_price": s.payload.get("combined_price"),
                "net_spread": s.payload.get("net_spread"),
                "timestamp": s.created_at.isoformat(),
            }
            for s in signals
        ]
    }


@router.get("/signals/regime")
async def get_regime_signals(
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Return market regime classification history."""
    result = await session.execute(
        select(Signal)
        .where(Signal.signal_type == "regime")
        .order_by(desc(Signal.created_at))
        .limit(limit)
    )
    signals = result.scalars().all()

    return {
        "signals": [
            {
                "regime": s.payload.get("regime"),
                "confidence": s.payload.get("confidence"),
                "features": s.payload.get("features", {}),
                "timestamp": s.created_at.isoformat(),
            }
            for s in signals
        ]
    }
