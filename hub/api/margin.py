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
POST /api/predict                    — canonical versioned envelope (Assembler1)
GET  /api/predict/ticks_vs_outcomes  — live prediction ticks joined to actual window outcomes
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session

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


# ─── POST /api/predict — Assembler1 canonical envelope ─────────────────────


async def _proxy_post(base_url: str, path: str, body: dict) -> dict:
    """Forward a POST with JSON body and return JSON.

    Mirrors _proxy_get's error translation: ConnectError → 502,
    upstream HTTPStatusError → passthrough status with a sanitised
    detail, other httpx.RequestError → 502.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(f"{base_url}{path}", json=body)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(
            status_code=502,
            detail=f"Cannot reach service at {base_url}",
        )
    except httpx.HTTPStatusError as exc:
        # Pass the upstream detail through so pydantic 422 validation
        # errors surface correctly to the caller (the frontend shows
        # them in the /assembler1 diagnostic panel).
        try:
            upstream_detail = exc.response.json()
        except Exception:
            upstream_detail = exc.response.text[:200]
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=upstream_detail,
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Network error: {exc}")


@router.post("/predict")
async def predict(
    body: dict = Body(...),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Proxy to TimesFM POST /predict — the canonical envelope.

    The hub is a thin passthrough: we don't validate the request body
    here because the timesfm service's PredictRequest pydantic model
    already does the heavy lifting. We just forward the JSON and let
    upstream 422s flow back to the caller.

    Frontend at /assembler1 uses this endpoint to build its live
    dashboard. See
    timesfm-repo: docs/superpowers/plans/2026-04-11-predict-envelope-refactor.md
    """
    return await _proxy_post(TIMESFM_URL, "/predict", body)


# ─── GET /api/predict/ticks_vs_outcomes — live prediction vs actual ────────


