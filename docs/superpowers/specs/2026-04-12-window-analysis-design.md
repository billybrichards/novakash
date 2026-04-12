# Window Analysis -- Per-Window Evaluation Timeline

**Date:** 2026-04-12
**Author:** Claude Opus 4.6
**Status:** DRAFT
**Estimate:** 1 day (hub endpoint + frontend modal)

---

## 1. Problem Statement

Predictions are not always right -- they are just normally right at *at least one point* in the window. The current Evaluate and Strategy Lab pages show only the FINAL evaluation per window (the last `eval_offset` before close). This hides the entire prediction trajectory: how confidence evolved, when direction flipped, and where the best entry point was.

We need a drilldown that shows ALL signal_evaluations for a single window ordered by time, overlaid with strategy decisions (V10 and V4), so the user can see:
- When the prediction was correct vs incorrect
- Where confidence peaked
- Which strategy would have traded at each offset
- The "best entry point" -- highest confidence in the correct direction

---

## 2. Data Model

### 2.1 What already exists

**`signal_evaluations` table** -- one row per `(window_ts, asset, timeframe, eval_offset)`. Written every 2 seconds from T-298 to T-2. Contains:

| Column group | Fields |
|---|---|
| Identity | `window_ts`, `asset`, `timeframe`, `eval_offset` |
| Prices | `binance_price`, `tiingo_open`, `tiingo_close`, `chainlink_price` |
| CLOB | `clob_up_bid`, `clob_up_ask`, `clob_down_bid`, `clob_down_ask`, `clob_spread`, `clob_mid` |
| Deltas | `delta_pct`, `delta_tiingo`, `delta_binance`, `delta_chainlink`, `delta_source` |
| Signals | `vpin`, `regime` |
| DUNE/V2 | `v2_probability_up`, `v2_direction`, `v2_agrees`, `v2_high_conf`, `v2_model_version`, `v2_quantiles`, `v2_quantiles_at_close` |
| Gates (V10) | `gate_vpin_passed`, `gate_delta_passed`, `gate_cg_passed`, `gate_twap_passed`, `gate_timesfm_passed`, `gate_passed`, `gate_failed` |
| Decision | `decision` (TRADE/SKIP) |
| TWAP | `twap_delta`, `twap_direction`, `twap_gamma_agree` |

**`strategy_decisions` table** -- one row per `(strategy_id, asset, window_ts, eval_offset)`. Written by `EvaluateStrategiesUseCase` for both LIVE and GHOST strategies. Contains:

| Column group | Fields |
|---|---|
| Identity | `strategy_id`, `strategy_version`, `asset`, `window_ts`, `timeframe`, `eval_offset`, `mode` |
| Decision | `action` (TRADE/SKIP/ERROR), `direction`, `confidence`, `confidence_score`, `entry_cap`, `collateral_pct`, `entry_reason`, `skip_reason` |
| Execution | `executed`, `order_id`, `fill_price`, `fill_size` |
| Metadata | `metadata_json` (JSONB -- strategy-specific internals) |

**`market_data` table** -- one row per resolved window. Contains `outcome` (UP/DOWN), `open_price`, `close_price`.

### 2.2 What we need: nothing new

Both tables already store per-`eval_offset` rows. We just need a new Hub endpoint that queries them together for a single window.

The `signal_evaluations` table gives us the V10 gate pipeline state at each offset. The `strategy_decisions` table gives us each strategy's TRADE/SKIP decision at each offset. Together they answer every question the user has.

---

## 3. Hub API Endpoint

### `GET /api/v58/window-analysis/{window_ts}`

**Path params:**
- `window_ts` -- Unix epoch seconds (bigint). Accepts seconds or milliseconds (auto-detected).

**Query params:**
- `asset` -- default `btc`, validated against `{btc, eth, sol, xrp}`
- `timeframe` -- default `5m`, validated against `{5m, 15m}`

**Response:**

