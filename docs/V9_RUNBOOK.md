# v9.0 Implementation Runbook

**Created:** April 7, 2026 21:30 UTC
**Status:** READY TO IMPLEMENT (pending approval)

---

## Data Summary

- **1,762 oracle-verified evaluations** from gate_audit (100 unique windows, 19 offsets each)
- **206 oracle-resolved window_predictions** (single day: Apr 7)
- **84 LIVE trades** with outcomes (Apr 5-7)
- **Signal_evaluations table:** 162 rows, started ~20:13 UTC Apr 7 (new table, limited data)

## Key Findings

### 1. Source Agreement = The Edge
- CL+TI Agree: **94.7% WR** (161/170 windows)
- CL+TI Disagree: 80.6% CL-correct (but engine signal was WRONG on those)
- Global gate_audit: 1,317 agree evals = **87.7% WR**

### 2. Golden Zone: T-130 to T-60
- Agreement WR jumps from 87% at T-150 to **95.5% at T-130**
- Stays 93-100% from T-130 to T-60
- Chainlink reaches 100% accuracy at T-60 (freshest oracle data)

### 3. LIVE Entry Price Reality (NOT paper)
| Fill Price | LIVE WR | LIVE P&L | Paper WR | Paper P&L |
|-----------|---------|----------|----------|-----------|
| $0.40-$0.55 | **29%** | **-$54** | 70% | +$17,320 |
| $0.55-$0.65 | **86%** | **+$48** | N/A | N/A |
| $0.65-$0.75 | 63% | -$50 | N/A | N/A |

**Paper data is dangerously misleading.** The $0.55-$0.65 zone is the only profitable live band.

### 4. OAK/CEDAR is Binary Garbage
- Confirmed live: `probability_up: 0.00909` at ALL offsets (T-60, T-90, T-120, T-240)
- `seconds_to_close` parameter is IGNORED — same output regardless
- OAK and CEDAR return identical values
- Unique probabilities from signal_evaluations: `[0.0, 0.009, 0.185, 0.333, 0.444, 0.778, 0.909, 1.0]`

### 5. Trades Per Day
- **249 BTC windows/day** (Apr 7)
- Gate_audit covers 100/206 resolved (48%) — rest were before gate_audit deployed
- Many early trades lack CL/TI data (feeds added mid-session)

## Data Caveats (MUST address before going live)

1. **Single day of data** — Apr 7 only. Market was ranging/downtrend.
2. **Gate_audit covers 48% of windows** — deployed mid-day.
3. **Signal_evaluations started at 20:13 UTC** — only 162 rows, ~1 hour of data.
4. **Most LIVE trades lack CL/TI delta data** — feeds weren't recording early.
5. **Fill rate at $0.55-$0.65 is UNKNOWN** — CLOB depth at these prices can be thin.
6. **v9.0 backtest on LIVE trades shows 44% WR** — BUT this is because most trades happened before CL/TI data was available, so v9.0 would skip them. Not a meaningful test.

## Implementation Steps

### Phase 0: Shadow Mode (2-3 days)
- [ ] Deploy v9.0 logic in shadow mode (log decisions, don't execute)
- [ ] Compare v9.0 decisions against actual v8.1.2 outcomes
- [ ] Validate: does shadow WR match expected 90%+?
- [ ] Validate: does $0.65 cap fill rate work?

### Phase 1: Continuous Eval Loop
- [ ] **File:** `engine/data/feeds/polymarket_5min.py`
- [ ] Add `V9_CONTINUOUS_EVAL=true` env var
- [ ] When enabled: emit CLOSING signal every 2s from T-240 to T-60
- [ ] Keep backward compat: if false, use old fixed offsets

### Phase 2: Source Agreement Gate
- [ ] **File:** `engine/strategies/five_min_vpin.py` → `_evaluate_window()`
- [ ] After delta calculation, add ~15 lines:
  ```python
  if delta_chainlink is not None and delta_tiingo is not None:
      cl_dir = "UP" if delta_chainlink > 0 else "DOWN"
      ti_dir = "UP" if delta_tiingo > 0 else "DOWN"
      if cl_dir != ti_dir:
          return None  # DISAGREE → skip
      direction = cl_dir
  ```
- [ ] Feature flag: `V9_SOURCE_AGREEMENT=true`

### Phase 3: Tiered VPIN + Dynamic Caps
- [ ] Replace `_get_v81_cap()` with:
  ```python
  def _get_v9_cap(offset: int, vpin: float) -> Optional[float]:
      if offset > 130:
          if vpin >= 0.65: return 0.55  # CASCADE early
          return None  # Skip non-CASCADE early
      return 0.65  # Golden zone
  ```
- [ ] Feature flags: `V9_CAP_EARLY=0.55`, `V9_CAP_GOLDEN=0.65`, `V9_VPIN_EARLY=0.65`, `V9_VPIN_LATE=0.45`

### Phase 4: Remove Dead Gates
- [ ] Delete TWAP override code (feature-flagged OFF, net harmful)
- [ ] Delete TimesFM agreement code (47.8% accuracy)
- [ ] Delete macro observer integration (always ALLOW_ALL)
- [ ] Keep OAK/CEDAR logging (for future calibration analysis)

### Phase 5: Deploy
- [ ] Set env vars on Railway
- [ ] Monitor first 24h closely
- [ ] Compare live WR vs shadow predictions

## Env Vars for v9.0

```env
V9_CONTINUOUS_EVAL=true
V9_SOURCE_AGREEMENT=true
V9_CAP_EARLY=0.55
V9_CAP_GOLDEN=0.65
V9_VPIN_EARLY=0.65
V9_VPIN_LATE=0.45
FIVE_MIN_EVAL_OFFSETS=240,230,220,210,200,190,180,170,160,150,140,130,120,110,100,90,80,70,60
```

## Validation Checklist (before going live)

- [ ] Shadow log shows 90%+ WR on agreement windows
- [ ] $0.65 cap fills are actually executing (not all unfilled)
- [ ] CL+TI data availability is 95%+ (no missing feeds)
- [ ] signal_evaluations table growing correctly
- [ ] gate_audit recording all evaluations with oracle backfill
- [ ] Telegram notifications working for v9.0 decisions

## Visual Report
See `docs/v9_pricing_surface.html` for the full rendered analysis.