@router.get("/predict/ticks_vs_outcomes")
async def ticks_vs_outcomes(
    asset: str = Query(default="BTC"),
    timeframe: str = Query(default="15m", regex="^(5m|15m|1h|4h|24h)$"),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return recent v2 prediction ticks joined to their window outcomes.

    This is the live-validation view: for each prediction tick in the
    requested window the database has a row showing the model's
    probability, and the window_predictions table records the actual
    outcome (Polymarket oracle winner, or Binance delta for live-only
    predictions). Joining them lets the frontend show a live
    "predicted vs actual" tape.

    The 5m and 15m timeframes join on `window_predictions.oracle_winner`
    which is populated from Polymarket. Longer horizons (1h, 4h) fall
    back to the Binance delta direction — same join key, different
    source.

    Returns:
      {
        "rows": [
          {
            "ts": "2026-04-11T15:42:01.123Z",
            "window_ts": 1776500400,
            "probability_up": 0.67,
            "probability_raw": 0.71,
            "model_version": "15a4e3e@v2/btc/btc_5m/...",
            "predicted_direction": "UP",
            "actual_direction": "UP",     // from window_predictions
            "correct": true,
            "outcome_source": "oracle_winner" | "binance_delta" | null,
            "window_open_price": 72810.0,
            "window_close_price": 72843.2,
            "window_move_bps": 4.56
          },
          ...
        ],
        "summary": {
          "n_total": 100,
          "n_with_outcome": 88,
          "n_correct": 52,
          "hit_rate": 0.591,
          "direction_counts": {"UP": 44, "DOWN": 44}
        }
      }

    Returns HTTP 400 if the timeframe is unsupported.
    """
    # Build the model_version LIKE pattern. 5m uses the short-term v5
    # slot, 15m uses the btc_15m slot, 1h uses btc_1h. The "/nogit"
    # variants are also matched so stale/manual artifacts still surface.
    if timeframe == "5m":
        model_like = "%/btc_5m/%"
    elif timeframe == "15m":
        model_like = "15m/%"
    elif timeframe == "1h":
        model_like = "1h/%"
    elif timeframe == "4h":
        model_like = "4h/%"
    else:  # 24h — no model, return empty rows
        return {
            "rows": [],
            "summary": {
                "n_total": 0, "n_with_outcome": 0, "n_correct": 0,
                "hit_rate": None, "direction_counts": {},
            },
            "note": f"timeframe {timeframe!r} has no dedicated v2 model yet",
        }

    # The actual join: pull recent ticks, left-join to window_predictions
    # on the trading window they belong to. window_predictions rows are
    # populated by the old engine's window-close handler — may be missing
    # for very recent ticks (the window hasn't closed yet).
    query = text("""
        SELECT
            t.ts                         AS ts,
            t.probability_up             AS probability_up,
            t.probability_raw            AS probability_raw,
            t.model_version              AS model_version,
            t.seconds_to_close           AS seconds_to_close,
            wp.window_ts                 AS window_ts,
            wp.v2_direction              AS predicted_direction,
            wp.our_signal_direction      AS our_direction,
            wp.oracle_winner             AS oracle_winner,
            wp.tiingo_direction          AS tiingo_direction,
            wp.chainlink_direction       AS chainlink_direction,
            wp.v2_correct                AS v2_correct,
            wp.our_signal_correct        AS our_signal_correct,
            wp.tiingo_open               AS window_open_price,
            wp.tiingo_close              AS window_close_price
        FROM ticks_v2_probability t
        LEFT JOIN window_predictions wp
          ON wp.asset = t.asset
         AND wp.timeframe = :timeframe
         AND wp.window_ts = (
             (EXTRACT(EPOCH FROM t.ts)::bigint / :window_seconds)
             * :window_seconds
         )
        WHERE t.asset = :asset
          AND t.model_version LIKE :model_like
          AND t.ts > now() - interval '2 hours'
        ORDER BY t.ts DESC
        LIMIT :limit
    """)

    window_seconds = {
        "5m": 300, "15m": 900, "1h": 3600, "4h": 14400,
    }[timeframe]

    result = await session.execute(
        query,
        {
            "asset": asset.upper(),
            "timeframe": timeframe,
            "model_like": model_like,
            "window_seconds": window_seconds,
            "limit": limit,
        },
    )
    raw_rows = result.mappings().all()

    rows: list[dict[str, Any]] = []
    n_with_outcome = 0
    n_correct = 0
    dir_counts: dict[str, int] = {}

    for r in raw_rows:
        p_up = r["probability_up"]
        predicted_dir = "UP" if p_up is not None and p_up > 0.5 else ("DOWN" if p_up is not None else None)
        actual_dir: str | None = None
        outcome_source: str | None = None
        if r["oracle_winner"]:
            actual_dir = r["oracle_winner"].upper()
            outcome_source = "oracle_winner"
        elif r["tiingo_direction"]:
            actual_dir = r["tiingo_direction"].upper()
            outcome_source = "tiingo_delta"
        elif r["chainlink_direction"]:
            actual_dir = r["chainlink_direction"].upper()
            outcome_source = "chainlink_delta"

        correct: bool | None = None
        if actual_dir and predicted_dir:
            correct = (actual_dir == predicted_dir)
            n_with_outcome += 1
            if correct:
                n_correct += 1

        if predicted_dir:
            dir_counts[predicted_dir] = dir_counts.get(predicted_dir, 0) + 1

        open_px = r["window_open_price"]
        close_px = r["window_close_price"]
        move_bps: float | None = None
        if open_px and close_px and open_px > 0:
            move_bps = float((close_px - open_px) / open_px * 10_000)

        rows.append({
            "ts": r["ts"].isoformat() if r["ts"] else None,
            "window_ts": r["window_ts"],
            "probability_up": float(p_up) if p_up is not None else None,
            "probability_raw": float(r["probability_raw"]) if r["probability_raw"] is not None else None,
            "model_version": r["model_version"],
            "seconds_to_close": r["seconds_to_close"],
            "predicted_direction": predicted_dir,
            "actual_direction": actual_dir,
            "correct": correct,
            "outcome_source": outcome_source,
            "window_open_price": float(open_px) if open_px else None,
            "window_close_price": float(close_px) if close_px else None,
            "window_move_bps": round(move_bps, 2) if move_bps is not None else None,
        })

    hit_rate = round(n_correct / n_with_outcome, 4) if n_with_outcome > 0 else None
    return {
        "rows": rows,
        "summary": {
            "n_total": len(rows),
            "n_with_outcome": n_with_outcome,
            "n_correct": n_correct,
            "hit_rate": hit_rate,
            "direction_counts": dir_counts,
        },
    }
