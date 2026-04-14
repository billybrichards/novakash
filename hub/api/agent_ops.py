"""
Agent Ops — Spawn Claude Agent SDK agents as background tasks, store results in DB.

Endpoints (all JWT-protected):
  POST   /agent-ops/run/{agent_type}   — spawn agent, return task_id
  GET    /agent-ops/tasks              — list recent tasks (last 20)
  GET    /agent-ops/tasks/{task_id}    — get single task result
  DELETE /agent-ops/tasks/{task_id}    — cancel/delete task
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

import anyio
import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/agent-ops", tags=["agent-ops"])

# ─── Agent type registry ──────────────────────────────────────────────────────

AGENT_TYPES: dict[str, str] = {
    "sitrep": "Full system sitrep — spawns all sub-agents and compiles report",
    "health": "System health check — feeds, strategies, error log scan",
    "trade_analysis": "Analyse recent trade decisions and outcomes",
    "signal_quality": "Signal quality audit — VPIN accuracy, prediction surface",
    "clean_arch": "Clean architecture audit — find violations, open PR with fixes",
    "error_analyzer": "Scan engine logs for errors, open fix PR",
    "data_audit": "Data surface audit — DB tables, feed coverage, gaps",
    "frontend_fix": "Scan frontend for bugs/stale code, open fix PR",
}

# ─── Prompt builder ───────────────────────────────────────────────────────────

_PROMPTS: dict[str, str] = {
    "sitrep": """You are performing a full system sitrep for the BTC Trader Hub.

Read the codebase at /Users/billyrichards/Code/novakash and produce a comprehensive status report covering:
1. Engine health: which strategies are LIVE vs GHOST, any recent errors
2. Data feeds: all 6 feeds (Binance, Chainlink, Tiingo, CoinGlass, Gamma, CLOB) — any gaps or staleness
3. Hub: any API endpoints missing, schema drift, migration issues
4. Frontend: any broken pages, stale imports, dead code
5. Recent trade performance: check strategy_decisions table stats if accessible
6. Top 3 issues requiring attention

Be specific and actionable. Reference file paths where relevant.
Format as a structured markdown report with sections.""",

    "health": """You are performing a system health check for the BTC Trader Hub.

Scan the codebase at /Users/billyrichards/Code/novakash and check:
1. hub/main.py — all routers imported and included correctly
2. engine/main.py — all feeds and strategies wired up, mode flags correct
3. Data feed files — any obvious errors or stale configs
4. Strategy files — LIVE vs GHOST mode flags
5. Recent log patterns: look for any ERROR or CRITICAL patterns in the code
6. Requirements files — any obvious missing or conflicting deps

Report findings as a concise health check with PASS/WARN/FAIL per area.""",

    "trade_analysis": """You are analysing recent trade decisions and outcomes for the BTC Trader Hub.

Read the codebase at /Users/billyrichards/Code/novakash and:
1. Read engine/strategies/ to understand current active strategy logic
2. Read hub/api/analysis.py or similar to understand what analytics are available
3. Check the strategy_decisions table schema in hub/db/ migrations
4. Look at the frontend pages related to trade analysis (WindowResults, StrategyAnalysis, etc.)
5. Identify: what data is being captured, what win-rate metrics are tracked, any gaps

