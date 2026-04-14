"""
Audit Tasks API — Agent Ops task queue + audit checklist persistence.

Endpoints (all JWT-protected):
  GET    /audit-tasks                     — list tasks (filters)
  GET    /audit-tasks/{id}                — single task
  POST   /audit-tasks                     — create task (dedupe_key optional)
  PATCH  /audit-tasks/{id}                — update task fields
  POST   /audit-tasks/claim               — claim next available task
  POST   /audit-tasks/{id}/claim          — claim a specific task
  POST   /audit-tasks/{id}/release        — release claim + reopen
  POST   /audit-tasks/{id}/heartbeat      — extend lease + heartbeat
"""

from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/audit-tasks", tags=["audit-tasks"])


class TaskCreate(BaseModel):
    task_key: Optional[str] = Field(default=None, max_length=64)
    task_type: str = Field(..., max_length=64)
    source: Optional[str] = Field(default=None, max_length=64)
    title: str = Field(..., min_length=1)
    status: Optional[str] = Field(default="OPEN", max_length=24)
    severity: Optional[str] = Field(default=None, max_length=16)
    category: Optional[str] = Field(default=None, max_length=64)
    priority: Optional[int] = Field(default=0, ge=0)
    dedupe_key: Optional[str] = Field(default=None, max_length=500)
    payload: Optional[dict] = Field(default_factory=dict)
    metadata: Optional[dict] = Field(default_factory=dict)
    status_reason: Optional[str] = Field(default=None)


class TaskUpdate(BaseModel):
    task_key: Optional[str] = Field(default=None, max_length=64)
    task_type: Optional[str] = Field(default=None, max_length=64)
    source: Optional[str] = Field(default=None, max_length=64)
    title: Optional[str] = Field(default=None)
    status: Optional[str] = Field(default=None, max_length=24)
    severity: Optional[str] = Field(default=None, max_length=16)
    category: Optional[str] = Field(default=None, max_length=64)
    priority: Optional[int] = Field(default=None, ge=0)
    payload: Optional[dict] = Field(default=None)
    metadata: Optional[dict] = Field(default=None)
    status_reason: Optional[str] = Field(default=None)
    last_error: Optional[str] = Field(default=None)


class ClaimRequest(BaseModel):
    agent_id: str = Field(..., max_length=64)
    lease_seconds: int = Field(default=600, ge=30, le=7200)
    status: Optional[str] = Field(default="OPEN")
    task_type: Optional[str] = Field(default=None)


class HeartbeatRequest(BaseModel):
    agent_id: str = Field(..., max_length=64)
    lease_seconds: int = Field(default=600, ge=30, le=7200)


def _row_to_task(row) -> dict:
    d = dict(row)
    for key in (
        "created_at",
        "updated_at",
        "claimed_at",
        "claim_expires_at",
        "started_at",
        "completed_at",
        "canceled_at",
        "last_heartbeat_at",
    ):
        val = d.get(key)
        if val is not None and hasattr(val, "isoformat"):
            d[key] = val.isoformat()
    return d


