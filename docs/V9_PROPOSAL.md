# v9.0 Proposal — Continuous Evaluation with Source Agreement Gate

**Date:** April 7, 2026 21:00 UTC
**Based on:** 43 real trades + 205 shadow windows (all Polymarket oracle verified)
**Supersedes:** v8.1.2 (fixed offsets, no source agreement gate)
**Status:** PROPOSAL — ready to implement

---

## The Problem

v8.1.2 loses money: 30W/14L (68.2% WR), -$20.09 P&L. The 68% WR sounds OK but at $0.73 average fill, breakeven is 73%. We're underwater.

**Root causes identified:**
1. **No source agreement gate** — engine trades even when Chainlink disagrees (9.1% WR when they disagree)
2. **Fixed eval offsets [240,180,120,60]** — misses the golden zone T-130..T-100 where agreement WR is 93-97%
3. **NORMAL regime leak** — 4 of 5 post-fix losses were NORMAL (VPIN < 0.55)
4. **OAK/CEDAR probability is binary garbage** — outputs 0.009 or 0.991 regardless of offset (confirmed live: same value at T-60, T-120, T-240)

## The Core Insight

Polymarket resolves against the **Chainlink oracle**. When our Tiingo delta and Chainlink delta agree on direction, we're betting WITH the judge. The data:

| Engine vs Chainlink | N | Engine WR |
|--------------------|----|-----------|
| **AGREE** | 134 | **92.5%** |
| **DISAGREE** | 66 | **9.1%** |

This is the single most important filter. Everything else is noise by comparison.

## Sub-Window Accuracy Surface (Oracle-Verified, N=68-96 per offset)

| Offset | CL Acc | TI Acc | CL+TI Agree N | Agree % | **Agree WR** |
|--------|--------|--------|---------------|---------|-------------|
| T-240 | 60.4% | 68.8% | 56 | 58.3% | 75.0% |
| T-210 | 66.7% | 71.9% | 65 | 67.7% | 78.5% |
| T-180 | 76.8% | 80.0% | 76 | 80.0% | 85.5% |
| T-150 | 83.9% | 79.6% | 77 | 82.8% | 88.3% |
| **T-130** | 81.7% | 82.8% | 64 | 68.8% | **96.9%** |
| T-120 | 81.5% | 88.0% | 70 | 76.1% | **95.7%** |
| T-110 | 82.4% | 84.6% | 71 | 78.0% | 93.0% |
| T-100 | 82.9% | 86.6% | 63 | 76.8% | **95.2%** |
| T-90 | 84.6% | 84.6% | 62 | 79.5% | 93.5% |
| T-80 | 85.3% | 88.0% | 61 | 81.3% | **95.1%** |
| T-70 | 89.0% | 86.3% | 61 | 83.6% | **95.1%** |
| **T-60** | **100%** | 83.8% | 57 | 83.8% | **100%** |

---

## v9.0 Architecture: Continuous 2s Polling

### Paradigm Shift

v8.x fires at fixed offsets [240, 180, 120, 60] then waits. v9.0 polls every 2 seconds from T-240 to T-60, checking agreement at each tick. **First moment all conditions align → bid.**

```
Window opens (T-300)
  │
  ├─ T-300..T-240: IDLE — collect prices, build VPIN
  │
  ├─ T-240..T-60:  CONTINUOUS EVAL (every 2s)
  │   │
  │   ├─ Fetch: Chainlink price, Tiingo price, VPIN, CoinGlass
  │   ├─ Compute: delta_chainlink, delta_tiingo from window open
  │   ├─ Check: do they agree on direction?
  │   ├─ Check: does VPIN pass threshold for this tier?
  │   ├─ Check: does CoinGlass NOT veto (3+ signals)?
  │   ├─ If ALL pass → compute dynamic cap → FOK ladder → DONE
  │   └─ If any fail → log to signal_evaluations → wait 2s → retry
  │
  ├─ T-60..T-0:    DEADLINE — if no trade yet, best signal or skip
  │
  └─ T-0: Window closes, await Polymarket oracle resolution
```

### Why 2s Polling?

- Chainlink updates every ~30s heartbeat or 5bp move
- Tiingo updates every 2s
- At 2s polling, we catch agreement within 1 Tiingo tick of it occurring
- 90 evaluations per window (T-240 to T-60 = 180s / 2s) — plenty of chances

---

## v9.0 Gate Stack

### The Pipeline (evaluated every 2s)

