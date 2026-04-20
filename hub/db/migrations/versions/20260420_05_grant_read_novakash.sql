-- Audit-task #255 (F6) — grant SELECT to the `novakash` Railway read role.
--
-- Currently the analysis user has access only to `window_snapshots`. This
-- forces every tuning query to be rewritten as a join through that table,
-- and blocks direct reads for incident triage.
--
-- Idempotent: GRANT is additive in Postgres; running twice is a no-op.
-- Wrapped in DO $$ ... $$ blocks so a missing role (dev environments
-- where `novakash` doesn't exist) emits a NOTICE rather than failing
-- the whole migration batch.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'novakash') THEN
        GRANT SELECT ON strategy_decisions              TO novakash;
        GRANT SELECT ON trades                           TO novakash;
        GRANT SELECT ON signals                          TO novakash;
        GRANT SELECT ON signal_evaluations               TO novakash;
        GRANT SELECT ON ticks_chainlink                  TO novakash;
        GRANT SELECT ON ticks_tiingo                     TO novakash;
        -- window_snapshots already granted; idempotent re-grant.
        GRANT SELECT ON window_snapshots                 TO novakash;
        GRANT SELECT ON window_traces                    TO novakash;

        -- F1 matview (from 20260420_02).
        GRANT SELECT ON strategy_skip_resolved           TO novakash;
        -- Existing view from task #222.
        GRANT SELECT ON strategy_decisions_resolved      TO novakash;
    ELSE
        RAISE NOTICE 'Role "novakash" does not exist — skipping GRANTs (dev environment?)';
    END IF;
END $$;
