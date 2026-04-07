# v9.0 Proposal — Gate Stack Recalibration

**Date:** April 7, 2026 18:00 UTC
**Based on:** 43 real trades + 205 Tiingo-era resolved windows (Polymarket oracle verified)
**Status:** PROPOSAL — not implemented

---

## The Problem

v8.1.2 is losing money: 29W/14L (67.4% WR), -$35 P&L on trades with window_predictions data. Wallet dropped from $131 to $67 (-$64).

## The Solution

Two new gates turn -$35 into +$21:

| Config | Trades | WR | P&L | Change |
|--------|--------|------|-------|--------|
| v8.1.2 current | 43 | 67.4% | -$35 | baseline |
| + v2.2 only | 30 | 70.0% | -$13 | +$22 |
| + block NORMAL | 19 | 73.7% | +$16 | +$51 |
| **+ Chainlink agrees (v9.0)** | **11** | **81.8%** | **+$21** | **+$56** |

## Verified on Polymarket Oracle Outcomes (N=205)

| Strategy | Windows | Correct | WR |
|----------|---------|---------|------|
| Raw engine signal | 205 | 131 | 63.9% |
| **TI+CL agree** | **169** | **158** | **93.5%** |
| v9.0 (TRANSITION+ AND agree) | 98 | 89 | 90.8% |

## Loss Autopsy (10 v2.2 losses today)

| # blocked by v9.0 | Losses saved | $ saved |
|-------------------|-------------|---------|
| 8 of 10 | $79 | 4x NORMAL + 4x CL disagree |
| 2 legitimate | -$17 | Market was wrong |

## v9.0 Gate Stack

```
G1: VPIN >= 0.55 (TRANSITION+)    ← raised from 0.45
G2: Tiingo delta >= 0.02%         ← unchanged
G3: Chainlink direction agrees     ← NEW (93.5% WR when agree)
G4: CEDAR v3.0 HIGH + agrees      ← replaces OAK v2.2
G5: CoinGlass veto check          ← unchanged
G6: Dynamic entry cap              ← unchanged
```

## Sub-Window Accuracy (gate_audit, N=68-96 per offset)

| Offset | CL+TI Agree WR | Agreement Rate |
|--------|----------------|---------------|
| T-240 | 75.0% | 58% |
| T-180 | 85.5% | 80% |
| T-130 | **96.9%** | 69% |
| T-120 | 95.7% | 76% |
| T-100 | 95.2% | 77% |
| T-80 | 95.1% | 81% |
| T-60 | **100%** | 84% |

Sweet spot: T-130 to T-60 (93-100% WR, 76-84% agreement)

## CEDAR Model

- **Test accuracy:** +5-9pp over OAK at every delta bucket
- **Endpoint:** `/v2/probability/cedar` (live, staging)
- **Ticks:** Saving to DB (237 ticks, 2 windows so far)
- **Promotion:** After 48h live comparison confirms test numbers

## Implementation

1. `VPIN_GATE = 0.55` (env var, currently 0.45)
2. Add `gate_chainlink_agree` check in `_evaluate_window()` (~10 lines)
3. Promote CEDAR to production on TimesFM service
4. Remove macro observer gate (always ALLOW_ALL)
5. All feature-flagged

## Expected Performance

| Metric | v8.1.2 | v9.0 |
|--------|--------|------|
| Trades/day | ~30 | ~8-12 |
| Win rate | 67% | ~82-91% |
| Avg win | $3.50 | $4.50 |
| Daily P&L | -$3.60 | +$21.60 |

## Caveats

- N=43 traded / N=169 agreement windows. Need N=100+ trades to confirm.
- Single day (Apr 7). Market was ranging/down.
- CEDAR test-set only — live validation pending.
- Fewer trades = more variance per trade.

## Recommendation

Deploy v9.0 with feature flags. Shadow-log for 48h (track what v9.0 WOULD do while trading v8.1.2). Promote when shadow confirms WR > 80%.