```json
{
  "window_ts": 1744450200,
  "asset": "BTC",
  "timeframe": "5m",
  "outcome": {
    "direction": "UP",
    "open_price": 83421.50,
    "close_price": 83467.20,
    "resolved": true,
    "delta_pct": 0.000547
  },
  "eval_count": 120,
  "timeline": [
    {
      "eval_offset": 298,
      "seconds_to_close": 298,
      "prediction": {
        "direction": "UP",
        "p_up": 0.62,
        "confidence": "MEDIUM",
        "high_conf": false
      },
      "signals": {
        "delta_pct": 0.0003,
        "delta_source": "tiingo_rest_candle",
        "delta_chainlink": 0.00028,
        "delta_tiingo": 0.0003,
        "delta_binance": 0.00031,
        "vpin": 0.51,
        "regime": "NORMAL",
        "clob_spread": 0.04,
        "clob_mid": 0.52
      },
      "gates_v10": {
        "vpin": true,
        "delta": false,
        "cg": true,
        "twap": null,
        "timesfm": null,
        "all_passed": false,
        "blocking_gate": "delta_magnitude"
      },
      "decision_v10": "SKIP",
      "strategies": {
        "v10_gate": {
          "action": "SKIP",
          "direction": "UP",
          "confidence_score": 0.62,
          "skip_reason": "delta_magnitude gate failed",
          "mode": "LIVE"
        },
        "v4_fusion": {
          "action": "TRADE",
          "direction": "UP",
          "confidence_score": 0.71,
          "entry_reason": "V4 surface above threshold",
          "mode": "GHOST"
        }
      }
    }
    // ... ~120 more entries, one per eval_offset
  ],
  "best_entry": {
    "eval_offset": 62,
    "seconds_to_close": 62,
    "direction": "UP",
    "p_up": 0.84,
    "correct": true,
    "any_strategy_would_trade": true,
    "strategies_trading": ["v4_fusion"]
  },
  "summary": {
    "direction_flips": 2,
    "peak_confidence": 0.84,
    "peak_confidence_offset": 62,
    "pct_time_correct_direction": 0.78,
    "v10_trade_offsets": [],
    "v4_trade_offsets": [62, 60, 58],
    "first_trade_offset_v10": null,
    "first_trade_offset_v4": 62
  }
}
```

### Implementation (hub/api/v58_monitor.py)

Add a new route `@router.get("/v58/window-analysis/{window_ts}")`. The handler executes two queries in parallel (asyncio.gather):

**Query 1 -- signal_evaluations:**
```sql
SELECT eval_offset,
       delta_pct, delta_tiingo, delta_binance, delta_chainlink, delta_source,
       vpin, regime, clob_spread, clob_mid,
       clob_up_bid, clob_up_ask, clob_down_bid, clob_down_ask,
       v2_probability_up, v2_direction, v2_agrees, v2_high_conf,
       gate_vpin_passed, gate_delta_passed, gate_cg_passed,
       gate_twap_passed, gate_timesfm_passed, gate_passed, gate_failed,
       decision,
       twap_delta, twap_direction, twap_gamma_agree
FROM signal_evaluations
WHERE asset = :asset AND window_ts = :window_ts AND timeframe = :timeframe
ORDER BY eval_offset DESC
```

**Query 2 -- strategy_decisions:**
```sql
SELECT strategy_id, strategy_version, mode, eval_offset,
       action, direction, confidence, confidence_score,
       entry_cap, collateral_pct, entry_reason, skip_reason,
       metadata_json::text
FROM strategy_decisions
WHERE asset = :asset AND window_ts = :window_ts
ORDER BY eval_offset DESC, strategy_id
```

**Query 3 -- market_data (outcome):**
```sql
SELECT outcome, open_price, close_price, resolved
FROM market_data
WHERE asset = :asset AND window_ts = :window_ts AND timeframe = :timeframe
LIMIT 1
```

All three queries run in parallel. The handler then:

1. Indexes strategy_decisions by `(eval_offset, strategy_id)` into a dict
2. Iterates signal_evaluations in `eval_offset DESC` order (T-298 down to T-2)
3. Merges strategy decisions at each offset
4. Computes `best_entry` -- the offset where `max(p_up, 1-p_up)` is highest AND the predicted direction matches the actual outcome
5. Computes `summary` stats client-side-friendly aggregates

**Performance:** For a 5m window with 2s eval interval, that is ~150 rows from signal_evaluations and ~150 x N_strategies from strategy_decisions. Both tables have indexes on `(asset, window_ts)`. This is a single-window query -- fast even without optimization.

---

## 4. Frontend Component Design

### 4.1 Trigger: clickable row in Evaluate / Strategy Lab

Both the Evaluate page and Strategy Lab page render per-window rows in tables. Add an `onClick` handler to each row that opens the Window Analysis modal.

```jsx
// In the table row:
<tr onClick={() => setAnalysisWindow(row.window_ts)} style={{ cursor: 'pointer' }}>
```

State:
```jsx
const [analysisWindow, setAnalysisWindow] = useState(null);
```

### 4.2 Modal component: `WindowAnalysisModal`

