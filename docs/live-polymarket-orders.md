# Live Polymarket CLOB Order Placement

> Technical documentation for the `feat/live-polymarket-orders` branch.
> PR #1 — Implements real order submission on Polymarket's Central Limit Order Book.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Order Lifecycle](#3-order-lifecycle)
4. [PolymarketClient](#4-polymarketclient)
5. [Gamma API Integration](#5-gamma-api-integration)
6. [Five-Minute VPIN Strategy Changes](#6-five-minute-vpin-strategy-changes)
7. [Safety Guards](#7-safety-guards)
8. [Configuration](#8-configuration)
9. [Going Live — Step by Step](#9-going-live--step-by-step)
10. [Troubleshooting](#10-troubleshooting)
11. [API Reference](#11-api-reference)

---

## 1. Overview

### What Changed

Before this PR, the engine had two modes:
- **Paper mode** — Fully working, simulates orders locally with slippage
- **Live mode** — Stubs that raised `NotImplementedError`

After this PR:
- **Paper mode** — Unchanged, still default
- **Live mode** — Real orders placed on Polymarket's CLOB via `py-clob-client`

### Key Components Modified

| File | Change |
|------|--------|
| `engine/execution/polymarket_client.py` | Implements `_live_place_order()`, `connect()` with ClobClient, live `get_balance()`, `get_order_status()`, `get_market_prices()`, `place_market_order()` |
| `engine/data/feeds/polymarket_5min.py` | Fixes Gamma API `clobTokenIds` extraction (YES = index 0, NO = index 1) |
| `engine/strategies/five_min_vpin.py` | Passes `token_id` to `place_order()`, uses real Gamma prices in live mode, propagates CLOB order ID |
| `engine/requirements.txt` | Adds `py-clob-client>=0.18.0` |

---

## 2. Architecture

### Paper Mode (existing)

```
┌─────────────────┐     ┌──────────────────┐     ┌───────────────┐
│  5-Min Strategy  │────▶│  PolymarketClient │────▶│  Paper Orders │
│  (evaluate T-10s)│     │  (paper_mode=True)│     │  (in-memory)  │
└─────────────────┘     └──────────────────┘     └───────────────┘
        │                        │
        │                        ├── Simulated fill price (±0.5% slippage)
        │                        ├── Simulated balance tracking
        │                        └── order_id = "paper-{uuid}"
        │
        ▼
  OrderManager ──▶ DB (trades table)
```

### Live Mode (new)

```
┌─────────────────┐     ┌──────────────────┐     ┌───────────────────┐
│  5-Min Strategy  │────▶│  PolymarketClient │────▶│  Polymarket CLOB  │
│  (evaluate T-10s)│     │  (paper_mode=False│     │  (clob.polymarket  │
│                  │     │   + ClobClient)   │     │   .com)            │
└─────────────────┘     └──────────────────┘     └───────────────────┘
        │                        │                        │
        │                        ├── Signed order (EIP-712)│
        │                        ├── GTC limit order       │
        │                        ├── Real CLOB order ID    │
        │                        └── token_id required     │
        │                                                  │
        ▼                                                  ▼
  OrderManager ──▶ DB                             Polygon Settlement
```

### Data Flow — Live Order Placement

```
1. Polymarket5MinFeed._fetch_live_data()
   │   Gamma API → extract clobTokenIds[0]=YES, [1]=NO
   │   Extract bestAsk → window.up_price, window.down_price
   ▼
2. Orchestrator._on_five_min_window()
   │   Sets strategy._pending_window = window
   ▼
3. FiveMinVPINStrategy.on_market_state()
   │   Evaluates delta + VPIN → signal
   ▼
4. FiveMinVPINStrategy._execute_trade()
   │   Uses window.up_price / window.down_price (real prices)
   │   Passes token_id to place_order()
   ▼
5. PolymarketClient.place_order()
   │   Validates: token_id present, stake ≤ $50 cap
   │   First-trade warning if first live order
   ▼
6. PolymarketClient._live_place_order()
   │   Runs in asyncio.to_thread() to avoid blocking
   │   ClobClient.create_order(OrderArgs) → signed order
   │   ClobClient.post_order(signed, GTC) → response
   │   Returns real CLOB order ID
   ▼
7. Order registered with OrderManager
   │   order_id = CLOB order ID (not paper UUID)
   │   metadata.clob_order_id = CLOB order ID
   │   metadata.market_slug = slug
   │   metadata.token_id = token_id
   ▼
8. Persisted to DB + Telegram alert
```

---

## 3. Order Lifecycle

### Paper Mode

```
OPEN ──(5s resolution poll)──▶ RESOLVED_WIN / RESOLVED_LOSS
  │                                │
  │  Simulated price check:        │  Checks BTC close vs open:
  │  ±0.5% slippage applied        │  close >= open → UP wins
  │  order_id: "paper-{uuid}"      │  close <  open → DOWN wins
  │                                │
  └── Bankroll adjusted ◄──────────┘
```

### Live Mode

```
OPEN ──(GTC limit on CLOB)──▶ MATCHED ──▶ SETTLEMENT
  │                                │              │
  │  Signed with EIP-712           │  On-chain     │  Chainlink oracle
  │  order_id: CLOB order ID       │  fill event   │  resolves at window end
  │  token_id: outcome token       │               │
  │                                │               ▼
  └── Track via get_order_status() ◄── WIN/LOSS based on oracle
```

> **Note:** Live order resolution currently still uses the paper resolution
> logic (BTC price comparison). Full on-chain settlement tracking is a future
> enhancement — the CLOB order ID is preserved in metadata for this purpose.

---

## 4. PolymarketClient

### Class: `PolymarketClient`

Location: `engine/execution/polymarket_client.py`

#### Constructor

```python
PolymarketClient(
    private_key: str,      # Ethereum private key (0x...)
    api_key: str,          # CLOB API key
    api_secret: str,       # CLOB API secret
    api_passphrase: str,   # CLOB API passphrase
    funder_address: str,   # Proxy wallet address
    paper_mode: bool = True,
)
```

**Safety check at construction:**
- If `paper_mode=False`, requires `LIVE_TRADING_ENABLED=true` env var
- Raises `EnvironmentError` if not set — prevents accidental live mode

#### `connect()`

| Mode | Behaviour |
|------|-----------|
| Paper | Logs startup, no network calls |
| Live (with API creds) | Creates `ClobClient` with provided `ApiCreds` |
| Live (without API creds) | Creates `ClobClient`, auto-derives creds via `create_or_derive_api_creds()` |

**Live client configuration:**
```python
ClobClient(
    host="https://clob.polymarket.com",
    key=private_key,
    chain_id=137,          # Polygon mainnet
    signature_type=2,      # POLY_GNOSIS_SAFE
    funder=funder_address,
    creds=ApiCreds(api_key, api_secret, api_passphrase),
)
```

#### `place_order()` — High-Level

```python
async def place_order(
    market_slug: str,           # e.g. "btc-updown-5m-1711900800"
    direction: str,             # "YES" or "NO"
    price: Decimal,             # Limit price [0, 1]
    stake_usd: float,           # USD to risk
    token_id: Optional[str],    # CLOB token ID (required for live)
) -> str:                       # Returns order ID
```

| Mode | Behaviour |
|------|-----------|
| Paper | Simulates fill with ±0.5% slippage, returns `"paper-{uuid}"` |
| Live | Validates inputs, signs order, submits to CLOB, returns real order ID |

#### `_live_place_order()` — Internal

1. **Validates** `_clob_client` connected, `token_id` present, `stake ≤ $50`
2. **First-trade warning** — emits `RuntimeWarning` + structured log on first live order
3. **Builds order:**
   ```python
   OrderArgs(
       token_id=token_id,
       price=float(price),
       size=round(stake_usd / float(price), 2),  # shares
       side=BUY,
   )
   ```
4. **Signs and submits** via `asyncio.to_thread()`:
   - `ClobClient.create_order(args)` → signed order (EIP-712 signature)
   - `ClobClient.post_order(signed, OrderType.GTC)` → CLOB response
5. **Extracts order ID** from response (handles both dict and object types)

#### Event Loop Safety

All py-clob-client calls are synchronous (HTTP + crypto signing). To prevent
blocking the asyncio event loop (which would freeze feeds, heartbeat, and
resolution polling), every live CLOB call is wrapped in `asyncio.to_thread()`:

```python
# ❌ Before (blocks event loop for 200-500ms)
signed = self._clob_client.create_order(args)
response = self._clob_client.post_order(signed, OrderType.GTC)

# ✅ After (runs in thread pool, event loop stays responsive)
def _sign_and_submit():
    signed = client.create_order(args)
    return client.post_order(signed, OrderType.GTC)

response = await asyncio.to_thread(_sign_and_submit)
```

Methods wrapped in `to_thread`:
- `_live_place_order()` — create_order + post_order
- `place_market_order()` — create_and_post_order
- `get_market_prices()` — get_order_book
- `get_balance()` — get_balance
- `get_order_status()` — get_order

#### `get_market_prices()` — Live

Queries the CLOB `/markets?slug=` endpoint for a specific market, then fetches
the order book for the YES token. Returns `{"yes": Decimal, "no": Decimal}`.

Uses direct `httpx.get()` to `/markets?slug=` instead of `get_markets()` which
fetches the entire market catalog (thousands of markets, very slow).

#### `get_balance()` and `get_order_status()` — Live

Both delegate to py-clob-client methods via `asyncio.to_thread()`.
Response handling is defensive — works with both dict and object return types
depending on py-clob-client version.

---

## 5. Gamma API Integration

### Market Discovery Flow

Location: `engine/data/feeds/polymarket_5min.py` → `_fetch_live_data()`

```
GET https://gamma-api.polymarket.com/events
  ?slug=btc-updown-5m-{window_ts}
```

### Response Structure

```json
[
  {
    "slug": "btc-updown-5m-1711900800",
    "markets": [
      {
        "clobTokenIds": ["<yes_token_id>", "<no_token_id>"],
        "bestAsk": "0.52",
        "bestBid": "0.48"
      }
    ]
  }
]
```

### Token ID Extraction

```python
clob_token_ids = market.get("clobTokenIds") or []

# Index 0 → YES / Up token
# Index 1 → NO  / Down token
window.up_token_id   = str(clob_token_ids[0])    # YES
window.down_token_id = str(clob_token_ids[1])     # NO
```

### Price Extraction

```python
best_ask = market.get("bestAsk") or market.get("best_ask")
if best_ask is not None:
    window.up_price   = float(best_ask)
    window.down_price = round(1.0 - window.up_price, 4)
```

These real Gamma prices are used in live mode instead of the synthetic
`_delta_to_token_price()` approximation.

### Edge Cases Handled

| Scenario | Behaviour |
|----------|-----------|
| No events returned | Logs warning, skips window |
| No markets in event | Logs warning, skips window |
| Only 1 token ID | Assigns to YES, logs warning |
| No token IDs | Logs warning, skips window |
| bestAsk missing | Prices stay `None`; strategy uses synthetic fallback |

---

## 6. Five-Minute VPIN Strategy Changes

Location: `engine/strategies/five_min_vpin.py` → `_execute_trade()`

### Price Selection (Paper vs Live)

```python
# Live mode: use real Gamma API prices when available
if direction == "YES" and window.up_price is not None:
    token_price = window.up_price      # Real market price
elif direction == "NO" and window.down_price is not None:
    token_price = window.down_price    # Real market price
else:
    # Paper mode fallback: synthetic pricing from delta
    token_price = self._delta_to_token_price(signal.delta_pct)
```

### Token ID Propagation

The strategy now passes `token_id` through to `place_order()`:

```python
order_id = await self._poly.place_order(
    market_slug=market_slug,
    direction=direction,
    price=price,
    stake_usd=stake,
    token_id=token_id,    # ← NEW: required for live mode
)
```

### Order ID Handling

```python
# Live mode: use real CLOB order ID for on-chain tracking
# Paper mode: use local UUID (unchanged)
order_id = clob_order_id if not self._poly.paper_mode else f"5min-{uuid.uuid4().hex[:12]}"
```

The CLOB order ID is also stored in `metadata.clob_order_id` for redundancy.

### Order Metadata

Orders now include additional fields in metadata:

```python
metadata = {
    "window_ts": window.window_ts,
    "window_open_price": window.open_price,
    "delta_pct": signal.delta_pct,
    "vpin": signal.current_vpin,
    "confidence": signal.confidence,
    "token_id": token_id,           # NEW
    "clob_order_id": clob_order_id, # NEW
    "market_slug": market_slug,     # NEW
}
```

---

## 7. Safety Guards

### Guard 1: Environment Variable Gate

```python
# At PolymarketClient construction
if not paper_mode:
    live_enabled = os.environ.get("LIVE_TRADING_ENABLED", "").strip().lower()
    if live_enabled != "true":
        raise EnvironmentError(
            "Live trading requires LIVE_TRADING_ENABLED=true"
        )
```

**Effect:** Engine cannot start in live mode without explicit `LIVE_TRADING_ENABLED=true`.
Even if `PAPER_MODE=false`, the client refuses to construct.

### Guard 2: Per-Trade Size Cap

```python
LIVE_MAX_TRADE_USD = 50.0

if stake_usd > LIVE_MAX_TRADE_USD:
    raise ValueError(f"stake ${stake_usd} exceeds cap ${LIVE_MAX_TRADE_USD}")
```

**Effect:** No single live trade can exceed $50. Raises `ValueError` which
prevents the order and logs an error.

### Guard 3: First-Trade Warning

```python
if not self._live_first_trade_warned:
    self._live_first_trade_warned = True
    warnings.warn(
        "First live Polymarket trade being placed — "
        "verify manually on polymarket.com/portfolio",
        RuntimeWarning,
    )
    self._log.warning("place_order.first_live_trade", ...)
```

**Effect:** First live trade in a process emits a Python `RuntimeWarning`
(visible in stderr) and a structured `WARNING` log. Prompts manual verification.

### Guard 4: Token ID Validation

```python
if not token_id:
    raise ValueError("token_id is required for live order placement")
```

**Effect:** Prevents placing live orders without knowing which outcome token
to buy. Would indicate a Gamma API failure.

### Guard 5: Client Connection Check

Every live method starts with:
```python
if not self._clob_client:
    raise RuntimeError("CLOB client not connected — call connect() first")
```

### Guard Summary

| Guard | Type | Scope | Failure Mode |
|-------|------|-------|-------------|
| `LIVE_TRADING_ENABLED` | Env var | Client construction | `EnvironmentError` → engine won't start |
| `$50 trade cap` | Hard limit | Per order | `ValueError` → order rejected |
| First-trade warning | Notification | First order per process | `RuntimeWarning` + structured log |
| Token ID check | Validation | Per order | `ValueError` → order rejected |
| Client connection | State check | Every method | `RuntimeError` → method fails |

---

## 8. Configuration

### New Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LIVE_TRADING_ENABLED` | (unset) | Must be `"true"` to allow live mode |
| `PAPER_MODE` | `true` | `false` enables live trading |

### Existing Variables (used by live mode)

| Variable | Purpose |
|----------|---------|
| `POLY_PRIVATE_KEY` | Ethereum private key for signing CLOB orders |
| `POLY_API_KEY` | CLOB API key (or auto-derived) |
| `POLY_API_SECRET` | CLOB API secret (or auto-derived) |
| `POLY_API_PASSPHRASE` | CLOB API passphrase (or auto-derived) |
| `POLY_FUNDER_ADDRESS` | Polymarket proxy wallet address |

### Constants

| Constant | Value | Location |
|----------|-------|----------|
| `LIVE_MAX_TRADE_USD` | `50.0` | `polymarket_client.py` |
| `POLY_WINDOW_SECONDS` | `300` | `constants.py` |
| `BET_FRACTION` | `0.025` (2.5%) | `constants.py` |

### Railway Env Vars

Set on the `engine` service:

```bash
railway variables --set "PAPER_MODE=false" --service engine
railway variables --set "LIVE_TRADING_ENABLED=true" --service engine
```

---

## 9. Going Live — Step by Step

### Prerequisites

- [ ] Paper mode win rate verified at ~82% over multiple hours
- [ ] All API keys configured (POLY_*, BINANCE_*, CG_*)
- [ ] Polymarket wallet funded with ≥$200 USDC on Polygon
- [ ] Manual trade placed on polymarket.com (initialises proxy wallet)
- [ ] `POLY_FUNDER_ADDRESS` set to your proxy wallet address

### Step 1: Verify Paper Performance

```bash
# Check recent paper trades
railway logs --service engine --lines 500 | grep "paper_resolution"

# Count wins vs losses
railway logs --service engine --lines 1000 | grep "resolved" | grep -c "WIN"
railway logs --service engine --lines 1000 | grep "resolved" | grep -c "LOSS"
```

Target: ≥75% win rate over 50+ trades.

### Step 2: Set Live Environment

```bash
cd /path/to/novakash
railway variables --set "PAPER_MODE=false" --service engine
railway variables --set "LIVE_TRADING_ENABLED=true" --service engine
```

### Step 3: Deploy

```bash
git push origin develop  # triggers Railway auto-deploy
```

### Step 4: Verify First Trade

1. Watch Railway logs: `railway logs --service engine -f`
2. Look for:
   ```
   WARNING  place_order.first_live_trade  market_slug=btc-updown-5m-...
   INFO     place_order.live_submitted    order_id=<CLOB_ID>
   ```
3. Check https://polymarket.com/portfolio — verify the order appears
4. Verify the fill price matches expected token price

### Step 5: Monitor

```bash
# Live trade submissions
railway logs --service engine | grep "live_submitted"

# Any errors
railway logs --service engine | grep "ERROR"

# Balance
railway logs --service engine | grep "get_balance"
```

### Step 6: Emergency Stop

```bash
# Immediate: set back to paper mode
railway variables --set "PAPER_MODE=true" --service engine
# This triggers a redeploy; engine restarts in paper mode
```

Or via the dashboard: System → Kill Switch.

---

## 10. Troubleshooting

### `EnvironmentError: Live trading requires LIVE_TRADING_ENABLED=true`

**Cause:** `PAPER_MODE=false` but `LIVE_TRADING_ENABLED` not set.
**Fix:** Set `LIVE_TRADING_ENABLED=true` on Railway.

### `ValueError: token_id is required for live order placement`

**Cause:** Gamma API didn't return `clobTokenIds` for the market.
**Possible reasons:**
- Market doesn't exist yet (too early in the 5-min window)
- Gamma API returned an empty event
- Network error during Gamma API fetch

**Fix:** Check engine logs for `market.no_token_ids` or `gamma_api_error`.

### `ValueError: Live trade stake $X exceeds safety cap $50.00`

**Cause:** `BET_FRACTION * bankroll > $50`.
**Fix:** Lower `BET_FRACTION` or `STARTING_BANKROLL` so per-trade stake stays under $50.

### `RuntimeError: CLOB client not connected`

**Cause:** `connect()` wasn't called before placing orders.
**Fix:** Ensure `Orchestrator.start()` calls `_poly_client.connect()` before
starting strategies. Check logs for `polymarket_client.connected`.

### CLOB order rejected / signing error

**Cause:** Invalid API creds, insufficient USDC balance, or nonce mismatch.
**Check:**
- `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE` are correct
- Wallet has USDC on Polygon
- Proxy wallet is initialised (manual trade on polymarket.com)

### Orders placed but not filling

**Cause:** GTC limit order price too aggressive (away from market).
**Check:** Compare order price with current market bestAsk/bestBid.
The strategy uses Gamma API `bestAsk` — if this is stale, the limit price
may be off-market.

---

## 11. API Reference

### py-clob-client Types Used

```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,       # API key/secret/passphrase
    OrderArgs,      # token_id, price, size, side
    OrderType,      # GTC, FOK, IOC
    MarketOrderArgs,# token_id, amount
)
from py_clob_client.order_builder.constants import BUY, SELL
```

### CLOB Endpoints Hit

| Endpoint | Method | Used By |
|----------|--------|---------|
| `POST /order` | post_order | `_live_place_order()` |
| `POST /order` | create_and_post_order | `place_market_order()` |
| `GET /markets` | get_markets (via httpx) | `get_market_prices()` |
| `GET /order-book/{token_id}` | get_order_book | `get_market_prices()` |
| `GET /balance` | get_balance | `get_balance()` |
| `GET /order/{id}` | get_order | `get_order_status()` |
| `POST /derive-api-key` | create_or_derive_api_creds | `connect()` (fallback) |

### Gamma API Endpoint

| Endpoint | Method | Used By |
|----------|--------|---------|
| `GET /events?slug=` | httpx GET | `polymarket_5min._fetch_live_data()` |

---

## Appendix: File Changes Summary

### `engine/execution/polymarket_client.py`

| Method | Before | After |
|--------|--------|-------|
| `__init__()` | No live safety check | `EnvironmentError` if live without opt-in |
| `connect()` | `NotImplementedError` | ClobClient init + optional cred derivation |
| `place_order()` | Live: `NotImplementedError` | Routes to `_live_place_order()` |
| `_live_place_order()` | N/A | Full implementation with safety guards |
| `place_market_order()` | `NotImplementedError` | MarketOrderArgs via to_thread |
| `get_market_prices()` | `NotImplementedError` | CLOB /markets?slug= + order book |
| `get_balance()` | `NotImplementedError` | ClobClient.get_balance via to_thread |
| `get_order_status()` | `NotImplementedError` | ClobClient.get_order via to_thread |

### `engine/data/feeds/polymarket_5min.py`

| Method | Before | After |
|--------|--------|-------|
| `_fetch_live_data()` | `event.get("up_token_id")` (wrong key) | Correct `clobTokenIds[0]`/`[1]` extraction |
| `_fetch_live_data()` | No price extraction | Extracts `bestAsk` → `up_price`/`down_price` |

### `engine/strategies/five_min_vpin.py`

| Method | Before | After |
|--------|--------|-------|
| `_execute_trade()` | Always synthetic price | Real Gamma prices in live mode |
| `_execute_trade()` | No token_id in place_order | Passes token_id |
| `_execute_trade()` | `order_id = "5min-{uuid}"` always | CLOB order ID in live mode |
| `_execute_trade()` | metadata: 5 fields | metadata: 8 fields (+ token_id, clob_order_id, market_slug) |

### `engine/requirements.txt`

Added: `py-clob-client>=0.18.0`
