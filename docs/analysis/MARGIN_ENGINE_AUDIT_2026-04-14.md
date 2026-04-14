# Margin Engine Status Audit — 2026-04-14

**Auditor:** Claude (automated audit)
**Date:** 2026-04-14
**Host:** 18.169.244.162:8090 (AWS eu-west-2)
**Service:** Novakash Margin Engine (systemd: margin-engine.service)

---

## 1. Deployment Status

### Last 5 CI Deploys (`.github/workflows/deploy-margin-engine.yml`)

| Run ID | Result | Date | Branch |
|--------|--------|------|--------|
| 24399780591 | SUCCESS | 2026-04-14T12:48:23Z | develop |
| 24367730098 | SUCCESS | 2026-04-13T21:26:26Z | develop |
| 24342288246 | FAIL | 2026-04-13T12:02:36Z | develop |
| 24316229963 | FAIL | 2026-04-12T20:57:01Z | develop |
| 24316218146 | FAIL | 2026-04-12T20:56:22Z | develop |

**Last successful deploy: 2026-04-14T12:48:23Z** (today, ~13:00 UTC). The service restarted cleanly and passed the 5-second health gate.

### Service State (from deploy log, run 24399780591)
- **Process state:** RUNNING (systemd `is-active` passed)
- **paper_mode=True, leverage=5x**
- **Exchange venue:** `hyperliquid` (paper)
- **Initial BTC mid price at boot:** $74,318.50
- **Fee:** 0.00045/side, spread=1.00bp

### Health Endpoint Reachability
`curl http://18.169.244.162:8090/health` — **TIMED OUT (28s)**.
The status server binds to `:8090` inside the instance, but port 8090 is not open in the AWS Security Group to external traffic. The Hub on Montreal (99.79.41.246) proxies to this endpoint via `/api/margin/status` — that proxy path is what the dashboard uses.

---

## 2. Critical Issue: Signal Source Unreachable

**Status: DEGRADED — engine is running but trading blind.**

From the deploy log:
```
[WARNING] ws_signal: Signal WS error: Cannot connect to host 3.98.114.0:8080
          [Connect call failed ('3.98.114.0', 8080)], reconnecting in 1s
[WARNING] v4_snapshot_http: V4SnapshotHttpAdapter request failed:
          Cannot connect to host 3.98.114.0:8080 ssl:default
```

The margin engine depends on two feeds from `3.98.114.0:8080` (the Strategy Hub v2 / Hub v2 on Montreal):
1. **`ws://3.98.114.0:8080/v3/signal`** — composite WS signal (used by `WsSignalAdapter` for entry decisions)
2. **`GET http://3.98.114.0:8080/v4/snapshot`** — V4 multi-timescale decision surface (used by `V4SnapshotHttpAdapter`)

Per the memory note "Strategy Engine v2 live status": **Hub at 3.98.114.0:8091 CRASHED**.

**Impact:**
- `WsSignalAdapter` is in a reconnect loop — no composite signals reaching the engine
- `V4SnapshotHttpAdapter` gets `None` on every poll — all V4 gates see no data
- `ProbabilityHttpAdapter` (against `http://16.52.14.182:8080`, the TimesFM service) IS working — BTC 15m p_up=0.484, conviction=0.016 was received at boot
- `open_position` use case is gating on conviction < 0.20 (last seen: 0.016), so **no trades are being placed**
- **The engine is effectively idle**: running, logging, rejecting every tick due to low conviction

---

## 3. Architecture Overview

### Clean Architecture Structure

The margin engine completed a clean architecture migration. Directory structure:

```
margin_engine/
├── domain/                          # Core business logic, no external deps
│   ├── entities/
│   │   ├── position.py              # Position lifecycle entity
│   │   └── portfolio.py             # Portfolio with risk gates
│   ├── ports.py                     # Abstract interfaces (exchange, repo, alerts)
│   ├── value_objects.py             # Money, Price, Leverage
│   └── strategy.py                  # Strategy base class
│
├── use_cases/                       # Application logic
│   ├── open_position.py             # Entry gate: v4 gates, conviction, regime
│   └── manage_positions.py          # Exit logic: SL/TP/trailing/expiry
│
├── adapters/                        # External-facing implementations
│   ├── exchange/
│   │   ├── binance_margin.py        # Live Binance Ed25519 signing
│   │   ├── hyperliquid_price_feed.py # HL perpetuals real-time price
│   │   └── paper.py                 # Paper trading with configurable price getter
│   ├── persistence/
│   │   ├── pg_repository.py         # margin_positions table (additive migrations)
│   │   ├── pg_log_repository.py     # margin_logs table (DB-side log sink)
│   │   └── pg_signal_repository.py  # margin_signals table (passive signal recorder)
│   ├── signal/
│   │   ├── ws_signal.py             # Composite signal WS consumer
│   │   ├── probability_http.py      # TimesFM v2 probability poller
│   │   └── v4_snapshot_http.py      # V4 multi-timescale snapshot poller
│   └── alert/
│       └── telegram.py              # Trade open/close/kill-switch Telegram alerts
│
├── services/                        # Implemented strategy services
│   ├── cascade_detector.py
│   ├── cascade_fade.py
│   ├── continuation_alignment.py
│   ├── fee_aware_continuation.py
│   ├── quantile_var_sizer.py
│   ├── regime_adaptive.py
│   ├── regime_mean_reversion.py
│   ├── regime_no_trade.py
│   └── regime_trend.py
│
├── infrastructure/
│   ├── config/
│   │   ├── settings.py              # Pydantic settings (14.3K — all env vars)
│   │   └── fee_aware_continuation_settings.py
│   └── status_server.py             # Lightweight HTTP: /status /health /logs /history
│
├── tests/
│   ├── unit/
│   │   ├── test_cascade_fade.py     (18.2K)
│   │   ├── test_fee_aware_continuation.py (17.9K)
│   │   ├── test_quantile_var_sizer.py (22.1K)
│   │   ├── test_regime_adaptive.py  (23.4K)
│   │   └── test_v4_data_flow.py     (33.0K)
│   └── use_cases/
│       ├── test_mark_divergence_gate.py
│       └── test_open_position_macro_advisory.py
│
├── docs/
│   ├── V4_STRATEGY_FOUNDATION.md
│   └── CASCADE_FADE_STRATEGY.md
│
└── main.py                          # Entry point, wires all layers
```

### Strategy Implementations (services/)

| Strategy | Status | Description |
|----------|--------|-------------|
| `fee_aware_continuation` | LIVE (feature-flagged ON) | Hold/exit based on fee-adjusted PnL |
| `continuation_alignment` | LIVE (feature-flagged ON) | Extend hold if multi-timescale aligned |
| `regime_adaptive` | Implemented, INACTIVE | Route to trend/mean-reversion/no-trade by regime |
| `regime_trend` | Implemented, INACTIVE | TRENDING_UP/DOWN: 1.2x size, 1.5% SL, 2% TP, 60m |
| `regime_mean_reversion` | Implemented, INACTIVE | MEAN_REVERTING: fade extremes, 0.8x, 15m hold |
| `regime_no_trade` | Implemented, INACTIVE | CHOPPY/NO_EDGE: skip entry |
| `cascade_fade` | Implemented, INACTIVE | Fade liquidation cascades, 0.5x, 3% SL, 1% TP |
| `quantile_var_sizer` | Implemented, INACTIVE | Risk-parity sizing 0.5x–2.0x based on VaR |

### Active Configuration (from deploy workflow env templating)

