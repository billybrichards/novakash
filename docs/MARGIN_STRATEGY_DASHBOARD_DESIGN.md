# Margin Strategy Dashboard Design

**Purpose**: Interactive strategy lab for margin engine strategies with regime-based PnL analysis

**Target Page**: `/margin-strategies`

**Data Source**: `GET /api/v58/margin-strategies` (new endpoint)

---

## Design Philosophy

The margin engine is fundamentally different from Polymarket:

| Dimension | Polymarket Engine | Margin Engine |
|-----------|------------------|---------------|
| **Instrument** | Binary options (UP/DOWN) | Perpetual futures (LONG/SHORT) |
| **Resolution** | Fixed 5-min windows | Continuous position management |
| **PnL Model** | Win/Loss binary | PnL% based on entry/exit price |
| **Exit Logic** | Window close | SL/TP/trailing/event/decision reversal |
| **Strategy Type** | Directional prediction | Multi-gate decision + risk management |
| **Timescales** | Single (5m) | Multi (5m/15m/1h/4h) |
| **Regimes** | N/A | TRENDING_UP/DOWN, MEAN_REVERTING, CHOPPY |

The Strategy Lab must reflect these differences:
- **No "window replay"** — margin engine uses continuous positions
- **Focus on regime analysis** — strategies perform differently in different regimes
- **Show strategy cards** — each strategy is a distinct approach (alignment, VaR, cascade, etc.)
- **Multi-timescale awareness** — strategies may use different timescales

---

## Page Layout

```
┌─────────────────────────────────────────────────────────────────────┐
│  MARGIN STRATEGY LAB                                [Paper Mode]    │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐    │
│  │ V4 PATH         │  │ MULTI-TIMESCALE │  │ QUANTILE-VAR    │    │
│  │ [LIVE]          │  │ [INACTIVE]      │  │ [INACTIVE]      │    │
│  │                 │  │                 │  │                 │    │
│  │ PnL: +$23.4     │  │ Backtest: N/A   │  │ Backtest: N/A   │    │
│  │ Win Rate: 58%   │  │                 │  │                 │    │
│  │ Sharpe: 1.2     │  │                 │  │                 │    │
│  │                 │  │                 │  │                 │    │
│  │ [Configure]     │  │ [Configure]     │  │ [Configure]     │    │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘    │
│                                                                     │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐    │
│  │ REGIME-ADAPTIVE │  │ CASCADE FADE    │  │ CLOB SCALP      │    │
│  │ [INACTIVE]      │  │ [INACTIVE]      │  │ [INACTIVE]      │    │
│  │                 │  │                 │  │                 │    │
│  │ Backtest: N/A   │  │ Backtest: N/A   │  │ Backtest: N/A   │    │
│  │                 │  │                 │  │                 │    │
│  │                 │  │                 │  │                 │    │
│  │ [Configure]     │  │ [Configure]     │  │ [Configure]     │    │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘    │
│                                                                     │
│  ┌─────────────────┐  ┌─────────────────┐                         │
│  │ MACRO CALIBRATED│  │ EVENT PRE-POS   │                         │
│  │ [INACTIVE]      │  │ [INACTIVE]      │                         │
│  │                 │  │                 │                         │
│  │ Backtest: N/A   │  │ Backtest: N/A   │                         │
│  │                 │  │                 │                         │
│  │                 │  │                 │                         │
│  │ [Configure]     │  │ [Configure]     │                         │
│  └─────────────────┘  └─────────────────┘                         │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  REGIME PERFORMANCE (Last 30 days)                                  │
│                                                                     │
│  TRENDING_UP    $45.2   62% WR   1.8 Sharpe   ████████████░░       │
│  TRENDING_DOWN  $38.7   59% WR   1.5 Sharpe   ██████████░░░░       │
│  MEAN_REVERT    $12.3   51% WR   0.8 Sharpe   ████░░░░░░░░░        │
│  CHOPPY         -$8.4   43% WR   -0.3 Sharpe  ██░░░░░░░░░░░        │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Strategy Cards

Each strategy card displays:

### Card Header
- Strategy name (e.g., "V4 PATH", "MULTI-TIMESCALE ALIGNMENT")
- Status badge:
  - `LIVE` (green) — currently active in production
  - `INACTIVE` (gray) — available but not active
  - `BACKTEST` (blue) — backtest results available
  - `BLOCKED` (red) — requires prerequisite work

### Card Body
- **PnL**: Cumulative PnL in USD (live or backtest)
- **Win Rate**: Win rate percentage
- **Sharpe**: Sharpe ratio
- **Max Drawdown**: Max drawdown %
- **Total Trades**: Number of trades
- **Avg Trade**: Average PnL per trade

### Card Footer
- `Configure` button → opens modal with detailed strategy view
- `Backtest` button (if not active) → runs client-side backtest simulation

---

## Strategy Detail Modal

When clicking "Configure" on a strategy card, opens a modal with:

### Tab 1: Strategy Overview

```
┌─────────────────────────────────────────────────────────────┐
│  MULTI-TIMESCALE ALIGNMENT                           [X]    │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  CONCEPT: Trade only when 3/4 timescales agree on direction │
│                                                             │
│  TRIGGER CONDITIONS:                                        │
│  ✓ 3/4 timescales agree (5m, 15m, 1h, 4h)                  │
│  ✓ Primary (15m) must agree                                │
│  ✓ |P(UP) - 0.5| >= 0.10 on primary                        │
│                                                             │
│  POSITION SIZING:                                           │
│  • Base: 2% of capital                                     │
│  • 3/4 alignment: 1.2x multiplier                          │
│  • 4/4 alignment: 1.4x multiplier                          │
│                                                             │
│  EXIT RULES:                                                │
│  • Stop Loss: 0.6%                                         │
│  • Take Profit: 0.5%                                       │
│  • Max Hold: 15 minutes                                    │
│  • Trailing Stop: 0.3%                                     │
│                                                             │
│  EXPECTED EDGE:                                             │
│  • Higher conviction trades (reduced false signals)         │
│  • Lower trade frequency (~40% of current)                  │
│  • Higher win rate expected (~65% vs 58%)                   │
│                                                             │
│  DATA REQUIREMENTS:                                         │
│  ✓ V4 snapshot (4 timescales)                              │
│  ✓ V4 consensus.alignment_score                            │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Tab 2: Performance Metrics

