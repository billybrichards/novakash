# Novakash V5: CoinGlass-Enriched Architecture
## What Changed, Why, and How It Works
### Deploy: April 3, 2026

---

## 1. WHAT CHANGED (v4.1 → v5)

### THREE CHANGES in this deploy:

**1. CoinGlass Standard Integration (the big one)**
- Before: CoinGlass data polled but NEVER used in trade decisions
- After: 6 real-time signals feed into a confidence modifier that confirms or warns against trades

**2. Taker Buy/Sell Volume — NEW signal**
- Before: Not tracked at all
- After: 1-min taker aggression data shows WHO is pushing the market (buyers vs sellers)

**3. Smart Money vs Crowd Divergence**
- Before: No positioning data in decisions
- After: When top traders disagree with the crowd, that's a powerful contrarian signal

---

## 2. SYSTEM ARCHITECTURE (v5)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        NOVAKASH V5 DATA PIPELINE                            │
│                                                                             │
│  ┌──────────────┐   ┌──────────────────┐   ┌──────────────┐               │
│  │   BINANCE     │   │   COINGLASS       │   │  POLYMARKET   │              │
│  │   WebSocket   │   │   REST API (v4)   │   │  WebSocket    │              │
│  │              │   │   Standard Plan   │   │              │              │
│  │  • aggTrade   │   │                  │   │  • Token px   │              │
│  │  • forceOrder │   │  Every 10 seconds:│   │  • Window     │              │
│  │              │   │  • OI (1m OHLC)   │   │    open/close │              │
│  └──────┬───────┘   │  • Liqs (1m L/S)  │   └──────┬───────┘              │
│         │           │  • L/S Ratio (1m)  │          │                      │
│         │           │  • Top Traders (1m)│          │                      │
│         │           │  • Taker Vol (1m)  │          │                      │
│         │           │  • Funding (8h)    │          │                      │
│         │           └────────┬───────────┘          │                      │
│         │                    │                      │                      │
│         ▼                    ▼                      ▼                      │
│  ┌──────────────────────────────────────────────────────────────┐          │
│  │                    SIGNAL PROCESSOR                          │          │
│  │                                                              │          │
│  │  ┌────────────┐  ┌─────────────────┐  ┌────────────────┐   │          │
│  │  │   VPIN     │  │  CG SNAPSHOT    │  │  WINDOW STATE  │   │          │
│  │  │ Calculator │  │  (point-in-time)│  │  (open price,  │   │          │
│  │  │            │  │                 │  │   delta, time) │   │          │
│  │  │ $500K      │  │ OI delta: ±X%  │  │                │   │          │
│  │  │ buckets    │  │ Liqs: $XM L/S  │  │ delta_pct      │   │          │
│  │  │ 50-bucket  │  │ Crowd: XX% L   │  │ seconds_left   │   │          │
│  │  │ lookback   │  │ Smart: XX% S   │  │                │   │          │
│  │  │            │  │ Taker: B/S vol │  │                │   │          │
│  │  │ → 0.0-1.0  │  │ Fund: ±X.XX%  │  │                │   │          │
│  │  └─────┬──────┘  └───────┬─────────┘  └───────┬────────┘   │          │
│  │        │                 │                     │            │          │
│  │        ▼                 ▼                     ▼            │          │
│  │  ┌──────────────────────────────────────────────────┐       │          │
│  │  │           REGIME-AWARE EVALUATOR (v5)            │       │          │
│  │  │                                                  │       │          │
│  │  │  Step 1: VPIN Gate (>= 0.45)                    │       │          │
│  │  │  Step 2: Regime + Direction (VPIN-based)         │       │          │
│  │  │  Step 3: Delta Check (per-regime threshold)      │       │          │
│  │  │  Step 4: Confidence (delta magnitude)            │       │          │
│  │  │  ┌────────────────────────────────────────┐      │       │          │
│  │  │  │ ★ NEW: CG Confidence Modifier (-0.5→+0.5) │  │       │          │
│  │  │  │                                        │      │       │          │
│  │  │  │ Liq direction confirms trade?    ±0.15 │      │       │          │
│  │  │  │ Smart money agrees?              ±0.15 │      │       │          │
│  │  │  │ Taker aggression aligns?         ±0.10 │      │       │          │
│  │  │  │ Crowd overleveraged (contrarian)? ±0.05│      │       │          │
│  │  │  │ Funding rate pressure?           ±0.05 │      │       │          │
│  │  │  └────────────────────────────────────────┘      │       │          │
│  │  │  Step 5: Final confidence → TRADE or SKIP       │       │          │
│  │  └──────────────────────────────────────────────────┘       │          │
│  └──────────────────────────────────────────────────────────────┘          │
│         │                                                                  │
│         ▼                                                                  │
│  ┌──────────────┐          ┌──────────────┐                               │
│  │  POLYMARKET   │          │  GAMMA API   │                               │
│  │  CLOB Order   │          │  (TRUTH)     │                               │
│  │              │          │              │                               │
│  │  GTC limit   │   ──→   │  Oracle      │                               │
│  │  +2¢ retry   │          │  resolution  │                               │
│  └──────────────┘          └──────────────┘                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. CoinGlass Signal Matrix

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    COINGLASS SIGNAL MATRIX (v5)                          │
├──────────────────┬────────────┬──────────────────┬───────────────────────┤
│ Signal           │ Interval   │ What It Tells Us │ How We Use It         │
├──────────────────┼────────────┼──────────────────┼───────────────────────┤
│ Liquidation      │ 1 min      │ WHO is getting   │ Longs liquidated →    │
│ History (L/S)    │            │ liquidated and   │ confirms DOWN         │
│                  │            │ which side       │ Shorts liquidated →   │
│                  │            │                  │ confirms UP           │
│                  │            │                  │ Weight: ±0.15         │
├──────────────────┼────────────┼──────────────────┼───────────────────────┤
│ Top Traders      │ 1 min      │ What SMART MONEY │ Smart money SHORT +   │
│ Position Ratio   │            │ is doing vs the  │ crowd LONG = strong   │
│                  │            │ crowd            │ contrarian DOWN       │
│                  │            │                  │ Weight: ±0.15         │
├──────────────────┼────────────┼──────────────────┼───────────────────────┤
│ Taker Buy/Sell   │ 1 min      │ Who is the       │ Sell vol >> Buy vol   │
│ Volume           │            │ AGGRESSOR right  │ → confirms DOWN       │
│                  │            │ now              │ momentum              │
│                  │            │                  │ Weight: ±0.10         │
├──────────────────┼────────────┼──────────────────┼───────────────────────┤
│ Global L/S       │ 1 min      │ Crowd positioning│ >60% long = over-     │
│ Account Ratio    │            │ (retail)         │ leveraged → contrarian│
│                  │            │                  │ DOWN bias             │
│                  │            │                  │ Weight: ±0.05         │
├──────────────────┼────────────┼──────────────────┼───────────────────────┤
│ Funding Rate     │ 8 hour     │ Cost of holding  │ High positive = longs │
│                  │            │ longs vs shorts  │ paying → DOWN pressure│
│                  │            │                  │ Weight: ±0.05         │
├──────────────────┼────────────┼──────────────────┼───────────────────────┤
│ OI Delta         │ 1 min      │ New positions    │ Rising OI + falling   │
│                  │            │ being opened     │ price = new shorts    │
│                  │            │ or closed        │ (bearish conviction)  │
│                  │            │                  │ Weight: context only  │
└──────────────────┴────────────┴──────────────────┴───────────────────────┘

  Total CG modifier range: -0.50 to +0.50
  Applied AFTER regime/direction decision
  Can promote LOW → MODERATE (enables trade)
  Can demote MODERATE → LOW (blocks trade)
