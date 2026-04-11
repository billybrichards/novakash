"""
CFG-03 — Hub read-only API for the new DB-backed config tables.

Mounts under /api/v58/config/*. Read-only in this PR; write endpoints
land in CFG-04.

Endpoints:
  GET  /api/v58/config/services             list of services + key counts
  GET  /api/v58/config?service=engine       all keys for a service with values
  GET  /api/v58/config/schema?service=...   key metadata only (no values)
  GET  /api/v58/config/history?service=...&key=...
                                            last N changes for a key

  POST /api/v58/config*                     all return 501 — pointer to CFG-04

The router lives in a new file (config_v2.py) to keep it isolated from the
existing hub/api/config.py mini-API, which will be retired in CFG-11.
v58_monitor.py was the alternative landing site but it's already focused
on trade monitoring; a dedicated router keeps the surface clean.

Auth: every endpoint takes Depends(get_current_user) — same JWT auth wall
the rest of the hub uses. CFG-06 will add an admin-claim check on the
write endpoints; v1 read-only is open to any authenticated user.
"""

from __future__ import annotations

from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session

log = structlog.get_logger(__name__)

# All routes mount under /v58/config so the full path is /api/v58/config/*
# (because main.py registers the router with prefix="/api/v58").
router = APIRouter(prefix="/v58/config", tags=["config-v2"])


# ─── helpers ──────────────────────────────────────────────────────────────────


def _coerce_value(raw: Optional[str], value_type: str) -> Any:
    """Best-effort cast a TEXT value out of config_keys/config_values into
    the type the UI expects.

    The DB stores everything as TEXT for schema simplicity (per the CFG-02
    DDL spec). The UI wants real bools / numbers so the widgets render
    correctly. This is the conversion layer.

    Returns the original string for type=string and for any value the
    coercion can't parse — never raises, never silently corrupts data.
    """
    if raw is None or raw == "":
        return None
    vt = (value_type or "string").lower()
    try:
        if vt == "bool":
            return raw.strip().lower() in ("true", "1", "yes", "on")
        if vt == "int":
            return int(raw)
        if vt == "float":
            return float(raw)
        # enum / string / csv → leave as the source string
        return raw
    except (ValueError, TypeError):
        log.warning("config_v2.coerce_failed", raw=raw, type=value_type)
        return raw


def _row_to_key_dict(row: dict) -> dict:
    """Shape a config_keys + config_values join row for the wire response."""
    default_value = row.get("default_value")
    current_value_raw = row.get("current_value_raw")
    value_type = row.get("type") or "string"
    return {
        "service": row.get("service"),
        "key": row.get("key"),
        "type": value_type,
        "category": row.get("category") or "uncategorized",
        "description": row.get("description") or "",
        "default_value": _coerce_value(default_value, value_type),
        "default_value_raw": default_value,
        "current_value": _coerce_value(current_value_raw, value_type),
        "current_value_raw": current_value_raw,
        "is_at_default": (current_value_raw is None) or (current_value_raw == default_value),
        "restart_required": bool(row.get("restart_required", False)),
        "editable_via_ui": bool(row.get("editable_via_ui", True)),
        "set_by": row.get("set_by"),
        "set_at": row.get("set_at").isoformat() if row.get("set_at") else None,
        "min_value": row.get("min_value"),
        "max_value": row.get("max_value"),
        "enum_values": row.get("enum_values"),
    }


# ─── GET /v58/config/services ────────────────────────────────────────────────


