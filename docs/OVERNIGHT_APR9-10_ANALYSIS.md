# Overnight Apr 9-10, 2026 — Adverse Selection Analysis

## Headline: Model Correct, Execution Broken

| Metric | Value |
|--------|-------|
| **Filled trades** | 41 (20W / 21L = **48.8% WR**) |
| **Net PnL** | **-$39.36** |
| **Signal eval WR** | 86W / 38L = **69% WR** (all TRADE decisions with outcomes) |
| **Unfilled signals** | 44W / 10L = **81% WR** (would-have-won if filled) |
| **Avg stake** | $3.33 (correctly sized) |

**The 32-percentage-point gap between filled WR (49%) and signal WR (81%) is adverse selection loss.**

## Comparison to Apr 8-9 Overnight

| Metric | Apr 8-9 (trending) | Apr 9-10 (choppy) |
|--------|-------------------|-------------------|
| Trades filled | 34 | 41 |
| WR | **100%** | **49%** |
| PnL | +$56.65 | -$39.36 |
| BTC range | (clean trend up) | $71,540 - $72,881 (chop) |
| Vol (stddev) | Low | $282 |

Same SEQUOIA model, same session, same overnight hours (00-05 UTC). The ONLY difference was market regime.

## Adverse Selection Explained

In a **trending market**: price moves in one direction. Your GTC limit fills when price reaches your bid — you ride the trend. Filled = winner.

In a **choppy market**: price oscillates. Your GTC limit fills when a reversal sweeps through your bid — the counterparty is the informed side. Filled = loser.

**Evidence:** Tonight's filled trades had IDENTICAL features to losses:

```
               WINS (20)    LOSSES (21)
avg_conf       0.732        0.736    ← identical
avg_delta      0.04370      0.05893  ← losses had LARGER delta
avg_offset     122          122      ← identical
```

No signal feature distinguishes wins from losses. It's pure execution roulette — whichever GTC got hit by a reversal lost.

## Regime Breakdown

```
REGIME        FILLED    SIGNAL_EVAL
              W/L  WR%  W/L  WR%
TRANSITION    7/8  47%  32/17  65%
CASCADE       7/9  44%  31/14  69%
NORMAL        5/4  56%  22/7   76%
CALM          1/0 100%  1/0   100%
```

**CASCADE broke overnight.** The regime that was 100% WR yesterday was 44% tonight. This is choppy-market cascade detection false-positiving on small reversals.

## Hourly Breakdown

```
Hour  Trades  W/L   WR%   PnL
00    9       6/3   67%   -$0.81
01    5       1/4   20%   -$12.02  ★ BLEED
02    1       1/0  100%    +$1.60
03    7       4/3   57%    -$3.82
04    7       4/3   57%    -$3.97
05    3       1/2   33%    -$5.18
21    2       0/2    0%    -$6.41  ★ OPEN BLEED
22    3       2/1   67%    -$0.20
23    4       1/3   25%    -$8.55  ★ BLEED
```

Hour 01 and 23 had loss clusters — a tighter consecutive loss cooldown (3 instead of 10) would have paused after loss 3 in each cluster.

## Offset Band Breakdown

```
T-060..090   0W/4L    0%   -$13.15  ★ NEW: previously 88.9% WR
T-091..120  12W/6L   67%    -$1.39
T-121..150   8W/11L  42%   -$24.82  ★ Second worst
```

**T-60..90 went from 88.9% WR (yesterday) to 0% (tonight).** This is NOT a signal problem — it's execution. The last-minute entries in a choppy market get adversely selected because takers sweep right before window close.

## Root Cause Analysis

### Why Signal WR is high (81% unfilled) but Filled WR is low (49%)

In a choppy market, the CLOB behavior is:
1. We post GTC at $0.68 (direction UP)
2. Market oscillates: $0.68 → $0.65 → $0.70 → $0.67 → $0.72
3. A taker sweeps to $0.72 (takes liquidity) — our $0.68 GTC never fills because the ask moved above our limit
4. Later, market drops and another taker sweeps down to $0.65 — our $0.68 NO token (wrong side) fills
5. Window closes: price back to middle, our side loses

The GTC orders that fill are the ones where the market **passed through our limit on the way to resolving against us**. The ones that don't fill are where the market moved **with** our direction but didn't come back.

This is textbook adverse selection — the fills happen on the reversals, not on the trend.

### Why this didn't happen yesterday

Yesterday overnight was trending. Our GTC filled as the market moved with us — we caught the momentum. No reversals = no adverse fills.

## Data Collection Gap

When a GTC expires without filling:
- `signal_evaluations` records the TRADE decision ✅
- `market_data` records the outcome ✅
- `trades` table records the order but status=EXPIRED ✅
- `trade_bible` has no entry (no resolution to track) ❌
- `poly_trade_history` has no entry (no fill) ❌

This means **we can only do counterfactual analysis via signal_evaluations × market_data joins**, which is how we computed the 81% unfilled WR.

## v10.7 Implications

The original v10.7 plan proposed:
1. ✅ Kill switch auto-recovery — still needed
2. ❌ ~~Session-aware sizing~~ — **DROP**. Overnight Apr 9-10 broke the "Asian = 100%" pattern. Time-of-day is a proxy for volatility, not a cause.
3. ✅ Tighter consecutive loss cooldown — still needed
4. ✅ Raise MAX_DRAWDOWN_KILL — still needed (with auto-resume)

**NEW #1 priority:** Regime-aware execution mode.
- Trending market → GTC maker (0% fee, ride the trend)
- Choppy market → FAK taker (pay 7.2% fee, guarantee fill quality)
- Detection via 5-minute volatility ratio or VPIN threshold

**v10.6 deployment:** HOLD. Confidence-scaled lower caps would make adverse selection worse in choppy markets. Deploy only after regime-aware execution is working.

## Key Metric to Track Post-Fix

**Filled WR vs Signal WR gap.** Currently 81% - 49% = 32 percentage points of execution loss. Target: gap <10 percentage points. If the gap persists, the regime detection isn't working.
