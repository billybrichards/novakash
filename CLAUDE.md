# CLAUDE.md — BTC Trader Hub

## Operating Modes

### 1. Plan Mode Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately — don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction from the user: update `tasks/lessons.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes — don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests — then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

## Task Management

1. **Plan First:** Write plan to `tasks/todo.md` with checkable items
2. **Verify Plan:** Check in before starting implementation
3. **Track Progress:** Mark items complete as you go
4. **Explain Changes:** High-level summary at each step
5. **Document Results:** Add review section to `tasks/todo.md`
6. **Capture Lessons:** Update `tasks/lessons.md` after corrections

## Core Principles

- **Simplicity First:** Make every change as simple as possible. Impact minimal code.
- **No Laziness:** Find root causes. No temporary fixes. Senior developer standards.

---

## Project: BTC Trader Hub

A prediction market trading system for BTC with three strategies:
1. **5-Minute Up/Down** ⭐ — Trades Polymarket's ephemeral 5-min BTC markets using window delta at T-10s (82% backtest win rate)
2. **Sub-$1 Arbitrage** — Exploits YES+NO pricing inefficiencies on Polymarket (currently inactive — spreads too wide)
3. **VPIN Cascade Filter** — Directional bets on forced liquidation cascades (currently inactive — low vol)

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
│  Data Feeds (6 sources):                                    │
│  ├─ Binance WS (aggTrade — primary BTC price, VPIN input)  │
│  ├─ Chainlink Multi-Asset (BTC/ETH/SOL/XRP on Polygon,     │
│  │   polls every 5s — oracle source of truth for Polymarket │
│  │   resolution, saved to ticks_chainlink)                  │
│  ├─ Tiingo Top-of-Book (BTC/ETH/SOL/XRP, polls every 2s,  │
│  │   multi-exchange bid/ask with exchange attribution,      │
│  │   saved to ticks_tiingo)                                 │
│  ├─ CoinGlass Enhanced (OI, liquidations, funding rate,    │
│  │   taker buy/sell, L/S ratio — 4 assets every 15s)       │
│  ├─ Gamma API (Polymarket token prices per window)         │
│  ├─ CLOB Order Book (ground truth Polymarket bid/ask,     │
│  │   every 10s — real entry prices, Montreal only)         │
│  └─ TimesFM (ML forecast direction + confidence)           │
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

## Build Phases

### Phase 1: Foundation ✓ (Done)
- Docker setup (Dockerfile, docker-compose.yml)
- Database schema (PostgreSQL, Alembic migrations)
- Auth system (JWT, single-user login)
- Project structure with full skeleton code

### Phase 2: Data Layer ✓ (Done)
- Binance WebSocket ingestion (aggTrade → VPIN, 1-3 Hz)
- CoinGlass Enhanced API (OI, liquidations, funding, taker buy/sell — 4 assets)
- Chainlink Multi-Asset Feed (BTC/ETH/SOL/XRP on Polygon, 5s polls — oracle source of truth)
- Tiingo Top-of-Book Feed (BTC/ETH/SOL/XRP, 2s polls — multi-exchange bid/ask with exchange attribution)
- Gamma API (Polymarket token prices per window)
- CLOB Order Book Feed (ground truth Polymarket bid/ask, 10s polls — real entry prices)
- TimesFM ML forecast (direction + confidence)
- v7.2: Multi-source delta calculation (Chainlink primary, Tiingo, Binance)
- Market aggregator wiring complete
- All 7 feeds save to dedicated tick tables in Railway PostgreSQL
- See `docs/DATA_FEEDS.md` for full schema, queries, and data flow diagram

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
- `data/` — Feeds (Binance WS, Chainlink multi-asset, Tiingo top-of-book, CoinGlass enhanced, Polymarket/Gamma, CLOB order book, TimesFM) + aggregator
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

## Setup

### Quick Start (Docker)

```bash
cd /root/.openclaw/workspace-novakash/novakash
cp .env.example .env
# Edit .env with your API keys
docker-compose up -d
```

Access:
- Frontend: http://localhost:3000
- Hub API: http://localhost:8000
- API Docs: http://localhost:8000/api/docs

### Local Development

**Engine:**
```bash
cd engine && python -m venv venv && source venv/bin/activate
pip install -r requirements.txt && python main.py
```

**Hub:**
```bash
cd hub && python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

**Frontend:**
```bash
cd frontend && npm install && npm run dev
```

## Monitoring & Logs

```bash
# Engine logs
docker logs btc-trader-engine-1 -f

# Hub logs
docker logs btc-trader-hub-1 -f

# Database
docker exec -it btc-trader-db-1 psql -U btctrader btc_trader
```

## Deployment Checklist

- [ ] Copy `.env.example` → `.env` and fill all secrets
- [ ] Run `docker-compose up -d`
- [ ] Verify DB migrations: `docker exec btc-trader-hub-1 alembic upgrade head`
- [ ] Check Hub health: `curl http://localhost:8000/health`
- [ ] Login to frontend with admin credentials
- [ ] Verify all data feeds connect (system page)
- [ ] Run test trade in paper mode
- [ ] Monitor logs for errors: `docker logs -f`

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

## License

Proprietary — Billy Richards.
