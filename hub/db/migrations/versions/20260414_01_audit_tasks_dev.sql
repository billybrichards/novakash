-- v12 Migration: audit_tasks_dev
-- Purpose: Agent Ops task queue + audit checklist persistence
-- Design:
--   - Claim/lease model for concurrent agents
--   - Dedupe key for idempotent inserts
--   - JSONB payload + metadata for extensibility

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
);

CREATE UNIQUE INDEX IF NOT EXISTS audit_tasks_dev_dedupe_key_uq
    ON audit_tasks_dev (dedupe_key) WHERE dedupe_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS audit_tasks_dev_status_priority_idx
    ON audit_tasks_dev (status, priority DESC, created_at ASC);

CREATE INDEX IF NOT EXISTS audit_tasks_dev_claim_expires_idx
    ON audit_tasks_dev (claim_expires_at);

CREATE INDEX IF NOT EXISTS audit_tasks_dev_claimed_by_idx
    ON audit_tasks_dev (claimed_by, status);

CREATE INDEX IF NOT EXISTS audit_tasks_dev_updated_at_idx
    ON audit_tasks_dev (updated_at DESC);

CREATE INDEX IF NOT EXISTS audit_tasks_dev_type_created_idx
    ON audit_tasks_dev (task_type, created_at DESC);

COMMENT ON TABLE audit_tasks_dev IS 'Agent Ops task queue + audit checklist tasks (v12).';
COMMENT ON COLUMN audit_tasks_dev.task_key IS 'Human-readable ID (e.g. CA-01) from audit checklist.';
COMMENT ON COLUMN audit_tasks_dev.dedupe_key IS 'Idempotency key for safe retries.';
COMMENT ON COLUMN audit_tasks_dev.payload IS 'Task input data; JSONB for extensibility.';
COMMENT ON COLUMN audit_tasks_dev.metadata IS 'Evidence, files, progress notes, and UI metadata.';
