# Hub API Reference

The Hub is a FastAPI application serving REST and WebSocket endpoints. All endpoints require JWT authentication except `/auth/login`.

**Base URL:** `https://<hub>.railway.app`

---

## Authentication

### `POST /auth/login`

Obtain JWT tokens.

**Request:**
```json
{
  "username": "admin",
  "password": "<password>"
}
```

**Response:**
```json
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "token_type": "bearer"
}
```

Access tokens expire in 15 minutes. Use the refresh token to get new access tokens.

All subsequent requests require:
```
Authorization: Bearer <access_token>
```

---

## v5.8 / v7.1 Monitor Endpoints

### `GET /api/v58/windows`

Recent window snapshots with all signal fields.

**Query params:**
- `limit` — number of windows (1–200, default 50)
- `asset` — filter by asset (e.g., `BTC`)

**Response:**
```json
{
  "windows": [
    {
      "window_ts": "2026-04-05T18:00:00+00:00",
      "asset": "BTC",
      "timeframe": "5m",
      "open_price": 83000.5,
      "close_price": 83050.2,
      "delta_pct": 0.0006,
      "vpin": 0.58,
      "regime": "TRANSITION",
      "direction": "UP",
      "confidence": 0.72,
      "trade_placed": true,
      "skip_reason": null,
      "twap_direction": "UP",
      "twap_agreement_score": 7,
      "twap_gamma_gate": "OK",
      "timesfm_direction": "UP",
      "timesfm_confidence": 0.81,
      "timesfm_predicted_close": 83100.0,
      "timesfm_agreement": true,
      "gamma_up_price": 0.52,
      "gamma_down_price": 0.48,
      "engine_version": "v7.1",
      "v71_would_trade": true,
      "v71_skip_reason": null,
      "v71_regime": "TRANSITION",
      "v71_correct": true,
      "v71_pnl": 2.35
    }
  ]
}
```

---

### `GET /api/v58/outcomes`

Per-window outcome analysis with what-if P&L calculations.

**Query params:**
- `limit` — number of windows (1–500, default 100)
- `asset` — filter by asset

**Response:**
```json
{
  "outcomes": [
    {
      "window_ts": "2026-04-05T18:00:00+00:00",
      "actual_direction": "UP",
      "gamma_implied_direction": "DOWN",
      "timesfm_correct": true,
      "v57c_correct": true,
      "twap_correct": true,
      "v58_would_trade": true,
      "v58_correct": true,
      "v58_pnl": 2.35,
      "ungated_pnl": 2.35,
      "poly_outcome": "WIN",
      "resolution_source": "polymarket",
      "v71_would_trade": true,
      "v71_correct": true,
      "v71_pnl": 2.35
    }
  ],
  "count": 100
}
```

---

### `GET /api/v58/accuracy`

Rolling accuracy statistics across recent windows.

**Query params:**
- `limit` — number of windows to analyse (10–500, default 100)
- `asset` — filter by asset

**Response:**
```json
{
  "windows_analysed": 100,
  "timesfm_accuracy": 72.5,
  "v57c_accuracy": 68.3,
  "twap_accuracy": 65.1,
  "gamma_accuracy": 71.2,
  "v58_accuracy": 74.8,
  "v58_trades_count": 43,
  "agreement_rate": 68.0,
  "cumulative_pnl": 45.20,
  "ungated_pnl": 28.40,
  "ungated_accuracy": 68.3,
  "ungated_wins": 41,
  "ungated_losses": 19,
  "gate_value": 16.80,
  "current_streak": 3,
  "pnl_timeline": [...],
  "resolution_sources": {
    "polymarket": 85,
    "binance_t60": 15
  },
  "v71_accuracy": 74.8,
  "v71_trades_count": 43,
  "v71_wins": 32,
  "v71_losses": 11,
  "v71_pnl": 45.20,
  "v71_streak": 3
}
```

---

### `GET /api/v58/stats`

Aggregate window stats for a time period.

**Query params:**
- `days` — number of days to cover (1–90, default 7)

**Response:**
```json
{
  "period_days": 7,
  "since": "2026-03-29T...",
  "total_windows": 2016,
  "trades_placed": 412,
  "windows_skipped": 1604,
  "explicit_skips": 1482,
  "trade_rate_pct": 20.4,
  "timesfm": {
    "evaluated": 1950,
    "agreed": 1365,
    "disagreed": 585,
    "agreement_rate_pct": 70.0
  },
  "direction": {
    "up": 1018,
    "down": 998
  },
  "confidence": {
    "avg": 0.71,
    "min": 0.30,
    "max": 0.99
  },
  "twap": {
    "gate_passed": 980,
    "avg_agreement_score": 6.2
  }
}
```

---

### `GET /api/v58/price-history`

BTC OHLC price history for charting.

**Query params:**
- `minutes` — history depth (5–1440, default 60)

**Response:**
```json
{
  "candles": [
    {
      "time": 1743868800,
      "open": 83000.0,
      "high": 83100.0,
      "low": 82950.0,
      "close": 83050.0,
      "delta_pct": 0.0006,
      "vpin": 0.58,
      "direction": "UP",
      "trade_placed": true
    }
  ],
  "source": "window_snapshots",
  "count": 12
}
```

