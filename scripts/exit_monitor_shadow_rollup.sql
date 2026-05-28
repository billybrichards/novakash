-- exit_monitor_shadow_rollup.sql
-- Analysis queries for the exit monitor shadow layer.
-- Run against RDS (or DuckDB parquet export) after collecting >= 7 days of data.
--
-- Sections:
--   1. Cross-threshold EV delta distribution
--   2. Recall + false-exit rate by threshold
--   3. Per-strategy breakdown
--   4. Time-of-day histogram of trigger times
--   5. eval_offset histogram at trigger
--   6. Backfill coverage check (how many rows still NULL)


-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Cross-threshold EV delta distribution
--    After 1+ week: what does each threshold deliver in ev_delta?
--    Positive ev_delta = shadow exit would have saved money vs holding to close.
-- ─────────────────────────────────────────────────────────────────────────────
SELECT
    trigger_threshold,
    COUNT(*)                                            AS trigger_count,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE realized_outcome IS NOT NULL)
        / NULLIF(COUNT(*), 0), 1
    )                                                   AS pct_backfilled,
    ROUND(AVG(ev_delta)::numeric, 4)                    AS avg_ev_delta,
    ROUND(STDDEV(ev_delta)::numeric, 4)                 AS stddev_ev_delta,
    ROUND(PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY ev_delta)::numeric, 4) AS p10_ev_delta,
    ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY ev_delta)::numeric, 4) AS p50_ev_delta,
    ROUND(PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY ev_delta)::numeric, 4) AS p90_ev_delta,
    COUNT(*) FILTER (WHERE ev_delta > 0)                AS triggers_with_positive_ev,
    COUNT(*) FILTER (WHERE ev_delta <= 0)               AS triggers_with_negative_ev
FROM exit_monitor_shadow
WHERE realized_outcome IS NOT NULL
  AND ev_delta IS NOT NULL
GROUP BY trigger_threshold
ORDER BY trigger_threshold;


-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Recall and false-exit rate by threshold
--    recall     = triggers on losers   / total losers in same period
--    false_rate = triggers on winners  / total winners in same period
-- ─────────────────────────────────────────────────────────────────────────────
WITH period AS (
    -- Adjust date range as needed
    SELECT
        MIN(created_at) AS start_ts,
        MAX(created_at) AS end_ts
    FROM exit_monitor_shadow
),
window_outcomes AS (
    -- Unique (decision_id, realized_outcome) pairs from shadow rows
    SELECT DISTINCT
        ems.decision_id,
        ems.realized_outcome
    FROM exit_monitor_shadow ems
    WHERE ems.realized_outcome IS NOT NULL
),
trigger_counts AS (
    SELECT
        ems.trigger_threshold,
        ems.realized_outcome,
        COUNT(DISTINCT ems.decision_id)  AS positions_triggered
    FROM exit_monitor_shadow ems
    WHERE ems.realized_outcome IS NOT NULL
    GROUP BY ems.trigger_threshold, ems.realized_outcome
),
outcome_totals AS (
    SELECT
        wo.realized_outcome,
        COUNT(DISTINCT wo.decision_id)   AS total_positions
    FROM window_outcomes wo
    GROUP BY wo.realized_outcome
)
SELECT
    tc.trigger_threshold,
    tc.realized_outcome,
    tc.positions_triggered,
    ot.total_positions,
    ROUND(
        100.0 * tc.positions_triggered / NULLIF(ot.total_positions, 0), 1
    )                                                   AS catch_rate_pct
FROM trigger_counts tc
JOIN outcome_totals ot ON ot.realized_outcome = tc.realized_outcome
ORDER BY tc.trigger_threshold, tc.realized_outcome;


