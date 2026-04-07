# Reconciliation Service — Design Document

> **Status:** Draft — ready for implementation
> **Author:** Novakash2 (subagent research pass)
> **Date:** 2026-04-07
> **Deployment target:** Montreal (VPS / same host as the trading engine)

---

## Problem Statement

Our trading engine records orders and trades to the DB at submission time, but the DB state can diverge from the actual CLOB state:

- **Orphaned OPEN orders** — our DB says `OPEN` but CLOB says `MATCHED`, `CANCELLED`, or `EXPIRED`
- **Missing fill data** — we know an order was submitted but don't have the actual `size_matched`, execution price, or `transaction_hash`
- **Stale USDC balance** — we estimate balance locally but don't confirm against chain state
- **Position drift** — positions accumulate from multiple fills; our DB may be ahead/behind the actual on-chain position

This service is the **source of truth synchroniser** — it takes CLOB data and makes our DB consistent with it.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                      Montreal VPS                               │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │               Reconciliation Service                    │    │
│  │                                                         │    │
│  │   ┌─────────────┐    ┌──────────────┐    ┌──────────┐  │    │
│  │   │  CLOB Poller │   │  DB Updater  │    │ Orphan   │  │    │
│  │   │  (30s loop)  │──▶│  (upsert)    │◀──▶│ Detector │  │    │
│  │   └─────────────┘    └──────────────┘    └──────────┘  │    │
│  │          │                   │                          │    │
│  │          ▼                   ▼                          │    │
│  │   ┌─────────────┐    ┌──────────────┐                  │    │
│  │   │ Balance      │   │  Balance     │                   │    │
│  │   │ Poller       │──▶│  History     │                   │    │
│  │   └─────────────┘    └──────────────┘                  │    │
│  └─────────────────────────────────────────────────────────┘    │
│                            │                                    │
│                      PostgreSQL DB                              │
│                      (shared with engine)                       │
└─────────────────────────────────────────────────────────────────┘
         │ periodic event push / webhook
         ▼
┌───────────────────────┐
│  Railway: macro-observer │
│  (AI evaluation layer)   │
└───────────────────────┘
```

---

## Components

### 1. CLOB Poller (30s Loop)

The core loop. Runs every 30 seconds and performs:

1. **Balance sync** — GET USDC balance, write to `wallet_balance_history`
2. **Order sweep** — paginate through all CLOB open orders, upsert to `trades`
3. **Trade/fill ingestion** — fetch new fills since last run, insert to `trades` / `fills`
4. **Orphan detection** — compare DB `OPEN` orders against CLOB, flag mismatches

```python
async def reconciliation_loop():
    while True:
        try:
            await sync_balance()
            await sync_orders()
            await sync_trades()
            await detect_orphans()
        except Exception as e:
            logger.error(f"Recon loop error: {e}")
            await alert_on_failure(e)
        await asyncio.sleep(30)
```

**Design decisions:**
- **Sequential, not concurrent** — avoids DB write conflicts and rate limit spikes
- **Idempotent upserts** — safe to re-run on crash/restart
- **State file / DB cursor** — persist `last_trade_cursor` and `last_sync_ts` in DB so restarts don't re-scan everything
- **Graceful failure** — individual sub-task failure logs and continues; doesn't crash the loop

---

### 2. Balance Sync

```python
async def sync_balance():
    balance_data = clob_client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    usdc_balance = float(balance_data["balance"])
    
    await db.execute("""
        INSERT INTO wallet_balance_history 
            (wallet_address, balance_usdc, allowance_usdc, recorded_at)
        VALUES ($1, $2, $3, NOW())
    """, WALLET_ADDRESS, usdc_balance, float(balance_data["allowance"]))
    
    # Also update the live balance in a summary table
    await db.execute("""
        INSERT INTO wallet_state (wallet_address, usdc_balance, last_synced_at)
        VALUES ($1, $2, NOW())
        ON CONFLICT (wallet_address) DO UPDATE
            SET usdc_balance = $2, last_synced_at = NOW()
    """, WALLET_ADDRESS, usdc_balance)
```

**Rate:** 200 req/10s available. We make 1 req per 30s — no concern.

---

### 3. Order Sync

Paginates through CLOB `/data/orders` and upserts to our `trades` table:

```python
async def sync_orders():
    cursor = "MA=="
    while cursor and cursor != "LTE=":
        resp = clob_client.get_orders(OpenOrderParams(next_cursor=cursor))
        orders = resp.get("data", [])
        cursor = resp.get("next_cursor")
        
        for order in orders:
            await upsert_order(order)
        
        if not orders:
            break

