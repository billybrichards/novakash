# v1.0 Design Spec: Composite Signal System + Binance Margin Execution

**Date:** 2026-04-09  
**Author:** Billy / Claude  
**Status:** PROPOSED  
**Extends:** v10.4 gate system, v10.2 cascade pricing surface  

---

## 1. Executive Summary

Two-repo enhancement that transforms the trading system from a single-venue, window-bound Polymarket bot into a multi-venue, multi-timescale trading platform.

**Three pillars:**

1. **Composite Signal Service** (timesfm repo) — Continuous scoring engine that fuses ELM probability, cascade state, CoinGlass flow, VPIN, momentum, and funding rate into a single [-1, +1] directional score across 9 timescales (5m → 2 weeks).

2. **Binance Margin Execution** (novakash engine) — Continuous position management on Binance 5x cross-margin, driven by composite score crossings with adaptive trailing stops and macro-aligned sizing.

3. **DB-Configurable Runtime + Dashboard** (both repos) — All tunable parameters in a `system_config` table, editable from a live dashboard. New frontend pages for composite visualization, macro timescale heatmaps, Binance position management, and config editing.

**Key constraint:** Polymarket strategy remains completely unchanged. The composite signal is additive — it doesn't replace the existing gate pipeline, it augments it. Binance runs in parallel, not in competition.

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────┐
│         TIMESFM REPO (Signal Brain)                     │
│         EC2 ca-central-1 / 8080                         │
│                                                         │
│  Existing (unchanged):                                  │
│  ├─ Binance WS price feed (2048-tick buffer)            │
│  ├─ CoinGlass poller (15s cadence)                      │
│  ├─ ELM v3 LightGBM scorer                             │
│  ├─ TimesFM v1 forecast (frozen)                        │
│  └─ /v2/probability endpoint                            │
│                                                         │
│  New v3 modules:                                        │
│  ├─ v3_cascade_estimator.py  — S, τ₁, τ₂ from prices   │
│  ├─ v3_composite_scorer.py   — fuses signals → [-1,+1]  │
│  ├─ v3_multiscale.py         — 9 timescale scoring loops │
│  ├─ v3_macro_store.py        — DB-backed 24h+ aggregates │
│  ├─ v3_routes.py             — WS /v3/signal + REST      │
│  └─ v3_db_writer.py          — ticks_v3_composite writes │
│                                                         │
│  Outputs:                                               │
│  ├─ WS /v3/signal (1-2Hz composite stream)              │
│  ├─ GET /v3/snapshot (REST fallback)                    │
│  ├─ GET /v2/probability (unchanged)                     │
│  └─ DB: ticks_v3_composite                              │
└──────────────────────┬──────────────────────────────────┘
                       │ WebSocket (bidirectional)
                       │ Engine → Service: VPIN, regime
                       │ Service → Engine: composite signal
                       ▼
