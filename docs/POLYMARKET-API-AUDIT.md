# Polymarket API Audit — Data Reconciliation Reference

> **Purpose:** Comprehensive audit of Polymarket CLOB, Data, and Gamma APIs for building a wallet/position/order reconciliation service on Montreal.
>
> **Last updated:** 2026-04-07
>
> **Sources:** https://docs.polymarket.com · https://github.com/Polymarket/py-clob-client

---

## API Overview

Polymarket exposes **three separate APIs**:

| API | Base URL | Auth Required | Primary Use |
|-----|----------|---------------|-------------|
| **CLOB API** | `https://clob.polymarket.com` | L2 for trading/ledger endpoints | Order placement, cancellation, balance, order/trade ledger |
| **Data API** | `https://data-api.polymarket.com` | None (public) | User positions, trade history, leaderboards |
| **Gamma API** | `https://gamma-api.polymarket.com` | None (public) | Market/event metadata |

---

## Authentication

Two-level system:

- **L1 (Private Key)**: EIP-712 signature — used to create/derive API credentials
- **L2 (API Key)**: HMAC-SHA256 signed headers — used for all authenticated requests

### L2 Headers required:
```
POLY_ADDRESS         — wallet address
POLY_SIGNATURE       — HMAC-SHA256 signature
POLY_TIMESTAMP       — Unix timestamp
POLY_NONCE           — nonce
POLY_API_KEY         — API key (UUID)
```

> The Data API and Gamma API require **no auth** at all. Use these for reconciliation reads where possible to avoid burning L2 rate limits.

---

## Rate Limits

All limits use a **sliding window** via Cloudflare throttling (delayed/queued, not immediately rejected).

### CLOB API (`https://clob.polymarket.com`)

| Endpoint | Limit |
|----------|-------|
| General | 9,000 req / 10s |
| `GET /balance-allowance` | **200 req / 10s** |
| `GET /data/orders`, `GET /data/trades`, `GET /orders`, `GET /trades` | **900 req / 10s** |
| `/data/orders` | 500 req / 10s |
| `/data/trades` | 500 req / 10s |

### Data API (`https://data-api.polymarket.com`)

| Endpoint | Limit |
|----------|-------|
| General | 1,000 req / 10s |
| `GET /trades` | **200 req / 10s** |
| `GET /positions` | **150 req / 10s** |
| `GET /closed-positions` | **150 req / 10s** |

---

## Endpoints for Reconciliation

### 1. USDC Wallet Balance

**`GET /balance-allowance`** — CLOB API
```
URL: https://clob.polymarket.com/balance-allowance
Auth: L2 required
```

**Query params:**
| Param | Type | Description |
|-------|------|-------------|
| `asset_type` | string | `COLLATERAL` (USDC) or `CONDITIONAL` (outcome tokens) |
| `token_id` | string | Required when `asset_type=CONDITIONAL` |
| `signature_type` | int | -1 (default) |

**Returns:**
```json
{
  "balance": "1000.000000",    // string, USDC amount
  "allowance": "500.000000"   // string, approved spending allowance
}
```

**Data type:** Aggregate (single wallet balance point-in-time)
**Rate limit:** 200 req / 10s
**Notes:** To get USDC balance, use `asset_type=COLLATERAL`. To get conditional token holdings, use `asset_type=CONDITIONAL` with `token_id`.

---

### 2. Open Orders (CLOB Ledger)

**`GET /data/orders`** — CLOB API
```
URL: https://clob.polymarket.com/data/orders
Auth: L2 required
```

**Query params (via `OpenOrderParams`):**
| Param | Type | Description |
|-------|------|-------------|
| `id` | string | Filter by specific order ID |
| `market` | string | Filter by condition ID (market) |
| `asset_id` | string | Filter by token ID |
| `next_cursor` | string | Pagination cursor (start with `MA==`) |

**Returns array of order objects.** Fields include:
```json
{
  "id": "order-uuid",
  "status": "LIVE | MATCHED | CANCELLED | EXPIRED",
  "market": "0x<condition_id>",
  "asset_id": "token_id",
  "side": "BUY | SELL",
  "original_size": "100.0",
  "size_matched": "50.0",
  "size_filled": "50.0",
  "price": "0.65",
  "type": "GTC | FOK | GTD | FAK",
  "expiration": 0,
  "created_at": 1712345678,
  "maker_address": "0x...",
  "outcome": "Yes | No"
}
```

