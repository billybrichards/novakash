# Novakash Strategy v4.1 — Regime-Aware Direction + Cascade Delta

**Date:** 2026-04-03  
**Status:** LIVE (deployed on Railway develop)  
**Commit:** `27e078b`

---

## 🎯 What Changed from v4.0 → v4.1

### v4.0 (Previous)
- Regime-aware direction (momentum vs contrarian based on VPIN)
- Same min delta (0.08%) for ALL regimes
- Delta check happened BEFORE regime check → blocked cascade trades with small deltas

### v4.1 (Current — LIVE)
- **Per-regime delta thresholds** — cascade gets a lower bar
- Delta check moved INSIDE each regime branch
- New env var: `FIVE_MIN_CASCADE_MIN_DELTA_PCT=0.03`

**Billy's insight:** In a cascade, VPIN IS the signal. A small delta still confirms direction when informed flow is extreme. Like how a small temperature rise matters more in a septic patient.

---

## 🏗️ Architecture — Signal Evaluation Flow

```
┌─────────────────────────────────────────────────────────────┐
│                    EVERY 5-MINUTE WINDOW                    │
│                                                             │
│  Step 1: VPIN Gate                                         │
│  ┌─────────────────────────────────────┐                   │
│  │ VPIN < 0.45 → SKIP (no informed flow)│                  │
│  └─────────────────┬───────────────────┘                   │
│                    │ VPIN >= 0.45                           │
│                    ▼                                        │
│  Step 2: Regime Classification                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │                                                     │   │
│  │  VPIN >= 0.65 ──────► CASCADE REGIME               │   │
│  │  │                     Direction: MOMENTUM          │   │
│  │  │                     Min Delta: 0.03%             │   │
│  │  │                     "Ride the trend"             │   │
│  │  │                                                  │   │
│  │  VPIN 0.55-0.65 ───► TRANSITION REGIME             │   │
│  │  │                     Direction: CONTRARIAN         │   │
│  │  │                     Min Delta: 0.12%             │   │
│  │  │                     "Careful mean-reversion"     │   │
│  │  │                                                  │   │
│  │  VPIN < 0.55 ──────► NORMAL REGIME                 │   │
│  │                        Direction: CONTRARIAN         │   │
│  │                        Min Delta: 0.08%             │   │
│  │                        "Standard mean-reversion"    │   │
│  │                                                     │   │
│  └─────────────────────┬───────────────────────────────┘   │
│                        │                                    │
│                        ▼                                    │
│  Step 3: Confidence Check                                  │
│  ┌─────────────────────────────────────┐                   │
│  │ NONE / LOW → SKIP                   │                   │
│  │ MODERATE / HIGH → TRADE             │                   │
│  └─────────────────────┬───────────────┘                   │
│                        │                                    │
│                        ▼                                    │
│  Step 4: Execute                                           │
│  ┌─────────────────────────────────────┐                   │
│  │ GTC limit order at Gamma API price  │                   │
│  │ +2¢ retry if no fill after 5s       │                   │
│  │ BET_FRACTION = 5% of bankroll       │                   │
│  └─────────────────────────────────────┘                   │
│                                                             │
│  Step 5: Resolution                                        │
│  ┌─────────────────────────────────────┐                   │
│  │ Query Polymarket oracle (Chainlink) │                   │
│  │ Record WIN/LOSS from oracle truth   │                   │
│  │ NEVER trust Binance-only resolution │                   │
│  └─────────────────────────────────────┘                   │
└─────────────────────────────────────────────────────────────┘
```

---

## 📊 Regime Decision Matrix

