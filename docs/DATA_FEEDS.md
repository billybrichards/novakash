# DATA_FEEDS.md — Novakash Data Sources

All real-time and near-real-time data ingested by the Novakash trading engine.

---

## Overview

| Source | Assets | Frequency | Table | Purpose |
|--------|--------|-----------|-------|---------|
| Binance WebSocket | BTC | 1-3 Hz | `ticks_binance` | Primary price feed, VPIN calculation |
| Chainlink (Polygon) | BTC, ETH, SOL, XRP | ~5s | `ticks_chainlink` | Oracle price — source of truth for on-chain settlement |
| Tiingo Top-of-Book | BTC, ETH, SOL, XRP | ~2s | `ticks_tiingo` | Multi-exchange best bid/ask |
| CoinGlass Enhanced | BTC, ETH, SOL, XRP | ~15s | `ticks_coinglass` | Open interest, funding rate, liquidations |
| Gamma API | BTC, ETH, SOL, XRP | per-window | `ticks_gamma` | Polymarket token prices at window open/close |
| CLOB Order Book | BTC | ~10s | `ticks_clob` | Ground truth Polymarket bid/ask (replaces Gamma for pricing) |
| TimesFM | BTC | ~1s | `ticks_timesfm` | ML price forecast — agreement signal |

---

## 1. Binance WebSocket

**Module:** `engine/data/feeds/binance_ws.py`  
**Symbol:** `btcusdt` (BTC only)  
**Update rate:** 1-3 Hz (every aggTrade event)

### How it's used
- Primary BTC price input to VPIN calculator
- Drives the `MarketAggregator` state machine
- All VPIN bucket fills (and cascade signals) originate here
- Heartbeat price reference for order resolution

### Table: `ticks_binance`
```sql
CREATE TABLE ticks_binance (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    asset           VARCHAR(10) NOT NULL,
    price           FLOAT8 NOT NULL,
    quantity        FLOAT8 NOT NULL,
    is_buyer_maker  BOOLEAN NOT NULL,
    vpin            FLOAT8,                -- VPIN value at time of tick
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

### Query examples
```sql
-- Last 100 BTC ticks
SELECT ts, price, quantity, vpin FROM ticks_binance ORDER BY ts DESC LIMIT 100;

-- VPIN time-series for past hour
SELECT ts, vpin FROM ticks_binance
WHERE ts > NOW() - INTERVAL '1 hour'
ORDER BY ts;

-- Average price in last 5 minutes
SELECT AVG(price) FROM ticks_binance WHERE ts > NOW() - INTERVAL '5 minutes';
```

---

## 2. Chainlink (Polygon Mainnet) — NEW

**Module:** `engine/data/feeds/chainlink_feed.py`  
**Chain:** Polygon Mainnet  
**Update rate:** Poll every 5s (on-chain updates every 10-30s)  
**Key:** No API key required — public on-chain reads via `POLYGON_RPC_URL`

### Contract Addresses
| Asset | Address |
|-------|---------|
| BTC/USD | `0xc907E116054Ad103354f2D350FD2514433D57F6f` |
| ETH/USD | `0xF9680D99D6C9589e2a93a78A04A279e509205945` |
| SOL/USD | `0x10C8264C0935b3B9870013e057f330Ff3e9C56dC` |
| XRP/USD | `0x785ba89291f676b5386652eB12b30cF361020694` |

### ABI
```json
latestRoundData() → (roundId, answer, startedAt, updatedAt, answeredInRound)
decimals() → 8  (all Chainlink crypto feeds use 8 decimal places)
```

### Why it matters
Chainlink is the **oracle source of truth** for Polymarket's BTC/ETH/SOL/XRP resolution.
When a window closes, Polymarket uses Chainlink's on-chain price to determine WIN/LOSS.
Comparing live Chainlink vs Tiingo shows if there's oracle lag we can exploit.

### Table: `ticks_chainlink`
```sql
CREATE TABLE ticks_chainlink (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    asset       VARCHAR(10) NOT NULL,
    price       DOUBLE PRECISION NOT NULL,
    round_id    BIGINT,
    updated_at  BIGINT,          -- Unix timestamp of on-chain update
    source      VARCHAR(50) DEFAULT 'chainlink_polygon'
);
CREATE INDEX idx_ticks_chainlink_ts ON ticks_chainlink(ts DESC);
CREATE INDEX idx_ticks_chainlink_asset_ts ON ticks_chainlink(asset, ts DESC);
```

### Query examples
```sql
-- Latest Chainlink price for each asset
SELECT DISTINCT ON (asset) asset, price, ts
FROM ticks_chainlink
ORDER BY asset, ts DESC;