```
┌─────────────────────────────────────────────────────────┐
│  GATE 1: Source Agreement (HARD GATE)                   │
│  ─────────────────────────────────────                  │
│  Chainlink delta direction == Tiingo delta direction    │
│  If DISAGREE → SKIP (9.1% WR = guaranteed loss)        │
│  If either source unavailable → fall through to G1b    │
│                                                         │
│  G1b: Fallback (single source available)                │
│  If only Chainlink → require |delta_CL| > 0.05%        │
│  If only Tiingo → require |delta_TI| > 0.05%           │
│  If neither → SKIP                                      │
└──────────────────────┬──────────────────────────────────┘
                       │ AGREE
┌──────────────────────▼──────────────────────────────────┐
│  GATE 2: VPIN Regime Filter                             │
│  ──────────────────────                                 │
│  Tier A (T-240..T-150): VPIN >= 0.55 (TRANSITION+)     │
│    → Early bets need informed flow confirmation         │
│    → CASCADE (VPIN >= 0.65) gets extra confidence       │
│                                                         │
│  Tier B (T-149..T-60): VPIN >= 0.45                     │
│    → Late bets are safer (93-100% agree WR)             │
│    → Lower VPIN bar because agreement IS the edge       │
│                                                         │
│  NORMAL regime (VPIN < 0.45): ALWAYS SKIP               │
│    → 4 of 5 post-fix losses were NORMAL                 │
│    → No informed flow = no conviction                   │
└──────────────────────┬──────────────────────────────────┘
                       │ PASS
┌──────────────────────▼──────────────────────────────────┐
│  GATE 3: Delta Magnitude                                │
│  ───────────────────────                                │
│  |delta_tiingo| >= 0.02% (standard)                     │
│  CASCADE override: >= 0.005% (near-zero bar)            │
│                                                         │
│  Rationale: tiny deltas (<0.02%) can flip in 2s.        │
│  CASCADE gets lower bar because momentum is real.       │
└──────────────────────┬──────────────────────────────────┘
                       │ PASS
┌──────────────────────▼──────────────────────────────────┐
│  GATE 4: CoinGlass Veto (unchanged from v7.1)           │
│  ─────────────────────────────────────────               │
│  5 micro-signals, need 3+ to VETO:                      │
│   1. Smart money opposing (top traders >52% other side)  │
│   2. Funding rate extreme (opposing direction)           │
│   3. Crowd overleveraged (>60% on our side)              │
│   4. Taker volume opposing (>60% selling into our UP)    │
│   5. CASCADE + taker divergence                          │
│                                                         │
│  3+ signals = VETO (skip this eval, retry in 2s)        │
│  CoinGlass is SOFT — veto lifts if data changes         │
└──────────────────────┬──────────────────────────────────┘
                       │ PASS (or no CG data)
┌──────────────────────▼──────────────────────────────────┐
│  GATE 5: Dynamic Entry Cap                              │
│  ─────────────────────────                              │
│  Cap = f(seconds_to_close, agreement_WR)                │
│                                                         │
│  Tier A (T-240..T-180): cap = $0.55                     │
│    Agreement WR 75-85% → breakeven $0.55 → margin +20pp │
│                                                         │
│  Tier B (T-179..T-131): cap = $0.65                     │
│    Agreement WR 85-93% → breakeven $0.65 → margin +20pp │
│                                                         │
│  Tier C (T-130..T-60):  cap = $0.83                     │
│    Agreement WR 93-100% → breakeven $0.83 → margin +10pp│
│    $0.83 = sweet spot per docs/LIVE_TRADING_PRICING.md  │
│    Most CLOB liquidity sits at $0.73 → high fill rate   │
│                                                         │
│  Floor: ALWAYS $0.30 (no fills below floor)             │
└──────────────────────┬──────────────────────────────────┘
                       │ cap computed
┌──────────────────────▼──────────────────────────────────┐
│  EXECUTION: FOK Ladder                                  │
│  ─────────────────────                                  │
│  Start at best_ask (from CLOB), cap at dynamic cap      │
│  5 attempts, 2s between, $0.01 bump per miss            │
│  If filled → DONE, record trade                         │
│  If all 5 fail → window marked ATTEMPTED, no retry      │
│  One trade per window max (dedup by window_ts)           │
└─────────────────────────────────────────────────────────┘
```

---

## What's REMOVED from v8.x

