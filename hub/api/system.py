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


def _safe_dict(value) -> dict:
    """Return ``value`` if it is a dict, else an empty dict — defensive
    against malformed jsonb that comes back as a string / list / None."""
    return value if isinstance(value, dict) else {}


def _derive_mode(state_row: SystemState | None) -> str:
    """
    Collapse the multi-writer system_state row into a single
    operator-facing mode string the FE can render directly.

    Two writers feed this row:
      - hub `/api/system/*` mutates the ``state`` jsonb. Sets
        ``kill_switch_manual`` (manual kill) and ``paper_mode``
        (mode toggle).
      - engine ``publish_heartbeat`` writes ``engine_status`` ("running"
        on every heartbeat) and ``config`` jsonb (carries
        ``kill_switch_active`` from the auto-drawdown kill, plus
        ``paper_mode`` from the engine's runtime config).

    Precedence (highest first):
      KILLED    any kill flag tripped (manual hub kill, engine
                kill_switch_active, or legacy kill_switch_auto)
      PAPER     paper-mode flag set in either hub-state or engine-config
                jsonb, or the dedicated ``paper_enabled`` column
      LIVE      engine_status == "running" / "active" (engine alive,
                not paper, not killed)
      UNKNOWN   no row, stale heartbeat, or unknown engine_status string
    """
    if state_row is None:
        return "UNKNOWN"

    hub_state = _safe_dict(state_row.state)
    engine_config = _safe_dict(state_row.config)

    # KILLED — any kill flag from either writer.
    if (
        hub_state.get("kill_switch_manual")
        or hub_state.get("kill_switch_auto")
        or hub_state.get("kill_switch_active")
        or engine_config.get("kill_switch_active")
    ):
        return "KILLED"

    # PAPER — three sources, in order of trust:
    #   1. hub state (operator just toggled via /api/system/paper-mode)
    #   2. engine config (engine's own runtime view)
    #   3. paper_enabled column (legacy fallback for hubs that haven't
    #      mapped the column yet — see SystemState model docstring)
    paper_flag = hub_state.get("paper_mode")
    if paper_flag is None:
        paper_flag = engine_config.get("paper_mode")
    if paper_flag is None:
        col = getattr(state_row, "paper_enabled", None)
        if col is not None:
            paper_flag = bool(col)
    if paper_flag:
        return "PAPER"

    # LIVE — engine_status is the canonical "engine alive" signal.
    # Accept both "running" (current) and "active" (legacy heartbeat).
    raw_status = (
        getattr(state_row, "engine_status", None)
        or hub_state.get("status")
        or hub_state.get("engine_status")
    )
    if isinstance(raw_status, str) and raw_status.strip().lower() in ("running", "active"):
        return "LIVE"

    return "UNKNOWN"


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

    `mode` is a derived string the FE renders as a single chip — one of
    LIVE / PAPER / KILLED / UNKNOWN. See `_derive_mode` for precedence.
    `engine_status` is the raw heartbeat string ("running" / "active" /
    "starting" / etc.) — exposed alongside `mode` so the FE tooltip can
    show diagnostic detail when `mode == "UNKNOWN"`.
    """
    result = await session.execute(select(SystemState).where(SystemState.id == 1))
    state = result.scalar_one_or_none()

    if state is None:
        return {
            "status": "offline",
            "mode": "UNKNOWN",
            "engine_status": None,
            "detail": "Engine has not reported state yet",
        }

    return {
        "status": "online",
        "mode": _derive_mode(state),
        "engine_status": getattr(state, "engine_status", None),
        "data": state.state,
        "config": getattr(state, "config", None),
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