-- BTC price last 30 minutes
SELECT ts, price, round_id FROM ticks_chainlink
WHERE asset = 'BTC' AND ts > NOW() - INTERVAL '30 minutes'
ORDER BY ts DESC;

-- Compare Chainlink vs Binance (oracle lag analysis)
SELECT
    c.ts,
    c.asset,
    c.price AS chainlink_price,
    b.price AS binance_price,
    (c.price - b.price) / b.price * 100 AS diff_pct
FROM ticks_chainlink c
JOIN LATERAL (
    SELECT price FROM ticks_binance
    WHERE ts <= c.ts ORDER BY ts DESC LIMIT 1
) b ON TRUE
WHERE c.asset = 'BTC' AND c.ts > NOW() - INTERVAL '1 hour'
ORDER BY c.ts DESC;
```

---

## 3. Tiingo Top-of-Book — NEW

**Module:** `engine/data/feeds/tiingo_feed.py`  
**Update rate:** Poll every 2s  
**Key:** `TIINGO_API_KEY` in `.env`  
**Endpoint:** `https://api.tiingo.com/tiingo/crypto/top?tickers=btcusd,ethusd,solusd,xrpusd&token=KEY`

### What Tiingo provides
- Best bid/ask prices **with exchange attribution**
- Which exchange (Coinbase, Kraken, Binance, etc.) has the best bid/ask
- Last trade price and exchange
- This is critical for understanding **where the market is pricing** vs. where Chainlink oracle sits

### Table: `ticks_tiingo`
```sql
CREATE TABLE ticks_tiingo (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    asset           VARCHAR(10) NOT NULL,
    last_price      DOUBLE PRECISION,
    bid_price       DOUBLE PRECISION,
    ask_price       DOUBLE PRECISION,
    bid_exchange    VARCHAR(20),     -- e.g. 'GDAX', 'KRAKEN', 'BINANCE'
    ask_exchange    VARCHAR(20),
    last_exchange   VARCHAR(20),
    source          VARCHAR(50) DEFAULT 'tiingo'
);
CREATE INDEX idx_ticks_tiingo_ts ON ticks_tiingo(ts DESC);
CREATE INDEX idx_ticks_tiingo_asset_ts ON ticks_tiingo(asset, ts DESC);
```

### Query examples
```sql
-- Latest top-of-book for all assets
SELECT DISTINCT ON (asset) asset, bid_price, ask_price, bid_exchange, ask_exchange, ts
FROM ticks_tiingo
ORDER BY asset, ts DESC;

-- ETH bid/ask spread over time
SELECT ts, bid_price, ask_price, ask_price - bid_price AS spread
FROM ticks_tiingo
WHERE asset = 'ETH' AND ts > NOW() - INTERVAL '1 hour'
ORDER BY ts DESC;

-- Which exchange is leading price (most often has best ask)
SELECT ask_exchange, COUNT(*) as count
FROM ticks_tiingo
WHERE asset = 'BTC' AND ts > NOW() - INTERVAL '1 day'
GROUP BY ask_exchange
ORDER BY count DESC;
```

---

## 4. CoinGlass Enhanced

**Module:** `engine/data/feeds/coinglass_enhanced.py`  
**Assets:** BTC, ETH, SOL, XRP (4 staggered feeds)  
**Update rate:** ~15s per asset (staggered to respect 300 req/min limit)  
**Key:** `COINGLASS_API_KEY` in `.env`

### What CoinGlass provides
- Open interest (OI) and OI delta %
- Long/short liquidation volumes
- Taker buy/sell ratio (buy pressure)
- Funding rate (annualised)
- Long/short account ratio
- Top trader position ratio

### How it's used
- Funding rate → regime bias (positive = longs dominant → mean-revert risk)
- Taker buy % > 60 → buy pressure → UP signal confidence boost
- OI delta spike → cascade detector input
- Liquidation volumes → VPIN context

