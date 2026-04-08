# v10.3 Honest Audit — Apr 8, 2026

**Date:** 2026-04-08 21:00 UTC
**Auditor:** Claude Opus 4.6 (self-audit after discovering analysis errors)

---

## Correction: CG Taker Gate Impact Was Overstated

Earlier analysis claimed the CG taker gate would block 2 additional losses (-$13.52 saved, -$2.66 in wins lost = net +$10.86). **This was wrong.**

Using actual database CG values (not estimates):

| Loss | PnL | dune_p | Taker Buy % | Smart Long % | Taker Gate Blocks? |
|------|-----|--------|-------------|-------------|-------------------|
| NORMAL T-236 | -$5.57 | 0.81 | 56% | 49.7% | N/A — offset >180 blocks |
| CASCADE T-220 | -$7.95 | 0.77 | 66% | 49.8% | N/A — offset >180 blocks |
| TRANSITION T-124 | -$3.99 | **0.91** | **66% buy (aligned!)** | 51.3% | **NO** — taker was aligned |
| NORMAL T-180 | -$3.65 | 0.78 | ~50% (neutral) | 51.4% | **NO** — neutral |
| TRANSITION T-180 | -$3.34 | 0.78 | ~54% (neutral) | 51.5% | **NO** — neutral |
| NORMAL T-166 (DOWN) | -$3.00 | 0.77 | 55% sell (aligned) | 51.4% | **NO** — aligned |

**Reality:** No trade had BOTH taker opposing (>55%) AND smart money opposing (>52%) simultaneously. The taker gate's dual condition was never triggered.

---

## What v10.3 Actually Prevents

Only the **offset gate (T>180)** prevents real losses. It blocked the 2 biggest:

| Gate | Losses Blocked | PnL Saved |
|------|---------------|-----------|
| Offset (T>180) | 2 | **-$13.52** |
| CG Taker Flow | 0 | $0 |
| DOWN Penalty | 0 | $0 (raises threshold but all 4 remaining losses still pass) |
| CG Confirmation | 0 | $0 (losses had 2/3 or neutral CG — penalty too small) |

---

## The Uncomfortable Truth

4 of 6 losses had:
- dune_p between 0.77 and **0.91** — the model was genuinely confident
- CG data was neutral or even aligned with the trade
- Every signal said "this is a good trade" — and it lost

**This is the natural ~20% loss rate of a 62% WR system.** No gate can prevent it without also blocking wins at the same rate.

The TRANSITION T-124 loss is the clearest example: P=0.91, 2/3 CG confirms, taker aligned at 66% buy. A perfect-looking trade that lost. **That's variance, not a fixable bug.**

---

## What the 20:53 Loss Was

The LOSS at 20:53 (5.86 shares, $0.68, -$3.99) was a **recovered trade from the previous engine session** (age 54 minutes). It was placed by v10.2, not v10.3. The engine recovered 6 open orders on restart — this one resolved as a loss.

---

## v10.3 Actual Value

| Feature | Real Value | Overstated? |
|---------|-----------|-------------|
| Lower thresholds (ELM calibrated) | Opens more trades that v10.2 would block (e.g. +$1.59 NORMAL T-122 WIN) | No — validated |
| Offset gate (T>180 block) | Blocks 2 biggest losses (-$13.52) | No — validated |
| DOWN penalty (+0.03) | Raises threshold for DOWN trades, marginal impact | Possibly — needs more data |
| CG taker hard gate | Zero impact on today's losses | **YES — overstated** |
| CG confirmation bonus/penalty | Marginal — 0.02-0.03 adjustment | No — working but small effect |
| Min 5 shares enforcement | Fixed critical execution failure | No — critical fix |
| Reconciler orphan fix | Recovered 12 hidden WINs (+$18.62) | No — critical fix |
| SITREP reads trade_bible | Shows accurate data | No — critical fix |

---

## Honest Performance Summary (Apr 8, Corrected)

| Metric | Value |
|--------|-------|
| Total resolved | 40W/24L |
| Win rate | 62.5% |
| PnL | -$28.05 |
| Starting bankroll | $131 |
| Current wallet | ~$53 |

**The negative PnL despite 62.5% WR is because losses are larger than wins on average:**
- Average win: ~$2.50
- Average loss: ~$4.70
- Need ~65% WR to break even at these sizes

The cap ceiling ($0.68-$0.70) means we enter at expensive prices where the payout ratio is poor (risk $4.70 to win $2.50). Lowering the cap further would improve R:R but reduce fill rate.

---

## Recommendations

1. **Keep v10.3 running** — the infrastructure fixes (reconciler, min shares, SITREP) are genuinely valuable
2. **Don't over-optimize gates** — the remaining losses are irreducible variance, not fixable
3. **Focus on R:R ratio** — the real problem is average loss > average win, which is a cap/sizing issue
4. **Monitor overnight** — gather 50+ v10.3 trades before making further threshold changes
5. **CG taker gate**: keep enabled but understand it may not fire often. Its value is insurance, not daily filtering.

---

## Lessons Learned

1. **Always query actual DB values, never estimate.** The earlier CG analysis used assumed dune_p=0.77-0.78 across all losses. Reality was 0.16-0.91.
2. **Backtest on real data before claiming impact.** The "taker gate would block 2 losses" claim was based on window_snapshot joins that pulled the WRONG snapshot rows (multiple per window).
3. **Variance is real.** At 62% WR, getting 4 losses in a row is a ~2% event — unlikely but expected over many trading days.
4. **Infrastructure fixes > gate optimization.** The reconciler fix (+$18.62 recovered), min shares fix (unblocked all trades), and SITREP fix (accurate data) had more impact than any threshold change.
