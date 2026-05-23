-- Add pUSD column to wallet_snapshots so the Montreal reconciler can persist
-- both balances side-by-side. Polymarket V2 (cutover 2026-04-28) settles in
-- pUSD; until this column exists, the hub box (which cannot reach Polymarket
-- per the Montreal-only rule) has no way to expose the pUSD balance to
-- /monitor/analysis. See engine/reconciliation/reconciler.py: `_read_pusd_balance`
-- already fetches the value via WalletRPCReader; this migration unlocks the
-- write path in the same reconciler row that already persists balance_usdc.
--
-- IDEMPOTENT: ADD COLUMN IF NOT EXISTS is safe to re-run; CREATE INDEX
-- IF NOT EXISTS likewise. No backfill — historical rows keep NULL pUSD
-- (the only honest answer for pre-cutover snapshots).

ALTER TABLE wallet_snapshots
    ADD COLUMN IF NOT EXISTS balance_pusd NUMERIC(14, 4);

-- Partial index for the common monitor-analysis query: "newest row that has
-- BOTH balances populated" lets the FE distinguish a fresh dual-write row
-- from a legacy USDC-only row without an extra round-trip.
CREATE INDEX IF NOT EXISTS idx_wallet_snapshots_pusd_present
    ON wallet_snapshots(recorded_at DESC)
    WHERE balance_pusd IS NOT NULL;
