-- Task #222 — expose strategy_decisions_resolved VIEW so the FE
-- SignalExplorer / Strategies-WR matrix can get outcome + pnl in one
-- payload instead of client-side cross-joining /v58/strategy-decisions
-- with /api/trades (O(N*M) slow + incorrect under pagination).
--
-- Design decision: ship as VIEW, not as a new column on
-- strategy_decisions. Rationale documented in hub note updating
-- audit-task #222 (see payload.rationale_for_view_over_column):
--
--   * Zero schema migration — view is metadata only
--   * Zero drift — single join definition, not copied across
--     backfill job + shadow_analysis.py + FE logic
--   * Clean-arch phase-3 compatible: when `position_resolutions`
--     table lands (PR-A of trades accounting refactor), we rewrite
--     the view body. Zero FE change, zero consumer change
--
-- LATERAL subquery guarantees exactly one outcome row per decision
-- even when an order_id resolves to multiple trade rows (2-leg,
-- multi-eval, re-fills). Picks the most recently resolved trade.
--
-- Idempotent — `CREATE OR REPLACE VIEW` is safe on every boot.

CREATE OR REPLACE VIEW strategy_decisions_resolved AS
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
    t.outcome,
    t.pnl_usd,
    t.resolved_at,
    t.sot_reconciliation_state
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
) t ON TRUE;

COMMENT ON VIEW strategy_decisions_resolved IS
    'Audit-task #222 — decisions enriched with resolved-trade outcome. ' ||
    'Read-only projection over strategy_decisions + trades. Designed to ' ||
    'be rewritten against position_resolutions table in clean-arch ' ||
    'phase-3 (see docs/CLEAN_ARCHITECT_MIGRATION_PLAN.md).';
