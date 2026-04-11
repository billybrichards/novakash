"""
Margin Engine & V3 Composite Signal proxy endpoints.

Forwards requests to the margin engine (eu-west-2) and TimesFM v3 service
so the frontend can access them through the Hub's auth layer.

GET  /api/margin/status              — margin engine portfolio + positions
GET  /api/margin/logs                — recent log lines (filterable)
GET  /api/margin/positions/history   — paginated closed-position history (Trade Timeline tab)
GET  /api/v3/snapshot                — v3 composite signal scores (all timescales)
GET  /api/v3/health                  — v3 system health
"""

from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from auth.jwt import TokenData
from auth.middleware import get_current_user

router = APIRouter()

MARGIN_ENGINE_URL = os.environ.get("MARGIN_ENGINE_URL", "http://localhost:8090")
TIMESFM_URL = os.environ.get("TIMESFM_URL", "http://localhost:8001")

_TIMEOUT = 8.0


async def _proxy_get(base_url: str, path: str, params: dict | None = None) -> dict:
    """Forward a GET request and return JSON."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{base_url}{path}", params=params)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail=f"Cannot reach service at {base_url}")
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"Upstream returned {exc.response.status_code}",
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Network error: {exc}")


# ─── Margin Engine ──────────────────────────────────────────────────────────


@router.get("/margin/status")
async def margin_status(
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Proxy to margin engine — portfolio state, positions, P&L."""
    return await _proxy_get(MARGIN_ENGINE_URL, "/status")


@router.get("/margin/logs")
async def margin_logs(
    limit: int = Query(default=100, le=500),
    level: str | None = Query(default=None),
    since_minutes: int = Query(default=60, le=1440),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Proxy to margin engine — persisted log entries."""
    return await _proxy_get(
        MARGIN_ENGINE_URL, "/logs",
        {"limit": limit, "level": level, "since_minutes": since_minutes},
    )


@router.get("/margin/positions/history")
async def margin_positions_history(
    limit: int = Query(default=25, le=100, ge=1),
    offset: int = Query(default=0, ge=0),
    side: str | None = Query(default=None, pattern="^(LONG|SHORT)$"),
    outcome: str | None = Query(default=None, pattern="^(win|loss)$"),
    exit_reason: str | None = Query(default=None),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Proxy to margin engine — paginated closed-position history.

    FastAPI's `pattern=` constraints reject malformed input at the Hub
    boundary so the engine never sees garbage. exit_reason is a free-form
    CSV (e.g. "TAKE_PROFIT,STOP_LOSS"); the engine validates internally.

    Returns: { rows: [...], total: int, limit: int, offset: int }
    """
    params = {"limit": limit, "offset": offset}
    if side:
        params["side"] = side
    if outcome:
        params["outcome"] = outcome
    if exit_reason:
        params["exit_reason"] = exit_reason
    return await _proxy_get(MARGIN_ENGINE_URL, "/history", params)


# ─── V3 Composite Signals ──────────────────────────────────────────────────


@router.get("/v3/snapshot")
async def v3_snapshot(
    asset: str = Query(default="BTC"),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Proxy to TimesFM — composite scores for all 9 timescales."""
    return await _proxy_get(TIMESFM_URL, "/v3/snapshot", {"asset": asset})


@router.get("/v3/health")
async def v3_health(
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Proxy to TimesFM — v3 system status."""
    return await _proxy_get(TIMESFM_URL, "/v3/health")