**Data type:** Per-order
**Rate limit:** 500 req / 10s
**Pagination:** Cursor-based. Pass `next_cursor` from response back in next request. Start with `MA==`.
**Order statuses:**
- `LIVE` — open, resting in the book
- `MATCHED` — fully filled
- `CANCELLED` — manually cancelled
- `EXPIRED` — past expiration timestamp

> ⚠️ **Important:** This endpoint returns **open/active** orders. Historical/cancelled orders may not appear here. Use `/data/trades` for fill history.

---

### 3. Trades / Fill History (CLOB Ledger)

**`GET /data/trades`** — CLOB API
```
URL: https://clob.polymarket.com/data/trades
Auth: L2 required
```

**Query params (via `TradeParams`):**
| Param | Type | Description |
|-------|------|-------------|
| `id` | string | Specific trade ID |
| `market` | string | Filter by condition ID |
| `asset_id` | string | Filter by token ID |
| `maker_address` | string | Filter by maker wallet address |
| `before` | int | Unix timestamp upper bound |
| `after` | int | Unix timestamp lower bound |
| `next_cursor` | string | Pagination cursor (start with `MA==`) |

**Returns array of trade/fill objects.** Fields include:
```json
{
  "id": "trade-uuid",
  "taker_order_id": "order-uuid",
  "maker_order_id": "order-uuid",
  "market": "0x<condition_id>",
  "asset_id": "token_id",
  "side": "BUY | SELL",
  "size": "50.0",           // shares filled
  "fee_rate_bps": 0,
  "price": "0.65",
  "status": "MATCHED | CONFIRMED | RETRYING | FAILED",
  "match_time": 1712345678,
  "last_update": 1712345679,
  "maker_address": "0x...",
  "transaction_hash": "0x..."
}
```

**Data type:** Per-fill/per-trade (individual execution records)
**Rate limit:** 500 req / 10s
**Pagination:** Cursor-based with `next_cursor`. Start with `MA==`.

> ✅ **This is the primary source for reconciling individual fills.** Each record = one execution match. Use `maker_address` param to filter to our wallet.

---

### 4. Single Order Lookup

**`GET /data/order/{order_id}`** — CLOB API
```
URL: https://clob.polymarket.com/data/order/<order_id>
Auth: L2 required
```

**Returns:** Single order object (same schema as `/data/orders` response items)

**Data type:** Per-order
**Use case:** Point lookup after DB miss, or verifying a specific order's current state.

---

### 5. Current Positions (Open Holdings)

**`GET /positions`** — Data API
```
URL: https://data-api.polymarket.com/positions
Auth: None (public)
```

**Query params:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `user` | string (Address) | **required** | Wallet address (0x-prefixed) |
| `market` | string[] | — | Comma-separated condition IDs (mutually exclusive with `eventId`) |
| `eventId` | int[] | — | Comma-separated event IDs |
| `sizeThreshold` | number | 1 | Min position size to include |
| `redeemable` | boolean | false | Filter to redeemable-only positions |
| `mergeable` | boolean | false | Filter to mergeable positions |
| `limit` | int | 100 | Max results (max 500) |
| `offset` | int | 0 | Pagination offset (max 10000) |
| `sortBy` | enum | TOKENS | `CURRENT`, `INITIAL`, `TOKENS`, `CASHPNL`, `PERCENTPNL`, `TITLE`, `RESOLVING`, `PRICE`, `AVGPRICE` |
| `sortDirection` | enum | DESC | `ASC` or `DESC` |
| `title` | string | — | Filter by market title substring |

**Returns array of `Position` objects:**
```json
{
  "proxyWallet": "0x...",
  "asset": "token_id",
  "conditionId": "0x<condition_id>",
  "size": 150.0,              // current token holdings
  "avgPrice": 0.62,           // average entry price
  "initialValue": 93.0,       // USDC spent
  "currentValue": 97.5,       // current mark value
  "cashPnl": 4.5,
  "percentPnl": 4.84,
  "title": "Will X happen?",
  "slug": "will-x-happen",
  "icon": "...",
  "endDate": "2026-01-01T00:00:00Z",
  "outcome": "Yes",
  "outcomeIndex": 0,
  "price": 0.65               // current market price
}
```

