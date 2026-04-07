# CLOB Execution Audit Logging — Complete System

## Overview

Comprehensive database logging of ALL Polymarket CLOB activity for debugging FOK ladder behavior, order fills, and market microstructure analysis.

**Location:** Montreal VPS only (Polymarket geo-blocked elsewhere)

## Tables Created

### 1. `clob_execution_log` — Main Execution Tracking

Every FOK attempt, GTC placement, fill, or kill.

**Key Columns:**
- `window_ts, outcome` — Which window and which token (UP/DOWN)
- `token_id` — Polymarket token ID
- `direction, target_price, target_size` — Intended order
- `max_price (cap), min_price (floor)` — FOK bounds
- `clob_best_ask, clob_best_bid` — CLOB state at execution time
- `execution_mode` — 'FOK' or 'GTC'
- `fok_attempt_num, fok_max_attempts` — FOK ladder state
- `status` — 'submitted', 'filled', 'killed', 'timeout', 'error'
- `fill_price, fill_size` — Actual fill details
- `order_id` — Polymarket order ID
- `error_code, error_message` — Failure details
- `latency_ms` — Execution latency
- `metadata` — JSONB for flexibility

**Example Query:**
```sql
-- FOK ladder attempts that killed (no fill)
SELECT 
    el.ts, el.window_ts, el.outcome,
    el.target_price, el.clob_best_ask,
    COUNT(fla.id) as attempts,
    MAX(fla.fill_size) as max_fill
FROM clob_execution_log el
LEFT JOIN fok_ladder_attempts fla ON fla.execution_log_id = el.id
WHERE el.execution_mode = 'FOK' AND el.status = 'killed'
GROUP BY el.id
ORDER BY el.ts DESC
LIMIT 20;
```

### 2. `fok_ladder_attempts` — Individual FOK Attempts

One row per attempt within a FOK ladder execution.

**Key Columns:**
- `execution_log_id` — Links to main execution log
- `attempt_num` — 1, 2, 3, ...
- `attempt_price, attempt_size` — This attempt's parameters
- `clob_best_ask, clob_best_bid` — CLOB state at this attempt
- `status` — 'attempted', 'filled', 'killed'
- `fill_size, fill_price` — Fill details if filled
- `error_message` — Why this attempt failed
- `attempt_duration_ms` — Latency for this attempt

### 3. `clob_book_snapshots` — Complete Book on Every Poll

Every CLOB poll (default: every 2 seconds), not just during execution.

**Key Columns:**
- `window_ts, up_token_id, down_token_id`
- `up_best_bid, up_best_ask, up_bid_depth, up_ask_depth`
- `down_best_bid, down_best_ask, down_bid_depth, down_ask_depth`
- `up_spread, down_spread, mid_price`
- `up_bids_top5, up_asks_top5` — Top 5 levels as JSONB
- `down_bids_top5, down_asks_top5` — Top 5 levels as JSONB

**Example Query:**
```sql
-- CLOB depth evolution over time for a window
SELECT 
    ts, 
    up_best_ask, down_best_ask,
    up_bid_depth, down_bid_depth
FROM clob_book_snapshots
WHERE window_ts = 1775593500
ORDER BY ts;
```

### 4. `order_audit_log` — All Order Submissions

All orders (FOK, GTC, GTD) regardless of execution mode.

**Key Columns:**
- `order_id` — Unique order ID
- `direction, token_id, price, size, stake_usd`
- `execution_mode` — 'FOK', 'GTC', 'GTD'
- `status` — 'submitted', 'filled', 'cancelled', 'expired', 'rejected'
- `fill_price, fill_size, fill_time`
- `window_ts, outcome, eval_offset`
- `clob_best_ask, clob_best_bid` — CLOB state at submission

## Database Schema

File: `migrations/add_clob_execution_audit_tables.sql`

Run on Railway Postgres:
```bash
cd /home/novakash/novakash/engine
psql $DATABASE_URL < ../../migrations/add_clob_execution_audit_tables.sql
```

## Integration Points

### 1. FOK Ladder (`engine/execution/fok_ladder.py`)

**To be updated:** Log each attempt to `fok_ladder_attempts` and execution to `clob_execution_log`

**Current logging (console):**
```python
self._log.info(
    "fok_ladder.attempt",
    attempt=attempt,
    max_attempts=max_attempts,
    price=f"${attempt_price:.4f}",
    size=f"{size:.2f}",
    token_id=token_id[:20] + "..." if len(token_id) > 20 else token_id,
)
```

**To add:**
```python
await self._poly.db.write_fok_ladder_attempt({
    "execution_log_id": exec_id,
    "attempt_num": attempt,
    "attempt_price": attempt_price,
    "attempt_size": size,
    "clob_best_ask": best_ask,
    "status": "attempted",
    "attempt_duration_ms": duration_ms
})
```