Summarise: what trade analysis is in place, what's missing, and what the current strategy performance signals suggest.""",

    "signal_quality": """You are auditing signal quality for the BTC Trader Hub.

Read the codebase at /Users/billyrichards/Code/novakash and:
1. Read engine/signals/ — VPIN calculator, cascade detector, regime classifier
2. Read engine/data/ — all feed files, especially Chainlink and Tiingo
3. Read the strategy files in engine/strategies/ to see how signals are consumed
4. Check hub/api/signals.py and frontend/src/pages/Signals.jsx
5. Identify: signal accuracy issues, potential bugs in the signal pipeline, data quality gaps

Produce a signal quality audit report with specific findings and recommendations.""",

    "clean_arch": """You are performing a clean architecture audit for the BTC Trader Hub.

Read the codebase at /Users/billyrichards/Code/novakash and:
1. Check for layering violations (engine importing from hub, etc.)
2. Check for circular imports
3. Check for dead code — unused imports, unreferenced functions
4. Check for inconsistent patterns (e.g., some routers use get_session differently)
5. Check for missing error handling in critical paths
6. Check that all DB writes use proper async patterns

List specific violations with file paths and line references. Prioritise by severity (HIGH/MEDIUM/LOW).""",

    "error_analyzer": """You are scanning the BTC Trader Hub codebase for error-prone patterns.

Read the codebase at /Users/billyrichards/Code/novakash and:
1. Search for broad exception catches (bare `except:`, `except Exception:` without logging)
2. Search for missing `await` on async calls
3. Search for potential None dereferences (accessing attributes without null checks)
4. Check DB session patterns for potential connection leaks
5. Check WebSocket error handling in hub/ws/
6. Look for any TODO/FIXME/HACK comments that indicate known issues

List each finding with: file path, line number (if determinable), issue type, and severity.""",

    "data_audit": """You are auditing the data surface for the BTC Trader Hub.

Read the codebase at /Users/billyrichards/Code/novakash and:
1. List all DB tables defined in hub/db/ migrations and startup_ddl
2. List all data feeds in engine/data/ and what tables they write to
3. Check the Schema page (hub/api/schema.py) to understand the table inventory
4. Identify any feeds that write to tables not in the schema, or tables with no feed writing to them
5. Check ticks tables: ticks_chainlink, ticks_tiingo, ticks_binance — are they all present?
6. Check the signal_evaluations and strategy_decisions tables — what's captured there?

Produce a data audit with: complete table list, feed-to-table mapping, coverage gaps.""",

    "frontend_fix": """You are scanning the BTC Trader Hub frontend for bugs and stale code.

Read the codebase at /Users/billyrichards/Code/novakash/frontend/src and:
1. Check all page imports in App.jsx — are all imported pages present on disk?
2. Check Layout.jsx nav links — do all paths have corresponding routes in App.jsx?
3. Look for pages that import API endpoints that no longer exist
4. Check for console.error calls that indicate known issues
5. Look for TODO comments
6. Check for any hardcoded URLs or IPs that should be env vars
7. Check that the useApi hook is used consistently (not raw fetch/axios)

List each issue with: file, component/function, issue description, severity (HIGH/MEDIUM/LOW).""",
}


def build_prompt(agent_type: str) -> str:
    return _PROMPTS.get(agent_type, f"Perform a {agent_type} analysis of the BTC Trader Hub codebase at /Users/billyrichards/Code/novakash and produce a detailed report.")


# ─── DB helpers ───────────────────────────────────────────────────────────────

def _row_to_task(row) -> dict:
    d = dict(row)
    for key in ("started_at", "completed_at"):
        val = d.get(key)
        if val is not None and hasattr(val, "isoformat"):
            d[key] = val.isoformat()
    # Cast UUID to string
    if "id" in d and d["id"] is not None:
        d["id"] = str(d["id"])
    return d


async def _get_db_session():
    """Get a fresh DB session for background task use."""
    from db.database import get_session as _get_session
    async for session in _get_session():
        return session


async def _save_task_result(task_id: str, result_text: str) -> None:
    """Update task row with result and mark done."""
    try:
        session = await _get_db_session()
        await session.execute(
            text("""
                UPDATE agent_tasks
                SET status = 'done',
                    result = :result,
                    completed_at = NOW()
                WHERE id = :id::uuid
            """),
            {"id": task_id, "result": result_text},
        )
        await session.commit()
        log.info("agent_ops.task_done", task_id=task_id)
    except Exception as exc:
        log.error("agent_ops.save_result_error", task_id=task_id, error=str(exc))


