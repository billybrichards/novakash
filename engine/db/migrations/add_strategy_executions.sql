-- engine/db/migrations/add_strategy_executions.sql
CREATE TABLE IF NOT EXISTS strategy_executions (
    strategy_id   TEXT        NOT NULL,
    window_ts     BIGINT      NOT NULL,
    order_id      TEXT,
    executed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (strategy_id, window_ts)
);
CREATE INDEX IF NOT EXISTS idx_strategy_executions_ts
    ON strategy_executions (executed_at DESC);
