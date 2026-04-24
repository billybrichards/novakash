"""Strategy Runtime Override API — audit #291.

Thin CRUD over the ``strategy_runtime_overrides`` table. Lets an
operator flip a strategy GHOST↔LIVE (or tune a gate param) without
a YAML PR + rsync + engine restart. The engine reads the same table
on a 30s timer (see ``engine/strategies/runtime_override.py``) and
applies the override at the top of its per-strategy evaluate loop,
so a PATCH here lands within ~35s end-to-end.

Governance: endpoints are JWT-gated identically to ``/api/system/*``.
Single-operator model (``ADMIN_USERNAME``) — no multi-user RBAC yet.
Every mutation captures the authenticated username into ``updated_by``
and requires a free-form ``updated_reason`` so the audit trail on
"why is v8_champion LIVE right now?" is answerable from the row
metadata alone.

Non-goals (deliberately not in this PR):

* No WebSocket broadcast on change — the engine's 30s poll is the
  contract. Instant propagation is follow-up work once the hub's
  ``/ws/feed`` pattern supports auth-gated admin channels.
* No auto-revert / schedule-off — overrides persist until DELETEd.
* No YAML hot-reload — this is an override LAYER. To change the
  YAML baseline the operator still does a YAML PR + redeploy.
* No per-conviction-tier Kelly override surface — ``params`` JSONB
  is strictly a shallow-merge of gate_params keys.

Routes (mounted at ``/api`` in ``hub/main.py``):

* ``GET /api/strategies/{strategy_id}/override`` — current override row
  or 404 if absent.
* ``PATCH /api/strategies/{strategy_id}/override`` — upsert with
  ``{mode, params, updated_reason}``. 400 on invalid mode.
* ``DELETE /api/strategies/{strategy_id}/override`` — remove row
  (reverts to YAML baseline). 404 if no row present.
* ``GET /api/strategies/overrides`` — list all active overrides
  (read-only, handy for the FE admin page).
"""

from __future__ import annotations

import json
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session

log = structlog.get_logger(__name__)
router = APIRouter()


# ─── Pydantic models ─────────────────────────────────────────────────────────


VALID_MODES: set[str] = {"LIVE", "GHOST", "DISABLED"}


class OverridePatch(BaseModel):
    """PATCH body. Any field may be omitted; omitted fields are left
    at their current DB value (or NULL on first write).

    ``mode`` of None means "clear the mode override — inherit YAML".
    Same semantics for ``params``. Passing the JSON literal ``null``
    is the canonical way to clear a sub-field without deleting the
    whole row.
    """
    mode: Optional[str] = Field(
        default=None,
        description="LIVE | GHOST | DISABLED, or null to inherit YAML mode",
    )
    params: Optional[dict[str, Any]] = Field(
        default=None,
        description="Partial gate_params override; shallow-merged on top of YAML",
    )
    updated_reason: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Operator-supplied free-form note; required for audit trail",
    )


class OverrideResponse(BaseModel):
    strategy_id: str
    mode: Optional[str]
    params: Optional[dict[str, Any]]
    updated_at: Optional[str]
    updated_by: Optional[str]
    updated_reason: Optional[str]


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _row_to_response(row: dict) -> dict:
    """Normalise a DB row (possibly containing datetime, bytes) into the
    JSON shape we return. Called for both GET and the response body of
    PATCH so the FE sees identical shapes across verbs.
    """
    raw_params = row.get("params")
    if raw_params is None:
        params = None
    elif isinstance(raw_params, dict):
        params = raw_params
    elif isinstance(raw_params, (str, bytes)):
        try:
            decoded = json.loads(raw_params)
            params = decoded if isinstance(decoded, dict) else None
        except Exception:
            params = None
    else:
        params = None

    updated_at = row.get("updated_at")
    return {
        "strategy_id": row["strategy_id"],
        "mode": row.get("mode"),
        "params": params,
        "updated_at": updated_at.isoformat() if updated_at is not None else None,
        "updated_by": row.get("updated_by"),
        "updated_reason": row.get("updated_reason"),
    }


def _validate_mode(mode: Optional[str]) -> None:
    """Reject invalid modes with 400 before hitting the DB CHECK constraint.

    Produces a nicer error than the raw psycopg / asyncpg constraint
    violation message and keeps the validation source-of-truth colocated
    with the Pydantic model.
    """
    if mode is None:
        return
    if mode not in VALID_MODES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid mode '{mode}'. Must be one of "
                f"{sorted(VALID_MODES)} or null."
            ),
        )


