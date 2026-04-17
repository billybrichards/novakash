"""
Trades API Routes

GET /api/trades           — paginated list with filters
GET /api/trades/{id}      — single trade detail
GET /api/trades/stats     — aggregate stats (win rate, avg PnL, etc.)
"""

from __future__ import annotations

import json
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session
from db.models import Trade

router = APIRouter()


def _f(v: Any) -> Optional[float]:
    """Decimal/None → float. Preserves 0.0 (the prior `if x else None` pattern
    treated Decimal('0') as falsy and dropped real zero values)."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _meta(v: Any) -> dict:
    """JSONB column → dict. asyncpg usually decodes automatically, but fall
    back to json.loads when the driver hands back a string."""
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return {}
    return {}


def _row_to_dict(r: dict) -> dict:
    """Serialize a `trades` row including v8 exec columns, SOT columns, and
    metadata JSONB extracts (regime / conviction / dedup_key / skip_reason /
    exit_price). The SQLAlchemy `Trade` model only declares a subset of the
    real columns, so endpoints below SELECT * and serialize directly from the
    row mapping."""
    meta = _meta(r.get("metadata"))

    created_at = r.get("created_at")
    resolved_at = r.get("resolved_at")

    return {
        "id": r.get("id"),
        "order_id": r.get("order_id"),
        "strategy": r.get("strategy"),
        "strategy_id": r.get("strategy_id"),
        "strategy_version": r.get("strategy_version"),
        "venue": r.get("venue"),
        "market_slug": r.get("market_slug"),
        "direction": r.get("direction"),
        "entry_price": _f(r.get("entry_price")),
        "fill_price": _f(r.get("fill_price")),
        "fill_size": _f(r.get("fill_size")),
        "stake_usd": _f(r.get("stake_usd")),
        "fee_usd": _f(r.get("fee_usd")),
        "status": r.get("status"),
        "outcome": r.get("outcome"),
        "payout_usd": _f(r.get("payout_usd")),
        "pnl_usd": _f(r.get("pnl_usd")),
        "mode": r.get("mode"),
        "is_live": r.get("is_live"),
        "execution_mode": r.get("execution_mode"),
        "clob_order_id": r.get("clob_order_id"),
        "engine_version": r.get("engine_version"),
        "polymarket_confirmed_status": r.get("polymarket_confirmed_status"),
        "polymarket_confirmed_fill_price": _f(r.get("polymarket_confirmed_fill_price")),
        "polymarket_confirmed_size": _f(r.get("polymarket_confirmed_size")),
        "sot_reconciliation_state": r.get("sot_reconciliation_state"),
        # Metadata JSONB extracts — not real columns, surfaced as top-level
        # fields for the FE table.
        "regime": meta.get("regime"),
        "conviction": meta.get("conviction"),
        "dedup_key": meta.get("dedup_key"),
        "skip_reason": meta.get("skip_reason"),
        "exit_price": _f(
            meta.get("exit_price")
            or r.get("polymarket_confirmed_fill_price")
        ),
        "created_at": created_at.isoformat() if created_at else None,
        "resolved_at": resolved_at.isoformat() if resolved_at else None,
        # Phantom flag — lets FE visually distinguish trades that never
        # went on-chain (gtc_resting/gtc with no fill) from real trades.
        "is_phantom": r.get("status") == "PHANTOM",
    }


@router.get("/trades")
async def list_trades(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=1000),
    limit: Optional[int] = Query(None, ge=1, le=1000),
    since_days: Optional[int] = Query(None, ge=1, le=365),
    strategy: Optional[str] = Query(None),
    outcome: Optional[str] = Query(None),
    market_slug: Optional[str] = Query(None),
    only_filled: bool = Query(True),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return paginated list of trades.

    Params:
      - page / page_size: standard pagination (page_size ≤ 1000)
      - limit: alias for page_size; when set, offset forced to 0 (simple
        "most recent N" mode — matches the FE `?limit=500` call shape).
      - since_days: restrict to trades created in the last N days
      - strategy: matches either `strategy` or `strategy_id` column
      - outcome: "WIN" | "LOSS" | "PUSH" | "OPEN"
      - market_slug: filter by specific market
      - only_filled: default true. Hides rows with NULL fill_price AND NULL
        entry_price AND no polymarket_confirmed_status — pre-#211 legacy
        orphans and abandoned orders which carry a stake/outcome but never
        actually filled on the CLOB.
    """

    effective_limit = limit if limit is not None else page_size
    offset = 0 if limit is not None else (page - 1) * effective_limit

    where: list[str] = []
    params: dict[str, Any] = {}

    if strategy:
        where.append("(strategy = :strategy OR strategy_id = :strategy)")
        params["strategy"] = strategy
    if outcome:
        where.append("outcome = :outcome")
        params["outcome"] = outcome
    if market_slug:
        where.append("market_slug = :market_slug")
        params["market_slug"] = market_slug
    if since_days is not None:
        where.append("created_at >= NOW() - make_interval(days => :since_days)")
        params["since_days"] = since_days
    if only_filled:
        where.append(
            "(fill_price IS NOT NULL "
            "OR polymarket_confirmed_status IN ('filled','matched') "
            "OR entry_price IS NOT NULL)"
        )

    where_sql = f" WHERE {' AND '.join(where)}" if where else ""

    count_row = await session.execute(
        text(f"SELECT COUNT(*) AS n FROM trades{where_sql}"), params
    )
    total = int(count_row.scalar_one() or 0)

    rows_res = await session.execute(
        text(
            f"""
            SELECT *
            FROM trades
            {where_sql}
            ORDER BY created_at DESC
            LIMIT :lim OFFSET :off
            """
        ),
        {**params, "lim": effective_limit, "off": offset},
    )
    rows = rows_res.mappings().all()

    return {
        "trades": [_row_to_dict(dict(r)) for r in rows],
        "total": total,
        "page": page if limit is None else 1,
        "page_size": effective_limit,
        "pages": (
            (total + effective_limit - 1) // effective_limit
            if effective_limit
            else 1
        ),
        "only_filled": only_filled,
    }


