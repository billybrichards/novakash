"""
Gate Traces API — per-gate pass/fail heatmap over the ``gate_check_traces`` table.

Closes audit task #188 ("Dashboard: gate_check_traces panel — per-window gate
pass/fail UI"). ``gate_check_traces`` accumulates ~477K rows / 24h (one per
gate evaluation per strategy per window per offset) and was previously unused
in the UI (see hub notes #41 + #43). This endpoint exposes the table as a
matrix the FE Gate Traces page can render as a heatmap + recent-traces table.

Endpoints
---------

``GET /api/gate-traces/heatmap``
    Aggregation matrix: strategy × gate → (fired, passed, pass_pct,
    top_skip_reasons). Response is a **bare dict** (not wrapped under a
    ``strategies`` / ``gates`` / ``cells`` envelope key) — ``useApiLoader``
    only unwraps ``rows / trades / decisions / items`` arrays so the FE
    receives the raw object untouched. See ``hub/api/strategies.py`` for
    the same bare-map pattern.

``GET /api/gate-traces/recent``
    Latest N trace rows grouped by (strategy, window, offset) for the
    expandable per-window drill-down panel beneath the heatmap.

Design notes
------------

* Read-only. Every query is a SELECT against ``gate_check_traces`` joined
  with no other tables. Zero write path, zero risk of production
  behaviour change — shipping this is pure UI affordance.
* Whitelisted columns on the wire — ``observed_json`` / ``config_json``
  contain market metrics + gate params only (inspected
  ``engine/strategies/registry.py::_gate_observed_data`` +
  ``_gate_config_data``). No secrets, no wallet material. Surfaced as-is
  so the FE can drill down without a second round-trip.
* SQL aggregation lifted from ``scripts/ops/shadow_analysis.py`` style
  (pctg via ``AVG(CASE ...)``) so numbers agree with the CLI when the
  same slice is queried (per ``reference_shadow_analysis.md``).
* Interval bind is parameterised via a precomputed timestamp (not
  ``NOW() - ($1 || ' hours')::interval`` — Postgres rejects that exact
  binding form with asyncpg params sometimes), which keeps the query
  plan cacheable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session

log = structlog.get_logger(__name__)
router = APIRouter()


# ─── Config ─────────────────────────────────────────────────────────────────

_MAX_HOURS = 168  # 7 days — matches the Signal Explorer upper bound
_DEFAULT_HOURS = 24
_RECENT_DEFAULT_LIMIT = 50
_RECENT_MAX_LIMIT = 200
_TOP_SKIP_REASONS = 3
# Truncate long skip_reason strings in the aggregation so the response
# stays under a sane size even when a gate emits a diagnostic reason
# with embedded floats / thresholds.
_SKIP_REASON_MAX_LEN = 80


def _validate_hours(hours: int) -> int:
    if hours < 1 or hours > _MAX_HOURS:
        raise HTTPException(
            status_code=400,
            detail=f"hours must be between 1 and {_MAX_HOURS}",
        )
    return hours


def _validate_timeframe(tf: str) -> str:
    if tf not in ("5m", "15m", "1h"):
        raise HTTPException(
            status_code=400,
            detail="timeframe must be one of: 5m, 15m, 1h",
        )
    return tf


# ─── Endpoints ──────────────────────────────────────────────────────────────


@router.get("/gate-traces/heatmap")
async def gate_traces_heatmap(
    timeframe: str = Query(default="5m"),
    asset: str = Query(default="BTC"),
    hours: int = Query(default=_DEFAULT_HOURS),
    strategy_id: Optional[str] = Query(default=None),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Aggregate ``gate_check_traces`` into a strategy × gate matrix.

    Each cell reports how many times a gate fired for a strategy, how many
    times it passed, and the top skip reasons when it failed. The FE renders
    this as a coloured heatmap (green = high pass-rate, red = low) keyed by
    ``wrColor()`` from ``theme/tokens.js``.

    Response shape (bare dict — do NOT wrap in an envelope key that
    ``useApiLoader`` would try to unwrap as an array)::

        {
          "strategies": ["v4_fusion", "v4_up_basic", ...],
          "gates":      ["confidence", "direction", "timing", ...],
          "cells": [
            {
              "strategy": "v4_fusion",
              "gate": "confidence",
              "fired": 288,
              "passed": 201,
              "pass_pct": 69.8,
              "top_skip_reasons": [
                {"reason": "dist < 0.12", "n": 45},
                ...
              ]
            },
            ...
          ],
          "window": {
            "timeframe": "5m",
            "asset": "BTC",
            "hours": 24,
            "strategy_id": null,
            "row_count_raw": 21477,
            "earliest": "2026-04-16T08:40:00Z",
            "latest":   "2026-04-17T08:40:00Z"
          }
        }
    """
    _validate_timeframe(timeframe)
    _validate_hours(hours)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Parameter bundle — asyncpg/SQLAlchemy prefers explicit binds so the
    # plan-cache stays hot across repeat calls.
    params: dict[str, Any] = {
        "asset": asset,
        "timeframe": timeframe,
        "cutoff": cutoff,
    }
    strategy_clause = ""
    if strategy_id:
        strategy_clause = "AND strategy_id = :sid"
        params["sid"] = strategy_id

    try:
        # 1. The aggregation matrix. pass_pct is kept numeric so the FE
        # can feed it directly into wrColor(pct/100) without a cast.
        agg_sql = text(f"""
            SELECT
                strategy_id,
                gate_name,
                COUNT(*)                                                  AS fired,
                SUM(CASE WHEN passed THEN 1 ELSE 0 END)                   AS passed,
                ROUND(100.0 * AVG(CASE WHEN passed THEN 1.0 ELSE 0.0 END), 1) AS pass_pct
            FROM gate_check_traces
            WHERE asset = :asset
              AND timeframe = :timeframe
              AND evaluated_at > :cutoff
              {strategy_clause}
            GROUP BY strategy_id, gate_name
            ORDER BY strategy_id, gate_name
        """)
        agg_rows = (await session.execute(agg_sql, params)).mappings().all()

        # 2. Top-N skip reasons per (strategy, gate) — only counts failed rows.
        # ``LEFT(skip_reason, N)`` truncates runaway diagnostic strings so
        # two near-identical rows aggregate under one bucket.
        reasons_sql = text(f"""
            WITH failed AS (
                SELECT
                    strategy_id,
                    gate_name,
                    COALESCE(
                        NULLIF(TRIM(LEFT(skip_reason, {_SKIP_REASON_MAX_LEN})), ''),
                        NULLIF(TRIM(LEFT(reason,      {_SKIP_REASON_MAX_LEN})), '')
                    ) AS reason_text
                FROM gate_check_traces
                WHERE asset = :asset
                  AND timeframe = :timeframe
                  AND evaluated_at > :cutoff
                  AND passed = FALSE
                  {strategy_clause}
            ),
            ranked AS (
                SELECT
                    strategy_id,
                    gate_name,
                    reason_text,
                    COUNT(*) AS n,
                    ROW_NUMBER() OVER (
                        PARTITION BY strategy_id, gate_name
                        ORDER BY COUNT(*) DESC
                    ) AS rnk
                FROM failed
                WHERE reason_text IS NOT NULL
                GROUP BY strategy_id, gate_name, reason_text
            )
            SELECT strategy_id, gate_name, reason_text, n
            FROM ranked
            WHERE rnk <= :top_n
            ORDER BY strategy_id, gate_name, n DESC
        """)
        reason_rows = (
            await session.execute(
                reasons_sql,
                {**params, "top_n": _TOP_SKIP_REASONS},
            )
        ).mappings().all()

        # 3. Window metadata — gives the operator a sanity check on the
        # slice they're viewing (e.g. "21K rows over 24h" → rate makes sense).
        meta_sql = text(f"""
            SELECT
                COUNT(*)                       AS row_count_raw,
                MIN(evaluated_at)              AS earliest,
                MAX(evaluated_at)              AS latest
            FROM gate_check_traces
            WHERE asset = :asset
              AND timeframe = :timeframe
              AND evaluated_at > :cutoff
              {strategy_clause}
        """)
        meta_row = (await session.execute(meta_sql, params)).mappings().first()

    except Exception as exc:
        log.warning("gate_traces.heatmap_error", error=str(exc)[:200])
        # Never 500 the dashboard — the FE shows an inline error banner
        # and the rest of the page keeps working.
        return {
            "strategies": [],
            "gates": [],
            "cells": [],
            "window": {
                "timeframe": timeframe,
                "asset": asset,
                "hours": hours,
                "strategy_id": strategy_id,
                "row_count_raw": 0,
                "earliest": None,
                "latest": None,
            },
            "error": str(exc)[:200],
        }

    # Bucket skip-reasons by (strategy, gate) for O(1) lookup while
    # we assemble the cell list.
    reason_bucket: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in reason_rows:
        key = (r["strategy_id"], r["gate_name"])
        reason_bucket.setdefault(key, []).append(
            {"reason": r["reason_text"], "n": int(r["n"] or 0)}
        )

    strategies_set: set[str] = set()
    gates_set: set[str] = set()
    cells: list[dict[str, Any]] = []
    for row in agg_rows:
        sid = row["strategy_id"]
        gate = row["gate_name"]
        strategies_set.add(sid)
        gates_set.add(gate)
        cells.append(
            {
                "strategy": sid,
                "gate": gate,
                "fired": int(row["fired"] or 0),
                "passed": int(row["passed"] or 0),
                "pass_pct": (
                    float(row["pass_pct"]) if row["pass_pct"] is not None else None
                ),
                "top_skip_reasons": reason_bucket.get((sid, gate), []),
            }
        )

    return {
        "strategies": sorted(strategies_set),
        "gates": sorted(gates_set),
        "cells": cells,
        "window": {
            "timeframe": timeframe,
            "asset": asset,
            "hours": hours,
            "strategy_id": strategy_id,
            "row_count_raw": int((meta_row or {}).get("row_count_raw") or 0),
            "earliest": (
                meta_row["earliest"].isoformat()
                if meta_row and meta_row.get("earliest")
                else None
            ),
            "latest": (
                meta_row["latest"].isoformat()
                if meta_row and meta_row.get("latest")
                else None
            ),
        },
    }


