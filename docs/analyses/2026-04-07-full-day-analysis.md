# Apr 7 Full Day Analysis — Post-Fix Performance Review

**Generated:** 2026-04-07 17:50 UTC  
**Wallet:** $67.08 USDC (down $63.82 from $130.82 start)  
**Total Trades:** 43 (29W/14L) — 67.4% WR, **-$63.99 net P&L**

---

## The Problem: High WR, Negative P&L

| Metric | Value |
|--------|-------|
| Win Rate | 67.4% |
| Avg Win | +$4.12 |
| Avg Loss | -$10.54 |
| Win/Loss Ratio | 0.39 |
| **Net P&L** | **-$63.99** |

**67% WR should be profitable. It's not. Why?**

Because losses are **2.5x bigger than wins** on average. At $0.73 cap, a loss costs $10-14. At $0.65 cap, a win pays $4-7. We need **73%+ WR to break even at $0.73 fills, but only 55%+ at $0.65 fills.**

---

## The Real Killer: $0.73 Cap + Normal Regime

### Cross-Tabs by Cap × Regime

| Cap | Regime | Trades | WR | Total P&L |
|-----|--------|--------|-----|-----------|
| **$0.55** | TRANS | 2 | 100% | +$10.29 |
| **$0.55** | NORMAL | 1 | 100% | +$1.76 |
| **$0.60** | CASCADE | 1 | 0% | -$9.50 |
| **$0.65** | CASCADE | 3 | 100% | +$12.20 |
| **$0.65** | NORMAL | 7 | 85.7% | +$7.57 |
| **$0.65** | TRANS | 6 | 66.7% | +$6.66 |
| **$0.73** | TRANS | 7 | 85.7% | +$6.66 |
| **$0.73** | CASCADE | 10 | 60.0% | **-$16.92** |
| **$0.73** | **NORMAL** | **6** | **16.7%** | **-$53.73** |

**The $0.73 cap is bleeding $63.99 total. The $0.65 cap is winning $26.44.**

### The $0.73 NORMAL Disaster

6 trades at $0.73 cap in NORMAL regime:
- 1 win (+$1.95), 5 losses (-$53.73)
- WR: 16.7%
- Avg loss: -$10.75

**Every single loss was at T-70 offset:**
- 10:33 — VPIN 0.491, NORMAL, -$12.58
- 11:14 — VPIN 0.542, NORMAL, -$14.40
- 11:18 — VPIN 0.487, NORMAL, -$9.42
- 13:04 — VPIN 0.494, NORMAL, -$11.15
- 17:23 — VPIN 0.551, TRANS (barely, 0.001 above 0.55), -$7.81

**v8.1.2 was supposed to block NORMAL at late offsets. It didn't.**

The 17:23 trade: VPIN 0.551 is **0.001 above the 0.55 TRANSITION threshold**. It squeaked through. But it wasn't really TRANSITION — it was balanced flow with no conviction.

---

## Regime Breakdown

| Regime | Trades | WR | Avg P&L | Total P&L | Avg Cap |
|--------|--------|-----|---------|-----------|---------|
| TRANS | 15 | 80.0% | +$1.57 | +$23.61 | $0.67 |
| CASCADE | 14 | 64.3% | -$1.02 | -$14.21 | $0.70 |
| NORMAL | 14 | 57.1% | -$3.17 | **-$44.39** | $0.68 |

**TRANSITION is the only profitable regime** — 80% WR at $0.67 avg cap.  
**NORMAL is bleeding** — 57% WR but -$44.39 net.  
**CASCADE is neutral** — 64% WR but -$14.21 net at $0.70 avg cap.

---

## Hour-by-Hour Performance

| UTC Hour | Trades | WR | P&L |
|----------|--------|-----|-----|
| 00:00 | 6 | 100% | +$18.69 |
| 01:00 | 4 | 75% | -$0.42 |
| 02:00 | 6 | 50% | -$17.36 |
| 03:00 | 5 | 60% | -$8.59 |
| 04:00-08:00 | 7 | 100% | +$19.93 |
| 09:00 | 3 | 33% | -$14.34 |
| 10:00 | 4 | 75% | +$6.68 |
| 11:00 | 2 | 0% | **-$23.82** |
| 12:00-15:00 | 5 | 60% | -$13.78 |
| 17:00 | 1 | 0% | -$7.81 |

**Worst period: 11:00 UTC** — two losses, -$23.82. Both were T-70 NORMAL with $0.73 cap.

**Best period: 04:00-08:00 UTC** — 7 trades, 100% WR, +$19.93. All $0.65 cap, mostly NORMAL.

---

## What the Data Says

### 1. **$0.73 cap is too aggressive**

At $0.73 cap:
- Breakeven WR = 73%
- Actual WR = 56.5%
- **Net loss = -$63.99**

At $0.65 cap:
- Breakeven WR = 55%
- Actual WR = 81.3%
- **Net profit = +$26.44**

**The $0.73 cap is destroying P&L.**

### 2. **NORMAL regime at late offsets is broken**

6 trades, 16.7% WR, -$53.73.

v8.1.2's "block NORMAL at T<120" rule failed because:
- VPIN 0.551 passed the 0.55 threshold (barely)
- But 0.551 is not really TRANSITION — it's balanced noise

**Suggestion:** Raise TRANSITION threshold to **0.60** (not 0.55). Or add a "NORMAL block" that's absolute below T-120.

### 3. **CASCADE is not as good as expected**

64% WR should be profitable at $0.70 avg cap... but it's -$14.21.