@router.get("/services")
async def list_services(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Return the list of services that have any DB-managed config keys.

    Used by the frontend sidebar. Each row reports total key count and
    last-changed timestamp from config_history (or NULL on first deploy
    when nothing has been edited yet).
    """
    sql = text("""
        SELECT
            k.service AS service,
            COUNT(k.id) AS key_count,
            COUNT(CASE WHEN k.restart_required THEN 1 END) AS restart_required_count,
            MAX(h.changed_at) AS last_changed
        FROM config_keys k
        LEFT JOIN config_history h ON h.config_key_id = k.id
        GROUP BY k.service
        ORDER BY k.service
    """)
    result = await session.execute(sql)
    services = []
    for row in result.mappings().all():
        services.append({
            "service": row["service"],
            "key_count": int(row["key_count"]),
            "restart_required_count": int(row["restart_required_count"] or 0),
            "last_changed": row["last_changed"].isoformat() if row["last_changed"] else None,
        })
    return {"services": services}


# ─── GET /v58/config?service=engine ──────────────────────────────────────────


@router.get("")
async def get_config_for_service(
    service: str = Query(..., description="service id (engine, margin_engine, ...)"),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Return all config_keys for a service, joined with their current
    config_values row (if any), grouped by category.

    Response shape:
        {
            "service": "engine",
            "key_count": 111,
            "categories": [
                {
                    "id": "sizing",
                    "key_count": 13,
                    "keys": [ { ... }, ... ]
                },
                ...
            ]
        }
    """
    sql = text("""
        SELECT
            k.id AS id,
            k.service AS service,
            k.key AS key,
            k.type AS type,
            k.category AS category,
            k.description AS description,
            k.default_value AS default_value,
            k.restart_required AS restart_required,
            k.editable_via_ui AS editable_via_ui,
            k.enum_values AS enum_values,
            k.min_value AS min_value,
            k.max_value AS max_value,
            v.value AS current_value_raw,
            v.set_by AS set_by,
            v.set_at AS set_at
        FROM config_keys k
        LEFT JOIN config_values v
               ON v.config_key_id = k.id
              AND v.is_active = TRUE
        WHERE k.service = :service
        ORDER BY k.category, k.key
    """)
    result = await session.execute(sql, {"service": service})
    rows = [dict(r) for r in result.mappings().all()]

    if not rows:
        # Don't 404 — return an empty service so the UI can render a blank
        # tab without erroring out. The /services endpoint is the source of
        # truth for which services exist.
        return {"service": service, "key_count": 0, "categories": []}

    # Group by category
    by_category: dict[str, list[dict]] = {}
    for r in rows:
        cat = r.get("category") or "uncategorized"
        by_category.setdefault(cat, []).append(_row_to_key_dict(r))

    categories = [
        {
            "id": cat_id,
            "key_count": len(keys),
            "keys": keys,
        }
        for cat_id, keys in sorted(by_category.items())
    ]

    return {
        "service": service,
        "key_count": len(rows),
        "categories": categories,
    }


# ─── GET /v58/config/schema?service=engine ───────────────────────────────────


@router.get("/schema")
async def get_schema_for_service(
    service: str = Query(..., description="service id"),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Return only the schema (no current values) for a service.

    Same shape as the per-service endpoint but the keys' current_value
    is always null. Used by the frontend to populate edit-form widgets
    before any user interaction.
    """
    sql = text("""
        SELECT
            k.id AS id,
            k.service AS service,
            k.key AS key,
            k.type AS type,
            k.category AS category,
            k.description AS description,
            k.default_value AS default_value,
            k.restart_required AS restart_required,
            k.editable_via_ui AS editable_via_ui,
            k.enum_values AS enum_values,
            k.min_value AS min_value,
            k.max_value AS max_value,
            NULL AS current_value_raw,
            NULL AS set_by,
            NULL AS set_at
        FROM config_keys k
        WHERE k.service = :service
        ORDER BY k.category, k.key
    """)
    result = await session.execute(sql, {"service": service})
    rows = [dict(r) for r in result.mappings().all()]

    keys = [_row_to_key_dict(r) for r in rows]
    return {
        "service": service,
        "key_count": len(keys),
        "keys": keys,
    }


# ─── GET /v58/config/history?service=...&key=...&limit=50 ────────────────────


@router.get("/history")
async def get_history_for_key(
    service: str = Query(..., description="service id"),
    key: str = Query(..., description="config key name"),
    limit: int = Query(50, ge=1, le=500, description="max history rows"),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Return the last N rows from config_history for a specific key.

    Empty in this PR — nothing has written to config_values yet (writes
    ship in CFG-04). Returns the resolved key metadata even if history
    is empty so the frontend drawer can render the header.
    """
    # First look up the key id + metadata
    key_sql = text("""
        SELECT id, service, key, type, default_value, description,
               category, restart_required
        FROM config_keys
        WHERE service = :service AND key = :key
    """)
    key_result = await session.execute(key_sql, {"service": service, "key": key})
    key_row = key_result.mappings().first()

    if not key_row:
        raise HTTPException(
            status_code=404,
            detail=f"unknown config key: {service}.{key}",
        )

    history_sql = text("""
        SELECT id, previous_value, new_value, changed_by, changed_at, comment
        FROM config_history
        WHERE config_key_id = :key_id
        ORDER BY changed_at DESC
        LIMIT :limit
    """)
    history_result = await session.execute(
        history_sql,
        {"key_id": key_row["id"], "limit": limit},
    )
    history = []
    for h in history_result.mappings().all():
        history.append({
            "id": int(h["id"]),
            "previous_value": h["previous_value"],
            "new_value": h["new_value"],
            "changed_by": h["changed_by"],
            "changed_at": h["changed_at"].isoformat() if h["changed_at"] else None,
            "comment": h["comment"],
        })

    return {
        "service": service,
        "key": key,
        "type": key_row["type"],
        "default_value": key_row["default_value"],
        "category": key_row["category"],
        "description": key_row["description"],
        "restart_required": bool(key_row["restart_required"]),
        "history": history,
    }


# ─── POST /v58/config — placeholder, returns 501 ─────────────────────────────


@router.post("")
async def post_config_not_implemented(
    user: TokenData = Depends(get_current_user),
):
    """Write endpoints are not implemented in this PR.

    Returns 501 Not Implemented with a message pointing at CFG-04 (which
    will ship POST /v58/config/upsert, /rollback, /reset). The route is
    defined here so any operator who reads the OpenAPI doc sees a clear
    'coming soon' instead of a 404.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "config write endpoints are not implemented in CFG-02/03. "
            "Writes ship in CFG-04 (POST /api/v58/config/upsert, /rollback, /reset). "
            "See docs/CONFIG_MIGRATION_PLAN.md §7.2 for the planned write API surface."
        ),
    )
