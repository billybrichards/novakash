# Novakash Trading System — Complete Overview

**Date:** 2026-04-03 | **Live Strategy:** v4.1 (Regime-Aware) | **Commit:** `b4052f6`

---

## What is Novakash?

Novakash is an automated trading bot for **Polymarket 5-minute BTC Up/Down markets**. Every 5 minutes, Polymarket creates a market: "Will BTC be higher or lower than $X in 5 minutes?" The bot analyzes real-time data and places bets.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         RAILWAY                              │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ ENGINE (engine-develop.up.railway.app)                │   │
│  │                                                       │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐ │   │
│  │  │ Binance WS  │  │ Polymarket  │  │  CoinGlass   │ │   │
│  │  │ (BTC price) │  │ (markets)   │  │  (optional)  │ │   │
│  │  └──────┬──────┘  └──────┬──────┘  └──────┬───────┘ │   │
│  │         │                │                │          │   │
│  │         ▼                ▼                ▼          │   │
│  │  ┌─────────────────────────────────────────────────┐ │   │
│  │  │              ORCHESTRATOR                        │ │   │
│  │  │  - Manages market state                         │ │   │
│  │  │  - Routes data to strategies                    │ │   │
│  │  │  - Heartbeat every ~10s                         │ │   │
│  │  └───────────────────┬─────────────────────────────┘ │   │
│  │                      │                               │   │
│  │                      ▼                               │   │
│  │  ┌─────────────────────────────────────────────────┐ │   │
│  │  │         5-MIN VPIN STRATEGY (v4.1)               │ │   │
│  │  │                                                  │ │   │
│  │  │  1. Receive window signal (T-60s before close)   │ │   │
│  │  │  2. Calculate delta: (BTC_now - open) / open     │ │   │
│  │  │  3. Calculate VPIN (informed flow metric)        │ │   │
│  │  │  4. Classify regime (CALM/NORMAL/TRANS/CASCADE) │ │   │
│  │  │  5. Pick direction based on regime               │ │   │
│  │  │  6. Execute if confidence MODERATE+              │ │   │
│  │  └───────────────────┬─────────────────────────────┘ │   │
│  │                      │                               │   │
│  │                      ▼                               │   │
│  │  ┌─────────────────────────────────────────────────┐ │   │
│  │  │           ORDER MANAGER                          │ │   │
│  │  │  - GTC limit orders only (never FAK/market)     │ │   │
│  │  │  - Fetches token price from Gamma API            │ │   │
│  │  │  - Bumps price +2¢ if no fill after 5s          │ │   │
│  │  │  - Max 3 bump attempts                           │ │   │
│  │  └───────────────────┬─────────────────────────────┘ │   │
│  │                      │                               │   │
│  │                      ▼                               │   │
│  │  ┌─────────────────────────────────────────────────┐ │   │
│  │  │         POLYMARKET CLOB API                      │ │   │
│  │  │  - py-clob-client (Polymarket SDK)              │ │   │
│  │  │  - Signs with POLY_API_KEY                       │ │   │
│  │  │  - Proxy wallet: 0x330ec...b6b                   │ │   │
│  │  └─────────────────────────────────────────────────┘ │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ HUB (hub-develop.up.railway.app)                      │   │
│  │  - REST API for dashboard/trades/config               │   │
│  │  - Telegram bot alerts (@Novakash_bot)                │   │
│  │  - Trade history, P&L tracking                        │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ FRONTEND (frontend-develop.up.railway.app)            │   │
│  │  - React dashboard                                    │   │
│  │  - Live trade feed, P&L charts                        │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ POSTGRES                                              │   │
│  │  - Trades, orders, config, P&L history                │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

---

## What is VPIN?

**Volume-Synchronized Probability of Informed Trading.**