async def upsert_order(clob_order: dict):
    await db.execute("""
        INSERT INTO trades (
            clob_order_id, market_condition_id, token_id, side,
            original_size, size_matched, size_filled, price,
            order_type, status, expiration, created_at, maker_address,
            last_synced_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, NOW())
        ON CONFLICT (clob_order_id) DO UPDATE SET
            status = EXCLUDED.status,
            size_matched = EXCLUDED.size_matched,
            size_filled = EXCLUDED.size_filled,
            last_synced_at = NOW()
        WHERE trades.status != EXCLUDED.status 
           OR trades.size_matched != EXCLUDED.size_matched
    """, 
        clob_order["id"], clob_order["market"], clob_order["asset_id"],
        clob_order["side"], float(clob_order["original_size"]),
        float(clob_order["size_matched"]), float(clob_order.get("size_filled", 0)),
        float(clob_order["price"]), clob_order["type"], clob_order["status"],
        clob_order.get("expiration"), clob_order["created_at"],
        clob_order["maker_address"]
    )
```

**Key field:** `size_matched / original_size` = fill ratio. If this is < 1.0 and status = `LIVE`, the order is partially filled.

---

### 4. Trade/Fill Ingestion

Fetches fills from CLOB `/data/trades` incrementally using `after` timestamp:

```python
async def sync_trades():
    last_cursor = await db.fetchval(
        "SELECT last_trade_cursor FROM recon_state WHERE id = 1"
    )
    
    cursor = last_cursor or "MA=="
    while cursor and cursor != "LTE=":
        resp = clob_client.get_trades(
            TradeParams(maker_address=WALLET_ADDRESS, next_cursor=cursor)
        )
        fills = resp.get("data", [])
        new_cursor = resp.get("next_cursor")
        
        for fill in fills:
            await insert_fill(fill)
        
        # Persist cursor after each page
        await db.execute(
            "UPDATE recon_state SET last_trade_cursor = $1 WHERE id = 1",
            new_cursor
        )
        cursor = new_cursor
        
        if not fills:
            break

async def insert_fill(fill: dict):
    # Size is in micro-units — divide by 1e6
    size = int(fill["size"]) / 1_000_000
    
    await db.execute("""
        INSERT INTO trade_fills (
            fill_id, taker_order_id, market_condition_id, token_id,
            side, size, price, fee_rate_bps, status,
            match_time, transaction_hash, trader_side
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, to_timestamp($10), $11, $12)
        ON CONFLICT (fill_id) DO NOTHING
    """,
        fill["id"], fill["taker_order_id"], fill["market"], fill["asset_id"],
        fill["side"], size, float(fill["price"]),
        int(fill.get("fee_rate_bps", 0)), fill["status"],
        int(fill["match_time"]), fill.get("transaction_hash"), fill.get("trader_side")
    )
    
    # Back-fill the order record with transaction_hash if we have it
    if fill.get("transaction_hash") and fill.get("taker_order_id"):
        await db.execute("""
            UPDATE trades 
            SET transaction_hash = $1, status = 'MATCHED', last_synced_at = NOW()
            WHERE clob_order_id = $2 AND transaction_hash IS NULL
        """, fill["transaction_hash"], fill["taker_order_id"])
```

---

### 5. Orphan Detector

Compares our DB's `OPEN` orders against what CLOB knows:

```python
async def detect_orphans():
    # All orders our DB thinks are open
    db_open_orders = await db.fetch("""
        SELECT clob_order_id, created_at, market_condition_id, size_matched
        FROM trades
        WHERE status = 'OPEN'
          AND created_at < NOW() - INTERVAL '2 minutes'  -- grace period for new orders
    """)
    
    orphans = []
    for order in db_open_orders:
        try:
            clob_order = clob_client.get_order(order["clob_order_id"])
            clob_status = clob_order.get("status", "UNKNOWN")
            
            if clob_status in ("MATCHED", "CANCELLED", "EXPIRED"):
                orphans.append({
                    "order_id": order["clob_order_id"],
                    "db_status": "OPEN",
                    "clob_status": clob_status,
                    "size_matched": clob_order.get("size_matched", 0)
                })
                
                # Fix it immediately
                await db.execute("""
                    UPDATE trades
                    SET status = $1,
                        size_matched = $2,
                        last_synced_at = NOW(),
                        orphan_resolved_at = NOW()
                    WHERE clob_order_id = $3
                """, clob_status, float(clob_order.get("size_matched", 0)),
                    order["clob_order_id"])
                    
        except Exception as e:
            logger.warning(f"Couldn't verify order {order['clob_order_id']}: {e}")
    
    if orphans:
        logger.warning(f"Resolved {len(orphans)} orphaned orders: {orphans}")
        await alert_orphans(orphans)
    
    return orphans
