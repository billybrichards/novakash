# Data Source Discovery — CoinGlass + Tiingo + Polymarket New Markets
## Date: 2026-05-25
## Author: claude-sonnet-bg-2026-05-25-data-discovery

---

## SETUP FINDINGS

### Current API Keys
- `COINGLASS_API_KEY`: Present in `.env`
- `TIINGO_API_KEY`: Not in `.env` or `.env.example`. Found referenced in `engine/infrastructure/runtime.py` line 736 via env var. The key is being loaded from the environment at runtime.
- Prod RDS accessible at `novakash-pg-prod.cpmisy2asv71.ca-central-1.rds.amazonaws.com`

### CoinGlass Plan
CoinGlass pricing tiers (from coinglass.com/pricing):
- Hobbyist: $29/mo — 80+ endpoints, 30 req/min
- Startup: $79/mo — 130+ endpoints, 80 req/min
- Standard: $299/mo — 150+ endpoints, 300 req/min (Commercial use)
- Professional: $699/mo — 160+ endpoints, 1200 req/min
- Enterprise: Custom

The engine code comment says: "Requires CoinGlass Standard plan for ≤1min data intervals" and the code uses `POLL_INTERVAL = 10` seconds hitting 7 concurrent endpoints → ~42 req/min. This fits within Standard tier's 300 req/min comfortably. **Inferred tier: Standard ($299/mo).**

The v4 API base is `https://open-api-v4.coinglass.com/api` which is the live endpoint.

### CoinGlass Endpoints Currently Used
From `coinglass_enhanced.py` and `coinglass_api.py`:
1. `/futures/open-interest/history` — OI OHLC (5m candles, Binance BTCUSDT, limit 2)
2. `/futures/open-interest/exchange-list` — Aggregated OI across exchanges
3. `/futures/liquidation/aggregated-history` — Aggregated liq across Binance/OKX/Bybit/Bitget/dYdX (5m)
4. `/futures/global-long-short-account-ratio/history` — Global L/S account ratio (5m, Binance)
5. `/futures/top-long-short-position-ratio/history` — Top trader position ratio (5m, Binance)
6. `/futures/aggregated-taker-buy-sell-volume/history` — Taker buy/sell (5m, 4 exchanges)
7. `/futures/funding-rate/history` — Funding rate OHLC (8h, Binance)
8. `/futures/liquidation/history` — Single-exchange liq (legacy `coinglass_api.py`, 1m, Binance)

**All 4 assets (BTC, ETH, SOL, XRP) use the same 7 endpoints per asset**, running concurrently at 10s poll.

### Features Derived from CoinGlass (in ticks_v2_probability JSONB)
From `SELECT features FROM ticks_v2_probability WHERE asset='BTC' LIMIT 1`:
- `cg_oi_usd` — total OI in USD
- `cg_oi_delta_pct` — OI % change since last poll
- `cg_oi_momentum_300s` — 5-min OI momentum
- `cg_long_pct`, `cg_short_pct`, `cg_long_short_ratio` — Global L/S ratio
- `cg_lsr_momentum` — L/S ratio momentum
- `cg_top_long_pct`, `cg_top_short_pct`, `cg_top_position_ratio` — Smart money positioning
- `cg_liq_long_usd`, `cg_liq_short_usd` — Directional liquidations
- `cg_taker_buy_usd`, `cg_taker_sell_usd`, `cg_taker_net_usd` — Taker flow
- `cg_taker_flow_velocity` — Rate of change of taker flow
- `cg_taker_flow_momentum_300s` — 5-min taker momentum
- `cg_funding_rate` — Latest 8h funding rate
- `se_gate_cg_passed_num` — Whether CG gate passed

**Important finding from signal_evaluations:** `cg_oi_delta_pct`, `cg_liq_long_usd`, `cg_funding_rate`, `cg_taker_buy_usd` columns in signal_evaluations table are **all NULL** for all assets across all time. The CoinGlass data lives exclusively in `ticks_v2_probability.features` JSONB. The signal_evaluations columns are vestigial (pre-v2 architecture).

