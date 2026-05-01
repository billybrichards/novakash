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

from fastapi import APIRouter, Query

router = APIRouter()


@router.get("/strategy-comparison")
async def get_strategy_comparison(
    strategy_id: str | None = Query(default=None),
    window_period: str = Query(default="24h", regex="^(1h|15h|24h|7d|30d)$"),
    t_band: str = Query(default="all"),
    direction_filter: str = Query(default="all", regex="^(all|UP|DOWN)$"),
    regime_filter: str = Query(default="all"),
    snapshot_at: str = Query(default="latest"),
):
    """Filtered slice of the latest (or specified) snapshot.

    NOT IMPLEMENTED — design skeleton.
    """
    raise NotImplementedError("design skeleton — see docs/architecture/")


@router.get("/strategy-comparison/leaderboard")
async def get_leaderboard(
    window_period: str = Query(default="15h", regex="^(1h|15h|24h|7d|30d)$"),
    top: int = Query(default=10, ge=1, le=50),
    sort: str = Query(default="real_net_pnl_usd"),
):
    """Top-N strategies by chosen metric in chosen window.

    NOT IMPLEMENTED — design skeleton.
    """
    raise NotImplementedError("design skeleton — see docs/architecture/")


@router.get("/strategy-comparison/snapshot/latest")
async def get_latest_snapshot_meta():
    """Snapshot freshness probe for the FE header.

    NOT IMPLEMENTED — design skeleton.
    """
    raise NotImplementedError("design skeleton — see docs/architecture/")


@router.get("/strategy-comparison/history")
async def get_strategy_history(
    strategy_id: str = Query(...),
    hours: int = Query(default=24, ge=1, le=720),
):
    """Per-strategy time-series (one point per snapshot) for sparklines.

    NOT IMPLEMENTED — design skeleton.
    """
    raise NotImplementedError("design skeleton — see docs/architecture/")