### Table: `ticks_coinglass`
```sql
CREATE TABLE ticks_coinglass (
    id                  BIGSERIAL PRIMARY KEY,
    ts                  TIMESTAMPTZ NOT NULL,
    asset               VARCHAR(10) NOT NULL,
    oi_usd              FLOAT8,
    oi_delta_pct        FLOAT8,
    liq_long_usd        FLOAT8,
    liq_short_usd       FLOAT8,
    long_pct            FLOAT8,
    short_pct           FLOAT8,
    top_long_pct        FLOAT8,
    top_short_pct       FLOAT8,
    taker_buy_usd       FLOAT8,
    taker_sell_usd      FLOAT8,
    funding_rate        FLOAT8,
    long_short_ratio    FLOAT8,
    top_position_ratio  FLOAT8,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
```

### Query examples
```sql
-- BTC taker buy pressure last 4 hours
SELECT ts,
    taker_buy_usd / NULLIF(taker_buy_usd + taker_sell_usd, 0) * 100 AS buy_pct,
    funding_rate * 3 * 365 AS annual_funding_pct
FROM ticks_coinglass
WHERE asset = 'BTC' AND ts > NOW() - INTERVAL '4 hours'
ORDER BY ts DESC;
```

---

## 5. Gamma API (Polymarket)

**Module:** `engine/data/feeds/polymarket_5min.py`  
**Assets:** BTC, ETH, SOL, XRP  
**Update rate:** Per request (every ~1s during active windows)  
**Key:** None (public API)

### What Gamma provides
- Polymarket token prices (UP/DOWN probabilities)
- Token IDs for CLOB order placement
- Window open/close times
- Best ask for UP and DOWN tokens

### How it's used
- Window OPEN → record open price → start TWAP tracker
- Every signal → update Gamma prices for the active window
- T-60s → use current Gamma price as entry point for order
- Resolution → compare Chainlink price at close to open price

### Table: `ticks_gamma`
```sql
CREATE TABLE ticks_gamma (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    asset           VARCHAR(10) NOT NULL,
    timeframe       VARCHAR(5),
    window_ts       BIGINT,
    up_price        FLOAT8,
    down_price      FLOAT8,
    price_source    VARCHAR(50),
    up_token_id     VARCHAR(120),
    down_token_id   VARCHAR(120),
    slug            VARCHAR(200),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

### Query examples
```sql
-- Recent BTC Gamma prices
SELECT ts, window_ts, up_price, down_price FROM ticks_gamma
WHERE asset = 'BTC' ORDER BY ts DESC LIMIT 20;
```

---

## 6. TimesFM

**Module:** `engine/signals/timesfm_client.py`  
**Asset:** BTC (forecast only)  
**Update rate:** ~1s during active windows  
**Endpoint:** `TIMESFM_URL` from `.env` (default: `http://3.98.114.0:8080`)

### What TimesFM provides
- ML-based price direction forecast (UP/DOWN)
- Confidence score (0-1)
- Predicted close price
- 10th/50th/90th percentile forecasts
- Window-horizon-aware (uses seconds-to-close for forecast horizon)

### How it's used (v5.8 agreement-only mode)
TimesFM is **NOT** a standalone strategy. It's used as an **agreement signal**
inside the FiveMinVPIN strategy:
- If TimesFM agrees with TWAP direction → confidence multiplier applied
- If TimesFM disagrees → confidence penalty
- Agreement threshold: `TIMESFM_MIN_CONFIDENCE` (default 0.30)

### Table: `ticks_timesfm`
```sql
CREATE TABLE ticks_timesfm (
    id                  BIGSERIAL PRIMARY KEY,
    ts                  TIMESTAMPTZ NOT NULL,
    asset               VARCHAR(10) NOT NULL,
    window_ts           BIGINT,
    window_close_ts     BIGINT,
    seconds_to_close    INTEGER,
    horizon             INTEGER,
    direction           VARCHAR(4),
    confidence          FLOAT8,
    predicted_close     FLOAT8,
    spread              FLOAT8,
    p10                 FLOAT8,
    p50                 FLOAT8,
    p90                 FLOAT8,
    delta_vs_open       FLOAT8,
    fetch_latency_ms    FLOAT8,
    is_stale            BOOLEAN,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
```

### Query examples
```sql
-- TimesFM accuracy: did direction match actual BTC move?
SELECT
    t.window_ts,
    t.direction AS predicted,
    t.confidence,
    CASE WHEN b_close.price > b_open.price THEN 'UP' ELSE 'DOWN' END AS actual
FROM ticks_timesfm t
JOIN LATERAL (
    SELECT price FROM ticks_binance
    WHERE ts >= to_timestamp(t.window_ts)
    ORDER BY ts ASC LIMIT 1
) b_open ON TRUE
JOIN LATERAL (
    SELECT price FROM ticks_binance
    WHERE ts >= to_timestamp(t.window_ts + 300)
    ORDER BY ts ASC LIMIT 1
) b_close ON TRUE
WHERE t.ts > NOW() - INTERVAL '24 hours'
ORDER BY t.window_ts DESC;
```