┌─────────────────────────────────────────────────────────┐
│         NOVAKASH ENGINE (Execution)                     │
│         Montreal / Railway                              │
│                                                         │
│  Existing (unchanged):                                  │
│  ├─ All data feeds (Binance, CG, Chainlink, Tiingo)    │
│  ├─ VPIN calculator, regime classifier                  │
│  ├─ Gate pipeline (gates.py)                            │
│  ├─ Polymarket client + 5-min strategy                  │
│  ├─ Reconciler, redeemer                                │
│  └─ Telegram alerts                                     │
│                                                         │
│  New modules:                                           │
│  ├─ data/feeds/v3_signal_feed.py  — WS consumer         │
│  ├─ execution/binance_margin.py   — margin client       │
│  ├─ strategies/binance_continuous.py — continuous strat  │
│  ├─ services/config_service.py    — DB config reader    │
│  └─ persistence/binance_recorder.py — tick storage      │
│                                                         │
│  Outputs:                                               │
│  ├─ Polymarket trades (unchanged)                       │
│  ├─ Binance margin trades (new)                         │
│  └─ DB: ticks_binance_margin, trades (venue column)     │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│         HUB + FRONTEND (Dashboard)                      │
│                                                         │
│  New pages:                                             │
│  ├─ /dashboard/composite  — Live composite scores       │
│  ├─ /dashboard/binance    — Margin position + history   │
│  ├─ /dashboard/macro      — Multi-timescale heatmap     │
│  └─ /dashboard/config     — DB config editor            │
│                                                         │
│  New API endpoints:                                     │
│  ├─ GET/PUT /api/config/*  — DB config CRUD             │
│  ├─ GET /api/composite/*   — Composite history + live   │
│  ├─ WS /ws/composite       — Real-time composite proxy  │
│  └─ GET /api/binance/*     — Position, trades, balance  │
└─────────────────────────────────────────────────────────┘
```

---

## 3. Composite Signal Service (TimesFM Repo)

### 3.1 The Composite Score

A single continuous value in **[-1.0, +1.0]** representing directional conviction:

- **+1.0** = maximum confidence BTC goes UP
- **-1.0** = maximum confidence BTC goes DOWN
- **0.0** = no signal / conflicting signals

Computed independently per timescale, producing a vector:
```
[5m: +0.72, 15m: +0.55, 1h: +0.30, 4h: -0.15, 24h: +0.40, 
 48h: +0.35, 72h: +0.28, 1w: +0.42, 2w: +0.38]
```

### 3.2 Signal Components

7 normalized component signals, each mapped to [-1, +1]:

| # | Component | Source | Normalization | Available |
|---|-----------|--------|---------------|-----------|
| 1 | **ELM probability** | v2_scorer P(UP) | `2 × P(UP) - 1` | 5m, 15m only |
| 2 | **Cascade state** | CascadeEstimator S | `S × sign(cascade_direction)` | All |
| 3 | **CG taker flow** | CoinGlass net taker | `net / (total × 0.5)`, clipped | All |
| 4 | **CG OI delta** | CoinGlass OI delta % | `clip(delta / 0.02, -1, 1)` | All |
| 5 | **Funding rate** | CoinGlass funding | `-clip(funding / 0.001, -1, 1)` (fade extreme) | All |
| 6 | **VPIN momentum** | Engine (via WS) | `2 × (vpin - 0.5)` | All |
| 7 | **Price momentum** | Price returns over timescale | `clip(ret / threshold, -1, 1)` | All |

### 3.3 Weight Profiles Per Timescale

```
                ELM   Cascade  CG_flow  CG_OI  Funding  VPIN  Momentum
5m:           [0.35,   0.20,    0.15,   0.10,   0.05,  0.10,   0.05]
15m:          [0.30,   0.20,    0.15,   0.10,   0.05,  0.10,   0.10]
1h:           [0.00,   0.15,    0.20,   0.15,   0.10,  0.10,   0.30]
4h:           [0.00,   0.10,    0.20,   0.15,   0.15,  0.05,   0.35]
24h:          [0.00,   0.05,    0.15,   0.15,   0.25,  0.05,   0.35]
48h:          [0.00,   0.00,    0.15,   0.15,   0.25,  0.00,   0.45]
72h:          [0.00,   0.00,    0.10,   0.15,   0.25,  0.00,   0.50]
1w:           [0.00,   0.00,    0.10,   0.10,   0.30,  0.00,   0.50]
2w:           [0.00,   0.00,    0.10,   0.10,   0.30,  0.00,   0.50]
```

Key shift: ELM dominates short-term (5m-15m), momentum and funding dominate long-term (24h+). ELM weight goes to zero for 1h+ because it's trained on 5-min window resolution only.

### 3.4 Cascade-Aware Weight Modulation

When cascade strength S > 0.80, weights shift dynamically:
- Cascade and momentum weights increase (cascade is the dominant regime)
- CG flow and OI weights decrease (they lag during fast cascades)
- Re-normalized to sum to 1.0

### 3.5 Derived Metrics

**Trend Strength** [0, 1]: How aligned are all timescales? 1.0 = all agree on direction. 0.0 = conflicting. Computed from sign agreement across all 9 views.

**Macro Alignment** [-1, +1]: Average of 24h+ timescale scores. Represents the broad directional bias. Used by Binance strategy to veto counter-trend trades and adjust stop widths.

### 3.6 Multi-Timescale Architecture

| Timescale | Update Freq | Data Source | Price Lookback | Primary Use |
|-----------|-------------|-------------|----------------|-------------|
| 5m | 1Hz | In-memory | 300s returns | Polymarket entry, Binance scalp |
| 15m | 0.5Hz | In-memory | 900s returns | Binance entry timing |
| 1h | 0.1Hz (10s) | In-memory | 3600s returns | Hold/trail decisions |
| 4h | 0.05Hz (20s) | In-memory | 14400s returns | Session directional bias |
| 24h | 0.01Hz (60s) | DB query | 86400s returns | Daily regime filter |
| 48h | 0.01Hz (60s) | DB query | 172800s returns | Multi-day trend |
| 72h | 0.005Hz (120s) | DB query | 259200s returns | Weekly cycle context |
| 1w | 0.005Hz (120s) | DB query | 604800s returns | Macro bias |
| 2w | 0.005Hz (120s) | DB query | 1209600s returns | Broadest context |

**Two-tier data strategy:**
- **In-memory (5m-4h):** Uses existing PriceFeed 2048-tick buffer and CoinGlass feature cache. <5ms scoring latency.
- **DB-backed (24h-2w):** Queries `ticks_binance` and `ticks_coinglass` with 60s result caching. Tolerates latency because macro signals don't change per-second.

### 3.7 Cascade State Estimator

Implements the v10.2 Novak-Tyson proposal. Fits the universal cascade response to the rolling price buffer:

```
R(lag) = A₁·exp(-lag/τ₁) + A₂·exp(-lag/τ₂)

S = (τ₂ - τ₁) / τ₂         cascade strength [0.5, 0.99]
exhaustion_t = τ₂ · ln(A₂/noise)   seconds until cascade dies
```

**Method:** Prony's method (closed-form linear algebra, ~2ms). No scipy.optimize on the hot path. Consumes the existing 2048-tick PriceFeed buffer — no new data sources.

**Output: CascadeState**
- `S` — cascade strength [0.5, 0.99]
- `tau1` — fast timescale (seconds)
- `tau2` — slow timescale (seconds)
- `cascade_direction` — "UP" or "DOWN"
- `exhaustion_t` — seconds until cascade signal exhausts
- `tau_ratio` — τ₂/τ₁ (compare to predicted 6.0)

### 3.8 WebSocket Protocol

```
WS /v3/signal?asset=BTC

Client → Server:
  {"type": "subscribe", "asset": "BTC"}
  {"type": "vpin_update", "vpin": 0.62, "regime": "NORMAL"}

Server → Client (1-2Hz):
  {
    "type": "composite",
    "ts": 1712700000000,
    "asset": "BTC",
    "scores": {"5m": 0.72, "15m": 0.55, "1h": 0.30, "4h": -0.15, 
               "24h": 0.40, "48h": 0.35, "72h": 0.28, "1w": 0.42, "2w": 0.38},
    "components": {"elm": 0.56, "cascade": 0.85, "cg_flow": 0.40, 
                   "cg_oi": 0.20, "funding": -0.10, "vpin": 0.24, "momentum": 0.30},
    "cascade": {"S": 0.91, "tau1": 8.2, "tau2": 52.0, "dir": "UP", "exhaust": 145.0},
    "macro_alignment": 0.37,
    "trend_strength": 0.68,
    "entry": {"signal": true, "direction": "LONG", "stop_pct": 0.003, "trail_pct": 0.002}
  }
```

### 3.9 New Files (TimesFM Repo)

| File | Lines (est) | Purpose |
|------|-------------|---------|
| `app/v3_cascade_estimator.py` | ~150 | Prony-fit cascade state from price buffer |
| `app/v3_composite_scorer.py` | ~250 | Fuses all signals → [-1,+1] per timescale |
| `app/v3_multiscale.py` | ~200 | Manages 9 timescale scoring loops |
| `app/v3_macro_store.py` | ~150 | DB-backed historical aggregates for 24h+ |
| `app/v3_routes.py` | ~200 | WS /v3/signal + GET /v3/snapshot |
| `app/v3_db_writer.py` | ~150 | Batched writes to ticks_v3_composite |

### 3.10 DB Table: `ticks_v3_composite`

```sql
CREATE TABLE ticks_v3_composite (
    ts          TIMESTAMPTZ NOT NULL,
    asset       TEXT NOT NULL,
    score_5m    FLOAT, score_15m   FLOAT, score_1h    FLOAT,
    score_4h    FLOAT, score_24h   FLOAT, score_48h   FLOAT,
    score_72h   FLOAT, score_1w    FLOAT, score_2w    FLOAT,
    cascade_s   FLOAT, cascade_tau1 FLOAT, cascade_tau2 FLOAT,
    cascade_dir TEXT,  cascade_exhaust FLOAT,
    macro_alignment FLOAT, trend_strength FLOAT,
    elm_p_up    FLOAT,
    components  JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_v3_composite_ts ON ticks_v3_composite (asset, ts DESC);
```

---

## 4. Binance Margin Execution (Engine)

### 4.1 Position Lifecycle

```
IDLE → ENTRY → HOLDING → EXIT → IDLE
         ↑                   │
         └───────────────────┘  (can re-enter immediately)
```

Unlike Polymarket (place order → wait → resolve), Binance margin is continuous with active position management.

### 4.2 Entry Logic

Entry requires composite score crossing a threshold with macro confirmation:

1. **Minimum conviction:** |5m score| ≥ 0.45
2. **Macro alignment filter:** Don't trade against strong macro trend (|macro_alignment| > 0.30 opposing)
3. **Cascade exhaustion filter:** Don't enter late in a dying cascade (S > 0.85 AND exhaustion_t < 30s)
4. **Fee hurdle:** Marginal signals (|score| < 0.55 AND trend_strength < 0.50) won't clear 0.20% round-trip fees

### 4.3 Position Sizing

Adaptive based on conviction × macro alignment × cascade state:

```
base = bankroll × bet_fraction (5%)
conviction_mult = 0.6 + 0.9 × (|score| - 0.45) / 0.55    [0.6x at threshold, 1.5x at max]
macro_mult = 1.0 + 0.30 × max(0, trend_strength - 0.40)   [up to +30% when aligned]
cascade_mult = 1.20 if S > 0.85 else 1.0                   [+20% during strong cascade]

stake = base × conviction_mult × macro_mult × cascade_mult
stake = min(stake, BINANCE_MAX_STAKE)                       [hard cap]
```

### 4.4 Exit Logic (Profit Maximizer)

Four concurrent exit mechanisms, checked every tick (~1Hz):

| Exit | Condition | Purpose |
|------|-----------|---------|
| **Hard stop-loss** | unrealized < -stop_pct | Cut losses fast |
| **Trailing stop** | profit > 0.1% AND drawdown from peak > trail_pct | Lock in profits |
| **Signal reversal** | Composite 5m score flipped opposing (< -0.20 for longs) | Model says regime changed |
| **Cascade exhaustion** | S dropped < 0.70 AND holding > 60s AND profitable | Cascade-driven entry, cascade is dying |

### 4.5 Adaptive Stop/Trail Widths

Stops adapt to market conditions:

- **Macro-aligned:** Wider stops (1.5x) — let it breathe, trend is in your favor
- **Counter-macro:** Default stops — tighter risk management
- **High cascade (S > 0.90):** Tighter trail (0.7x) — capture the spike precisely
- **High trend strength (> 0.70):** Wider trail (1.3x) — let runners run

### 4.6 BinanceMarginClient

Authenticated REST client for Binance cross-margin:

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `connect()` | Account verification | Verify API keys, check margin status |
| `get_margin_balance()` | `GET /sapi/v1/margin/account` | Free USDT, margin level |
| `place_margin_order()` | `POST /sapi/v1/margin/order` | Entry/exit orders (MARKET or LIMIT) |
| `borrow()` | `POST /sapi/v1/margin/loan` | Borrow for short positions |
| `repay()` | `POST /sapi/v1/margin/repay` | Repay after closing short |
| `get_open_orders()` | `GET /sapi/v1/margin/openOrders` | Check resting orders |
| `cancel_order()` | `DELETE /sapi/v1/margin/order` | Cancel resting |
| `place_oco()` | `POST /sapi/v1/margin/order/oco` | Bracket order for exit |

Paper mode simulates fills with ±0.5% random slippage (same pattern as existing PolymarketClient).

### 4.7 Fee Structure (UK VIP 0)

| Cost | Rate | Per trade ($18.75 exposure @ 5x) |
|------|------|----------------------------------|
| Maker fee (entry) | 0.10% | $0.019 |
| Maker fee (exit) | 0.10% | $0.019 |
| Round-trip | 0.20% | $0.038 |
| Borrow interest (USDT) | ~0.03%/day | ~$0.0002 (5 min hold) |
| **Total per trade** | ~0.20% | **~$0.038** |

The fee hurdle means BTC must move >0.20% for the trade to be profitable. This is why the strategy holds for trailing profits rather than scalping fixed 5-min windows.

### 4.8 Wiring Into Orchestrator

Minimal touch — new feed + strategy added as tasks:

```python
# New: v3 signal feed (WS consumer from timesfm)
self._v3_feed = V3SignalFeed(url="ws://timesfm:8080/v3/signal", on_signal=self._on_v3_signal)

# New: Binance strategy (behind feature flag)
if config.get_bool("binance.enabled"):
    self._binance_client = BinanceMarginClient(...)
    self._binance_strategy = BinanceContinuousStrategy(client, config)
```

### 4.9 New Files (Engine)

| File | Lines (est) | Purpose |
|------|-------------|---------|
| `engine/data/feeds/v3_signal_feed.py` | ~100 | WebSocket consumer for composite signal |
| `engine/execution/binance_margin.py` | ~300 | Authenticated Binance margin REST client |
| `engine/strategies/binance_continuous.py` | ~350 | Continuous position management strategy |
| `engine/services/config_service.py` | ~120 | DB config reader with polling |
| `engine/persistence/binance_recorder.py` | ~80 | Tick storage for margin trades |

### 4.10 DB Table: `ticks_binance_margin`

```sql
CREATE TABLE ticks_binance_margin (
    ts              TIMESTAMPTZ NOT NULL,
    asset           TEXT NOT NULL,
    direction       TEXT,
    entry_price     FLOAT,
    exit_price      FLOAT,
    quantity        FLOAT,
    pnl_usd        FLOAT,
    pnl_pct         FLOAT,
    hold_seconds    FLOAT,
    exit_reason     TEXT,
    entry_score_5m  FLOAT,
    entry_score_15m FLOAT,
    macro_alignment FLOAT,
    trend_strength  FLOAT,
    cascade_s       FLOAT,
    fees_usd        FLOAT,
    borrow_cost_usd FLOAT,
    net_pnl_usd     FLOAT,
    signal_snapshot JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

### 4.11 Existing `trades` Table Enhancement

```sql
ALTER TABLE trades ADD COLUMN IF NOT EXISTS venue TEXT DEFAULT 'polymarket';
-- Values: 'polymarket', 'binance_margin'
```

---

## 5. DB-Configurable Runtime Config

### 5.1 `system_config` Table

```sql
CREATE TABLE system_config (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    category    TEXT NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_by  TEXT DEFAULT 'system'
);

CREATE TABLE system_config_history (
    id          SERIAL PRIMARY KEY,
    key         TEXT NOT NULL,
    old_value   JSONB,
    new_value   JSONB NOT NULL,
    updated_by  TEXT,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
```

### 5.2 Config Categories

**Polymarket (`polymarket`):**
- `poly.bet_fraction` — 0.075 (7.5% of bankroll)
- `poly.max_bet_usd` — 10.0
- `poly.max_entry_price` — 0.80
- `poly.normal_min_offset` — 100 (v10.4)
- `poly.transition_down_max_offset` — 140 (v10.4)
- `poly.down_penalty` — 0.03
- `poly.regime_threshold_normal` — 0.65
- `poly.regime_threshold_cascade` — 0.72
- `poly.regime_threshold_transition` — 0.70

**Binance (`binance`):**
- `binance.enabled` — false (master switch)
- `binance.paper_mode` — true
- `binance.bet_fraction` — 0.05
- `binance.max_stake_usd` — 20.0
- `binance.leverage` — 5
- `binance.entry_threshold` — 0.45
- `binance.stop_pct` — 0.003
- `binance.trail_pct` — 0.002
- `binance.cooldown_after_loss` — 30
- `binance.macro_veto_threshold` — 0.30

**Composite (`composite`):**
- `composite.weight_elm` — 0.35
- `composite.weight_cascade` — 0.20
- `composite.weight_cg_flow` — 0.15
- `composite.weight_cg_oi` — 0.10
- `composite.weight_funding` — 0.05
- `composite.weight_vpin` — 0.10
- `composite.weight_momentum` — 0.05
- `composite.cascade_modulation` — true

**Risk (`risk`):**
- `risk.max_drawdown_kill` — 0.45
- `risk.daily_loss_limit_pct` — 0.10
- `risk.consecutive_loss_cooldown` — 3
- `risk.cooldown_seconds` — 900

### 5.3 ConfigService

Both repos use the same pattern:
- Reads all config from DB on startup
- Caches in-memory (reads are <1ms)
- Polls DB every 10 seconds for changes
- No restart required for config changes

---

## 6. Frontend Dashboard

### 6.1 New Pages

**`/dashboard/composite`** — Live Composite Score
- Horizontal score bars for all 9 timescales, colored red↔green
- Trend strength, macro alignment, cascade S cards
- Component breakdown (5m view): ELM, cascade, CG flow, OI, funding, VPIN, momentum
- Entry signal indicator with stop/trail suggestions
- Score history chart (Recharts, last 1-24h selectable)

**`/dashboard/macro`** — Multi-Timescale Heatmap
- Grid: 9 timescales × time columns (now, -1h, -2h, -4h, -8h, -12h, -24h)
- Each cell colored by score intensity
- Cascade state timeline (sparkline)
- Funding rate trend chart
- Session analysis: automatic WR breakdown by time-of-day

**`/dashboard/binance`** — Binance Margin
- Current position card (if open): direction, entry, unrealized P&L, hold time, stop/trail levels
- Trade history table with filters (direction, exit reason, P&L)
- P&L chart (cumulative, daily)
- Margin account balance + margin level

**`/dashboard/config`** — Configuration Editor
- Grouped by category (Polymarket, Binance, Composite, Risk)
- Inline editing with save button
- Toggle switches for boolean values
- Sliders for numeric ranges
- Config change history log at bottom

### 6.2 New Hub API Endpoints

```
GET  /api/config/all                   — All configs grouped by category
GET  /api/config/category/:category    — Single category
PUT  /api/config/:key                  — Update single value
GET  /api/config/history               — Change audit log

GET  /api/composite/snapshot           — Current composite scores
GET  /api/composite/history?hours=24   — Historical for charts
WS   /ws/composite                     — Real-time composite (proxied)

GET  /api/binance/position             — Current open position
GET  /api/binance/trades               — Trade history
GET  /api/binance/balance              — Margin account
```

### 6.3 Tech Stack

Unchanged from existing: React 18, Vite, Tailwind CSS (dark theme), Recharts, JWT auth. Same aesthetic as the executive HQ dashboard.

---

## 7. Data Architecture

### 7.1 New Tables

| Table | Repo | Write Freq | Purpose |
|-------|------|-----------|---------|
| `ticks_v3_composite` | timesfm | 1Hz (5m) + lower for macro | Full composite history |
| `ticks_binance_margin` | engine | Per trade open/close | Binance execution records |
| `system_config` | both | On change | Runtime config store |
| `system_config_history` | both | On change | Config audit trail |

### 7.2 Existing Tables (Unchanged)

All existing tick tables (`ticks_binance`, `ticks_coinglass`, `ticks_chainlink`, `ticks_tiingo`, `ticks_gamma`, `ticks_timesfm`, `ticks_v2_probability`, `ticks_clob`) remain unchanged. The `trades` table gets a `venue` column. `trade_bible` remains Polymarket-only.

---

## 8. Feature Flags & Config

All new functionality behind env vars for initial deployment:

```bash
# TimesFM repo
V3_ENABLED=true                      # Enable composite scoring
V3_CASCADE_ENABLED=true              # Enable cascade estimator
V3_MACRO_ENABLED=true                # Enable 24h+ DB-backed views

# Engine
BINANCE_MARGIN_ENABLED=false         # Master switch
BINANCE_PAPER_MODE=true              # Paper trade first
V3_SIGNAL_URL=ws://timesfm:8080/v3/signal  # Composite feed URL
```

After initial validation, these migrate to `system_config` for runtime control.

---

## 9. Deployment Sequence

### Phase 1: TimesFM v3 (Signal Brain)
- Implement cascade estimator + composite scorer
- Add multiscale manager + macro store
- Add /v3/signal WS + /v3/snapshot REST
- Add ticks_v3_composite table + writer
- Deploy to EC2 Montreal
- **Verify:** Composite stream works, scores look sane, no impact on /v2/probability

### Phase 2: Engine Integration (Execution)
- Add V3SignalFeed (WS consumer)
- Add BinanceMarginClient (paper + live)
- Add BinanceContinuousStrategy
- Add ConfigService + system_config table
- Wire into orchestrator behind feature flag
- **Verify:** Paper trades executing, composite consumed correctly

### Phase 3: Dashboard
- Add composite page + macro heatmap
- Add Binance position/history page
- Add config editor page
- Extend Hub API with new endpoints
- **Verify:** All pages render, config changes propagate

### Phase 4: Paper Trading Validation (1 week)
- Run both venues in parallel (Polymarket live, Binance paper)
- Compare composite entry signals vs gate decisions
- Validate exit logic (stops, trails, reversals)
- Tune thresholds, sizing, stop widths from dashboard
- **Target:** 1 week of paper data before live

### Phase 5: Live Margin Trading
- `binance.enabled=true`, `binance.paper_mode=false`
- Start with `binance.max_stake_usd=5.0` (small)
- Monitor fill quality, slippage, borrow costs
- Ramp up stake over 1 week via config editor
- Full production

---

## 10. Performance Insights from Live Data

### 10.1 Overnight Session 2026-04-09

| Period | W | L | WR | PnL | Insight |
|--------|---|---|-----|-----|---------|
| Evening (20-00 UTC) | 12 | 18 | 40% | -$45.82 | High-vol chop, model collapsed |
| Overnight (00-08 UTC) | 34 | 0 | 100% | +$44.61 | Clean trending, model dominant |

**Key insight:** The 4h/24h macro context would have flagged the evening session as high-vol/choppy (conflicting timescales, trend_strength near 0) and reduced sizing or paused trading. The overnight session had aligned timescales (trend_strength near 1.0) — full conviction trading.

### 10.2 Config State (as of commit `1baf811`)

- BET_FRACTION = 7.5%, ABSOLUTE_MAX_BET = $10, STARTING_BANKROLL = $115
- Wallet ~$122 (73% WR overnight, doubled from ~$46)
- All trades at $0.68 entry, $3.40 stake, $1.60 profit per win

### 10.3 Design Implications

1. **Session-aware sizing** via macro timescales is the highest-impact feature
2. **The model works** — 100% overnight WR proves the signal is strong in the right conditions
3. **The losses are regime-dependent** — evening drawdowns are preventable with macro context
4. **DB-configurable risk** would have let you reduce exposure during the drawdown in real-time

---

## 11. Risk Analysis

### 11.1 Binance-Specific Risks

| Risk | Mitigation |
|------|-----------|
| Liquidation on 5x margin | Hard stop-loss at 0.3% (well within margin maintenance) |
| Borrow rate spike | Monitor via CoinGlass funding, auto-pause if borrow > threshold |
| API rate limiting | Client-side rate limiter, exponential backoff |
| Fill slippage on MARKET orders | Use LIMIT orders as maker when composite score is strong but not urgent |
| UK FCA enforcement | Margin is grey area, not derivatives. Monitor regulatory updates. |

### 11.2 System Risks

| Risk | Mitigation |
|------|-----------|
| TimesFM service down | Engine falls back to existing gate pipeline (Polymarket unaffected). Binance strategy pauses (no composite = no entries, existing positions held with last-known stops). V3SignalFeed auto-reconnects with exponential backoff. |
| Composite score miscalibrated | Paper mode first, ramp slowly, config-adjustable weights |
| Cascade estimator noise | Prony method has warm-start defaults, clipped outputs [0.5, 0.99] |
| DB config corruption | Config history table enables rollback, env var overrides always available |

---

## 12. Success Criteria

1. **Composite signal:** Scores correlate with trade outcomes (>70% accuracy when |score| > 0.45)
2. **Binance paper mode:** Positive expected value after fees over 1 week
3. **Macro context:** Evening sessions (20-00 UTC) show reduced exposure when trend_strength < 0.40
4. **Config editor:** All parameters adjustable from dashboard with <10s propagation
5. **Dashboard:** All 4 new pages render correctly with live data
6. **Zero regression:** Polymarket strategy unaffected (same WR, same fill rate)
