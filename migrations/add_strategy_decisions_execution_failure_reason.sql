-- migrations/add_strategy_decisions_execution_failure_reason.sql
-- Adds execution_failure_reason column to strategy_decisions for executor
-- failure forensics. Companion to the existing executed/order_id/fill_price
-- columns set by mark_executed() on success.
--
-- Without this, action='TRADE' decisions that fail inside ExecuteTradeUseCase
-- (rate limit, exposure cap, hard lock, polymarket cap, etc.) keep executed=false
-- with no SQL-queryable record of *why* — the structured log line is the only
-- audit trail, which makes fill-rate forensics impossible without engine
-- container access.
--
-- Populated by registry.py after execute_uc.execute() returns when
-- not result.success, via PgStrategyDecisionRepository.mark_execution_failed.
-- Fire-and-forget — never blocks the trade path. Reasons are capped at 200
-- chars to keep row sizes sane; longer messages get truncated.
--
-- Hub note #841 / Task #55 — BTC tickformer 8% fill-rate investigation.
-- Additive + idempotent. Reversible via DROP COLUMN.

ALTER TABLE strategy_decisions
    ADD COLUMN IF NOT EXISTS execution_failure_reason TEXT;

-- Forensics-friendly index for the next "why did fills tank" question.
-- Partial so it only carries rows that actually failed — keeps the index
-- small (most rows are SKIP / executed=true).
CREATE INDEX IF NOT EXISTS strategy_decisions_execution_failure_reason_idx
    ON strategy_decisions (execution_failure_reason, evaluated_at DESC)
    WHERE execution_failure_reason IS NOT NULL;

COMMENT ON COLUMN strategy_decisions.execution_failure_reason IS
    'When action=TRADE and executed=false, holds the ExecuteTradeUseCase failure_reason (rate_limit, exposure_cap_blocked, hard_lock_*, etc.). Truncated to 200 chars. NULL for SKIP rows and for executed=true rows. Hub note #841.';
