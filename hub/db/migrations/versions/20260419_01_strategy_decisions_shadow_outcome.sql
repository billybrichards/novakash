-- Task #222 (follow-up) — strategy_decisions_resolved view SHADOW outcome.
--
-- The previous view (20260417_02) joined strategy_decisions → trades on
-- order_id to get outcome. That works for FILLED TRADE decisions, but:
--
--   1. SKIP decisions have NULL order_id → never resolve.
--   2. strategy_decisions.order_id is NULL until the engine writes back
--      the Polymarket tx-hash after fill. For most rows this backfill is
--      incomplete, so even successful TRADE decisions returned outcome=NULL.
--   3. Result: FE SignalExplorer WR matrix was empty. 1338 WIN trades in
--      the trades table but zero rows with outcome via the view.
--
-- Fix: add a SHADOW outcome computed from direction vs
-- window_snapshots.actual_direction (PR #213 resolution column, also used
-- by scripts/ops/shadow_analysis.py — the canonical analysis script per
-- memory `reference_shadow_analysis.md`).
--
-- Final outcome = COALESCE(trades.outcome, shadow_outcome).
--   * Filled trade with on-chain resolution → trades.outcome wins
--   * No trade fill OR backfill gap → shadow_outcome fills in
--   * Direction NULL (SKIP without a committed direction) → still NULL
--
-- This lets the matrix populate from the moment `window_snapshots.
-- actual_direction` is written (~2 min after window close) regardless of
-- whether a trade actually filled.
--
-- Idempotent via DROP + CREATE — CREATE OR REPLACE VIEW refuses when the
-- outcome column's expression type changes and a new trailing column is
-- added. DROP IF EXISTS keeps re-runs safe.

DROP VIEW IF EXISTS strategy_decisions_resolved;

CREATE VIEW strategy_decisions_resolved AS
SELECT
    sd.id,
    sd.strategy_id,
    sd.strategy_version,
    sd.asset,
    sd.window_ts,
    sd.timeframe,
    sd.eval_offset,
    sd.mode,
    sd.action,
    sd.direction,
    sd.confidence,
    sd.confidence_score,
    sd.entry_cap,
    sd.collateral_pct,
    sd.entry_reason,
    sd.skip_reason,
    sd.executed,
    sd.order_id,
    sd.fill_price,
    sd.fill_size,
    sd.metadata_json,
    sd.evaluated_at,
    -- Fallback chain: real fill outcome → shadow outcome → NULL.
    -- shadow is WIN iff the strategy committed a direction AND the
    -- window resolved AND the directions match. Strategy direction
    -- is stored UP/DOWN on the engine side; window_snapshots stores
    -- actual_direction UP/DOWN, with poly_winner as a legacy fallback.
    COALESCE(
        t.outcome,
        CASE
            WHEN sd.direction IS NOT NULL
             AND COALESCE(snap.actual_direction, UPPER(snap.poly_winner)) IS NOT NULL
            THEN CASE
                WHEN sd.direction = COALESCE(snap.actual_direction, UPPER(snap.poly_winner))
                THEN 'WIN'
                ELSE 'LOSS'
            END
            ELSE NULL
        END
    ) AS outcome,
    t.pnl_usd,
    t.resolved_at,
    t.sot_reconciliation_state,
    -- New: explicit flag so the FE / audit tooling can distinguish
    -- "real on-chain settled" from "would-have-been (shadow) settled".
    CASE WHEN t.outcome IS NOT NULL THEN 'fill'
         WHEN COALESCE(snap.actual_direction, UPPER(snap.poly_winner)) IS NOT NULL
              AND sd.direction IS NOT NULL THEN 'shadow'
         ELSE NULL
    END AS outcome_source
FROM strategy_decisions sd
LEFT JOIN LATERAL (
    SELECT
        outcome,
        pnl_usd,
        resolved_at,
        sot_reconciliation_state
    FROM trades
    WHERE trades.order_id = sd.order_id
      AND sd.order_id IS NOT NULL
    ORDER BY resolved_at DESC NULLS LAST, created_at DESC
    LIMIT 1
) t ON TRUE
LEFT JOIN window_snapshots snap
    ON snap.asset = sd.asset
   AND snap.window_ts = sd.window_ts
   AND snap.timeframe = sd.timeframe;

COMMENT ON VIEW strategy_decisions_resolved IS
    'Task #222 + 2026-04-19 shadow follow-up — decisions enriched with ' ||
    'resolved outcome. Fallback chain: trades.outcome → shadow (direction ' ||
    'vs window_snapshots.actual_direction) → NULL. outcome_source column ' ||
    'flags which was used. Read-only projection.';
