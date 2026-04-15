from __future__ import annotations

import json
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session

router = APIRouter()


@router.get("/window-traces/{asset}/{timeframe}/{window_ts}")
async def get_window_trace(
    asset: str,
    timeframe: str,
    window_ts: int,
    eval_offset: int | None = Query(None),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    offset_clause = ""
    params: dict[str, object] = {
        "asset": asset,
        "timeframe": timeframe,
        "window_ts": window_ts,
    }
    if eval_offset is not None:
        offset_clause = " AND eval_offset = :eval_offset"
        params["eval_offset"] = eval_offset

    trace_q = text(
        f"""
        SELECT asset, window_ts, timeframe, eval_offset, surface_json, assembled_at
        FROM window_evaluation_traces
        WHERE asset = :asset AND timeframe = :timeframe AND window_ts = :window_ts
        {offset_clause}
        ORDER BY eval_offset DESC
        LIMIT 1
        """
    )
    trace_row = (await session.execute(trace_q, params)).mappings().first()
    if not trace_row:
        raise HTTPException(status_code=404, detail="Window trace not found")

    decisions_q = text(
        f"""
        SELECT strategy_id, strategy_version, mode, action, direction,
               confidence, confidence_score, entry_cap, collateral_pct,
               entry_reason, skip_reason, executed, order_id, fill_price,
               fill_size, eval_offset, evaluated_at, metadata_json
        FROM strategy_decisions
        WHERE asset = :asset AND timeframe = :timeframe AND window_ts = :window_ts
        {offset_clause}
        ORDER BY eval_offset DESC, strategy_id ASC
        """
    )
    decision_rows = (await session.execute(decisions_q, params)).mappings().all()

    gate_q = text(
        f"""
        SELECT strategy_id, gate_order, gate_name, passed, mode, action,
               direction, reason, skip_reason, observed_json, config_json,
               eval_offset, evaluated_at
        FROM gate_check_traces
        WHERE asset = :asset AND timeframe = :timeframe AND window_ts = :window_ts
        {offset_clause}
        ORDER BY eval_offset DESC, strategy_id ASC, gate_order ASC
        """
    )
    gate_rows = (await session.execute(gate_q, params)).mappings().all()

    outcome_q = text(
        """
        SELECT actual_direction, outcome, pnl_usd, poly_winner, resolved_at
        FROM window_snapshots
        WHERE asset = :asset AND timeframe = :timeframe AND window_ts = :window_ts
        LIMIT 1
        """
    )
    outcome_row = (await session.execute(outcome_q, params)).mappings().first()

    eligible_now: list[str] = []
    blocked_by_signal: list[str] = []
    blocked_by_timing: list[str] = []
    inactive_this_offset: list[str] = []

    decisions = []
    for row in decision_rows:
        label = f"{row['strategy_id']} ({row['mode']})"
        reason = row["skip_reason"] or ""
        if row["action"] == "TRADE":
            direction = row["direction"] or "?"
            conf = f" | conf={row['confidence']}" if row["confidence"] else ""
            eligible_now.append(f"{label}: TRADE {direction}{conf}")
        elif row["action"] == "SKIP":
            if reason.startswith("timing:") and " outside [" in reason:
                inactive_this_offset.append(label)
            elif "too late" in reason or "timing=late" in reason:
                blocked_by_timing.append(f"{label}: {reason}")
            else:
                blocked_by_signal.append(f"{label}: {reason}")
        else:
            blocked_by_signal.append(f"{label}: ERROR")

        metadata = row["metadata_json"]
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {"raw": metadata}
        decisions.append(
            {
                **dict(row),
                "evaluated_at": row["evaluated_at"].isoformat()
                if row["evaluated_at"]
                else None,
                "metadata_json": metadata,
            }
        )

    gate_checks_by_strategy: dict[str, list[dict]] = defaultdict(list)
    for row in gate_rows:
        gate_checks_by_strategy[row["strategy_id"]].append(
            {
                **dict(row),
                "evaluated_at": row["evaluated_at"].isoformat()
                if row["evaluated_at"]
                else None,
            }
        )

    return {
        "asset": trace_row["asset"],
        "window_ts": trace_row["window_ts"],
        "timeframe": trace_row["timeframe"],
        "eval_offset": trace_row["eval_offset"],
        "surface_data": trace_row["surface_json"] or {},
        "assembled_at": trace_row["assembled_at"].isoformat()
        if trace_row["assembled_at"]
        else None,
        "eligible_now": eligible_now,
        "blocked_by_signal": blocked_by_signal,
        "blocked_by_timing": blocked_by_timing,
        "inactive_this_offset": inactive_this_offset,
        "strategy_decisions": decisions,
        "gate_checks_by_strategy": gate_checks_by_strategy,
        "outcome": dict(outcome_row) if outcome_row else None,
    }


@router.get("/strategy-window-analysis/{asset}/{timeframe}/{strategy_id}")
async def get_strategy_window_analysis(
    asset: str,
    timeframe: str,
    strategy_id: str,
    start_window_ts: int = Query(...),
    end_window_ts: int = Query(...),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    decision_q = text(
        """
        SELECT strategy_id, mode, action, direction, confidence, entry_cap,
               skip_reason, eval_offset, window_ts, executed, metadata_json
        FROM strategy_decisions
        WHERE asset = :asset
          AND timeframe = :timeframe
          AND strategy_id = :strategy_id
          AND window_ts BETWEEN :start_window_ts AND :end_window_ts
        ORDER BY window_ts DESC, eval_offset DESC
        """
    )
    decision_rows = (
        (
            await session.execute(
                decision_q,
                {
                    "asset": asset,
                    "timeframe": timeframe,
                    "strategy_id": strategy_id,
                    "start_window_ts": start_window_ts,
                    "end_window_ts": end_window_ts,
                },
            )
        )
        .mappings()
        .all()
    )

    trace_q = text(
        """
        SELECT window_ts, eval_offset, surface_json, assembled_at
        FROM window_evaluation_traces
        WHERE asset = :asset
          AND timeframe = :timeframe
          AND window_ts BETWEEN :start_window_ts AND :end_window_ts
        ORDER BY window_ts DESC, eval_offset DESC
        """
    )
    trace_rows = (
        (
            await session.execute(
                trace_q,
                {
                    "asset": asset,
                    "timeframe": timeframe,
                    "start_window_ts": start_window_ts,
                    "end_window_ts": end_window_ts,
                },
            )
        )
        .mappings()
        .all()
    )
    trace_map = {
        (r["window_ts"], r["eval_offset"]): r["surface_json"] or {} for r in trace_rows
    }

    total = len(decision_rows)
    tradeable = 0
    inactive = 0
    blocked_by_timing = 0
    blocked_by_signal = 0
    executed = 0
    recent_tradeable_examples = []
    recent_non_tradeable_examples = []
    latest_surface_examples = []

    for row in decision_rows:
        surface = trace_map.get((row["window_ts"], row["eval_offset"]), {})
        metadata = row["metadata_json"]
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {"raw": metadata}
        example = {
            "window_ts": row["window_ts"],
            "eval_offset": row["eval_offset"],
            "action": row["action"],
            "direction": row["direction"],
            "skip_reason": row["skip_reason"],
            "entry_cap": row["entry_cap"],
            "confidence": row["confidence"],
            "surface": surface,
            "metadata": metadata,
        }
        if len(latest_surface_examples) < 10:
            latest_surface_examples.append(example)

        if row["executed"]:
            executed += 1

        if row["action"] == "TRADE":
            tradeable += 1
            if len(recent_tradeable_examples) < 10:
                recent_tradeable_examples.append(example)
            continue

        reason = row["skip_reason"] or ""
        if reason.startswith("timing:") and " outside [" in reason:
            inactive += 1
        elif "too late" in reason or "timing=late" in reason:
            blocked_by_timing += 1
        else:
            blocked_by_signal += 1
        if len(recent_non_tradeable_examples) < 10:
            recent_non_tradeable_examples.append(example)

    return {
        "strategy_id": strategy_id,
        "timeframe": timeframe,
        "asset": asset,
        "total_evaluations": total,
        "tradeable_evaluations": tradeable,
        "non_tradeable_evaluations": total - tradeable,
        "executed_trades": executed,
        "inactive_evaluations": inactive,
        "blocked_by_timing": blocked_by_timing,
        "blocked_by_signal": blocked_by_signal,
        "latest_surface_examples": latest_surface_examples,
        "recent_tradeable_examples": recent_tradeable_examples,
        "recent_non_tradeable_examples": recent_non_tradeable_examples,
    }
