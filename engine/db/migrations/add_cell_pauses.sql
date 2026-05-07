-- engine/db/migrations/add_cell_pauses.sql
-- Audit #379 + #385 (2026-05-06): per-cell rolling-WR auto-pause table.
--
-- A "cell" is the tuple (strategy_id, direction, t_band, regime, session).
-- The rolling-WR monitor inserts a row here whenever a cell crosses a
-- pause trigger (Wilson LB < fill-adjusted breakeven, 7d-baseline drop,
-- or 60-min PnL collapse). The CellPauseGate reads active rows
-- (`released_at IS NULL AND pause_until > NOW()`) and SKIPs the strategy
-- when a match exists for the current evaluation context.
--
-- C4 fix (audit #379): original design keyed on (strategy_id, direction,
-- t_band, regime, session, paused_at) with microsecond precision, which
-- allowed two concurrent inserts at different microseconds to both succeed,
-- producing duplicate active pauses. Replaced with a PARTIAL UNIQUE INDEX
-- on `released_at IS NULL` to enforce at most one active pause per cell.
-- Race-condition handling: one insert wins, the concurrent one receives a
-- UniqueViolationError that callers treat as "already paused" (idempotent).

CREATE TABLE IF NOT EXISTS cell_pauses (
    id                 BIGSERIAL    PRIMARY KEY,
    strategy_id        TEXT         NOT NULL,
    direction          TEXT         NOT NULL,
    t_band             TEXT         NOT NULL,
    regime             TEXT,
    session            TEXT,
    paused_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    pause_until        TIMESTAMPTZ  NOT NULL,
    reason             TEXT         NOT NULL,
    trigger_metric     JSONB,
    released_at        TIMESTAMPTZ,
    released_by        TEXT
    -- NOTE: no inline UNIQUE constraint here; enforced by partial index below.
);

-- C4 fix: partial unique index — only one ACTIVE pause per cell at a time.
-- COALESCE handles nullable regime/session (NULL != NULL in unique indexes).
CREATE UNIQUE INDEX IF NOT EXISTS idx_cell_pauses_one_active
    ON cell_pauses (
        strategy_id,
        direction,
        t_band,
        COALESCE(regime, ''),
        COALESCE(session, '')
    )
    WHERE released_at IS NULL;

-- Hot-path lookup: active pauses (no `released_at`) for a given cell.
CREATE INDEX IF NOT EXISTS idx_cell_pauses_active
    ON cell_pauses (strategy_id, direction, t_band, regime)
    WHERE released_at IS NULL;

-- Time-ordered scans (recent pauses for ops dashboard).
CREATE INDEX IF NOT EXISTS idx_cell_pauses_paused_at
    ON cell_pauses (paused_at DESC);

-- Reverse migration:
-- DROP INDEX IF EXISTS idx_cell_pauses_paused_at;
-- DROP INDEX IF EXISTS idx_cell_pauses_active;
-- DROP UNIQUE INDEX IF EXISTS idx_cell_pauses_one_active;
-- DROP TABLE IF EXISTS cell_pauses;
