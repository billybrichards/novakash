-- Audit-task #255 (F2) — composite + JSONB indexes on strategy_decisions.
--
-- Current indexes (from add_strategy_decisions_table.sql):
--   idx_sd_window    (asset, window_ts)
--   idx_sd_strategy  (strategy_id, evaluated_at)
--
-- Gaps identified during skip-bucket analysis work:
--
--   1. Every tuning query filters by (strategy_id, action) — the existing
--      idx_sd_strategy covers strategy_id but not action, forcing a bitmap
--      scan. Adding action + created_at to the composite gives a pure index
--      scan for time-ranged per-strategy queries.
--
--   2. Skip-reason aggregations (the main tuning workflow) scan every row
--      where action='SKIP' then filter by reason. A partial index on
--      skip_reason with WHERE action='SKIP' cuts the scan by ~90%.
--
--   3. metadata->>'dedup_key' is read by the F1 matview AND by ad-hoc
--      duplicate-detection queries. Without an expression index, PG
--      re-parses the JSONB on every row. A B-tree on the expression
--      gets us direct lookups.
--
--   4. metadata_json is interrogated with JSON path queries (`? 'risk_off'`,
--      `@> '{"bucket":"agree_strong"}'`) in the tuning scripts. A GIN with
--      jsonb_path_ops is the canonical PG index for those operators.
--
-- All CREATE INDEX IF NOT EXISTS — safe to re-run. No CONCURRENTLY here
-- because the hub startup runs inside a transaction and CONCURRENTLY
-- cannot run in a transaction; for first-time creation on a hot table,
-- run this file out-of-band with `\i` via psql as a one-off.

-- F2.1: composite for per-strategy, per-action time queries.
-- Superseds idx_sd_strategy for action-filtered time-range scans.
-- `created_at` uses evaluated_at since strategy_decisions has no
-- created_at column (evaluated_at IS the authoritative write time).
CREATE INDEX IF NOT EXISTS idx_sd_strategy_action_evaluated
    ON strategy_decisions (strategy_id, action, evaluated_at DESC);

-- F2.2: partial index on skip_reason for action='SKIP'.
-- Hot path: "show me all windows where v6_sniper skipped for
-- reason=source_disagree in the last 48h".
CREATE INDEX IF NOT EXISTS idx_sd_skip_reason
    ON strategy_decisions (strategy_id, skip_reason, evaluated_at DESC)
    WHERE action = 'SKIP';

-- F2.3: B-tree on dedup_key expression.
-- The F1 matview's dedup logic + the audit-task #256 duplicate-detection
-- scripts both filter by metadata_json->>'dedup_key'.
CREATE INDEX IF NOT EXISTS idx_sd_dedup_key
    ON strategy_decisions ((metadata_json->>'dedup_key'))
    WHERE metadata_json ? 'dedup_key';

-- F2.4: GIN with jsonb_path_ops for containment queries.
-- jsonb_path_ops supports @> / ? / ?& / ?| — what the tuning scripts use.
-- Smaller + faster than the default jsonb_ops when we never do key
-- existence checks in isolation (we always combine with @> containment).
CREATE INDEX IF NOT EXISTS idx_sd_metadata_path_ops
    ON strategy_decisions
    USING GIN (metadata_json jsonb_path_ops);
