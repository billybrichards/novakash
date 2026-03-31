"""
hub/api/paper.py — Paper Trading API endpoints.

All endpoints are unauthenticated (dev/paper mode).
Uses SQLAlchemy async sessions for DB access.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import get_session

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/paper", tags=["paper"])

# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _fetch_all(session: AsyncSession, query: str) -> list[dict]:
    """Execute raw SQL, return list of dicts."""
    try:
        result = await session.execute(text(query))
        rows = result.mappings().all()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.warning("paper.query_failed", query=query[:80], exc=str(exc))
        return []


async def _fetch_one(session: AsyncSession, query: str) -> dict | None:
    """Execute raw SQL, return single dict or None."""
    try:
        result = await session.execute(text(query))
        row = result.mappings().first()
        return dict(row) if row else None
    except Exception as exc:
        log.warning("paper.fetchrow_failed", query=query[:80], exc=str(exc))
        return None


def _ts(dt) -> str | None:
    """Convert datetime to ISO string."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/status")
async def paper_status(session: AsyncSession = Depends(get_session)):
    """Engine status, VPIN, cascade state, feeds, balance."""
    row = await _fetch_one(session, """
        SELECT
            engine_status,
            current_balance,
            peak_balance,
            current_drawdown_pct,
            binance_connected,
            coinglass_connected,
            chainlink_connected,
            polymarket_connected,
            opinion_connected,
            last_vpin,
            last_cascade_state,
            active_positions,
            last_heartbeat,
            config
        FROM system_state
        WHERE id = 1
    """)

    if not row:
        return {
            "engine_status": "UNKNOWN",
            "current_balance": 0,
            "peak_balance": 0,
            "current_drawdown_pct": 0,
            "binance_connected": False,
            "coinglass_connected": False,
            "chainlink_connected": False,
            "polymarket_connected": False,
            "opinion_connected": False,
            "last_vpin": None,
            "last_cascade_state": "IDLE",
            "regime": "UNKNOWN",
            "active_positions": 0,
            "last_heartbeat": None,
            "uptime_seconds": 0,
        }

    hb = row.get("last_heartbeat")
    uptime = 0
    if hb:
        uptime = max(0, int((datetime.now(timezone.utc) - hb.replace(tzinfo=timezone.utc if hb.tzinfo is None else hb.tzinfo)).total_seconds()))

    return {
        "engine_status": row.get("engine_status", "UNKNOWN"),
        "current_balance": float(row.get("current_balance") or 0),
        "peak_balance": float(row.get("peak_balance") or 0),
        "current_drawdown_pct": float(row.get("current_drawdown_pct") or 0),
        "binance_connected": bool(row.get("binance_connected")),
        "coinglass_connected": bool(row.get("coinglass_connected")),
        "chainlink_connected": bool(row.get("chainlink_connected")),
        "polymarket_connected": bool(row.get("polymarket_connected")),
        "opinion_connected": bool(row.get("opinion_connected")),
        "last_vpin": float(row["last_vpin"]) if row.get("last_vpin") is not None else None,
        "last_cascade_state": row.get("last_cascade_state") or "IDLE",
        "regime": (row.get("config") or {}).get("regime", "UNKNOWN") if isinstance(row.get("config"), dict) else "UNKNOWN",
        "active_positions": int(row.get("active_positions") or 0),
        "last_heartbeat": _ts(hb),
        "uptime_seconds": uptime,
    }


