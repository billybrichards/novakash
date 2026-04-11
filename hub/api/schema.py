"""
Schema API — DB catalog + live runtime stats for the /schema page (SCHEMA-01).

Exposes the hand-curated SCHEMA_CATALOG joined with live Postgres metadata
(column definitions, row count, last write timestamp) so the frontend can
render the full inventory of tables.

Endpoints (all JWT-protected):
  GET /api/v58/schema/tables                 — summary list of all tables
  GET /api/v58/schema/tables/{table_name}    — full detail for one table
  GET /api/v58/schema/summary                — header stats (N total, N active, etc.)

Design notes
------------

* The catalog is the authoritative list — we do NOT auto-discover tables
  from pg_catalog. A table that isn't in SCHEMA_CATALOG is not shown.

* Live runtime data is joined on-demand from the DB the hub is connected
  to:
    - columns → information_schema.columns
    - row count → pg_class.reltuples for tables marked `large`, else
      SELECT COUNT(*) bounded to ~100ms via statement_timeout
    - last write → MAX(recency_column) if the catalog entry has one
    - exists? → information_schema.tables

* Tables that exist in the catalog but not in the hub's DB (planned tables,
  or tables in a different service's DB like ticks_v3_composite) are
  returned with exists=False and the catalog metadata only. The frontend
  shows them with an "External / planned" badge.

* Tables that exist in the hub's DB but aren't in the catalog are simply
  ignored — by design, per the CLAUDE.md scope note.

* This file is DATA-ONLY — no side effects, no migrations, read-only
  queries against information_schema and the user tables themselves.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session
from db.schema_catalog import (
    GATES_CATALOG,
    SCHEMA_CATALOG,
    gates_by_table,
    list_categories,
    list_engines,
    list_services,
    status_breakdown,
    tables_for_gate,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v58/schema", tags=["schema"])


# ─── Helpers ───────────────────────────────────────────────────────────────

# A plausible identifier — used to defend against schema catalog typos
# that could otherwise be fed as-is into raw SQL. All lookups go through
# parameterised queries, but the table name can't be parameterised in
# `SELECT COUNT(*) FROM {table}` so we need an allowlist check too.
def _is_safe_identifier(name: str) -> bool:
    if not name or len(name) > 63:
        return False
    # Postgres identifiers: letters, digits, underscore.
    for c in name:
        if not (c.isalnum() or c == "_"):
            return False
    return True


def _ts_iso(val: Any) -> Optional[str]:
    """Convert datetime / epoch / int → ISO string."""
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.isoformat()
    # Some recency columns store Unix epoch seconds (window_ts, ts BIGINT).
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(val, tz=timezone.utc).isoformat()
        except (OverflowError, ValueError, OSError):
            return str(val)
    return str(val)


async def _table_exists(session: AsyncSession, table_name: str) -> bool:
    """Check if a table is present in the connected DB."""
    q = text("""
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = :t
        LIMIT 1
    """)
    res = await session.execute(q, {"t": table_name})
    return res.first() is not None


async def _get_columns(session: AsyncSession, table_name: str) -> list[dict]:
    """Return column metadata for the given table."""
    q = text("""
        SELECT column_name, data_type, is_nullable, column_default,
               character_maximum_length, numeric_precision, numeric_scale
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :t
        ORDER BY ordinal_position
    """)
    res = await session.execute(q, {"t": table_name})
    cols: list[dict] = []
    for row in res.mappings().all():
        cols.append({
            "name": row["column_name"],
            "type": row["data_type"],
            "nullable": (row["is_nullable"] == "YES"),
            "default": row["column_default"],
            "max_length": row["character_maximum_length"],
            "numeric_precision": row["numeric_precision"],
            "numeric_scale": row["numeric_scale"],
        })
    return cols


async def _get_row_count(
    session: AsyncSession,
    table_name: str,
    large: bool,
) -> Optional[int]:
    """
    Return row count for a table.

    For large tables, use pg_class.reltuples (fast catalog estimate) so
    we never block on a multi-million-row COUNT(*). For small tables,
    do a real COUNT(*) under a short statement_timeout guard so an
    unexpectedly-big table just returns None instead of hanging the
    endpoint.
    """
    if not _is_safe_identifier(table_name):
        return None

    if large:
        q = text("""
            SELECT reltuples::BIGINT AS n
            FROM pg_class
            WHERE relname = :t AND relkind = 'r'
            LIMIT 1
        """)
        try:
            res = await session.execute(q, {"t": table_name})
            row = res.first()
            if row is None:
                return None
            # reltuples is an estimate; it may be 0 on fresh tables before
            # the autovacuum analyser has run. Treat -1 / 0 as "unknown"
            # and fall through to a real count if it's clearly empty.
            est = int(row[0] or 0)
            if est > 0:
                return est
            # Zero estimate → empty or unanalysed. Do a quick real count
            # with a tight timeout so we don't block.
        except Exception as exc:
            log.warning("schema.reltuples_failed", table=table_name, error=str(exc))
            return None

    # Real COUNT(*) under a statement timeout. 300ms is plenty for any
    # small table and protects against surprises.
    try:
        await session.execute(text("SET LOCAL statement_timeout = '300ms'"))
        res = await session.execute(
            text(f"SELECT COUNT(*) FROM public.{table_name}")  # safe — identifier validated above
        )
        return int(res.scalar_one() or 0)
    except Exception as exc:
        log.warning(
            "schema.count_failed",
            table=table_name,
            error=str(exc)[:200],
        )
        return None


async def _get_last_write(
    session: AsyncSession,
    table_name: str,
    recency_column: Optional[str],
) -> Optional[str]:
    """
    Return ISO-formatted "last write time" using the catalog's
    recency_column. Returns None if the column is missing or the table
    is empty.
    """
    if not recency_column:
        return None
    if not _is_safe_identifier(table_name) or not _is_safe_identifier(recency_column):
        return None

    try:
        await session.execute(text("SET LOCAL statement_timeout = '300ms'"))
        res = await session.execute(
            text(
                f"SELECT MAX({recency_column}) AS last_ts "  # safe — both validated
                f"FROM public.{table_name}"
            )
        )
        val = res.scalar_one_or_none()
        return _ts_iso(val)
    except Exception as exc:
        log.warning(
            "schema.last_write_failed",
            table=table_name,
            column=recency_column,
            error=str(exc)[:200],
        )
        return None


def _catalog_entry_to_summary(table_name: str, entry: dict) -> dict:
    """Flatten a catalog entry for the summary listing endpoint."""
    return {
        "name": table_name,
        "service": entry.get("service", "unknown"),
        "category": entry.get("category", "uncategorised"),
        "status": entry.get("status", "active"),
        "purpose": entry.get("purpose", ""),
        "writers": entry.get("writers", []),
        "readers": entry.get("readers", []),
        "recency_column": entry.get("recency_column"),
        "docs": entry.get("docs", []),
        "notes": entry.get("notes", ""),
        "large": bool(entry.get("large", False)),
    }


# ─── Endpoints ─────────────────────────────────────────────────────────────

@router.get("/summary")
async def schema_summary(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Header stats for the /schema page. Cheap — no per-table DB hit.

    Returns:
      {
        "generated_at": ISO,
        "total_tables": int,
        "active": int,
        "legacy": int,
        "deprecated": int,
        "categories": [...],
        "services": [...],
      }
    """
    breakdown = status_breakdown()
    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "total_tables": len(SCHEMA_CATALOG),
        "active": breakdown.get("active", 0),
        "legacy": breakdown.get("legacy", 0),
        "deprecated": breakdown.get("deprecated", 0),
        "categories": list_categories(),
        "services": list_services(),
    }


