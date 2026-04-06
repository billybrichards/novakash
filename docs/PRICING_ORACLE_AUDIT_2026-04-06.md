# Pricing & Oracle Audit — April 6, 2026

## Executive Summary

Our engine's reported P&L today was **-$52.51**. The actual Polymarket P&L is **-$11.41**. We've been operating with fundamentally wrong data about our own performance due to six interconnected issues with CLOB accuracy, fill detection, pricing sources, and oracle alignment.

This document covers every issue found, root cause, and fix plan.

---

## Issue 1: YES/UP Fills Not Detected

### What happens
The engine places YES (UP) orders on the CLOB. The CLOB accepts them, returns an order ID, and **matches them successfully** — confirmed by querying the CLOB directly:

```
Order: 0x47d5df34...
CLOB status: MATCHED
Side: BUY
Size matched: 6.85 shares
Price: $0.73
```

But our DB shows this same order as `status=OPEN`, `size_matched=null`. The engine doesn't know the order filled.

### Root cause
The fill-check loop polls every 5 seconds for 30 seconds max. YES orders may match with a slight delay (1-3 seconds after the poll window). After 30 seconds the engine gives up and marks the order as unfilled.

Additionally, the order ID appears twice in our DB — once as the original order, once as a "retry" attempt — both sharing the same CLOB order ID. The fill-check only runs on the first record.

### Evidence
- 17:54 UTC: YES order `0x47d5df34` → CLOB says MATCHED, DB says OPEN
- Polymarket activity shows: `17:54 TRADE Up @ $0.730 | $5.00` then `17:57 REDEEM | $6.85` → **+$1.85 profit** engine never recorded
- Every single YES order in the last 24h shows `EXPIRED` or `OPEN` in our DB
- We've been reporting 0% UP fill rate when it's actually >0%

### Impact
- Wrong WR calculation (UP wins not counted)
- Wrong P&L ($41 discrepancy today)
- Wrong strategic conclusions ("UP never fills" → actually fills fine)
- Missed redemption tracking

### Fix
1. **Extend fill poll to 60 seconds** (from 30) with first check at 3s
2. **Add reconciliation loop**: every 5 minutes, query `data-api.polymarket.com/activity` and backfill any fills our DB missed
3. **Deduplicate order records**: one DB row per CLOB order ID, not two

---

## Issue 2: P&L Tracking Mismatch

### What happens
Our DB calculates P&L as: `pnl = payout - stake` where `payout = shares * $1.00` for wins, `$0.00` for losses. But the actual amounts on Polymarket differ because:
- CLOB fills partial sizes (we request 6.85 shares, get 6.85 or fewer)
- Fill prices differ from entry prices we record
- Fees are deducted differently than our calculation

### Evidence
```
DB says: 20 resolved, 8W/12L, -$52.51
Polymarket says: 48 trades, ~18W/30L, -$11.41
Discrepancy: $41.10
```

Key differences:
- DB records $5.00 stakes but CLOB actually spends $2.25 (at $0.29 entry) or $0.16 (at $0.023 entry)
- DB doesn't track split fills (one window → multiple CLOB trades)
- DB doesn't know about YES fills (Issue 1)

### Fix
1. **Periodic reconciliation from Polymarket activity API** — authoritative P&L source
2. **Store actual CLOB fill amount** (not calculated stake) in trades table
3. **Add `actual_spent_usd` and `actual_redeemed_usd` columns** to trades table

---

## Issue 3: Fill Price vs Entry Price Mismatch

### What happens
The `entry_price` in our DB is what the engine submits as the limit price. The actual fill price on the CLOB can be very different:

```
Window 15:15: entry_price=$0.290, actual fill=$0.645 (Polymarket spent $2.25)
Window 15:30: entry_price=$0.230, actual fill=$0.695 (Polymarket spent $1.65)
Window 10:20: entry_price=$0.023, actual fill=$0.730 (Polymarket spent $0.16)
```

### Root cause
When Gamma returns `None` for `bestAsk`, the engine falls through to the model price — which can be very low ($0.02-$0.03). The CLOB receives this as a limit order and fills at the best available ask (which is $0.50-$0.73 typically). The entry_price in DB is the model price, not the fill price.

In "bestask" pricing mode, the engine does record the Gamma bestAsk. But when Gamma fails, it records garbage.

### Fix
1. **After fill confirmation, query CLOB for actual fill price** and update DB
2. **If Gamma returns None, fall back to Chainlink price** (available in ticks_chainlink, infrastructure already built)
3. **If no price source available, SKIP the trade** (safe default)

---

## Issue 4: Duplicate/Split Fills Not Reconciled

### What happens
The CLOB splits large orders across multiple counterparties. One engine order → multiple CLOB fills:

```
Window 9:45AM-9:50AM (13:49 UTC):
  Polymarket: 3 separate trades: $2.10 + $2.00 + $0.84 = $4.94
  Our DB: 1 trade at $5.00
```

Also, the engine creates two DB rows per order (original + retry) sharing the same CLOB order ID. This inflates trade counts and confuses P&L.

### Fix
1. **One DB row per CLOB order ID** — upsert, not insert
2. **Sum split fills** into the parent order record
3. **Remove duplicate retry records** or mark them as `status=SUPERSEDED`

---

## Issue 5: Floor Price Bypass ($0.03 and $0.023 Trades)

### What happens
The floor check at line 1702 of `five_min_vpin.py`:
```python
if _fresh_best_ask is not None and _fresh_best_ask < _min_entry:
    skip(...)
```

When Gamma API returns `None` (timeout, rate limit, or no market data), the condition `_fresh_best_ask is not None` is False, so the floor check is **entirely skipped**. The engine then falls through to the model price path and submits at $0.02-$0.03.

### Evidence
```
09:09 UTC: Down @ $0.030 | spent $10.00 | LOSS -$10.00 (333 shares at 3¢)
10:24 UTC: Down @ $0.023 | spent $0.16  | LOSS -$0.16
08:44 UTC: Down @ $0.247 | spent $10.00 | LOSS -$10.00
```

### Available infrastructure (not yet wired)
- `ticks_chainlink` — Chainlink Polygon push feed, updates every ~27s, live since 17:26 UTC
- `ticks_tiingo` — Tiingo multi-exchange mid-price, live since 17:22 UTC (currently stopped)
- Both are wired into orchestrator and logged in `countdown_evaluations`
- Neither is used for pricing decisions or floor checks

### Fix (two layers)
**Layer 1 — Safe default (1 line):**
```python
if _fresh_best_ask is None or _fresh_best_ask < _min_entry:
    skip("NO_PRICE or BELOW_FLOOR")
```

**Layer 2 — Chainlink fallback:**
```python
if _fresh_best_ask is None:
    _fresh_best_ask = await self._db.get_latest_chainlink_price(asset)
    # Chainlink price ≈ $69,400 (BTC/USD) — need to map to token price
    # Token price = based on delta from window open → close
    # So Chainlink doesn't directly give token price, but confirms price exists
```

**Layer 3 — Direct CLOB query (best):**
```python
if _fresh_best_ask is None:
    book = await self._poly.get_order_book(token_id)
    _fresh_best_ask = float(book['asks'][0]['price'])
```

This uses the CLOB orderbook directly — same source the trade will execute against. Most accurate, no Gamma dependency.

---

## Issue 6: Duplicate Redemption Counting

### What happens
The Polymarket activity API shows redemptions per conditionId. When two fills happen on the same market (split fills or original+retry), the redemption appears to be counted against each trade separately.

```
08:54 Down @ $0.800 | spent $10.00 | redeemed $30.20 | +$20.20
08:55 Down @ $0.560 | spent $9.91  | redeemed $30.20 | +$20.29
```

Both show $30.20 redeemed — but that's the same redemption counted twice in our audit. Actual redemption was $30.20 once, covering both fills.

### Fix
Deduplicate redemptions by `conditionId` when calculating P&L from activity API.

---

## Oracle Mismatch — Why We Lost Today Despite Correct Signals

### The core problem
Polymarket resolves using **Chainlink Data Streams** — a multi-exchange LWBA (Liquidity-Weighted Bid/Ask) median from 15+ oracle nodes. Our signal uses **Binance BTC/USDT** last-trade price.

These diverge due to:
1. **USDT premium**: Binance trades in USDT, Chainlink aggregates USD-settled venues (Coinbase, Kraken, Gemini). $10-50 spread at any instant.
2. **Timing**: Chainlink reads exact millisecond timestamps. Our Binance klines use first/last trade in a bar.
3. **Price type**: Chainlink uses orderbook mid-price. We use last execution price.

### Today's measured divergence
At 17:44 UTC snapshot:
```
Chainlink: $69,468.07
Tiingo:    $69,468.99
Binance:   $69,435.20
Spread:    +$33 (Chainlink above Binance)
```

### Mismatch rate
Of 21 resolved windows where Binance showed DOWN:
- 9 (43%): Oracle also said DOWN — MATCH
- 12 (57%): Oracle said UP — MISMATCH

**57% mismatch rate.** More than half our Binance-DOWN signals resolve opposite on Chainlink.

### Why we missed the trend
BTC went from $67,400 → $69,700 (+3.4%) over the session. The engine bet DOWN on every single window because Binance showed micro-dips within the uptrend. Chainlink, reading the broader multi-exchange picture, saw these as noise within a clear uptrend and resolved UP.

The macro observer would have caught this: 75% UP ratio in resolved windows = BULL bias → gate DOWN bets. But the macro observer isn't wired in yet.

