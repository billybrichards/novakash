# Margin Engine Persistence Guide

**Last Updated:** 2026-04-14  
**Status:** Production Ready (160 tests passing)

---

## Overview

The margin engine persists **all trading decisions** (entries, exits, position updates) to PostgreSQL. This guide documents:

1. **Database Schema** - What data is stored
2. **Data Flow** - How decisions flow through the system
3. **Persistence Points** - When data is written to DB
4. **Strategy Attribution** - How we track which strategy made each decision
5. **Query Examples** - How to analyze historical decisions

---

## Database Schema

### Table: `margin_positions`

**Primary Table** - All positions (open and closed) are stored here.

```sql
CREATE TABLE margin_positions (
    -- Core identity
    id TEXT PRIMARY KEY,                    -- UUID for the position
    asset TEXT NOT NULL DEFAULT 'BTC',      -- Always 'BTC' currently
    side TEXT NOT NULL,                     -- 'LONG' | 'SHORT'
    state TEXT NOT NULL,                    -- 'OPEN' | 'CLOSED'
    leverage INT NOT NULL DEFAULT 5,        -- Leverage used at entry

    -- Entry details
    entry_price REAL,                       -- Execution price at entry
    notional REAL,                          -- Position size in USDT
    collateral REAL,                        -- Margin posted
    stop_loss_price REAL,                   -- Initial SL price
    take_profit_price REAL,                 -- Initial TP price
    
    -- Exit details
    exit_price REAL,                        -- Execution price at exit
    exit_reason TEXT,                       -- See ExitReason enum below
    realised_pnl REAL DEFAULT 0,            -- PnL in USDT (includes fees)
    
    -- Timestamps
    opened_at TIMESTAMPTZ,                  -- Entry time
    closed_at TIMESTAMPTZ,                  -- Exit time
    created_at TIMESTAMPTZ DEFAULT now(),   -- DB record creation time
    
    -- Signal metadata (entry-time only)
    entry_signal_score REAL,                -- Signal strength at entry
    entry_timescale TEXT,                   -- '5m' | '15m' | etc
    entry_order_id TEXT,                    -- Exchange order ID
    exit_order_id TEXT,                     -- Exchange order ID at exit
    
    -- Fees
    entry_commission REAL DEFAULT 0,        -- Fee paid at entry (USDT)
    exit_commission REAL DEFAULT 0,         -- Fee paid at exit (USDT)
    
    -- Execution context
    venue TEXT,                             -- 'binance' | 'hyperliquid'
    strategy_version TEXT,                  -- 'v1-composite' | 'v4' | etc
    
    -- Continuation tracking (re-prediction feature)
    hold_clock_anchor TIMESTAMPTZ,          -- When position was opened
    continuation_count INT DEFAULT 0,       -- Number of continuation extensions
    last_continuation_ts TIMESTAMPTZ,       -- Last continuation decision time
    last_continuation_p_up REAL,            -- P_up at last continuation
    
    -- v4 audit snapshot (FREEZE at entry - for post-trade analysis)
    v4_entry_regime TEXT,                   -- 'TREND' | 'MEAN_REVERSION' | 'NO_TRADE'
    v4_entry_macro_bias TEXT,               -- 'BULLISH' | 'BEARISH' | 'NEUTRAL'
    v4_entry_macro_confidence INT,          -- 0-100
    v4_entry_expected_move_bps REAL,        -- Expected move in basis points
    v4_entry_composite_v3 REAL,             -- Composite signal score
    v4_entry_consensus_safe BOOLEAN,        -- Safe to trade flag
    v4_entry_window_close_ts BIGINT,       -- Window close timestamp
    v4_snapshot_ts_at_entry DOUBLE PRECISION -- v4 snapshot fetch time
);

-- Indexes for common queries
CREATE INDEX idx_margin_pos_state ON margin_positions(state);
CREATE INDEX idx_margin_pos_opened ON margin_positions(opened_at);
CREATE INDEX idx_margin_pos_closed ON margin_positions(closed_at DESC) WHERE state = 'CLOSED';
```

### Table: `margin_signals`

