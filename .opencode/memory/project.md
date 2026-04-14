# Novakash - BTC Trader Hub Project Memory

## Project Overview

**Domain:** Prediction market trading system for BTC on Polymarket  
**Strategies:**
1. **5-Minute DOWN-Only** (v4_down_only) - Trades DOWN predictions with CLOB-sensitive sizing, 75-80% WR expected
2. **Asian UP** (v4_up_asian) - Trades UP predictions during Asian session (23:00-02:59 UTC), 80-90% WR expected
3. **Sub-$1 Arbitrage** - Exploits YES+NO pricing inefficiencies (currently inactive)
4. **VPIN Cascade Filter** - Directional bets on liquidation cascades (currently inactive)

**Current Status:** DOWN-Only and Asian UP live in paper trading mode

---

## Critical Knowledge - TimesFM v5.2 Model

### Issue (Fixed 2026-04-13)

**Problem:** Model returned constant `0.606` probability (10.2% conviction, always UP)  
**Root Cause:** Missing `chainlink_price` in 3 out of 4 `build_v5_feature_body()` call sites  
**Impact:** 13,096 evals in 12h, all UP at 10.2% conviction → 0 trades  
**Fix:** Added `chainlink_price=window_snapshot.get("chainlink_open")` to:
- `engine/strategies/five_min_vpin.py:1759`
- `engine/use_cases/evaluate_window.py:238`
- `engine/use_cases/evaluate_window.py:851`

**Expected After Fix:**
- P(UP) varies 0.3-0.9 instead of constant 0.606
- Conviction 15-25% instead of stuck at 10.2%
- Trades resume at ~40/day for DOWN-only
- Win rate returns to 75-80%

### Key Architecture

**Model:** LightGBM with 25 features trained on 9,797 windows  
**Serving:** Push-mode POST `/v2/probability` with full feature body  
**Feature Body:** `V5FeatureBody` dataclass in `engine/signals/v2_feature_body.py`  
**Client:** `TimesFMV2Client` in `engine/signals/timesfm_v2_client.py`  
**Service:** `http://16.52.14.182:8080` (TimesFM service on Montreal)

**Train/Serve Skew:** v5 trained with 25 features, but v4 pull-mode feature assembly produced NaNs → model fell back to default leaf → constant 0.606

**Push-Mode:** Engine sends full 25-feature body via POST, scorer uses directly (no pull-mode)

---

## Sparta Agent Guide References

### Key Documents

- `docs/analysis/SPARTA_AGENT_GUIDE.md` - Complete guide for AI agents working on this codebase
- `docs/analysis/TIMESFM_V5_MODEL_BROKEN_2026-04-13.md` - Root cause analysis
- `docs/analysis/TIMESFM_V5_FIX_2026-04-13.md` - Detailed fix implementation
- `docs/analysis/TIMESFM_V5_FIX_APPLIED_2026-04-13.md` - Fix verification checklist

### Sparta Workflow

1. **Plan Mode First:** Enter plan mode for ANY non-trivial task (3+ steps)
2. **Subagent Strategy:** Use subagents liberally for research, exploration, parallel analysis
3. **Self-Improvement:** Update `tasks/lessons.md` after corrections
4. **Verification:** Never mark complete without proving it works (tests, logs, demo)

### When to Use Each Model Surface

- **signal_evaluations:** Signal quality analysis (does model predict well?)
- **unique_windows:** Strategy design (how many windows have valid signal?)
- **trade_bible:** Performance tracking (real PnL)
- **strategy_decisions:** Gate-level decisions (which gates passed/failed)

### Important: 12 Evaluations Per Window

Each 5-minute window is evaluated **12 times** (T-140 to T-90, every 5s), but only **1 trade executes per window** after all filters.

**Filter Cascade:** 18,701 evaluations → 36 unique windows → 40 actual trades (0.2% filter rate)

---

## Architecture

### Data Feeds (6 sources)

1. **Binance WS** (aggTrade) - Primary BTC price, VPIN input, 1-3 Hz
2. **Chainlink Multi-Asset** (Polygon) - Oracle source of truth for Polymarket resolution, 5s polls
3. **Tiingo Top-of-Book** - Multi-exchange bid/ask, 2s polls
4. **CoinGlass Enhanced** - OI, liquidations, funding, taker buy/sell, 4 assets every 15s
5. **Gamma API** (Polymarket) - Token prices per window
6. **CLOB Order Book** - Ground truth Polymarket bid/ask, 10s polls (Montreal only)