```
┌──────────────────────────────────────────────────────────────────┐
│                     REGIME DECISION MATRIX                       │
├────────────┬───────────┬──────────┬───────────┬─────────────────┤
│ VPIN Range │ Regime    │Direction │ Min Delta │ Rationale       │
├────────────┼───────────┼──────────┼───────────┼─────────────────┤
│ < 0.45     │ CALM      │ SKIP     │ —         │ No edge, noise  │
│ 0.45-0.55  │ NORMAL    │ CONTRA   │ 0.08%     │ Mean-reversion  │
│ 0.55-0.65  │ TRANSITION│ CONTRA   │ 0.12%     │ Cautious revert │
│ >= 0.65    │ CASCADE   │ MOMENTUM │ 0.03%     │ Ride the trend  │
└────────────┴───────────┴──────────┴───────────┴─────────────────┘

                    VPIN Scale
    0.0          0.45       0.55    0.65        1.0
    ├─────────────┼──────────┼───────┼───────────┤
    │   CALM      │  NORMAL  │TRANS  │  CASCADE  │
    │   (skip)    │ ◄contra  │◄caut  │ momentum► │
    │             │  δ≥0.08% │δ≥0.12%│  δ≥0.03%  │
    └─────────────┴──────────┴───────┴───────────┘
```

---

## 🧬 The Biology Behind Each Regime

### NORMAL (VPIN 0.45-0.55) — Le Chatelier's Principle
Like a buffered equilibrium. BTC overreacts to news/noise, then the "buffer" (market makers, mean-reversion traders) pushes price back. The bigger the perturbation (delta), the stronger the restoring force.

**Trade:** CONTRARIAN — bet against the move.

### TRANSITION (VPIN 0.55-0.65) — Early-Stage Detection
Like elevated biomarkers (PSA, CRP) — something's happening but it might be benign. Need stronger evidence (bigger delta) before acting.

**Trade:** CONTRARIAN but only with delta >= 0.12%.

### CASCADE (VPIN >= 0.65) — CDK1/Cyclin Bistable Switch
The system has crossed the activation threshold. Positive feedback loop: liquidations → price drop → more liquidations. Like CDK1 activating Cdc25 which activates more CDK1.

**The key insight:** In this regime, even a small delta (0.03%) confirms the direction because the VPIN itself is the signal. Like how a 0.5°C temperature rise is alarming in a septic patient but meaningless in a healthy one.

**Trade:** MOMENTUM — ride the cascade until OI depletes (substrate exhaustion).

```
    CDK1/Cyclin Cascade          Market Cascade
    ─────────────────          ──────────────
    Cyclin accumulates    →    OI builds up (leveraged positions)
    CDK1 crosses threshold →   Liquidation cascade triggers
    CDK1→Cdc25→CDK1      →    Liquidation→price drop→more liq
    APC destroys cyclin   →    OI depletes (no more fuel)
    CDK1 falls            →    VPIN drops, price bounces
    Cell returns to G1    →    Market returns to NORMAL
```

---

## ⚙️ Full Configuration (Railway Env Vars)

### Strategy Parameters
```env
FIVE_MIN_VPIN_GATE=0.45              # Below this = no trade
FIVE_MIN_MIN_DELTA_PCT=0.08          # Normal regime min delta
FIVE_MIN_CASCADE_MIN_DELTA_PCT=0.03  # CASCADE regime min delta (NEW in v4.1)
FIVE_MIN_ENTRY_OFFSET=60             # T-60s evaluation
FIVE_MIN_MODE=safe                   # Trading mode
FIVE_MIN_ENABLED=true
FIVE_MIN_ASSETS=BTC
```

### VPIN Thresholds
```env
VPIN_INFORMED_THRESHOLD=0.55         # Normal → Transition boundary
VPIN_CASCADE_THRESHOLD=0.70          # General cascade alert
VPIN_CASCADE_DIRECTION_THRESHOLD=0.65 # Contrarian → Momentum switch
VPIN_BUCKET_SIZE_USD=500000          # $500K buckets (was $50K)
```

### Risk Management
```env
BET_FRACTION=0.05                    # 5% per trade (live)
MAX_POSITION_USD=120                 # Max single position
MAX_OPEN_EXPOSURE_PCT=0.45           # 45% max open
DAILY_LOSS_LIMIT_PCT=0.30            # 30% daily loss halt
STARTING_BANKROLL=160
```

### Trading Mode
```env
LIVE_TRADING_ENABLED=true
PAPER_MODE=false
SKIP_DB_CONFIG_SYNC=true             # Env vars override DB config
```