**Passive Signal Recorder** - Every composite signal is recorded, regardless of whether a trade occurred.

```sql
CREATE TABLE margin_signals (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    asset TEXT NOT NULL DEFAULT 'BTC',
    
    -- Core signal
    composite_score REAL,                    -- Main composite signal (-1 to +1)
    
    -- Timescale breakdown
    timescale_5m_score REAL,
    timescale_15m_score REAL,
    timescale_1h_score REAL,
    timescale_4h_score REAL,
    
    -- Model agreement
    model_count INT,                        -- How many models agreed
    model_agreement_pct REAL,               -- Agreement percentage
    
    -- Regime detection
    regime TEXT,                            -- 'TREND' | 'MEAN_REVERSION' | 'NO_TRADE'
    
    -- Metadata
    server_version TEXT,
    strategy TEXT
);

CREATE INDEX idx_margin_signals_timestamp ON margin_signals(timestamp DESC);
CREATE INDEX idx_margin_signals_composite ON margin_signals(composite_score);
```

---

## Data Flow: Entry Decision

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Main Loop (main.py)                                      │
│    - Every tick_interval_s (default 5s)                     │
│    - Check v3 signal WebSocket for new signals              │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. OpenPositionUseCase.execute()                            │
│    - Receives OpenPositionInput with signal + v4 snapshot   │
│    - Dispatches to V4Strategy or V2Strategy                 │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. Entry Strategy (v4_strategy.py OR v2_strategy.py)        │
│    - Evaluates entry gates (10 gates for v4)                │
│    - Computes SL/TP prices                                  │
│    - Computes position size (quantile_var_sizer)            │
│    - Returns Position entity if all gates pass              │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. Exchange Adapter (place_market_order())                  │
│    - Sends order to exchange (Binance/Hyperliquid)          │
│    - Receives FillResult with execution details             │
│    - In paper mode, simulates fill with fees                │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ 5. Position Entity Created                                  │
│    - Position(id, side, entry_price, notional, ...)         │
│    - v4 audit snapshot FROZEN on Position entity            │
│    - All entry metadata attached                            │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ 6. PgPositionRepository.save()                              │
│    - INSERT INTO margin_positions ... ON CONFLICT UPDATE    │
│    - All v4 audit fields saved as write-once at entry       │
│    - Continuation fields updated on every save              │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ 7. Telegram Alert (optional)                                │
│    - "LONG BTC @ $82,340, 5x, size=$500"                   │
│    - Includes v4 regime + macro bias if available           │
└─────────────────────────────────────────────────────────────┘
```

---

## Data Flow: Exit Decision

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Main Loop (main.py) - ManagePositions Use Case           │
│    - Every tick_interval_s (default 5s)                     │
│    - Load all OPEN positions from DB                        │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. ManagePositionsUseCase.execute()                         │
│    - Iterates over open positions                           │
│    - Calls position_management managers in order:           │
│      1. StopLossManager.check()                             │
│      2. TakeProfitManager.check()                           │
│      3. TrailingManager.update()                            │
│      4. ExpiryManager.check()                               │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. Management Manager (e.g., stop_loss.py)                  │
│    - Checks if exit condition met                           │
│    - Returns ExitDecision(reason, price) if triggered       │
│    - Examples:                                              │
│      - StopReason.STOP_LOSS_HIT                             │
│      - StopReason.TAKE_PROFIT_HIT                           │
│      - StopReason.TRAILING_STOP_HIT                         │
│      - StopReason.EXPIRY                                    │
│      - StopReason.SIGNAL_REVERSAL                           │
│      - StopReason.MARK_DIVERGENCE                           │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼

┌─────────────────────────────────────────────────────────────┐
│ 4. Exchange Adapter (close_position())                      │
│    - Sends market order to close                            │
│    - Receives FillResult with execution price + fees        │
│    - Computes realised_pnl (includes all fees)              │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼

┌─────────────────────────────────────────────────────────────┐
│ 5. Position Entity Updated                                  │
│    - state = 'CLOSED'                                       │
│    - exit_price, exit_reason, realised_pnl set              │
│    - closed_at = now()                                      │
│    - exit_order_id, exit_commission set                     │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼

┌─────────────────────────────────────────────────────────────┐
│ 6. PgPositionRepository.save()                              │
│    - UPDATE margin_positions SET state='CLOSED', ...        │
│    - Final state written to DB                              │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼

┌─────────────────────────────────────────────────────────────┐
│ 7. Telegram Alert (optional)                                │
│    - "CLOSED LONG BTC: +$42.30 PnL (TP hit)"               │
│    - Includes hold duration + PnL%                          │
└─────────────────────────────────────────────────────────────┘
```

