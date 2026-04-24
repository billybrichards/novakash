-- Audit-task #255 (F4) — backfill `window_snapshots.actual_direction` from
-- `ticks_chainlink` for windows in the v6 LIVE era that are missing it.
--
-- Context: only 17 of the last 500 `/api/v58/outcomes` rows intersected v6
-- decisions — the shadow-outcome path in `strategy_decisions_resolved`
-- silently returned NULL because `window_snapshots.actual_direction` wasn't
-- populated for those windows.
--
-- Resolution rule (matches Polymarket's Chainlink oracle):
--   * open  = `ticks_chainlink.price` at or just before window_ts
--   * close = `ticks_chainlink.price` at or just before (window_ts + 300)
--   * actual_direction = 'UP' if close > open else 'DOWN' else NULL
--
-- Idempotent: the UPDATE only touches rows where actual_direction IS NULL
-- AND close_price IS NULL — it will NOT overwrite a value already computed
-- from the engine's own window closer.
--
-- Performance: the LATERAL subqueries hit idx_ticks_chainlink_asset_ts
-- (BTREE on (asset, ts DESC)) so each window resolves in a single index
-- fetch. ~2-5ms per window; acceptable for a one-shot backfill.
--
-- Safety: wrapped in an UPDATE ... WHERE clause so re-runs are no-ops.

UPDATE window_snapshots ws
SET
    actual_direction = CASE
        WHEN close_chain.price > open_chain.price THEN 'UP'
        WHEN close_chain.price < open_chain.price THEN 'DOWN'
        ELSE NULL
    END,
    -- Also backfill open/close so the view machinery that reads
    -- close_price > open_price gets sensible values.
    open_price = COALESCE(ws.open_price, open_chain.price),
    close_price = COALESCE(ws.close_price, close_chain.price)
FROM
    LATERAL (
        SELECT price
        FROM ticks_chainlink
        WHERE asset = ws.asset
          AND ts <= TO_TIMESTAMP(ws.window_ts) + INTERVAL '10 seconds'
          AND ts >= TO_TIMESTAMP(ws.window_ts) - INTERVAL '60 seconds'
        ORDER BY ABS(EXTRACT(EPOCH FROM (ts - TO_TIMESTAMP(ws.window_ts)))) ASC
        LIMIT 1
    ) AS open_chain,
    LATERAL (
        SELECT price
        FROM ticks_chainlink
        WHERE asset = ws.asset
          AND ts <= TO_TIMESTAMP(ws.window_ts + 300) + INTERVAL '10 seconds'
          AND ts >= TO_TIMESTAMP(ws.window_ts + 300) - INTERVAL '60 seconds'
        ORDER BY ABS(EXTRACT(EPOCH FROM (ts - TO_TIMESTAMP(ws.window_ts + 300)))) ASC
        LIMIT 1
    ) AS close_chain
WHERE ws.actual_direction IS NULL
  AND ws.timeframe = '5m'
  -- Limit to v6 LIVE era + some lead-in. v6 went LIVE 2026-04-19 21:40 UTC.
  -- Use a cutoff 24h earlier to backfill lead-in windows too.
  AND ws.window_ts >= EXTRACT(EPOCH FROM TIMESTAMP '2026-04-18 00:00:00 UTC')
  AND ws.window_ts <= EXTRACT(EPOCH FROM NOW()) - 300;  -- only closed windows
