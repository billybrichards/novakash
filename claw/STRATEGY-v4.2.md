# Novakash Strategy v4.2 — All-Momentum Architecture

**Date:** 2026-04-03 | **Status:** LIVE | **Commit:** `4cf801b`

---

## 🚨 What Changed (v4.1 → v4.2)

### The Bug We Fixed

v4.1 used **CONTRARIAN** direction in NORMAL and TRANSITION regimes. This was based on a misapplication of De Nicola (2021)'s mean-reversion finding.

**De Nicola's finding:** Negative autocorrelation (-0.1016) between CONSECUTIVE 5-min windows. If window N goes UP, window N+1 tends to go DOWN.

**What v4.1 did:** Bet AGAINST the delta WITHIN THE SAME WINDOW. At T-60, if BTC is up → bet DOWN.

**The problem:** These are completely different things. Within a window, 60 seconds isn't enough for a 0.08% move to reverse. The delta at T-60 predicts the oracle outcome 97%+ of the time.

### 7-Day Proof (2,016 Polymarket-Oracle-Resolved Markets)

```
WITHIN-WINDOW (our trade — T-60 delta vs oracle):
  d>=0.03%: Momentum 89.5%   Contrarian 10.5%
  d>=0.05%: Momentum 93.5%   Contrarian  6.5%
  d>=0.08%: Momentum 97.1%   Contrarian  2.9%
  d>=0.10%: Momentum 98.1%   Contrarian  1.9%
  d>=0.15%: Momentum 99.3%   Contrarian  0.7%

BETWEEN-WINDOWS (De Nicola's actual effect):
  d>=0.08%: Next reverses 52.8%  (small edge, different strategy)
  d>=0.10%: Next reverses 53.5%
  d>=0.15%: Next reverses 54.7%
  CASCADE:  Next PERSISTS 57.1%  (momentum continues in cascade!)
```

---

## 🏗️ v4.2 Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                   EVERY 5-MINUTE WINDOW                     │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ Step 1: VPIN GATE                                     │  │
│  │ VPIN < 0.45 → SKIP (no informed flow, no edge)        │  │
│  └──────────────────────┬────────────────────────────────┘  │
│                         │ VPIN >= 0.45                      │
│                         ▼                                   │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ Step 2: REGIME CLASSIFICATION                         │  │
│  │                                                       │  │
│  │  VPIN >= 0.65 ───► CASCADE                            │  │
│  │  │                  Min Delta: 0.03%                   │  │
│  │  │                  (0.015% if VPIN>0.75)              │  │
│  │  │                  (0.005% if VPIN>0.85)              │  │
│  │  │                                                    │  │
│  │  VPIN 0.55-0.65 ─► TRANSITION                         │  │
│  │  │                  Min Delta: 0.05%                   │  │
│  │  │                                                    │  │
│  │  VPIN 0.45-0.55 ─► NORMAL                             │  │
│  │                     Min Delta: 0.08%                   │  │
│  └──────────────────────┬────────────────────────────────┘  │
│                         │                                   │
│                         ▼                                   │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ Step 3: DIRECTION = ALWAYS MOMENTUM                   │  │
│  │                                                       │  │
│  │  Delta > 0 (BTC up from window open)  → Bet UP        │  │
│  │  Delta < 0 (BTC down from window open) → Bet DOWN     │  │
│  │                                                       │  │
│  │  ⚠️ NEVER contrarian. Momentum is 97%+ correct        │  │
│  │     within the same window at d>=0.08%.               │  │
│  └──────────────────────┬────────────────────────────────┘  │
│                         │                                   │
│                         ▼                                   │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ Step 4: CONFIDENCE (affects sizing, NOT direction)     │  │
│  │                                                       │  │
│  │  d >= 0.15%              → HIGH                       │  │
│  │  d >= 0.08%              → MODERATE                   │  │
│  │  d >= 0.03% + VPIN>=0.55 → MODERATE                   │  │
│  │  d >= 0.03%              → LOW (blocked)              │  │
│  │  d <  0.03%              → NONE (blocked)             │  │
│  └──────────────────────┬────────────────────────────────┘  │
│                         │                                   │
│                         ▼                                   │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ Step 5: EXECUTE                                       │  │
│  │                                                       │  │
│  │  1. Fetch fresh token price from Gamma API            │  │
│  │  2. Place GTC limit order (maker = zero fees)         │  │
│  │  3. If no fill after 5s → bump price +2¢, retry       │  │
│  │  4. Stake = BET_FRACTION (5%) × bankroll              │  │
│  └──────────────────────┬────────────────────────────────┘  │
│                         │                                   │
│                         ▼                                   │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ Step 6: RESOLUTION                                    │  │
│  │                                                       │  │
│  │  Query Polymarket oracle (Chainlink Data Streams)     │  │
│  │  Record WIN/LOSS based on oracle truth                │  │
│  │  NEVER trust Binance-only resolution                  │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

---

## 📊 Regime Decision Matrix

