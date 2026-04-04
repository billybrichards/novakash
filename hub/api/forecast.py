"""
Forecast & TWAP Dashboard API Routes

GET /api/forecast/latest          — latest TimesFM forecast
GET /api/forecast/history         — TimesFM forecasts + results  
GET /api/forecast/accuracy        — TimesFM backtesting accuracy
GET /api/forecast/twap-history    — TWAP data from window_snapshots
GET /api/forecast/window-detail   — full window snapshot with TWAP + CG + forecast
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text, func
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session

router = APIRouter()


@router.get("/forecast/latest")
async def get_latest_forecast(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Latest TimesFM forecast."""
    try:
        result = await session.execute(
            text("SELECT * FROM timesfm_forecasts ORDER BY created_at DESC LIMIT 1")
        )
        row = result.mappings().first()
        if not row:
            return {"status": "no_data"}
        return {k: v for k, v in row.items()}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}


@router.get("/forecast/history")
async def get_forecast_history(
    hours: int = Query(default=6, ge=1, le=48),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> list:
    """TimesFM forecast history for the last N hours."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = await session.execute(
            text("""
                SELECT window_ts, seconds_to_close, btc_price,
                       predicted_close, direction, confidence, spread,
                       p10, p25, p50, p75, p90,
                       actual_close, actual_direction, correct,
                       created_at
                FROM timesfm_forecasts
                WHERE created_at >= :cutoff
                ORDER BY created_at ASC
            """),
            {"cutoff": cutoff},
        )
        return [{k: v for k, v in r.items()} for r in result.mappings().all()]
    except Exception:
        return []


@router.get("/forecast/accuracy")
async def get_forecast_accuracy(
    hours: int = Query(default=24, ge=1, le=168),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """TimesFM prediction accuracy."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        # Overall stats
        result = await session.execute(
            text("""
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE correct = true) as correct,
                    COUNT(*) FILTER (WHERE correct = false) as wrong,
                    COUNT(*) FILTER (WHERE correct IS NULL) as pending,
                    AVG(confidence) FILTER (WHERE correct = true) as avg_conf_correct,
                    AVG(confidence) FILTER (WHERE correct = false) as avg_conf_wrong,
                    COUNT(*) FILTER (WHERE direction = 'UP') as up_count,
                    COUNT(*) FILTER (WHERE direction = 'UP' AND correct = true) as up_wins
                FROM timesfm_forecasts
                WHERE created_at >= :cutoff
            """),
            {"cutoff": cutoff},
        )
        row = result.mappings().first()
        stats = {k: v for k, v in row.items()} if row else {}

        # By time bucket
        buckets_result = await session.execute(
            text("""
                SELECT
                    CASE
                        WHEN seconds_to_close >= 240 THEN '240s+'
                        WHEN seconds_to_close >= 180 THEN '180-240s'
                        WHEN seconds_to_close >= 120 THEN '120-180s'
                        WHEN seconds_to_close >= 60 THEN '60-120s'
                        ELSE '0-60s'
                    END as time_bucket,
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE correct = true) as wins,
                    AVG(confidence) as avg_confidence
                FROM timesfm_forecasts
                WHERE created_at >= :cutoff AND correct IS NOT NULL
                GROUP BY time_bucket
                ORDER BY CASE time_bucket
                    WHEN '240s+' THEN 0
                    WHEN '180-240s' THEN 1
                    WHEN '120-180s' THEN 2
                    WHEN '60-120s' THEN 3
                    ELSE 4
                END
            """),
            {"cutoff": cutoff},
        )
        buckets = [{k: v for k, v in r.items()} for r in buckets_result.mappings().all()]

        return {"overall": stats, "by_time_bucket": buckets, "hours_analysed": hours}
    except Exception as exc:
        return {"error": str(exc)[:200]}


@router.get("/forecast/twap-history")
async def get_twap_history(
    hours: int = Query(default=6, ge=1, le=48),
    asset: str = Query(default="BTC"),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> list:
    """TWAP analysis data from window_snapshots."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = await session.execute(
            text("""
                SELECT window_ts, asset, timeframe,
                       delta_pct, direction, confidence, regime,
                       trade_placed, outcome, pnl_usd,
                       twap_delta_pct, twap_direction, twap_gamma_agree,
                       twap_agreement_score, twap_confidence_boost,
                       twap_n_ticks, twap_stability,
                       created_at
                FROM window_snapshots
                WHERE created_at >= :cutoff
                  AND (:asset = 'ALL' OR asset = :asset)
                ORDER BY created_at ASC
            """),
            {"cutoff": cutoff, "asset": asset},
        )
        return [{k: v for k, v in r.items()} for r in result.mappings().all()]
    except Exception:
        return []


@router.get("/forecast/window-detail")
async def get_window_detail(
    window_ts: int = Query(...),
    asset: str = Query(default="BTC"),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Full window snapshot with TWAP + CG + TimesFM forecasts."""
    try:
        # Window snapshot
        ws_result = await session.execute(
            text("SELECT * FROM window_snapshots WHERE window_ts = :wts AND asset = :asset LIMIT 1"),
            {"wts": window_ts, "asset": asset},
        )
        window = ws_result.mappings().first()

        # TimesFM forecasts for this window (if table exists)
        forecasts = []
        try:
            tf_result = await session.execute(
                text("""
                    SELECT * FROM timesfm_forecasts
                    WHERE window_ts = :wts
                    ORDER BY seconds_to_close DESC
                """),
                {"wts": window_ts},
            )
            forecasts = [{k: v for k, v in r.items()} for r in tf_result.mappings().all()]
        except Exception:
            pass  # Table might not exist yet

        return {
            "window": {k: v for k, v in window.items()} if window else None,
            "forecasts": forecasts,
            "forecast_count": len(forecasts),
        }
    except Exception as exc:
        return {"error": str(exc)[:200]}