---

### `GET /api/v58/gate-analysis`

Win rate breakdown by VPIN level — used to tune the VPIN gate.

**Response:**
```json
{
  "buckets": [
    {
      "vpin_range": "0.45-0.55",
      "eligible": 120,
      "wins": 78,
      "losses": 42,
      "wr_pct": 65.0,
      "pnl": 42.30
    }
  ],
  "cumulative": [...],
  "overall_wr": 71.2,
  "total_wins": 280,
  "total_losses": 113,
  "total_pnl": 145.60,
  "current_gate": 0.45,
  "best_gate": {...},
  "suggestion": "Current gate is performing well at 71.2% WR."
}
```

---

### `GET /api/v58/countdown/{window_ts}`

Evaluation stages for a specific window (T-180, T-120, T-90, T-60).

**Path params:**
- `window_ts` — ISO 8601 timestamp or unix timestamp

**Response:**
```json
{
  "window_ts": "2026-04-05T18:00:00+00:00",
  "evaluations": [
    {
      "stage": "T-60",
      "evaluated_at": "2026-04-05T18:04:00+00:00",
      "direction": "UP",
      "confidence": 0.72,
      "agreement": true,
      "action": "TRADE",
      "notes": "VPIN 0.58, delta +0.06%, TimesFM agrees"
    }
  ]
}
```

---

### `GET /api/v58/window-detail/{window_ts}`

Full detail for a single window including price ticks, entry timing, and what-if P&L.

**Path params:**
- `window_ts` — ISO 8601 or unix timestamp

**Response includes:**
- `snapshot` — full window_snapshot row with outcome
- `evaluations` — countdown stage evaluations
- `price_ticks` — BTC price through the window
- `entry_timing` — Gamma prices and TimesFM at each stage (T-240 to T-60)
- `what_if` — P&L scenarios for all signal sources

---

### `GET /api/v58/live-prices`

Real-time UP/DOWN prices from Polymarket Gamma API.

**Query params:**
- `window_ts` — specific window (optional, defaults to current)

**Response:**
```json
{
  "up_price": 0.52,
  "down_price": 0.48,
  "spread": 0.04,
  "timestamp": "2026-04-05T18:04:00+00:00",
  "up_bet": {
    "entry": 0.52,
    "stake": 4.0,
    "shares": 7.69,
    "win_pnl": 1.88,
    "loss_pnl": -2.08,
    "breakeven_pct": 52.0
  },
  "down_bet": {...}
}
```

---

### `POST /api/v58/manual-trade`

Place a manual paper or live trade for the current window.

**Request:**
```json
{
  "asset": "BTC",
  "direction": "UP",
  "mode": "paper",
  "window_ts": 1743868800
}
```

**Response:**
```json
{
  "trade_id": "manual_abc123",
  "direction": "UP",
  "entry_price": 0.52,
  "gamma_up_price": 0.52,
  "gamma_down_price": 0.48,
  "stake": 4.0,
  "mode": "paper",
  "status": "open"
}
```

---

### `GET /api/v58/manual-trades`

List all manual trades with outcomes.

---

## Other API Endpoints

### Dashboard

- `GET /api/dashboard` — Aggregated overview: recent trades, win rate, current balance, feed status

### Trades

- `GET /api/trades` — Trade history with filters
- `GET /api/trades/{id}` — Single trade detail
- `GET /api/pnl/daily` — Daily P&L breakdown
- `GET /api/pnl/equity` — Equity curve data

### Signals

- `GET /api/signals` — Recent signals (VPIN, cascade events)
- `GET /api/signals/vpin` — VPIN history

### Config

- `GET /api/config` — Current runtime config
- `POST /api/config` — Update runtime config (live, no restart needed)
- `GET /api/trading-configs` — List saved configs
- `POST /api/trading-configs` — Create new config
- `POST /api/trading-configs/{id}/activate` — Activate a config

### System

- `GET /api/system/status` — Engine status, feed connections, heartbeat
- `POST /api/system/paper-mode` — Toggle paper/live mode

### Forecasts

- `GET /api/forecast/timesfm` — Latest TimesFM forecast
- `GET /api/forecast/history` — Historical forecast accuracy

### Analysis

- `GET /api/analysis` — AI analysis documents
- `POST /api/analysis` — Upload new analysis

### Backtest

- `GET /api/backtest` — Backtest run history
- `POST /api/backtest/run` — Run a new backtest

---

## WebSocket

### `WS /ws/live`

Real-time event stream. Connect with a valid JWT.

**Connection:**
```
wss://<hub>.railway.app/ws/live?token=<access_token>
```

**Event types:**
- `window_update` — New window snapshot
- `trade_placed` — Trade placed by engine
- `trade_resolved` — Trade outcome known
- `vpin_update` — VPIN tick
- `system_status` — Engine heartbeat
- `cascade_state` — Cascade FSM state change