### Signal Layer

- **VPIN Calculator** - Volume-synchronized informed flow (50k USD buckets, 50 bucket lookback)
- **Cascade Detector** - FSM: IDLE→CASCADE→BET→COOLDOWN
- **Arb Scanner** - Sub-$1 opportunities
- **Regime Classifier** - Volatility regimes (LOW/NORMAL/HIGH/CASCADE)

### Execution Layer

- **Polymarket Client** - CLOB execution
- **Opinion Client** - Directional bets, lower fees
- **Order Manager** - Lifecycle, PnL tracking
- **Risk Manager** - Kelly sizing, drawdown kill switch

### Persistence

- **PostgreSQL** (asyncpg) - Trades, signals, daily_pnl, system_state
- **Telegram Alerts** - Trades, cascades, kill switch

---

## Tech Stack

- **Engine:** Python 3.12, asyncio, SQLAlchemy
- **Hub Backend:** FastAPI, Uvicorn, PostgreSQL 16
- **Frontend:** React 18, Vite, React Router, Recharts, Tailwind CSS
- **Deployment:** Docker Compose, Caddy, Railway (PostgreSQL)

### Key Files

**Engine (`engine/`)**
- `main.py` - Entry point
- `data/` - Feeds (Binance WS, Chainlink, Tiingo, CoinGlass, Gamma, CLOB)
- `signals/` - VPIN, cascade, arb, regime, TimesFM v2 client
- `strategies/` - DOWN-only, Asian UP, orchestrator
- `execution/` - Clients & risk management
- `persistence/` - PostgreSQL writes

**Hub (`hub/`)**
- `main.py` - FastAPI app
- `api/` - REST endpoints (dashboard, trades, signals, PnL, system, config)
- `ws/` - WebSocket live feed
- `auth/` - JWT auth

---

## Runtime Configuration

### Environment Variables (`.env`)

```bash
# Database
DB_USER=btctrader
DB_PASSWORD=<password>

# Auth
ADMIN_USERNAME=billy
ADMIN_PASSWORD=<password>
JWT_SECRET=<64_chars>

# Polymarket
POLY_PRIVATE_KEY=0x...
POLY_API_KEY=...
POLY_API_SECRET=...
POLY_API_PASSPHRASE=...
POLY_FUNDER_ADDRESS=0x...

# TimesFM
TIMESFM_URL=http://16.52.14.182:8080
TIMESFM_V2_URL=http://16.52.14.182:8080  # v2 push-mode

# Risk
STARTING_BANKROLL=500
PAPER_MODE=true

# Thresholds
V10_DUNE_MODEL=oak  # Model name for v5 (oak = production)
```

### Runtime Config (via `/api/config`)

```json
{
  "bet_fraction": 0.025,
  "max_open_exposure_pct": 0.30,
  "daily_loss_limit_pct": 0.10,
  "vpin_informed_threshold": 0.55,
  "vpin_cascade_threshold": 0.70,
  "arb_min_spread": 0.015,
  "paper_mode": true
}
```

---

## Deployment

### Services

- **Engine** - Runs on Montreal EC2 (15.223.247.178)
- **Hub** - Runs on AWS (16.54.141.121:8091)
- **Frontend** - localhost:3000 (dev), HTTPS via Caddy (prod)
- **TimesFM** - Dedicated box (16.52.14.182:8080)
- **PostgreSQL** - Railway (hopper.proxy.rlwy.net:35772)

### Commands

```bash
# Engine
cd engine && source venv/bin/activate && python -m engine.main

# Hub
cd hub && source venv/bin/activate && uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Frontend
cd frontend && npm run dev

# Docker
docker-compose up -d
docker logs btc-trader-engine-1 -f
docker logs btc-trader-hub-1 -f
```

---

## Common Issues & Fixes

### Issue: Model returns constant 0.606
**Fix:** Add `chainlink_price` to `build_v5_feature_body()` calls

