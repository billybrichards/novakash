"""
/api/ticks/{source} — read endpoints over ticks_chainlink, ticks_binance,
ticks_tiingo for the FE chart components on /desk and /dashboard.

Why this exists:
  The engine writes these tables on every poll (chainlink_feed,
  tiingo_feed, tick_recorder) but no read endpoint was registered in
  hub/main.py — so /desk PriceChart, CrossAssetSparks, and the new
  multi-source delta panel all 404'd. This router fills that gap with
  three small endpoints sharing the same shape:

    GET /api/ticks/chainlink ?asset=BTC&limit=300
    GET /api/ticks/binance   ?asset=BTC&limit=300
    GET /api/ticks/tiingo    ?asset=BTC&limit=300

  Returns:
    { rows: [{ ts_unix, price, source }, ...] } ordered by ts ASC for
    direct plotting (most-recent last).

Cold tables degrade to {rows: []} with a 200 — never 500.
"""

from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import get_session

log = structlog.get_logger(__name__)
router = APIRouter(tags=["ticks"])


def _is_missing_table(exc: Exception) -> bool:
    if not isinstance(exc, ProgrammingError):
        return False
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) == "42P01"


async def _fetch(
    session: AsyncSession,
    table: str,
    price_col: str,
    asset: str,
    limit: int,
) -> list[dict]:
    """Generic tail-N read for any ticks_* table with (ts, asset, <price>)."""
    asset_up = (asset or "BTC").upper()
    q = text(
        f"""
        SELECT
            EXTRACT(EPOCH FROM ts)::BIGINT AS ts_unix,
            {price_col}                    AS price
        FROM {table}
        WHERE asset = :asset
          AND {price_col} IS NOT NULL
        ORDER BY ts DESC
        LIMIT :limit
        """
    )
    try:
        res = await session.execute(q, {"asset": asset_up, "limit": limit})
    except Exception as exc:
        if _is_missing_table(exc):
            return []
        log.warning("ticks.fetch_err", table=table, error=str(exc)[:200])
        return []

    rows = [
        {"ts_unix": int(r[0]), "price": float(r[1])}
        for r in res.fetchall()
        if r[0] is not None and r[1] is not None
    ]
    rows.reverse()  # ASC for plotting
    return rows


@router.get("/ticks/chainlink")
async def get_chainlink_ticks(
    asset: str = Query("BTC"),
    limit: int = Query(300, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = await _fetch(session, "ticks_chainlink", "price", asset, limit)
    return {"rows": rows, "source": "chainlink", "asset": asset.upper()}


@router.get("/ticks/binance")
async def get_binance_ticks(
    asset: str = Query("BTC"),
    limit: int = Query(300, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = await _fetch(session, "ticks_binance", "price", asset, limit)
    return {"rows": rows, "source": "binance", "asset": asset.upper()}


@router.get("/ticks/tiingo")
async def get_tiingo_ticks(
    asset: str = Query("BTC"),
    limit: int = Query(300, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = await _fetch(session, "ticks_tiingo", "last_price", asset, limit)
    return {"rows": rows, "source": "tiingo", "asset": asset.upper()}
