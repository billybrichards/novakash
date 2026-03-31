# BTC Trader Hub

A prediction market trading system for BTC with two strategies:
1. **Sub-$1 Arbitrage** — Exploits YES+NO pricing inefficiencies on Polymarket
2. **VPIN Cascade Filter** — Directional bets on forced liquidation cascades

## Tech Stack

- **Engine:** Python 3.12, asyncio, SQLAlchemy, websockets
- **Hub Backend:** FastAPI, Uvicorn, PostgreSQL 16
- **Frontend:** React 18, Vite, React Router, Recharts, Tailwind CSS
- **Deployment:** Docker Compose, Caddy

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Frontend (React/Vite)                    │
│                  localhost:3000 (prod: HTTPS)               │
└────────────────────────────┬────────────────────────────────┘
                             │
                    Caddy Reverse Proxy
                             │
┌────────────────────────────┴────────────────────────────────┐
│                  Hub (FastAPI)                              │
│              localhost:8000 → /api, /auth, /ws             │
│                                                             │
│  ├─ REST API (trades, signals, PnL, system control)        │
│  ├─ WebSocket Feed (real-time events)                      │
│  ├─ JWT Auth (15min access, 7day refresh)                  │
│  └─ PostgreSQL (trades, signals, daily_pnl, system_state)  │
└────────────────────────────┬────────────────────────────────┘
                             │
           PostgreSQL ◄─────────────────┐
          (localhost:5432)              │
                                        │
┌───────────────────────────────────────┴──────────────────────┐
│                   Engine (Python/asyncio)                    │
│                                                              │
│  Data Feeds:                                                │
│  ├─ Binance WS (aggTrade, depth20, forceOrder)             │
│  ├─ CoinGlass API (OI, liquidations)                       │
│  ├─ Chainlink RPC (oracle price, Polygon)                  │
│  └─ Polymarket WS (CLOB order books)                       │
│                                                              │
│  Signal Processors:                                         │
│  ├─ VPIN Calculator (volume-synchronized informed flow)    │
│  ├─ Cascade Detector (FSM: IDLE→CASCADE→BET→COOLDOWN)     │
│  ├─ Arb Scanner (sub-$1 opportunities)                     │
│  └─ Regime Classifier (vol regimes: LOW/NORMAL/HIGH/CASCADE)
│                                                              │
│  Strategies:                                                │
│  ├─ Sub-$1 Arb (buy YES+NO when combined < $1)            │
│  └─ VPIN Cascade (fade liquidations on Opinion/Polymarket) │
│                                                              │
│  Execution:                                                 │
│  ├─ Polymarket Client (CLOB execution)                     │
│  ├─ Opinion Client (directional bets, lower fees)          │
│  ├─ Order Manager (lifecycle, PnL tracking)                │
│  └─ Risk Manager (Kelly sizing, drawdown kill switch)      │
│                                                              │
│  Persistence:                                               │
│  ├─ Async PostgreSQL writes (trades, signals)              │
│  └─ Telegram alerts (trades, cascades, kill switch)        │
└──────────────────────────────────────────────────────────────┘
```

## Setup

### Prerequisites

- Docker + Docker Compose
- Node.js 20+ (if building frontend locally)
- Python 3.12 (if running engine locally)
- PostgreSQL 16 (Docker or local)

### Quick Start (Docker)

1. **Clone & enter directory:**
   ```bash
   cd /root/.openclaw/workspace-novakash/novakash
   ```

2. **Create `.env` from template:**
   ```bash
   cp .env.example .env
   # Edit .env with your API keys
   ```

3. **Build & start:**
   ```bash
   docker-compose up -d
   ```

4. **Access:**
   - Frontend: http://localhost:3000
   - Hub API: http://localhost:8000
   - API Docs: http://localhost:8000/api/docs
   - Login: `billy` / (password from .env)

### Local Development

**Engine:**
```bash
cd engine
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

**Hub:**
```bash
cd hub
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```

## Build Phases

### Phase 1: Foundation ✓ (You are here)
- Docker setup (Dockerfile, docker-compose.yml)
- Database schema (PostgreSQL, Alembic migrations)
- Auth system (JWT, single-user login)
- Project structure with full skeleton code
- **Next:** Database initialization & migration

### Phase 2: Data Layer
- Binance WebSocket ingestion
- CoinGlass API polling
- Chainlink RPC integration
- Polymarket CLOB WebSocket
- Market aggregator test & polish

### Phase 3: Signal Layer
- VPIN calculator implementation & testing
- Cascade detector FSM validation
- Arb scanner integration
- Regime classifier tuning
- **Output:** Signal history in DB, WebSocket broadcast

### Phase 4: Execution Layer
- Polymarket order placement & fills
- Opinion exchange integration
- Order manager lifecycle
- Risk manager approval flow
- Telegram alerts integration