### Issue: No trades in 12h
**Check:** 
1. `v2_probability_up` in signal_evaluations (should vary, not constant)
2. `feature_coverage` in logs (should be >= 0.80)
3. Push-mode status: `v2.probability.push_mode_active`

### Issue: All UP predictions
**Check:** Model is stuck in default leaf (missing features)

### Issue: Win rate < 70%
**Check:** 
1. Regime classifier accuracy
2. CLOB sizing logic
3. Gate thresholds (may be too loose)

---

## Testing

```bash
# Engine tests
cd engine && pytest tests/

# Test coverage
- VPIN: bucket accumulation, tick rule, thresholds
- Cascade: FSM transitions, signal emission
- Arb: opportunity detection, fee math
- Risk: Kelly sizing, drawdown kill, daily halt
```

---

## Monitoring

### Key Metrics

- **Trade frequency:** ~40/day (DOWN-only), ~3/day (Asian UP)
- **Win rate:** 75-80% (DOWN-only), 80-90% (Asian UP)
- **PnL:** Track via `/api/pnl/daily`
- **System status:** `/api/system/status`

### Alerts

- **Kill switch** - Max drawdown 45%
- **Cooldown** - 3 consecutive losses
- **Feed errors** - Data feed disconnection

---

## Recent Changes (2026-04-13)

**Fix:** TimesFM v5.2 missing `chainlink_price` feature
- PR: `fix/timesfm-v5-chainlink-feature`
- Files: `engine/strategies/five_min_vpin.py`, `engine/use_cases/evaluate_window.py`
- Impact: Model now returns variable P(UP) instead of constant 0.606

---

## Related Documentation

- `docs/analysis/SIGNAL_EVAL_RUNBOOK.md` - DB query patterns
- `docs/analysis/24H_STRATEGY_PERFORMANCE_2026-04-12.md` - Last 24h performance
- `docs/analysis/12H_STRATEGY_PERFORMANCE_2026-04-13.md` - Last 12h analysis
- `CLAUDE.md` - AI agent instructions
- `README.md` - Project overview

</content>
<parameter=filePath>
/Users/billyrichards/Code/novakash/.opencode/memory/project.md
## CLOB Audit Methodology (confirmed 2026-04-14)

**Ground truth for real trades = Polymarket data-api, NOT the DB.**

DB `trades` table is unreliable for execution audit:
- `EXPIRED` status = GTC order unfilled (stake returned, no P&L)
- `OPEN` with large stake = may be resolved on-chain already
- `RESOLVED_LOSS` only set if reconciler ran — has known bugs

### Real audit steps:
```bash
# Get funder address from Montreal
aws ec2-instance-connect send-ssh-public-key --region ca-central-1 \
  --instance-id i-0785ed930423ae9fd --instance-os-user novakash \
  --ssh-public-key file:///tmp/ec2ic_key.pub
FUNDER=$(ssh -i /tmp/ec2ic_key -o StrictHostKeyChecking=no novakash@15.223.247.178 \
  'grep POLY_FUNDER_ADDRESS /home/novakash/novakash/engine/.env | cut -d= -f2')

# All on-chain activity (TRADE + REDEEM events)
curl -s "https://data-api.polymarket.com/activity?user=$FUNDER&limit=50" > /tmp/poly_activity.json

# Current open positions (unredeemed wins = redeemable:true)
curl -s "https://data-api.polymarket.com/positions?user=$FUNDER&sizeThreshold=0.01" > /tmp/poly_positions.json
```

**Interpreting activity:**
- `TRADE` = real fill, money left wallet
- `REDEEM usdcSize>0` = WIN
- `REDEEM usdcSize=0` = LOSS (losing side gets nothing)
- `TRADE` with no `REDEEM` = still open

**Today (2026-04-14) confirmed real losses:**
- 01:13 UTC: -$68.34 (9:10PM ET window — MASSIVE oversize, bug in bankroll calc)
- 11:57 UTC: -$14.40 (7:55AM ET window — 4 fills same window, dedup bug)
- 14:12 UTC: -$18.75 (10:10AM ET window — 4 fills same window again)

**Total: ~$93 start → $40.93 accessible ($25.27 CLOB + $15.66 unredeemed win)**
