# Novakash - Memory Document (13 April 2026)

**Purpose:** Central reference for Novakash BTC Trader Hub architecture, strategies, and operational guidelines.

---

## 1. Architecture Overview

**Microservices Architecture** - Three independent services:

### Engine (Python/asyncio)
- **Location:** Montreal EC2 (15.223.247.178)
- **User:** `novakash`
- **Process:** `python3 main.py` in `/home/novakash/novakash/engine`
- **Logs:** `/home/novakash/engine.log`
- **Purpose:** Trading logic, data feeds, strategy execution

### Hub (FastAPI)
- **Location:** Railway
- **Port:** 8000
- **Endpoints:** `/api`, `/auth`, `/ws`
- **Database:** PostgreSQL (Railway)
- **Purpose:** REST API, WebSocket, storage

### Frontend (React/Vite)
- **Location:** Local (dev: 3000), Production via Caddy
- **Purpose:** Dashboard, trade monitoring, system control

### Communication
- Engine ↔ Hub: PostgreSQL (Railway)
- Frontend ↔ Hub: HTTP/WebSocket via Caddy reverse proxy

---

## 2. SSH Access (Montreal)

### Connection
```bash
# Via EC2 Instance Connect
# 1. Send SSH key via AWS API
aws ec2-instance-connect send-ssh-public-key \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user novakash \
  --ssh-public-key "ssh-ed25519 YOUR_KEY" \
  --region ca-central-1

# 2. SSH to server
ssh novakash@15.223.247.178
```

### Engine Management
```bash
# Check status
pgrep -fa "python3 main.py"

# Check logs
tail -100 /home/novakash/engine.log

# Restart (if dead)
cd /home/novakash/novakash/engine
nohup python3 main.py >> /home/novakash/engine.log 2>&1 &
```

**Critical:** Engine must run continuously. Dead engine = no trades.

---

## 3. V4 Down-Only Strategy

### Specification
- **Direction:** DOWN only (UP predictions always skipped)
- **Confidence:** dist ≥ 0.12 (|p_up - 0.5| ≥ 0.12)
- **Timing Window:** T-90 to T-150 seconds before window close (**CRITICAL**)
- **Hours:** Any UTC hour
- **CLOB Sizing:**
  - 2.0× at clob_down_ask ≥ 0.55
  - 1.5× at ≥ 0.35
  - 1.0× below

### Performance
- **Historical WR:** 90.3% (897K samples, Apr 7-12 2026)
- **Last 24h WR:** 77% (40 trades, 31W/9L)
- **Expected Trades:** 30-50/day (volatile), 5-15/day (flat)

### Expected Behavior
- **When DOWN predicted** with dist ≥ 0.12 AND in T-90-150 window → **TRADE**
- **When UP predicted** → SKIP (by design)
- **When dist < 0.12** → SKIP (low conviction)
- **When outside T-90-150** → SKIP (timing gate)

### Monitoring
```bash
# Check TimesFM predictions
grep "timesfm.forecast_fetched" /home/novakash/engine.log | tail -10

# Check v4_down_only decisions
grep "v4_down_only" /home/novakash/engine.log | tail -20

# Check timing progression
grep "eval_offset=" /home/novakash/engine.log | tail -30
```

---

## 4. V4 Up-Asian Strategy

### Specification
- **Direction:** UP only
- **Confidence:** dist 0.15-0.20 (medium conviction band)
- **Timing:** T-90 to T-150
- **Hours:** 23:00-02:59 UTC (Asian session only)
- **Historical WR:** 81-99% (5,543 samples, Apr 10-12 2026)
- **Expected Trades:** 2-5 per Asian session

### Direction Exclusivity
Both strategies are direction-exclusive. In any given 5-min window, at most ONE fires:
- DOWN with dist ≥ 0.12 → v4_down_only fires
- UP with dist 0.15-0.20 AND Asian session → v4_up_asian fires
- Both SKIP if neither condition met

---

## 5. Data Feeds

### 6 Sources (All save to PostgreSQL)

1. **Binance WS** (aggTrade)
   - Primary BTC price, VPIN input
   - Frequency: 1-3 Hz

2. **Chainlink Multi-Asset**
   - BTC/ETH/SOL/XRP on Polygon
   - Polls every 5s
   - Oracle source of truth for Polymarket resolution

3. **Tiingo Top-of-Book**
   - BTC/ETH/SOL/XRP
   - Polls every 2s
   - Multi-exchange bid/ask with exchange attribution

4. **CoinGlass Enhanced**
   - OI, liquidations, funding rate, taker buy/sell, L/S ratio
   - 4 assets every 15s

5. **Gamma API / CLOB**
   - Polymarket token prices per window
   - CLOB order book (ground truth bid/ask)
   - Every 10s, Montreal only

6. **TimesFM v5.2**
   - ML forecast (direction + confidence)
   - **Fixed:** Returns variable probabilities (0.96-0.98) instead of broken constant 0.606

### Data Flow
```
Data Feeds → Engine → PostgreSQL (Railway) → Hub API → Frontend
```

---

## 6. Environment Variables (Engine)