### 2. CLOB Feed (`engine/data/feeds/clob_feed.py`)

**Updated:** Now writes to both `ticks_clob` (backwards compatible) and `clob_book_snapshots`

**Current code:**
```python
await self._pool.execute(
    """
    INSERT INTO clob_book_snapshots (...)
    VALUES (NOW(), $1, $2, $3, ...)
    ON CONFLICT (window_ts, up_token_id, down_token_id, ts) DO NOTHING
    """,
    "BTC", "5m", window.window_ts,
    window.up_token_id, window.down_token_id,
    up_best_bid, up_best_ask,
    down_best_bid, down_best_ask,
    up_spread, down_spread, mid
)
```

### 3. Trade Execution (`engine/strategies/five_min_vpin.py`)

**To be updated:** After FOK/GTC execution, log to `clob_execution_log`

**Current logging (console):**
```python
self._log.info(
    "place_order.live_submitted",
    direction=direction,
    price=price,
    stake_usd=stake_usd,
    order_id=order_id,
    ...
)
```

**To add:**
```python
await self._db.write_clob_execution_log({
    "window_ts": window.window_ts,
    "outcome": outcome,
    "token_id": token_id,
    "direction": "BUY",
    "strategy": "five_min_vpin",
    "eval_offset": eval_offset,
    "target_price": price,
    "target_size": size,
    "max_price": max_price,
    "clob_best_ask": clob_ask,
    "execution_mode": "FOK" if fok_result else "GTC",
    "status": "filled" if filled else "submitted",
    "fill_price": fill_price,
    "fill_size": fill_size,
    "order_id": order_id,
    "metadata": {"fok_attempts": fok_attempts}
})
```

## Debugging Queries

### FOK vs CLOB Price Gap
```sql
-- How often is CLOB above our cap?
SELECT 
    ts, window_ts, outcome,
    target_price, clob_best_ask,
    ROUND((clob_best_ask - target_price) / target_price * 100, 2) as price_gap_pct,
    status, fill_size
FROM clob_execution_log
WHERE execution_mode = 'FOK'
ORDER BY ts DESC
LIMIT 50;
```

### Fill Rate by Execution Mode
```sql
SELECT 
    execution_mode,
    COUNT(*) as total_orders,
    COUNT(CASE WHEN status = 'filled' THEN 1 END) as filled,
    ROUND(100.0 * COUNT(CASE WHEN status = 'filled' THEN 1 END) / COUNT(*), 2) as fill_rate_pct
FROM clob_execution_log
WHERE ts > NOW() - INTERVAL '24 hours'
GROUP BY execution_mode;
```

### FOK Ladder Depth Analysis
```sql
-- How many attempts before fill or kill?
SELECT 
    CASE 
        WHEN fill_size > 0 THEN 'filled_at_attempt_' || fok_fill_step
        ELSE 'killed_after_' || COUNT(*) || '_attempts'
    END as outcome,
    COUNT(*) as count
FROM clob_execution_log
WHERE execution_mode = 'FOK'
GROUP BY 1
ORDER BY 2 DESC;
```

### CLOB Spread Analysis
```sql
-- Typical spreads by time-to-close
SELECT 
    EXTRACT(EPOCH FROM (window_ts * 1000 - ts)) / 1000 as seconds_to_close,
    ROUND(AVG(up_spread), 4) as avg_up_spread,
    ROUND(AVG(down_spread), 4) as avg_down_spread,
    COUNT(*) as samples
FROM clob_book_snapshots
WHERE ts > NOW() - INTERVAL '7 days'
GROUP BY 1
ORDER BY 1;
```

## Montreal Compliance

- **All CLOB API calls:** Must originate from Montreal VPS (15.223.247.178)
- **Polymarket geo-blocking:** Other regions will get 403 errors
- **SSH access:** `ssh -i /root/.ssh/novakash-montreal.pem ubuntu@15.223.247.178`
- **Engine path:** `/home/novakash/novakash/engine`

## Migration Status

- [x] Schema created (`migrations/add_clob_execution_audit_tables.sql`)
- [ ] Run migration on Railway Postgres
- [ ] Update FOK ladder to log attempts
- [ ] Update trade execution to log to `clob_execution_log`
- [ ] Verify data flow end-to-end

## Next Steps

1. Run migration on Montreal VPS
2. Deploy updated FOK ladder with DB logging
3. Monitor first few windows for data quality
4. Add dashboard queries for real-time monitoring

## Related Files

- `migrations/add_clob_execution_audit_tables.sql` — Schema
- `engine/persistence/db_client.py` — DB write methods
- `engine/data/feeds/clob_feed.py` — CLOB polling
- `engine/execution/fok_ladder.py` — FOK execution
- `engine/strategies/five_min_vpin.py` — Trade orchestration
