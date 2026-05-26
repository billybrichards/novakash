-- Backfill XRP/SOL 5m window outcomes from ticks_chainlink.
--
-- Context: RDS note #712 (2026-05-26). The forward writer
-- (PgWindowRepository.populate_oracle_outcomes) was BTC/ETH-only until
-- this PR added XRP and SOL to _GAMMA_SLUG_PREFIXES. Going forward,
-- newly-closed XRP/SOL windows resolve via Gamma like BTC/ETH; this
-- script fills in the *historical* XRP/SOL backlog that was never polled
-- (~108k signal_evaluations rows + ~150k window_snapshots rows over the
-- last 7 days at the time of the audit).
--
-- Methodology:
--   * Open price  = latest ticks_chainlink row at-or-before window_ts
--   * Close price = latest ticks_chainlink row at-or-before window_ts + 300
--   * UP if close > open, DOWN if close < open, FLAT if equal.
--
-- This is the same canonical price source Polymarket uses for resolution
-- (Chainlink on Polygon, see docs/DATA_FEEDS.md). Expect minor drift vs.
-- Polymarket's actual oracle_outcome on flat windows where Chainlink's
-- 5-second polling cadence + the ~30s heartbeat causes the tick at the
-- window boundary to land just before/after the true price-feed update.
-- Cross-check on BTC against the live Gamma-stamped column suggests
-- ~82% agreement (vs ~95%+ for non-flat windows).
--
-- Idempotent: writes only where outcome IS NULL (or oracle_outcome IS
-- NULL). Safe to re-run; safe to run while the engine is live.
--
-- Run: psql ... -f scripts/backfill_xrp_sol_outcomes_from_chainlink.sql
--
-- Audit columns updated:
--   window_snapshots: outcome, oracle_outcome, poly_resolved_outcome,
--                     poly_winner  (only where NULL — COALESCE-safe)
--   signal_evaluations: outcome (only where NULL)

BEGIN;

-- ─── XRP + SOL backfill ──────────────────────────────────────────────
-- Single CTE chain so the same derivation is reused for both tables.

WITH unresolved AS (
    SELECT DISTINCT asset, timeframe, window_ts
      FROM window_snapshots
     WHERE asset IN ('XRP', 'SOL')
       AND timeframe = '5m'
       AND (outcome IS NULL OR oracle_outcome IS NULL)
       -- Only windows that have had time to resolve (>6 min old).
       AND window_ts < EXTRACT(EPOCH FROM NOW())::bigint - 360
),
priced AS (
    SELECT
        u.asset,
        u.timeframe,
        u.window_ts,
        (SELECT price FROM ticks_chainlink
            WHERE asset = u.asset
              AND ts <= to_timestamp(u.window_ts)
            ORDER BY ts DESC LIMIT 1) AS open_price,
        (SELECT price FROM ticks_chainlink
            WHERE asset = u.asset
              AND ts <= to_timestamp(u.window_ts + 300)
            ORDER BY ts DESC LIMIT 1) AS close_price
      FROM unresolved u
),
derived AS (
    SELECT
        asset,
        timeframe,
        window_ts,
        open_price,
        close_price,
        CASE
            WHEN open_price IS NULL OR close_price IS NULL THEN NULL
            WHEN close_price > open_price THEN 'UP'
            WHEN close_price < open_price THEN 'DOWN'
            ELSE 'FLAT'
        END AS outcome
      FROM priced
     WHERE open_price IS NOT NULL AND close_price IS NOT NULL
)
-- Stash the derivation in a temp table so we can update both tables
-- without re-running the (expensive) ticks_chainlink lookup.
SELECT * INTO TEMP TABLE _xrp_sol_outcomes FROM derived;

\echo 'Derived outcomes:'
SELECT asset, outcome, COUNT(*) FROM _xrp_sol_outcomes GROUP BY 1,2 ORDER BY 1,2;

-- window_snapshots: COALESCE so we never overwrite a real Gamma resolution
UPDATE window_snapshots ws
   SET outcome               = COALESCE(ws.outcome, d.outcome),
       oracle_outcome        = COALESCE(ws.oracle_outcome, d.outcome),
       poly_resolved_outcome = COALESCE(ws.poly_resolved_outcome, d.outcome),
       poly_winner           = COALESCE(ws.poly_winner, d.outcome)
  FROM _xrp_sol_outcomes d
 WHERE ws.asset     = d.asset
   AND ws.timeframe = d.timeframe
   AND ws.window_ts = d.window_ts
   AND (ws.outcome IS NULL OR ws.oracle_outcome IS NULL);

-- signal_evaluations: idempotent NULL-only fill
UPDATE signal_evaluations se
   SET outcome = d.outcome
  FROM _xrp_sol_outcomes d
 WHERE se.asset     = d.asset
   AND se.timeframe = d.timeframe
   AND se.window_ts = d.window_ts
   AND se.outcome IS NULL
   AND d.outcome IN ('UP', 'DOWN', 'FLAT');

DROP TABLE _xrp_sol_outcomes;

COMMIT;

-- ─── Verification ────────────────────────────────────────────────────
SELECT
    asset,
    timeframe,
    COUNT(*)                       AS n_rows,
    COUNT(outcome)                 AS n_outcome,
    ROUND(100.0 * COUNT(outcome) / NULLIF(COUNT(*), 0), 1) AS pct_outcome
  FROM signal_evaluations
 WHERE asset IN ('XRP', 'SOL', 'BTC', 'ETH')
   AND timeframe = '5m'
   AND window_ts > EXTRACT(EPOCH FROM NOW())::bigint - 7*86400
 GROUP BY 1, 2
 ORDER BY 1, 2;
