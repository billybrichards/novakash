# MONITOR — April 7, 2026

**Wallet:** $130.82 → $96.63 = **-$34.19 (-26.1%)**
**Kill switch:** 80% drawdown ($26.16 remaining before kill)

---

## Post-Fix Trades (09:50+ UTC, dynamic caps active)

| Time | Dir | Result | Cap | VPIN | Regime | P&L | Status |
|------|-----|--------|-----|------|--------|-----|--------|
| 09:58 | NO | ✅WIN | $0.65 | 0.566 | TRANS | +$4.35 | |
| 10:13 | NO | ✅WIN | $0.65 | 0.670 | CASCADE | +$5.59 | |
| 10:33 | NO | ❌LOSS | $0.73 | 0.491 | NORMAL | -$12.58 | **v8.1.1 now blocks** |
| 10:43 | NO | ✅WIN | $0.65 | 0.554 | TRANS | +$6.20 | |
| 10:58 | NO | ✅WIN | $0.65 | 0.650 | TRANS | +$7.47 | |
| 11:14 | NO | ❌LOSS | $0.73 | 0.542 | NORMAL | -$14.40 | **v8.1.1 now blocks** |
| 11:18 | YES | ❌LOSS | $0.65 | 0.487 | NORMAL | -$9.42 | T-100 NORMAL — extend gate? |
| 12:28 | YES | ✅WIN | $0.65 | 0.577 | TRANS | +$5.65 | |
| 13:04 | NO | ❌LOSS | $0.73 | 0.494 | NORMAL | -$11.15 | **v8.1.1 now blocks** |
| 14:34 | YES | ❌LOSS | $0.73 | 0.818 | CASCADE | -$7.87 | Legit — CASCADE, market wrong |

**5W/5L (50% WR), -$26.16 net**

---

## Loss Pattern Analysis

| Category | Count | Total Lost | Preventable? |
|----------|-------|-----------|-------------|
| NORMAL at T-70 | 3 | -$38.13 | ✅ Blocked by v8.1.1 (deployed 13:19) |
| NORMAL at T-100 | 1 | -$9.42 | ⚠️ Consider extending NORMAL gate to all offsets |
| CASCADE/TRANS (legit) | 1 | -$7.87 | ❌ Market was wrong, gates can't help |

**If v8.1.1 had been active all day:** 3 fewer losses = +$38.13 saved → net would be +$11.97

---

## Key Concerns to Watch

### 1. 🔴 Win/Loss asymmetry at $0.73
- Wins at $0.65: avg **+$5.85** per win
- Losses at $0.73: avg **-$11.08** per loss
- Need **65.4% WR** at these fills to break even
- Post-fix WR is only 50% — **not profitable yet**

### 2. ✅ NORMAL regime gate extended (v8.1.2)
- 4 of 5 post-fix losses were NORMAL regime
- v8.1.2 blocks NORMAL (VPIN<0.55) at ALL late offsets (<120)
- Deployed 16:10 UTC — covers T-110 through T-60
- Skip reason: `v8.1.2: NORMAL at T-{offset} (VPIN {value} < 0.55)`

### 3. 🟡 Drawdown approaching kill switch
- Current: -$34.19 (-26.1%) from $130.82
- Kill switch: MAX_DRAWDOWN_KILL=0.80 → kills at ~$26.16 wallet
- **$70.47 remaining before kill** (wallet $96.63, kill at $26.16)
- At current loss rate (~$5/hr post-fix), ~14 hours to kill

### 4. 🟢 Dynamic caps working correctly
- T-90/T-110 fills: $0.65-0.66 ✅
- T-70/T-60 fills: $0.73 ✅  
- Win sizes doubled from $2.87 → $5.85

### 5. 🟢 v2.2 gate still the best filter
- All 5 post-fix wins had v2.2 HIGH + agrees
- v2.2 disagreements correctly skipping ~60% of windows

### 6. 🔬 Signal Source Accuracy (200 resolved windows, Apr 7)

| Source | Accuracy | N | Notes |
|--------|----------|---|-------|
| **Chainlink** | **92.0%** | 200 | Oracle IS Chainlink — near-perfect |
| **Tiingo** | **81.5%** | 200 | Strong but 10% gap to oracle |
| **Our Signal** | **65.0%** | 200 | VPIN+delta at T-70, 27% gap to oracle |

**Key insight:** The 27% accuracy gap between our signal (65%) and Chainlink (92%) is
almost entirely a **timing gap**. We evaluate at T-70 (70 seconds before close) but
the oracle settles at T-0. In 70 seconds, BTC can move enough to flip the direction.

**Implication:** Our signal is right 65% of the time when measured 70s early.
The v2.2 gate lifts actual trade WR to ~70% by filtering out low-confidence signals.
The remaining gap to Chainlink's 92% is the **irreducible timing risk** of predicting
70 seconds ahead.

**Possible improvements:**
- Evaluate closer to T-0 (T-30? T-15?) — but CLOB liquidity dries up
- Use Chainlink live price at T-10 as a final confirmation gate
- Weight Tiingo more heavily than our VPIN signal (81% vs 65%)

---

## Decisions Needed

- [x] ~~Extend NORMAL gate to all offsets?~~ **Done — v8.1.2 deployed 16:10 UTC**
- [ ] **Reduce bet fraction?** Currently 7.3% → losses are $7-14 each. At 5% they'd be $5-10.
- [ ] **Wait for more data?** Post-fix + v8.1.2 trades still small N. Need 2-3 days.
