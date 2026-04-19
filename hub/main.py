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
from api.positions import router as positions_router
from api.schema import router as schema_router

# CFG-02/03: DB-backed config schema + read-only API
from api.config_v2 import router as config_v2_router

# AGENT-OPS: Claude Agent SDK background task runners
from api.agent_ops import router as agent_ops_router
from api.strategy_decisions import router as strategy_decisions_router
from api.strategies import router as strategies_router
from api.window_traces import router as window_traces_router
from api.gate_traces import router as gate_traces_router

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
            await session.execute(
                text("""
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
            """)
            )
            await session.execute(
                text(
                    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS mode VARCHAR(16) DEFAULT 'paper'"
                )
            )
            await session.execute(
                text(
                    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS vpin_at_entry NUMERIC(10,6)"
                )
            )
            await session.execute(
                text(
                    "ALTER TABLE system_state ADD COLUMN IF NOT EXISTS paper_enabled BOOLEAN DEFAULT TRUE"
                )
            )
            await session.execute(
                text(
                    "ALTER TABLE system_state ADD COLUMN IF NOT EXISTS live_enabled BOOLEAN DEFAULT FALSE"
                )
            )
            await session.execute(
                text(
                    "ALTER TABLE system_state ADD COLUMN IF NOT EXISTS active_paper_config_id INTEGER"
                )
            )
            await session.execute(
                text(
                    "ALTER TABLE system_state ADD COLUMN IF NOT EXISTS active_live_config_id INTEGER"
                )
            )
            # Phase-2 (audit #216 follow-up): strategy_configs registry.
            # Engine upserts YAML into this table at startup; hub reads it
            # in preference to the filesystem (see api/strategies.py). See
            # hub/db/migrations/versions/20260417_03_strategy_configs.sql
            # for the full rationale.
            await session.execute(
                text("""
                CREATE TABLE IF NOT EXISTS strategy_configs (
                    strategy_id   VARCHAR(64)  NOT NULL,
                    version       VARCHAR(32)  NOT NULL,
                    mode          VARCHAR(16)  NOT NULL,
                    asset         VARCHAR(16),
                    timescale     VARCHAR(16),
                    config_yaml   TEXT         NOT NULL,
                    gates_json    JSONB,
                    sizing_json   JSONB,
                    hooks_file    VARCHAR(256),
                    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (strategy_id, version)
                )
            """)
            )
            await session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_strategy_configs_strategy "
                    "ON strategy_configs (strategy_id)"
                )
            )
            await session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_strategy_configs_updated "
                    "ON strategy_configs (updated_at DESC)"
                )
            )
            # NT-01: persistent notes/journal table
            await session.execute(
                text("""
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
            """)
            )
            await session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS notes_status_updated_idx "
                    "ON notes (status, updated_at DESC)"
                )
            )
            # Seed one initial note so the page isn't empty on first deploy.
            # SQL escapes the apostrophe in "don't" with '' (string escape).
            await session.execute(
                text("""
                INSERT INTO notes (title, body, tags, status, author)
                SELECT
                    'Notes page live (NT-01)',
                    'This page is a persistent journal for audit observations, to-do items, and working notes. It backs /audit by providing a place to drop quick observations that don''t warrant a new task. Add new notes with the + button. Filter by status or tag. Cmd+Enter submits.',
                    'nt-01,meta',
                    'open',
                    'claude'
                WHERE NOT EXISTS (SELECT 1 FROM notes WHERE title = 'Notes page live (NT-01)')
            """)
            )
            # AUDIT-01: agent ops task queue + audit checklist table
            await session.execute(
                text("""
                CREATE TABLE IF NOT EXISTS audit_tasks_dev (
                    id                BIGSERIAL PRIMARY KEY,
                    task_key          VARCHAR(64),
                    task_type         VARCHAR(64) NOT NULL,
                    source            VARCHAR(64),
                    title             TEXT NOT NULL,
                    status            VARCHAR(24) NOT NULL DEFAULT 'OPEN',
                    severity          VARCHAR(16),
                    category          VARCHAR(64),
                    priority          INTEGER NOT NULL DEFAULT 0,
                    dedupe_key        TEXT,
                    payload           JSONB NOT NULL DEFAULT '{}'::jsonb,
                    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_by        VARCHAR(64),
                    updated_by        VARCHAR(64),
                    claimed_by        VARCHAR(64),
                    claimed_at        TIMESTAMPTZ,
                    claim_expires_at  TIMESTAMPTZ,
                    started_at        TIMESTAMPTZ,
                    completed_at      TIMESTAMPTZ,
                    canceled_at       TIMESTAMPTZ,
                    last_heartbeat_at TIMESTAMPTZ,
                    attempt_count     INTEGER NOT NULL DEFAULT 0,
                    last_error        TEXT,
                    status_reason     TEXT,
                    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            )
            await session.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS audit_tasks_dev_dedupe_key_uq "
                    "ON audit_tasks_dev (dedupe_key) WHERE dedupe_key IS NOT NULL"
                )
            )
            # Ensure constraint exists for ON CONFLICT (dedupe_key) — needed when
            # dedupe_key is always set (e.g. audit_checklist seed). The partial index
            # above only works with ON CONFLICT ... WHERE, so add a full constraint too.
            await session.execute(
                text(
                    "DO $$ BEGIN "
                    "  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='audit_tasks_dev_dedupe_key_uniq') THEN "
                    "    ALTER TABLE audit_tasks_dev ADD CONSTRAINT audit_tasks_dev_dedupe_key_uniq UNIQUE (dedupe_key); "
                    "  END IF; "
                    "END $$"
                )
            )
            await session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS audit_tasks_dev_status_priority_idx "
                    "ON audit_tasks_dev (status, priority DESC, created_at ASC)"
                )
            )
            await session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS audit_tasks_dev_claim_expires_idx "
                    "ON audit_tasks_dev (claim_expires_at)"
                )
            )
            await session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS audit_tasks_dev_claimed_by_idx "
                    "ON audit_tasks_dev (claimed_by, status)"
                )
            )
            await session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS audit_tasks_dev_updated_at_idx "
                    "ON audit_tasks_dev (updated_at DESC)"
                )
            )
            await session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS audit_tasks_dev_type_created_idx "
                    "ON audit_tasks_dev (task_type, created_at DESC)"
                )
            )
            # Task #222 — strategy_decisions_resolved view.
            #
            # Enriches strategy_decisions with resolved-trade outcome in a
            # single query so the FE SignalExplorer / Strategies-WR matrix
            # can get outcome + pnl without the O(N*M) client-side cross
            # join. Shipped as VIEW (not column) to stay clean-arch-phase-3
            # compatible — when position_resolutions table lands the view
            # body rewrites; zero FE change.
            #
            # LATERAL subquery picks the most-recently-resolved trade when
            # an order_id has multiple rows (2-leg, multi-eval, re-fills).
            # `CREATE OR REPLACE VIEW` is idempotent — safe on every boot.
            # Canonical source:
            #   hub/db/migrations/versions/20260417_02_strategy_decisions_resolved_view.sql
            # 2026-04-19 follow-up: the original view returned zero outcomes
            # because trades.order_id rarely backfills onto strategy_decisions
            # and SKIPs have no order_id at all. Shadow outcome from
            # window_snapshots.actual_direction (PR #213) is the canonical
            # source — matches scripts/ops/shadow_analysis.py. See
            # hub/db/migrations/versions/20260419_01_strategy_decisions_shadow_outcome.sql
            await session.execute(
                text(
                    """
                    CREATE OR REPLACE VIEW strategy_decisions_resolved AS
                    SELECT
                        sd.id, sd.strategy_id, sd.strategy_version, sd.asset,
                        sd.window_ts, sd.timeframe, sd.eval_offset, sd.mode,
                        sd.action, sd.direction, sd.confidence, sd.confidence_score,
                        sd.entry_cap, sd.collateral_pct, sd.entry_reason, sd.skip_reason,
                        sd.executed, sd.order_id, sd.fill_price, sd.fill_size,
                        sd.metadata_json, sd.evaluated_at,
                        COALESCE(
                            t.outcome,
                            CASE
                                WHEN sd.direction IS NOT NULL
                                 AND COALESCE(snap.actual_direction, UPPER(snap.poly_winner)) IS NOT NULL
                                THEN CASE
                                    WHEN sd.direction = COALESCE(snap.actual_direction, UPPER(snap.poly_winner))
                                    THEN 'WIN' ELSE 'LOSS'
                                END
                                ELSE NULL
                            END
                        ) AS outcome,
                        t.pnl_usd,
                        COALESCE(t.resolved_at, snap.resolved_at) AS resolved_at,
                        t.sot_reconciliation_state,
                        CASE WHEN t.outcome IS NOT NULL THEN 'fill'
                             WHEN COALESCE(snap.actual_direction, UPPER(snap.poly_winner)) IS NOT NULL
                                  AND sd.direction IS NOT NULL THEN 'shadow'
                             ELSE NULL
                        END AS outcome_source
                    FROM strategy_decisions sd
                    LEFT JOIN LATERAL (
                        SELECT outcome, pnl_usd, resolved_at, sot_reconciliation_state
                        FROM trades
                        WHERE trades.order_id = sd.order_id
                          AND sd.order_id IS NOT NULL
                        ORDER BY resolved_at DESC NULLS LAST, created_at DESC
                        LIMIT 1
                    ) t ON TRUE
                    LEFT JOIN window_snapshots snap
                        ON snap.asset = sd.asset
                       AND snap.window_ts = sd.window_ts
                       AND snap.timeframe = sd.timeframe
                    """
                )
            )
            await session.commit()
            log.info("hub.migrations_applied")
            # Ensure manual_trades table exists
            try:
                from db.migrations.v58_monitor_ddl import ensure_manual_trades_table

                await ensure_manual_trades_table(session)
            except Exception as mt_exc:
                log.warning("hub.manual_trades_migration_error", error=str(mt_exc))
            # LT-03: ensure manual_trade_snapshots table exists
            try:
                from db.migrations.v58_monitor_ddl import (
                    ensure_manual_trade_snapshots_table,
                )

                await ensure_manual_trade_snapshots_table(session)
            except Exception as mts_exc:
                log.warning(
                    "hub.manual_trade_snapshots_migration_error", error=str(mts_exc)
                )
            # CFG-02: ensure config_keys / config_values / config_history tables exist
            # and seed config_keys with the inventoried 142+ keys. Idempotent —
            # re-running on every hub boot is safe and picks up new seed entries.
            try:
                from db.config_schema import ensure_config_tables
                from db.config_seed import seed_config_keys

                await ensure_config_tables(session)
                await session.commit()
                counts = await seed_config_keys(session)
                await session.commit()
                log.info("hub.config_seed_done", per_service=counts)
            except Exception as cfg_exc:
                log.warning("hub.config_schema_migration_error", error=str(cfg_exc))
            # AGENT-OPS: ensure agent_tasks table exists
            await session.execute(
                text("""
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    agent_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    result TEXT,
                    error TEXT,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    completed_at TIMESTAMPTZ
                )
            """)
            )
            await session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS agent_tasks_started_idx "
                    "ON agent_tasks (started_at DESC)"
                )
            )
            # SP-05: ensure strategy_decisions table exists
            try:
                await session.execute(
                    text("""
                    CREATE TABLE IF NOT EXISTS strategy_decisions (
                        id              BIGSERIAL PRIMARY KEY,
                        strategy_id     TEXT NOT NULL,
                        strategy_version TEXT NOT NULL,
                        asset           TEXT NOT NULL,
                        window_ts       BIGINT NOT NULL,
                        timeframe       TEXT NOT NULL DEFAULT '5m',
                        eval_offset     INTEGER,
                        mode            TEXT NOT NULL,
                        action          TEXT NOT NULL,
                        direction       TEXT,
                        confidence      TEXT,
                        confidence_score DOUBLE PRECISION,
                        entry_cap       DOUBLE PRECISION,
                        collateral_pct  DOUBLE PRECISION,
                        entry_reason    TEXT NOT NULL DEFAULT '',
                        skip_reason     TEXT,
                        executed        BOOLEAN NOT NULL DEFAULT false,
                        order_id        TEXT,
                        fill_price      DOUBLE PRECISION,
                        fill_size       DOUBLE PRECISION,
                        metadata_json   JSONB NOT NULL DEFAULT '{}',
                        evaluated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (strategy_id, asset, window_ts, eval_offset)
                    )
                """)
                )
                await session.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_sd_window "
                        "ON strategy_decisions (asset, window_ts)"
                    )
                )
                await session.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_sd_strategy "
                        "ON strategy_decisions (strategy_id, evaluated_at)"
                    )
                )
                await session.commit()
                log.info("hub.strategy_decisions_table_ensured")
            except Exception as sd_exc:
                log.warning("hub.strategy_decisions_migration_error", error=str(sd_exc))
            # v59: mark phantom trades (gtc_resting/gtc with no on-chain fill)
            try:
                from db.migrations.v59_mark_phantom_trades import mark_phantom_trades

                n_phantom = await mark_phantom_trades(session)
                await session.commit()
                if n_phantom:
                    log.info("hub.v59_phantom_trades_marked", count=n_phantom)
            except Exception as ph_exc:
                log.warning("hub.v59_phantom_migration_error", error=str(ph_exc))
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
app.include_router(audit_tasks_router, prefix="/api", tags=["audit-tasks"])
# TG-REDEMPTION-VIS Task 8: positions snapshot for Telegram top bar
app.include_router(positions_router, prefix="/api", tags=["positions"])
# SCHEMA-01: /schema page — DB table inventory (catalog + live runtime stats)
app.include_router(schema_router, prefix="/api", tags=["schema"])
# CFG-02/03: DB-backed config (read-only in this PR; writes ship in CFG-04)
app.include_router(config_v2_router, prefix="/api", tags=["config-v2"])
# AGENT-OPS: Claude Agent SDK background task runners
app.include_router(agent_ops_router, prefix="/api", tags=["agent-ops"])
# STRATEGY-DECISIONS: 15m + 5m strategy decision queries
app.include_router(
    strategy_decisions_router, prefix="/api", tags=["strategy-decisions"]
)
# STRATEGIES: registry listing for FE Strategies page (audit #216)
app.include_router(strategies_router, prefix="/api", tags=["strategies"])
app.include_router(window_traces_router, prefix="/api", tags=["window-traces"])
# GATE-TRACES: per-gate pass/fail heatmap from gate_check_traces (audit #188)
app.include_router(gate_traces_router, prefix="/api", tags=["gate-traces"])


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
# Deploy trigger 2026-04-14T16:11:57Z