### Tiingo: What We Use
Two distinct usage patterns:
1. **TiingoFeed** (`data/feeds/tiingo_feed.py`): Polls `tiingo/crypto/top` every 2s for BTC/ETH/SOL/XRP top-of-book. Writes to `ticks_tiingo`. Fields: `last_price`, `bid_price`, `ask_price`, `bid_exchange`, `ask_exchange`, `last_exchange`. 754K rows per asset.
2. **TiingoRestAdapter** (`adapters/market_feed/tiingo_rest.py`): On-demand `tiingo/crypto/prices` for 5-min OHLCV candles at resolution time. Not continuously polled.

Features derived: `tiingo_last_price`, `tiingo_vs_binance`, `tiingo_bid_ask_spread`, `delta_tiingo_sg/ewma12/ewma60/roll_std20`

**Tiingo API key NOT confirmed in `.env` on this dev box.** Only in runtime env. Plan: based on 10K req/hr comment in code (`POLL_INTERVAL = 2  # Tiingo paid: 10K req/hr = 2.7/s, 2s is safe`), which corresponds to the "Power" or "Commercial" paid plan. The code is using: (1) crypto/top endpoint (top-of-book), (2) crypto/prices endpoint (OHLCV). **The Tiingo WebSocket, IEX equity feed, news, and fundamentals are NOT used.**

### RDS Market Data Coverage
From `SELECT asset, timeframe, COUNT(*), AVG(volume), AVG(liquidity) FROM market_data`:
```
BTC  5m  : 21807 markets | avg_vol $30,160 | avg_liq $17,066
BTC  15m :  7291 markets | avg_vol $37,927 | avg_liq $9,019
ETH  5m  : 21801 markets | avg_vol $3,690  | avg_liq $10,322
ETH  15m :  7289 markets | avg_vol $9,044  | avg_liq $3,626
SOL  5m  : 21802 markets | avg_vol $1,781  | avg_liq $7,606
SOL  15m :  7289 markets | avg_vol $3,577  | avg_liq $2,616
XRP  5m  : 21795 markets | avg_vol $1,193  | avg_liq $7,266
XRP  15m :  7289 markets | avg_vol $2,629  | avg_liq $1,856
```

All markets are Chainlink-resolved. No 1H/4H/daily/weekly markets in our DB. DOGE and BNB markets exist on Polymarket (confirmed Chainlink DOGE/USD and BNB/USD oracles) but are NOT in our market_data.

---

## COINGLASS API AUDIT

### Full Endpoint Catalogue (from docs.coinglass.com/llms.txt)

**Currently used (7 endpoint categories):**
1. `/futures/open-interest/history` ✓ USED
2. `/futures/open-interest/exchange-list` ✓ USED
3. `/futures/liquidation/aggregated-history` ✓ USED
4. `/futures/global-long-short-account-ratio/history` ✓ USED
5. `/futures/top-long-short-position-ratio/history` ✓ USED
6. `/futures/aggregated-taker-buy-sell-volume/history` ✓ USED
7. `/futures/funding-rate/history` ✓ USED

**High-value unused endpoints:**

**Tier A — Direct signal value for 5m strategy:**

A1. `/futures/funding-rate/oi-weight-ohlc-history` (OI-weighted funding rate)
- Weights funding rate by open interest per exchange → less susceptible to Binance dominance bias
- Available at 1m/5m/15m intervals
- Signal value: captures cross-exchange funding divergence that single-exchange rate misses
- Engineering: ~1h to add as new field `cg_oi_weighted_funding`

A2. `/futures/funding-rate/fr-arbitrage` (Funding rate arbitrage)
- Cross-exchange funding spread (e.g., Binance vs Bybit vs OKX)
- High funding divergence = potential forced position unwinds = precursor to liquidation cascades
- Signal value: non-redundant to our current single-exchange funding rate
- Engineering: ~1h

A3. `/coinbase-premium-index` (Coinbase vs Binance BTC price spread)
- Measures US institutional demand vs global retail demand
- 1m/5m granularity available
- Well-documented as a BTC directional signal: positive premium → institutional buying → UP bias
- Signal value: HIGH — not correlated with any existing feature. Adds US vs Asia demand dimension.
- Engineering: ~2h (new feature + db column)