async def _save_task_error(task_id: str, error_text: str) -> None:
    """Update task row with error and mark failed."""
    try:
        session = await _get_db_session()
        await session.execute(
            text("""
                UPDATE agent_tasks
                SET status = 'failed',
                    error = :error,
                    completed_at = NOW()
                WHERE id = :id::uuid
            """),
            {"id": task_id, "error": error_text[:2000]},
        )
        await session.commit()
        log.info("agent_ops.task_failed", task_id=task_id)
    except Exception as exc:
        log.error("agent_ops.save_error_error", task_id=task_id, error=str(exc))


# ─── Agent runner ─────────────────────────────────────────────────────────────

async def run_agent(agent_type: str, task_id: str) -> None:
    """Run a Claude Agent SDK agent and save results to DB."""
    log.info("agent_ops.agent_start", agent_type=agent_type, task_id=task_id)
    prompt = build_prompt(agent_type)

    try:
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage  # type: ignore

        options = ClaudeAgentOptions(
            cwd="/Users/billyrichards/Code/novakash",
            allowed_tools=["Read", "Glob", "Grep", "Bash"],
            max_turns=20,
            permission_mode="default",
        )

        result_text = ""
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                result_text = message.result

        if not result_text:
            result_text = "(Agent completed but produced no result text)"

        await _save_task_result(task_id, result_text)

    except ImportError:
        error_msg = (
            "claude-agent-sdk not installed. "
            "Run: pip install claude-agent-sdk"
        )
        log.warning("agent_ops.sdk_not_found", task_id=task_id)
        await _save_task_error(task_id, error_msg)
    except Exception as exc:
        log.error("agent_ops.agent_error", task_id=task_id, error=str(exc))
        await _save_task_error(task_id, str(exc))


def _spawn_agent(agent_type: str, task_id: str) -> None:
    """Synchronous wrapper to run the async agent in a new event loop thread."""
    import asyncio
    try:
        asyncio.run(run_agent(agent_type, task_id))
    except Exception as exc:
        log.error("agent_ops.spawn_error", task_id=task_id, error=str(exc))


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/run/{agent_type}")
async def run_agent_task(
    agent_type: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Spawn an agent task. Returns task_id immediately; agent runs in background."""
    if agent_type not in AGENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown agent type '{agent_type}'. Valid types: {list(AGENT_TYPES.keys())}",
        )

    task_id = str(uuid.uuid4())

    await session.execute(
        text("""
            INSERT INTO agent_tasks (id, agent_type, status)
            VALUES (:id::uuid, :agent_type, 'running')
        """),
        {"id": task_id, "agent_type": agent_type},
    )
    await session.commit()

    # Run in a thread so it gets its own event loop
    import concurrent.futures
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(executor, _spawn_agent, agent_type, task_id)

    log.info("agent_ops.task_spawned", agent_type=agent_type, task_id=task_id)
    return {
        "task_id": task_id,
        "agent_type": agent_type,
        "status": "running",
        "description": AGENT_TYPES[agent_type],
    }


@router.get("/tasks")
async def list_tasks(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """List the 20 most recent agent tasks."""
    result = await session.execute(
        text("""
            SELECT id, agent_type, status, result, error, started_at, completed_at
            FROM agent_tasks
            ORDER BY started_at DESC
            LIMIT 20
        """)
    )
    rows = [_row_to_task(r) for r in result.mappings().all()]
    return {"tasks": rows, "agent_types": AGENT_TYPES}


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Get a single task by ID."""
    result = await session.execute(
        text("""
            SELECT id, agent_type, status, result, error, started_at, completed_at
            FROM agent_tasks
            WHERE id = :id::uuid
        """),
        {"id": task_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": _row_to_task(row)}


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Delete a task record. Running tasks cannot be cancelled but the record will be removed."""
    result = await session.execute(
        text("DELETE FROM agent_tasks WHERE id = :id::uuid RETURNING id"),
        {"id": task_id},
    )
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await session.commit()
    log.info("agent_ops.task_deleted", task_id=task_id)
    return {"deleted": True, "id": task_id}
