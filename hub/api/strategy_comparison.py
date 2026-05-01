"""Hub API: /api/strategy-comparison endpoints.

Read-only — reads from the persistent `strategy_comparison` table populated
by the engine scheduler. NO compute on the hot path.

See docs/architecture/2026-05-01-strategy-comparison-system.md.

Backward compat: `/api/v58/strategy-comparison` (existing route in
`hub/api/v58_monitor.py:3954`) will be shimmed to read from this same table
in a follow-up PR — preserving the old response shape so existing FE pages
(StrategyCommand, StrategyLab, Evaluate, StrategyConfigs) keep working.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.middleware import get_current_user
from db.database import get_session

log = structlog.get_logger(__name__)
router = APIRouter()


def _row_to_dict(r) -> dict[str, Any]:
    keys = [
        "snapshot_at", "strategy_id", "asset", "timeframe",
        "window_period", "t_band", "direction_filter", "regime_filter",
        "n_fires", "n_wins", "n_losses", "n_pending",
        "wr_pct", "wilson_low", "wilson_high",
        "avg_fill", "median_fill", "avg_stake_usd",
        "real_net_pnl_usd", "real_pnl_per_fire", "daily_run_rate_usd",
    ]
    out: dict[str, Any] = {}
    for k in keys:
        v = r[k]
        if isinstance(v, datetime):
            v = v.isoformat()
        elif hasattr(v, "__float__"):
            v = float(v) if v is not None else None
        out[k] = v
    return out


def _latest_snapshot_at(rows) -> Optional[str]:
    for r in rows:
        ts = r["snapshot_at"]
        if ts is not None:
            return ts.isoformat() if isinstance(ts, datetime) else str(ts)
    return None


@router.get("/strategy-comparison")
async def get_strategy_comparison(
    strategy_id: str | None = Query(default=None),
    window_period: str = Query(default="24h", pattern="^(1h|15h|24h|7d|30d)$"),
    t_band: str = Query(default="all"),
    direction_filter: str = Query(default="all", pattern="^(all|UP|DOWN)$"),
    regime_filter: str = Query(default="all"),
    snapshot_at: str = Query(default="latest"),
    db: AsyncSession = Depends(get_session),
    _user=Depends(get_current_user),
):
    """Filtered slice of the latest (or specified) snapshot."""
    try:
        if snapshot_at == "latest":
            ts_clause = """
                AND sc.snapshot_at = (
                    SELECT MAX(snapshot_at) FROM strategy_comparison
                )
            """
            params: dict = {
                "window_period": window_period,
                "t_band": t_band,
                "direction_filter": direction_filter,
                "regime_filter": regime_filter,
            }
        else:
            ts_clause = "AND sc.snapshot_at = :snapshot_at"
            params = {
                "window_period": window_period,
                "t_band": t_band,
                "direction_filter": direction_filter,
                "regime_filter": regime_filter,
                "snapshot_at": snapshot_at,
            }

        strategy_clause = ""
        if strategy_id:
            strategy_clause = "AND sc.strategy_id = :strategy_id"
            params["strategy_id"] = strategy_id

        sql = text(f"""
            SELECT *
            FROM strategy_comparison sc
            WHERE sc.window_period    = :window_period
              AND sc.t_band           = :t_band
              AND sc.direction_filter = :direction_filter
              AND sc.regime_filter    = :regime_filter
              {ts_clause}
              {strategy_clause}
            ORDER BY sc.strategy_id
        """)
        result = (await db.execute(sql, params)).mappings().all()
        rows = [_row_to_dict(r) for r in result]
        return {
            "rows": rows,
            "snapshot_at": _latest_snapshot_at(result),
        }
    except Exception as exc:
        log.warning("strategy_comparison.get_failed", error=str(exc)[:200])
        return {"rows": [], "snapshot_at": None, "error": str(exc)[:200]}


@router.get("/strategy-comparison/leaderboard")
async def get_leaderboard(
    window_period: str = Query(default="15h", pattern="^(1h|15h|24h|7d|30d)$"),
    top: int = Query(default=10, ge=1, le=50),
    sort: str = Query(default="real_net_pnl_usd"),
    db: AsyncSession = Depends(get_session),
    _user=Depends(get_current_user),
):
    """Top-N strategies by chosen metric in chosen window."""
    _SAFE_SORT_COLS = {
        "real_net_pnl_usd", "wr_pct", "n_fires", "real_pnl_per_fire",
        "daily_run_rate_usd", "wilson_low",
    }
    sort_col = sort if sort in _SAFE_SORT_COLS else "real_net_pnl_usd"
    try:
        sql = text(f"""
            SELECT *
            FROM v_strategy_comparison_latest
            WHERE window_period    = :window_period
              AND t_band           = 'all'
              AND direction_filter = 'all'
              AND regime_filter    = 'all'
            ORDER BY {sort_col} DESC NULLS LAST
            LIMIT :top
        """)
        result = (
            await db.execute(sql, {"window_period": window_period, "top": top})
        ).mappings().all()
        return {"rows": [_row_to_dict(r) for r in result]}
    except Exception as exc:
        log.warning("strategy_comparison.leaderboard_failed", error=str(exc)[:200])
        return {"rows": [], "error": str(exc)[:200]}


@router.get("/strategy-comparison/snapshot/latest")
async def get_latest_snapshot_meta(
    db: AsyncSession = Depends(get_session),
    _user=Depends(get_current_user),
):
    """Snapshot freshness probe for the FE header."""
    try:
        sql = text("""
            SELECT snapshot_at, COUNT(*) AS n_strategies
            FROM strategy_comparison
            WHERE snapshot_at = (SELECT MAX(snapshot_at) FROM strategy_comparison)
            GROUP BY snapshot_at
        """)
        row = (await db.execute(sql)).mappings().first()
        if row is None:
            return {"snapshot_at": None, "n_strategies": 0}
        return {
            "snapshot_at": row["snapshot_at"].isoformat() if row["snapshot_at"] else None,
            "n_strategies": int(row["n_strategies"]),
        }
    except Exception as exc:
        log.warning("strategy_comparison.snapshot_meta_failed", error=str(exc)[:200])
        return {"snapshot_at": None, "n_strategies": 0, "error": str(exc)[:200]}


@router.get("/strategy-comparison/history")
async def get_strategy_history(
    strategy_id: str = Query(...),
    hours: int = Query(default=24, ge=1, le=720),
    db: AsyncSession = Depends(get_session),
    _user=Depends(get_current_user),
):
    """Per-strategy time-series (one point per snapshot) for sparklines."""
    try:
        sql = text("""
            SELECT snapshot_at, real_net_pnl_usd, wr_pct, n_fires
            FROM strategy_comparison
            WHERE strategy_id    = :strategy_id
              AND t_band         = 'all'
              AND direction_filter = 'all'
              AND regime_filter  = 'all'
              AND window_period  = '24h'
              AND snapshot_at    >= NOW() - (:hours * INTERVAL '1 hour')
            ORDER BY snapshot_at ASC
        """)
        result = (
            await db.execute(sql, {"strategy_id": strategy_id, "hours": hours})
        ).mappings().all()
        points = [
            {
                "snapshot_at": r["snapshot_at"].isoformat() if r["snapshot_at"] else None,
                "real_net_pnl_usd": float(r["real_net_pnl_usd"]) if r["real_net_pnl_usd"] is not None else None,
                "wr_pct": float(r["wr_pct"]) if r["wr_pct"] is not None else None,
                "n_fires": int(r["n_fires"]) if r["n_fires"] is not None else 0,
            }
            for r in result
        ]
        return {"points": points, "strategy_id": strategy_id}
    except Exception as exc:
        log.warning("strategy_comparison.history_failed", error=str(exc)[:200])
        return {"points": [], "strategy_id": strategy_id, "error": str(exc)[:200]}
