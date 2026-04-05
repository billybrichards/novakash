# Architecture

## Overview

Novakash is a distributed trading system split across three hosting environments:

1. **Montreal Engine** — the core trading process (AWS EC2, ca-central-1)
2. **Railway** — hub API, frontend, data-collector, PostgreSQL database
3. **TimesFM Service** — AI forecast microservice (AWS EC2, ca-central-1)

These are connected via public internet; the engine writes directly to the Railway PostgreSQL database, and reads forecasts from the TimesFM HTTP API.

---

## Montreal Engine

The heart of the system. Runs continuously as the `novakash` user on a dedicated server.

**Path:** `/home/novakash/novakash/engine`  
**Language:** Python 3.12, `asyncio`-based  
**Entry point:** `engine/main.py`

### Internal Modules

```
engine/
├── main.py                  # Startup, orchestrator wiring
├── config/
│   ├── settings.py          # Pydantic Settings (env vars)
│   ├── constants.py         # Numerical parameters (VPIN thresholds, risk limits)
│   └── runtime_config.py    # Live-overridable config (from DB)
├── data/
│   ├── aggregator.py        # Combines all feeds into MarketState
│   └── feeds/
│       ├── binance_ws.py    # Binance aggTrade + depth + forceOrder WebSocket
│       ├── coinglass_api.py # CoinGlass OI, liquidations (polling)
│       ├── coinglass_enhanced.py  # Enriched CG feed (long/short ratio, funding)
│       ├── polymarket_5min.py     # Polymarket 5m window state machine
│       ├── polymarket_ws.py       # Polymarket CLOB order book WebSocket
│       └── chainlink_rpc.py       # Chainlink oracle price via Polygon RPC
├── signals/
│   ├── vpin.py              # VPIN calculator (USD-bucket method)
│   ├── twap_delta.py        # TWAP tracker for directional momentum
│   ├── timesfm_client.py    # HTTP client for TimesFM forecast service
│   ├── window_evaluator.py  # Combines all signals per window
│   ├── cascade_detector.py  # FSM: IDLE → CASCADE → BET → COOLDOWN
│   ├── regime_classifier.py # Vol regime: LOW/NORMAL/HIGH/CASCADE
│   └── arb_scanner.py       # Sub-$1 YES+NO arbitrage detection
├── strategies/
│   ├── orchestrator.py      # Strategy runner, wires everything together
│   ├── five_min_vpin.py     # ⭐ Primary strategy (5m Up/Down)
│   ├── vpin_cascade.py      # VPIN cascade directional strategy (inactive)
│   ├── sub_dollar_arb.py    # Sub-$1 arb strategy (inactive)
│   ├── timesfm_only.py      # Pure TimesFM strategy (inactive)
│   └── timesfm_multi_entry.py # Multi-entry TimesFM (inactive)
├── execution/
│   ├── order_manager.py     # Order lifecycle, fill tracking, PnL
│   ├── polymarket_client.py # Polymarket CLOB execution (GTC limit orders)
│   ├── opinion_client.py    # Opinion exchange client (inactive)
│   ├── risk_manager.py      # Kelly sizing, drawdown kill switch
│   └── redeemer.py          # Auto-redeem winning positions
├── evaluation/
│   └── claude_evaluator.py  # Claude Opus 4.6 trade analysis + Telegram alerts
├── persistence/
│   ├── db_client.py         # Async PostgreSQL writes (asyncpg)
│   └── tick_recorder.py     # Records all ticks to DB tables
├── alerts/
│   ├── telegram.py          # Telegram bot alerts (trades, errors)
│   ├── telegram_v2.py       # Enhanced Telegram with charts
│   ├── chart_generator.py   # Matplotlib chart generation
│   └── window_chart.py      # Per-window signal visualisation
└── polymarket_browser/      # Playwright browser automation (legacy)
```

### Data Flow