# ─── Routes ──────────────────────────────────────────────────────────────────


@router.get("/strategies/overrides")
async def list_overrides(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict[str, dict[str, Any]]:
    """List every active runtime override, keyed by strategy_id.

    Response is a bare map (no envelope) to match the shape of
    ``GET /api/strategies`` — the FE's ``useApiLoader`` unwraps
    ``rows/trades/decisions/items`` arrays but leaves bare maps alone.
    """
    result = await session.execute(
        text(
            """
            SELECT strategy_id, mode, params, updated_at, updated_by, updated_reason
            FROM strategy_runtime_overrides
            ORDER BY updated_at DESC
            """
        )
    )
    rows = [dict(r) for r in result.mappings().all()]
    return {r["strategy_id"]: _row_to_response(r) for r in rows}


@router.get("/strategies/{strategy_id}/override")
async def get_override(
    strategy_id: str,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """Return the current override row, 404 if no override is set."""
    result = await session.execute(
        text(
            """
            SELECT strategy_id, mode, params, updated_at, updated_by, updated_reason
            FROM strategy_runtime_overrides
            WHERE strategy_id = :sid
            """
        ),
        {"sid": strategy_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No runtime override for strategy '{strategy_id}'",
        )
    return _row_to_response(dict(row))


@router.patch("/strategies/{strategy_id}/override")
async def upsert_override(
    strategy_id: str,
    body: OverridePatch,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """Upsert a runtime override row.

    The write is a classic Postgres ``ON CONFLICT DO UPDATE`` — atomic,
    returns the post-write row. ``updated_by`` is recorded from the
    JWT subject, NOT from the request body (clients cannot spoof the
    audit trail).

    ``mode`` and ``params`` in the body are applied as-is (null =
    "clear that field"). If both are null, the row still exists but
    the engine treats it as a no-op; operators wanting to fully revert
    should call DELETE.
    """
    _validate_mode(body.mode)

    params_json = json.dumps(body.params) if body.params is not None else None

    result = await session.execute(
        text(
            """
            INSERT INTO strategy_runtime_overrides
                (strategy_id, mode, params, updated_at, updated_by, updated_reason)
            VALUES (:sid, :mode, CAST(:params AS jsonb), NOW(), :user, :reason)
            ON CONFLICT (strategy_id) DO UPDATE SET
                mode           = EXCLUDED.mode,
                params         = EXCLUDED.params,
                updated_at     = NOW(),
                updated_by     = EXCLUDED.updated_by,
                updated_reason = EXCLUDED.updated_reason
            RETURNING strategy_id, mode, params, updated_at, updated_by, updated_reason
            """
        ),
        {
            "sid": strategy_id,
            "mode": body.mode,
            "params": params_json,
            "user": user.username,
            "reason": body.updated_reason,
        },
    )
    row = result.mappings().first()
    await session.commit()

    log.info(
        "strategy_runtime_override.upsert",
        strategy_id=strategy_id,
        mode=body.mode,
        params_keys=list(body.params.keys()) if body.params else [],
        updated_by=user.username,
        reason=body.updated_reason[:80],
    )
    return _row_to_response(dict(row))


@router.delete("/strategies/{strategy_id}/override")
async def delete_override(
    strategy_id: str,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """Remove the override row — strategy reverts to YAML baseline
    on the engine's next cache refresh.

    Returns 404 if no row exists; this is a safer default than 204
    because the UI flow "click revert, something doesn't revert" is
    easier to surface with an explicit not-found.
    """
    result = await session.execute(
        text(
            """
            DELETE FROM strategy_runtime_overrides
            WHERE strategy_id = :sid
            RETURNING strategy_id
            """
        ),
        {"sid": strategy_id},
    )
    deleted = result.mappings().first()
    await session.commit()

    if deleted is None:
        raise HTTPException(
            status_code=404,
            detail=f"No runtime override for strategy '{strategy_id}'",
        )

    log.info(
        "strategy_runtime_override.delete",
        strategy_id=strategy_id,
        updated_by=user.username,
    )
    return {"strategy_id": strategy_id, "deleted": True}