**Data type:** Aggregate per-market position (not per-order)
**Rate limit:** 150 req / 10s
**Notes:** No auth needed — just pass the wallet address as `user`. Use `conditionId` to link back to our DB market records.

---

### 6. Closed Positions (Exited/Resolved)

**`GET /closed-positions`** — Data API
```
URL: https://data-api.polymarket.com/closed-positions
Auth: None (public)
```

**Query params:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `user` | string (Address) | **required** | Wallet address |
| `market` | string[] | — | Condition IDs (CSV) |
| `eventId` | int[] | — | Event IDs (CSV) |
| `title` | string | — | Market title filter |
| `limit` | int | 10 | Max results (max 50) |
| `offset` | int | 0 | Pagination offset (max 100000) |
| `sortBy` | enum | REALIZEDPNL | `REALIZEDPNL`, `TITLE`, `PRICE`, `AVGPRICE`, `TIMESTAMP` |
| `sortDirection` | enum | DESC | `ASC` or `DESC` |

**Returns array of `ClosedPosition` objects:**
```json
{
  "proxyWallet": "0x...",
  "conditionId": "0x<condition_id>",
  "asset": "token_id",
  "title": "Market title",
  "slug": "...",
  "outcome": "Yes",
  "outcomeIndex": 0,
  "size": 0,                   // (sold/resolved, so 0)
  "avgPrice": 0.62,
  "realizedPnl": 38.0,
  "percentPnl": 40.86,
  "initialValue": 93.0,
  "endValue": 131.0,
  "closedAt": 1712345678
}
```

**Data type:** Aggregate per-market closed position
**Rate limit:** 150 req / 10s

---

### 7. Trades (Data API — Public Trade History)

**`GET /trades`** — Data API
```
URL: https://data-api.polymarket.com/trades
Auth: None (public)
```

**Query params:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `user` | string (Address) | — | Filter to specific wallet |
| `market` | string[] | — | Condition IDs (CSV) |
| `eventId` | int[] | — | Event IDs (CSV) |
| `side` | enum | — | `BUY` or `SELL` |
| `takerOnly` | boolean | true | Only include taker-side fills |
| `filterType` | enum | — | `CASH` or `TOKENS` (pair with `filterAmount`) |
| `filterAmount` | number | — | Min threshold for filter |
| `limit` | int | 100 | Max results (max 10000) |
| `offset` | int | 0 | Pagination offset (max 10000) |

**Returns array of `Trade` objects:**
```json
{
  "proxyWallet": "0x...",
  "side": "BUY | SELL",
  "asset": "token_id",
  "conditionId": "0x<condition_id>",
  "size": 100.0,
  "price": 0.65,
  "timestamp": 1712345678,
  "title": "Market title",
  "slug": "market-slug"
}
```

**Data type:** Per-trade
**Rate limit:** 200 req / 10s
**Notes:** This is a **public** endpoint — no auth needed. However it seems to return taker trades only by default (`takerOnly=true`). For maker fills, use the CLOB API `/data/trades` with `maker_address`.

---

### 8. Accounting Snapshot (Bulk Export)

**`GET /v1/accounting/snapshot`** — Data API
```
URL: https://data-api.polymarket.com/v1/accounting/snapshot
Auth: None (public)
```

**Query params:**
| Param | Type | Description |
|-------|------|-------------|
| `user` | string (Address) | **required** — wallet address |

**Returns:** ZIP file containing:
- `positions.csv` — current positions
- `equity.csv` — equity/value breakdown

**Data type:** Bulk snapshot (CSV)
**Use case:** Cold start / initial DB population. Not suitable for real-time sync.

---

## Reconciliation Strategy for Montreal

### Data Freshness Sources (by use case)

| What we need | Best Endpoint | API | Auth | Rate Limit |
|---|---|---|---|---|
| **Real-time USDC balance** | `GET /balance-allowance?asset_type=COLLATERAL` | CLOB | L2 | 200/10s |
| **Conditional token balance** | `GET /balance-allowance?asset_type=CONDITIONAL&token_id=<id>` | CLOB | L2 | 200/10s |
| **All orders (with fill status)** | `GET /data/orders` + paginate | CLOB | L2 | 500/10s |
| **Individual fill/trade records** | `GET /data/trades?maker_address=<wallet>` | CLOB | L2 | 500/10s |
| **Order status (LIVE/MATCHED/CANCELLED/EXPIRED)** | `GET /data/orders` or `GET /data/order/<id>` | CLOB | L2 | 500/10s |
| **Open positions (aggregate, per conditionId)** | `GET /positions?user=<wallet>` | Data | None | 150/10s |
| **Closed/exited positions** | `GET /closed-positions?user=<wallet>` | Data | None | 150/10s |
| **Trade history (public taker view)** | `GET /trades?user=<wallet>` | Data | None | 200/10s |
| **Initial DB bootstrap** | `GET /v1/accounting/snapshot` | Data | None | N/A |

