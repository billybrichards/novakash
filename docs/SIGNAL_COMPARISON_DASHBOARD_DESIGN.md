# Signal Comparison Dashboard Design

**Purpose**: Track and compare accuracy of all directional prediction signals across timescales and resolution venues

**Target Page**: `/signal-comparison`

**Data Source**: `GET /api/v58/signal-comparison`

---

## Background: All Signals Predict Direction

We now have **6 different signal types** that all try to predict the same thing (directional bias), but they output in different formats:

| Signal | Output Format | Frequency | Status |
|--------|--------------|-----------|--------|
| **Sequoia v5.2 (v2)** | P(UP) 0-1 | 5m, 15m | LIVE |
| **V3 Composite** | Score [-1,+1] | All timescales | LIVE |
| **HMM Regime** | 4-state + confidence | All timescales | LIVE (PR #67) |
| **MacroV2** | LONG/SHORT/NEUTRAL | 5m, 15m, 1h, 4h | LIVE (PR #71) |
| **V4 Consensus** | Alignment score 0-1 | All timescales | LIVE |
| **Cascade FSM** | IDLE/CASCADE/BET/COOLDOWN | Real-time | LIVE |

**Qwen is DEPRECATED**: Replaced by MacroV2Classifier (heuristic rules) because Qwen had 20-30% BEAR hit rate (anti-predictive).

---

## Key Feature: Dual Resolution Tracking

**Critical Insight**: We need to track signal performance against TWO different resolution venues:

1. **Polymarket Resolution** - Oracle-based BTC prediction market outcomes
   - True/false resolution based on oracle price at window close
   - What our Polymarket engine trades against
   - Resolution is binary (UP/DN) at 5m window boundaries

2. **Hyperliquid Price Movement** - Actual perpetual futures price action
   - What our margin_engine trades against
   - Measured as price direction over the same window
   - May differ from Polymarket resolution due to:
     - Oracle lag vs real-time price
     - Perp basis/mark price vs spot oracle
     - Different window boundaries or resolution logic

**Why This Matters**:
- A signal might have 65% WR on Polymarket but only 55% WR on Hyperliquid
- This explains why paper trading wins don't translate to live trading PnL
- We can identify signals that are "Polymarket-aligned" vs "Perp-aligned"
- Margin engine should prioritize signals with high Hyperliquid WR

---

## Page Layout

```
┌─────────────────────────────────────────────────────────────────────┐
│  SIGNAL COMPARISON DASHBOARD                        [5m] [15m] [1h] [4h]  [30d] [7d]    │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ ACCURACY OVERVIEW (Last 30 Days) - 15m Timescale            │   │
│  │                                                             │   │
│  │ Signal              POLY WR  HLP WR   Δ     PnL    Samples  │   │
│  │ ─────────────────────────────────────────────────────────   │   │
│  │ Sequoia v5.2        62%      58%     -4%   +$45.2   865    │   │
│  │ HMM calm_trend      68%      61%     -7%   +$38.7   312    │   │
│  │ MacroV2 LONG        59%      54%     -5%   +$23.4   198    │   │
│  │ V3 Composite >0.5   61%      57%     -4%   +$32.1   542    │   │
│  │ V4 Alignment >0.7   64%      59%     -5%   +$28.9   287    │   │
│  │ Cascade IDLE        58%      55%     -3%   +$41.3   756    │   │
│  │                                                             │   │
│  │ Legend: POLY = Polymarket resolution | HLP = Hyperliquid price │   │
│  │         Δ = HLP - POLY (negative = signal less effective on perp)│  │
│  │                                                             │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ REGIME-SPECIFIC ACCURACY                                     │   │
│  │                                                             │   │
│  │                    calm_trend  volatile_trend  chop  risk_off│   │
│  │ Sequoia v5.2           68%           64%       45%     38%  │   │
│  │ HMM (self)             72%           61%       52%     41%  │   │
│  │ MacroV2                65%           58%       48%     35%  │   │
│  │ V3 Composite           71%           63%       47%     39%  │   │
│  │ V4 Consensus           69%           62%       46%     40%  │   │
│  │ Cascade (inverse)      55%           42%       61%     78%  │   │
│  │                                                             │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ SIGNAL CORRELATION MATRIX                                    │   │
│  │                                                             │   │
│  │               Seq  HMM  Macro  V3  V4  Casc               │   │
│  │ Sequoia       1.00  0.72  0.65  0.81 0.78  0.23          │   │
│  │ HMM           0.72  1.00  0.58  0.69 0.64  0.18          │   │
│  │ MacroV2       0.65  0.58  1.00  0.61 0.71  0.31          │   │
│  │ V3 Composite  0.81  0.69  0.61  1.00 0.85  0.25          │   │
│  │ V4 Consensus  0.78  0.64  0.71  0.85 1.00  0.28          │   │
│  │ Cascade       0.23  0.18  0.31  0.25 0.28  1.00          │   │
│  │                                                             │   │
│  │ Legend: ████ 0.8+  ███ 0.6-0.8  ██ 0.4-0.6  █ 0.2-0.4    │   │
│  │                                                             │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ SIGNAL TIMELINE (Last 24 Hours)                              │   │
│  │                                                             │   │
│  │ Time  Seq  HMM     Macro  V3   V4    Casc  Actual          │   │
│  │ ─────────────────────────────────────────────────────────   │   │
│  │ 14:00  UP  calm    LONG  +0.4  0.7   IDLE  UP ✓            │   │
│  │ 14:05  UP  calm    LONG  +0.5  0.8   IDLE  UP ✓            │   │
│  │ 14:10  DN  chop    NEU   -0.1  0.4   IDLE  DN ✓            │   │
│  │ 14:15  UP  volatile SHORT+0.6  0.6   CASCADE DN ✗          │   │
│  │ ...                                                         │   │
│  │                                                             │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Section Details

### Section 1: Accuracy Overview

**Metrics per Signal**:
- **Win Rate**: % of correct predictions (aligned with actual outcome)
- **PnL**: Cumulative PnL if we traded on this signal alone
- **Sharpe**: Risk-adjusted return
- **Samples**: Number of predictions in the period

**Normalization**:
- Sequoia: P(UP) >= 0.5 → UP prediction
- V3 Composite: score > 0 → UP, < 0 → DOWN
- HMM: calm_trend/volatile_trend → direction from composite, chop/risk_off → NO TRADE
- MacroV2: LONG → UP, SHORT → DOWN, NEUTRAL → NO TRADE
- V4 Consensus: alignment_score > 0.5 → direction from primary timescale
- Cascade: IDLE → normal trading, CASCADE → fade the cascade (inverse)

---

### Section 2: Regime-Specific Accuracy

**Key Insight**: Different signals work better in different regimes

**Expected Patterns**:
- **Sequoia**: Best in calm_trend, worst in chop/risk_off
- **HMM**: Self-predictive (higher accuracy in its own predicted regime)
- **MacroV2**: Best in longer horizons (1h/4h), worst in 5m chop
- **V3 Composite**: Best when |composite| > 0.5 (strong signals)
- **V4 Consensus**: Best when 3/4+ timescales agree
- **Cascade**: Best in INVERSE mode during CASCADE (fade liquidations)

**Display**:
- Heatmap-style color coding (green = high WR, red = low WR)
- Sample count per cell (to avoid overfitting to small samples)
- "Best in regime" badge for the top signal in each regime

---

### Section 3: Signal Correlation Matrix

**Purpose**: See which signals agree/disagree

**Interpretation**:
- High correlation (0.8+): Signals are redundant (e.g., V3 Composite ↔ V4 Consensus)
- Low correlation (<0.3): Signals provide unique information (e.g., Cascade ↔ others)
- Negative correlation: Signals are inversely related (cascade fade strategy)

**Use Cases**:
- **Diversification**: Combine low-correlation signals for ensemble
- **Redundancy check**: If two signals are 0.95+ correlated, one is enough
- **Divergence alerts**: When high-correlation signals disagree, that's a signal itself

**Visualization**:
- Heatmap with color intensity
- Numerical values on hover
- Cluster grouping (automatically group similar signals)

---

### Section 4: Signal Timeline

**Purpose**: See all signals side-by-side with actual outcome

**Columns**:
- Time: Window timestamp
- Seq: Sequoia direction (UP/DN)
- HMM: Regime state (calm/volatile/chop/risk)
- Macro: MacroV2 bias (LONG/SHORT/NEU)
- V3: Composite score (+/- 0-1)
- V4: Alignment score (0-1)
- Casc: Cascade state (IDLE/CASC)
- Actual: Window outcome (UP/DN) with checkmark/x

**Features**:
- Sortable columns
- Filter by signal value (e.g., "show only HMM=chop rows")
- Click row → expand to see full signal context (all sub-signals, prices, VPIN, etc.)
- Highlight divergences (when 3+ signals disagree)

---

## API Design

### GET `/api/v58/signal-comparison`

**Request**:
```
GET /api/v58/signal-comparison?period=30d&asset=BTC
```

**Response**:
```json
{
  "period_days": 30,
  "asset": "BTC",
  "accuracy_overview": {
    "sequoia_v5_2": {
      "win_rate": 0.62,
      "pnl": 45.2,
      "sharpe": 1.8,
      "samples": 865,
      "precision": 0.64,
      "recall": 0.59,
      "f1_score": 0.61
    },
    "hmm_regime": {
      "win_rate": 0.68,
      "pnl": 38.7,
      "sharpe": 1.5,
      "samples": 312,
      "by_state": {
        "calm_trend": { "win_rate": 0.72, "samples": 145 },
        "volatile_trend": { "win_rate": 0.64, "samples": 98 },
        "chop": { "win_rate": 0.52, "samples": 52 },
        "risk_off": { "win_rate": 0.41, "samples": 17 }
      }
    },
    "macrov2": {
      "win_rate": 0.59,
      "pnl": 23.4,
      "sharpe": 1.2,
      "samples": 198,
      "by_bias": {
        "LONG": { "win_rate": 0.61, "samples": 112 },
        "SHORT": { "win_rate": 0.56, "samples": 68 },
        "NEUTRAL": { "win_rate": null, "samples": 18 }
      }
    },
    "v3_composite": {
      "win_rate": 0.61,
      "pnl": 32.1,
      "sharpe": 1.4,
      "samples": 542,
      "by_strength": {
        "strong_positive": { "threshold": ">0.5", "win_rate": 0.68, "samples": 187 },
        "weak_positive": { "threshold": "0.2-0.5", "win_rate": 0.59, "samples": 198 },
        "neutral": { "threshold": "-0.2-0.2", "win_rate": 0.48, "samples": 87 },
        "weak_negative": { "threshold": "-0.5--0.2", "win_rate": 0.57, "samples": 42 },
        "strong_negative": { "threshold": "<-0.5", "win_rate": 0.65, "samples": 28 }
      }
    },
    "v4_consensus": {
      "win_rate": 0.64,
      "pnl": 28.9,
      "sharpe": 1.6,
      "samples": 287,
      "by_alignment": {
        "4_4": { "alignment": "4/4 timescales", "win_rate": 0.74, "samples": 45 },
        "3_4": { "alignment": "3/4 timescales", "win_rate": 0.66, "samples": 123 },
        "2_4": { "alignment": "2/4 timescales", "win_rate": 0.55, "samples": 119 }
      }
    },
    "cascade_fsm": {
      "win_rate": 0.58,
      "pnl": 41.3,
      "sharpe": 1.3,
      "samples": 756,
      "by_state": {
        "IDLE": { "win_rate": 0.58, "samples": 612, "strategy": "normal" },
        "CASCADE": { "win_rate": 0.42, "samples": 98, "strategy": "fade", "note": "inverse=true" },
        "BET": { "win_rate": 0.51, "samples": 32 },
        "COOLDOWN": { "win_rate": 0.55, "samples": 14 }
      }
    }
  },
  "regime_specific_accuracy": {
    "calm_trend": {
      "sequoia_v5_2": 0.68,
      "hmm_regime": 0.72,
      "macrov2": 0.65,
      "v3_composite": 0.71,
      "v4_consensus": 0.69,
      "cascade_fsm": 0.55
    },
    "volatile_trend": {
      "sequoia_v5_2": 0.64,
      "hmm_regime": 0.61,
      "macrov2": 0.58,
      "v3_composite": 0.63,
      "v4_consensus": 0.62,
      "cascade_fsm": 0.42
    },
    "chop": {
      "sequoia_v5_2": 0.45,
      "hmm_regime": 0.52,
      "macrov2": 0.48,
      "v3_composite": 0.47,
      "v4_consensus": 0.46,
      "cascade_fsm": 0.61
    },
    "risk_off": {
      "sequoia_v5_2": 0.38,
      "hmm_regime": 0.41,
      "macrov2": 0.35,
      "v3_composite": 0.39,
      "v4_consensus": 0.40,
      "cascade_fsm": 0.78
    }
  },
  "correlation_matrix": {
    "sequoia_v5_2": {
      "sequoia_v5_2": 1.00,
      "hmm_regime": 0.72,
      "macrov2": 0.65,
      "v3_composite": 0.81,
      "v4_consensus": 0.78,
      "cascade_fsm": 0.23
    },
    "hmm_regime": {
      "sequoia_v5_2": 0.72,
      "hmm_regime": 1.00,
      "macrov2": 0.58,
      "v3_composite": 0.69,
      "v4_consensus": 0.64,
      "cascade_fsm": 0.18
    },
    "macrov2": {
      "sequoia_v5_2": 0.65,
      "hmm_regime": 0.58,
      "macrov2": 1.00,
      "v3_composite": 0.61,
      "v4_consensus": 0.71,
      "cascade_fsm": 0.31
    },
    "v3_composite": {
      "sequoia_v5_2": 0.81,
      "hmm_regime": 0.69,
      "macrov2": 0.61,
      "v3_composite": 1.00,
      "v4_consensus": 0.85,
      "cascade_fsm": 0.25
    },
    "v4_consensus": {
      "sequoia_v5_2": 0.78,
      "hmm_regime": 0.64,
      "macrov2": 0.71,
      "v3_composite": 0.85,
      "v4_consensus": 1.00,
      "cascade_fsm": 0.28
    },
    "cascade_fsm": {
      "sequoia_v5_2": 0.23,
      "hmm_regime": 0.18,
      "macrov2": 0.31,
      "v3_composite": 0.25,
      "v4_consensus": 0.28,
      "cascade_fsm": 1.00
    }
  },
  "signal_timeline": [
    {
      "timestamp": "2026-04-12T14:00:00Z",
      "window_close": "2026-04-12T14:05:00Z",
      "sequoia_direction": "UP",
      "sequoia_p_up": 0.68,
      "hmm_regime": "calm_trend",
      "hmm_confidence": 0.72,
      "macrov2_bias": "LONG",
      "macrov2_confidence": 0.63,
      "v3_composite": 0.42,
      "v4_alignment": 0.75,
      "cascade_state": "IDLE",
      "actual_outcome": "UP",
      "all_correct": true
    },
    {
      "timestamp": "2026-04-12T14:05:00Z",
      "window_close": "2026-04-12T14:10:00Z",
      "sequoia_direction": "UP",
      "sequoia_p_up": 0.71,
      "hmm_regime": "calm_trend",
      "hmm_confidence": 0.68,
      "macrov2_bias": "LONG",
      "macrov2_confidence": 0.61,
      "v3_composite": 0.51,
      "v4_alignment": 0.82,
      "cascade_state": "IDLE",
      "actual_outcome": "UP",
      "all_correct": true
    },
    {
      "timestamp": "2026-04-12T14:10:00Z",
      "window_close": "2026-04-12T14:15:00Z",
      "sequoia_direction": "DN",
      "sequoia_p_up": 0.38,
      "hmm_regime": "chop",
      "hmm_confidence": 0.55,
      "macrov2_bias": "NEUTRAL",
      "macrov2_confidence": 0.42,
      "v3_composite": -0.12,
      "v4_alignment": 0.48,
      "cascade_state": "IDLE",
      "actual_outcome": "DN",
      "all_correct": true
    }
  ],
  "divergence_alerts": [
    {
      "timestamp": "2026-04-12T14:15:00Z",
      "type": "high_correlation_divergence",
      "signals": ["sequoia_v5_2", "v3_composite", "v4_consensus"],
      "values": ["UP", "+0.62", "0.78"],
      "actual": "DN",
      "note": "All three high-correlation signals agreed on UP, but outcome was DN"
    }
  ]
}
```

---

## Implementation Priority

### Phase 1 (MVP)
1. Accuracy Overview table (all signals, basic metrics)
2. Regime-Specific Accuracy heatmap
3. Time period selector (30d/7d/24h)

### Phase 2 (Correlation)
4. Signal Correlation Matrix heatmap
5. Correlation interpretation tooltips
6. Cluster grouping (auto-group similar signals)

### Phase 3 (Timeline)
7. Signal Timeline table
8. Row expansion (full signal context)
9. Divergence highlighting

### Phase 4 (Advanced)
10. Divergence alerts panel
11. Ensemble builder (combine signals)
12. Export reports (CSV/PDF)

---

## Technical Notes

1. **Data Source**: `strategy_decisions` table (all signal values at decision time) + `window_snapshots` (actual outcomes)
2. **Normalization**: Each signal must be normalized to UP/DN/NO_TRADE for accuracy calculation
3. **Regime Labels**: Use HMM regime classification at decision time (stored in `strategy_decisions._ctx`)
4. **Correlation Calculation**: Pearson correlation on binary predictions (UP=1, DN=-1, NO_TRADE=0)
5. **Real-time Updates**: WebSocket subscription to `strategy_decisions` insertions
6. **Caching**: Pre-compute accuracy metrics every 5 minutes (window-level data doesn't change retroactively)

---

## Design Patterns to Follow

- **FactoryFloor.jsx** — for data tables with tooltips and expandable rows
- **V4Panel.jsx** — for chart layouts and metric cards
- **AuditChecklist.jsx** — for status badges and color coding
- **StrategyLab.jsx** — for configuration panels and time period selectors

---

## Success Metrics

The dashboard is successful when:
1. Operator can identify which signals work best in which regimes
2. Operator can see signal correlation and avoid redundant signals
3. Operator can spot divergences between high-correlation signals
4. Operator can make informed decisions about signal weighting in ensemble

---

*Design completed: 2026-04-12*
*Based on current signal stack: Sequoia v5.2, V3 Composite, HMM Regime (PR #67), MacroV2 (PR #71), V4 Consensus, Cascade FSM*
