"""
System API Routes

GET  /api/system/status     — engine health, venue connectivity, kill-switch state
POST /api/system/kill       — trigger emergency kill switch
POST /api/system/resume     — resume trading after kill/pause
POST /api/system/paper-mode — toggle paper trading mode
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session
from db.models import SystemState

router = APIRouter()


class PaperModeRequest(BaseModel):
    enabled: bool


class RedeemRequest(BaseModel):
    redeem_type: str = "all"


@router.get("/system/status")
async def get_system_status(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return the current engine system state including:
      - Engine running status
      - Kill switch active flag
      - Paper mode flag
      - Venue connectivity (Polymarket, Opinion)
      - Current bankroll and drawdown
      - Last heartbeat timestamp
    """
    result = await session.execute(select(SystemState).where(SystemState.id == 1))
    state = result.scalar_one_or_none()

    if state is None:
        return {"status": "offline", "detail": "Engine has not reported state yet"}

    return {
        "status": "online",
        "data": state.state,
        "updated_at": state.updated_at.isoformat() if state.updated_at else None,
    }


@router.post("/system/kill")
async def kill_switch(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Trigger the emergency kill switch.

    This sets a flag in system_state that the engine reads on its next heartbeat.
    The engine will halt all new order placement immediately.
    """
    result = await session.execute(select(SystemState).where(SystemState.id == 1))
    state = result.scalar_one_or_none()

    if state is None:
        return {
            "success": False,
            "detail": "Engine state not found — is the engine running?",
        }

    current = state.state or {}
    current["kill_switch_manual"] = True
    state.state = current
    await session.commit()

    return {
        "success": True,
        "message": "Kill switch activated — engine will halt new orders",
    }


@router.post("/system/resume")
async def resume_trading(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Resume trading after a kill switch or manual pause.

    Clears the kill_switch_manual and paused flags in system_state.
    """
    result = await session.execute(select(SystemState).where(SystemState.id == 1))
    state = result.scalar_one_or_none()

    if state is None:
        return {"success": False, "detail": "Engine state not found"}

    current = state.state or {}
    current["kill_switch_manual"] = False
    current["paused"] = False
    state.state = current
    await session.commit()

    return {"success": True, "message": "Trading resumed"}


@router.post("/system/paper-mode")
async def set_paper_mode(
    body: PaperModeRequest,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Toggle paper trading mode.

    In paper mode the engine evaluates all signals and logs simulated trades
    but does not submit any real orders.
    """
    result = await session.execute(select(SystemState).where(SystemState.id == 1))
    state = result.scalar_one_or_none()

    if state is None:
        return {"success": False, "detail": "Engine state not found"}

    current = state.state or {}
    current["paper_mode"] = body.enabled
    state.state = current
    state.paper_enabled = body.enabled
    state.live_enabled = not body.enabled
    await session.commit()

    mode = "enabled" if body.enabled else "disabled"
    return {"success": True, "message": f"Paper mode {mode}"}


@router.get("/system/redeemer-status")
async def get_redeemer_status(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    state_row = (
        (
            await session.execute(
                text(
                    """
                SELECT usdc_balance, positions_value, positions_json, redeemable_json,
                       quota_used_today, quota_limit, cooldown_until, cooldown_reason, updated_at
                FROM playwright_state WHERE id = 1
                """
                )
            )
        )
        .mappings()
        .first()
    )
    latest_event = (
        (
            await session.execute(
                text(
                    """
                SELECT redeem_type, redeemed_count, failed_count, total_value, details_json, created_at
                FROM redeem_events ORDER BY created_at DESC LIMIT 1
                """
                )
            )
        )
        .mappings()
        .first()
    )

    if not state_row:
        return {"success": False, "detail": "Redeemer state not available yet"}

    positions = state_row["positions_json"] or []
    redeemable = state_row["redeemable_json"] or []
    redeemable_wins = [p for p in redeemable if p.get("outcome") == "WIN"]
    redeemable_losses = [p for p in redeemable if p.get("outcome") == "LOSS"]
    return {
        "success": True,
        "data": {
            "cash_balance": state_row["usdc_balance"] or 0.0,
            "positions_value": state_row["positions_value"] or 0.0,
            "portfolio_value": (state_row["usdc_balance"] or 0.0)
            + (state_row["positions_value"] or 0.0),
            "open_positions": len(positions),
            "redeemable_wins": len(redeemable_wins),
            "redeemable_losses": len(redeemable_losses),
            "quota_used_today": state_row["quota_used_today"] or 0,
            "quota_limit": state_row["quota_limit"] or 100,
            "cooldown_until": state_row["cooldown_until"].isoformat()
            if state_row["cooldown_until"]
            else None,
            "cooldown_reason": state_row["cooldown_reason"] or "",
            "latest_event": dict(latest_event) if latest_event else None,
            "updated_at": state_row["updated_at"].isoformat()
            if state_row["updated_at"]
            else None,
        },
    }


@router.post("/system/redeem/wins")
async def request_redeem_wins(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    await session.execute(
        text(
            "UPDATE playwright_state SET redeem_requested = TRUE, redeem_request_type = 'wins', updated_at = NOW() WHERE id = 1"
        )
    )
    await session.commit()
    return {"success": True, "message": "Manual win redemption requested"}


@router.post("/system/redeem/losses")
async def request_redeem_losses(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    await session.execute(
        text(
            "UPDATE playwright_state SET redeem_requested = TRUE, redeem_request_type = 'losses', updated_at = NOW() WHERE id = 1"
        )
    )
    await session.commit()
    return {"success": True, "message": "Manual loss redemption requested"}


@router.post("/system/redeem/all")
async def request_redeem_all(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    await session.execute(
        text(
            "UPDATE playwright_state SET redeem_requested = TRUE, redeem_request_type = 'all', updated_at = NOW() WHERE id = 1"
        )
    )
    await session.commit()
    return {"success": True, "message": "Manual full redemption requested"}