@router.get("")
async def list_tasks(
    status: str = Query("all", max_length=64),
    task_type: Optional[str] = Query(None, max_length=64),
    claimed_by: Optional[str] = Query(None, max_length=64),
    category: Optional[str] = Query(None, max_length=64),
    severity: Optional[str] = Query(None, max_length=16),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    where_clauses: list[str] = []
    params: dict = {}

    if status and status.lower() != "all":
        where_clauses.append("status = :status")
        params["status"] = status.upper()
    if task_type:
        where_clauses.append("task_type = :task_type")
        params["task_type"] = task_type
    if claimed_by:
        where_clauses.append("claimed_by = :claimed_by")
        params["claimed_by"] = claimed_by
    if category:
        where_clauses.append("category = :category")
        params["category"] = category
    if severity:
        where_clauses.append("severity = :severity")
        params["severity"] = severity

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    count_q = f"SELECT COUNT(*) AS c FROM audit_tasks_dev {where_sql}"
    count_res = await session.execute(text(count_q), params)
    total = int(count_res.scalar_one() or 0)

    params.update({"limit": limit, "offset": offset})
    list_q = f"""
        SELECT *
        FROM audit_tasks_dev
        {where_sql}
        ORDER BY updated_at DESC
        LIMIT :limit OFFSET :offset
    """
    result = await session.execute(text(list_q), params)
    rows = [_row_to_task(r) for r in result.mappings().all()]
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/{task_id}")
async def get_task(
    task_id: int,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    q = "SELECT * FROM audit_tasks_dev WHERE id = :id"
    result = await session.execute(text(q), {"id": task_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": _row_to_task(row)}


@router.post("")
async def create_task(
    req: TaskCreate,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    q = """
        INSERT INTO audit_tasks_dev (
            task_key, task_type, source, title, status, severity, category, priority,
            dedupe_key, payload, metadata, created_by, updated_by, status_reason
        )
        VALUES (
            :task_key, :task_type, :source, :title, :status, :severity, :category, :priority,
            :dedupe_key, :payload, :metadata, :created_by, :updated_by, :status_reason
        )
        ON CONFLICT (dedupe_key) DO UPDATE
          SET updated_at = NOW(), status_reason = EXCLUDED.status_reason
        RETURNING *
    """
    params = {
        "task_key": req.task_key,
        "task_type": req.task_type,
        "source": req.source,
        "title": req.title,
        "status": (req.status or "OPEN").upper(),
        "severity": req.severity,
        "category": req.category,
        "priority": req.priority or 0,
        "dedupe_key": req.dedupe_key,
        "payload": req.payload or {},
        "metadata": req.metadata or {},
        "created_by": user.username,
        "updated_by": user.username,
        "status_reason": req.status_reason,
    }
    result = await session.execute(text(q), params)
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create task")
    log.info("audit_tasks.created", task_id=row["id"], created_by=user.username)
    return {"task": _row_to_task(row)}


@router.patch("/{task_id}")
async def update_task(
    task_id: int,
    req: TaskUpdate,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    existing = await session.execute(
        text("SELECT id FROM audit_tasks_dev WHERE id = :id"), {"id": task_id}
    )
    if existing.first() is None:
        raise HTTPException(status_code=404, detail="Task not found")

    set_parts: list[str] = ["updated_at = NOW()", "updated_by = :updated_by"]
    params: dict = {"id": task_id, "updated_by": user.username}

    for field, col in (
        (req.task_key, "task_key"),
        (req.task_type, "task_type"),
        (req.source, "source"),
        (req.title, "title"),
        (req.status, "status"),
        (req.severity, "severity"),
        (req.category, "category"),
        (req.priority, "priority"),
        (req.payload, "payload"),
        (req.metadata, "metadata"),
        (req.status_reason, "status_reason"),
        (req.last_error, "last_error"),
    ):
        if field is not None:
            set_parts.append(f"{col} = :{col}")
            params[col] = field

    if len(set_parts) == 2:
        return await get_task(task_id, session, user)

    q = f"""
        UPDATE audit_tasks_dev
        SET {", ".join(set_parts)}
        WHERE id = :id
        RETURNING *
    """
    result = await session.execute(text(q), params)
    row = result.mappings().first()
    return {"task": _row_to_task(row)}


@router.post("/claim")
async def claim_next_task(
    req: ClaimRequest,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    status = (req.status or "OPEN").upper()
    task_type_filter = "AND task_type = :task_type" if req.task_type else ""
    params = {
        "status": status,
        "task_type": req.task_type,
        "agent_id": req.agent_id,
        "lease_seconds": req.lease_seconds,
    }

    async with session.begin():
        select_q = f"""
            SELECT id
            FROM audit_tasks_dev
            WHERE status = :status
              AND (claim_expires_at IS NULL OR claim_expires_at < NOW())
              {task_type_filter}
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        """
        res = await session.execute(text(select_q), params)
        row = res.mappings().first()
        if not row:
            return {"task": None}

        update_q = """
            UPDATE audit_tasks_dev
            SET status = 'CLAIMED',
                claimed_by = :agent_id,
                claimed_at = NOW(),
                claim_expires_at = NOW() + (:lease_seconds || ' seconds')::interval,
                last_heartbeat_at = NOW(),
                attempt_count = attempt_count + 1,
                updated_at = NOW(),
                updated_by = :agent_id
            WHERE id = :id
            RETURNING *
        """
        result = await session.execute(text(update_q), {**params, "id": row["id"]})
        claimed = result.mappings().first()
        return {"task": _row_to_task(claimed)}


@router.post("/{task_id}/claim")
async def claim_task(
    task_id: int,
    req: ClaimRequest,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    async with session.begin():
        select_q = """
            SELECT id
            FROM audit_tasks_dev
            WHERE id = :id
              AND status = :status
              AND (claim_expires_at IS NULL OR claim_expires_at < NOW())
            FOR UPDATE SKIP LOCKED
        """
        res = await session.execute(
            text(select_q), {"id": task_id, "status": (req.status or "OPEN").upper()}
        )
        row = res.mappings().first()
        if not row:
            raise HTTPException(status_code=409, detail="Task not available to claim")

        update_q = """
            UPDATE audit_tasks_dev
            SET status = 'CLAIMED',
                claimed_by = :agent_id,
                claimed_at = NOW(),
                claim_expires_at = NOW() + (:lease_seconds || ' seconds')::interval,
                last_heartbeat_at = NOW(),
                attempt_count = attempt_count + 1,
                updated_at = NOW(),
                updated_by = :agent_id
            WHERE id = :id
            RETURNING *
        """
        result = await session.execute(
            text(update_q),
            {
                "agent_id": req.agent_id,
                "lease_seconds": req.lease_seconds,
                "id": task_id,
            },
        )
        claimed = result.mappings().first()
        return {"task": _row_to_task(claimed)}


@router.post("/{task_id}/release")
async def release_task(
    task_id: int,
    req: ClaimRequest,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    q = """
        UPDATE audit_tasks_dev
        SET status = 'OPEN',
            claimed_by = NULL,
            claimed_at = NULL,
            claim_expires_at = NULL,
            updated_at = NOW(),
            updated_by = :agent_id
        WHERE id = :id
        RETURNING *
    """
    result = await session.execute(text(q), {"id": task_id, "agent_id": req.agent_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": _row_to_task(row)}


@router.post("/{task_id}/heartbeat")
async def heartbeat_task(
    task_id: int,
    req: HeartbeatRequest,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    q = """
        UPDATE audit_tasks_dev
        SET last_heartbeat_at = NOW(),
            claim_expires_at = NOW() + (:lease_seconds || ' seconds')::interval,
            updated_at = NOW(),
            updated_by = :agent_id
        WHERE id = :id
        RETURNING *
    """
    result = await session.execute(
        text(q),
        {"id": task_id, "agent_id": req.agent_id, "lease_seconds": req.lease_seconds},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": _row_to_task(row)}