@router.get("/gate-traces/recent")
async def gate_traces_recent(
    timeframe: str = Query(default="5m"),
    asset: str = Query(default="BTC"),
    hours: int = Query(default=_DEFAULT_HOURS),
    strategy_id: Optional[str] = Query(default=None),
    limit: int = Query(default=_RECENT_DEFAULT_LIMIT),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Recent trace rows grouped by (strategy, window, offset).

    Each group is one "gate chain" — an ordered list of gate results for
    a single (strategy × window × eval_offset) triple. The FE renders the
    group as an expandable row beneath the heatmap: strategy + window
    timestamp + action + direction + the individual gate pass/fail pills.

    Response shape (bare-map; ``rows`` is an array but we intentionally
    nest it under ``groups`` so ``useApiLoader`` does NOT auto-unwrap it).
    """
    _validate_timeframe(timeframe)
    _validate_hours(hours)
    if limit < 1 or limit > _RECENT_MAX_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be between 1 and {_RECENT_MAX_LIMIT}",
        )

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    params: dict[str, Any] = {
        "asset": asset,
        "timeframe": timeframe,
        "cutoff": cutoff,
        "lim": limit,
    }
    strategy_clause = ""
    if strategy_id:
        strategy_clause = "AND strategy_id = :sid"
        params["sid"] = strategy_id

    try:
        # First — pick the N most-recent (strategy, window, offset) triples.
        keys_sql = text(f"""
            SELECT strategy_id, window_ts, eval_offset, MAX(evaluated_at) AS latest
            FROM gate_check_traces
            WHERE asset = :asset
              AND timeframe = :timeframe
              AND evaluated_at > :cutoff
              {strategy_clause}
            GROUP BY strategy_id, window_ts, eval_offset
            ORDER BY latest DESC
            LIMIT :lim
        """)
        key_rows = (await session.execute(keys_sql, params)).mappings().all()

        if not key_rows:
            return {"groups": [], "count": 0}

        # Then fetch every gate row for those triples in one shot.
        # Build the (strategy, window, offset) tuple-IN predicate via a
        # VALUES-style subselect — asyncpg doesn't love array-of-tuple binds.
        triples = [
            (k["strategy_id"], int(k["window_ts"]), int(k["eval_offset"]))
            for k in key_rows
        ]
        triple_params: dict[str, Any] = {
            "asset": asset,
            "timeframe": timeframe,
        }
        placeholders: list[str] = []
        for i, (sid, wts, off) in enumerate(triples):
            placeholders.append(f"(:s{i}, :w{i}, :o{i})")
            triple_params[f"s{i}"] = sid
            triple_params[f"w{i}"] = wts
            triple_params[f"o{i}"] = off

        traces_sql = text(f"""
            SELECT strategy_id, window_ts, eval_offset,
                   gate_order, gate_name, passed, mode, action, direction,
                   reason, skip_reason,
                   observed_json::text AS observed_text,
                   config_json::text   AS config_text,
                   evaluated_at
            FROM gate_check_traces
            WHERE asset = :asset
              AND timeframe = :timeframe
              AND (strategy_id, window_ts, eval_offset) IN ({", ".join(placeholders)})
            ORDER BY evaluated_at DESC, strategy_id ASC, gate_order ASC
        """)
        trace_rows = (await session.execute(traces_sql, triple_params)).mappings().all()

    except Exception as exc:
        log.warning("gate_traces.recent_error", error=str(exc)[:200])
        return {"groups": [], "count": 0, "error": str(exc)[:200]}

    # Group trace rows by (strategy, window_ts, eval_offset).
    import json

    groups_map: dict[tuple[str, int, int], dict[str, Any]] = {}
    for r in trace_rows:
        key = (r["strategy_id"], int(r["window_ts"]), int(r["eval_offset"]))
        group = groups_map.get(key)
        if group is None:
            group = {
                "strategy_id": r["strategy_id"],
                "window_ts": int(r["window_ts"]),
                "eval_offset": int(r["eval_offset"]),
                "mode": r["mode"],
                "action": r["action"],
                "direction": r["direction"],
                "evaluated_at": (
                    r["evaluated_at"].isoformat() if r["evaluated_at"] else None
                ),
                "gates": [],
            }
            groups_map[key] = group
        # Parse JSONB-as-text back to dicts. We stringified in SQL so
        # asyncpg returns str (not already-parsed dict) — consistent across
        # driver versions.
        def _load(v: Any) -> Any:
            if isinstance(v, (dict, list)):
                return v
            if v is None or v == "":
                return {}
            try:
                return json.loads(v)
            except Exception:
                return {"raw": str(v)[:500]}

        group["gates"].append(
            {
                "gate_order": int(r["gate_order"]),
                "gate_name": r["gate_name"],
                "passed": bool(r["passed"]),
                "reason": r["reason"] or "",
                "skip_reason": r["skip_reason"],
                "observed": _load(r["observed_text"]),
                "config": _load(r["config_text"]),
            }
        )

    # Preserve the "most recent first" ordering from the keys query.
    ordered: list[dict[str, Any]] = []
    for k in key_rows:
        key = (k["strategy_id"], int(k["window_ts"]), int(k["eval_offset"]))
        g = groups_map.get(key)
        if g is not None:
            ordered.append(g)

    return {"groups": ordered, "count": len(ordered)}