```bash
# V10.6 Gate
V10_6_ENABLED=true
V10_6_MIN_EVAL_OFFSET=90
V10_6_MAX_EVAL_OFFSET=120

# Evaluation Timing
FIVE_MIN_EVAL_INTERVAL=2  # Generates T-240, T-238, ..., T-60

# Trading Mode
PAPER_MODE=true  # Set false for live trading

# Database
DATABASE_URL=postgresql+asyncpg://...

# API Keys
BINANCE_API_KEY=...
COINGLASS_API_KEY=...
POLYGON_RPC_URL=...
TELEGRAM_BOT_TOKEN=...
```

---

## 7. V10.6 Gate

### Timing Window
- **Valid eval_offset:** T-90 to T-120 seconds before window close
- **All strategies** must pass this gate to trade
- **Default:** Enabled

### Why T-90-120?
- Too early (T>120): Accuracy degrades to ~50-65%
- Too late (T<90): Insufficient execution time
- Sweet spot (T-90-120): 90.3% WR validated

---

## 8. TimesFM v5.2 Fix

### What Was Broken
- v5 model returning **constant probability=0.6061** for every prediction
- Caused by v5 scorer calling `score()` instead of `score_from_features()`
- 4h delta buckets missing from `DELTA_BUCKETS_BY_TIMEFRAME`

### What Was Fixed
1. Added 4h delta buckets to model registry
2. Added chainlink_price to v5.2 feature body
3. Fixed 3 call sites where chainlink_price was missing

### Verification
- **Before:** Constant 0.6061
- **After:** Variable 0.96-0.98 (DOWN predictions)
- **Latency:** ~1.3s per prediction

---

## 9. Audit Checklist

### Location
`frontend/src/pages/AuditChecklist.jsx`

### Categories
- **Engine:** Implementation and testing
- **Hub:** API completeness
- **Frontend:** UI pages and data surfaces
- **Strategy:** Signal validation
- **Security:** Auth and risk
- **CI/CD:** Deployment pipelines
- **Data:** Feed reliability

### Key Tasks
- **SIG-01:** VPIN implementation
- **SIG-02:** Cascade detector
- **SIG-03/SIG-04:** Down-only strategy audit
- **LT-04:** Fast trade execution
- **FE-04/05/06:** V1/V2/V3 data surfaces

### Progress Tracking
Each task has `progressNotes` array with dated updates.

---

## 10. SPARTA Documentation

### Key Docs
- **MONTREAL_DEPLOYMENT_TROUBLESHOOTING.md** - SSH access, engine monitoring
- **V10_6_DECISION_SURFACE_PROPOSAL.md** - eval_offset gate specification
- **DATA_FEEDS.md** - 6 data sources, schema, queries
- **CLAUDE.md** - Operating modes, subagent strategy, verification
- **UP_DOWN_STRATEGY_RUNBOOK.md** - Strategy specs and monitoring

### Runbook Commands
```bash
# Run UP/DOWN strategy report
export PUB_URL="postgresql://..."
python3 docs/analysis/up_down_strategy_report.py

# Check 24h DOWN WR
# See UP_DOWN_STRATEGY_RUNBOOK.md for SQL queries
```

---

## 11. Common Issues & Fixes

### Engine Dead
**Symptom:** No logs for >10 minutes
**Fix:** Restart via SSH
```bash
cd /home/novakash/novakash/engine
nohup python3 main.py >> /home/novakash/engine.log 2>&1 &
```

### No Trades Being Placed
**Check:**
1. Engine running? `pgrep -fa "python3 main.py"`
2. eval_offset in T-90-150 window?
3. DOWN signal with dist ≥ 0.12?
4. TimesFM returning variable probabilities?

### Window Evaluation Timing
**Expected:** Engine evaluates every 2 seconds (T-240 → T-60)
**Tradeable:** Only T-90 to T-150
**Outside window:** Strategies SKIP (by design)

---

## 12. Monitoring Checklist

### Daily
- [ ] Engine process running
- [ ] New windows being evaluated (window_ts changes every 5 min)
- [ ] eval_offset progressing T-240 → T-60
- [ ] v4_down_only seeing T-90-150 window
- [ ] DOWN predictions (check TimesFM logs)

### Alert Triggers
- Engine dead for >10 minutes
- DOWN WR <65% over 10+ trades
- No trades in 4+ hours during volatile market
- eval_offset stuck (not progressing)

---

## 13. Quick Reference

### Engine Status
```bash
ssh novakash@15.223.247.178 'pgrep -fa "python3 main.py" && tail -20 /home/novakash/engine.log'
```

### TimesFM Check
```bash
ssh novakash@15.223.247.178 'grep "timesfm.forecast_fetched" /home/novakash/engine.log | tail -5'
```

### Strategy Decisions
```bash
ssh novakash@15.223.247.178 'grep "v4_down_only" /home/novakash/engine.log | tail -10'
```

### Window Timing
```bash
ssh novakash@15.223.247.178 'grep "eval_offset=" /home/novakash/engine.log | tail -10'
```

---

**Document Created:** 13 April 2026  
**Last Updated:** 13 April 2026  
**Version:** 1.0

**Related Memories:**
- Memory ID: `arch-mem-1` - Microservices architecture
- Memory ID: `novakash-sparta-docs` - SPARTA docs guidelines
- Memory ID: `novakash-down-only-behavior` - Down-only strategy behavior
- Memory ID: `novakash-core-engine` - Core engine operations
- Memory ID: `novakash-audit-checklist` - Audit checklist
- Memory ID: `novakash-polymarket-engine` - Polymarket engine