---

### Key Design Notes for the Sync Service

1. **Order pagination is cursor-based.** CLOB endpoints use opaque `next_cursor` strings. Always start with `MA==`, then feed the cursor from each response back into the next request. Store the last cursor to enable incremental syncs.

2. **CLOB `/data/orders` = open/active orders only.** For full order history including cancelled/expired, you need `/data/trades` + reconcile against what you've seen before. There is no single "all orders including closed" CLOB endpoint.

3. **Two trade APIs with different perspectives:**
   - CLOB `/data/trades`: Authenticated, includes `maker_address` filter — gives maker+taker fills with `transaction_hash`
   - Data `/trades`: Public, `takerOnly=true` by default — simpler but less complete

4. **`conditionId` is the market key.** Use it to join across CLOB orders/trades and Data API positions. The `asset_id` / `token_id` is the specific outcome token (YES or NO side).

5. **Position data is aggregate.** Neither `/positions` nor `/closed-positions` gives per-order breakdown. They show net holdings. For order-level detail, you must use CLOB `/data/orders` + `/data/trades`.

6. **No rate limit pain at our scale.** Even `/positions` at 150 req/10s = 900/min = plenty for a reconciliation service running periodic syncs. Watch the CLOB L2 endpoints more carefully during high-activity periods.

7. **`size_matched` on orders:** The `/data/orders` response includes `size_matched` and `original_size` so you can calculate fill percentage per order directly. This is the primary reconciliation signal: `size_matched / original_size`.

---

### Recommended Sync Loop

```
Every N seconds (e.g. 30s):
  1. GET /balance-allowance?asset_type=COLLATERAL → update USDC balance
  2. GET /data/orders (full paginated sweep) → upsert all orders, detect status changes
  3. GET /data/trades (incremental, after=<last_seen_ts>) → insert new fills
  4. GET /positions?user=<wallet> (paginated) → upsert position records

Every market close / resolution:
  5. GET /closed-positions?user=<wallet> → finalize PnL records

On cold start:
  6. GET /v1/accounting/snapshot → bulk-load positions.csv + equity.csv
```

---

## Python Client Reference

```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    TradeParams,
    OpenOrderParams,
    BalanceAllowanceParams,
    AssetType,
)

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
client.set_api_creds(client.create_or_derive_api_creds())

# USDC balance
balance = client.get_balance_allowance(
    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
)

# All open orders (paginated)
orders = client.get_orders(OpenOrderParams())

# Orders for specific market
orders = client.get_orders(OpenOrderParams(market="0x<condition_id>"))

# Trades (maker fills for our wallet)
trades = client.get_trades(
    TradeParams(maker_address="0x<our_wallet>", after=last_seen_ts)
)

# Conditional token balance
token_balance = client.get_balance_allowance(
    BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id="<token_id>")
)
```

For the Data API (no auth):
```python
import httpx

WALLET = "0x<our_wallet>"

# Open positions
positions = httpx.get(
    "https://data-api.polymarket.com/positions",
    params={"user": WALLET, "limit": 500}
).json()

# Trade history
trades = httpx.get(
    "https://data-api.polymarket.com/trades",
    params={"user": WALLET, "limit": 10000, "takerOnly": False}
).json()
```

---

## CLOB `/data/trades` Full Schema (Confirmed)

From CLOB API OpenAPI spec (`GET /data/trades`):

```json
{
  "id": "trade-123",                         // unique trade/fill ID
  "taker_order_id": "0xabc...",              // order that triggered the match
  "market": "0x<condition_id>",
  "asset_id": "<token_id>",
  "side": "BUY",
  "size": "100000000",                       // in micro-USDC (divide by 1e6 for USDC)
  "fee_rate_bps": "30",
  "price": "0.5",
  "status": "TRADE_STATUS_CONFIRMED",        // or RETRYING, FAILED
  "match_time": "1700000000",
  "last_update": "1700000000",
  "outcome": "YES",
  "bucket_index": 0,
  "owner": "f4f247b7-4ac7-ff29-a152-04fda0a8755a",
  "maker_address": "0x1234...",
  "transaction_hash": "0xabcd...",
  "trader_side": "TAKER",
  "maker_orders": []                         // array of maker order IDs matched against
}
```