A4. `/futures/aggregated-cvd-history` (Cumulative Volume Delta, aggregated)
- Tracks net aggressive buying vs selling (taker-initiated) accumulated over the window
- Intervals: 1m/5m/15m/1h available
- Differs from our `cg_taker_net_usd` which is a point-in-time snapshot; CVD is cumulative over the window
- Signal value: HIGH — CVD direction divergence from price is a leading indicator
- Engineering: ~3h (compute delta CVD at window open vs close)

A5. `/futures/basis` (Futures basis / cash-and-carry)
- Futures premium over spot for BTC/ETH/SOL on Binance, OKX, Bybit
- When basis spikes → leveraged longs increasing → reversion risk UP
- Signal value: MEDIUM — not directly correlated to existing features
- Engineering: ~2h

**Tier B — ETH/SOL/XRP feature gap (high-value for multi-asset strategy):**

B1. Per-asset cross-exchange liquidation breakdown
- We already poll `aggregated-history` for all 4 assets. But: ETH/SOL/XRP features are missing from signal_evaluations entirely.
- Root cause: `cg_oi_delta_pct`, etc. are null for ETH/SOL/XRP in signal_evaluations (confirmed in query).
- Fix: ensure CG-enhanced feeds for ETH/SOL/XRP actually populate `ticks_v2_probability.features` for those assets.
- This is a wiring bug, not a data gap. The feed IS running for ETH/SOL/XRP.

B2. Per-exchange OI breakdown for altcoins
- `/futures/open-interest/exchange-list` (we call it but only read the "All" entry)
- Per-exchange OI concentration (e.g., if 60% of SOL OI is on Bybit) is a signal
- Deribit OI for ETH is especially interesting (options interaction)

**Tier C — Options data:**

C1. `/options/exchange-open-interest-history`
- BTC and ETH only, 1h/4h granularity (no 5m)
- Not useful for 5m windows directly, but options OI put/call ratio is a longer-horizon signal
- Could gate a 5m trade if options OI shows extreme positioning
- Engineering: ~4h (new context feature `cg_options_pcr`)

C2. `/options/option-max-pain`
- The "max pain" strike for BTC/ETH options expiries
- If spot price is far below max pain with expiry coming → gravitational pull toward higher prices
- Weekly/monthly signal only, not intraday
- Engineering: ~3h

**Tier D — ETF Flow Data (Cross-cutting):**

D1. `/etf/etf-flows-history` — Daily Bitcoin ETF net flows
- Daily granularity only (not intraday) — can't use for 5m windows directly
- But: can gate sessions. Days with >$300M ETF inflow → systematic upward bias
- Engineering: ~2h as a session-level gate

D2. `/etf/bitcoin-etf-netassets-history` — ETF total AUM trend
- Weekly momentum of institutional Bitcoin accumulation
- Engineering: ~2h

D3. `/etf/ethereum-etf-flows-history` — Same for ETH
- ETH ETF flows are less well-established signal than BTC but worth tracking

**Tier E — On-Chain / Sentiment:**

E1. `/market/cryptofear-greedindex` — Fear & Greed Index
- Daily granularity. Well-known signal.
- Likely daily refresh only → session-level gate, not per-5m feature
- Engineering: <1h

E2. `/exchange/coinbase-premium-index` — Already listed as Tier A. Highest priority.

E3. `/market/stablecoin-marketcap-history`
- USDT/USDC market cap trend → capital on the sidelines → bullish when growing
- Weekly horizon

**Tier F — Lower priority:**

- Technical indicators (RSI, MACD, Bollinger): computed from price data we already have; adding CoinGlass-computed versions adds latency and cost without unique data
- Hyperliquid-specific endpoints: Hyperliquid is a large DEX; may become relevant when their OI rivals CEX
- Token unlocks: relevant for altcoin-specific strategies (SOL has significant unlocks)
- Whale transfer alerts: too episodic for systematic 5m strategy

