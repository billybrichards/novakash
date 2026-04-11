# v9.0 Overnight Monitor — April 8, 2026

**Generated:** 09:56 UTC
**Deploy:** 2026-04-07 23:28 UTC
**Early CASCADE disabled:** 2026-04-08 08:43 UTC (V9_VPIN_EARLY=9.99)

## Wallet

Current: unknown
Starting: $131.00

## v9.0 Resolved Trades

| Tier | W | L | WR | PnL |
|------|---|---|------|-----|
| **GOLDEN** | **6** | **0** | **100%** | **$+18.50** |
| EARLY CASCADE | 2 | 4 | 33% | $-15.04 |
| **TOTAL** | **8** | **4** | **67%** | **$+3.46** |

### Trade Log

| Placed | Resolved | Dir | Tier | Entry | Out | PnL |
|--------|----------|-----|------|-------|-----|-----|
| 23:13 | 23:21 | T | GOLDEN | $0.39 | WIN | $+3.18 |
| 23:53 | 00:02 | T | GOLDEN | $0.54 | WIN | $+2.92 |
| 00:17 | 00:37 | T | GOLDEN | $0.64 | WIN | $+3.04 |
| 00:27 | 00:43 | T | GOLDEN | $0.65 | WIN | $+2.98 |
| 00:32 | 00:53 | T | GOLDEN | $0.65 | WIN | $+3.19 |
| 01:12 | 02:10 | T | GOLDEN | $0.65 | WIN | $+3.19 |
| 23:36 | 23:46 | C | EARLY | $0.55 | WIN | $+4.50 |
| 23:41 | 23:51 | C | EARLY | $0.55 | LOSS | $-5.49 |
| 00:06 | 00:27 | C | EARLY | $0.55 | LOSS | $-6.16 |
| 00:21 | 00:36 | C | EARLY | $0.55 | WIN | $+3.97 |
| 01:26 | 02:25 | C | EARLY | $0.55 | LOSS | $-6.16 |
| 02:41 | 03:25 | C | EARLY | $0.55 | LOSS | $-5.70 |

## Oracle Agreement Accuracy (v9 lifetime)

**Overall:** 7W/4L = **63.6% WR** (N=11)
**Disagree skips:** 0

### By Regime

| Regime | W | L | WR | Note |
|--------|---|---|------|------|
| CASCADE | 2 | 4 | 33% | ALL LOSSES HERE |
| TRANSITION | 4 | 0 | 100% | PERFECT |
| NORMAL | 1 | 0 | 100% | PERFECT |

### By Hour (UTC)

| Hour | W | L | WR |
|------|---|---|------|
| 00:00 | 4 | 1 | 80% |
| 01:00 | 1 | 1 | 50% |
| 02:00 | 0 | 1 | 0% |
| 23:00 | 2 | 1 | 67% |

## Signal Evaluations

- Total evals: 1257
- Windows: 122
- Trade decisions: 113 (9.0%)
- Skip decisions: 1144 (91.0%)
- Avg VPIN on trades: 0.601
- Avg VPIN on skips: 0.549

## Pending Orders

- Total: 2 (GOLDEN: 2, EARLY: 0)

## Data Consistency Check

| Table | Rows since deploy |
|-------|-------------------|
| signal_evaluations | 1257 |
| gate_audit | 1257 |
| window_snapshots | 1257 |
| window_predictions | 122 |
| trades | 105 |
| telegram_notifications | 606 |

**signal_evaluations vs gate_audit:** MATCH
**window_snapshots vs signal_evaluations:** MATCH

## v9.1 Ideas (Based on Overnight Data)


### Problem: CASCADE regime is 25% WR (1W/3L)

All 4 lifetime losses were CASCADE (VPIN >= 0.65). Possible causes:
1. **High VPIN = volatile/choppy** — price reverses more often in CASCADE
2. **Chainlink staleness** — CL updates every ~30s. In CASCADE (high vol), price moves fast enough to flip direction between CL update and oracle close
3. **Early entries compound the problem** — CASCADE fires at T-230+ giving price 3+ minutes to reverse

### Observation: TRANSITION/NORMAL = 100% WR (5W/0L)

Lower VPIN (0.45-0.65) windows are perfectly accurate. The calm, steady markets are where agreement signal shines.

### Potential v9.1 Adjustments

**Option A: Block CASCADE entirely**
```
V9_VPIN_LATE=0.45    # min VPIN in golden zone (keep)
V9_VPIN_MAX=0.65     # NEW: max VPIN cap — skip CASCADE
```
Would have avoided all 4 losses. But also missed 1 CASCADE win.
Net: +$15.04 saved, -$4.50 missed = +$10.54 improvement.

**Option B: CASCADE only at very late offsets**
Only trade CASCADE at T-80 or later (when CL is freshest).
Early CASCADE + late offset = potentially safe.

**Option C: Require larger delta in CASCADE**
CASCADE + tiny delta (0.02-0.05%) = noise. Require delta >= 0.10% in CASCADE.

**Option D: Wait for more data**
N=11 is too small. US/EU session (13:00-21:00 UTC) was where 94.7% WR was measured.
Overnight might just be a different market microstructure.

### Recommendation
**Keep monitoring through US/EU session before changing.** The 94.7% WR data was from daytime.
Overnight is thin, choppy, and may not be representative.
Early CASCADE is already disabled — that was the immediate fix.