---

## 7. CLOB Order Book (Polymarket) — NEW

**Module:** `engine/data/feeds/clob_feed.py`  
**Assets:** BTC (follows active 5-min window)  
**Update rate:** Every 10s  
**Endpoint:** `https://clob.polymarket.com/book?token_id=<TOKEN>`  
**Runs on:** Montreal only (Polymarket geo-blocked)

### Why CLOB, not Gamma?

Gamma API's `outcomePrices` are **stale/smoothed** — they lag the real market.
The CLOB is the **ground truth** — it's what you actually pay when you trade.

| Source | Stale by | Data |
|--------|----------|------|
| Gamma API | 5-60s | Smoothed mid-price |
| CLOB Book | Real-time | Actual best bid/ask from order book |

### What it provides
- **UP token:** best bid, best ask
- **DOWN token:** best bid, best ask
- **Spreads:** per-token bid-ask spread (liquidity indicator)
- **Mid price:** composite from UP and DOWN asks

### How it's used
- Compare CLOB bid/ask vs Gamma prices (how stale is Gamma?)
- Real entry price estimation — CLOB best ask is what we'll actually pay
- Spread as liquidity gate — wide spread = thin book = risky entry
- Window snapshot records CLOB prices alongside Gamma for full audit trail

### Table: `ticks_clob`
```sql
CREATE TABLE ticks_clob (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    asset           VARCHAR(10) NOT NULL,
    timeframe       VARCHAR(5) DEFAULT '5m',
    window_ts       BIGINT,
    up_token_id     VARCHAR,
    down_token_id   VARCHAR,
    up_best_bid     DOUBLE PRECISION,
    up_best_ask     DOUBLE PRECISION,
    down_best_bid   DOUBLE PRECISION,
    down_best_ask   DOUBLE PRECISION,
    up_spread       DOUBLE PRECISION,
    down_spread     DOUBLE PRECISION,
    mid_price       DOUBLE PRECISION,
    source          VARCHAR(20) DEFAULT 'clob'
);
```

### Window snapshot columns
- `clob_up_bid` — CLOB best bid for UP token at evaluation
- `clob_up_ask` — CLOB best ask for UP token at evaluation
- `clob_down_bid` — CLOB best bid for DOWN token at evaluation
- `clob_down_ask` — CLOB best ask for DOWN token at evaluation

### Query examples
```sql
-- Compare Gamma vs CLOB pricing (stale detection)
SELECT
    ws.window_ts,
    ws.gamma_up_price AS gamma_up,
    ws.clob_up_ask AS clob_up_ask,
    ws.gamma_up_price - ws.clob_up_ask AS gamma_clob_diff,
    ws.outcome
FROM window_snapshots ws
WHERE ws.clob_up_ask IS NOT NULL
ORDER BY ws.window_ts DESC LIMIT 20;

-- CLOB spread over time (liquidity tracking)
SELECT ts, up_spread, down_spread, mid_price
FROM ticks_clob
WHERE asset = 'BTC' AND ts > NOW() - INTERVAL '1 hour'
ORDER BY ts DESC;

-- Average spread per window (are some windows thinner?)
SELECT
    window_ts,
    AVG(up_spread) AS avg_up_spread,
    AVG(down_spread) AS avg_down_spread,
    COUNT(*) AS samples
FROM ticks_clob
WHERE asset = 'BTC' AND ts > NOW() - INTERVAL '6 hours'
GROUP BY window_ts
ORDER BY window_ts DESC;
```

---

## Data Flow: How Sources Map to Trading Decisions

