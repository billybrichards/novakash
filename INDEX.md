# btc-trader Project Index

## 📍 You Are Here

**Location:** `/root/.openclaw/workspace-novakash/novakash/`  
**Project:** BTC Prediction Market Trading System  
**Phase:** 1 (Foundation) — COMPLETE ✅  
**Status:** Ready for Phase 2 (Data Layer)

---

## 🚀 Quick Links

### Getting Started
1. **First Time?** → Read [`QUICKSTART.md`](QUICKSTART.md) (5 min)
2. **Want Full Context?** → Read [`README.md`](README.md) (30 min)
3. **Need Project Overview?** → Read [`BUILD_SUMMARY.md`](BUILD_SUMMARY.md) (10 min)
4. **Starting Phase 2?** → See Phase 2 instructions in [`README.md`](README.md#Phase-2-Data-Layer)

### Top-Level Files
| File | Purpose | Read Time |
|------|---------|-----------|
| [`README.md`](README.md) | Complete documentation | 30 min |
| [`QUICKSTART.md`](QUICKSTART.md) | Local development setup | 5 min |
| [`CLAUDE.md`](CLAUDE.md) | AI development context | 10 min |
| [`BUILD_SUMMARY.md`](BUILD_SUMMARY.md) | Phase 1 deliverables | 10 min |
| [`docker-compose.yml`](docker-compose.yml) | 5-service orchestration | reference |
| [`.env.example`](.env.example) | Configuration template | reference |

---

## 📦 Project Structure

### `/engine` — Trading Engine (Python 3.12)

**Key Files:**
- [`main.py`](engine/main.py) — Async entry point
- [`config/settings.py`](engine/config/settings.py) — Pydantic settings from .env
- [`config/constants.py`](engine/config/constants.py) — 20 constants (VPIN, risk, arb)

**Data Feeds** (`/data/feeds/`):
- [`binance_ws.py`](engine/data/feeds/binance_ws.py) — Binance WebSocket
- [`coinglass_api.py`](engine/data/feeds/coinglass_api.py) — CoinGlass API
- [`chainlink_rpc.py`](engine/data/feeds/chainlink_rpc.py) — Chainlink oracle
- [`polymarket_ws.py`](engine/data/feeds/polymarket_ws.py) — Polymarket CLOB

**Signal Processors** (`/signals/`):
- [`vpin.py`](engine/signals/vpin.py) — Volume-synchronized informed flow
- [`cascade_detector.py`](engine/signals/cascade_detector.py) — FSM (5 states)
- [`arb_scanner.py`](engine/signals/arb_scanner.py) — Sub-$1 opportunity detection
- [`regime_classifier.py`](engine/signals/regime_classifier.py) — Vol regime classification

**Execution** (`/execution/`):
- [`polymarket_client.py`](engine/execution/polymarket_client.py) — CLOB orders
- [`opinion_client.py`](engine/execution/opinion_client.py) — Directional bets
- [`order_manager.py`](engine/execution/order_manager.py) — Order lifecycle
- [`risk_manager.py`](engine/execution/risk_manager.py) — Risk enforcement

**Strategies** (`/strategies/`):
- [`base.py`](engine/strategies/base.py) — Abstract interface
- [`sub_dollar_arb.py`](engine/strategies/sub_dollar_arb.py) — Arb strategy
- [`vpin_cascade.py`](engine/strategies/vpin_cascade.py) — Cascade strategy
- [`orchestrator.py`](engine/strategies/orchestrator.py) — Component wiring

**Other:**
- [`data/models.py`](engine/data/models.py) — Pydantic schemas
- [`data/aggregator.py`](engine/data/aggregator.py) — Unified market state
- [`persistence/db_client.py`](engine/persistence/db_client.py) — PostgreSQL writes
- [`alerts/telegram.py`](engine/alerts/telegram.py) — Notifications

**Tests** (`/tests/`):
- [`test_vpin.py`](engine/tests/test_vpin.py) — VPIN unit tests
- [`test_cascade.py`](engine/tests/test_cascade.py) — Cascade FSM tests
- [`test_arb_scanner.py`](engine/tests/test_arb_scanner.py) — Arb tests
- [`test_risk_manager.py`](engine/tests/test_risk_manager.py) — Risk tests

### `/hub` — FastAPI Backend

**API Routers** (`/api/`):
- [`dashboard.py`](hub/api/dashboard.py) — GET /api/dashboard, /summary
- [`trades.py`](hub/api/trades.py) — GET /api/trades, /:id, /stats
- [`signals.py`](hub/api/signals.py) — GET /api/signals/{vpin,cascade,arb,regime}
- [`pnl.py`](hub/api/pnl.py) — GET /api/pnl/{daily,cumulative,by-strategy,monthly}
- [`system.py`](hub/api/system.py) — GET /api/system/status, POST /kill, /resume
- [`config.py`](hub/api/config.py) — GET/PUT /api/config
- [`backtest.py`](hub/api/backtest.py) — GET /api/backtest/runs, /runs/:id

**Authentication** (`/auth/`):
- [`jwt.py`](hub/auth/jwt.py) — Token creation/decode
- [`middleware.py`](hub/auth/middleware.py) — OAuth2 dependency
- [`routes.py`](hub/auth/routes.py) — POST /auth/login, /auth/refresh

**WebSocket** (`/ws/`):
- [`live_feed.py`](hub/ws/live_feed.py) — /ws/feed with JWT auth, real-time events

**Database** (`/db/`):
- [`database.py`](hub/db/database.py) — SQLAlchemy async engine
- [`models.py`](hub/db/models.py) — 6 ORM models
- [`schema.sql`](hub/db/schema.sql) — DDL for all tables
- [`migrations/env.py`](hub/db/migrations/env.py) — Alembic setup

**Services** (`/services/`):
- [`dashboard_service.py`](hub/services/dashboard_service.py) — Dashboard aggregation
- [`pnl_service.py`](hub/services/pnl_service.py) — Daily PnL computation
- [`signal_service.py`](hub/services/signal_service.py) — Signal persistence

**Core:**
- [`main.py`](hub/main.py) — FastAPI app, CORS, routes, events

### `/frontend` — React 18 UI

**Pages** (`/src/pages/`):
- [`Dashboard.jsx`](frontend/src/pages/Dashboard.jsx) — System status, VPIN, cascade, arb
- [`Trades.jsx`](frontend/src/pages/Trades.jsx) — Trade history with filters
- [`Signals.jsx`](frontend/src/pages/Signals.jsx) — Signal history by type
- [`PnL.jsx`](frontend/src/pages/PnL.jsx) — Equity curve, daily/monthly
- [`System.jsx`](frontend/src/pages/System.jsx) — Engine control, kill switch
- [`Config.jsx`](frontend/src/pages/Config.jsx) — Parameter adjustment

**Components** (`/src/components/`):
- [`Layout.jsx`](frontend/src/components/Layout.jsx) — Sidebar nav + header
- [`StatCard.jsx`](frontend/src/components/StatCard.jsx) — Metric display
- [`VPINChart.jsx`](frontend/src/components/VPINChart.jsx) — VPIN line chart
- [`CascadeIndicator.jsx`](frontend/src/components/CascadeIndicator.jsx) — FSM visualization
- [`EquityCurve.jsx`](frontend/src/components/EquityCurve.jsx) — Equity area chart
- [`TradeTable.jsx`](frontend/src/components/TradeTable.jsx) — Trade history table
- [`ArbMonitor.jsx`](frontend/src/components/ArbMonitor.jsx) — Arb opportunities
- [`StatusBadge.jsx`](frontend/src/components/StatusBadge.jsx) — Connection status

**Auth** (`/src/auth/`):
- [`AuthContext.jsx`](frontend/src/auth/AuthContext.jsx) — JWT state management
- [`LoginPage.jsx`](frontend/src/auth/LoginPage.jsx) — Login form
- [`ProtectedRoute.jsx`](frontend/src/auth/ProtectedRoute.jsx) — Auth guard

**Hooks** (`/src/hooks/`):
- [`useApi.js`](frontend/src/hooks/useApi.js) — Authenticated API calls
- [`useWebSocket.js`](frontend/src/hooks/useWebSocket.js) — WebSocket with reconnect

**Library** (`/src/lib/`):
- [`api.js`](frontend/src/lib/api.js) — axios + format helpers
- [`utils.js`](frontend/src/lib/utils.js) — Utility functions

**Core:**
- [`main.jsx`](frontend/src/main.jsx) — React entry point
- [`App.jsx`](frontend/src/App.jsx) — Router + auth wrapper
- [`index.css`](frontend/src/index.css) — Tailwind + dark theme

**Config:**
- [`package.json`](frontend/package.json) — Dependencies
- [`vite.config.js`](frontend/vite.config.js) — Build config
- [`tailwind.config.js`](frontend/tailwind.config.js) — Tailwind theme

### `/scripts` — Utilities

- [`setup_polymarket.py`](scripts/setup_polymarket.py) — Derive API credentials
- [`fetch_history.py`](scripts/fetch_history.py) — Binance history downloader
- [`paper_trade.py`](scripts/paper_trade.py) — Paper trading entry
- [`backtest.py`](scripts/backtest.py) — Backtest runner

---

## 🔧 Common Tasks

### Start Development
```bash
cd /root/.openclaw/workspace-novakash/novakash
docker-compose up -d
# Access: http://localhost:3000 (login: billy)
```

### Run Tests
```bash
cd engine
pytest tests/ -v
```

### View Logs
```bash
docker-compose logs -f engine  # Engine logs
docker-compose logs -f hub     # API logs
docker-compose logs -f db      # Database logs
```

### Access Database
```bash
docker exec -it btc-trader-db-1 psql -U btctrader -d btc_trader
SELECT * FROM trades LIMIT 5;
\q
```

### Stop Services
```bash
docker-compose down
```

### Full Rebuild
```bash
docker-compose down -v
docker-compose build
docker-compose up -d
```

---

## 📚 Documentation Map

| Document | Content | Audience |
|----------|---------|----------|
| [`README.md`](README.md) | Full setup, architecture, API reference, 6 build phases, troubleshooting | Everyone |
| [`QUICKSTART.md`](QUICKSTART.md) | Local dev setup, Docker commands, testing | Developers |
| [`CLAUDE.md`](CLAUDE.md) | Project context, build phases, design decisions | AI/Claude |
| [`BUILD_SUMMARY.md`](BUILD_SUMMARY.md) | Phase 1 deliverables, file-by-file breakdown | Technical review |
| `engine/config/constants.py` | All 20 constants with explanations | Risk/strategy |
| `hub/db/schema.sql` | Full database DDL | DB admins |
| `frontend/tailwind.config.js` | Dark theme colors and styling | Frontend devs |

---

## 🎯 Phase 2 Checklist

When starting Phase 2 (Data Layer):

1. **Read:** [`README.md`](README.md) — Phase 2 section
2. **Implement:** Feed clients in `/engine/data/feeds/`
3. **Test:** Use pytest to verify feed logic
4. **Monitor:** `docker-compose logs -f` to watch integration
5. **Verify:** Check `/api/system/status` for connection health

---

## 🔑 Key Concepts

**VPIN** (Volume-Synchronized Probability of Informed Trading)
- Real-time estimate of informed trading flow
- See: [`engine/signals/vpin.py`](engine/signals/vpin.py)
- Constants: [`engine/config/constants.py`](engine/config/constants.py#L8-L12)

**Cascade Detector** (FSM — 5 States)
- Detects forced liquidation cascades
- States: IDLE → CASCADE_DETECTED → EXHAUSTING → BET_SIGNAL → COOLDOWN
- See: [`engine/signals/cascade_detector.py`](engine/signals/cascade_detector.py)

**Sub-$1 Arbitrage**
- Exploit YES+NO mispricing on Polymarket
- Combined ask < $1.00 − fees = profit
- See: [`engine/signals/arb_scanner.py`](engine/signals/arb_scanner.py)

**Risk Manager**
- Kelly-fractional sizing (2.5% of bankroll per bet)
- Kill switch at 45% drawdown
- Daily loss limit (10%)
- Cooldown after 3 consecutive losses
- See: [`engine/execution/risk_manager.py`](engine/execution/risk_manager.py)

---

## 💾 File Sizes (for reference)

| Component | Size | Files |
|-----------|------|-------|
| Engine | 45 KB | ~35 |
| Hub | 35 KB | ~20 |
| Frontend | 40 KB | ~25 |
| Scripts | 7 KB | 4 |
| Docs | 50+ KB | 6 |
| **Total** | **1.4 MB** | **283** |

---

## 🚀 Next: Phase 2

See [`README.md`](README.md) section "Build Phases" → "Phase 2: Data Layer"

Key files to edit:
1. `engine/data/feeds/binance_ws.py` — Real WebSocket
2. `engine/data/feeds/coinglass_api.py` — Real API
3. `engine/data/feeds/chainlink_rpc.py` — Real RPC
4. `engine/data/feeds/polymarket_ws.py` — Real CLOB feed
5. `engine/data/aggregator.py` — Verify integration

---

**Last Updated:** 2026-03-31 UTC  
**Phase:** 1 (Foundation) — ✅ COMPLETE  
**Ready for:** Phase 2 (Data Layer)