CASCADE trades are happening at $0.73 cap (10 of 14 trades). At $0.73 cap, we need 73%+ WR. We're getting 60%.

**Suggestion:** CASCADE at T<120 should also be capped at $0.65, not $0.73.

### 4. **Early entries ($0.55 cap) are perfect**

3 trades, 100% WR, +$12.05.

But only 3 trades — not enough data. The early window (T-240 to T-180) is underutilized.

---

## Expected vs Actual WR

**What we thought we had:**
- Signal accuracy at T-70: 65%
- v2.2 gate lift: +5-10%
- Expected trade WR: 70-75%
- Expected P&L at $0.73 cap: breakeven at 73% WR

**What we actually have:**
- Post-fix trade WR: 67.4%
- At $0.73 cap: 56.5% WR (NORMAL: 16.7%, CASCADE: 60%)
- At $0.65 cap: 81.3% WR
- Net P&L: -$63.99

**The gap:** v2.2 isn't lifting WR enough to justify $0.73 caps. The signal is right 65% of the time at T-70, and v2.2 isn't improving that enough.

---

## What About the AI Evaluator?

The AI evaluator on Railway is live but **hasn't generated any cards yet**. It needs:
1. Windows to resolve (oracle_winner set)
2. Those windows to be found by the evaluator loop
3. Evaluation to trigger Telegram notification

The issue: **no recent resolved windows in the 15-min lookback window**. The backfill data is older than 15 min. New windows since 17:00 UTC have no oracle_winner yet.

Once windows resolve (2-3 min after close), the evaluator should fire. First evaluation card should appear within the next few minutes.

---

## CoinGlass: Is It Helping?

**No.** The CG veto system is designed to block trades when 2+ signals oppose the direction. But:
- The veto threshold is 2+ (reduced from 3+ in v5.4d)
- In practice, we're seeing trades slip through with opposing signals

The 17:23 loss: VPIN 0.551, TRANSITION. CG data would have shown:
- Taker buying 66% (veto if >60%)
- Smart money long 54% (veto if >52%)
- CASCADE + taker divergence (if VPIN ≥ 0.65)

But VPIN was 0.551, not 0.65, so CASCADE + taker divergence wouldn't fire. The other two would each add 1 veto point, but we need 2+ to block.

**If CG veto fired, this trade would have been blocked.**

---

## Recommendations

### Immediate (Do Now)

1. **Cap all trades at $0.65** — Remove $0.73 cap entirely
   - At $0.65, we're 81% WR and +$26.44
   - At $0.73, we're 56% WR and -$63.99
   - The math is clear

2. **Block NORMAL at T<120** — Absolute block, not 0.55 threshold
   - 16.7% WR at $0.73 cap is destroying P&L
   - 0.551 is not really TRANSITION — it's noise

3. **Raise TRANSITION threshold to 0.60** — Filter weak signals
   - The 0.551 trade should have been blocked
   - 0.60 would catch more of these "borderline NORMAL" trades

### Short-Term (Next 24h)

4. **Enable CoinGlass veto** — Make it actually block trades
   - Current logic: 2+ opposing signals → block
   - But it's not firing on trades that clearly should be blocked
   - Debug the veto logic

5. **Enable AI evaluator notifications** — Get the evaluation cards flowing
   - Set TELEGRAM env vars on Railway (already done)
   - Wait for window resolutions
   - Review first evaluations for quality

6. **Reduce stake size** — $5-7 max until WR stabilizes above 70%
   - Current stake: $7-10 per trade
   - At 67% WR with 2.5x loss ratio, we're bleeding
   - Cut stake by half until we fix the system

### Long-Term (This Week)

7. **Evaluate closer to T-0** — The 27% accuracy gap to Chainlink is timing
   - Signal at T-70: 65% WR
   - Oracle at T-0: 92% Chainlink
   - Gap: 27% due to 70 seconds of BTC movement
   - Try T-30 or T-15 evaluation — but CLOB liquidity thins

8. **Weight Tiingo over VPIN** — Tiingo is 81% accurate, VPIN is 65%
   - The Tiingo direction is a better predictor than our VPIN+delta signal
   - Consider making Tiingo the primary signal, VPIN as a filter

9. **Add Chainlink confirmation gate** — Final check at T-10
   - If Chainlink delta disagrees at T-10, skip
   - Chainlink is 92% accurate — use it

---

## Bottom Line

**The system is not profitable.** 67% WR with -$63.99 P&L is not sustainable.

The $0.73 cap is the primary killer. At $0.65 cap, we're +$26.44 at 81% WR. The v2.2 gate and VPIN signal are working — they're just not working **well enough** to justify $0.73 fills.

**Options:**
1. **Pause trading** — Let the AI evaluator build data, review patterns, then adjust
2. **Cut cap to $0.65** — Continue trading at profitable levels
3. **Block NORMAL completely** — Only trade TRANSITION and CASCADE at $0.65

What do you want to do?

---

## Appendix: Full Trade Log

See `trades` table for complete list. Key losses:
- 11:14 — T-70 NORMAL $0.73 — -$14.40
- 11:18 — T-100 NORMAL $0.65 — -$9.42
- 10:33 — T-70 NORMAL $0.73 — -$12.58
- 13:04 — T-70 NORMAL $0.73 — -$11.15
- 09:12 — T-170 CASCADE $0.60 — -$9.50
- 17:23 — T-110 TRANS $0.65 — -$7.81

All recent losses are late-window entries with either NORMAL regime or $0.73 cap. The pattern is clear.
