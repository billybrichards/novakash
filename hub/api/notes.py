"""
Notes API — DB-backed journal for audit observations, TODOs, and working notes.

Persistent across frontend redeploys. Used by the /notes page (NT-01).
Think of it as a collaborative whiteboard that a future session can read back.

Endpoints (all JWT-protected):
  GET    /notes?status=open&tag=foo&limit=100&offset=0  — list notes
  GET    /notes/{id}                                     — single note
  POST   /notes                                          — create note
  PATCH  /notes/{id}                                     — update note
  DELETE /notes/{id}                                     — delete note
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

router = APIRouter(prefix="/notes", tags=["notes"])


# ─── Pydantic schemas ─────────────────────────────────────────────────────────

class NoteCreate(BaseModel):
    title: Optional[str] = Field(default="", max_length=200)
    body: str = Field(..., min_length=1)
    tags: Optional[str] = Field(default="", max_length=500)
    status: Optional[str] = Field(default="open", pattern="^(open|archived)$")
    author: Optional[str] = Field(default="claude", max_length=50)


class NoteUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    body: Optional[str] = Field(default=None, min_length=1)
    tags: Optional[str] = Field(default=None, max_length=500)
    status: Optional[str] = Field(default=None, pattern="^(open|archived)$")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _row_to_note(row) -> dict:
    """Convert a SQLAlchemy row mapping to a JSON-safe note dict."""
    d = dict(row)
    for key in ("created_at", "updated_at"):
        val = d.get(key)
        if val is not None and hasattr(val, "isoformat"):
            d[key] = val.isoformat()
    return d


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("")
async def list_notes(
    status: str = Query("open", pattern="^(open|archived|all)$"),
    tag: Optional[str] = Query(None, max_length=100),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    List notes, most-recently-updated first.

    status:
      - "open"     — only open notes (default)
      - "archived" — only archived notes
      - "all"      — both
    tag: substring match against the CSV tags column (case-insensitive)
    """
    where_clauses: list[str] = []
    params: dict = {}

    if status != "all":
        where_clauses.append("status = :status")
        params["status"] = status

    if tag:
        where_clauses.append("LOWER(tags) LIKE :tag")
        params["tag"] = f"%{tag.lower()}%"

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    # Count total (for pagination)
    count_q = f"SELECT COUNT(*) AS c FROM notes {where_sql}"
    count_res = await session.execute(text(count_q), params)
    total = int(count_res.scalar_one() or 0)

    # Fetch rows
    params["limit"] = limit
    params["offset"] = offset
    list_q = f"""
        SELECT id, title, body, tags, status, author, created_at, updated_at
        FROM notes
        {where_sql}
        ORDER BY updated_at DESC
        LIMIT :limit OFFSET :offset
    """
    result = await session.execute(text(list_q), params)
    rows = [_row_to_note(r) for r in result.mappings().all()]

    return {
        "rows": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{note_id}")
async def get_note(
    note_id: int,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Fetch a single note by id."""
    q = """
        SELECT id, title, body, tags, status, author, created_at, updated_at
        FROM notes
        WHERE id = :id
    """
    result = await session.execute(text(q), {"id": note_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"note": _row_to_note(row)}


@router.post("")
async def create_note(
    req: NoteCreate,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Create a new note."""
    q = """
        INSERT INTO notes (title, body, tags, status, author)
        VALUES (:title, :body, :tags, :status, :author)
        RETURNING id, title, body, tags, status, author, created_at, updated_at
    """
    result = await session.execute(
        text(q),
        {
            "title": req.title or "",
            "body": req.body,
            "tags": req.tags or "",
            "status": req.status or "open",
            "author": req.author or "claude",
        },
    )
    row = result.mappings().first()
    log.info("notes.created", note_id=row["id"] if row else None, author=req.author)
    return {"note": _row_to_note(row)}


@router.patch("/{note_id}")
async def update_note(
    note_id: int,
    req: NoteUpdate,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Partial update of a note. Only supplied fields are changed.
    """
    # Fetch existing row to confirm it exists
    existing = await session.execute(
        text("SELECT id FROM notes WHERE id = :id"), {"id": note_id}
    )
    if existing.first() is None:
        raise HTTPException(status_code=404, detail="Note not found")

    set_parts: list[str] = []
    params: dict = {"id": note_id}

    if req.title is not None:
        set_parts.append("title = :title")
        params["title"] = req.title
    if req.body is not None:
        set_parts.append("body = :body")
        params["body"] = req.body
    if req.tags is not None:
        set_parts.append("tags = :tags")
        params["tags"] = req.tags
    if req.status is not None:
        set_parts.append("status = :status")
        params["status"] = req.status

    if not set_parts:
        # Nothing to update — just return the current row
        fetch = await session.execute(
            text(
                """
                SELECT id, title, body, tags, status, author, created_at, updated_at
                FROM notes WHERE id = :id
                """
            ),
            {"id": note_id},
        )
        row = fetch.mappings().first()
        return {"note": _row_to_note(row)}

    # updated_at is handled by onupdate=func.now() in the ORM, but raw SQL
    # bypasses that, so bump it explicitly.
    set_parts.append("updated_at = NOW()")
    set_sql = ", ".join(set_parts)

    q = f"""
        UPDATE notes
        SET {set_sql}
        WHERE id = :id
        RETURNING id, title, body, tags, status, author, created_at, updated_at
    """
    result = await session.execute(text(q), params)
    row = result.mappings().first()
    log.info("notes.updated", note_id=note_id, fields=[p.split(" = ")[0] for p in set_parts])
    return {"note": _row_to_note(row)}


@router.delete("/{note_id}")
async def delete_note(
    note_id: int,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Hard-delete a note by id."""
    result = await session.execute(
        text("DELETE FROM notes WHERE id = :id RETURNING id"),
        {"id": note_id},
    )
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Note not found")
    log.info("notes.deleted", note_id=note_id)
    return {"deleted": True, "id": note_id}