---

## Exit Reasons (ExitReason Enum)

All exit reasons are tracked in the `exit_reason` column:

```python
class ExitReason(Enum):
    STOP_LOSS_HIT = "stop_loss"              # Hard stop triggered
    TAKE_PROFIT_HIT = "take_profit"          # Target reached
    TRAILING_STOP_HIT = "trailing_stop"      # Trailing stop triggered
    EXPIRY = "expiry"                        # Position timed out
    SIGNAL_REVERSAL = "signal_reversal"      # Signal flipped against position
    MARK_DIVERGENCE = "mark_divergence"      # Market price diverged from signal
    MANUAL = "manual"                        # Manual close via API
    ERROR = "error"                          # Error during management
```

---

## Strategy Attribution

### How We Track Which Strategy Made Each Decision

**Entry Attribution:**
1. **`strategy_version`** column - Tracks the strategy code version:
   - `'v1-composite'` - Legacy v2 composite signal
   - `'v4'` - New v4 decision stack
   - `'v4.1'`, `'v4.2'` - Future v4 variants

2. **`v4_entry_*` columns** - Complete v4 decision context:
   - `v4_entry_regime` - Which regime strategy was used
   - `v4_entry_macro_bias` - Macro adjustment applied
   - `v4_entry_consensus_safe` - Safety gate result
   - All other v4 fields frozen at entry

3. **`entry_signal_score`** - Raw signal strength at entry

4. **`entry_timescale`** - Which timescale drove the entry

**Exit Attribution:**
1. **`exit_reason`** - Why the position closed
2. **`hold_duration_s`** - How long it was held (computed at query time)
3. **`realised_pnl`** - Final PnL (includes all fees)

### Query: Win Rate by Strategy Version

```sql
SELECT 
    strategy_version,
    COUNT(*) AS total_trades,
    SUM(CASE WHEN realised_pnl > 0 THEN 1 ELSE 0 END) AS wins,
    ROUND(100.0 * SUM(CASE WHEN realised_pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS win_rate_pct,
    ROUND(AVG(realised_pnl), 2) AS avg_pnl,
    ROUND(SUM(realised_pnl), 2) AS total_pnl
FROM margin_positions
WHERE state = 'CLOSED'
  AND strategy_version IS NOT NULL
GROUP BY strategy_version
ORDER BY total_pnl DESC;
```

### Query: Win Rate by v4 Regime

```sql
SELECT 
    v4_entry_regime,
    COUNT(*) AS total_trades,
    SUM(CASE WHEN realised_pnl > 0 THEN 1 ELSE 0 END) AS wins,
    ROUND(100.0 * SUM(CASE WHEN realised_pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS win_rate_pct,
    ROUND(AVG(realised_pnl), 2) AS avg_pnl
FROM margin_positions
WHERE state = 'CLOSED'
  AND v4_entry_regime IS NOT NULL
GROUP BY v4_entry_regime
ORDER BY win_rate_pct DESC;
```

### Query: Exit Reason Distribution

```sql
SELECT 
    exit_reason,
    COUNT(*) AS count,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct,
    ROUND(AVG(realised_pnl), 2) AS avg_pnl
FROM margin_positions
WHERE state = 'CLOSED'
GROUP BY exit_reason
ORDER BY count DESC;
```

---

## Code Locations

### Persistence Layer

