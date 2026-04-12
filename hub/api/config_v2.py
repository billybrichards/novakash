"""
CFG-03/04 — Hub API for the new DB-backed config tables.

Mounts under /api/v58/config/*. CFG-03 shipped the read endpoints;
CFG-04 adds the write surface (upsert / rollback / reset).

Read endpoints (CFG-03):
  GET  /api/v58/config/services             list of services + key counts
  GET  /api/v58/config?service=engine       all keys for a service with values
  GET  /api/v58/config/schema?service=...   key metadata only (no values)
  GET  /api/v58/config/history?service=...&key=...
                                            last N changes for a key

Write endpoints (CFG-04):
  POST /api/v58/config/upsert               set a config value
  POST /api/v58/config/rollback             roll back to a history entry
  POST /api/v58/config/reset                reset to default (delete override)

The router lives in a new file (config_v2.py) to keep it isolated from the
existing hub/api/config.py mini-API, which will be retired in CFG-11.

Auth: every endpoint takes Depends(get_current_user) — same JWT auth wall
the rest of the hub uses. CFG-06 will add an admin-claim check on the
write endpoints; v1 is open to any authenticated user.
"""

from __future__ import annotations

from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
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
        # enum / string / csv -> leave as the source string
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
        # Don't 404 -- return an empty service so the UI can render a blank
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
    """Return the last N rows from config_history for a specific key."""
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


# ─── Write helpers ───────────────────────────────────────────────────────────


# Type-validation map.  DB stores everything as TEXT; we validate the
# *incoming* string can be parsed as the declared type before persisting.
_VALID_BOOL_STRINGS = {"true", "false", "1", "0", "yes", "no", "on", "off"}


def _validate_value_for_type(value: str, value_type: str, enum_values: Any = None) -> str:
    """Validate *value* against *value_type* and return the normalised TEXT
    representation to persist.  Raises ValueError on mismatch."""
    vt = (value_type or "string").lower()
    if vt == "bool":
        if value.strip().lower() not in _VALID_BOOL_STRINGS:
            raise ValueError(f"expected bool, got {value!r}")
        return value.strip().lower()
    if vt == "int":
        int(value)  # raises ValueError on bad input
        return value.strip()
    if vt == "float":
        float(value)  # raises ValueError on bad input
        return value.strip()
    if vt == "enum":
        allowed = enum_values or []
        if isinstance(allowed, str):
            allowed = [s.strip() for s in allowed.split(",")]
        if value not in allowed:
            raise ValueError(f"expected one of {allowed}, got {value!r}")
        return value
    # string / csv / anything else -- pass through
    return value


async def _resolve_config_key(
    session: AsyncSession, service: str, key: str,
) -> dict:
    """Look up a config_keys row.  Raises HTTPException(404) if not found."""
    sql = text("""
        SELECT id, service, key, type, default_value, description,
               category, restart_required, editable_via_ui, enum_values
        FROM config_keys
        WHERE service = :service AND key = :key
    """)
    result = await session.execute(sql, {"service": service, "key": key})
    row = result.mappings().first()
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"unknown config key: {service}.{key}",
        )
    return dict(row)


async def _get_current_value(session: AsyncSession, key_id: int) -> Optional[str]:
    """Return the current active value for a key, or None if unset."""
    sql = text("""
        SELECT value FROM config_values
        WHERE config_key_id = :key_id AND is_active = TRUE
    """)
    result = await session.execute(sql, {"key_id": key_id})
    row = result.mappings().first()
    return row["value"] if row else None


async def _write_config_value(
    session: AsyncSession,
    key_id: int,
    new_value: Optional[str],
    previous_value: Optional[str],
    set_by: str,
    comment: str,
    *,
    delete: bool = False,
) -> dict:
    """Atomically update config_values and append to config_history.

    When delete=True, removes the active row (reset to default).
    Otherwise, upserts the active row.

    Returns the newly inserted config_history row as a dict.
    """
    if delete:
        await session.execute(
            text("DELETE FROM config_values WHERE config_key_id = :key_id AND is_active = TRUE"),
            {"key_id": key_id},
        )
    else:
        # Deactivate any existing active row, then insert a new one.
        # The DEFERRABLE UNIQUE constraint on (config_key_id, is_active)
        # allows this within a single transaction.
        await session.execute(
            text("UPDATE config_values SET is_active = FALSE WHERE config_key_id = :key_id AND is_active = TRUE"),
            {"key_id": key_id},
        )
        await session.execute(
            text("""
                INSERT INTO config_values (config_key_id, value, set_by, set_at, is_active)
                VALUES (:key_id, :value, :set_by, NOW(), TRUE)
            """),
            {"key_id": key_id, "value": new_value, "set_by": set_by},
        )

    # Append history -- always, even for deletes/resets.
    history_sql = text("""
        INSERT INTO config_history (config_key_id, previous_value, new_value, changed_by, changed_at, comment)
        VALUES (:key_id, :prev, :new, :changed_by, NOW(), :comment)
        RETURNING id, previous_value, new_value, changed_by, changed_at, comment
    """)
    h_result = await session.execute(history_sql, {
        "key_id": key_id,
        "prev": previous_value,
        "new": new_value,
        "changed_by": set_by,
        "comment": comment,
    })
    h_row = h_result.mappings().first()

    await session.commit()

    return {
        "id": int(h_row["id"]),
        "previous_value": h_row["previous_value"],
        "new_value": h_row["new_value"],
        "changed_by": h_row["changed_by"],
        "changed_at": h_row["changed_at"].isoformat() if h_row["changed_at"] else None,
        "comment": h_row["comment"],
    }