### Phase 5: Strategies
- Sub-$1 arb strategy runner
- VPIN cascade bet execution
- Orchestrator full integration
- Live paper trading
- **Output:** Trade history in DB, PnL calculations

### Phase 6: Polish & Deploy
- Backtesting framework
- Hub dashboard API completeness
- Frontend UI refinement
- Production Docker build
- Monitoring & alerting

## Key Files

### Root
- `docker-compose.yml` — Multi-container orchestration
- `.env.example` — Configuration template
- `Caddyfile` — Reverse proxy (HTTPS, routing)

### Engine (`engine/`)
- `main.py` — Entry point
- `config/` — Settings & constants
- `data/` — Feeds (Binance, CoinGlass, Chainlink, Polymarket) + aggregator
- `signals/` — VPIN, cascade, arb, regime
- `execution/` — Clients & risk management
- `strategies/` — Sub-$1 arb, VPIN cascade, orchestrator
- `persistence/` — PostgreSQL writes
- `alerts/` — Telegram notifications
- `tests/` — Unit tests (VPIN, cascade, arb, risk)

### Hub (`hub/`)
- `main.py` — FastAPI app
- `auth/` — JWT, login
- `api/` — REST endpoints (dashboard, trades, signals, PnL, system, config, backtest)
- `ws/` — WebSocket live feed
- `db/` — Models, migrations (Alembic)
- `services/` — Dashboard, PnL, signal aggregation

### Frontend (`frontend/`)
- `src/` — React components & pages
- `index.css` — Tailwind + dark theme CSS variables
- `vite.config.js` — Build config
- `tailwind.config.js` — Custom colors & theme
- `nginx.conf` — SPA serving (prod)

### Scripts (`scripts/`)
- `setup_polymarket.py` — Derive API credentials from wallet
- `fetch_history.py` — Download Binance historical data
- `paper_trade.py` — Paper trading mode entry
- `backtest.py` — Backtest runner skeleton

## Constants & Configuration

### Engine Constants (`engine/config/constants.py`)

**Polymarket / Window:**
- `POLY_WINDOW_SECONDS = 300` — Arb price validity

**Fee Multipliers:**
- `POLYMARKET_CRYPTO_FEE_MULT = 0.072` (7.2%)
- `OPINION_CRYPTO_FEE_MULT = 0.04` (4%)

**VPIN:**
- `VPIN_BUCKET_SIZE_USD = 50_000` — Volume bucket
- `VPIN_LOOKBACK_BUCKETS = 50` — Rolling window
- `VPIN_INFORMED_THRESHOLD = 0.55` — Warning level
- `VPIN_CASCADE_THRESHOLD = 0.70` — Signal level

**Cascade:**
- `CASCADE_OI_DROP_THRESHOLD = 0.02` (2%)
- `CASCADE_LIQ_VOLUME_THRESHOLD = 5e6` ($5M)

**Risk:**
- `MAX_DRAWDOWN_KILL = 0.45` (45% = kill switch)
- `BET_FRACTION = 0.025` (2.5% Kelly)
- `MIN_BET_USD = 2.0`
- `MAX_OPEN_EXPOSURE_PCT = 0.30` (30%)
- `DAILY_LOSS_LIMIT_PCT = 0.10` (10%)
- `CONSECUTIVE_LOSS_COOLDOWN = 3`
- `COOLDOWN_SECONDS = 900` (15 minutes)

**Arb:**
- `ARB_MIN_SPREAD = 0.015` (1.5 cents)
- `ARB_MAX_POSITION = 50.0`
- `ARB_MAX_EXECUTION_MS = 500`

### Runtime Configuration

Overridable via `/api/config` (Hub API):
```json
{
  "bet_fraction": 0.025,
  "max_open_exposure_pct": 0.30,
  "daily_loss_limit_pct": 0.10,
  "vpin_informed_threshold": 0.55,
  "vpin_cascade_threshold": 0.70,
  "arb_min_spread": 0.015,
  "arb_max_position": 50.0,
  "paper_mode": true
}
```

## API Endpoints

### Auth
- `POST /auth/login` — Username/password → JWT
- `POST /auth/refresh` — Refresh token → new access token

### Dashboard
- `GET /api/dashboard` — Full snapshot
- `GET /api/dashboard/summary` — Lightweight summary

### Trades
- `GET /api/trades` — Paginated history (filters: strategy, outcome)
- `GET /api/trades/:id` — Single trade
- `GET /api/trades/stats` — Aggregate win rate, PnL by strategy

### Signals
- `GET /api/signals/vpin` — VPIN history
- `GET /api/signals/cascade` — Cascade state changes
- `GET /api/signals/arb` — Arb opportunities
- `GET /api/signals/regime` — Vol regime history