| Gate | v8.x | v9.0 | Why |
|------|------|------|-----|
| OAK/CEDAR probability | G4 (HIGH + agrees) | **REMOVED** | Outputs binary 0.009/0.991 — useless. Confirmed live: same value at T-60/T-120/T-240. Cannot distinguish confidence levels. |
| TWAP override | Feature-flagged OFF | **REMOVED** | Blocked 12 windows, 8 were winners. Net harmful. |
| TWAP gamma gate | Feature-flagged OFF | **REMOVED** | Blocked more winners than losers. |
| TimesFM agreement | Feature-flagged OFF | **REMOVED** | 47.8% accuracy — worse than coin flip. |
| Macro observer | Always ALLOW_ALL | **REMOVED** | Never fires. Dead code. |
| Fixed eval offsets | [240,180,120,60] | **Continuous 2s** | Misses golden zone T-130..T-100 |

### OAK/CEDAR: Why Removed, What Replaces It

OAK v2.2 and CEDAR both return `probability_up: 0.00909` for BTC regardless of `seconds_to_close` (confirmed live query: identical at T-60, T-90, T-120, T-240). The underlying TimesFM quantile surface IS real data, but the LightGBM calibration layer snaps to binary extremes. Until the probability layer is fixed:

- **OAK/CEDAR direction agree/disagree** → subsumed by Chainlink+Tiingo agreement (which is the actual oracle, not a model)
- **OAK/CEDAR confidence** → meaningless (96.5% of predictions are "HIGH CONF")
- **Continue logging** OAK/CEDAR predictions to `signal_evaluations` for future analysis
- **Revisit** when calibration produces continuous values (0.55, 0.70, 0.85, etc.)

---

## What's KEPT from v8.x

| Component | Status | Notes |
|-----------|--------|-------|
| CoinGlass veto (3+ signals) | **KEPT** | Catches crowd-against-us scenarios. Soft gate — retries in 2s. |
| VPIN regime classification | **KEPT but simplified** | NORMAL always skips. TRANSITION+ trades. No sub-regime logic. |
| Delta magnitude floor | **KEPT** | 0.02% standard, 0.005% CASCADE. Prevents noise trades. |
| FOK ladder execution | **KEPT** | 5 attempts, 2s interval, $0.01 bump. Proven execution. |
| Dynamic entry caps | **REBUILT** | Now 3 tiers based on empirical agreement WR, not offset alone. |

---

## Expected Performance

### Based on 205 Oracle-Verified Windows

| Config | Windows | Correct | WR | Est. Daily Trades | Est. Daily P&L |
|--------|---------|---------|------|-------------------|----------------|
| v8.1.2 (current) | 205 | 131 | 63.9% | ~30 | -$3.60 |
| + CL+TI agree only | 169 | 158 | 93.5% | ~20 | +$28 |
| + VPIN >= 0.45 | ~140 | ~131 | ~93.6% | ~16 | +$24 |
| **v9.0 full stack** | **~110** | **~100** | **~91%** | **~12** | **+$22** |

### Profit Per Trade at Each Cap Tier

| Tier | Cap | Avg Entry | WR | Win Payout | Loss | EV/trade |
|------|-----|-----------|------|-----------|------|----------|
| A (T-240..T-180) | $0.55 | ~$0.55 | 80% | +$0.43 | -$0.55 | +$0.23 |
| B (T-179..T-131) | $0.65 | ~$0.65 | 90% | +$0.33 | -$0.65 | +$0.23 |
| C (T-130..T-60) | $0.83 | ~$0.73 | 95% | +$0.25 | -$0.73 | +$0.20 |

Note: Tier C cap is $0.83 but most CLOB fills happen at $0.73. The higher cap means we rarely miss fills.

### Backtested on Today's 44 Trades

| Config | Trades | WR | P&L |
|--------|--------|------|------|
| v8.1.2 actual | 44 | 68.2% | -$20.09 |
| **v9.0 simulated** | **~15** | **~87%** | **+$18** |

If v9.0 gates had been active all day: 29 of 44 trades would have been blocked (most losses), keeping ~15 high-confidence trades.

---

## Implementation Plan

### Step 1: Continuous Eval Loop (replace fixed offsets)

**File:** `engine/data/feeds/polymarket_5min.py`

Change `FIVE_MIN_EVAL_OFFSETS` from `[240,180,120,60]` to continuous polling:
- Add new env var: `FIVE_MIN_CONTINUOUS_EVAL=true`
- When enabled: fire CLOSING signal every 2s from T-240 to T-60
- Dedup: strategy tracks `_last_executed_window` to prevent double-trading

