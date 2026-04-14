"""
hub/db/migrations/startup_ddl.py

Startup DDL runner — all idempotent schema migrations that were previously
inlined in main.py lifespan().

Extracted so:
  1. They're in one auditable place instead of buried in app startup.
  2. A `SET lock_timeout = '5s'` guard prevents ALTER TABLE statements from
     hanging forever when old connections hold locks (caused a 45-min outage
     on 2026-04-14).

All statements use IF NOT EXISTS / ADD COLUMN IF NOT EXISTS — safe to re-run
on every boot.

Usage (from lifespan):
    async for session in get_session():
        await run_startup_migrations(session)
        break
"""

from __future__ import annotations

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


async def run_startup_migrations(session: AsyncSession) -> None:
    """Run all idempotent startup DDL. Sets a 5-second lock timeout so that
    any ALTER TABLE that can't acquire a lock fails fast rather than hanging."""

    # ── Lock timeout guard ──────────────────────────────────────────────────
    # Any ALTER TABLE that can't acquire an AccessExclusiveLock within 5s
    # will raise a lock_not_available error instead of hanging indefinitely.
    await session.execute(text("SET lock_timeout = '5s'"))

    # ── trading_configs ─────────────────────────────────────────────────────
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

    # ── trades columns ──────────────────────────────────────────────────────
    await session.execute(
        text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS mode VARCHAR(16) DEFAULT 'paper'")
    )
    await session.execute(
        text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS vpin_at_entry NUMERIC(10,6)")
    )

    # ── system_state columns ────────────────────────────────────────────────
    await session.execute(
        text("ALTER TABLE system_state ADD COLUMN IF NOT EXISTS paper_enabled BOOLEAN DEFAULT TRUE")
    )
    await session.execute(
        text("ALTER TABLE system_state ADD COLUMN IF NOT EXISTS live_enabled BOOLEAN DEFAULT FALSE")
    )
    await session.execute(
        text("ALTER TABLE system_state ADD COLUMN IF NOT EXISTS active_paper_config_id INTEGER")
    )
    await session.execute(
        text("ALTER TABLE system_state ADD COLUMN IF NOT EXISTS active_live_config_id INTEGER")
    )

    # ── NT-01: notes table ──────────────────────────────────────────────────
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

    # ── AUDIT-01: audit_tasks_dev table ─────────────────────────────────────
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

    # ── SP-05: strategy_decisions table ─────────────────────────────────────
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
    log.info("hub.startup_ddl_applied")