@router.get("/positions")
async def paper_positions(session: AsyncSession = Depends(get_session)):
    """Currently open paper orders."""
    rows = await _fetch_all(session, """
        SELECT id, strategy, direction, venue, entry_price, stake_usd,
               vpin_at_entry, created_at
        FROM trades
        WHERE status = 'OPEN'
        ORDER BY created_at DESC
    """)

    return [
        {
            "id": r["id"],
            "strategy": r.get("strategy", ""),
            "direction": r.get("direction", ""),
            "venue": r.get("venue", ""),
            "entry_price": str(r.get("entry_price", "")),
            "stake_usd": float(r.get("stake_usd") or 0),
            "vpin_at_entry": float(r["vpin_at_entry"]) if r.get("vpin_at_entry") is not None else None,
            "created_at": _ts(r.get("created_at")),
            "age_seconds": int((datetime.now(timezone.utc) - r["created_at"].replace(
                tzinfo=timezone.utc if r["created_at"].tzinfo is None else r["created_at"].tzinfo
            )).total_seconds()) if r.get("created_at") else 0,
        }
        for r in rows
    ]


@router.get("/trades")
async def paper_trades(session: AsyncSession = Depends(get_session)):
    """Last 50 resolved paper trades."""
    rows = await _fetch_all(session, """
        SELECT id, strategy, direction, venue, entry_price, stake_usd,
               pnl_usd, status, vpin_at_entry, created_at, resolved_at
        FROM trades
        WHERE outcome IN ('WIN', 'LOSS')
        ORDER BY resolved_at DESC NULLS LAST
        LIMIT 50
    """)

    return [
        {
            "id": r["id"],
            "strategy": r.get("strategy", ""),
            "direction": r.get("direction", ""),
            "venue": r.get("venue", ""),
            "entry_price": str(r.get("entry_price", "")),
            "stake_usd": float(r.get("stake_usd") or 0),
            "pnl_usd": float(r.get("pnl_usd") or 0),
            "outcome": r.get("outcome", ""),
            "vpin_at_entry": float(r["vpin_at_entry"]) if r.get("vpin_at_entry") is not None else None,
            "created_at": _ts(r.get("created_at")),
            "resolved_at": _ts(r.get("resolved_at")),
        }
        for r in rows
    ]