```
┌─────────────────────────────────────────────────────────────┐
│  PERFORMANCE                            [30d] [7d] [24h]   │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  PnL Curve (last 30 days)                                  │
│  $                                                           
│  50 │                              ╭──────╮                
│  40 │                          ╭───╯        ╰──╮           
│  30 │                      ╭───╯                ╰───       
│  20 │                  ╭───╯                          ╭───  
│  10 │      ╭──────╭──╯                                ╰    
│   0 ┼──────╯                                             ───
│  -10 │                                                      
│     └───────────────────────────────────────────────────────
│      1   5   10   15   20   25   30  (days)                │
│                                                             │
│  Metrics                                                    │
│  • Total PnL: +$45.2                                       │
│  • Win Rate: 62% (31/50 trades)                           │
│  • Sharpe: 1.8                                             │
│  • Max Drawdown: -8.3%                                     │
│  • Avg Trade: +$0.9                                        │
│  • Best Trade: +$4.2 (2026-04-10)                         │
│  • Worst Trade: -$2.1 (2026-04-05)                        │
│  • Profit Factor: 2.1                                      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Tab 3: Regime Breakdown

```
┌─────────────────────────────────────────────────────────────┐
│  REGIME PERFORMANCE                                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  TRENDING_UP (12 days)                                     │
│  PnL: +$45.2                                                │
│  Trades: 28 (56% of total)                                 │
│  Win Rate: 62%                                             │
│  Sharpe: 1.8                                               │
│  ████████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░          │
│                                                             │
│  TRENDING_DOWN (8 days)                                    │
│  PnL: +$38.7                                                │
│  Trades: 18 (36% of total)                                 │
│  Win Rate: 59%                                             │
│  Sharpe: 1.5                                               │
│  ████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░            │
│                                                             │
│  MEAN_REVERTING (5 days)                                   │
│  PnL: +$12.3                                                │
│  Trades: 8 (16% of total)                                  │
│  Win Rate: 51%                                             │
│  Sharpe: 0.8                                               │
│  ████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░                  │
│                                                             │
│  CHOPPY (3 days)                                           │
│  PnL: -$8.4                                                 │
│  Trades: 6 (12% of total)                                  │
│  Win Rate: 33%                                             │
│  Sharpe: -0.3                                              │
│  ████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░                       │
│                                                             │
│  Regime Distribution Chart (pie)                           │
│  ████████████ TRENDING_UP 56%                              │
│  ████████ TRENDING_DOWN 36%                                │
│  ████ MEAN_REVERT 16%                                      │
│  ████ CHOPPY 12%                                           │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Tab 4: Configuration

