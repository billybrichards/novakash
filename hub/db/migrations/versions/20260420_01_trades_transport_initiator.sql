-- Wallet v2 (note #189 §7.3) — transport + initiator columns on trades.
--
-- `transport`  — which code path redeemed a WIN (relayer | onchain_matic | unknown)
-- `initiator`  — who triggered redemption (engine_auto | manual_billy | polymarket_sweeper)
--
-- FE reads these via /api/wallet/history; red banner on /wallet depends on
-- counting WIN+manual+polymarket_sweeper rows in last 24h (audit #252).
--
-- Idempotent — safe to re-run. `ADD COLUMN IF NOT EXISTS` skips existing
-- columns, backfill predicate restricts to NULL rows only so retries are
-- no-ops.

ALTER TABLE trades ADD COLUMN IF NOT EXISTS transport VARCHAR(32);
ALTER TABLE trades ADD COLUMN IF NOT EXISTS initiator VARCHAR(32);

-- Backfill heuristic (spec §7.3):
--   created_at < '2026-04-16' AND outcome='WIN'  → transport='relayer'
--     (MATIC route was introduced on 2026-04-16 per reference_onchain_redeem.md)
--   outcome='WIN' AND transport IS NULL          → transport='unknown'
-- Engine + scripts/ops/manual_redeem.py write the correct values going forward.
--
-- Initiator is left NULL for historical rows — FE renders NULL as "—".
-- Safe interpretation: we cannot retroactively tell engine_auto vs manual,
-- so we refuse to guess.

UPDATE trades
   SET transport = 'relayer'
 WHERE transport IS NULL
   AND outcome = 'WIN'
   AND created_at < TIMESTAMP '2026-04-16';

UPDATE trades
   SET transport = 'unknown'
 WHERE transport IS NULL
   AND outcome = 'WIN';

CREATE INDEX IF NOT EXISTS idx_trades_transport_initiator
    ON trades (transport, initiator)
    WHERE outcome = 'WIN';
