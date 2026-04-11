"""
Margin Engine, V1/V2/V3/V4 data-surface proxy endpoints.

Forwards requests to the margin engine (eu-west-2) and TimesFM service
(Montreal) so the frontend can access them through the Hub's auth layer.

GET  /api/margin/status              — margin engine portfolio + positions
GET  /api/margin/logs                — recent log lines (filterable)
GET  /api/margin/positions/history   — paginated closed-position history (Trade Timeline tab)
GET  /api/v1/forecast                — legacy TimesFM point forecast (BTC only)
GET  /api/v1/health                  — legacy TimesFM health
GET  /api/v2/probability             — Sequoia v5.2 5m probability + quantiles
GET  /api/v2/probability/15m         — Sequoia v5.2 15m probability + quantiles
GET  /api/v2/health                  — v2 scorer health
GET  /api/v2/models                  — v2 model registry (production + staging)
GET  /api/v3/snapshot                — v3 composite signal scores (all timescales)
GET  /api/v3/health                  — v3 system health
GET  /api/v4/snapshot                — v4 fusion surface (consensus + macro + per-TS)
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


# ─── V1 Legacy Forecast ────────────────────────────────────────────────────
# TimesFM point forecast is the original /forecast endpoint on the model
# service — BTC only, no asset param. The v1 surface has been superseded by
# v2/v3/v4 but the endpoint is still live for backward compatibility and
# for the /data/v1 dashboard page.


@router.get("/v1/forecast")
async def v1_forecast(
    horizon: int = Query(default=0, ge=0, le=600),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Proxy to TimesFM — legacy /forecast endpoint (BTC only, frozen surface).

    horizon=0 (default) returns the cached 300-step forecast.
    horizon>0 runs a fresh inference with that exact horizon (1-600).
    """
    params = {"horizon": horizon} if horizon else None
    return await _proxy_get(TIMESFM_URL, "/forecast", params)


@router.get("/v1/health")
async def v1_health(
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Proxy to TimesFM — legacy /health endpoint (model + feed status)."""
    return await _proxy_get(TIMESFM_URL, "/health")


# ─── V2 Sequoia Probability ───────────────────────────────────────────────
# Sequoia v5.2 LightGBM scorer — calibrated P(UP) at a specific
# seconds_to_close window close. Returns the nested timesfm block (quantiles,
# predicted_close, direction, confidence, spread) so the /data/v2 dashboard
# can render the raw-vs-calibrated split and the quantile fan.


@router.get("/v2/probability")
async def v2_probability(
    asset: str = Query(default="BTC"),
    seconds_to_close: int = Query(default=60, ge=1, le=300),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Proxy to TimesFM — production 5m probability + quantiles."""
    return await _proxy_get(
        TIMESFM_URL, "/v2/probability",
        {"asset": asset, "seconds_to_close": seconds_to_close},
    )


@router.get("/v2/probability/15m")
async def v2_probability_15m(
    asset: str = Query(default="BTC"),
    seconds_to_close: int = Query(default=300, ge=1, le=900),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Proxy to TimesFM — 15-minute probability + quantiles."""
    return await _proxy_get(
        TIMESFM_URL, "/v2/probability/15m",
        {"asset": asset, "seconds_to_close": seconds_to_close},
    )


@router.get("/v2/health")
async def v2_health(
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Proxy to TimesFM — v2 scorer health + per-asset feature cache warmth."""
    return await _proxy_get(TIMESFM_URL, "/v2/health")


@router.get("/v2/models")
async def v2_models(
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Proxy to TimesFM — list of loaded v2 models with metadata."""
    return await _proxy_get(TIMESFM_URL, "/v2/models")


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


# ─── V4 Fusion Decision Surface ────────────────────────────────────────────


@router.get("/v4/snapshot")
async def v4_snapshot(
    asset: str = Query(default="BTC"),
    timescales: str = Query(default="5m,15m,1h,4h"),
    strategy: str = Query(default="fee_aware_15m"),
    max_age_s: int = Query(default=120, ge=10, le=600),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Proxy to TimesFM — fused decision surface for paper-mode monitoring.

    Returns the same payload the margin engine consumes: macro bias +
    per-timescale recommended_action with the gate stack's actual reason
    (e.g. macro_gate_skip_up, regime_choppy_skip, quantile_fee_wall_skip).

    The frontend polls this so a human can see why the engine is skipping
    or entering — the surface is self-explaining.
    """
    return await _proxy_get(
        TIMESFM_URL, "/v4/snapshot",
        {
            "asset": asset,
            "timescales": timescales,
            "strategy": strategy,
            "max_age_s": max_age_s,
        },
    )


@router.get("/v4/macro")
async def v4_macro(
    asset: str = Query(default="BTC"),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Proxy to TimesFM — Qwen-generated macro bias with per-timescale map."""
    return await _proxy_get(TIMESFM_URL, "/v4/macro", {"asset": asset})


@router.get("/v4/recommendation")
async def v4_recommendation(
    asset: str = Query(default="BTC"),
    strategy: str = Query(default="fee_aware_15m"),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Proxy to TimesFM — recommended_action only (no full snapshot)."""
    return await _proxy_get(
        TIMESFM_URL, "/v4/recommendation",
        {"asset": asset, "strategy": strategy},
    )
