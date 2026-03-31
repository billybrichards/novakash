"""
Config API Routes

GET /api/config  — retrieve current runtime configuration
PUT /api/config  — update runtime configuration
"""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session
from db.models import SystemState

router = APIRouter()


class ConfigUpdateRequest(BaseModel):
    """Partial config update — only provided fields are changed."""
    vpin_bucket_size_usd: float | None = None
    vpin_lookback_buckets: int | None = None
    vpin_informed_threshold: float | None = None
    vpin_cascade_threshold: float | None = None
    bet_fraction: float | None = None
    max_open_exposure_pct: float | None = None
    daily_loss_limit_pct: float | None = None
    arb_min_spread: float | None = None
    arb_max_position: float | None = None
    strategy_sub_dollar_arb_enabled: bool | None = None
    strategy_vpin_cascade_enabled: bool | None = None


@router.get("/config")
async def get_config(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return the current runtime configuration.

    Config is stored as a JSONB blob in system_state alongside
    the live engine metrics.
    """
    result = await session.execute(select(SystemState).where(SystemState.id == 1))
    state = result.scalar_one_or_none()

    config = {}
    if state and state.state:
        config = state.state.get("config", {})

    return {"config": config}


@router.put("/config")
async def update_config(
    body: ConfigUpdateRequest,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Partial update of the runtime configuration.

    Changes are persisted to system_state. The engine reads this
    on its next heartbeat cycle and applies them.
    """
    result = await session.execute(select(SystemState).where(SystemState.id == 1))
    state = result.scalar_one_or_none()

    if state is None:
        return {"success": False, "detail": "Engine state not found"}

    current = state.state or {}
    config = current.get("config", {})

    # Apply only provided fields
    updates = body.model_dump(exclude_none=True)
    config.update(updates)
    current["config"] = config
    state.state = current

    await session.commit()

    return {"success": True, "config": config}
