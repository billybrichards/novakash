# Novakash — BTC Prediction Market Trading Engine

An automated trading system for Polymarket's 5-minute BTC Up/Down markets. Uses a multi-signal pipeline combining VPIN (volume microstructure), TWAP momentum, TimesFM AI forecasting, and CoinGlass derivatives data.

**Active Strategy:** v7.1 — VPIN gate 0.45, regime-aware delta thresholds, GTC limit orders at T-60s

---

## Quick Links

| | |
|--|--|
| 📖 **Full Documentation** | [`docs/`](docs/) |
| 🏗️ Architecture | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| 🖥️ Infrastructure | [`docs/INFRASTRUCTURE.md`](docs/INFRASTRUCTURE.md) |
| 📈 Trading Strategy | [`docs/STRATEGY.md`](docs/STRATEGY.md) |
| 🗄️ Database Schema | [`docs/DATABASE.md`](docs/DATABASE.md) |
| 🚀 Deployment | [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) |
| 🔌 API Reference | [`docs/API.md`](docs/API.md) |
| 🤖 AI Integration | [`docs/CLAUDE.md`](docs/CLAUDE.md) |

---

## Architecture

```
Binance WS ─────────────────────────────────────────────────┐
CoinGlass API ──────────────────────────────────────────────┤
Polymarket Gamma API ───────────────────────────────────────┤
                                                            ▼
                                              ┌─────────────────────┐
                                              │  Montreal Engine     │
                                              │  15.223.247.178      │
                                              │  Python / asyncio    │
                                              └──────────┬──────────┘
                                                         │
                    ┌────────────────────────────────────┤
                    │                                    │
                    ▼                                    ▼
        ┌───────────────────────┐          ┌────────────────────┐
        │  Railway PostgreSQL    │          │  Polymarket CLOB    │
        │  hopper.proxy.rlwy.net│          │  (GTC limit orders) │
        └───────────┬───────────┘          └────────────────────┘
                    │
          ┌─────────┼─────────┐
          ▼         ▼         ▼
       Hub API   Frontend  Data Collector
       (FastAPI)  (React)   (Gamma poller)
       Railway    Railway   Railway
```

**TimesFM:** `16.52.148.255:8080` — AI forecast service (AWS EC2, ca-central-1)

> ⚠️ All Polymarket CLOB API calls must originate from Montreal (15.223.247.178)

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Engine | Python 3.12, asyncio |
| API | FastAPI, Uvicorn |
| Frontend | React 18, Vite, Tailwind CSS, Recharts |
| Database | PostgreSQL 16 (Railway) |
| AI | Claude Opus 4.6, Qwen 122B (fallback), TimesFM |
| Deployment | Railway (hub/frontend/db), AWS EC2 (engine + TimesFM) |
| Data | Binance WebSocket, CoinGlass API, Polymarket Gamma API |

---

## Active Strategy Summary

- **Signal evaluation:** T-60 seconds before window close
- **VPIN gate:** Skip if VPIN < 0.45 (low informed flow)
- **Regime:** CASCADE (VPIN ≥ 0.65) / TRANSITION (0.55–0.65) / NORMAL (< 0.55)
- **Delta threshold:** 0.02% (NORMAL/TRANSITION), 0.01% (CASCADE)
- **TimesFM gate:** TimesFM must agree with primary direction
- **Entry cap:** Token price ≤ $0.70
- **Stake:** 2.5% Kelly fraction
- **Resolution:** Polymarket oracle ~4 min post-close

---

## Repository Structure

```
novakash-repo/
├── engine/              # Core trading engine (runs on Montreal)
│   ├── config/          # Settings, constants, runtime config
│   ├── data/            # Data feeds (Binance, CoinGlass, Polymarket)
│   ├── signals/         # VPIN, TWAP, TimesFM, cascade detector
│   ├── strategies/      # five_min_vpin (active), arb, cascade (inactive)
│   ├── execution/       # Order manager, Polymarket client, risk manager
│   ├── evaluation/      # Claude evaluator
│   ├── persistence/     # DB writes, tick recorder
│   └── alerts/          # Telegram notifications, chart generation
├── hub/                 # FastAPI backend (Railway)
│   ├── api/             # REST endpoints
│   ├── auth/            # JWT authentication
│   ├── db/              # Schema, migrations, models
│   ├── services/        # Business logic
│   └── ws/              # WebSocket live feed
├── frontend/            # React dashboard (Railway)
│   └── src/
│       ├── pages/       # Dashboard, V58Monitor, Trades, Config, etc.
│       └── components/  # Reusable UI components
├── data-collector/      # Polymarket market data poller (Railway)
├── timesfm-service/     # TimesFM Docker service (AWS EC2)
├── docs/                # 📖 Full system documentation
└── scripts/             # Utility scripts
```

---

## Getting Started

See [`QUICKSTART.md`](QUICKSTART.md) for local setup instructions.

For production deployment, see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

---

## Key Files for AI Agents

- **`CLAUDE.md`** — Agent operating procedures and coding standards
- **`docs/INFRASTRUCTURE.md`** — All servers, IPs, SSH access
- **`docs/STRATEGY.md`** — Full strategy explanation
- **`docs/DATABASE.md`** — Complete table schemas
- **`docs/API.md`** — Hub API reference
