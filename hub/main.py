"""
BTC-Trader Hub — FastAPI backend.

Provides REST API for the dashboard frontend and a WebSocket endpoint
for real-time event streaming. Connects to the trading engine via
PostgreSQL (reads) and a shared system_state table.
"""

from __future__ import annotations

import structlog
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db.database import init_db, close_db
from auth.routes import router as auth_router
from api.dashboard import router as dashboard_router
from api.trades import router as trades_router
from api.signals import router as signals_router
from api.pnl import router as pnl_router
from api.system import router as system_router
from api.config import router as config_router
from api.backtest import router as backtest_router
from api.setup import router as setup_router
from api.paper import router as paper_router
from api.trading_config import router as trading_config_router
from api.forecast import router as forecast_router
from ws.live_feed import router as ws_router
from api.playwright import router as playwright_router
from api.v58_monitor import router as v58_router
from api.analysis import router as analysis_router
from api.margin import router as margin_router
from api.notes import router as notes_router
from api.audit_tasks import router as audit_tasks_router
from api.schema import router as schema_router

# CFG-02/03: DB-backed config schema + read-only API
from api.config_v2 import router as config_v2_router

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup/shutdown lifecycle."""
    log.info("hub.starting")
    await init_db()

    # ── Startup DDL (idempotent, lock_timeout=5s to prevent hangups) ────────
    try:
        from db.database import get_session
        from db.migrations.startup_ddl import run_startup_migrations

        async for session in get_session():
            await run_startup_migrations(session)
            break
    except Exception as exc:
        log.warning("hub.startup_ddl_error", error=str(exc))

    # ── v58 monitor tables ───────────────────────────────────────────────────
    try:
        from db.database import get_session
        from db.migrations.v58_monitor_ddl import (
            ensure_manual_trades_table,
            ensure_manual_trade_snapshots_table,
        )

        async for session in get_session():
            await ensure_manual_trades_table(session)
            await ensure_manual_trade_snapshots_table(session)
            break
    except Exception as exc:
        log.warning("hub.v58_migration_error", error=str(exc))

    # ── CFG-02: config_keys / config_values / config_history + seed ─────────
    try:
        from db.database import get_session
        from db.config_schema import ensure_config_tables
        from db.config_seed import seed_config_keys

        async for session in get_session():
            await ensure_config_tables(session)
            await session.commit()
            counts = await seed_config_keys(session)
            await session.commit()
            log.info("hub.config_seed_done", per_service=counts)
            break
    except Exception as exc:
        log.warning("hub.config_schema_migration_error", error=str(exc))

    yield
    log.info("hub.stopping")
    await close_db()


app = FastAPI(
    title="BTC-Trader Hub",
    description="Dashboard API for the BTC prediction market trading engine.",
    version="0.1.0",
    lifespan=lifespan,
)

# ─── CORS ────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten in production via env
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Routers ─────────────────────────────────────────────────────────────────

app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(dashboard_router, prefix="/api", tags=["dashboard"])
app.include_router(trades_router, prefix="/api", tags=["trades"])
app.include_router(signals_router, prefix="/api", tags=["signals"])
app.include_router(pnl_router, prefix="/api", tags=["pnl"])
app.include_router(system_router, prefix="/api", tags=["system"])
app.include_router(config_router, prefix="/api", tags=["config"])
app.include_router(backtest_router, prefix="/api", tags=["backtest"])
app.include_router(setup_router, tags=["setup"])
app.include_router(paper_router, prefix="/api", tags=["paper"])
app.include_router(trading_config_router, prefix="/api", tags=["trading-config"])
app.include_router(forecast_router, prefix="/api", tags=["forecast"])
app.include_router(ws_router, tags=["websocket"])
app.include_router(playwright_router, prefix="/api", tags=["playwright"])
app.include_router(v58_router, prefix="/api", tags=["v58-monitor"])
app.include_router(analysis_router, prefix="/api", tags=["analysis"])
app.include_router(margin_router, prefix="/api", tags=["margin"])
app.include_router(notes_router, prefix="/api", tags=["notes"])
app.include_router(audit_tasks_router, prefix="/api", tags=["audit-tasks"])
# SCHEMA-01: /schema page — DB table inventory (catalog + live runtime stats)
app.include_router(schema_router, prefix="/api", tags=["schema"])
# CFG-02/03: DB-backed config (read-only in this PR; writes ship in CFG-04)
app.include_router(config_v2_router, prefix="/api", tags=["config-v2"])


@app.get("/health", tags=["health"])
async def health_check() -> dict:
    """Liveness probe."""
    return {"status": "ok"}


# ── Internal TimesFM proxy (no auth) — for engine on Montreal which cannot
# reach the timesfm service directly due to VPC routing constraints.
# Engine sets TIMESFM_URL=http://<hub-host>:8091 and calls /v4/snapshot.
# Hub forwards to localhost:8080 (co-located timesfm service).
import httpx as _httpx
import os as _os

_TIMESFM_INTERNAL = _os.environ.get("TIMESFM_URL", "http://localhost:8080")


@app.get("/v4/snapshot", tags=["internal-proxy"])
async def proxy_v4_snapshot(
    asset: str = "btc", timescale: str = "5m", strategy: str = "polymarket_5m"
) -> dict:
    """No-auth proxy to timesfm /v4/snapshot. Used by Strategy Engine v2 on Montreal."""
    try:
        async with _httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"{_TIMESFM_INTERNAL}/v4/snapshot",
                params={"asset": asset, "timescale": timescale, "strategy": strategy},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        return {"error": str(exc)[:200]}


# Hub v10 — deployed 2026-04-08T14:44
