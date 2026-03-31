"""
hub/api/paper.py — Paper Trading API endpoints.

All endpoints are unauthenticated (dev/paper mode).
Queries the same DB as the trading engine:
  - trades table
  - signals table
  - system_state table
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter
from asyncpg.exceptions import UndefinedTableError

from db.database import get_pool

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/paper", tags=["paper"])

# ─── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_STATUS = {
    "engine_status": "UNKNOWN",
    "current_balance": 10000.0,
    "peak_balance": 10000.0,
    "current_drawdown_pct": 0.0,
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


async def _safe_fetch(pool, query: str, *args) -> list[dict]:
    """Execute a query and return rows as dicts. Returns [] on any error."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
            return [dict(r) for r in rows]
    except Exception as exc:
        log.warning("paper.query_failed", query=query[:80], exc=str(exc))
        return []


async def _safe_fetchrow(pool, query: str, *args) -> dict | None:
    """Execute a query and return one row as a dict. Returns None on error."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, *args)
            return dict(row) if row else None
    except Exception as exc:
        log.warning("paper.fetchrow_failed", query=query[:80], exc=str(exc))
        return None


def _to_float(val, default=None):
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


# ─── /paper/status ───────────────────────────────────────────────────────────


@router.get("/status")
async def get_status() -> dict[str, Any]:
    """
    Returns engine status, connected feeds, VPIN, cascade state, regime,
    uptime estimate, and paper balance from system_state.
    """
    pool = await get_pool()
    row = await _safe_fetchrow(
        pool,
        """
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
            last_heartbeat
        FROM system_state
        LIMIT 1
        """,
    )

    if not row:
        return DEFAULT_STATUS

    # Estimate uptime from last_heartbeat if available
    uptime_seconds = 0
    if row.get("last_heartbeat"):
        try:
            hb = row["last_heartbeat"]
            if isinstance(hb, datetime):
                delta = datetime.now(timezone.utc) - hb.replace(tzinfo=timezone.utc) if hb.tzinfo is None else datetime.now(timezone.utc) - hb
                uptime_seconds = max(0, int(delta.total_seconds()))
        except Exception:
            pass

    return {
        "engine_status": row.get("engine_status") or "UNKNOWN",
        "current_balance": _to_float(row.get("current_balance"), 10000.0),
        "peak_balance": _to_float(row.get("peak_balance"), 10000.0),
        "current_drawdown_pct": _to_float(row.get("current_drawdown_pct"), 0.0),
        "binance_connected": bool(row.get("binance_connected")),
        "coinglass_connected": bool(row.get("coinglass_connected")),
        "chainlink_connected": bool(row.get("chainlink_connected")),
        "polymarket_connected": bool(row.get("polymarket_connected")),
        "opinion_connected": bool(row.get("opinion_connected")),
        "last_vpin": _to_float(row.get("last_vpin")),
        "last_cascade_state": row.get("last_cascade_state") or "IDLE",
        "regime": row.get("regime") or "UNKNOWN",
        "active_positions": int(row.get("active_positions") or 0),
        "last_heartbeat": _iso(row.get("last_heartbeat")),
        "uptime_seconds": uptime_seconds,
    }


# ─── /paper/positions ────────────────────────────────────────────────────────


@router.get("/positions")
async def get_positions() -> list[dict[str, Any]]:
    """
    Returns currently open paper orders (status = 'OPEN').
    """
    pool = await get_pool()
    rows = await _safe_fetch(
        pool,
        """
        SELECT
            id,
            strategy,
            direction,
            venue,
            entry_price,
            stake_usd,
            status,
            vpin_at_entry,
            created_at
        FROM trades
        WHERE status = 'OPEN'
        ORDER BY created_at DESC
        """,
    )

    return [
        {
            "id": str(r.get("id", "")),
            "strategy": r.get("strategy"),
            "direction": r.get("direction"),
            "venue": r.get("venue"),
            "entry_price": _to_float(r.get("entry_price")),
            "stake_usd": _to_float(r.get("stake_usd")),
            "status": r.get("status"),
            "vpin_at_entry": _to_float(r.get("vpin_at_entry")),
            "created_at": _iso(r.get("created_at")),
        }
        for r in rows
    ]


# ─── /paper/trades ───────────────────────────────────────────────────────────


@router.get("/trades")
async def get_trades() -> list[dict[str, Any]]:
    """
    Returns last 50 resolved paper trades (WIN or LOSS).
    """
    pool = await get_pool()
    rows = await _safe_fetch(
        pool,
        """
        SELECT
            id,
            strategy,
            direction,
            venue,
            entry_price,
            stake_usd,
            pnl_usd,
            status,
            vpin_at_entry,
            created_at,
            resolved_at
        FROM trades
        WHERE status IN ('WIN', 'LOSS')
        ORDER BY created_at DESC
        LIMIT 50
        """,
    )

    return [
        {
            "id": str(r.get("id", "")),
            "strategy": r.get("strategy"),
            "direction": r.get("direction"),
            "venue": r.get("venue"),
            "entry_price": _to_float(r.get("entry_price")),
            "stake_usd": _to_float(r.get("stake_usd")),
            "pnl_usd": _to_float(r.get("pnl_usd"), 0.0),
            "status": r.get("status"),
            "vpin_at_entry": _to_float(r.get("vpin_at_entry")),
            "created_at": _iso(r.get("created_at")),
            "resolved_at": _iso(r.get("resolved_at")),
        }
        for r in rows
    ]


# ─── /paper/stats ────────────────────────────────────────────────────────────


@router.get("/stats")
async def get_stats() -> dict[str, Any]:
    """
    Computed stats: win rate, total P&L, avg trade, sharpe estimate, max drawdown.
    All computed from the trades table.
    """
    pool = await get_pool()

    # Aggregate query
    agg = await _safe_fetchrow(
        pool,
        """
        SELECT
            COUNT(*) FILTER (WHERE status IN ('WIN', 'LOSS'))                   AS total_trades,
            COUNT(*) FILTER (WHERE status = 'WIN')                              AS wins,
            COALESCE(SUM(pnl_usd) FILTER (WHERE status IN ('WIN','LOSS')), 0)  AS total_pnl,
            COALESCE(AVG(pnl_usd) FILTER (WHERE status IN ('WIN','LOSS')), 0)  AS avg_pnl,
            COALESCE(MAX(pnl_usd) FILTER (WHERE status IN ('WIN','LOSS')), 0)  AS best_trade,
            COALESCE(MIN(pnl_usd) FILTER (WHERE status IN ('WIN','LOSS')), 0)  AS worst_trade,
            COALESCE(STDDEV(pnl_usd) FILTER (WHERE status IN ('WIN','LOSS')), 0) AS pnl_stddev,
            COALESCE(
                AVG(
                    EXTRACT(EPOCH FROM (resolved_at - created_at))
                ) FILTER (WHERE status IN ('WIN','LOSS') AND resolved_at IS NOT NULL),
                0
            )                                                                   AS avg_duration_seconds
        FROM trades
        """,
    )

    # Get current balance for drawdown
    state = await _safe_fetchrow(
        pool,
        "SELECT current_balance, peak_balance, current_drawdown_pct FROM system_state LIMIT 1",
    )

    if not agg:
        return {
            "total_trades": 0,
            "win_rate": None,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "best_trade": None,
            "worst_trade": None,
            "sharpe_ratio": None,
            "max_drawdown_pct": None,
            "avg_duration_seconds": None,
            "current_balance": 10000.0,
        }

    total = int(agg.get("total_trades") or 0)
    wins = int(agg.get("wins") or 0)
    win_rate = wins / total if total > 0 else None

    avg_pnl = _to_float(agg.get("avg_pnl"), 0.0)
    pnl_std = _to_float(agg.get("pnl_stddev"), 0.0)

    # Simple Sharpe estimate: avg_pnl / std  (annualised would need trade frequency)
    sharpe = (avg_pnl / pnl_std) if pnl_std and pnl_std > 0 else None

    current_balance = _to_float(state.get("current_balance") if state else None, 10000.0)
    max_drawdown = _to_float(state.get("current_drawdown_pct") if state else None, 0.0)

    return {
        "total_trades": total,
        "win_rate": win_rate,
        "total_pnl": _to_float(agg.get("total_pnl"), 0.0),
        "avg_pnl": avg_pnl,
        "best_trade": _to_float(agg.get("best_trade")),
        "worst_trade": _to_float(agg.get("worst_trade")),
        "sharpe_ratio": round(sharpe, 3) if sharpe is not None else None,
        "max_drawdown_pct": max_drawdown,
        "avg_duration_seconds": _to_float(agg.get("avg_duration_seconds")),
        "current_balance": current_balance,
    }


# ─── /paper/strategy-breakdown ───────────────────────────────────────────────


@router.get("/strategy-breakdown")
async def get_strategy_breakdown() -> dict[str, Any]:
    """
    Per-strategy stats, grouped by strategy name.
    Returns arb and vpin_cascade buckets with equity curves.
    """
    pool = await get_pool()

    rows = await _safe_fetch(
        pool,
        """
        SELECT
            strategy,
            COUNT(*) FILTER (WHERE status IN ('WIN', 'LOSS'))                   AS trade_count,
            COUNT(*) FILTER (WHERE status = 'WIN')                              AS wins,
            COALESCE(SUM(pnl_usd) FILTER (WHERE status IN ('WIN','LOSS')), 0)  AS total_pnl,
            COALESCE(MAX(pnl_usd) FILTER (WHERE status IN ('WIN','LOSS')), 0)  AS best_trade,
            COALESCE(MIN(pnl_usd) FILTER (WHERE status IN ('WIN','LOSS')), 0)  AS worst_trade,
            COALESCE(AVG(vpin_at_entry) FILTER (WHERE status IN ('WIN','LOSS')), 0) AS avg_vpin_entry,
            COALESCE(
                AVG(EXTRACT(EPOCH FROM (resolved_at - created_at)))
                FILTER (WHERE status IN ('WIN','LOSS') AND resolved_at IS NOT NULL),
                0
            )                                                                   AS avg_hold_seconds
        FROM trades
        WHERE status IN ('WIN', 'LOSS')
        GROUP BY strategy
        """,
    )

    # Build equity curve per strategy (running sum of pnl_usd ordered by created_at)
    equity_rows = await _safe_fetch(
        pool,
        """
        SELECT strategy, pnl_usd, created_at
        FROM trades
        WHERE status IN ('WIN', 'LOSS')
        ORDER BY strategy, created_at ASC
        """,
    )

    # Group equity curves
    curves: dict[str, list[float]] = {}
    running: dict[str, float] = {}
    for r in equity_rows:
        strat = r.get("strategy") or "unknown"
        pnl = _to_float(r.get("pnl_usd"), 0.0)
        running[strat] = running.get(strat, 0.0) + pnl
        curves.setdefault(strat, []).append(round(running[strat], 4))

    def _build(strategy_key: str, row: dict | None) -> dict:
        if not row:
            return {
                "trade_count": 0,
                "win_rate": None,
                "total_pnl": 0.0,
                "best_trade": None,
                "worst_trade": None,
                "avg_vpin_entry": None,
                "avg_hold_seconds": None,
                "avg_spread": None,
                "equity_curve": curves.get(strategy_key, []),
            }
        total = int(row.get("trade_count") or 0)
        wins = int(row.get("wins") or 0)
        return {
            "trade_count": total,
            "win_rate": wins / total if total > 0 else None,
            "total_pnl": _to_float(row.get("total_pnl"), 0.0),
            "best_trade": _to_float(row.get("best_trade")),
            "worst_trade": _to_float(row.get("worst_trade")),
            "avg_vpin_entry": _to_float(row.get("avg_vpin_entry")),
            "avg_hold_seconds": _to_float(row.get("avg_hold_seconds")),
            "avg_spread": None,  # Could compute from arb-specific data if stored
            "equity_curve": curves.get(strategy_key, []),
        }

    # Map rows to strategy keys
    row_map: dict[str, dict] = {}
    for r in rows:
        strat = (r.get("strategy") or "").lower()
        row_map[strat] = r

    # Identify arb vs vpin_cascade keys
    arb_key = next((k for k in row_map if "arb" in k), None)
    vpin_key = next((k for k in row_map if "vpin" in k or "cascade" in k), None)

    return {
        "arb": _build(arb_key or "arb", row_map.get(arb_key) if arb_key else None),
        "vpin_cascade": _build(vpin_key or "vpin_cascade", row_map.get(vpin_key) if vpin_key else None),
    }


# ─── /paper/log ──────────────────────────────────────────────────────────────


@router.get("/log")
async def get_log() -> list[dict[str, Any]]:
    """
    Recent engine events/signals from the signals table, formatted as log entries.
    Returns up to 200 entries ordered newest-first.
    """
    pool = await get_pool()
    rows = await _safe_fetch(
        pool,
        """
        SELECT
            id,
            signal_type,
            metadata,
            value,
            created_at
        FROM signals
        ORDER BY created_at DESC
        LIMIT 200
        """,
    )

    entries = []
    for r in rows:
        signal_type = (r.get("signal_type") or "SYSTEM").upper()
        meta = r.get("metadata") or {}

        # Build a human-readable message
        message = _format_log_message(signal_type, meta, r.get("value"))

        entries.append({
            "id": str(r.get("id", "")),
            "type": signal_type,
            "message": message,
            "timestamp": _iso(r.get("created_at")),
            "value": _to_float(r.get("value")),
        })

    return entries


def _format_log_message(signal_type: str, meta: dict, value) -> str:
    """Convert a signal record to a readable log line."""
    if not meta:
        if value is not None:
            return f"{signal_type}: {value}"
        return signal_type

    # Common fields
    parts = []
    for key in ("strategy", "direction", "venue", "price", "pnl", "vpin", "state", "message", "detail"):
        v = meta.get(key)
        if v is not None:
            parts.append(f"{key}={v}")

    if parts:
        return f"{signal_type} — {', '.join(parts)}"

    # Fallback: dump first few keys
    short = {k: v for i, (k, v) in enumerate(meta.items()) if i < 4}
    return f"{signal_type} — {short}"


# ─── /paper/equity ───────────────────────────────────────────────────────────


@router.get("/equity")
async def get_equity() -> list[dict[str, Any]]:
    """
    Cumulative P&L over the trade sequence, ordered by trade time.
    Returns [{trade_num, cumulative_pnl, pnl_usd, created_at}]
    """
    pool = await get_pool()
    rows = await _safe_fetch(
        pool,
        """
        SELECT
            id,
            pnl_usd,
            created_at
        FROM trades
        WHERE status IN ('WIN', 'LOSS')
        ORDER BY created_at ASC
        """,
    )

    if not rows:
        return []

    cumulative = 0.0
    result = []
    for i, r in enumerate(rows):
        pnl = _to_float(r.get("pnl_usd"), 0.0)
        cumulative += pnl
        result.append({
            "trade_num": i + 1,
            "cumulative_pnl": round(cumulative, 4),
            "pnl_usd": round(pnl, 4),
            "created_at": _iso(r.get("created_at")),
        })

    return result
