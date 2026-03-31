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
from ws.live_feed import router as ws_router

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup/shutdown lifecycle."""
    log.info("hub.starting")
    await init_db()
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
app.include_router(ws_router, tags=["websocket"])


@app.get("/health", tags=["health"])
async def health_check() -> dict:
    """Liveness probe."""
    return {"status": "ok"}