---

## 📈 Evidence Summary

### 4-Day Backtest (1,152 markets, Mar 31 — Apr 3)
| Strategy | Win Rate | Daily EV (@$5 flat) |
|----------|----------|---------------------|
| Contrarian (d>=0.08%, T-15) | 57.5% | $86/day |
| Contrarian (d>=0.09%, T-30) | 57.8% | $76/day |
| Contrarian (d>=0.10%, T-60) | 56.7% | $55/day |

### Live Cascade Data (Apr 3, VPIN 0.75-0.98)
| Strategy | Trades | Wins | Win Rate |
|----------|--------|------|----------|
| Momentum (live paper) | 6 | 5 | **83%** |

### Key Research
- **De Nicola (2021):** 5-min BTC autocorrelation = -0.1016 (p < 10^-200) → mean-reversion
- **Easley et al. (2024):** Crypto VPIN averages 0.469 (2x traditional markets)
- **Live observation:** Momentum works in cascade (VPIN > 0.65), contrarian works in normal

---

## 🔄 What v4.1 Trades Look Like

### CASCADE Trade (VPIN >= 0.65)
```
BTC at $66,800 (window open: $66,850)
Delta: -0.075% (BTC dropped $50 from window open)
VPIN: 0.78 (extreme informed selling)

v4.0: SKIP (delta -0.075% < min 0.08%)  ← MISSED TRADE
v4.1: CASCADE regime, delta -0.075% >= 0.03% → TRADE
      Direction: DOWN (momentum — ride the cascade)
      Bet: 5% of bankroll
```

### NORMAL Trade (VPIN < 0.55)
```
BTC at $67,100 (window open: $67,000)
Delta: +0.149% (BTC up $100 from window open)
VPIN: 0.48 (normal informed flow)

v4.0 & v4.1: NORMAL regime, delta +0.149% >= 0.08% → TRADE
             Direction: DOWN (contrarian — bet on mean-reversion)
             Bet: 5% of bankroll
```

### TRANSITION Trade (VPIN 0.55-0.65)
```
BTC at $66,600 (window open: $66,500)
Delta: +0.150% (BTC up $100)
VPIN: 0.58 (elevated informed flow)

v4.0 & v4.1: TRANSITION regime, delta +0.150% >= 0.12% → TRADE
             Direction: DOWN (contrarian but cautious)
             Bet: 5% of bankroll
```

---

## 🚨 Hard Rules (Unchanged)

1. **ALWAYS GTC limit orders** — never FAK/market orders
2. **ALWAYS resolve from Polymarket oracle** — never Binance-only
3. **ALWAYS push to git** — never `railway up` (gets overwritten)
4. **NEVER change BET_FRACTION** without Billy's approval
5. **NEVER go above 5%** bet fraction in live mode
6. **SKIP_DB_CONFIG_SYNC=true** — env vars are source of truth

---

## 📁 Key Files

| File | Purpose |
|------|---------|
| `engine/strategies/five_min_vpin.py` | Main strategy — regime-aware v4.1 |
| `engine/config/runtime_config.py` | All configurable parameters |
| `engine/execution/order_manager.py` | Polymarket oracle resolution |
| `engine/execution/risk_manager.py` | Position limits, bankroll |
| `engine/execution/redeemer.py` | On-chain redemption (use relayer instead) |
| `claw/STRATEGY-v4.1.md` | THIS DOCUMENT |

---

## 🔮 Future Improvements (v4.2+)

1. **Delta-tiered bet sizing:** 3% base, 5% at d>0.12%, 8% at d>0.15%
2. **Cascade exhaustion detection:** Switch from momentum→contrarian when OI stabilises
3. **T-15 fallback:** If T-60 skips, re-evaluate at T-30 and T-15
4. **VPIN from real tick data:** Currently using Binance aggTrade (exact buy/sell classification)
5. **Post-cascade bounce:** Boost stake to 10% when VPIN drops from >0.70 back below 0.55