@router.get("/stats")
async def paper_stats(session: AsyncSession = Depends(get_session)):
    """Aggregated paper trading statistics."""
    row = await _fetch_one(session, """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE outcome = 'WIN') AS wins,
            COUNT(*) FILTER (WHERE outcome = 'LOSS') AS losses,
            COALESCE(SUM(pnl_usd), 0) AS total_pnl,
            COALESCE(AVG(pnl_usd), 0) AS avg_pnl,
            COALESCE(MAX(pnl_usd), 0) AS best_trade,
            COALESCE(MIN(pnl_usd), 0) AS worst_trade,
            COALESCE(STDDEV(pnl_usd), 0) AS stddev_pnl,
            COALESCE(AVG(EXTRACT(EPOCH FROM (resolved_at - created_at))), 0) AS avg_duration_s
        FROM trades
        WHERE outcome IN ('WIN', 'LOSS')
    """)

    if not row or row["total"] == 0:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0, "total_pnl": 0, "avg_pnl": 0,
            "best_trade": 0, "worst_trade": 0, "sharpe": 0,
            "max_drawdown_pct": 0, "avg_duration_seconds": 0,
        }

    total = int(row["total"])
    wins = int(row["wins"])
    stddev = float(row["stddev_pnl"])
    avg = float(row["avg_pnl"])
    sharpe = (avg / stddev) if stddev > 0 else 0

    # Get max drawdown from system_state
    state = await _fetch_one(session, "SELECT current_drawdown_pct FROM system_state WHERE id = 1")
    dd = float(state["current_drawdown_pct"]) if state and state.get("current_drawdown_pct") else 0

    return {
        "total_trades": total,
        "wins": wins,
        "losses": int(row["losses"]),
        "win_rate": round(wins / total, 4) if total > 0 else 0,
        "total_pnl": round(float(row["total_pnl"]), 2),
        "avg_pnl": round(avg, 2),
        "best_trade": round(float(row["best_trade"]), 2),
        "worst_trade": round(float(row["worst_trade"]), 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(dd, 4),
        "avg_duration_seconds": round(float(row["avg_duration_s"]), 0),
    }


@router.get("/strategy-breakdown")
async def paper_strategy_breakdown(session: AsyncSession = Depends(get_session)):
    """Per-strategy stats."""
    rows = await _fetch_all(session, """
        SELECT
            strategy,
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE outcome = 'WIN') AS wins,
            COALESCE(SUM(pnl_usd), 0) AS total_pnl,
            COALESCE(AVG(pnl_usd), 0) AS avg_pnl,
            COALESCE(MAX(pnl_usd), 0) AS best,
            COALESCE(MIN(pnl_usd), 0) AS worst,
            COALESCE(AVG(EXTRACT(EPOCH FROM (resolved_at - created_at))), 0) AS avg_duration_s
        FROM trades
        WHERE outcome IN ('WIN', 'LOSS')
        GROUP BY strategy
    """)

    result = {}
    for r in rows:
        total = int(r["total"])
        result[r["strategy"]] = {
            "total": total,
            "wins": int(r["wins"]),
            "win_rate": round(int(r["wins"]) / total, 4) if total > 0 else 0,
            "total_pnl": round(float(r["total_pnl"]), 2),
            "avg_pnl": round(float(r["avg_pnl"]), 2),
            "best": round(float(r["best"]), 2),
            "worst": round(float(r["worst"]), 2),
            "avg_duration_seconds": round(float(r["avg_duration_s"]), 0),
        }

    return result


@router.get("/log")
async def paper_log(session: AsyncSession = Depends(get_session)):
    """Recent engine signals as log entries."""
    rows = await _fetch_all(session, """
        SELECT signal_type, value, metadata, created_at
        FROM signals
        ORDER BY created_at DESC
        LIMIT 200
    """)

    entries = []
    for r in reversed(rows):
        sig_type = r.get("signal_type", "unknown")
        value = r.get("value")
        meta = r.get("metadata") or {}

        if sig_type == "vpin":
            msg = f"VPIN = {float(value):.4f}" if value else "VPIN update"
            if meta.get("cascade_threshold_crossed"):
                msg += " ⚠️ CASCADE THRESHOLD"
            elif meta.get("informed_threshold_crossed"):
                msg += " ⚡ INFORMED FLOW"
            level = "warning" if meta.get("cascade_threshold_crossed") else "info"
        elif sig_type == "cascade":
            state = meta.get("state", "?")
            direction = meta.get("direction", "?")
            msg = f"Cascade: {state} direction={direction}"
            level = "error" if state == "EXHAUSTING" else "warning"
        elif sig_type == "arb_opportunity":
            spread = meta.get("spread", 0)
            msg = f"Arb opportunity: spread={spread:.4f}" if spread else "Arb detected"
            level = "success"
        elif sig_type == "trade":
            msg = f"Trade: {meta.get('strategy', '?')} {meta.get('direction', '?')} ${meta.get('stake', 0):.2f}"
            level = "success" if meta.get("outcome") == "WIN" else "error"
        else:
            msg = f"{sig_type}: {value or ''}"
            level = "info"

        entries.append({
            "timestamp": _ts(r.get("created_at")),
            "type": sig_type,
            "level": level,
            "message": msg,
        })

    return entries


@router.get("/equity")
async def paper_equity(session: AsyncSession = Depends(get_session)):
    """Cumulative P&L over trade sequence."""
    rows = await _fetch_all(session, """
        SELECT pnl_usd, resolved_at
        FROM trades
        WHERE outcome IN ('WIN', 'LOSS')
        ORDER BY resolved_at ASC NULLS LAST
    """)

    if not rows:
        return []

    cumulative = 0.0
    result = []
    for i, r in enumerate(rows):
        cumulative += float(r.get("pnl_usd") or 0)
        result.append({
            "trade_num": i + 1,
            "cumulative_pnl": round(cumulative, 2),
            "timestamp": _ts(r.get("resolved_at")),
        })

    return result