VPIN measures how much of the trading volume is "informed" (i.e., traders who know something the market doesn't yet). Higher VPIN = more one-sided flow = likely a liquidation cascade or whale activity.

**How we calculate it:**
1. Take the last 50 minutes of Binance 1-minute klines
2. Split volume into $500K buckets
3. For each bucket: estimate buy/sell split using close-low/high-low ratio
4. VPIN = average |buy - sell| / total across buckets

**Thresholds:**
| VPIN | Regime | Meaning |
|------|--------|---------|
| < 0.45 | CALM | No informed flow → skip |
| 0.45 - 0.55 | NORMAL | Some informed flow → trade with higher delta bar |
| 0.55 - 0.65 | TRANSITION | Significant informed flow → contrarian, high delta bar |
| >= 0.65 | CASCADE | Extreme informed flow → momentum, low delta bar |

---

## v4.1 Strategy (Currently Live)

### Signal Evaluation at T-60 Seconds

When a 5-minute window is 60 seconds from closing:

1. **VPIN Gate:** If VPIN < 0.45 → SKIP (no informed flow)

2. **Calculate Delta:** `delta = (BTC_now - BTC_open) / BTC_open × 100`

3. **Regime Classification + Direction:**

| VPIN | Regime | Direction | Min Delta | Logic |
|------|--------|-----------|-----------|-------|
| >= 0.65 | CASCADE | **MOMENTUM** | 0.03% (scaled by VPIN) | Ride the cascade |
| 0.55-0.65 | TRANSITION | **CONTRARIAN** | 0.12% | Mean-revert with high bar |
| 0.45-0.55 | NORMAL | **CONTRARIAN** | 0.08% | Mean-revert standard |
| < 0.45 | CALM | SKIP | — | No edge |

4. **CASCADE VPIN Scaling:**
   - VPIN 0.65-0.75: min delta = 0.03%
   - VPIN 0.75-0.85: min delta = 0.015%
   - VPIN 0.85+: min delta = 0.005% ("mega cascade, just go")

5. **Confidence Check:**
   - |delta| > 0.10% → HIGH
   - |delta| > 0.02% → MODERATE
   - |delta| > 0.005% → LOW
   - Only MODERATE and HIGH trade

6. **Execute:** GTC limit order at best Gamma API price, bump +2¢ if no fill after 5s

---

## 30-Day Data Findings (8,646 markets)

### What the data actually shows:

**Within-window (T-60 delta → oracle):**
- Momentum correct 95.8% at d>=0.08% (but token pricing may erase edge)

**Between-window (this window → next):**
- **CALM:** 48.8% reversal — random
- **NORMAL:** 50.4% reversal — random
- **TRANSITION:** 48.7% reversal — random (NOT 57.8% from 7-day sample)
- **CASCADE:** 39.1% reversal / **60.9% persistence** — real edge

### Critical findings:
1. **No contrarian between-window edge at ANY regime except CASCADE** — all ~50%
2. **CASCADE momentum persists across windows** — 60.9% at d>=0.08%
3. **Within-window momentum works directionally** — but execution profitability unclear
4. **7-day data was misleading** — TRANSITION 57.8% was sampling noise

---

## Key Files

| File | Purpose |
|------|---------|
| `engine/strategies/five_min_vpin.py` | Main strategy (v4.1) |
| `engine/config/runtime_config.py` | Live-reloadable config |
| `engine/execution/order_manager.py` | GTC limit order execution |
| `engine/execution/polymarket_client.py` | CLOB API wrapper |
| `engine/signals/vpin.py` | VPIN calculator |
| `engine/signals/window_evaluator.py` | Continuous evaluator (disabled) |
| `engine/data/feeds/polymarket_5min.py` | Market discovery feed |
| `engine/execution/redeemer.py` | Position redemption (Builder Relayer) |

---

## Environment Variables (Railway)

### Strategy
```env
FIVE_MIN_ENABLED=true
FIVE_MIN_ASSETS=BTC
FIVE_MIN_MODE=safe
FIVE_MIN_ENTRY_OFFSET=60
FIVE_MIN_VPIN_GATE=0.45
FIVE_MIN_MIN_DELTA_PCT=0.08
FIVE_MIN_CASCADE_MIN_DELTA_PCT=0.03
```

### VPIN
```env
VPIN_INFORMED_THRESHOLD=0.55
VPIN_CASCADE_DIRECTION_THRESHOLD=0.65
VPIN_CASCADE_THRESHOLD=0.70
VPIN_BUCKET_SIZE_USD=500000
VPIN_LOOKBACK_BUCKETS=50
```

### Risk
```env
BET_FRACTION=0.05
STARTING_BANKROLL=160
MAX_POSITION_USD=120
MAX_OPEN_EXPOSURE_PCT=0.45
DAILY_LOSS_LIMIT_PCT=0.30
MIN_BET_USD=2.0
```

### Execution
```env
LIVE_TRADING_ENABLED=true
PAPER_MODE=false
SKIP_DB_CONFIG_SYNC=true
```

### API Keys
```env
POLY_API_KEY=e7dfe7e5-9254-fcfb-5d08-42123123f5cc
POLY_FUNDER_ADDRESS=0x330ec13157b50057843fea262fd509162e710b6b
POLYGON_RPC_URL=https://polygon-bor-rpc.publicnode.com
```

---

## Deployment

```
Git push to develop → Railway auto-deploys
```

- **NEVER** use `railway up` (gets overwritten by git deploy)
- **NEVER** change env vars without approval
- `SKIP_DB_CONFIG_SYNC=true` means Railway env vars are source of truth

### Railway Services
| Service | URL |
|---------|-----|
| Engine | engine-develop.up.railway.app |
| Hub | hub-develop-0433.up.railway.app |
| Frontend | frontend-develop-2bdf.up.railway.app |

---

## Resolution

Polymarket uses **Chainlink Data Streams** as oracle for BTC price.

- Oracle != Binance. Agreement rate: ~96.4% over 30 days
- **ALWAYS resolve from Polymarket oracle** — the gamma-api `outcomePrices` field
- **NEVER trust Binance-only** — 54% disagreement during volatile windows

---

## Hard Rules

1. **GTC limit orders ONLY** — FAK/market orders caused $258 loss
2. **Polymarket oracle** — never Binance-only resolution
3. **Git deploy** — never `railway up` CLI
4. **Billy approves** before changing BET_FRACTION or going live
5. **SKIP_DB_CONFIG_SYNC=true** — env vars override database

---

## Version History

| Version | What | Status |
|---------|------|--------|
| v1-v3 | Early iterations, various bugs | Retired |
| v4.0 | Regime-aware: contrarian in normal, momentum in cascade | Superseded |
| **v4.1** | Cascade VPIN scaling (0.03% → 0.015% → 0.005%) | **LIVE** |
| v4.2 | All-momentum (reverted — based on incomplete analysis) | Reverted |
| v4.3 | 30-day data-driven: CASCADE next-window momentum | **Proposed** |

---

## What's Next (v4.3 Proposal)

Based on 30-day data:
1. **Within-window:** Always momentum (95.8% directional WR) — need execution study
2. **Between-window CASCADE:** Momentum continues (60.9%) — add next-window logic
3. **Between-window everything else:** No trade (no edge)
4. Drop contrarian in NORMAL/TRANSITION (30-day data shows ~50% = random)

---

**End of Document**
