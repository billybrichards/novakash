"""
Analysis Library API

GET  /api/analysis           — list all analysis docs
GET  /api/analysis/:doc_id   — get a specific doc
POST /api/analysis           — create a new analysis doc
"""

from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session

log = structlog.get_logger(__name__)

router = APIRouter()


class AnalysisDocCreate(BaseModel):
    doc_id: str
    title: str
    author: str = "Novakash"
    status: str = "draft"
    tags: list[str] = []
    summary: Optional[str] = None
    content: str
    data_period: Optional[str] = None


@router.get("/analysis")
async def list_analysis(
    limit: int = Query(50, ge=1, le=200),
    tag: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """List all analysis docs, newest first. Optional tag filter."""
    try:
        if tag:
            q = text("""
                SELECT id, doc_id, title, author, status, tags, summary, data_period, created_at, updated_at,
                       LENGTH(content) as content_length
                FROM analysis_docs
                WHERE :tag = ANY(tags)
                ORDER BY created_at DESC LIMIT :limit
            """)
            result = await session.execute(q, {"tag": tag, "limit": limit})
        else:
            q = text("""
                SELECT id, doc_id, title, author, status, tags, summary, data_period, created_at, updated_at,
                       LENGTH(content) as content_length
                FROM analysis_docs
                ORDER BY created_at DESC LIMIT :limit
            """)
            result = await session.execute(q, {"limit": limit})

        rows = result.mappings().all()
        return {
            "docs": [
                {
                    "id": r["id"],
                    "doc_id": r["doc_id"],
                    "title": r["title"],
                    "author": r["author"],
                    "status": r["status"],
                    "tags": list(r["tags"]) if r["tags"] else [],
                    "summary": r["summary"],
                    "data_period": r["data_period"],
                    "content_length": r["content_length"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                }
                for r in rows
            ],
            "count": len(rows),
        }
    except Exception as exc:
        log.warning("analysis.list_failed", error=str(exc))
        return {"docs": [], "count": 0, "error": str(exc)}


@router.get("/analysis/{doc_id}")
async def get_analysis(
    doc_id: str,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Get a specific analysis doc by doc_id."""
    try:
        q = text("""
            SELECT id, doc_id, title, author, status, tags, summary, content, data_period, created_at, updated_at
            FROM analysis_docs WHERE doc_id = :doc_id
        """)
        result = await session.execute(q, {"doc_id": doc_id})
        row = result.mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Analysis doc not found")
        return {
            "id": row["id"],
            "doc_id": row["doc_id"],
            "title": row["title"],
            "author": row["author"],
            "status": row["status"],
            "tags": list(row["tags"]) if row["tags"] else [],
            "summary": row["summary"],
            "content": row["content"],
            "data_period": row["data_period"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("analysis.get_failed", doc_id=doc_id, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/analysis")
async def create_analysis(
    body: AnalysisDocCreate,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Create a new analysis doc."""
    try:
        await session.execute(text("""
            INSERT INTO analysis_docs (doc_id, title, author, status, tags, summary, content, data_period)
            VALUES (:doc_id, :title, :author, :status, :tags, :summary, :content, :data_period)
        """), {
            "doc_id": body.doc_id,
            "title": body.title,
            "author": body.author,
            "status": body.status,
            "tags": body.tags,
            "summary": body.summary,
            "content": body.content,
            "data_period": body.data_period,
        })
        await session.commit()
        return {"ok": True, "doc_id": body.doc_id}
    except Exception as exc:
        log.warning("analysis.create_failed", doc_id=body.doc_id, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))