```

---

## 4. DECISION FLOWCHART (v5)

```
┌─────────────────────────────────────────────────────────────────────┐
│                    EVERY 5-MINUTE WINDOW (v5)                       │
│                                                                     │
│  Step 1: VPIN Gate                                                  │
│  ┌───────────────────────────────────────┐                         │
│  │ VPIN < 0.45 → SKIP (no informed flow) │                         │
│  └──────────────────┬────────────────────┘                         │
│                     │ VPIN >= 0.45                                  │
│                     ▼                                               │
│  Step 2: Regime Classification + Direction                          │
│  ┌─────────────────────────────────────────────────────────┐       │
│  │ VPIN >= 0.65 → CASCADE    (momentum,  δ >= 0.03%*)     │       │
│  │ VPIN 0.55-0.65 → TRANSITION (contrarian, δ >= 0.12%)   │       │
│  │ VPIN < 0.55 → NORMAL     (contrarian, δ >= 0.08%)      │       │
│  │                                                         │       │
│  │ * Scaled: VPIN 0.75-0.85 → δ >= 0.015%                │       │
│  │           VPIN 0.85+     → δ >= 0.005%                 │       │
│  └──────────────────┬──────────────────────────────────────┘       │
│                     │                                               │
│                     ▼                                               │
│  Step 3: Base Confidence (delta magnitude)                          │
│  ┌───────────────────────────────────────┐                         │
│  │ |delta| > 0.10% → HIGH               │                         │
│  │ |delta| > 0.02% → MODERATE            │                         │
│  │ |delta| > 0.005% → LOW               │                         │
│  │ else → NONE                           │                         │
│  └──────────────────┬────────────────────┘                         │
│                     │                                               │
│                     ▼                                               │
│  ┌─────────────────────────────────────────────────────────┐       │
│  │ ★ Step 3b: CoinGlass Confidence Modifier (NEW in v5)   │       │
│  │                                                         │       │
│  │   Read CoinGlassEnhancedFeed snapshot (updated 10s ago) │       │
│  │                                                         │       │
│  │   modifier = 0.0                                        │       │
│  │                                                         │       │
│  │   ┌─ Liquidation Direction ──────────────────────┐      │       │
│  │   │ Long liqs > 2× Short liqs?                   │      │       │
│  │   │   → Longs getting wrecked → DOWN signal      │      │       │
│  │   │   → If our direction = DOWN: +0.15           │      │       │
│  │   │   → If our direction = UP:   -0.15           │      │       │
│  │   └──────────────────────────────────────────────┘      │       │
│  │                                                         │       │
│  │   ┌─ Smart Money Divergence ─────────────────────┐      │       │
│  │   │ Top traders SHORT > 55% AND crowd LONG > 60%?│      │       │
│  │   │   → Smart money bearish, crowd bullish        │      │       │
│  │   │   → If our direction = DOWN: +0.15           │      │       │
│  │   │   → If our direction = UP:   -0.15           │      │       │
│  │   └──────────────────────────────────────────────┘      │       │
│  │                                                         │       │
│  │   ┌─ Taker Aggression ───────────────────────────┐      │       │
│  │   │ Taker sell vol > 2× Taker buy vol?            │      │       │
│  │   │   → Aggressive selling                        │      │       │
│  │   │   → If our direction = DOWN: +0.10           │      │       │
│  │   │   → If our direction = UP:   -0.10           │      │       │
│  │   └──────────────────────────────────────────────┘      │       │
│  │                                                         │       │
│  │   ┌─ Crowd Positioning ──────────────────────────┐      │       │
│  │   │ Global long_pct > 60%?                        │      │       │
│  │   │   → Crowd overleveraged long → DOWN bias     │      │       │
│  │   │   → If our direction = DOWN: +0.05           │      │       │
│  │   │   → If our direction = UP:   -0.05           │      │       │
│  │   └──────────────────────────────────────────────┘      │       │
│  │                                                         │       │
│  │   ┌─ Funding Pressure ───────────────────────────┐      │       │
│  │   │ Funding rate > +0.01%?                        │      │       │
│  │   │   → Longs paying heavily → DOWN pressure     │      │       │
│  │   │   → If our direction = DOWN: +0.05           │      │       │
│  │   │   → If our direction = UP:   -0.05           │      │       │
│  │   └──────────────────────────────────────────────┘      │       │
│  │                                                         │       │
│  │   confidence_score += modifier (clamped -0.5 to +0.5)  │       │
│  │   Reclassify: NONE → LOW → MODERATE → HIGH             │       │
│  └─────────────────────────────────────────────────────────┘       │
│                     │                                               │
│                     ▼                                               │
│  Step 4: Final Gate                                                 │
│  ┌───────────────────────────────────────┐                         │
│  │ NONE / LOW → SKIP                     │                         │
│  │ MODERATE / HIGH → TRADE               │                         │
│  └──────────────────┬────────────────────┘                         │
│                     │                                               │
│                     ▼                                               │
│  Step 5: Execute                                                    │
│  ┌───────────────────────────────────────┐                         │
│  │ GTC limit order at Gamma API price    │                         │
│  │ +2¢ retry if no fill after 5s         │                         │
│  │ BET_FRACTION = 5% of bankroll         │                         │
│  └───────────────────────────────────────┘                         │
│                     │                                               │
│                     ▼                                               │
│  Step 6: Resolution                                                 │
│  ┌───────────────────────────────────────┐                         │
│  │ Query Polymarket oracle (Chainlink)   │                         │
│  │ Record WIN/LOSS from oracle truth     │                         │
│  │ NEVER trust Binance-only resolution   │                         │
│  └───────────────────────────────────────┘                         │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 5. CoinGlass Confidence Modifier — Worked Examples