**File:** `frontend/src/pages/polymarket/components/WindowAnalysisModal.jsx`

**Props:**
- `windowTs` -- the window_ts to analyze (null = closed)
- `onClose` -- callback
- `asset` -- default "btc"
- `timeframe` -- default "5m"

**Layout (top to bottom):**

```
+---------------------------------------------------------------+
|  Window Analysis: 2026-04-12 14:30 UTC            [X] Close   |
|  Outcome: UP (+0.05%)  |  120 evaluations  |  V10: SKIP       |
+---------------------------------------------------------------+
|                                                               |
|  [A] Confidence Timeline (area chart, full width)             |
|  Y-axis: P(UP) 0.0 -- 1.0                                    |
|  X-axis: T-300 ... T-0                                        |
|  - Blue area = P(UP) over time                                |
|  - Red dashed line at 0.5 (direction flip threshold)          |
|  - Green dashed lines at 0.65 / 0.35 (DUNE gate thresholds)  |
|  - Gold star marker = best entry point                        |
|  - Orange/green bg bands showing actual outcome direction     |
|                                                               |
+---------------------------------------------------------------+
|                                                               |
|  [B] Strategy Heatmap Strip (2 rows, full width)              |
|  V10: |SKIP|SKIP|SKIP|...|SKIP|SKIP|                         |
|  V4:  |SKIP|SKIP|TRADE|...|TRADE|SKIP|                       |
|  (green = TRADE, gray = SKIP, red = ERROR)                    |
|                                                               |
+---------------------------------------------------------------+
|                                                               |
|  [C] Signal Detail Table (scrollable)                         |
|  Offset | P(UP) | Dir | Delta | VPIN | Regime | Gates | V10  |
|  T-298  | 0.52  | UP  | 0.03% | 0.51 | NORMAL | 5/6   | SKIP |
|  T-296  | 0.54  | UP  | 0.04% | 0.51 | NORMAL | 5/6   | SKIP |
|  ...                                                          |
|  T-62   | 0.84  | UP  | 0.12% | 0.67 | NORMAL | 6/6   | SKIP |  <-- best entry (highlighted)
|  ...                                                          |
|                                                               |
+---------------------------------------------------------------+
|                                                               |
|  [D] Best Entry Card                                          |
|  "Best entry at T-62: UP with 84% confidence (correct)"      |
|  "V4 would have traded. V10 skipped (DUNE gate)."            |
|                                                               |
+---------------------------------------------------------------+
```

### 4.3 Section A -- Confidence Timeline Chart

Inline SVG (no chart library dependency -- matches existing pattern in Evaluate.jsx). The chart renders:

1. **P(UP) area** -- filled area chart from `v2_probability_up` at each offset. Blue fill with 20% opacity below the line.
2. **0.5 threshold line** -- red dashed horizontal. Above = UP prediction, below = DOWN.
3. **DUNE gate lines** -- green dashed at 0.65 and 0.35. Confidence must exceed these for the DUNE gate to pass.
4. **Best entry marker** -- gold star/diamond at the offset with peak directionally-correct confidence.
5. **Outcome band** -- thin green (UP) or red (DOWN) bar at the bottom showing the actual result, giving immediate visual feedback on whether the prediction was right.

The X-axis runs from T-300 (left) to T-0 (right), matching the natural "time flows right" convention. Labels at T-300, T-240, T-180, T-120, T-60, T-0.

**Why inline SVG:** The Evaluate page already uses inline SVG for sparklines/charts. Adding Recharts as a dependency for one modal is overkill. An inline SVG area chart for ~150 points is straightforward.

### 4.4 Section B -- Strategy Action Strip

Two horizontal bars (one per strategy) showing TRADE/SKIP/ERROR at each eval_offset as a colored strip:

- Green cell = TRADE at that offset
- Gray cell = SKIP
- Red cell = ERROR

This gives an instant visual of "when did each strategy want to trade?" without reading a table. The strip is a single SVG with rect elements -- one pixel-wide column per eval_offset.

### 4.5 Section C -- Signal Detail Table

A scrollable table with all ~150 eval rows. Columns:

| Column | Source | Notes |
|---|---|---|
| Offset | `eval_offset` | Displayed as T-{offset} |
| P(UP) | `v2_probability_up` | Color-coded: green > 0.65, red < 0.35, neutral otherwise |
| Dir | derived from P(UP) | UP/DOWN pill |
| Delta | `delta_pct` | Percentage, color by sign |
| VPIN | `vpin` | Color by threshold (green >= 0.55, yellow 0.45-0.55, red < 0.45) |
| Regime | `regime` | Badge |
| Gates | count of passed gates / total | e.g. "5/6" |
| Blocking | `gate_failed` | Name of first failed gate (if any) |
| V10 | `decision` | TRADE/SKIP pill |
| V4 | from strategy_decisions | TRADE/SKIP pill |

The best-entry row is highlighted with a gold left-border and subtle background.

### 4.6 Section D -- Best Entry Card

A summary card that computes:
- The eval_offset where `max(p_up, 1 - p_up)` is highest AND the predicted direction matches the actual outcome
- Which strategies would have traded at that offset
- How much time before close (seconds)
- Whether it was in the "tradeable zone" (T-120 to T-10)

If no eval_offset had the correct direction, the card says "No correct prediction in this window" with the peak confidence and its (wrong) direction.

---

## 5. Implementation Plan

### Step 1: Hub endpoint (30 min)

Add `GET /v58/window-analysis/{window_ts}` to `hub/api/v58_monitor.py`:
- Three parallel SQL queries (signal_evaluations + strategy_decisions + market_data)
- Merge into timeline array
- Compute best_entry and summary
- Add route to the existing `router`

### Step 2: Frontend modal shell (30 min)

Create `frontend/src/pages/polymarket/components/WindowAnalysisModal.jsx`:
- Modal overlay with close button
- Fetch from `/api/v58/window-analysis/{windowTs}` on open
- Loading/error states
- Sections A-D as described

### Step 3: Confidence timeline chart (45 min)

Inline SVG area chart:
- Scale P(UP) to Y, eval_offset to X
- Draw area, threshold lines, best-entry marker
- Outcome band at bottom

### Step 4: Strategy action strip (20 min)

Two horizontal SVG strips (V10 + V4) with colored rects per offset.

### Step 5: Signal detail table (30 min)

Scrollable table with all columns. Highlight best-entry row. Color-code key columns.

### Step 6: Wire into Evaluate + Strategy Lab (15 min)

Add `onClick` to table rows in both pages. Render `<WindowAnalysisModal>` when `analysisWindow` state is set.

### Step 7: Best entry card (15 min)

Compute best entry from timeline data. Render summary sentence.

**Total: ~3 hours of focused implementation.**

---

## 6. Design Decisions

### Q1: signal_evaluations sufficient, or do we need strategy_decisions too?

**Both.** `signal_evaluations` gives the V10 gate pipeline state (which gates passed/failed) and the raw signal values. `strategy_decisions` gives each strategy's final TRADE/SKIP action, which incorporates strategy-specific logic beyond the gates (e.g., V4 uses the fusion surface, not the gate pipeline). We need both to answer "what would V10 have done?" and "what would V4 have done?"

### Q2: Modal vs dedicated page?

**Modal.** A dedicated page requires URL routing, loses context of the parent table, and adds navigation friction. A modal lets the user click a row, see the analysis, close it, and click another row -- rapid comparison. The modal is full-screen-ish (90vw x 90vh) with internal scroll.

### Q3: Chart type for prediction evolution?

**Area chart.** An area chart with P(UP) on Y-axis makes direction visually obvious: above 0.5 = UP prediction, below = DOWN. The filled area shows confidence magnitude. This is superior to a line chart (harder to see direction) or a bar chart (too many bars for 150 points).

### Q4: Best entry point calculation?

**Directionally-correct peak confidence.** For each eval_offset:
1. Determine predicted direction: `UP` if `p_up >= 0.5`, else `DOWN`
2. If predicted direction matches actual outcome, candidate confidence = `max(p_up, 1 - p_up)`
3. Best entry = candidate with highest confidence
4. Tiebreaker: prefer later offsets (closer to close = more information)

If no offset predicted correctly, best_entry.correct = false and we report the peak confidence regardless.

### Q5: Performance concern with ~150 rows per window?

Not a concern. The queries hit indexed columns `(asset, window_ts)`. Even joining strategy_decisions (2 strategies x 150 offsets = 300 rows), the total data per request is under 100KB. No pagination needed.

---

## 7. Future Extensions (Not in v1)

- **Diff two windows side-by-side** -- compare a WIN window's trajectory against a LOSS
- **Aggregate best-entry statistics** -- across all windows, where does peak confidence typically occur? (answer: probably T-60 to T-30)
- **Replay animation** -- step through the window eval-by-eval with a slider
- **Export** -- download the timeline as CSV for offline analysis