@router.get("/tables")
async def list_tables(
    service: Optional[str] = Query(None, description="Filter by service"),
    status: Optional[str] = Query(
        None,
        pattern="^(active|legacy|deprecated)$",
        description="Filter by status",
    ),
    category: Optional[str] = Query(None, description="Filter by category"),
    include_runtime: bool = Query(
        True,
        description="Whether to include row count + last write time per table",
    ),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    List all tables in the catalog, grouped by service.

    Runtime stats (row_count, last_write) are looked up per-table and
    each lookup is guarded by a short statement timeout, so this endpoint
    is bounded in the low-hundreds of ms even on a large catalog.

    Pass include_runtime=false to skip the runtime hits entirely — useful
    for populating the sidebar on initial page load before the user
    clicks a table.
    """
    tables: list[dict] = []

    for name, entry in SCHEMA_CATALOG.items():
        if service and entry.get("service") != service:
            continue
        if status and entry.get("status") != status:
            continue
        if category and entry.get("category") != category:
            continue

        summary = _catalog_entry_to_summary(name, entry)

        if include_runtime:
            try:
                exists = await _table_exists(session, name)
            except Exception as exc:
                log.warning("schema.exists_check_failed", table=name, error=str(exc)[:200])
                exists = False

            summary["exists"] = exists
            summary["row_count"] = None
            summary["row_count_is_estimate"] = False
            summary["last_write"] = None

            if exists:
                try:
                    rc = await _get_row_count(session, name, summary["large"])
                    summary["row_count"] = rc
                    summary["row_count_is_estimate"] = bool(summary["large"]) and rc is not None
                except Exception as exc:
                    log.warning(
                        "schema.row_count_failed",
                        table=name,
                        error=str(exc)[:200],
                    )
                try:
                    summary["last_write"] = await _get_last_write(
                        session, name, entry.get("recency_column")
                    )
                except Exception as exc:
                    log.warning(
                        "schema.last_write_failed",
                        table=name,
                        error=str(exc)[:200],
                    )
        else:
            summary["exists"] = None
            summary["row_count"] = None
            summary["row_count_is_estimate"] = False
            summary["last_write"] = None

        tables.append(summary)

    # Group by service for the frontend
    by_service: dict[str, list[dict]] = {}
    for t in tables:
        by_service.setdefault(t["service"], []).append(t)

    return {
        "tables": tables,
        "by_service": by_service,
        "total": len(tables),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


@router.get("/tables/{table_name}")
async def get_table(
    table_name: str,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Full detail for one table: catalog metadata + columns + row count +
    last write time.
    """
    if not _is_safe_identifier(table_name):
        raise HTTPException(status_code=400, detail="Invalid table name")

    entry = SCHEMA_CATALOG.get(table_name)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Table {table_name!r} is not in the schema catalog. "
                "If it should be tracked, add an entry to "
                "hub/db/schema_catalog.py."
            ),
        )

    summary = _catalog_entry_to_summary(table_name, entry)

    try:
        exists = await _table_exists(session, table_name)
    except Exception as exc:
        log.warning("schema.exists_check_failed", table=table_name, error=str(exc)[:200])
        exists = False

    columns: list[dict] = []
    row_count: Optional[int] = None
    row_count_is_estimate = False
    last_write: Optional[str] = None

    if exists:
        try:
            columns = await _get_columns(session, table_name)
        except Exception as exc:
            log.warning(
                "schema.get_columns_failed",
                table=table_name,
                error=str(exc)[:200],
            )
        try:
            row_count = await _get_row_count(session, table_name, summary["large"])
            row_count_is_estimate = bool(summary["large"]) and row_count is not None
        except Exception as exc:
            log.warning(
                "schema.row_count_failed",
                table=table_name,
                error=str(exc)[:200],
            )
        try:
            last_write = await _get_last_write(
                session, table_name, entry.get("recency_column")
            )
        except Exception as exc:
            log.warning(
                "schema.last_write_failed",
                table=table_name,
                error=str(exc)[:200],
            )

    return {
        **summary,
        "exists": exists,
        "columns": columns,
        "column_count": len(columns),
        "row_count": row_count,
        "row_count_is_estimate": row_count_is_estimate,
        "last_write": last_write,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


# ─── GATES endpoints (NAV-01 consolidation) ─────────────────────────────────


@router.get("/gates")
async def get_gates(
    user: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Return the GATES_CATALOG as a structured list grouped by engine.

    NAV-01: the user asked for a single place to answer "which gates
    consume which tables". This endpoint returns the hand-curated gates
    inventory (pipeline position, file:line, inputs, outputs, env flags,
    fail reasons, tables read/written, notes) for both trading engines:
    Polymarket 5-minute (V10.6 8-gate pipeline) and margin_engine
    (v4 inline gates, only the ones with standalone status).

    This is static data — no DB query, no side effects. It's served
    from the hub so the frontend can consume it through the same
    authed /api/v58/schema/* namespace.
    """
    items: list[dict[str, Any]] = []
    for key, entry in GATES_CATALOG.items():
        out: dict[str, Any] = {"key": key}
        out.update(entry)
        # Cross-reference: for each table this gate reads from, which
        # other gates also read from it? Useful for "if I change this
        # table, which gates are affected?" queries.
        other_gates_per_table: dict[str, list[str]] = {}
        for table in entry.get("tables_read", []):
            # Strip parenthetical annotations for cleaner matching
            bare = table.split(" (")[0].strip() if " (" in table else table.strip()
            others = [
                g for g in gates_by_table(bare)
                if g != key
            ]
            if others:
                other_gates_per_table[bare] = others
        out["other_gates_by_shared_table"] = other_gates_per_table
        items.append(out)

    engines = list_engines()
    by_engine: dict[str, list[dict[str, Any]]] = {eng: [] for eng in engines}
    for item in items:
        eng = item.get("engine", "unknown")
        by_engine.setdefault(eng, []).append(item)

    return {
        "items": items,
        "by_engine": by_engine,
        "engines": engines,
        "count": len(items),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


@router.get("/gates/by-table/{table_name}")
async def get_gates_by_table(
    table_name: str,
    user: TokenData = Depends(get_current_user),
) -> dict[str, Any]:
    """Return the list of gate keys that read from a given table.

    Used by the /schema page's table detail view to show "which gates
    depend on this table" under each table's expanded card. This is
    the inverse of the /gates endpoint's `tables_read` field.
    """
    if not table_name or not table_name.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="invalid table name")

    matches = gates_by_table(table_name)
    return {
        "table": table_name,
        "gates": [
            {
                "key": g,
                "engine": GATES_CATALOG.get(g, {}).get("engine"),
                "pipeline_position": GATES_CATALOG.get(g, {}).get("pipeline_position"),
                "class_name": GATES_CATALOG.get(g, {}).get("class_name"),
            }
            for g in matches
        ],
        "count": len(matches),
    }