### Example A: CASCADE + CoinGlass AGREES (Strong Trade)

```
Window State:
  VPIN: 0.82 (CASCADE regime)
  Delta: -0.04% (BTC falling)
  Direction: DOWN (momentum)
  Base Confidence: MODERATE

CoinGlass Snapshot:
  Liq Long: $2.1M | Liq Short: $0.3M → Longs getting wrecked   → +0.15
  Top Traders: 56% SHORT | Crowd: 64% LONG → Smart money bearish → +0.15
  Taker Sell: $8.2M | Buy: $3.1M → Aggressive selling            → +0.10
  Crowd: 64% long → Overleveraged                                → +0.05
  Funding: +0.02% → Longs paying                                 → +0.05

  Total CG modifier: +0.50 (maximum)
  Final Confidence: MODERATE → HIGH ✅
  Result: STRONG TRADE — everything agrees
```

### Example B: NORMAL + CoinGlass DISAGREES (Blocked Trade)

```
Window State:
  VPIN: 0.48 (NORMAL regime)
  Delta: +0.09% (BTC rising)
  Direction: DOWN (contrarian — bet against the rise)
  Base Confidence: MODERATE

CoinGlass Snapshot:
  Liq Short: $1.8M | Liq Long: $0.2M → Shorts getting wrecked   → -0.15
  Top Traders: 58% LONG | Crowd: 55% LONG → Smart money agrees UP → -0.15
  Taker Buy: $7.5M | Sell: $2.1M → Aggressive buying              → -0.10
  Crowd: 55% long → Mild, not extreme                             → +0.00
  Funding: -0.005% → Shorts paying (neutral)                      → +0.00

  Total CG modifier: -0.40
  Final Confidence: MODERATE → LOW ❌
  Result: BLOCKED — CoinGlass says the UP move is genuine, not mean-reversion
```