> ⚠️ **Size is in micro-units.** Divide `size` by 1,000,000 to get USDC/shares.
> The `taker_order_id` links this fill back to the originating order in our DB.

---

## Relayer / Builder Order Flow

The **Relayer API** (`https://relayer-v2.polymarket.com`) handles gasless transactions. Relevant for us if we use the builder flow:

### `GET /transactions` — Get Recent Relayer Transactions
```
URL: https://relayer-v2.polymarket.com/transactions
Auth: Builder API Keys or Relayer API Keys
```

Headers (Builder auth):
```
POLY_BUILDER_API_KEY
POLY_BUILDER_TIMESTAMP
POLY_BUILDER_PASSPHRASE
POLY_BUILDER_SIGNATURE
```

Returns transaction objects with fields:
```json
{
  "transactionID": "uuid",
  "transactionHash": "0x...",
  "from": "0x...",
  "to": "0x...",
  "proxyAddress": "0x...",
  "state": "STATE_CONFIRMED | STATE_PENDING | STATE_FAILED",
  "type": "SAFE | PROXY",
  "owner": "0x...",
  "createdAt": "2024-07-14T21:13:08Z",
  "updatedAt": "2024-07-14T21:13:46Z"
}
```

**Relayer rate limit:** 25 req / 1 min (much tighter — do not poll frequently)

### `GET /transactions?id=<txId>` — Single Transaction Lookup
```
URL: https://relayer-v2.polymarket.com/transactions?id=<transactionID>
Auth: Builder API Keys or Relayer API Keys
```

---

## Streaming / WebSocket API

### `GET /live-activity/events/<token_id>` — CLOB Live Events
```
URL: https://clob.polymarket.com/live-activity/events/<token_id>
Auth: None required (public)
```

This endpoint streams live order book events (fills, orders placed/cancelled) for a specific token. Used by the Polymarket UI. Could be used for real-time fill detection instead of polling.

> ⚠️ **Recommendation:** For our reconciliation service, **don't rely on websocket as primary**. Use it as a supplement to catch fast fills between poll cycles. The 30s polling loop via REST is safer and stateless.

There is **no official WebSocket API documented** for user-specific order status updates. The CLOB does emit events via server-sent events (SSE) for market-level activity, but user-level authenticated streaming is not documented.

---

## Open Questions / Gaps

1. **CLOB `/data/orders` — does it include historical cancelled/expired orders?** Docs are ambiguous. Testing needed. If not, we may need to track order lifecycle ourselves and only query CLOB for current state. Working assumption: **only LIVE orders are returned** — use `/data/trades` for MATCHED history.

2. **`/data/trades` vs `/trades`** — Both exist. CLOB `/data/trades` appears more complete (includes `transaction_hash`, `maker_order_id`, `taker_order_id`). Data API `/trades` is public and simpler. We should use CLOB for reconciliation, Data API as a cross-check.

3. **Accounting snapshot freshness** — Unknown how frequently the snapshot ZIP is regenerated. Not suitable for real-time use, only cold-start bootstrap.

4. **Proxy wallet vs maker address** — Some wallets use a proxy/funder pattern. The `proxyWallet` field in Data API vs `maker_address` in CLOB may need mapping. Confirm which address we're tracking.

5. **`size` units in CLOB trades** — Confirmed as micro-units (1e6 = 1 share/USDC). The Data API `/trades` uses whole units. Be careful when joining across APIs.

6. **`maker_orders` in CLOB trade response** — This array links the taker fill back to all maker orders it matched against. Potentially useful for multi-fill order reconciliation.

7. **Relayer vs CLOB order placement** — If we use the builder/relayer flow for gasless order submission, order IDs assigned by the Relayer may differ from CLOB order IDs until confirmed. Need to track Relayer `transactionID` → CLOB `order_id` mapping.

---

## Last Updated

**2026-04-07** — Initial comprehensive audit written.
**2026-04-07** (revision) — Added confirmed CLOB trade schema, Relayer API details, streaming API notes, clarified open questions.