```

**Rate consideration:** This does 1 CLOB request per open order. At 500 req/10s for `/data/order/<id>`, this is fine unless we have >500 concurrent open orders. For large batches, use the paginated `/data/orders` sweep instead.

---

## Database Schema

### New tables required

```sql
-- Wallet balance history (append-only time series)
CREATE TABLE wallet_balance_history (
    id                BIGSERIAL PRIMARY KEY,
    wallet_address    TEXT NOT NULL,
    balance_usdc      NUMERIC(20, 6) NOT NULL,
    allowance_usdc    NUMERIC(20, 6),
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_wallet_balance_wallet ON wallet_balance_history(wallet_address, recorded_at DESC);

-- Live wallet state (single-row summary per wallet)
CREATE TABLE wallet_state (
    wallet_address    TEXT PRIMARY KEY,
    usdc_balance      NUMERIC(20, 6) NOT NULL,
    last_synced_at    TIMESTAMPTZ NOT NULL
);

-- Individual trade fills from CLOB
CREATE TABLE trade_fills (
    fill_id               TEXT PRIMARY KEY,
    taker_order_id        TEXT NOT NULL,
    market_condition_id   TEXT,
    token_id              TEXT,
    side                  TEXT,
    size                  NUMERIC(20, 6),        -- in USDC/shares (normalised)
    price                 NUMERIC(10, 6),
    fee_rate_bps          INT DEFAULT 0,
    status                TEXT,
    match_time            TIMESTAMPTZ,
    transaction_hash      TEXT,
    trader_side           TEXT,                  -- TAKER or MAKER
    inserted_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_trade_fills_order ON trade_fills(taker_order_id);
CREATE INDEX idx_trade_fills_market ON trade_fills(market_condition_id, match_time DESC);

-- Reconciliation service state
CREATE TABLE recon_state (
    id                    INT PRIMARY KEY DEFAULT 1,
    last_trade_cursor     TEXT DEFAULT 'MA==',
    last_order_cursor     TEXT DEFAULT 'MA==',
    last_balance_sync_at  TIMESTAMPTZ,
    last_order_sync_at    TIMESTAMPTZ,
    last_trade_sync_at    TIMESTAMPTZ,
    last_orphan_check_at  TIMESTAMPTZ,
    created_at            TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT single_row CHECK (id = 1)
);

INSERT INTO recon_state (id) VALUES (1) ON CONFLICT DO NOTHING;
```

### Columns to add to existing `trades` table

```sql
-- Add to existing trades table
ALTER TABLE trades ADD COLUMN IF NOT EXISTS clob_order_id TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS size_matched NUMERIC(20, 6);
ALTER TABLE trades ADD COLUMN IF NOT EXISTS size_filled NUMERIC(20, 6);
ALTER TABLE trades ADD COLUMN IF NOT EXISTS original_size NUMERIC(20, 6);
ALTER TABLE trades ADD COLUMN IF NOT EXISTS transaction_hash TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS maker_address TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS orphan_resolved_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_trades_clob_order_id ON trades(clob_order_id);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status) WHERE status = 'OPEN';
```

---

## Service Configuration

```python
# recon_config.py
RECON_CONFIG = {
    "poll_interval_seconds": 30,
    "orphan_check_interval_seconds": 60,   # run orphan detector every 2nd cycle
    "balance_check_interval_seconds": 30,
    "order_page_size": 100,                # CLOB default
    "trade_page_size": 100,
    "new_order_grace_period_seconds": 120, # don't orphan-check orders < 2 min old
    "max_consecutive_errors": 5,           # alert after this many back-to-back failures
    "clob_host": "https://clob.polymarket.com",
    "chain_id": 137,
}
```

---

## Integration with macro-observer on Railway

The macro-observer (Railway) is the AI evaluation component that decides when to trade. The reconciliation service on Montreal feeds it with accurate state via:

### Option A: Shared DB (recommended if same Postgres)

macro-observer reads directly from `wallet_state`, `wallet_balance_history`, and `trade_fills`. No additional API needed.

```python
# macro-observer reads:
current_balance = await db.fetchval(
    "SELECT usdc_balance FROM wallet_state WHERE wallet_address = $1",
    WALLET_ADDRESS
)
open_positions = await db.fetch("""
    SELECT conditionId, size, avgPrice 
    FROM positions 
    WHERE wallet_address = $1 AND status = 'OPEN'
""", WALLET_ADDRESS)
```

### Option B: Internal Webhook Push (if different hosts)

Reconciliation service pushes events to macro-observer after significant state changes:

```python
# After orphan resolution or significant fill
await httpx.post(f"{MACRO_OBSERVER_URL}/internal/state-update", json={
    "event": "order_resolved",
    "order_id": order_id,
    "new_status": clob_status,
    "size_matched": size_matched,
    "wallet_balance_usdc": current_balance,
    "timestamp": int(time.time())
}, headers={"X-Internal-Key": INTERNAL_SECRET})
```

macro-observer exposes:
```
POST /internal/state-update  — receive reconciliation events
GET  /internal/health        — recon service heartbeat check
```

### Option C: Redis Pub/Sub (if low-latency updates needed)

Reconciliation service publishes to a Redis channel; macro-observer subscribes:

```python
# Publish (Montreal recon service)
redis.publish("polymarket:recon:events", json.dumps({
    "type": "balance_update",
    "balance_usdc": current_balance,
    "ts": int(time.time())
}))

# Subscribe (macro-observer on Railway)
pubsub = redis.pubsub()
pubsub.subscribe("polymarket:recon:events")
```

**Recommendation:** Start with **Option A (shared DB)** if both services use the same Postgres. Simplest, no extra infra. Upgrade to Option B if Railway and Montreal have separate databases.

---

## Deployment

### File structure (Montreal)

```
services/
  reconciliation/
    __init__.py
    recon_service.py       # main loop
    balance_sync.py        # balance poller
    order_sync.py          # order sweep
    trade_sync.py          # fill ingestion  
    orphan_detector.py     # orphan detection
    alerts.py              # alerting (Telegram / log)
    config.py              # configuration
    models.py              # DB models/helpers
    migrations/
      001_add_recon_tables.sql
      002_add_trades_columns.sql
    tests/
      test_orphan_detector.py
      test_trade_sync.py
```

### Systemd service (Montreal)

```ini
[Unit]
Description=Polymarket Reconciliation Service
After=postgresql.service network.target

[Service]
Type=simple
User=novakash
WorkingDirectory=/opt/novakash
ExecStart=/opt/novakash/.venv/bin/python -m services.reconciliation.recon_service
Restart=always
RestartSec=10
Environment=PYTHONPATH=/opt/novakash
EnvironmentFile=/opt/novakash/.env

[Install]
WantedBy=multi-user.target
```

### Or via Docker Compose (if containerised)

```yaml
services:
  reconciliation:
    build: .
    command: python -m services.reconciliation.recon_service
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - POLYMARKET_PRIVATE_KEY=${POLYMARKET_PRIVATE_KEY}
      - WALLET_ADDRESS=${WALLET_ADDRESS}
      - MACRO_OBSERVER_URL=${MACRO_OBSERVER_URL}
      - INTERNAL_SECRET=${INTERNAL_SECRET}
    restart: unless-stopped
    depends_on:
      - db
```

---

## Alerting

The service should alert via Telegram (via novakash2 bot) for:

| Event | Severity | Threshold |
|-------|----------|-----------|
| Orphaned orders resolved | WARNING | Any |
| USDC balance drops below threshold | CRITICAL | < $100 |
| Reconciliation loop failure | ERROR | > 2 consecutive |
| Trade fill not in DB | WARNING | Any |
| CLOB → DB divergence > 5 orders | CRITICAL | 5+ |

---

## Testing Plan

1. **Unit tests** — mock CLOB client, verify upsert logic
2. **Integration test** — spin up against staging CLOB, verify full loop
3. **Orphan injection test** — manually create a DB `OPEN` order for a known-closed CLOB order, verify it gets resolved
4. **Cursor persistence test** — kill service mid-run, restart, verify no duplicate fills
5. **Rate limit test** — run with 200 open orders, verify no 429s

---

## Roll-out Plan

1. **Week 1:** Deploy migrations + `wallet_balance_history` table
2. **Week 1:** Deploy balance sync only (safest first)
3. **Week 2:** Enable order sync (read-only upserts, no status changes yet)
4. **Week 2:** Enable orphan detector in **read-only mode** (log but don't fix)
5. **Week 3:** Enable auto-fix for orphans after validating detection accuracy
6. **Week 3:** Enable trade fill ingestion
7. **Week 4:** Wire macro-observer integration (DB reads or webhook)

---

## Open Questions for Billy

1. **Same Postgres as the trading engine?** Confirms Option A for macro-observer integration.
2. **What's the current `trades` table schema?** Need to confirm column names before writing migration.
3. **Do we use a proxy wallet (funder address) or direct EOA?** Affects which address to pass as `maker_address` to CLOB.
4. **Should orphan resolution auto-cancel on CLOB side** (if order is actually still live but we think it's matched) — or just flag it?
5. **Alert destination** — existing Telegram channel or new one for infra alerts?

---

## References

- [POLYMARKET-API-AUDIT.md](./POLYMARKET-API-AUDIT.md) — full endpoint reference
- [ARCHITECTURE.md](./ARCHITECTURE.md) — system architecture overview
- [DATABASE.md](./DATABASE.md) — current DB schema
- [DEPLOYMENT.md](./DEPLOYMENT.md) — Montreal deployment config