### Example C: CASCADE + No CoinGlass Data (Backward Compatible)

```
Window State:
  VPIN: 0.71 (CASCADE regime)
  Delta: -0.05%
  Direction: DOWN (momentum)
  Base Confidence: MODERATE

CoinGlass Snapshot: None (feed disconnected or no data)

  Total CG modifier: 0.00
  Final Confidence: MODERATE (unchanged)
  Result: TRADE — works exactly like v4.1 when CG is unavailable
```

---

## 6. The Biology — How CoinGlass Enriches Each Regime

```
┌──────────────────────────────────────────────────────────────────┐
│                   REGIME × COINGLASS INTERACTION                  │
├────────────┬─────────────────────────────────────────────────────┤
│            │                                                     │
│  CALM      │  CoinGlass irrelevant — no trade regardless.        │
│  (<0.45)   │  But we LOG the data for dashboard monitoring.      │
│            │                                                     │
├────────────┼─────────────────────────────────────────────────────┤
│            │                                                     │
│  NORMAL    │  CoinGlass is the QUALITY FILTER.                   │
│ (0.45-0.55)│                                                     │
│            │  We're betting contrarian (against the delta).       │
│            │  CG tells us if this is a REAL mean-reversion       │
│            │  opportunity or if the move is genuine.              │
│            │                                                     │
│            │  IDEAL: Smart money agrees with our contrarian bet, │
│            │  crowd is overleveraged the other way, no liq       │
│            │  cascade happening.                                 │
│            │                                                     │
│            │  Biology: Like checking if the immune system is     │
│            │  functional before assuming a fever will self-       │
│            │  resolve. CG = immune system status check.          │
│            │                                                     │
├────────────┼─────────────────────────────────────────────────────┤
│            │                                                     │
│ TRANSITION │  CoinGlass is the EARLY WARNING SYSTEM.             │
│ (0.55-0.65)│                                                     │
│            │  Something is brewing. CG tells us WHAT.            │
│            │  Are liquidations starting? Which side?             │
│            │  Is smart money repositioning?                      │
│            │                                                     │
│            │  If CG says cascade is forming → we might switch    │
│            │  from contrarian to waiting for full CASCADE.       │
│            │                                                     │
│            │  Biology: Like seeing elevated CRP + specific       │
│            │  biomarkers. CRP alone = "something's wrong."       │
│            │  CRP + specific markers = "it's this disease."     │
│            │                                                     │
├────────────┼─────────────────────────────────────────────────────┤
│            │                                                     │
│  CASCADE   │  CoinGlass is the CONFIRMATION SIGNAL.              │
│  (>=0.65)  │                                                     │
│            │  VPIN says cascade is active. CG tells us:          │
│            │  - ARE liquidations actually happening? (liq data)  │
│            │  - WHICH SIDE is getting liquidated? (direction)    │
│            │  - Is the cascade ACCELERATING? (OI dropping)       │
│            │  - Is smart money riding it? (top trader ratio)     │
│            │                                                     │
│            │  IDEAL: Massive long liqs + falling OI + smart      │
│            │  money short + aggressive taker selling = MAXIMUM   │
│            │  confidence in DOWN momentum.                       │
│            │                                                     │
│            │  Biology: Like having CDK1 (VPIN) confirmed by      │
│            │  observing actual cyclin destruction (OI drop),     │
│            │  substrate depletion rate (liq volume), and         │
│            │  downstream pathway activation (taker aggression). │
│            │                                                     │
└────────────┴─────────────────────────────────────────────────────┘
```