| File | Purpose |
|------|---------|
| `adapters/persistence/pg_repository.py` | Position DB read/write |
| `adapters/persistence/pg_log_repository.py` | Log persistence |
| `adapters/persistence/pg_signal_repository.py` | Passive signal recorder |
| `application/use_cases/open_position.py` | Entry decision use case |
| `application/use_cases/manage_positions.py` | Exit decision use case |
| `application/use_cases/entry_strategies/v4_strategy.py` | v4 entry logic |
| `application/use_cases/entry_strategies/v2_strategy.py` | v2 entry logic |
| `application/use_cases/position_management/stop_loss.py` | Stop loss checks |
| `application/use_cases/position_management/take_profit.py` | Take profit checks |
| `application/use_cases/position_management/trailing.py` | Trailing stop logic |
| `application/use_cases/position_management/expiry.py` | Position expiry logic |

### Database Setup

| File | Purpose |
|------|---------|
| `alembic/versions/001_initial_schema.py` | Initial table creation |
| `alembic/versions/002_add_commission_columns.py` | Fee tracking columns |
| `alembic/versions/003_add_continuation_and_v4_columns.py` | v4 audit columns |

---

## Testing

### Run All Tests

```bash
cd margin_engine
python3 -m pytest tests/ -v
```

### Test Position Persistence

```bash
python3 -m pytest tests/use_cases/test_open_position_macro_advisory.py -v
python3 -m pytest tests/use_cases/test_mark_divergence_gate.py -v
```

### Verify Migration

```bash
python3 -m pytest tests/unit/test_v4_data_flow.py::test_alembic_migrations_exist -v
```

---

## API Endpoints

The hub exposes these endpoints for querying position data:

### GET `/api/margin/history`

**Query Parameters:**
- `side` - 'LONG' | 'SHORT' | (none)
- `outcome` - 'win' | 'loss' | (none)
- `exit_reason` - Specific exit reason | (none)
- `limit` - Page size (default 50)
- `offset` - Page offset (default 0)

**Response:**
```json
{
  "positions": [
    {
      "id": "abc-123",
      "asset": "BTC",
      "side": "LONG",
      "state": "CLOSED",
      "leverage": 5,
      "entry_price": 82340.0,
      "notional": 500.0,
      "exit_price": 82890.0,
      "exit_reason": "take_profit",
      "realised_pnl": 42.30,
      "opened_at": "2026-04-14T10:30:00Z",
      "closed_at": "2026-04-14T11:45:00Z",
      "hold_duration_s": 4500.0,
      "entry_signal_score": 0.72,
      "entry_timescale": "5m",
      "entry_commission": 0.18,
      "exit_commission": 0.18,
      "total_commission": 0.36,
      "venue": "hyperliquid",
      "strategy_version": "v4",
      "v4_entry_regime": "TREND",
      "v4_entry_macro_bias": "BULLISH",
      "v4_entry_consensus_safe": true
    }
  ],
  "total": 143,
  "page": 1,
  "pages": 3
}
```

---

## Troubleshooting

### Positions Not Saving

1. Check DB connection: `SELECT 1;` in psql
2. Check logs for `ensure_table()` success message
3. Verify `MARGIN_DATABASE_URL` env var is set

### Missing v4 Columns

1. Run migration: `python3 -m alembic upgrade head`
2. Check `v4_entry_regime` is not NULL for v4 trades

### PnL Looks Wrong

1. Check `entry_commission` + `exit_commission` are recorded
2. Verify `venue` matches expected exchange fee model
3. Compare with exchange trade history

---

## Summary

**All trading decisions are persisted:**
- ✅ Entry details (price, size, SL/TP, signal)
- ✅ v4 audit snapshot (frozen at entry)
- ✅ Exit details (price, reason, PnL)
- ✅ Fees (entry + exit commissions)
- ✅ Continuation state (trailing updates)
- ✅ All signals (passive recorder, even no-trade)

**Query capabilities:**
- ✅ Win rate by strategy version
- ✅ Win rate by v4 regime
- ✅ Win rate by macro bias
- ✅ Exit reason distribution
- ✅ Hold duration analysis
- ✅ PnL by timescale
- ✅ Performance by signal strength

---

*Last updated: 2026-04-14*