```
Binance WS ──► aggTrade events ──► VPIN Calculator ──► VPINSignal
                                                          │
CoinGlass API ──► OI, liquidations, L/S ratio            │
                                                          ▼
Polymarket Gamma ──► window open/close prices ──► Window Evaluator
                                                          │
TimesFM Service ──► direction + confidence                │
                                                          ▼
                                               Signal Decision (T-60s)
                                                          │
                                          ┌───────────────┴──────────────┐
                                          │    Risk Manager approval      │
                                          └───────────────┬──────────────┘
                                                          │
                                                 Order Manager
                                                          │
                                             Polymarket CLOB (GTC order)
                                                          │
                                                   DB write (trades)
                                                          │
                                            Claude Evaluator → Telegram
```

---

## Railway Services

Four services run on Railway, sharing a single PostgreSQL instance.

### Hub (FastAPI)

REST API and WebSocket server. The frontend and engine both interact with it.

**Key routes:**
- `POST /auth/login` — JWT authentication
- `GET /api/v58/*` — v5.8/v7.1 strategy monitor
- `GET /api/trades` — trade history
- `GET /api/dashboard` — aggregated metrics
- `WS /ws/live` — real-time event stream

**Auth:** JWT (15-minute access tokens, 7-day refresh tokens). Single user account.

### Frontend (React/Vite)

Dashboard for monitoring the engine in real-time. Pages:
- **Dashboard** — live engine status, current window, recent trades
- **V58 Monitor** — per-window signal breakdown, accuracy stats
- **Trades** — full trade history with P&L
- **Signals** — raw signal feed
- **Config** — live config editing (bet fraction, thresholds)
- **TimesFM** — forecast visualisation
- **PnL** — equity curve, daily breakdown
- **System** — engine health, feed connectivity

### Data Collector

Background service that polls the Polymarket Gamma API every second, collecting open/close prices and token prices for BTC, ETH, SOL, XRP across 5m and 15m windows. Stores to `market_data` table with resolution tracking.

### PostgreSQL

Shared Railway PostgreSQL instance. All services connect to the same database.

**Connection:** `hopper.proxy.rlwy.net:35772`  
**Database:** `railway`

---

## TimesFM Service

A Docker container running on AWS EC2 (t3.xlarge, ca-central-1) that serves BTC price-direction forecasts.

- **Endpoint:** `http://16.52.148.255:8080`
- **Refresh rate:** ~1 second
- **Output:** `direction` (UP/DOWN), `confidence` (0–1), `predicted_close`, quantile spreads (P10/P50/P90)
- **Input:** Recent Binance BTC price history
- **Cache TTL:** 0.8s in engine client (avoids duplicate calls)

The engine queries TimesFM at T-60s during each window evaluation. If the service is unreachable, the engine falls back to TWAP-only direction.

---

## Connectivity Summary

| From | To | Protocol | Notes |
|------|----|----------|-------|
| Engine | Railway PostgreSQL | TCP/asyncpg | All trade + signal writes |
| Engine | TimesFM Service | HTTP | Forecast queries |
| Engine | Binance | WebSocket | Real-time trade feed |
| Engine | CoinGlass | HTTPS REST | Polling every 5s |
| Engine | Polymarket Gamma API | HTTPS REST | Window prices |
| Engine | Polymarket CLOB | HTTPS REST + WS | Order execution |
| Hub | Railway PostgreSQL | TCP/asyncpg | Read all tables |
| Data Collector | Railway PostgreSQL | TCP/asyncpg | Write market_data |
| Data Collector | Polymarket Gamma API | HTTPS REST | Market discovery |
| Frontend | Hub | HTTPS REST + WebSocket | Dashboard data |

> ⚠️ **CRITICAL:** All Polymarket API calls (CLOB execution) must come from the Montreal engine (15.223.247.178). Polymarket geo-blocks many regions. Never execute orders from the OpenClaw VPS or Railway.