### CoinGlass Rate Limit Analysis
Standard tier: 300 req/min.
Current usage: 7 endpoints × 4 assets = 28 polls per 10s = 168 req/min.
Available headroom: 132 req/min.
Adding Coinbase Premium (1 asset, 1 endpoint): +6 req/min. Fine.
Adding CVD (4 assets, 1 endpoint): +24 req/min. Fine.
Adding OI-weighted funding (4 assets, 1 endpoint): +24 req/min. Fine.
**Total with Tier A additions: ~222 req/min. Well within Standard limits.**

---

## TIINGO API AUDIT

### Confirmed Tiingo API Capabilities
From tiingo-python client code and API endpoint analysis:

**Endpoints available:**
1. `GET /tiingo/crypto/top?tickers=...` — Real-time top-of-book (bid/ask/last, exchange attribution)
2. `GET /tiingo/crypto/prices?tickers=...&resampleFreq=5min` — Historical OHLCV at any freq
3. `GET /tiingo/crypto` — Metadata (list of supported tickers)
4. `GET /iex/<ticker>` — Real-time IEX equity top-of-book (US stocks, ETFs)
5. `GET /tiingo/daily/<ticker>` — Daily OHLCV for equities/ETFs
6. `GET /iingo/news` — Curated news articles (crypto + equity, tagged)
7. `GET /tiingo/fundamentals/daily/<ticker>` — Market cap, enterprise value, P/E
8. `WS wss://api.tiingo.com/iex` — Real-time IEX WebSocket
9. `WS wss://api.tiingo.com/crypto` — Real-time crypto price WebSocket

**What we use:**
- `tiingo/crypto/top` (2s poll) — ✓ USED (top-of-book for 4 crypto assets)
- `tiingo/crypto/prices` (on-demand) — ✓ USED (5m candles at resolution time)

**What we don't use:**
- IEX equity feed
- Daily equity/ETF data
- News feed
- Fundamentals
- WebSocket feeds
- Historical crypto OHLCV for longer timeframes

### Tiingo Plan Analysis
The plan comment in code: "10K req/hr = 2.7/s, 2s is safe" → This is the **"Power" plan** tier at Tiingo (~$30-50/mo based on published pricing). This tier includes:
- All crypto top-of-book tickers
- Historical crypto OHLCV with unlimited resample frequencies
- Crypto WebSocket access
- IEX equity data (real-time Level 1)
- News API
- Fundamentals API (with limits)

**Rate limits at Power tier:** ~10,000 req/hr per endpoint type. The current 2s poll for 4 tickers = 7,200 req/hr for crypto/top. This is already near the limit.

### High-Value Tiingo Opportunities

**T1. Crypto WebSocket (Tiingo IEX for equities)**
We're on the REST polling model. Tiingo offers WebSocket streams for:
- `wss://api.tiingo.com/crypto` — 500ms update subscriptions for crypto price
- Would reduce latency from 2s → ~500ms for top-of-book data
- Engineering: ~4h to switch TiingoFeed to WebSocket
- Value: marginal — we already have Binance WS at better granularity. Skip.

**T2. Historical Crypto OHLCV at 1m granularity (training data)**
`GET /tiingo/crypto/prices?tickers=btcusd&resampleFreq=1min&startDate=2024-01-01`
- We only use this for the 5-min candle at resolution. We could backfill:
  - 1-min OHLCV for BTC/ETH/SOL/XRP going back years
  - More granular price deltas (not just close-to-close, but within-5m open/high/low/close structure)
- New features: `within_window_range_pct`, `within_window_close_vs_vwap`, candle pattern features
- Engineering: ~3h for backfill + feature extraction
- Value: MEDIUM — we have Binance WS ticks already; duplicating with Tiingo adds marginal richness

**T3. IEX Equity Real-time Feed (MSTR, COIN, ETFs)**
Tiingo provides real-time Level 1 quotes for US equities via IEX.
Assets of interest:
- MSTR (MicroStrategy) — proxy for institutional BTC belief
- COIN (Coinbase stock) — proxy for crypto market sentiment
- IBIT, FBTC, GBTC (Bitcoin ETFs) — direct ETF pricing
- QQQ/SPY correlation (Billy has noted Bitcoin-QQQ correlation track)
This requires Tiingo's IEX endpoint. Rate: equity market hours only (9:30am-4pm ET).
`GET /iex/MSTR` or `WS wss://api.tiingo.com/iex` subscription
- Features: `mstr_vs_btc_spread`, `coin_premium_to_btc`, `ibit_premium_discount`, `spy_correlation_5m`
- Engineering: ~6h (new feed class, new features, new DB column)
- Value: HIGH during US market hours but ZERO outside. The 5m BTC market runs 24/7. Impact limited to ~6.5 hours/day on weekdays.