-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Per-strategy breakdown
--    Shows which strategies are most often shadow-triggering and the avg EV.
-- ─────────────────────────────────────────────────────────────────────────────
SELECT
    strategy_id,
    tickformer_model,
    trigger_threshold,
    COUNT(*)                                        AS trigger_count,
    ROUND(AVG(p_against_at_trigger)::numeric, 4)   AS avg_p_against,
    ROUND(AVG(trigger_eval_offset)::numeric, 0)    AS avg_trigger_offset_s,
    ROUND(AVG(ev_delta)::numeric, 4)               AS avg_ev_delta,
    COUNT(*) FILTER (WHERE realized_outcome = 'LOSS') AS triggered_on_losers,
    COUNT(*) FILTER (WHERE realized_outcome = 'WIN')  AS triggered_on_winners
FROM exit_monitor_shadow
WHERE realized_outcome IS NOT NULL
GROUP BY strategy_id, tickformer_model, trigger_threshold
ORDER BY strategy_id, trigger_threshold;


-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Time-of-day histogram of trigger times (UTC hour)
--    Helps identify whether shadow triggers cluster at specific market hours
--    (e.g. US open vs Asian session).
-- ─────────────────────────────────────────────────────────────────────────────
SELECT
    EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC')    AS utc_hour,
    trigger_threshold,
    COUNT(*)                                            AS trigger_count,
    ROUND(AVG(ev_delta)::numeric, 4)                   AS avg_ev_delta
FROM exit_monitor_shadow
WHERE realized_outcome IS NOT NULL
GROUP BY utc_hour, trigger_threshold
ORDER BY trigger_threshold, utc_hour;


-- ─────────────────────────────────────────────────────────────────────────────
-- 5. eval_offset histogram at trigger time
--    How many seconds before close did shadows fire?
--    Binned in 30-second buckets. Validates the "median 97s lead time" claim.
-- ─────────────────────────────────────────────────────────────────────────────
SELECT
    trigger_threshold,
    WIDTH_BUCKET(trigger_eval_offset, 0, 300, 10)      AS offset_bucket,
    (WIDTH_BUCKET(trigger_eval_offset, 0, 300, 10) - 1) * 30  AS bucket_start_s,
    WIDTH_BUCKET(trigger_eval_offset, 0, 300, 10) * 30        AS bucket_end_s,
    COUNT(*)                                            AS trigger_count,
    COUNT(*) FILTER (WHERE realized_outcome = 'LOSS')  AS on_losers,
    COUNT(*) FILTER (WHERE realized_outcome = 'WIN')   AS on_winners
FROM exit_monitor_shadow
WHERE realized_outcome IS NOT NULL
  AND trigger_eval_offset BETWEEN 0 AND 300
GROUP BY trigger_threshold, offset_bucket
ORDER BY trigger_threshold, offset_bucket;


-- ─────────────────────────────────────────────────────────────────────────────
-- 6. Backfill coverage check
--    How many rows still have NULL realized_outcome? If many, check that the
--    BackfillRealizedOutcomeUseCase is being called at window resolution.
-- ─────────────────────────────────────────────────────────────────────────────
SELECT
    COUNT(*)                                            AS total_rows,
    COUNT(*) FILTER (WHERE realized_outcome IS NULL)    AS pending_backfill,
    COUNT(*) FILTER (WHERE realized_outcome IS NOT NULL) AS backfilled,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE realized_outcome IS NOT NULL)
        / NULLIF(COUNT(*), 0), 1
    )                                                   AS backfill_pct,
    MIN(created_at)                                     AS oldest_row,
    MAX(created_at)                                     AS newest_row
FROM exit_monitor_shadow;


-- ─────────────────────────────────────────────────────────────────────────────
-- 7. CLOB data availability
--    Quantifies how often clob_best_bid_held was captured (vs NULL stub).
--    Until the Polymarket CLOB sidecar exposes per-side depth, expect ~100%
--    non-NULL bids at ~$0.01 (structural bid-side stub).
-- ─────────────────────────────────────────────────────────────────────────────
SELECT
    COUNT(*)                                                       AS total_rows,
    COUNT(*) FILTER (WHERE clob_best_bid_held IS NOT NULL)         AS rows_with_clob_bid,
    ROUND(AVG(clob_best_bid_held)::numeric, 4)                     AS avg_clob_bid_held,
    ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY clob_best_bid_held)::numeric, 4)
                                                                   AS median_clob_bid_held
FROM exit_monitor_shadow;