### Available data feeds for better alignment
| Feed | Status | Use case |
|---|---|---|
| ticks_chainlink | ✅ Live (1,269 rows) | Oracle-proxy pricing, divergence risk calc |
| ticks_tiingo | ⚠️ Stopped at 17:24 (47 rows) | Multi-exchange mid-price, exchange attribution |
| ticks_binance | ✅ Live (3.6M rows) | Current signal source (misaligned with oracle) |

---

## Macro Observer — Design & Status

### What it does
Separate Railway service polling every 60s. Gathers market conditions, calls Claude Sonnet, writes a MacroSignal to `macro_signals` DB table. Engine reads latest signal each window.

### Key inputs
- Last 12+48 resolved Polymarket outcomes (oracle's own history)
- BTC deltas at 15m/1h/4h/24h
- Chainlink-Binance spread (oracle divergence risk)
- CoinGlass: OI, funding, L/S, taker, liquidations
- Recent AI analyses (Claude reading its own prior reasoning)
- Upcoming macro events (Fed/CPI calendar)
- VPIN trend, regime streak, session stats

### Three modes
- **Neutral (<50%)**: engine unchanged
- **Trend-Aware (50-79%)**: gate contrarian bets, adjust thresholds
- **Override (80%+)**: early entry T-120/T-180, direction flip, 1.3x sizing

### Oracle divergence gate (independent of macro mode)
| Chainlink-Binance spread | Action on DOWN bets |
|---|---|
| < $15 | No change |
| $15-30 | Delta threshold +50% |
| > $30 | Delta threshold +100% |
| > $50 | Block DOWN entirely |

### Status
- ✅ `macro_signals` + `macro_events` tables created on Railway
- ✅ `macro-observer/observer.py` built and committed
- ⚠️ Railway service needs deployment (Billy: set root dir = `macro-observer` in dashboard)
- 🔲 Engine wiring (Phase 2) — orchestrator reads macro_signals each window

---

## FOK Ladder — Design

### Current system
Single GTC/GTD order at bestAsk (or cap), poll for 30s, bump +2¢ once, accept miss.

**Result:** ~60% of orders expire unfilled. YES orders show as unfilled in DB even when they actually matched (Issue 1).

### FOK Ladder design
3 rapid FOK attempts over 10-15 seconds, each with fresh CLOB price + signal re-evaluation:

```
Attempt 1: FOK at CLOB best ask (direct book query, not Gamma)
  → If FILLED: done ✅
  → If KILLED: wait 2s

Re-check: VPIN still valid? Delta still in range? Macro still allows?
  → If signal died: abort

Attempt 2: FOK at best ask + 1¢
  → If FILLED: done ✅  
  → If KILLED: wait 2s

Re-check signal again

Attempt 3: FOK at best ask + 2¢
  → If FILLED: done ✅
  → If KILLED: accept miss
```

### Key improvements over current system
| Problem | FOK fix |
|---|---|
| Stale Gamma prices | Direct CLOB book query each attempt |
| GTC sits unfilled 30s | FOK instant — fills or kills in <1s |
| Single price point | 3 escalating price points |
| No signal re-eval mid-order | Re-checks VPIN/delta/macro each step |
| YES orders "never fill" | FOK removes GTD timing issue |
| No floor check when Gamma=None | CLOB book is the floor check |

### Pre-requisite
Fix Issue 1 (fill detection) first — otherwise FOK fills on YES tokens would also go unrecorded.

---

## Priority Fix Order

| # | Issue | Impact | Effort | Dependencies |
|---|---|---|---|---|
| 1 | Floor bypass (None bestAsk) | -$10-20 per incident | 1 line | None |
| 2 | YES fill detection | $41 P&L discrepancy today | 2 hours | None |
| 3 | Fill price recording | Wrong analysis/R&R calcs | 1 hour | #2 |
| 4 | Activity API reconciliation | Authoritative P&L | 2 hours | #2, #3 |
| 5 | Macro observer deployment | ~$15-20/day improvement | 30 min | Billy deploys Railway service |
| 6 | FOK Ladder | ~$80/day from unfilled orders | 1 day | #1, #2, #3 |
| 7 | Swap delta to Chainlink/Tiingo | Fix 57% oracle mismatch | 4 hours | Feed stability confirmed |
| 8 | Duplicate order cleanup | Clean DB | 1 hour | #2 |

---

## Appendix: Real P&L from Polymarket Activity API

```
Total trades:    48
Total spent:     $225.56
Total redeemed:  $214.15
NET P&L:         -$11.41

UP trades:   6  | spent $18.12  | redeemed $19.14  | P&L +$1.02
DOWN trades: 42 | spent $207.44 | redeemed $195.01 | P&L -$12.42
```

Our DB reported -$52.51. Actual is -$11.41. The system is performing 4x better than we thought.