# ─── Request models ──────────────────────────────────────────────────────────


class UpsertRequest(BaseModel):
    service: str = Field(..., description="service id (engine, margin_engine, ...)")
    key: str = Field(..., description="config key name")
    value: str = Field(..., description="new value as a string")
    reason: str = Field("", description="human-readable reason for the change")


class RollbackRequest(BaseModel):
    service: str = Field(..., description="service id")
    key: str = Field(..., description="config key name")
    history_id: int = Field(..., description="config_history.id to roll back to")


class ResetRequest(BaseModel):
    service: str = Field(..., description="service id")
    key: str = Field(..., description="config key name")


# ─── POST /v58/config/upsert ────────────────────────────────────────────────


@router.post("/upsert")
async def upsert_config(
    body: UpsertRequest,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Set (or update) a config value.

    1. Validates the key exists in config_keys.
    2. Validates the value type matches config_keys.type.
    3. Upserts config_values (deactivate old, insert new active row).
    4. Appends to config_history.
    5. Returns the updated value + history entry.
    """
    key_row = await _resolve_config_key(session, body.service, body.key)
    key_id = key_row["id"]

    # Type validation
    try:
        normalised = _validate_value_for_type(body.value, key_row["type"], key_row.get("enum_values"))
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"type validation failed for {body.service}.{body.key}: {exc}",
        )

    previous = await _get_current_value(session, key_id)

    history_entry = await _write_config_value(
        session,
        key_id=key_id,
        new_value=normalised,
        previous_value=previous,
        set_by=user.username,
        comment=body.reason or f"set via API by {user.username}",
    )

    return {
        "service": body.service,
        "key": body.key,
        "previous_value": _coerce_value(previous, key_row["type"]),
        "current_value": _coerce_value(normalised, key_row["type"]),
        "current_value_raw": normalised,
        "default_value": _coerce_value(key_row["default_value"], key_row["type"]),
        "type": key_row["type"],
        "history_entry": history_entry,
    }


# ─── POST /v58/config/rollback ──────────────────────────────────────────────


@router.post("/rollback")
async def rollback_config(
    body: RollbackRequest,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Roll back a config value to a previous state from config_history.

    1. Finds the history entry by ID.
    2. Sets config_values.current_value to the history entry's previous_value.
    3. Appends a new history entry with reason "rollback to history_id=N".
    4. Returns the rolled-back value.
    """
    key_row = await _resolve_config_key(session, body.service, body.key)
    key_id = key_row["id"]

    # Look up the target history entry
    h_sql = text("""
        SELECT id, previous_value, new_value, config_key_id
        FROM config_history
        WHERE id = :history_id
    """)
    h_result = await session.execute(h_sql, {"history_id": body.history_id})
    h_row = h_result.mappings().first()

    if not h_row:
        raise HTTPException(
            status_code=404,
            detail=f"config_history entry id={body.history_id} not found",
        )
    if h_row["config_key_id"] != key_id:
        raise HTTPException(
            status_code=422,
            detail=(
                f"history entry id={body.history_id} belongs to a different key "
                f"(key_id={h_row['config_key_id']}), not {body.service}.{body.key}"
            ),
        )

    rollback_to = h_row["previous_value"]
    current = await _get_current_value(session, key_id)
    comment = f"rollback to history_id={body.history_id}"

    if rollback_to is None:
        # Rolling back to "no override" state -- delete the active row
        history_entry = await _write_config_value(
            session,
            key_id=key_id,
            new_value=None,
            previous_value=current,
            set_by=user.username,
            comment=comment,
            delete=True,
        )
    else:
        history_entry = await _write_config_value(
            session,
            key_id=key_id,
            new_value=rollback_to,
            previous_value=current,
            set_by=user.username,
            comment=comment,
        )

    effective = rollback_to if rollback_to is not None else key_row["default_value"]
    return {
        "service": body.service,
        "key": body.key,
        "rolled_back_to_value": _coerce_value(effective, key_row["type"]),
        "rolled_back_to_value_raw": effective,
        "default_value": _coerce_value(key_row["default_value"], key_row["type"]),
        "type": key_row["type"],
        "history_entry": history_entry,
    }


# ─── POST /v58/config/reset ─────────────────────────────────────────────────


@router.post("/reset")
async def reset_config(
    body: ResetRequest,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Reset a config value to its default by deleting the config_values row.

    1. Deletes the active row from config_values.
    2. Appends a history entry with reason "reset to default".
    3. Returns the default value from config_keys.
    """
    key_row = await _resolve_config_key(session, body.service, body.key)
    key_id = key_row["id"]

    current = await _get_current_value(session, key_id)

    history_entry = await _write_config_value(
        session,
        key_id=key_id,
        new_value=None,
        previous_value=current,
        set_by=user.username,
        comment="reset to default",
        delete=True,
    )

    return {
        "service": body.service,
        "key": body.key,
        "current_value": _coerce_value(key_row["default_value"], key_row["type"]),
        "current_value_raw": key_row["default_value"],
        "is_default": True,
        "type": key_row["type"],
        "history_entry": history_entry,
    }


# ─── POST /v58/config -- legacy stub, redirects to specific endpoints ───────


@router.post("")
async def post_config_redirect(
    user: TokenData = Depends(get_current_user),
):
    """The generic POST endpoint now redirects callers to the specific
    write endpoints added in CFG-04."""
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            "Use the specific write endpoints: "
            "POST /api/v58/config/upsert, "
            "POST /api/v58/config/rollback, or "
            "POST /api/v58/config/reset."
        ),
    )