```
MARGIN_PAPER_MODE=true
MARGIN_EXCHANGE_VENUE=hyperliquid
MARGIN_ENGINE_USE_V4_ACTIONS=true
MARGIN_V4_SNAPSHOT_URL=http://3.98.114.0:8080          ← BROKEN (hub v2 down)
MARGIN_V4_PRIMARY_TIMESCALE=5m
MARGIN_V4_TIMESCALES=5m,15m,1h,4h
MARGIN_V4_STRATEGY=fee_aware_15m
MARGIN_V4_POLL_INTERVAL_S=2.0
MARGIN_ALIGNMENT_ENABLED=true
MARGIN_ALIGNMENT_MIN_TIMESCALES=3
MARGIN_FEE_AWARE_CONTINUATION_ENABLED=true
MARGIN_CONTINUATION_ALIGNMENT_ENABLED=true
MARGIN_V4_MACRO_MODE=advisory
MARGIN_V4_MACRO_HARD_VETO_CONFIDENCE_FLOOR=80
MARGIN_V4_MACRO_ADVISORY_SIZE_MULT_ON_CONFLICT=0.75
```

---

## 4. Known Bugs / Design Quirks Documented in Code

### DQ-06: paper+binance branch is intentionally broken
- `PaperExchangeAdapter` constructed without `price_getter` → `_last_price` stuck at $80,000 constant
- Hardened: deploy workflow templates `MARGIN_EXCHANGE_VENUE=hyperliquid` on every deploy
- Runtime guard: raises `RuntimeError` unless `MARGIN_ALLOW_BROKEN_PAPER_BINANCE=1` is set
- **Status:** Mitigated; current venue is hyperliquid (confirmed from deploy log)

### DQ-07: mark_divergence gate (default OFF)
- Defensive gate that vetoes entries when HL mark price diverges from signal assumptions
- Default: disabled (`MARGIN_MARK_DIVERGENCE_GATE=false`)
- **Status:** Not active; needs evaluation before enabling

### 5m pull-mode scorer broken (documented in deploy workflow)
- V5 TimesFM scorer produces constant `probability_raw=0.6201` via pull-mode GET
- Root cause: v5 trained on 25 engine-side features; pull-mode sends v4-shape features → all-NaN → LightGBM returns default constant
- **Workaround:** Deployed on `15m/nogit` model (v4-shape) which works with pull-mode
- **Fix path:** Port `v2_feature_body.py` push-mode into margin_engine (Phase B.0, not yet done)

### Live Hyperliquid trading not implemented
- `live + hyperliquid` branch raises `NotImplementedError`
- **Status:** By design — signing layer is a follow-up; engine is paper-only on HL

---

## 5. Signal Dependencies

```
WS signal feed:    ws://3.98.114.0:8080/v3/signal        ← DOWN (Hub v2 crashed)
V4 snapshot:       http://3.98.114.0:8080/v4/snapshot     ← DOWN (Hub v2 crashed)
Probability HTTP:  http://16.52.14.182:8080 (TimesFM)     ← WORKING (BTC 15m polls OK)
Price feed:        Hyperliquid info API (public)           ← WORKING ($74,318 at boot)
Database:          Railway PostgreSQL                      ← WORKING (tables ensured OK)
```

---

## 6. DB Tables

| Table | Purpose |
|-------|---------|
| `margin_positions` | Position lifecycle with additive migrations (17 statements) |
| `margin_logs` | DB-side log sink for all INFO+ log lines |
| `margin_signals` | Passive composite signal recorder (write-only, for edge analysis) |

---

## 7. Frontend Dashboard Coverage

### Implemented (from `MARGIN_STRATEGY_DASHBOARD_IMPLEMENTATION.md`)
- `/margin-strategies` page: Strategy cards, V4 panel, PnL distribution, signal histogram, hold extension analysis, partial close audit, regime performance
- Deployed 2026-04-12 as part of commit `57a60f4`

### Missing Backend Endpoints (using placeholder data)
1. `GET /api/margin/strategy-stats` — strategy performance metrics (needs `hub/api/margin.py`)
2. `GET /api/margin/positions` — position history with metadata (needs DB query in hub)
3. `GET /api/margin/strategy-config` — configuration read-back (optional)