---

## 7. SITREP Format (v5)

```
📋 5-MIN SITREP (🟢 ACTIVE) 📄 PAPER

🏦 Cash: $157.42
📊 Positions: $12.50
💰 Portfolio: $169.92
📈 P&L: +$9.92 (from $160)

✅ Wins: 8 | ❌ Losses: 4
📉 Drawdown: 2.1%

🔬 VPIN: 0.5443 | Vol: LOW_VOL | Trade: NORMAL
📡 CG: Liq $0.0M (L:$0.0/S:$0.0) | Crowd: 63%L | Smart: 53%S | Taker: B$6.7M/S$1.5M
🔗 Binance: ✅ | BTC: $66,847.50
```

---

## 8. Full Configuration (v5 Railway Env Vars)

```
# ── Strategy Parameters ──
FIVE_MIN_VPIN_GATE=0.45
FIVE_MIN_MIN_DELTA_PCT=0.08
FIVE_MIN_CASCADE_MIN_DELTA_PCT=0.03
FIVE_MIN_ENTRY_OFFSET=60
FIVE_MIN_MODE=safe
FIVE_MIN_ENABLED=true
FIVE_MIN_ASSETS=BTC

# ── VPIN Thresholds ──
VPIN_INFORMED_THRESHOLD=0.55
VPIN_CASCADE_THRESHOLD=0.70
VPIN_CASCADE_DIRECTION_THRESHOLD=0.65
VPIN_BUCKET_SIZE_USD=500000

# ── CoinGlass (Standard Plan) ──
COINGLASS_API_KEY=abd0524e...
# Enhanced feed polls every 10s
# 6 endpoints × 6 req/min = 36 req/min (of 300 limit)
# Headroom: 264 req/min spare

# ── Risk Management ──
BET_FRACTION=0.05
MAX_POSITION_USD=120
MAX_OPEN_EXPOSURE_PCT=0.45
DAILY_LOSS_LIMIT_PCT=0.30
STARTING_BANKROLL=160

# ── Trading Mode ──
PAPER_MODE=true
SKIP_DB_CONFIG_SYNC=true
```

