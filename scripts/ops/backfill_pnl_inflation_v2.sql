-- backfill_pnl_inflation_v2.sql
--
-- Audit-task #331 — historical repair for the FAK-ladder pnl-inflation bug.
--
-- Background
-- ----------
-- Until the Layer-1 fix (Order.fill_price) ships, FAK-ladder strategies
-- (v9_lgb_only, v10_lgb_only, v15m_fusion, the legacy five_min_vpin path,
-- etc.) computed payout from ``order.price`` (the FIRST ladder rung) instead
-- of the volume-weighted average fill price actually paid. For every WIN
-- where the avg fill exceeded the first-rung limit by N×, the stored
-- ``pnl_usd`` is inflated by the same ratio.
--
-- The actual avg fill price was ALSO recorded — separately — into
-- ``trades.fill_price`` by the FOK ladder (see fok_ladder.py::_build_result,
-- audit #260). That column is therefore the source of truth for repair.
--
-- The ground-truth payout for a binary-options WIN is exactly the share
-- count, which is precisely what ``trades.fill_size`` already records:
--
--     payout = fill_size           (one USDC per share at settlement)
--     pnl    = fill_size - stake_usd
--
-- Repair logic
-- ------------
-- For every WIN row where the stored ``pnl_usd`` diverges from
-- ``fill_size - stake_usd`` by more than $0.50 (a tolerance that protects
-- single-fill rows where rounding from the legacy formula matched perfectly),
-- recompute pnl from the per-trade fill_size. Idempotent — re-running on a
-- repaired row is a no-op (divergence is now 0).
--
-- Pre-flight check (run BEFORE the BEGIN block to validate the assumption
-- that fill_size is the per-trade share count, not a position aggregate):
--
--   SELECT id, strategy, stake_usd, entry_price, fill_price, fill_size,
--          ROUND((stake_usd / NULLIF(entry_price, 0))::numeric, 2)
--              AS shares_from_entry_price,
--          ROUND((stake_usd / NULLIF(fill_price, 0))::numeric, 2)
--              AS shares_from_fill_price,
--          pnl_usd
--     FROM trades
--    WHERE outcome = 'WIN'
--      AND status  = 'RESOLVED_WIN'
--      AND fill_size > 0
--      AND stake_usd > 0
--    ORDER BY id DESC
--    LIMIT 50;
--
-- Eyeball the rows: ``fill_size`` should be ~= ``shares_from_fill_price``.
-- If a swathe of rows show fill_size = sum-of-shares-across-trades instead,
-- DO NOT RUN THE BACKFILL — the assumption is broken on those strategies and
-- the repair would silently corrupt them. The diagnostic in Hub note #306
-- confirms the assumption holds for the affected strategies as of 2026-05-01.
--
-- Recommended workflow:
--   1. Run the diagnostic query above.
--   2. Run the dry-run SELECT (see "Dry run" block below) to see the row
--      count and a sample of rewrites. Sanity-check the deltas.
--   3. If everything looks right, run the BEGIN/COMMIT block.

-- ─── Dry run (no writes) ──────────────────────────────────────────────────
-- Uncomment this block to preview the rewrite candidates without changing
-- anything. Safe to run on any environment.
--
-- SELECT
--     id,
--     strategy,
--     stake_usd,
--     fill_size,
--     fill_price,
--     pnl_usd                                            AS pnl_before,
--     ROUND((fill_size - stake_usd)::numeric, 4)         AS pnl_after,
--     ROUND((pnl_usd - (fill_size - stake_usd))::numeric, 4)
--                                                        AS inflation_usd
--   FROM trades
--  WHERE outcome = 'WIN'
--    AND status  = 'RESOLVED_WIN'
--    AND fill_size > 0
--    AND stake_usd > 0
--    AND ABS(pnl_usd - (fill_size - stake_usd)) > 0.5
--  ORDER BY ABS(pnl_usd - (fill_size - stake_usd)) DESC
--  LIMIT 100;

-- ─── Repair (transactional) ───────────────────────────────────────────────
BEGIN;

WITH repair AS (
    UPDATE trades
       SET pnl_usd    = ROUND((fill_size - stake_usd)::numeric, 4),
           payout_usd = ROUND(fill_size::numeric, 4)
     WHERE outcome = 'WIN'
       AND status  = 'RESOLVED_WIN'
       AND fill_size > 0
       AND stake_usd > 0
       AND ABS(pnl_usd - (fill_size - stake_usd)) > 0.5
    RETURNING id, strategy
)
SELECT
    strategy,
    COUNT(*) AS rows_repaired
  FROM repair
 GROUP BY strategy
 ORDER BY rows_repaired DESC;

-- Manual COMMIT — review the rows_repaired summary first, then run:
--     COMMIT;
-- (or ``ROLLBACK;`` to abort).