```
┌─────────────────────────────────────────────────────────────┐
│  CONFIGURATION                                              │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  [ ] Enable strategy (requires restart)                     │
│                                                             │
│  Alignment Threshold                                        │
│  ○ 2/4 timescales (lower bar, more trades)                 │
│  ● 3/4 timescales (recommended)                            │
│  ○ 4/4 timescales (highest bar, fewer trades)              │
│                                                             │
│  Position Sizing Multiplier                                 │
│  3/4 alignment: [1.2] x (default)                          │
│  4/4 alignment: [1.4] x (default)                          │
│                                                             │
│  Entry Threshold                                            │
│  Primary timescale P(UP): |p - 0.5| >= [0.10]             │
│                                                             │
│  [Save Configuration]                                       │
│                                                             │
│  Note: Configuration changes require engine restart to take│
│  effect. Current mode: Paper trading.                      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## API Design

### GET `/api/v58/margin-strategies`

**Request:**
```
GET /api/v58/margin-strategies?limit=100
```

**Response:**
```json
{
  "strategies": [
    {
      "id": "v4_path",
      "name": "V4 PATH",
      "description": "Enable V4 fusion surface path (currently dark-deployed)",
      "status": "live",
      "active": true,
      "config": {
        "engine_use_v4_actions": true,
        "v4_primary_timescale": "15m",
        "v4_entry_edge": 0.10,
        "v4_min_expected_move_bps": 15.0
      },
      "performance": {
        "period_days": 30,
        "total_pnl": 23.4,
        "win_rate": 0.58,
        "sharpe": 1.2,
        "max_drawdown": -0.12,
        "total_trades": 45,
        "avg_trade": 0.52,
        "best_trade": 4.2,
        "worst_trade": -2.1,
        "profit_factor": 1.8
      },
      "regime_breakdown": {
        "TRENDING_UP": {
          "pnl": 45.2,
          "trades": 28,
          "win_rate": 0.62,
          "sharpe": 1.8
        },
        "TRENDING_DOWN": {
          "pnl": 38.7,
          "trades": 18,
          "win_rate": 0.59,
          "sharpe": 1.5
        },
        "MEAN_REVERTING": {
          "pnl": 12.3,
          "trades": 8,
          "win_rate": 0.51,
          "sharpe": 0.8
        },
        "CHOPPY": {
          "pnl": -8.4,
          "trades": 6,
          "win_rate": 0.33,
          "sharpe": -0.3
        }
      }
    },
    {
      "id": "multi_timescale_alignment",
      "name": "Multi-Timescale Alignment",
      "description": "Trade only when 3/4 timescales agree on direction",
      "status": "inactive",
      "active": false,
      "config": {
        "alignment_threshold": 3,
        "size_mult_3_4": 1.2,
        "size_mult_4_4": 1.4,
        "entry_edge": 0.10
      },
      "backtest": {
        "period_days": 30,
        "total_pnl": 45.2,
        "win_rate": 0.65,
        "sharpe": 1.8,
        "max_drawdown": -0.08,
        "total_trades": 28,
        "notes": "Backtest based on historical v4 snapshot data"
      },
      "required_data": [
        "v4_snapshot",
        "v4_consensus_alignment"
      ]
    },
    {
      "id": "quantile_var_sizing",
      "name": "Quantile-VaR Position Sizing",
      "description": "Size positions based on TimesFM VaR (p10 downside)",
      "status": "inactive",
      "active": false,
      "config": {
        "target_risk_pct": 0.5,
        "size_mult_min": 0.5,
        "size_mult_max": 2.0
      },
      "backtest": null,
      "required_data": [
        "v4_timescales_quantiles"
      ]
    }
    // ... other strategies
  ],
  "overall_regime_performance": {
    "period_days": 30,
    "TRENDING_UP": { "pnl": 45.2, "trades": 28, "win_rate": 0.62, "sharpe": 1.8 },
    "TRENDING_DOWN": { "pnl": 38.7, "trades": 18, "win_rate": 0.59, "sharpe": 1.5 },
    "MEAN_REVERTING": { "pnl": 12.3, "trades": 8, "win_rate": 0.51, "sharpe": 0.8 },
    "CHOPPY": { "pnl": -8.4, "trades": 6, "win_rate": 0.33, "sharpe": -0.3 }
  }
}
```

---

## Implementation Priority

### Phase 1 (MVP)
1. Strategy cards grid (static data)
2. Modal with Strategy Overview tab
3. Basic performance metrics display
4. Status badges (LIVE/INACTIVE)

### Phase 2 (Data Integration)
5. API integration for real strategy data
6. Performance charts (PnL curve)
7. Regime breakdown tab
8. Configuration tab (read-only)

### Phase 3 (Advanced)
9. Backtest simulation (client-side)
10. Configuration writes (requires auth)
11. Strategy comparison mode
12. Export performance reports

---

## Design Patterns to Follow

- **V4Panel.jsx** — for layout and data display patterns
- **FactoryFloor.jsx** — for data tables and tooltips
- **StrategyLab.jsx (Polymarket)** — for config panel patterns
- **Margin V4Panel** — for margin-specific styling

---

## Technical Notes

1. **Regime Classification**: Use V3 regime classifier output from `v4_snapshot.timescales[primary].regime`
2. **PnL Calculation**: Query `position_history` from margin engine, filter by strategy (if tracked)
3. **Backtest**: Client-side simulation using historical `ticks_v4_snapshot` + `position_history`
4. **Real-time Updates**: WebSocket subscription to margin engine `/logs` for live PnL updates
5. **Configuration**: Stored in margin engine `settings.py`, requires restart to apply

---

*Design completed: 2026-04-12*
*Based on audit findings: ME-STRAT-01 through ME-STRAT-08*
