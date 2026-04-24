-- Audit-task #255 (F1) — materialised view `strategy_skip_resolved`.
--
-- Problem: `/api/v58/strategy-decisions` and ad-hoc skip-bucket analyses
-- re-scan `strategy_decisions` (O(100K+ rows) per query) and re-join to
-- `window_snapshots` every call. Over the v6 LIVE era this made each
-- skip-bucket analysis ~5-30s — slow enough to block tuning sessions.
--
-- Fix: one row per (strategy_id, asset, window_ts, timeframe) carrying
--   * the final (latest) decision for that strategy on that window
--   * the resolved direction (shadow or real)
--   * a `hypo_outcome` WIN/LOSS flag for counterfactual WR, and
--   * the dedup_key extracted from metadata for downstream debugging.
--
-- Why materialised: the underlying CROSS-JOIN LATERAL + GROUP BY is the
-- expensive bit. With a matview we pay it once every 5 minutes, and
-- every skip-bucket query becomes an index scan.
--
-- Dedup rule: the engine re-evaluates the same (strategy, window) every
-- ~20ms while the window is open. Audit #256 documented the phantom
-- "1000 rows / 2 windows" artifact. Here we use DISTINCT ON
-- (strategy_id, asset, window_ts, timeframe) ORDER BY evaluated_at DESC
-- so we keep the FINAL state of the strategy's decision for each window
-- — which is what tuning analysis cares about.
--
-- Strategy-agnostic: no hard-coded strategy_id filter. v4/v5/v6/v10 all
-- populate via the same projection. New strategies auto-appear when the
-- matview refreshes.
--
-- Refresh strategy: REFRESH MATERIALIZED VIEW CONCURRENTLY on a 5-minute
-- APScheduler tick (see hub/main.py startup wiring). CONCURRENTLY needs
-- a UNIQUE index on the matview, added below.
--
-- Idempotent: DROP MATERIALIZED VIEW IF EXISTS + recreate. The refresh
-- wiring in hub/main.py tolerates both the "first boot — matview empty"
-- and "mid-refresh" cases.

DROP MATERIALIZED VIEW IF EXISTS strategy_skip_resolved CASCADE;

CREATE MATERIALIZED VIEW strategy_skip_resolved AS
WITH latest_decision AS (
    -- One row per (strategy, window). Keep the latest evaluation — this
    -- is the strategy's FINAL verdict for the window, after all re-evals.
    SELECT DISTINCT ON (strategy_id, asset, window_ts, timeframe)
        strategy_id,
        strategy_version,
        asset,
        window_ts,
        timeframe,
        eval_offset,
        mode,
        action,
        direction,
        confidence,
        confidence_score,
        entry_cap,
        collateral_pct,
        entry_reason,
        skip_reason,
        executed,
        order_id,
        fill_price,
        fill_size,
        metadata_json,
        metadata_json->>'dedup_key' AS dedup_key,
        evaluated_at
    FROM strategy_decisions
    ORDER BY strategy_id, asset, window_ts, timeframe, evaluated_at DESC
),
re_eval_counts AS (
    -- How many times the engine re-evaluated this (strategy, window).
    -- Exposed on the matview so analysis can distinguish stable windows
    -- from heavily churned ones (see audit #256).
    SELECT
        strategy_id, asset, window_ts, timeframe,
        COUNT(*) AS re_eval_count
    FROM strategy_decisions
    GROUP BY strategy_id, asset, window_ts, timeframe
)
SELECT
    ld.strategy_id,
    ld.strategy_version,
    ld.asset,
    ld.window_ts,
    ld.timeframe,
    ld.eval_offset,
    ld.mode,
    ld.action,
    ld.direction,
    ld.confidence,
    ld.confidence_score,
    ld.entry_cap,
    ld.collateral_pct,
    ld.entry_reason,
    ld.skip_reason,
    ld.executed,
    ld.order_id,
    ld.fill_price,
    ld.fill_size,
    ld.metadata_json,
    ld.dedup_key,
    ld.evaluated_at,
    rc.re_eval_count,
    -- Resolved direction: prefer window_snapshots.actual_direction,
    -- fall back to poly_winner (legacy), else NULL.
    COALESCE(snap.actual_direction, UPPER(snap.poly_winner)) AS resolved_direction,
    snap.close_price AS resolved_close_price,
    snap.open_price AS resolved_open_price,
    -- hypo_outcome: what would the strategy's WR look like if every
    -- committed direction resolved at face value? NULL for SKIP /
    -- unresolved windows.
    CASE
        WHEN ld.direction IS NOT NULL
         AND COALESCE(snap.actual_direction, UPPER(snap.poly_winner)) IS NOT NULL
        THEN CASE
            WHEN ld.direction = COALESCE(snap.actual_direction, UPPER(snap.poly_winner))
            THEN 'WIN'
            ELSE 'LOSS'
        END
        ELSE NULL
    END AS hypo_outcome,
    -- Real-fill outcome (if the order actually resolved on-chain).
    t.outcome AS real_outcome,
    t.pnl_usd AS real_pnl_usd,
    t.resolved_at AS real_resolved_at,
    t.sot_reconciliation_state
FROM latest_decision ld
JOIN re_eval_counts rc USING (strategy_id, asset, window_ts, timeframe)
LEFT JOIN window_snapshots snap
    ON snap.asset = ld.asset
   AND snap.window_ts = ld.window_ts
   AND snap.timeframe = ld.timeframe
LEFT JOIN LATERAL (
    SELECT outcome, pnl_usd, resolved_at, sot_reconciliation_state
    FROM trades
    WHERE trades.order_id = ld.order_id
      AND ld.order_id IS NOT NULL
    ORDER BY resolved_at DESC NULLS LAST, created_at DESC
    LIMIT 1
) t ON TRUE;

-- UNIQUE index required for REFRESH ... CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS ux_strategy_skip_resolved
    ON strategy_skip_resolved (strategy_id, asset, window_ts, timeframe);

-- Partial indexes for the hot paths.
CREATE INDEX IF NOT EXISTS ix_ssr_strategy_action
    ON strategy_skip_resolved (strategy_id, action);
CREATE INDEX IF NOT EXISTS ix_ssr_skip_reason
    ON strategy_skip_resolved (strategy_id, skip_reason)
    WHERE action = 'SKIP';
CREATE INDEX IF NOT EXISTS ix_ssr_evaluated_at
    ON strategy_skip_resolved (evaluated_at DESC);
CREATE INDEX IF NOT EXISTS ix_ssr_resolved
    ON strategy_skip_resolved (strategy_id, hypo_outcome)
    WHERE hypo_outcome IS NOT NULL;

COMMENT ON MATERIALIZED VIEW strategy_skip_resolved IS
    'Audit #255 F1 — one row per (strategy, window). Deduped via ' ||
    'DISTINCT ON latest evaluation. Joins to window_snapshots for shadow ' ||
    'resolution + trades for real fill outcome. Refreshed every 5 min. ' ||
    'Used by /api/v58/skip-bucket-analysis for sub-second tuning queries.';