```
Binance WS (1-3Hz)
    └─► VPIN Calculator ──► Cascade Detector ──► VPINCascadeStrategy
    └─► MarketAggregator ──► MarketState ──────► All strategies
    └─► OrderManager (BTC price for resolution)

Chainlink Polygon (every 5s) ◄─────────────────────────────────────────────
    └─► ticks_chainlink (oracle source of truth for Polymarket resolution)    │
    └─► [future] Compare vs Tiingo for oracle lag signal                      │
                                                                               │
Tiingo Top-of-Book (every 2s)                                                 │
    └─► ticks_tiingo (multi-exchange bid/ask with exchange attribution)        │
    └─► [future] Oracle lag detector: if Tiingo >> Chainlink → directional edge

CoinGlass Enhanced (every 15s)
    └─► ticks_coinglass
    └─► FiveMinVPINStrategy (taker buy %, funding rate modifier)
    └─► Sitrep alerts (every 5 min)

Gamma API (per window evaluation)
    └─► ticks_gamma
    └─► FiveMinVPINStrategy (entry price for UP/DOWN tokens)
    └─► CLOB order placement (token IDs)

CLOB Order Book (every 10s)
    └─► ticks_clob (ground truth bid/ask from Polymarket order book)
    └─► Window snapshots (clob_up_bid/ask, clob_down_bid/ask)
    └─► [future] Replace Gamma for entry pricing (CLOB best ask = real cost)

TimesFM (every 1s during windows)
    └─► ticks_timesfm
    └─► FiveMinVPINStrategy (agreement signal: boosts/penalises confidence)
```

---

## Environment Variables

```bash
# Chainlink / Polygon RPC
POLYGON_RPC_URL=https://polygon-rpc.com/

# Chainlink contract addresses (informational — hardcoded in chainlink_feed.py)
CHAINLINK_BTC_USD=0xc907E116054Ad103354f2D350FD2514433D57F6f
CHAINLINK_ETH_USD=0xF9680D99D6C9589e2a93a78A04A279e509205945
CHAINLINK_SOL_USD=0x10C8264C0935b3B9870013e057f330Ff3e9C56dC
CHAINLINK_XRP_USD=0x785ba89291f676b5386652eB12b30cF361020694

# Tiingo
TIINGO_API_KEY=<your-key>
```

---

## Multi-Source Delta Strategy (v7.2)

The engine now calculates **three independent window deltas** at evaluation time:

| Delta | Source | Role |
|-------|--------|------|
| `delta_binance` | Binance WebSocket (`state.btc_price`) | Legacy baseline |
| `delta_chainlink` | Chainlink oracle (DB: `ticks_chainlink`) | **PRIMARY** — oracle-aligned |
| `delta_tiingo` | Tiingo top-of-book (DB: `ticks_tiingo`) | Secondary validation |

### Primary Delta Selection

The `DELTA_PRICE_SOURCE` env var (default: `chainlink`) controls which delta drives the trading decision:

| Value | Behaviour |
|-------|-----------|
| `chainlink` | Use Chainlink oracle price as primary (default — aligns with Polymarket settlement) |
| `binance` | Use Binance WebSocket price (legacy v7.1 behaviour) |
| `tiingo` | Use Tiingo top-of-book price |
| `consensus` | Only trade when ALL sources agree on direction (most conservative) |

Fallback: if the selected source is unavailable, falls back to Binance (always available).

### Consensus Scoring

`price_consensus` column in `window_snapshots`:
- `AGREE` — all available sources point the same direction
- `MIXED` — 2 out of 3 sources agree
- `DISAGREE` — sources split evenly (only possible with 2 sources)

If Chainlink and Binance disagree on direction, the engine logs a `LOW` confidence flag (but still trades unless `DELTA_PRICE_SOURCE=consensus`).

### DB Columns Added

**window_snapshots:**
- `delta_chainlink` — Chainlink-based window delta %
- `delta_tiingo` — Tiingo-based window delta %
- `delta_binance` — Binance-based window delta %
- `price_consensus` — AGREE / MIXED / DISAGREE
- `binance_close` — Binance price at trade resolution
- `chainlink_binance_direction_match` — did CL and Binance agree at close?
- `resolution_delay_secs` — seconds from evaluation to oracle resolution

**countdown_evaluations:**
- `chainlink_price` — Chainlink price at each T-minus snapshot
- `tiingo_price` — Tiingo price at each T-minus snapshot
- `binance_price` — Binance price at each T-minus snapshot

### Switching Delta Source

To revert to Binance-only delta (legacy):
```bash
DELTA_PRICE_SOURCE=binance
```

To require full consensus before trading:
```bash
DELTA_PRICE_SOURCE=consensus
```

---

## Adding a New Data Source

1. Create `engine/data/feeds/<name>_feed.py` with `start()` / `stop()` async methods
2. Add table creation to `ensure_tables()` in `TickRecorder` (or create a migration)
3. Instantiate in `Orchestrator.__init__` (or `start()` if pool is needed)
4. Start as `asyncio.create_task()` in `Orchestrator.start()`
5. Add to `stop()` in `Orchestrator.stop()`
6. Update this doc