### PnL
- `GET /api/pnl/daily` — Daily summaries
- `GET /api/pnl/cumulative` — Equity curve
- `GET /api/pnl/by-strategy` — Strategy breakdown
- `GET /api/pnl/monthly` — Monthly aggregates

### System
- `GET /api/system/status` — Engine state, connections, balance
- `POST /api/system/kill` — Activate kill switch
- `POST /api/system/resume` — Resume after kill
- `POST /api/system/paper-mode` — Toggle paper mode

### Config
- `GET /api/config` — Current config
- `PUT /api/config` — Update config (partial)

### WebSocket
- `WS /ws/feed?token=<access_token>` — Real-time events
  - `tick` — Price + VPIN
  - `trade` — Execution
  - `signal` — VPIN/cascade/arb/regime
  - `system` — Status updates

### Backtest
- `GET /api/backtest/runs` — List runs
- `GET /api/backtest/runs/:id` — Full results

## Frontend Pages

- **Dashboard** — System status, VPIN chart, cascade FSM, arb monitor, today's PnL
- **Trades** — Trade history with filters & stats
- **Signals** — Signal history by type (VPIN, cascade, arb, regime)
- **P&L** — Equity curve, daily/monthly summaries, by-strategy breakdown
- **System** — Engine control (kill switch, resume, heartbeat)
- **Config** — Parameter adjustment (Kelly, risk limits, thresholds)

## Testing

```bash
# Run engine tests
cd engine
pytest tests/

# Test fixtures cover:
#   - VPIN: bucket accumulation, tick rule, thresholds
#   - Cascade: FSM transitions, signal emission
#   - Arb: opportunity detection, fee math
#   - Risk: Kelly sizing, drawdown kill, daily halt
```

## Environment Variables (`.env`)

```bash
# ── Database ──
DB_USER=btctrader
DB_PASSWORD=<strong_password>

# ── Hub Auth ──
ADMIN_USERNAME=billy
ADMIN_PASSWORD=<login_password>
JWT_SECRET=<random_64_chars>

# ── Polymarket ──
POLY_PRIVATE_KEY=0x...
POLY_API_KEY=...
POLY_API_SECRET=...
POLY_API_PASSPHRASE=...
POLY_FUNDER_ADDRESS=0x...

# ── Opinion ──
OPINION_API_KEY=...
OPINION_WALLET_KEY=0x...

# ── Binance ──
BINANCE_API_KEY=...
BINANCE_API_SECRET=...

# ── CoinGlass ──
COINGLASS_API_KEY=...

# ── Polygon RPC ──
POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/...

# ── Telegram ──
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# ── Risk ──
STARTING_BANKROLL=500
PAPER_MODE=true

# ── Domain ──
DOMAIN=trader.yourdomain.com
```

## Monitoring & Logs

**Engine Logs:**
```bash
docker logs btc-trader-engine-1 -f
```

**Hub Logs:**
```bash
docker logs btc-trader-hub-1 -f
```

**Database:**
```bash
docker exec -it btc-trader-db-1 psql -U btctrader btc_trader
```

## Troubleshooting

**Database connection failed:**
- Ensure `postgresql+asyncpg://` URL syntax
- Check `DB_USER`, `DB_PASSWORD` match docker-compose.yml
- Wait for Postgres container to be ready (~5s startup)

**Engine won't start:**
- Check `.env` API keys are valid
- Verify Binance/Polymarket/Opinion endpoints are accessible
- Run with `docker logs` to see asyncio errors

**Frontend can't reach API:**
- Verify `/api` proxy in `vite.config.js` (dev) or Caddyfile (prod)
- Check CORS headers in Hub (`main.py`)
- Ensure Hub container is running

**Auth loop:**
- Verify `JWT_SECRET` is set
- Check token expiry (access: 15min, refresh: 7 days)
- Clear browser localStorage & retry login

## Deployment Checklist

- [ ] Copy `.env.example` → `.env` and fill all secrets
- [ ] Run `docker-compose up -d`
- [ ] Verify DB migrations: `docker exec btc-trader-hub-1 alembic upgrade head`
- [ ] Check Hub health: `curl http://localhost:8000/health`
- [ ] Login to frontend with admin credentials
- [ ] Verify all data feeds connect (system page)
- [ ] Run test trade in paper mode
- [ ] Monitor logs for errors: `docker logs -f`

## Contributing

Build phases are sequential — complete Phase 1 (done!) before Phase 2.

Each component has skeleton code and docstrings. Fill in TODOs in order:
1. Data feeds (async WebSocket/API clients)
2. Signal processors (VPIN, cascade, arb, regime)
3. Execution clients (order placement, fills)
4. Strategies (run signals through risk manager)
5. Backtesting & polish

Test each piece with the provided unit tests.

## License

Proprietary — Billy Richards.
