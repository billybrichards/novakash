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

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup/shutdown lifecycle."""
    log.info("hub.starting")
    await init_db()
    # Auto-run migrations on startup
    try:
        from sqlalchemy import text
        from db.database import get_session
        async for session in get_session():
            await session.execute(text("""
                CREATE TABLE IF NOT EXISTS trading_configs (
                    id SERIAL PRIMARY KEY, name VARCHAR(128) NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1, description TEXT,
                    config JSONB NOT NULL, mode VARCHAR(16) NOT NULL DEFAULT 'paper',
                    is_active BOOLEAN DEFAULT FALSE, is_approved BOOLEAN DEFAULT FALSE,
                    approved_at TIMESTAMPTZ, approved_by VARCHAR(64),
                    parent_id INTEGER REFERENCES trading_configs(id),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            await session.execute(text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS mode VARCHAR(16) DEFAULT 'paper'"))
            await session.execute(text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS vpin_at_entry NUMERIC(10,6)"))
            await session.execute(text("ALTER TABLE system_state ADD COLUMN IF NOT EXISTS paper_enabled BOOLEAN DEFAULT TRUE"))
            await session.execute(text("ALTER TABLE system_state ADD COLUMN IF NOT EXISTS live_enabled BOOLEAN DEFAULT FALSE"))
            await session.execute(text("ALTER TABLE system_state ADD COLUMN IF NOT EXISTS active_paper_config_id INTEGER"))
            await session.execute(text("ALTER TABLE system_state ADD COLUMN IF NOT EXISTS active_live_config_id INTEGER"))
            # NT-01: persistent notes/journal table
            await session.execute(text("""
                CREATE TABLE IF NOT EXISTS notes (
                    id SERIAL PRIMARY KEY,
                    title VARCHAR(200) NOT NULL DEFAULT '',
                    body TEXT NOT NULL,
                    tags VARCHAR(500) NOT NULL DEFAULT '',
                    status VARCHAR(20) NOT NULL DEFAULT 'open',
                    author VARCHAR(50) NOT NULL DEFAULT 'claude',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            await session.execute(text(
                "CREATE INDEX IF NOT EXISTS notes_status_updated_idx "
                "ON notes (status, updated_at DESC)"
            ))
            # Seed one initial note so the page isn't empty on first deploy.
            # SQL escapes the apostrophe in "don't" with '' (string escape).
            await session.execute(text("""
                INSERT INTO notes (title, body, tags, status, author)
                SELECT
                    'Notes page live (NT-01)',
                    'This page is a persistent journal for audit observations, to-do items, and working notes. It backs /audit by providing a place to drop quick observations that don''t warrant a new task. Add new notes with the + button. Filter by status or tag. Cmd+Enter submits.',
                    'nt-01,meta',
                    'open',
                    'claude'
                WHERE NOT EXISTS (SELECT 1 FROM notes WHERE title = 'Notes page live (NT-01)')
            """))
            await session.commit()
            log.info("hub.migrations_applied")
            # Ensure manual_trades table exists
            try:
                from api.v58_monitor import ensure_manual_trades_table
                await ensure_manual_trades_table(session)
            except Exception as mt_exc:
                log.warning("hub.manual_trades_migration_error", error=str(mt_exc))
            break
    except Exception as exc:
        log.warning("hub.migration_error", error=str(exc))
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


@app.get("/health", tags=["health"])
async def health_check() -> dict:
    """Liveness probe."""
    return {"status": "ok"}
# Hub v10 — deployed 2026-04-08T14:44