```
┌──────────┬───────────┬───────────┬───────────┬────────────────────────┐
│ VPIN     │ Regime    │ Direction │ Min Delta │ Why                    │
├──────────┼───────────┼───────────┼───────────┼────────────────────────┤
│ < 0.45   │ CALM      │ SKIP      │ —         │ No informed flow       │
│ 0.45-0.55│ NORMAL    │ MOMENTUM  │ 0.08%     │ Need strong signal     │
│ 0.55-0.65│ TRANSITION│ MOMENTUM  │ 0.05%     │ Informed flow confirms │
│ 0.65-0.75│ CASCADE   │ MOMENTUM  │ 0.03%     │ VPIN IS the signal     │
│ 0.75-0.85│ CASCADE+  │ MOMENTUM  │ 0.015%    │ Strong cascade         │
│ > 0.85   │ MEGA      │ MOMENTUM  │ 0.005%    │ Just go                │
└──────────┴───────────┴───────────┴───────────┴────────────────────────┘
```

**Key insight:** Higher VPIN = lower delta needed. When informed flow is extreme (VPIN > 0.85), even a tiny delta confirms direction because the VPIN itself is the signal. Like how a 0.5°C temperature rise is alarming in a septic patient but meaningless in a healthy one.

---

## ⚙️ Railway Environment Variables

```env
# Strategy
FIVE_MIN_VPIN_GATE=0.45              # CALM below this
FIVE_MIN_MIN_DELTA_PCT=0.08          # NORMAL regime
FIVE_MIN_CASCADE_MIN_DELTA_PCT=0.03  # CASCADE regime
FIVE_MIN_ENTRY_OFFSET=60             # T-60s evaluation
FIVE_MIN_MODE=safe
FIVE_MIN_ENABLED=true
FIVE_MIN_ASSETS=BTC

# VPIN
VPIN_INFORMED_THRESHOLD=0.55         # NORMAL → TRANSITION
VPIN_CASCADE_DIRECTION_THRESHOLD=0.65 # → CASCADE
VPIN_CASCADE_THRESHOLD=0.70          # General cascade alert
VPIN_BUCKET_SIZE_USD=500000          # $500K volume buckets

# Risk
BET_FRACTION=0.05                    # 5% per trade
MAX_POSITION_USD=120
MAX_OPEN_EXPOSURE_PCT=0.45
DAILY_LOSS_LIMIT_PCT=0.30
STARTING_BANKROLL=160

# Mode
LIVE_TRADING_ENABLED=true
PAPER_MODE=false
SKIP_DB_CONFIG_SYNC=true
```

---

## 🔄 What v4.2 Trades Look Like

### Example 1: NORMAL Regime
```
Window: BTC $66,900 → evaluating at T-60s
BTC at T-60: $66,980 (+0.12% from open)
VPIN: 0.48 (normal informed flow)
Regime: NORMAL (0.12% >= 0.08% threshold)
Direction: UP (momentum — follow the delta)
Confidence: MODERATE
→ Place GTC BUY YES at market price
→ Oracle says UP → WIN ✅
```

### Example 2: CASCADE Regime
```
Window: BTC $66,500 → evaluating at T-60s
BTC at T-60: $66,470 (-0.045% from open)
VPIN: 0.78 (extreme informed selling)
Regime: CASCADE (min delta = 0.015% at VPIN 0.78)
Direction: DOWN (momentum — ride the cascade)
Confidence: MODERATE (d>=0.03% + VPIN>=0.55)
→ Place GTC BUY NO at market price
→ Oracle says DOWN → WIN ✅
```

### Example 3: SKIP (No Trade)
```
Window: BTC $66,800 → evaluating at T-60s
BTC at T-60: $66,810 (+0.015% from open)
VPIN: 0.42 (below gate)
→ SKIP (VPIN < 0.45 = no informed flow)
```

---

## 📈 Expected Performance

Based on 7-day data (2,016 markets):

| Delta Threshold | Markets | Momentum WR |
|-----------------|---------|-------------|
| >= 0.03% | 1,434 | 89.5% |
| >= 0.05% | 1,125 | 93.5% |
| >= 0.08% | 714 | 97.1% |
| >= 0.10% | 533 | 98.1% |
| >= 0.15% | 298 | 99.3% |

**Real-world WR will be lower** because:
1. Token pricing already reflects the move (cheaper edge at higher delta)
2. GTC limit orders may not fill if price moves
3. Oracle timing differences (Chainlink vs Binance)
4. But should still be dramatically above 50%

---

## 🔮 Future: v5 — Next-Window Contrarian (Separate Strategy)

De Nicola's mean-reversion IS real — just between windows, not within:

```
After d>=0.15% window: 54.7% chance next window reverses
After d>=0.10% window: 53.5% chance next window reverses
CASCADE regime: next window PERSISTS 57.1% (don't contrarian here!)
```

A v5 strategy could place a SECOND trade on the NEXT window based on the current window's outcome. This is additive to v4.2 — doesn't replace it.

---

## 🚨 Hard Rules

1. **Direction is ALWAYS MOMENTUM** — never contrarian within a window
2. **ALWAYS GTC limit orders** — never FAK/market orders
3. **ALWAYS resolve from Polymarket oracle** — never Binance-only
4. **ALWAYS push to git** — never `railway up` (gets overwritten)
5. **SKIP_DB_CONFIG_SYNC=true** — env vars are source of truth
6. **Never change BET_FRACTION** without Billy's approval