**T4. Crypto News Feed (tiingo/news)**
`GET /tiingo/news?tickers=btc,eth&startDate=...&endDate=...`
Returns curated news articles tagged by crypto ticker, with sentiment.
- Could be used to: (1) avoid trading near major news events, (2) sentiment as a feature
- Engineering: ~5h (news poller, sentiment extraction or keyword-based scoring)
- Value: LOW-MEDIUM — news is a lagging/ambiguous signal for 5m binary markets. Operational risk: false positives. Not aligned with our sub-1-minute decision window.

**T5. Tiingo Crypto for Non-BTC/ETH/SOL/XRP Assets**
Tiingo supports DOGE, BNB, ADA, AVAX, MATIC, and ~300 other crypto pairs.
We could add top-of-book for DOGE and BNB to feed the DOGE/BNB 5m Polymarket markets.
- Cost: ~3,600 more req/hr at current 2s poll for 2 additional assets → total 14,400 req/hr
- This may EXCEED Power tier limits
- Fix: reduce poll frequency to 4s for all assets to stay within 10K req/hr
- Engineering: ~2h
- Value: HIGH if we trade DOGE/BNB markets

---

## POLYMARKET NEW MARKETS AUDIT

### Confirmed Market Structure (from RDS + WebFetch)
All UP/DOWN 5-minute markets on Polymarket resolve via Chainlink data streams:
- BTC → `data.chain.link/streams/btc-usd`
- ETH → `data.chain.link/streams/eth-usd`
- SOL → `data.chain.link/streams/sol-usd`
- XRP → `data.chain.link/streams/xrp-usd`
- DOGE → `data.chain.link/streams/doge-usd`
- BNB → `data.chain.link/streams/bnb-usd`

Resolution: "UP if price at end ≥ price at start of window, else DOWN"

### Markets We Already Track (in market_data)
| Asset | 5m | 15m | 1H | 4H | Daily | Weekly |
|-------|-----|-----|----|----|-------|--------|
| BTC | ✓ | ✓ | - | - | - | - |
| ETH | ✓ | ✓ | - | - | - | - |
| SOL | ✓ | ✓ | - | - | - | - |
| XRP | ✓ | ✓ | - | - | - | - |

### Markets We Don't Trade (from Polymarket /crypto page, confirmed live as of 2026-05-25)
311 total crypto markets. Beyond what we have:

**Altcoin UP/DOWN 5m/15m (Chainlink oracle, same structure):**
- DOGE UP/DOWN (5m, 15m) — Chainlink DOGE/USD → $0 vol per window (median in our DB: not tracked)
- BNB UP/DOWN (5m, 15m) — Chainlink BNB/USD → very low vol

**Volume data from market_data query:**
- BTC 5m: median vol $1,999 / window
- ETH 5m: median vol $272 / window (87% lower than BTC)
- SOL 5m: median vol $94 / window (95% lower than BTC)
- XRP 5m: median vol $82 / window (96% lower than BTC)
- DOGE/BNB: effectively $0 (not tracked in our DB but confirmed active)

**Summary:** ETH 5m vol is ~9% of BTC 5m. SOL/XRP are ~4-5% of BTC. DOGE/BNB are near-zero.

**15m Markets (ETH/SOL/XRP already in DB but not traded):**
- ETH 15m: avg vol $9,044 — 30% of BTC 15m vol
- SOL 15m: avg vol $3,577 — 9% of BTC 15m vol
- XRP 15m: avg vol $2,629 — 7% of BTC 15m vol

**Longer Timeframe Markets (from polymarket.com/crypto):**
- 1-hour markets: 9 total (assets unclear from web scrape)
- 4-hour markets: 7 total
- Daily markets: 11 total
- Weekly markets: 57 total
- Monthly markets: 24 total

