-- 20260430_01_add_operator_to_manual_trades.sql
--
-- Track A: add operator identity columns to manual_trades so the hub can
-- stamp which JWT user submitted a trade via the new /desk/manual-trade
-- endpoint.
--
-- Columns are nullable because rows written by the engine poller
-- (ExecuteManualTradeUseCase) have no authenticated hub user — those
-- rows remain NULL. Only trades submitted via hub JWT-gated endpoints
-- will have operator_user_id / operator_username populated.
--
-- Idempotent: both statements use ADD COLUMN IF NOT EXISTS so re-running
-- this migration on a DB that already has the columns is a no-op.
--
-- Applied by: hub/db/migrations/v58_monitor_ddl.py
-- ensure_manual_trades_table() which is called from hub/main.py lifespan
-- and defensively from route handlers that need the table.

ALTER TABLE manual_trades
    ADD COLUMN IF NOT EXISTS operator_user_id INTEGER;

ALTER TABLE manual_trades
    ADD COLUMN IF NOT EXISTS operator_username VARCHAR(64);

COMMENT ON COLUMN manual_trades.operator_user_id IS
    'JWT user_id of the hub operator who submitted this trade, NULL for engine-issued rows.';

COMMENT ON COLUMN manual_trades.operator_username IS
    'JWT username of the hub operator who submitted this trade, NULL for engine-issued rows.';
