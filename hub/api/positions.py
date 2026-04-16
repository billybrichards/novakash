"""
Positions snapshot — read-only view consumed by the Telegram page top bar.

Mirrors the dict shape of engine.alerts.positions.build_snapshot() so the
frontend can drive its top-bar from either source (engine push or hub
pull) with the same renderer.

Source-of-truth tables (all written by the engine):
  - poly_wallet_balance   — latest USDC reading
  - poly_pending_wins     — engine writes this every redeemer sweep (Task 9)
  - redeemer_state        — engine writes cooldown + quota every loop (Task 9)

Defensive design: each table query is wrapped in try/except. If a table
doesn't exist yet (Task 9 hasn't shipped, fresh DB, etc.) the endpoint
returns sensible defaults (zeros + empty list) instead of a 500. This
lets the Telegram page render TODAY without waiting on the engine-side
schema migration.

JWT auth required, consistent with the rest of /api/* in the hub.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/positions", tags=["positions"])


def _is_missing_table_error(exc: Exception) -> bool:
    """True when the underlying driver says 'relation does not exist'.

    Postgres (asyncpg) returns `UndefinedTableError`, wrapped by SQLAlchemy
    as `ProgrammingError` with pgcode '42P01'. Matching on pgcode is more
    precise than string-sniffing and survives asyncpg version changes.
    """
    if not isinstance(exc, ProgrammingError):
        return False
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate == "42P01"


@router.get("/snapshot")
async def get_snapshot(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return a single dict capturing the live position state for the TG top bar.

    Shape (13 keys):
      now_utc            — ISO timestamp the snapshot was built at
      wallet_usdc        — latest USDC balance from poly_wallet_balance
      pending_wins       — list of {condition_id, value, window_end_utc, overdue_seconds}
      pending_count      — len(pending_wins)
      pending_total_usd  — sum of pending_wins[].value
      overdue_count      — pending wins where window_end_utc was > 5 min ago
      effective_balance  — wallet_usdc + pending_total_usd
      open_orders        — placeholder [] (open-orders enrichment is out of scope)
      open_orders_count  — 0
      cooldown           — {active, remaining_seconds, resets_at, reason}
      daily_quota_limit  — daily redemption-call budget
      quota_used_today   — calls used so far today
      quota_remaining    — max(0, limit - used)
    """
    # Track which source tables are absent so the frontend can surface
    # "migration not applied" visibly instead of rendering all-zeros as if
    # the system were healthy. (TimesFM incident 2026-04-16 taught us:
    # silent fallback indistinguishable from healthy state = hidden outage.)
    missing_tables: list[str] = []

    # ─── Wallet ──────────────────────────────────────────────────────────────
    wallet_usdc = 0.0
    try:
        wallet_row = (
            await session.execute(
                text(
                    "SELECT usdc_balance FROM poly_wallet_balance "
                    "ORDER BY observed_at DESC LIMIT 1"
                )
            )
        ).mappings().first()
        if wallet_row:
            wallet_usdc = float(wallet_row["usdc_balance"])
    except Exception as exc:  # noqa: BLE001
        if _is_missing_table_error(exc):
            missing_tables.append("poly_wallet_balance")
            log.warning("positions.wallet_table_missing")
        else:
            log.error(
                "positions.wallet_query_failed", error=str(exc)[:200]
            )
            raise  # non-schema DB errors must propagate to a 500

    # ─── Pending wins (Task 9 will create this table) ────────────────────────
    pending: list[dict] = []
    try:
        pending_rows = (
            await session.execute(
                text(
                    "SELECT condition_id, value, window_end_utc, "
                    "  EXTRACT(EPOCH FROM (NOW() - window_end_utc))::int "
                    "    AS overdue_seconds "
                    "FROM poly_pending_wins "
                    "ORDER BY window_end_utc ASC"
                )
            )
        ).mappings().all()
        pending = [_serialise_pending_row(r) for r in pending_rows]
    except Exception as exc:  # noqa: BLE001
        if _is_missing_table_error(exc):
            missing_tables.append("poly_pending_wins")
            log.warning("positions.pending_table_missing")
        else:
            log.error("positions.pending_query_failed", error=str(exc)[:200])
            raise

    pending_total = round(sum(float(r["value"]) for r in pending), 2)
    overdue_count = sum(
        1 for r in pending if int(r.get("overdue_seconds") or 0) > 300
    )

    # ─── Cooldown + daily quota (Task 9 will create this table) ──────────────
    rs: dict = {}
    try:
        rs_row = (
            await session.execute(
                text(
                    "SELECT cooldown_active, cooldown_remaining_seconds, "
                    "  cooldown_resets_at, cooldown_reason, "
                    "  daily_quota_limit, quota_used_today "
                    "FROM redeemer_state "
                    "ORDER BY observed_at DESC LIMIT 1"
                )
            )
        ).mappings().first()
        if rs_row:
            rs = dict(rs_row)
    except Exception as exc:  # noqa: BLE001
        if _is_missing_table_error(exc):
            missing_tables.append("redeemer_state")
            log.warning("positions.redeemer_state_table_missing")
        else:
            log.error(
                "positions.redeemer_state_query_failed", error=str(exc)[:200]
            )
            raise

    resets_at_raw = rs.get("cooldown_resets_at")
    cooldown = {
        "active": bool(rs.get("cooldown_active")),
        "remaining_seconds": int(rs.get("cooldown_remaining_seconds") or 0),
        "resets_at": resets_at_raw.isoformat()
        if hasattr(resets_at_raw, "isoformat")
        else None,
        "reason": rs.get("cooldown_reason") or "",
    }
    daily_quota_limit = int(rs.get("daily_quota_limit") or 100)
    quota_used_today = int(rs.get("quota_used_today") or 0)
    quota_remaining = max(0, daily_quota_limit - quota_used_today)

    return {
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "wallet_usdc": round(wallet_usdc, 2),
        "pending_wins": pending,
        "pending_count": len(pending),
        "pending_total_usd": pending_total,
        "overdue_count": overdue_count,
        "effective_balance": round(wallet_usdc + pending_total, 2),
        "open_orders": [],          # TODO: open-orders enrichment out of scope
        "open_orders_count": 0,
        "cooldown": cooldown,
        "daily_quota_limit": daily_quota_limit,
        "quota_used_today": quota_used_today,
        "quota_remaining": quota_remaining,
        # _meta carries "the data you see is NOT trustworthy because X".
        # Frontend reads _meta.missing_tables to show a red "migration
        # missing" banner instead of pretending the all-zeros payload is
        # healthy. data_stale=true whenever ANY source table is missing.
        "_meta": {
            "missing_tables": missing_tables,
            "data_stale": len(missing_tables) > 0,
        },
    }


def _serialise_pending_row(row) -> dict:
    """Convert a SQLAlchemy mapping into a JSON-safe dict.

    `window_end_utc` arrives as a datetime — render as ISO so JSON
    serialisation succeeds. `overdue_seconds` may already be int (from
    EXTRACT(EPOCH ...)::int) but coerce defensively.
    """
    d = dict(row)
    val = d.get("window_end_utc")
    if hasattr(val, "isoformat"):
        d["window_end_utc"] = val.isoformat()
    if d.get("value") is not None:
        d["value"] = float(d["value"])
    if d.get("overdue_seconds") is not None:
        d["overdue_seconds"] = int(d["overdue_seconds"])
    return d