---

## 9. API Rate Budget

```
┌────────────────────────────────┬──────────┬──────────────────┐
│ Endpoint                       │ Interval │ Req/min          │
├────────────────────────────────┼──────────┼──────────────────┤
│ OI History (1m)                │ 10s      │ 6                │
│ Liquidation History (1m)       │ 10s      │ 6                │
│ Global L/S Account Ratio (1m)  │ 10s      │ 6                │
│ Top Traders Position (1m)      │ 10s      │ 6                │
│ Taker Buy/Sell Volume (1m)     │ 10s      │ 6                │
│ Funding Rate History (8h)      │ 60s      │ 1                │
├────────────────────────────────┼──────────┼──────────────────┤
│ TOTAL                          │          │ 31 req/min       │
│ Standard Plan Limit            │          │ 300 req/min      │
│ Headroom                       │          │ 269 req/min (90%)│
└────────────────────────────────┴──────────┴──────────────────┘
```

---

## 10. What v5 Trades Look Like

### CASCADE + CoinGlass Confirms (v5 advantage)

```
BTC at $66,800 (window open: $66,850)
Delta: -0.075% | VPIN: 0.78

v4.1 Decision:
  CASCADE regime, delta -0.075% >= 0.03% → TRADE DOWN
  Confidence: MODERATE
  → TRADE (but we don't know HOW confident to be)

v5 Decision:
  CASCADE regime, delta -0.075% >= 0.03% → TRADE DOWN
  Confidence: MODERATE

  CoinGlass Check:
    Liq: $3.2M longs vs $0.4M shorts → confirms DOWN (+0.15)
    Smart money: 54% short → agrees (+0.15)
    Taker: sell $5.1M vs buy $2.3M → confirms (+0.10)
    Crowd: 62% long → overleveraged (+0.05)
    Funding: +0.015% → longs paying (+0.05)
    CG modifier: +0.50

  Final: MODERATE → HIGH
  → STRONG TRADE (CG says this cascade is REAL)
```

### NORMAL + CoinGlass Warns (v5 saves us)

```
BTC at $67,100 (window open: $67,050)
Delta: +0.075% | VPIN: 0.48

v4.1 Decision:
  NORMAL regime, delta +0.075% < 0.08% → SKIP
  (Saved by delta threshold)

But what if delta were 0.09%?

v4.1 Decision:
  NORMAL regime, delta +0.09% >= 0.08% → TRADE DOWN (contrarian)
  Confidence: MODERATE
  → TRADE ← but is this a real mean-reversion?

v5 Decision:
  NORMAL regime, delta +0.09% >= 0.08% → TRADE DOWN (contrarian)
  Confidence: MODERATE

  CoinGlass Check:
    Liq: $0.8M shorts vs $0.1M longs → shorts wrecked (-0.15)
    Smart money: 57% long → agrees with UP (-0.15)
    Taker: buy $6.2M vs sell $1.8M → buying pressure (-0.10)
    CG modifier: -0.40

  Final: MODERATE → LOW
  → SKIP ← CG says this UP move is genuine, don't fight it
```
