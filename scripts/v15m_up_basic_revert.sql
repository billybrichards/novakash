-- v15m_up_basic EMERGENCY REVERT — drops the LIVE override row
-- Strategy reverts to YAML's GHOST mode within ~35s (engine cache refresh).
--
-- Usage:
--   PGPASSWORD=$RDS_PWD psql -h novakash-pg-prod.cpmisy2asv71.ca-central-1.rds.amazonaws.com \
--     -U postgres -d novakash -f scripts/v15m_up_basic_revert.sql
--
-- After running this, no NEW v15m_up_basic LIVE TRADEs will fire. Open positions
-- are NOT closed automatically — they continue to follow the engine's normal
-- exit logic.

\echo '=== BEFORE revert ==='
SELECT strategy_id, mode, updated_at, updated_by, updated_reason
FROM strategy_runtime_overrides
WHERE strategy_id = 'v15m_up_basic';

\echo ''
\echo '=== DELETE override row ==='
DELETE FROM strategy_runtime_overrides
WHERE strategy_id = 'v15m_up_basic'
RETURNING strategy_id, mode, updated_at;

\echo ''
\echo '=== AFTER revert (should be empty) ==='
SELECT strategy_id, mode, updated_at, updated_by, updated_reason
FROM strategy_runtime_overrides
WHERE strategy_id = 'v15m_up_basic';

\echo ''
\echo 'Revert applied. Engine cache refreshes every 30s — wait 35-40s before'
\echo 'expecting LIVE decisions to disappear from strategy_decisions.'
\echo ''
\echo 'Verify revert took effect by running:'
\echo '  SELECT mode, COUNT(*) FROM strategy_decisions'
\echo '   WHERE strategy_id = ''v15m_up_basic'''
\echo '     AND evaluated_at > NOW() - INTERVAL ''2 minutes'''
\echo '   GROUP BY mode;'
\echo ''
\echo 'Expected: only mode=GHOST after 35-40s. If LIVE persists past 60s,'
\echo 'the engine cache may be stuck — escalate to manual engine restart via'
\echo '  bash /home/novakash/novakash/scripts/restart_engine.sh'