@router.get("/trades/stats")
async def get_trade_stats(
    since_days: Optional[int] = Query(None, ge=1, le=365),
    strategy: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return aggregate statistics split into real vs phantom.

    Real trades = on-chain verified (have fill_price or tx_hash).
    Phantom trades = gtc_resting/gtc with no fill, status='PHANTOM'.

    The top-level fields (total_trades, wins, etc.) reflect REAL trades only.
    Phantom stats are in a nested `phantom` object for comparison.
    """
    where: list[str] = ["pnl_usd IS NOT NULL"]
    params: dict[str, Any] = {}

    if since_days is not None:
        where.append("created_at >= NOW() - make_interval(days => :since_days)")
        params["since_days"] = since_days
    if strategy:
        where.append("(strategy = :strategy OR strategy_id = :strategy)")
        params["strategy"] = strategy

    where_sql = " AND ".join(where)

    row = await session.execute(
        text(f"""
            SELECT
                COUNT(*) FILTER (WHERE status != 'PHANTOM' OR status IS NULL) AS real_total,
                COUNT(*) FILTER (WHERE status != 'PHANTOM' OR status IS NULL) FILTER (WHERE outcome = 'WIN') AS real_wins,
                SUM(CASE WHEN (status != 'PHANTOM' OR status IS NULL) THEN pnl_usd ELSE 0 END) AS real_pnl,
                AVG(CASE WHEN (status != 'PHANTOM' OR status IS NULL) THEN pnl_usd END) AS real_avg_pnl,
                MAX(CASE WHEN (status != 'PHANTOM' OR status IS NULL) THEN pnl_usd END) AS real_best,
                MIN(CASE WHEN (status != 'PHANTOM' OR status IS NULL) THEN pnl_usd END) AS real_worst,
                COUNT(*) FILTER (WHERE status = 'PHANTOM') AS phantom_total,
                COUNT(*) FILTER (WHERE status = 'PHANTOM') FILTER (WHERE outcome = 'WIN') AS phantom_wins,
                SUM(CASE WHEN status = 'PHANTOM' THEN pnl_usd ELSE 0 END) AS phantom_pnl
            FROM trades
            WHERE {where_sql}
        """),
        params,
    )
    r = row.mappings().first()

    real_total = int(r["real_total"] or 0)
    real_wins = int(r["real_wins"] or 0)
    phantom_total = int(r["phantom_total"] or 0)
    phantom_wins = int(r["phantom_wins"] or 0)

    return {
        # Top-level = real trades only
        "total_trades": real_total,
        "wins": real_wins,
        "losses": real_total - real_wins,
        "win_rate": real_wins / real_total if real_total > 0 else 0.0,
        "total_pnl": float(r["real_pnl"] or 0),
        "avg_pnl": float(r["real_avg_pnl"] or 0),
        "best_trade": float(r["real_best"] or 0),
        "worst_trade": float(r["real_worst"] or 0),
        # Phantom comparison — "what would have happened"
        "phantom": {
            "total": phantom_total,
            "wins": phantom_wins,
            "losses": phantom_total - phantom_wins,
            "win_rate": phantom_wins / phantom_total if phantom_total > 0 else 0.0,
            "total_pnl": float(r["phantom_pnl"] or 0),
        },
    }


@router.get("/trades/{trade_id}")
async def get_trade(
    trade_id: int,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Return a single trade by database ID."""
    result = await session.execute(
        text("SELECT * FROM trades WHERE id = :id"), {"id": trade_id}
    )
    row = result.mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="Trade not found")

    return _row_to_dict(dict(row))