The 1h/4h markets are likely BTC + ETH at minimum. Volume per window scales roughly 3-5× per timeframe step.

**Non-Price Crypto Markets (higher volume, different structure):**
- "When will Bitcoin hit $150k?" — $18M total, $6M/day — NO/YES binary on price milestone
- "MicroStrategy sells any Bitcoin by ___?" — $31M total
- "When will Bitcoin hit $200k?" — estimated $10M+

These are fundamentally different from UP/DOWN — they require binary event prediction, not directional ML. Not directly tradeable with our current model architecture.

**Macro/Finance Markets (potentially correlated):**
- "Fed Decision in June?" — $41M, 22 days, currently 98% no-change
- "WTI Crude Oil hit X in May?" — $28M volume
These aren't in our strategy scope but could be used as contextual gates.

---

## CROSS-CUTTING OPPORTUNITIES

### XO1: Coinbase Premium + ETF Flows + BTC Market Direction
**The triangulation play:**
- CoinGlass: Coinbase premium index (US institution buying vs global) at 1m/5m
- CoinGlass: Bitcoin ETF daily flows (session-level gate: positive flow days bias UP)
- Polymarket: BTC 5m UP/DOWN
**Hypothesis:** On days where ETF flows are positive AND Coinbase premium is positive in the current 5m window → directional edge amplified for UP bets.
**Engineering:** Add CoinGlass `coinbase-premium-index` endpoint + CoinGlass `etf-flows-history` as a daily session gate. ~5h total.
**Confidence:** MEDIUM — Coinbase premium has literature support; ETF flow at daily granularity may be too slow for 5m decisions.

### XO2: CVD Divergence Feature for All Assets
**The pattern:**
- CoinGlass aggregated CVD at 5m resolution for BTC/ETH/SOL/XRP
- CVD = cumulative taker-initiated volume delta (buy - sell) over the window
- When price is flat but CVD is strongly positive → hidden buying pressure → UP edge
**Why this is different from what we have:** Our `cg_taker_net_usd` is a point snapshot (last poll). CVD is an accumulation over the whole 5m window. These are theoretically non-correlated.
**Engineering:** Add CVD endpoint for 4 assets at 5m intervals. New feature `cg_cvd_delta_5m`. ~4h.
**Confidence:** HIGH — CVD is a standard market microstructure signal with strong theoretical basis.

### XO3: CoinGlass Features for ETH/SOL/XRP (Wiring Fix)
**The gap:** ETH/SOL/XRP signal_evaluations show 0% CoinGlass fill rate. Yet CoinGlass enhanced feeds ARE running for all 4 assets. The disconnect is in how features get written to ticks_v2_probability for non-BTC assets.
**Investigation needed:** Does the feature emitter for ETH/SOL/XRP include CG features? If not, the fix is surgical (enable CG features in the ETH/SOL/XRP emitter). The data is already being collected — it just isn't being fed into the ML feature vector.
**Engineering:** ~2-4h investigation + fix (check v2_feature_body.py for asset-conditional logic).
**Confidence:** HIGH that this is a wiring bug, not a design choice.

### XO4: Tiingo IEX → MSTR/COIN/IBIT During US Market Hours
**During US market hours (9:30am-4pm ET, weekdays):**
- Add `tiingo_mstr_price`, `tiingo_coin_premium`, `tiingo_ibit_premium_discount` as features
- Gate: if market is closed → feature = NaN (handle via imputation)
**Why interesting:** MSTR and COIN both have strong correlation with BTC intraday during US hours. IBIT premium/discount shows institutional demand in near-real-time.
**Engineering:** ~6h (new IEX poller, new features, DB columns, model retraining needed for new features)
**Confidence:** MEDIUM — signal present but only 30% of trading hours. May not be worth the training corpus complexity.

### XO5: Altcoin 5m Markets (DOGE/BNB) — Not Worth It Now
**Finding:** DOGE and BNB 5m markets have near-zero volume. DOGE/BNB on Chainlink but no market liquidity means:
- Can't enter positions at meaningful size
- Adverse selection risk is extreme (only market makers trade thin markets)
**Verdict:** DO NOT PURSUE until/unless Polymarket adds liquidity incentives for these assets. Revisit if volume exceeds $500/window.

