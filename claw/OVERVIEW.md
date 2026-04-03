# Novakash — Project Overview

## What Is This?

A **BTC prediction market trading system** deployed on Railway. Trades Polymarket's 5-minute BTC Up/Down binary markets using technical signals.

**GitHub:** github.com/billybrichards/novakash  
**Branch:** develop (auto-deploys to Railway)  
**Owner:** Billy Richards (@brb1480)

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Frontend (React/Vite/Tailwind)                             │
│  https://frontend-develop-2bdf.up.railway.app               │
│  7 real-time canvas charts, trade history, config UI        │
└────────────────────────┬─────────────────────────────────────┘
                         │
┌────────────────────────┴─────────────────────────────────────┐
│  Hub (FastAPI + PostgreSQL)                                  │
│  https://hub-develop-0433.up.railway.app                     │
│  REST API, WebSocket feed, JWT auth                         │
└────────────────────────┬─────────────────────────────────────┘
                         │
┌────────────────────────┴─────────────────────────────────────┐
│  Engine (Python 3.12 / asyncio)                              │
│  https://engine-develop.up.railway.app                       │
│                                                              │
│  Data Feeds:                                                │
│  - Binance WS (price, depth, liquidations)                  │
│  - CoinGlass API (OI, liquidations)                         │
│  - Polymarket WS (order books)                              │
│  - Chainlink RPC (Polygon oracle)                           │
│                                                              │
│  Signals:                                                   │
│  - VPIN Calculator (informed flow)                          │
│  - Window Evaluator (delta + VPIN combined)                 │
│  - Cascade Detector (FSM: IDLE→CASCADE→BET)                 │
│  - Arb Scanner (sub-$1 opportunities)                       │
│  - Regime Classifier (volatility states)                    │
│                                                              │
│  Strategies:                                                │
│  - 5-min VPIN (main — T-60s single-shot evaluation)         │
│  - 15-min VPIN Cascade                                      │
│  - Sub-dollar Arb                                           │
│                                                              │
│  Execution:                                                 │
│  - Polymarket Client (CLOB + Gamma API)                     │
│  - Order Manager (lifecycle, PnL tracking, DB persistence)  │
│  - Risk Manager (Kelly sizing, 7 risk gates, kill switch)   │
│  - Position Redeemer (on-chain auto-redeem via CTF)         │
│                                                              │
│  Alerts:                                                    │
│  - Telegram bot (@Novakash_bot) → Billy's chat              │
└──────────────────────────────────────────────────────────────┘
```

---

## Current Status (2026-04-03)

| Setting | Value |
|---------|-------|
| PAPER_MODE | true |
| LIVE_TRADING_ENABLED | false |
| PAPER_BANKROLL | $160 |
| BET_FRACTION | 0.10 (10%) |
| Max Stake | $16 per trade |
| 5-min Strategy | T-60s single-shot (safe mode) |
| 15-min Strategy | Enabled |
| Wallet | 0x330ec13157b50057843fea262fd509162e710b6b |
| USDC Balance | ~$129.60 |

---

## File Structure

```
novakash/
├── engine/              # Python trading engine
│   ├── config/          # settings.py, constants.py, runtime_config.py
│   ├── data/feeds/      # binance_ws, polymarket_ws, coinglass, chainlink
│   ├── signals/         # vpin, cascade_detector, arb_scanner, regime_classifier
│   ├── execution/       # polymarket_client, order_manager, risk_manager, redeemer
│   ├── strategies/      # five_min_vpin, vpin_cascade, sub_dollar_arb, orchestrator
│   ├── persistence/     # db_client
│   ├── alerts/          # telegram
│   └── tests/
├── hub/                 # FastAPI backend
│   ├── auth/            # JWT, middleware, routes
│   ├── api/             # dashboard, trades, signals, pnl, system, config, paper
│   ├── ws/              # WebSocket live feed
│   ├── db/              # models, schema, migrations
│   └── services/
├── frontend/            # React dashboard
│   └── src/pages/       # Dashboard, Positions, PnL, Risk, Config, etc.
├── docs/                # Analysis, postmortems, PDFs
├── claw/                # THIS FOLDER — agent documentation
└── scripts/
```

---

## Key Results

**Morning Strategy (Working):** +$93, 67% win rate  
**Afternoon Changes (Broke):** -$258 (reverted)  
**Net P&L:** -$165 (from $209 deposit)  
**Current Wallet:** ~$44 USDC + $129 in positions
