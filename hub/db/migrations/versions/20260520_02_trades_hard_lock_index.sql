-- Strict single-fire-per-(strategy, window, direction) lock support.
--
-- Audit 2026-05-20: v9_2_eth_raw_lgb fired DOWN 3x on window 1779312000
-- in 40s, -$21.91 wallet hit. Every TTL-based dedup (15s lease, 25s
-- placeholder, 30s order-interval) cleared between fires.
--
-- This index backs has_fill_for_strategy_window_direction() (O(log n)
-- lookup) used by the new Step -0.5 HARD lock in ExecuteTradeUseCase.
-- Partial index keeps the b-tree small — only "live" non-cancelled rows
-- count toward the lock.

CREATE INDEX IF NOT EXISTS idx_trades_strategy_window_dir
ON trades (
    strategy_id,
    (COALESCE(metadata->>'window_ts', '')),
    direction,
    status
)
WHERE status NOT IN ('CANCELLED', 'SKIPPED', 'FAILED_EXECUTION');

COMMENT ON INDEX idx_trades_strategy_window_dir IS
    'Backs the HARD lock query in PgTradeRepository.has_fill_for_strategy_window_direction. See ExecuteTradeUseCase Step -0.5 (audit 2026-05-20).';
