"""
Strategy Decisions API

Endpoints for querying strategy_decisions table (15m + 5m).
"""

from fastapi import APIRouter, Query
from typing import Optional
import structlog

from db.session import get_db

log = structlog.get_logger(__name__)
router = APIRouter()


@router.get("/strategy-decisions/15m")
async def get_fifteen_min_decisions(
    limit: int = Query(20, ge=1, le=100),
    asset: str = Query("BTC"),
):
    """
    Get recent 15m strategy decisions grouped by window.

    Returns:
    {
      "windows": [
        {
          "window_ts": 1234567890,
          "decisions": [
            {
              "strategy_id": "v15m_down_only",
              "action": "TRADE",
              "direction": "DOWN",
              "mode": "GHOST",
              "skip_reason": null,
              ...
            }
          ],
          "outcome": "UP"  // if resolved, else null
        }
      ]
    }
    """
    db = get_db()
    if not db:
        return {"windows": []}

    try:
        # Get unique windows
        windows_query = """
            SELECT DISTINCT window_ts
            FROM strategy_decisions
            WHERE timeframe = '15m' AND asset = $1
            ORDER BY window_ts DESC
            LIMIT $2
        """
        window_rows = await db.fetch_all(windows_query, asset, limit)

        windows = []
        for win_row in window_rows:
            window_ts = win_row["window_ts"]

            # Get all decisions for this window
            decisions_query = """
                SELECT 
                    strategy_id, strategy_version, mode,
                    action, direction, confidence, confidence_score,
                    entry_cap, collateral_pct, entry_reason, skip_reason,
                    executed, order_id, fill_price, fill_size,
                    eval_offset, evaluated_at
                FROM strategy_decisions
                WHERE timeframe = '15m' AND asset = $1 AND window_ts = $2
                ORDER BY strategy_id
            """
            decision_rows = await db.fetch_all(decisions_query, asset, window_ts)

            decisions = [
                {
                    "strategy_id": row["strategy_id"],
                    "strategy_version": row["strategy_version"],
                    "mode": row["mode"],
                    "action": row["action"],
                    "direction": row["direction"],
                    "confidence": row["confidence"],
                    "confidence_score": row["confidence_score"],
                    "entry_cap": row["entry_cap"],
                    "collateral_pct": row["collateral_pct"],
                    "entry_reason": row["entry_reason"],
                    "skip_reason": row["skip_reason"],
                    "executed": row["executed"],
                    "order_id": row["order_id"],
                    "fill_price": row["fill_price"],
                    "fill_size": row["fill_size"],
                    "eval_offset": row["eval_offset"],
                    "evaluated_at": row["evaluated_at"].isoformat()
                    if row["evaluated_at"]
                    else None,
                    "hypothetical_outcome": None,  # TODO: backfill from oracle data
                }
                for row in decision_rows
            ]

            # TODO: resolve outcome from oracle data (window_snapshots or trades)
            outcome = None

            windows.append(
                {
                    "window_ts": window_ts,
                    "decisions": decisions,
                    "outcome": outcome,
                }
            )

        return {"windows": windows}

    except Exception as exc:
        log.error("strategy_decisions.15m_query_error", error=str(exc))
        return {"windows": []}


@router.get("/strategy-configs/{strategy_id}")
async def get_strategy_config(strategy_id: str):
    """
    Read YAML config for a strategy by ID.

    Returns parsed YAML as JSON.
    """
    import os
    import yaml

    config_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "engine",
        "strategies",
        "configs",
    )
    yaml_path = os.path.join(config_dir, f"{strategy_id}.yaml")

    if not os.path.exists(yaml_path):
        return {"error": f"Config not found: {strategy_id}"}

    try:
        with open(yaml_path, "r") as f:
            config = yaml.safe_load(f)
        return config
    except Exception as exc:
        log.error(
            "strategy_configs.read_error", strategy_id=strategy_id, error=str(exc)
        )
        return {"error": str(exc)}
