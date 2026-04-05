# Novakash — System Overview

Novakash is a fully automated cryptocurrency prediction market trading engine. It trades 5-minute BTC Up/Down markets on [Polymarket](https://polymarket.com) using a multi-signal pipeline that combines volume microstructure (VPIN), price momentum (TWAP), AI time-series forecasting (TimesFM), and CoinGlass derivatives data.

---

## What It Does

Every 5 minutes a new "window" opens on Polymarket: will BTC be higher or lower in the next 5 minutes? The engine ingests real-time data from Binance, CoinGlass, and Polymarket itself, evaluates whether conditions are favourable at **T-60 seconds before window close**, then places a GTC limit order at the best available price if the signal passes all gates.

---

## Architecture Diagram

```
┌───────────────────────────────────────────────────────────────────────┐
│                        External Data Sources                          │
│  Binance WS (trades)  ·  CoinGlass API  ·  Polymarket Gamma API      │
└──────────────┬──────────────────────────────────────────┬────────────┘
               │                                          │
               ▼                                          ▼
┌──────────────────────────┐              ┌───────────────────────────┐
│   Montreal Engine         │              │   TimesFM Service          │
│   15.223.247.178          │◄────────────►│   16.52.148.255:8080       │
│   /home/novakash/novakash │              │   Docker / t3.xlarge       │
│   Python 3.12 + asyncio   │              │   ca-central-1             │
│                           │              └───────────────────────────┘
│   Signals: VPIN, TWAP,    │
│   CG veto, TimesFM gate   │
│   Execution: Polymarket   │──────► Polymarket CLOB (GTC orders)
└──────────────┬────────────┘
               │  writes
               ▼
┌──────────────────────────────────────────────────────────────────────┐
│               Railway PostgreSQL (hopper.proxy.rlwy.net:35772)        │
└──────────────┬───────────────────────────────────────────────────────┘
               │
       ┌───────┴────────┐
       ▼                ▼
┌─────────────┐  ┌───────────────────┐
│  Hub (API)  │  │  Data Collector    │
│  Railway    │  │  Railway           │
│  FastAPI    │  │  Gamma + market    │
│  /api/v58/* │  │  data polling      │
└──────┬──────┘  └───────────────────┘
       │
       ▼
┌─────────────┐
│  Frontend   │
│  Railway    │
│  React/Vite │
│  Dashboard  │
└─────────────┘
```

---

## Key Components

| Component | Location | Purpose |
|-----------|----------|---------|
| **Engine** | Montreal (15.223.247.178) | Core trading loop — signals, execution, risk |
| **Hub** | Railway | REST API + WebSocket feed for frontend |
| **Frontend** | Railway | React dashboard for monitoring |
| **Data Collector** | Railway | Background Polymarket market data poller |
| **TimesFM Service** | AWS (16.52.148.255:8080) | AI price-direction forecasting |
| **Database** | Railway PostgreSQL | Shared state: trades, signals, snapshots |

---

## Active Strategy

**v7.1 — Five-Minute VPIN with TimesFM Gate**

- Evaluates at **T-60 seconds** before window close
- VPIN gate: skip if VPIN < 0.45 (low informed flow)
- Regime-aware delta thresholds (CASCADE/TRANSITION/NORMAL)
- TimesFM must agree with TWAP/window-delta direction
- GTC limit orders at Gamma best price (entry cap $0.70)
- Polymarket oracle resolves ~4 minutes post-close

See [`STRATEGY.md`](STRATEGY.md) for full details.

---

## Further Reading

| Doc | Contents |
|-----|----------|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Detailed component architecture |
| [`INFRASTRUCTURE.md`](INFRASTRUCTURE.md) | Servers, IPs, SSH, deploy procedures |
| [`STRATEGY.md`](STRATEGY.md) | Trading logic, signals, gates |
| [`DATABASE.md`](DATABASE.md) | Full table schemas |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | How to deploy each component |
| [`API.md`](API.md) | Hub REST API reference |
| [`CLAUDE.md`](CLAUDE.md) | AI/Claude integration details |