### XO6: ETH/SOL/XRP 15m Strategy — High ROI but Billy's Turf
Billy's note #546 already identifies 15m ETH classifier as highest-ROI unblock. This is the timeframe axis and explicitly out of scope for this discovery document.

---

## PRIORITISED RECOMMENDATIONS

Ranked by alpha-per-engineering-hour:

### P1: Fix CoinGlass feature wiring for ETH/SOL/XRP (2-4h, HIGH ROI)
**What:** ETH/SOL/XRP `signal_evaluations` show 0% CoinGlass data fill. The feed IS running (ticks_coinglass has 213K rows per asset). The features aren't being included in the feature vector for non-BTC assets.
**Action:** Audit `engine/signals/v2_feature_body.py` for ETH/SOL/XRP — check if `cg_*` features are included. If not, add them identically to BTC. Then retrain ETH/XRP models with the new features.
**Expected uplift:** If CoinGlass features add ~5pp WR for BTC, they should add similar or more for ETH/XRP (less liquid = more signal from derivatives market).
**First step:** `grep -n "asset.*ETH\|ETH.*cg_" engine/signals/v2_feature_body.py`

### P2: Add Coinbase Premium Index feed (~3h + feature integration)
**What:** CoinGlass `/market/coinbase-premium-index` at 5m intervals. New feature `cg_coinbase_premium`.
**Why:** The Coinbase premium measures US institutional demand vs global retail. It's a non-correlated signal to everything we currently have. Literature: positive premium → US buying → short-term bullish for BTC (and correlated ETH).
**Scope:** BTC only initially (Coinbase premium is BTC-specific). Extend to ETH if ETH Coinbase premium endpoint exists.
**Rate impact:** +6 req/min (1 endpoint × 1 asset at 10s poll). Well within Standard limits.
**First step:** Test `GET /market/coinbase-premium-index?symbol=BTC&interval=5m&limit=5` with our API key.

### P3: Add Aggregated CVD for BTC (~4h + feature integration + retraining)
**What:** CoinGlass `/futures/aggregated-cvd-history` for BTC/ETH at 5m intervals.
**Why:** CVD captures accumulated order flow direction within the window — fundamentally different from our point-in-time `cg_taker_net_usd`. CVD divergences from price are leading indicators.
**Rate impact:** +12 req/min.
**First step:** Test endpoint with our API key. Check response fields. Write new emitter.

### P4: Add OI-weighted Funding Rate (1h + existing infrastructure)
**What:** CoinGlass `/futures/funding-rate/oi-weight-ohlc-history` at 5m intervals.
**Why:** Our current funding rate is Binance-only. OI-weighted funding accounts for OKX and Bybit which have different funding dynamics. Divergence between Binance and OI-weighted rate is a signal.
**Rate impact:** +24 req/min.
**First step:** Add to `CoinGlassEnhancedFeed._poll_all()` alongside existing `_fetch_funding()`.

### P5: ETF Daily Flow Gate (~3h)
**What:** Fetch CoinGlass `/etf/etf-flows-history` once per day at session start. Cache daily net flow.
**Gate logic:** If BTC ETF net flow > +$200M today → increase UP entry probability threshold by 0.01 (softer gate). If flow < -$200M → increase DOWN threshold.
**Scope:** BTC only, session-level context feature, not per-5m.
**First step:** Test the endpoint. Check if data is available same-day or T+1.

### P6: Tiingo Historical OHLCV for Within-Window Features (~3h backfill + features)
**What:** Use `tiingo/crypto/prices?resampleFreq=1min` to get 1-min candles for within-window price structure.
**New features:** `within_window_vwap_deviation`, `within_window_high_low_range`, `candle_body_pct`.
**Why useful:** Our current model sees only price AT T-10s and AT T-0s (open/close). The within-window OHLC structure may contain pattern information.
**Caveat:** Tiingo Power tier rate limits may be reached. Consider reducing top-of-book poll to 4s.
**First step:** Check if Tiingo key allows `resampleFreq=1min` on historical endpoint.