---

## 8. Open Issues / Action Items

### CRITICAL
1. **Hub v2 at 3.98.114.0:8080 is down** — margin engine is trading blind. WS signal feed and V4 snapshot are both unreachable. The engine rejects every tick due to low conviction from TimesFM alone. Fix: restart Hub v2 on the Strategy Hub EC2 instance.

### HIGH
2. **5m push-mode wiring not done** — `probability_http.py` still uses pull-mode GET `/v2/probability`. Phase B.0 (port `v2_feature_body.py` into `margin_engine/adapters/signal`) is incomplete. Until done, V5 model is unusable; engine falls back to `15m/nogit` pull-mode.

3. **Backend endpoints for margin dashboard not implemented** — `/api/margin/strategy-stats` and `/api/margin/positions` are placeholder data. No real PnL/win-rate data is visible in the strategy dashboard.

### MEDIUM
4. **Port 8090 not reachable externally** — status server binds on `:8090` but the Security Group doesn't expose it. Hub proxy at `/api/margin/status` is the only way to get status data. Direct health checks from outside will always time out.

5. **Live Hyperliquid trading not implemented** — `live + hyperliquid` raises `NotImplementedError`. No path to go live on HL without implementing the signing layer.

6. **`margin_signals` table: 0 signals recorded** — From deploy log: `recorded=0 flushed=0 dropped=0` on shutdown. The signal recorder gets no data because `WsSignalAdapter` can't connect to the WS feed. Passive edge analysis data is not accumulating.

### LOW
7. **DQ-07 mark_divergence gate needs evaluation** — Built and tested, default OFF, not yet evaluated for activation.

8. **142 unit tests** — Test suite exists but CI doesn't run it (deploy workflow only deploys, no test step). No test failure gate before deploy.

9. **`actions/checkout@v4` Node.js 20 deprecation warning** — Will break on September 16, 2026; upgrade to `@v5` before then.

---

## 9. Git History Summary

Key commits to `margin_engine/` (most recent first):

| Commit | Description |
|--------|-------------|
| 93cab50 | infra: split TimesFM + Hub into dedicated EC2 instances |
| 57a60f4 | feat(v4-strategies): 5 V4 strategies + fee-aware continuation + dashboards (142 tests) |
| 0c61c6f | DQ-07: defensive mark_divergence gate (default OFF) |
| 4977171 | Phase A: 5m primary + macro advisory mode + 24h audit |
| 7235bb6 | DQ-06: pin paper venue to hyperliquid (3 layers) |
| 195ae12 | feat: v4 gates + folded re-prediction continuation (PR B) |
| 15e0a42 | feat: v4 snapshot adapter — dark deploy (PR A) |
| 2c864ea | feat: Hyperliquid venue + Trade Timeline tab + CI safety net |
| bb67a79 | feat: v2 ML-directed strategy, kill SIGNAL_REVERSAL fee trap |

---

## 10. Summary Assessment

| Dimension | Status |
|-----------|--------|
| Architecture | Clean arch fully implemented. Domain, use cases, adapters properly separated. |
| Deployment | Running. Last deploy 2026-04-14T12:48. systemd service active. |
| Health check (external) | UNREACHABLE (Security Group blocks port 8090 externally) |
| Trading activity | IDLE — zero trades since deploy. Conviction below threshold (0.016 < 0.20). |
| Signal feeds | DEGRADED — WS signal + V4 snapshot both down (Hub v2 crashed). Only TimesFM probability working. |
| Paper PnL | Unknown — no positions opened since Hub v2 crashed, data lost on restart. |
| Test coverage | 142 tests written. No CI test step before deploy. |
| Dashboard | Frontend exists; backend endpoints use placeholder data. |
| Go-live readiness | NOT READY — needs: Hub v2 restart, push-mode wiring, HL signing layer for live. |