### Step 2: Source Agreement Gate

**File:** `engine/strategies/five_min_vpin.py` → `_evaluate_window()`

Add ~15 lines after delta calculation:
```python
# v9.0 G1: Source agreement gate
if delta_chainlink is not None and delta_tiingo is not None:
    cl_dir = "UP" if delta_chainlink > 0 else "DOWN"
    ti_dir = "UP" if delta_tiingo > 0 else "DOWN"
    if cl_dir != ti_dir:
        return None  # DISAGREE → 9.1% WR → skip
    direction = cl_dir  # Both agree — use shared direction
```

### Step 3: Rebuild Dynamic Caps

**File:** `engine/strategies/five_min_vpin.py` → `_get_v81_cap()`

Replace offset-only tiers with agreement-WR-based tiers:
```python
def _get_v9_cap(offset: int) -> float:
    if offset >= 180: return 0.55   # Tier A: early, cheap
    if offset >= 131: return 0.65   # Tier B: mid
    return 0.83                     # Tier C: golden zone
```

### Step 4: Simplify VPIN Gate

Remove regime sub-logic. Two states:
- VPIN < 0.45 → SKIP (NORMAL)
- VPIN >= 0.45 → TRADE (early tier requires 0.55)

### Step 5: Remove Dead Gates

Delete feature-flagged-off code: TWAP override, TWAP gamma gate, TimesFM agreement, macro observer. Keep CoinGlass veto.

### Step 6: Feature Flags

All behind env vars for safe rollout:
```env
V9_CONTINUOUS_EVAL=true      # 2s polling (false = old fixed offsets)
V9_SOURCE_AGREEMENT=true     # CL+TI agreement gate
V9_CAP_TIER_A=0.55
V9_CAP_TIER_B=0.65
V9_CAP_TIER_C=0.83
V9_VPIN_EARLY=0.55           # VPIN floor for T-240..T-150
V9_VPIN_LATE=0.45            # VPIN floor for T-149..T-60
```

---

## Caveats & Risks

1. **Sample size**: N=205 shadow windows, N=44 real trades. Need N=200+ trades for HIGH confidence. Plan: shadow-log v9.0 decisions for 48h while trading v8.1.2.
2. **Single day**: All data from Apr 7 (ranging/downtrend). May not generalize to trending or volatile conditions.
3. **Fewer trades**: ~12/day vs ~30/day. Higher WR but more variance per trade. Could have consecutive skip streaks.
4. **Chainlink staleness at early offsets**: CL updates every ~30s. At T-240, CL price could be 30s stale. Agreement WR at T-240 is 75% (good but not great). The empirical surface already accounts for this.
5. **CLOB liquidity**: Higher caps ($0.83) should improve fill rates since most liquidity is at $0.73. But thin books at $0.55-$0.65 mean Tier A/B fills will be spotty.
6. **OAK/CEDAR**: Intentionally excluded because calibration is broken. If fixed (continuous probabilities instead of binary), can add back as G5 confidence multiplier.

---

## Rollout Plan

1. **Day 1**: Deploy with `V9_SOURCE_AGREEMENT=true` only. Keep existing caps and offsets. Measure agreement gate impact alone.
2. **Day 2**: Enable `V9_CONTINUOUS_EVAL=true`. Switch to 2s polling. Measure fill rate and timing distribution.
3. **Day 3**: Switch to v9.0 dynamic caps. Monitor fill rates at each tier.
4. **Day 7**: Review first week. Adjust VPIN thresholds and caps based on live data.

---

## Decision: What About OAK/CEDAR Direction?

Currently OAK/CEDAR v2.2 is used as a binary agree/disagree gate. The data shows:

| OAK direction | N | Standalone Acc | With Engine Agree |
|--------------|-----|---------------|-------------------|
| T-60 | 200 | 74.5% | 78.0% |
| T-120 | 200 | 68.0% | 73.9% |

OAK adds ~4pp over engine signal alone. But **Chainlink agreement adds +27pp** (65% → 92.5%). The marginal value of adding OAK on top of CL+TI agreement is minimal:

| Config | WR |
|--------|------|
| CL+TI agree | 93.5% |
| CL+TI agree + OAK agrees | ~94.5% |

+1pp for additional complexity and a binary model. Not worth it now. **Keep logging, revisit when calibration is fixed.**