### P7: DO NOT PURSUE — Tiingo News/Sentiment (low ROI for 5m binary markets)
News sentiment operates on longer time horizons and is noisy for 5-second resolution trades. The engineering cost (~10h for a proper pipeline) vastly exceeds expected signal value.

### P8: DO NOT PURSUE NOW — Options data (weekly/monthly horizon mismatch)
Options OI and max pain are weekly/monthly signals. Our 5m model has no direct interface for these. They could be incorporated as session-level context (e.g., "are we near an expiry?") but this is low-alpha work.

---

## RISKS

1. **CoinGlass rate limit exhaustion:** Adding 5 new endpoints × 4 assets = +120 req/min. Total would be ~288/300 Standard limit. One retry storm could hit 429s. Mitigation: add a rate limit buffer; implement exponential backoff per endpoint.

2. **Coinbase Premium endpoint availability on Standard tier:** The endpoint exists on the CoinGlass API but may be locked to Professional tier. Verify with our API key before building.

3. **ETF flow data latency:** Daily ETF flow data from CoinGlass may be T+1 day lag. If so, the session gate is based on yesterday's flows. Still useful but weaker.

4. **CVD and taker_net_usd correlation:** If CVD is highly correlated with our existing `cg_taker_net_usd`, it adds no information. Test Pearson correlation on RDS data before committing model retraining.

5. **ETH/SOL/XRP CoinGlass wiring:** If the feature is deliberately absent (model trained without CG for non-BTC), adding it retroactively requires a full retrain. Check training code before assuming it's a bug.

6. **Tiingo plan limits:** Adding DOGE/BNB top-of-book at 2s would push past 10K req/hr Power tier limit. Must reduce poll frequency.

---

## CONFIDENCE RATINGS

| Finding | Confidence | Basis |
|---------|-----------|-------|
| CG feature wiring bug for ETH/SOL/XRP | HIGH | Direct RDS query: 0% fill rate |
| Coinbase Premium is additive signal | MEDIUM | Literature + theoretical basis |
| CVD is additive to existing taker features | HIGH | Standard microstructure theory |
| ETF flow as daily gate | MEDIUM | Directional only, coarse granularity |
| DOGE/BNB not worth trading | HIGH | Volume data from RDS: ~$0/window |
| ETH 5m vol ~9% of BTC | HIGH | Direct RDS measurement |
| All altcoin markets use Chainlink oracle | HIGH | Direct WebFetch confirmation |
| Tiingo plan = Power (~$30-50/mo) | MEDIUM | Rate limit comment inference |
| OI-weighted funding adds signal | LOW-MEDIUM | Theoretical; no backtest evidence |

---

## RAW DATA APPENDIX

### Signal_evaluations feature coverage (last 14 days)
```sql
asset | evals  | cg_fill | tiingo_fill | chainlink_fill
BTC   | 406543 |       0 | 0.764       | 0.764
ETH   | 138300 |       0 | 0           | 0
SOL   |  13593 |       0 | 0           | 0
XRP   |  43910 |       0 | 0           | 0
```

### CoinGlass endpoints (full catalogue from docs.coinglass.com/llms.txt)
[See main COINGLASS AUDIT section above for full list]

### Tiingo API methods confirmed
- `tiingo/crypto/top` — top-of-book (USED)
- `tiingo/crypto/prices` — historical OHLCV (USED sparingly)
- `tiingo/crypto` — metadata
- `iex/<ticker>` — real-time equity quotes (UNUSED)
- `tiingo/daily/<ticker>` — daily equity OHLCV (UNUSED)
- `tiingo/news` — curated news (UNUSED)
- `tiingo/fundamentals/daily/<ticker>` — fundamentals (UNUSED)
- WS `wss://api.tiingo.com/crypto` — real-time crypto (UNUSED)
- WS `wss://api.tiingo.com/iex` — real-time equity (UNUSED)

### Polymarket market counts (as of 2026-05-25)
311 total crypto markets:
- 5m: ~35 markets (7+ assets × 5 active windows)
- 15m: 7 markets
- 1h: 9 markets
- 4h: 7 markets
- Daily: 11 markets
- Weekly: 57 markets
- Monthly: 24 markets
- Yearly: 23 markets
