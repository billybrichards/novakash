# Trading Window Analysis — 2026-04-12

## Methodology

Run directly against Railway PostgreSQL. Two tables joined:
- `signal_evaluations`: eval data per 2s tick (v2_direction, v2_probability_up, vpin, clob_up_ask, clob_down_ask, eval_offset)
- `window_snapshots`: per-window close_price, open_price (ground truth: close>open=UP)

Ground truth: `close_price > open_price → UP`, `close_price < open_price → DOWN`

**NOT** `oracle_outcome` (always NULL) or `actual_direction` (doesn't exist). Use prices.

### Quick-run script

```bash
export PUB_URL="postgresql://postgres:PASSWORD@hopper.proxy.rlwy.net:35772/railway"
python3 analysis/run_window_analysis.py
```

## Key Results (71,540 windows, 4.5M evaluations)

### A1: Signal accuracy peaks at T-120 to T-135

| Offset | Accuracy | Notes |
|--------|---------|-------|
| T-240 | 49.1% | Worse than random |
| T-180 | 54.7% | OK |
| **T-135** | **55.6%** | **Peak** |
| T-120 | 55.1% | Very good |
| T-90 | 48.7% | DROPS below 50% |
| T-60 | 45.2% | Anti-predictive |

**Key insight:** Signal gets WORSE below T-90. CLOB has already priced in the outcome.

### A2: Confidence threshold matters hugely

At T-90-150, baseline 63.9% (dist>=0.12):
- `high(>20% distance)` at T-120: **65.1%**
- `strong(12-20%)` at T-120: 64.3%
- `mod(6-12%)`: 37.6% — anti-predictive, never trade
- `weak(<6%)`: 32.4% — anti-predictive, never trade

**Only trade if confidence_distance >= 0.12 (strong or high band)**

### A3: CLOB ask is the biggest signal

**Critical finding (corrected):**

| Filter | Accuracy | EV (at $0.56 ask) |
|--------|---------|---------|
| DOWN pred + NO ask ≤ $0.58 + dist≥0.12 | **82.5%** | +0.265 |
| DOWN pred + NO ask ≤ $0.58 + dist≥0.15 + VPIN≥0.55 | **82.8%** | +0.268 |
| UP pred + YES ask ≤ $0.58 | 1.8% | -0.542 |

**UP predictions when YES is cheap → market says unlikely, don't trade UP.**
**DOWN predictions when NO is cheap → market agrees, very high WR.**

### ⚠️ Caveat: Dataset is bearish (86% DOWN windows)

Only 12% of windows ended UP in this data. The 82% DOWN WR partially reflects bear trend bias, not pure edge. Need to test on neutral periods before trusting for live trading.

## Optimal Config Recommendation

```
eval_offset: T-90 to T-150
confidence_distance: >= 0.12 (only strong/high confidence)
direction: DOWN preferred
cap: follow CLOB — buy NO when clob_down_ask <= $0.58
     for UP: only if clob_up_ask <= $0.56 AND VPIN >= 0.55
VPIN filter: >= 0.55 adds +0.9pp vs baseline
```

## Schema Notes

- `signal_evaluations.v2_direction` = Sequoia predicted direction (UP/DOWN)
- `signal_evaluations.v2_probability_up` = calibrated probability (0.5 = no signal)
- `signal_evaluations.clob_up_ask` / `clob_down_ask` = CLOB prices at eval time
- `window_snapshots.direction` = what the engine's source agreement decided
- `window_snapshots.oracle_outcome` = NULL (reconciler doesn't populate this column)
- `window_snapshots.close_price` / `open_price` = use for ground truth
- `ticks_v3_composite` timestamp doesn't align with signal_evaluations timestamps — V3 join needs window-level aggregation, not direct ts match

## Re-running This Analysis

```python
# Get public DB URL from Railway dashboard
export PUB_URL="postgresql://postgres:PASSWORD@hopper.proxy.rlwy.net:35772/railway"
# Or get from Montreal engine .env (internal URL) via EC2 Instance Connect
```

See `docs/analysis/run_window_analysis.py` for full script.
