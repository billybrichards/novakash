# Data Structure — Novakash Trading System

**Last updated:** April 7, 2026 16:50 UTC
**DB:** Railway Postgres (`hopper.proxy.rlwy.net:35772`)

---

## Core Data Flow

```
Every 5 minutes:
┌──────────────────────────────────────────────────────┐
│ BTC Price Feeds (Tiingo, Chainlink, Binance)         │
│    ↓                                                 │
│ ticks_tiingo / ticks_chainlink / ticks_binance       │
│    ↓                                                 │
│ Engine evaluates 19 checkpoints (T-240 to T-60)      │
│    ↓                                                 │
│ window_snapshots (signal, VPIN, delta, regime)        │
│ gate_audit (19 rows: each checkpoint pass/fail)       │
│ window_predictions (Tiingo/Chainlink/signal dirs)     │
│    ↓                                                 │
│ Gates pass? → trades (CLOB order placed)              │
│ Gates fail? → skip_reason recorded                    │
│ Bid unfilled? → bid_unfilled = true                   │
│    ↓                                                 │
│ ticks_clob (book prices during fill poll)             │
│    ↓                                                 │
│ Oracle resolves → window_predictions.oracle_winner    │
│                 → window_snapshots.poly_winner        │
│                 → trades.outcome (WIN/LOSS)            │
│    ↓                                                 │
│ AI Evaluator (Railway) → telegram_notifications       │
└──────────────────────────────────────────────────────┘
```

---

## Tables — What Lives Where

### window_predictions (NEW — 201 rows, complete for Apr 7)

**Purpose:** Single source of truth for "what did we predict vs what happened?"

| Column | Type | Description |
|--------|------|-------------|
| window_ts | BIGINT | Window start epoch (PK with asset+timeframe) |
| tiingo_direction | VARCHAR(4) | UP/DOWN based on Tiingo close > open |
| chainlink_direction | VARCHAR(4) | UP/DOWN based on Chainlink close > open |
| our_signal_direction | VARCHAR(4) | What our engine's VPIN+delta signal said |
| v2_direction | VARCHAR(4) | What TimesFM v2.2 predicted |
| v2_probability | FLOAT | v2.2's calibrated probability |
| oracle_winner | VARCHAR(4) | What ACTUALLY happened (Polymarket oracle) |
| tiingo_correct | BOOL | Did Tiingo predict correctly? |
| chainlink_correct | BOOL | Did Chainlink predict correctly? |
| our_signal_correct | BOOL | Did our signal predict correctly? |
| trade_placed | BOOL | Did we actually trade this window? |
| bid_unfilled | BOOL | Did we bid but get no fill? |
| skip_reason | TEXT | Why we skipped (if skipped) |
| gate_summary | TEXT | "18/19 passed | TRADE at T-70" |
| gates_total | INT | How many checkpoints evaluated |
| gates_passed | INT | How many passed |
| gate_trade_offset | INT | Which offset triggered the trade |
| vpin_at_close | FLOAT | VPIN at window close |
| regime | VARCHAR(15) | CASCADE/TRANSITION/NORMAL/CALM |

**Accuracy (200 resolved windows, Apr 7):**

| Source | Accuracy | N |
|--------|----------|---|
| Chainlink | 92.0% | 200 |
| Tiingo | 81.5% | 200 |
| Our Signal | 65.0% | 200 |

### window_snapshots (2,541 rows)

**Purpose:** Full evaluation data for every window.

Key columns: `vpin`, `delta_pct`, `direction`, `regime`, `trade_placed`, `skip_reason`, `poly_winner`, `delta_tiingo`, `delta_chainlink`, `delta_binance`, `v2_probability_up`, `v2_direction`

### gate_audit (1,627 rows)

**Purpose:** Per-checkpoint gate decisions (up to 19 per window).

Key columns: `eval_offset`, `decision` (TRADE/SKIP), `gate_failed`, `vpin`, `delta_pct`

**Join:** `gate_audit.window_ts = window_predictions.window_ts`

### trades (1,263 rows)

**Purpose:** Every CLOB order placed. Ground truth for P&L.

Key columns: `direction`, `entry_price`, `stake_usd`, `outcome` (WIN/LOSS), `pnl_usd`, `status` (OPEN/EXPIRED/RESOLVED_WIN/RESOLVED_LOSS)

**metadata JSONB:** `actual_fill_price`, `size_matched`, `v81_entry_cap`, `entry_reason`, `clob_order_id`, `clob_status`, `window_ts`

### telegram_notifications (539 rows)

**Purpose:** Every notification sent. Audit trail.

Types: `window_summary`, `trade_decision_v8`, `outcome_v8`, `raw_message` (SITREP), `ai_window_eval` (new from macro-observer), `shadow_resolution`

---

## Window States

Each 5-min window ends up in one of 4 states:

| State | trade_placed | bid_unfilled | In trades table? |
|-------|-------------|--------------|-----------------|
| **SKIPPED** | false | false | No |
| **BID UNFILLED** | false* | true | Yes (status=EXPIRED) |
| **TRADED + WON** | true | false | Yes (outcome=WIN) |
| **TRADED + LOST** | true | false | Yes (outcome=LOSS) |

*bid_unfilled: signal passed gates, CLOB order placed, but no counterparty filled within 60s.

---

## Gate Config (v8.1.2 — live)

```
Offset          Cap     Requirement
T-240..T-180    $0.55   CASCADE (VPIN≥0.65) + delta≥5bp + v2.2 HIGH + agrees
T-170..T-120    $0.60   CASCADE (VPIN≥0.65) + delta≥5bp + v2.2 HIGH + agrees
T-110..T-80     $0.65   TRANSITION+ (VPIN≥0.55) + v2.2 HIGH + agrees
T-70..T-60      $0.73   TRANSITION+ (VPIN≥0.55) + v2.2 HIGH + agrees
```

---

## Price Feed Tables

| Table | Rows | Size | Source | Frequency |
|-------|------|------|--------|-----------|
| ticks_tiingo | — | 27MB | Tiingo REST candles | per window eval |
| ticks_chainlink | — | 14MB | Chainlink on-chain | per window eval |
| ticks_binance | — | 788MB | Binance websocket | continuous |
| ticks_clob | 27,895 | 11MB | Polymarket CLOB book | every 2s |
| ticks_coinglass | — | 34MB | CoinGlass API | every 60s |
| ticks_v2_probability | — | 470MB | TimesFM v2.2 | per eval offset |

---

## System Tables

| Table | Purpose |
|-------|---------|
| system_state | Engine status, wallet balance, drawdown, config snapshot |
| macro_signals | Macro observer output (Railway service, every 60s) |
| trading_configs | Historical config snapshots |
| post_resolution_analyses | AI post-trade analysis (sparse) |
