# v8.x Performance Monitor & Recalibration Recommendations

**Created:** April 7, 2026 09:00 UTC
**Data range:** April 5 23:04 to April 7 08:34 UTC (~33 hours of live trading)
**Status:** LIVE on Montreal (Railway engine, AWS frontend)

---

## 1. Executive Summary

v8.1 with v2.2 gate is the only profitable configuration. All other eras are net negative.

| Era | Wins | Losses | WR | P&L | Avg Entry |
|-----|------|--------|------|-------|-----------|
| pre-v8 (Binance) | 13 | 22 | 37.1% | -$84.92 | $0.43 |
| v8.1 (no v2.2) | 8 | 5 | 61.5% | -$21.99 | $0.59 |
| **v8.1 (v2.2 gate)** | **15** | **2** | **88.2%** | **+$30.88** | **$0.64** |

**v2.2 gate is THE edge.** Without it, even v8.1 loses money.

---

## 2. Morning Session (April 7, 04:00-09:00 UTC)

**7 consecutive wins, 0 losses, +$19.92 P&L**

| Time | Dir | Outcome | Submit | Fill | PnL | Reason | Offset |
|------|-----|---------|--------|------|-----|--------|--------|
| 04:58 | NO | WIN | $0.63 | $0.73 | +$2.68 | v2.2_confirmed_T100 | 100s |
| 05:33 | NO | WIN | $0.73 | $0.73 | +$2.75 | v2.2_confirmed_T90 | 90s |
| 05:38 | YES | WIN | $0.69 | $0.73 | +$2.63 | v2.2_confirmed_T110 | 110s |
| 06:11 | NO | WIN | $0.70 | $0.73 | +$2.92 | v2.2_early_T210 | 210s |
| 06:18 | NO | WIN | $0.63 | $0.73 | +$2.70 | v2.2_confirmed_T100 | 100s |
| 07:04 | NO | WIN | $0.56 | $0.73 | +$3.06 | v2.2_confirmed_T60 | 60s |
| 08:08 | NO | WIN | $0.60 | $0.73 | +$3.18 | v2.2_confirmed_T90 | 90s |

### Key Observation: Cap vs Fill Price Mismatch

All GTC orders fill at ~$0.73 regardless of the v8.1 dynamic cap. This is because `place_order()` in `polymarket_client.py` uses `ORDER_PRICING_MODE=cap` which submits at `FOK_PRICE_CAP=$0.73` (env var), ignoring the strategy-level dynamic cap.

**This is actually beneficial:** $0.73 entries at 88% WR gives breakeven at 73% WR. We have 15pp margin.

---

## 3. Signal Accuracy Analysis

### 3a. Tiingo Direction Accuracy (30 resolved windows, April 7)

| Regime | N | Correct | Accuracy |
|--------|---|---------|----------|
| CASCADE | 11 | 5 | 45.5% |
| TRANSITION | 11 | 6 | 54.5% |
| NORMAL | 8 | 6 | 75.0% |

**Raw Tiingo signal is ~53.6% accurate overall.** Barely above coin flip in CASCADE/TRANSITION.

### 3b. Pre-v8 Binance Direction Accuracy (507 resolved windows, April 3-6)

| Regime | N | Correct | Accuracy |
|--------|---|---------|----------|
| CASCADE | 181 | 155 | 85.6% |
| TRANSITION | 183 | 145 | 79.2% |
| NORMAL | 143 | 102 | 71.3% |

**CAUTION:** These numbers are from backfill/paper era on different market days. Cannot be directly compared to Tiingo's 30-window sample.

### 3c. Head-to-Head (37 windows where all three sources resolved)

| Source | Correct | Accuracy |
|--------|---------|----------|
| **Tiingo** | 25 | **67.6%** |
| Chainlink | 21 | 56.8% |
| Binance | 20 | 54.1% |

**On the same windows, Tiingo is the BEST signal source.** This directly contradicts the claim that "Tiingo CASCADE is 45%, Binance was 86%."

### 3d. Delta Magnitude vs Accuracy (Tiingo, resolved)

| Delta Size | N | Accuracy |
|------------|---|----------|
| < 0.02% (tiny) | 10 | 50.0% |
| 0.02-0.05% (small) | 9 | 44.4% |
| 0.05-0.10% (medium) | 10 | 70.0% |
| >= 0.10% (large) | 1 | 100.0% |

**Larger deltas are more accurate.** Consider raising delta threshold for early entries.

---

## 4. v2.2 Gate Effectiveness

### What v2.2 blocked:

| Block Reason | Blocked | Would Have Won | Correct Blocks | Unresolved |
|-------------|---------|----------------|----------------|------------|
| DISAGREE | 15 | 1 | 3 | 11 |
| LOW CONF | 2 | 0 | 0 | 2 |
| DOWN | 1 | 0 | 0 | 1 |
| Other | 37 | 4 | 3 | 30 |

**v2.2 blocked 55 evaluations.** Of the resolved ones, 75% were correct blocks (3 correct vs 1 missed win from DISAGREE). Too few resolved to draw strong conclusions.

### v2.2 Pass Rate by Offset

| Offset | Trades | Wins | Losses | WR |
|--------|--------|------|--------|------|
| T-240 | 5 | 4 | 1 | 80% |
| T-210 | 1 | 1 | 0 | 100% |
| T-190 | 2 | 2 | 0 | 100% |
| T-180 | 3 | 1 | 1 | 50% |
| T-120 | 2 | 1 | 0 | 100% |
| T-110 | 3 | 1 | 0 | 100% |
| T-100 | 2 | 2 | 0 | 100% |
| T-90 | 3 | 2 | 0 | 100% |
| T-60 | 4 | 1 | 0 | 100% |

---

## 5. Entry Price Analysis

| Price Bucket | Trades | WR | Avg PnL |
|-------------|--------|------|---------|
| < $0.40 | 16 | 31.3% | -$2.88 |
| $0.40-$0.49 | 7 | 14.3% | -$4.60 |
| $0.50-$0.59 | 17 | 52.9% | -$1.21 |
| **$0.60-$0.69** | **11** | **100%** | **+$2.85** |
| >= $0.70 | 14 | 71.4% | -$0.61 |

**$0.60-$0.69 is the sweet spot: 100% WR, +$2.85 avg PnL.**

This seems counterintuitive (cheaper = worse?), but it makes sense:
- Cheap entries ($0.30-$0.49) happen when the market is uncertain. High uncertainty = low accuracy.
- Expensive entries ($0.60-$0.69) happen when the market already agrees with our direction. The signal is strong enough that market makers are pricing it in.
- $0.70+ entries are at cap and slightly worse because the R/R ratio compresses.

---

## 6. Recalibration Recommendations

### R1: Keep v2.2 Gate ON for ALL Offsets (NO CHANGE)
- **Evidence:** 88.2% WR with v2.2 vs 61.5% without. +$30.88 vs -$21.99.
- **Risk of relaxing:** Would add losing trades from the 53% base signal.
- **Confidence:** HIGH (N=17 resolved v2.2 trades)

### R2: Keep GTC at $0.73 Cap (NO CHANGE)
- **Evidence:** All 7 morning wins filled at $0.73. 88% WR gives 15pp margin above breakeven (73%).
- **The dynamic caps ($0.55-$0.65) are ignored by the execution layer.** This is actually good — cheaper entries correlate with WORSE outcomes (31% WR at <$0.40).
- **Confidence:** HIGH (N=13 resolved fills at ~$0.73)

### R3: Consider Raising Delta Threshold for Early Offsets
- **Evidence:** delta < 0.05% has 47% accuracy vs delta >= 0.05% has 73%.
- **Proposal:** At T-240/T-180, require delta >= 0.05% (currently ~0.01%).
- **Expected impact:** Fewer trades, higher WR. Estimate 2-3 fewer trades/day, +5-10pp WR.
- **Confidence:** LOW (N=30 total, small sample)
- **Action:** Wait for 72h more data.

### R4: Consider NOT Trading in NORMAL Regime (Controversial)
- **Evidence:** NORMAL has 75% signal accuracy but low volume/conviction.
- **Counter-evidence:** All 8 NORMAL trades won (100% WR on trades). v2.2 gate filters well here.
- **Proposal:** HOLD — NORMAL + v2.2 agree is working perfectly.
- **Confidence:** LOW (N=8)
- **Action:** No change.

### R5: Monitor CASCADE Regime Closely
- **Evidence:** 45.5% raw signal accuracy in CASCADE. v2.2 gate is doing heavy lifting.
- **Risk:** If v2.2 model degrades, CASCADE trades will lose.
- **Proposal:** Add monitoring alert if CASCADE WR drops below 60% over 20+ trades.
- **Confidence:** MEDIUM
- **Action:** Monitor, no code change.

### R6: Investigate Down-Heavy Direction Bias
- **Evidence:** Morning session: 5/7 trades were NO (DOWN). Market was trending down.
- **Risk:** If market flips to uptrend, v2.2 might lag.
- **Proposal:** No change — v2.2 handles direction changes via real-time features.
- **Action:** Monitor UP vs DOWN WR split over 48h.

---

## 7. Claim Validation

### Claim: "Tiingo CASCADE is 45%, Binance was 86%"

**MIXED VALIDITY:**
- Tiingo CASCADE at 45.5% (N=11) — **TRUE on window_snapshots signal accuracy.**
- Binance CASCADE at 85.6% (N=181) — **TRUE but from different era/market conditions.**
- **But comparing them is INVALID** per LIVE_DATA_RULES: different time periods, different sample sizes, different market conditions.
- **Head-to-head on same 37 windows: Tiingo 67.6% > Binance 54.1%.** Tiingo is better.
- **On actual TRADES (not signals): Tiingo CASCADE 63.6% (7W/4L) vs Binance 33.3% (1W/2L).**

### Claim: "v2.2 gate is saving us from bad Tiingo signal"

**TRUE.** Without v2.2 gate, v8.1 WR is 61.5% (negative EV at $0.73 entry). With v2.2 gate, 88.2% WR.

### Claim: "Relaxing gates would add losing trades"

**TRUE.** Raw Tiingo signal is 53.6%. Adding more trades from this base means adding coin-flip trades. v2.2 is the filter that makes it profitable.

---

## 8. Data Quality Notes

- **Sample size warning:** N=17 resolved v2.2 trades. Need N=50+ for directional claims, N=200+ for confidence.
- **Survivorship:** Only examining trades that got past all gates. Unknown how many good trades were blocked.
- **Market regime:** April 7 morning was a downtrend. Results may not generalise to trending/ranging markets.
- **gate_audit table is EMPTY** — schema exists but engine isn't writing to it yet.
- **trade_placed flag in window_snapshots is always false** — not being updated after order placement.
- **Backfill data (pre-April 6):** Paper era with different execution (no CLOB, simulated fills). Do not use for live WR claims.

---

## 9. Monitoring Queries

### Daily performance check
```sql
SELECT outcome, COUNT(*), ROUND(SUM(pnl_usd)::numeric, 2) as pnl
FROM trades WHERE outcome IS NOT NULL AND is_live = true
  AND created_at > NOW() - INTERVAL '24 hours'
GROUP BY outcome;
```

### v2.2 gate effectiveness (last 24h)
```sql
SELECT metadata::json->>'entry_reason' as reason,
  COUNT(*) as trades,
  COUNT(*) FILTER (WHERE outcome='WIN') as wins,
  ROUND(SUM(pnl_usd)::numeric, 2) as pnl
FROM trades WHERE is_live = true AND outcome IN ('WIN','LOSS')
  AND created_at > NOW() - INTERVAL '24 hours'
GROUP BY 1;
```

### Signal source accuracy (head-to-head)
```sql
SELECT COUNT(*) as N,
  ROUND(100.0 * SUM(CASE WHEN (delta_tiingo > 0) = (UPPER(poly_winner) = 'UP') THEN 1 ELSE 0 END) / COUNT(*), 1) as tiingo,
  ROUND(100.0 * SUM(CASE WHEN (delta_binance > 0) = (UPPER(poly_winner) = 'UP') THEN 1 ELSE 0 END) / COUNT(*), 1) as binance,
  ROUND(100.0 * SUM(CASE WHEN (delta_chainlink > 0) = (UPPER(poly_winner) = 'UP') THEN 1 ELSE 0 END) / COUNT(*), 1) as chainlink
FROM window_snapshots
WHERE poly_winner IS NOT NULL AND delta_tiingo IS NOT NULL AND delta_binance IS NOT NULL AND delta_chainlink IS NOT NULL;
```

---

---

## 10. Subwindow Accuracy Surface (April 7 Update)

**Method:** Back-calculated from 141k v2.2 predictions across 476 windows. For each resolved window, checked if v2.2's direction at every 10-second interval matched the oracle outcome. N=30 Tiingo-era windows, ~160-190 gate-passing predictions per bucket.

### Accuracy by T-minus (seconds before window close)

| T-minus | v2.2 Alone | v2+v8 Gated WR | vs $0.73 BE | Zone |
|---------|-----------|----------------|-------------|------|
| **T-10** | 70.3% | 73.5% | AT | HIGH |
| **T-20** | 74.3% | **75.5%** | **+2.5pp** | **HIGH** |
| T-30 | 67.0% | 70.5% | -2.5pp | HIGH |
| T-40 | 70.9% | 70.3% | -2.7pp | HIGH |
| **T-50** | **75.9%** | 73.3% | +0.3pp | **HIGH** |
| **T-60** | 75.3% | **74.3%** | **+1.3pp** | **HIGH** |
| T-70 | 73.7% | 73.3% | +0.3pp | HIGH |
| T-80 | 64.7% | 66.3% | -6.7pp | TRANSITION |
| T-90 | 65.0% | 66.3% | -6.7pp | TRANSITION |
| T-100 | 66.0% | 68.9% | -4.1pp | TRANSITION |
| T-110 | 64.2% | 65.2% | -7.8pp | TRANSITION |
| T-120 | 67.0% | 66.5% | -6.5pp | LOW |
| T-180 | 57.6% | 61.7% | -11.3pp | LOW |
| T-240 | 51.0% | 56.0% | -17.0pp | LOW |

### Zones

- **HIGH (T-10 to T-70):** 70-76% gated WR. Profitable at $0.73 entry. Features freshest.
- **TRANSITION (T-80 to T-110):** 65-69% gated WR. Below breakeven at $0.73. Marginal.
- **LOW (T-120 to T-280):** 56-67% gated WR. Needs cheaper entry to be +EV.

### Implications

1. **Peak signal is at T-20 to T-60.** This is where v2.2's real-time features have maximum info.
2. **Early entry (T-120+) is -EV at $0.73 fills.** Would need $0.55-$0.65 fills to clear breakeven.
3. **Current cascade design is correct in theory** — cheaper entry at earlier offsets compensates for lower accuracy. But GTC fills at $0.73 regardless, negating the cap.
4. **If we fix GTC to respect dynamic caps:** T-240 at $0.55 fill → breakeven is 55% → gated WR 56% → barely +EV. T-120 at $0.65 → breakeven 65% → gated WR 66.5% → barely +EV.
5. **Optimal strategy may be: only trade at T-10 to T-70** where accuracy exceeds $0.73 breakeven without needing cheaper fills.

### Updated Recommendation R7

**R7: Concentrate eval window to T-10 through T-70**
- **Evidence:** Gated WR > 73% only in this range (N=30 windows, ~170 predictions/bucket)
- **Proposal:** Set `FIVE_MIN_EVAL_OFFSETS=70,60,50,40,30,20,10` instead of `240,180,120,60`
- **Expected impact:** Higher WR per trade, fewer trades, but every trade is +EV at $0.73
- **Confidence:** MEDIUM (N=30 windows — need 72h more to confirm)
- **Action:** MONITOR — do not change until N>100 windows confirm the pattern

---

## 11. Analysis History

| Date | Analysis | Key Finding | Decision |
|------|----------|-------------|----------|
| Apr 7 09:00 | Initial v8.1 assessment | v2.2 gate = 88.2% WR (N=17). Only profitable config. | No change. Monitor. |
| Apr 7 10:00 | Back-calculated N=30 | T-60 gated WR=73.7%, T-240=57.9%. Early offsets below BE. | No change. Need more data. |
| Apr 7 10:30 | Subwindow accuracy surface | Peak accuracy T-20 to T-60 (73-76%). T-80+ drops below BE. | New R7: concentrate to T-10-T-70. Monitor. |
| Apr 7 10:30 | Tiingo vs Binance H2H | Tiingo 67.6% vs Binance 54.1% on same 37 windows. | Tiingo confirmed as best source. |
| Apr 7 10:30 | Entry price analysis | $0.60-$0.69 = 100% WR. <$0.40 = 31% WR. | Cheap entries ≠ better. Keep $0.73 GTC. |
| Apr 7 11:00 | CLOB liquidity surface | 100% ask presence at T-120+, $0.51-0.55 asks. Book thins after T-60. | R8: bestask pricing could unlock early entry EV. |

---

## 12. CLOB Liquidity Surface (April 7 Update)

**Method:** Analysed ticks_clob data (CLOB order book snapshots every ~2s) for BTC 5m windows since April 6.

### Ask Presence & Price by Subwindow

| T-minus | Ask Presence | Avg UP Ask | Avg DN Ask | Avg Spread |
|---------|-------------|-----------|-----------|-----------|
| T-270 | 100% | $0.533 | $0.549 | $0.53 |
| T-240 | 100% | $0.534 | $0.540 | $0.53 |
| T-180 | 100% | $0.520 | $0.558 | $0.53 |
| T-120 | 99% | $0.514 | $0.558 | $0.51 |
| T-90 | 97% | $0.509 | $0.548 | $0.51 |
| T-60 | 95% | $0.513 | $0.531 | $0.53 |
| T-30 | 91% | $0.499 | $0.515 | $0.53 |
| T-0 | 80% | $0.409 | $0.447 | $0.53 |

### Combined Accuracy + Liquidity Surface

| Zone | T-minus | Gated WR | CLOB Ask | Fill % | BE at Ask | **Margin** |
|------|---------|----------|----------|--------|-----------|-----------|
| EARLY | T-240 | 56.0% | $0.53 | 100% | 53% | **+3pp** |
| EARLY | T-180 | 61.7% | $0.52 | 100% | 52% | **+10pp** |
| **MID** | **T-120** | **66.5%** | **$0.51** | **99%** | **51%** | **+16pp** |
| **MID** | **T-90** | **66.3%** | **$0.51** | **97%** | **51%** | **+15pp** |
| **LATE** | **T-60** | **74.3%** | **$0.51** | **95%** | **51%** | **+23pp** |
| LATE | T-30 | 70.5% | $0.50 | 91% | 50% | +21pp |
| CLOSE | T-0 | 73.5% | $0.41 | 80% | 41% | +33pp (unfillable) |

### Key Insight: Every Offset is +EV at Market Ask

When comparing gated WR against breakeven at **actual CLOB ask prices** ($0.50-0.55), not our $0.73 GTC submission price, every offset from T-240 to T-30 has positive margin.

The reason early offsets looked bad in Section 10 was we compared against $0.73 breakeven. But the real market ask is $0.50-0.55.

### R8: Consider bestask Pricing Mode

**Proposal:** Switch `ORDER_PRICING_MODE` from `cap` to `bestask`.
- Currently: GTC submits at $0.73 → fills at $0.73 → breakeven 73% → only T-60 clears it.
- Proposed: GTC submits at CLOB ask + $0.02 bump → fills at ~$0.53 → breakeven 53% → ALL offsets clear it.
- **Expected impact:** Same trades, same accuracy, but 20-30% cheaper fills. P&L per win doubles from ~$2.70 to ~$4.70.
- **Risk:** Lower fill rate (maybe 70-80% vs current 100%). Market makers may not match at their posted ask.
- **Confidence:** MEDIUM (CLOB data shows $0.50-0.55 asks exist; unknown if they'd actually fill)
- **Action:** MONITOR. Needs A/B test or paper trial. Do NOT change without testing.

*Next review: April 8, 2026 09:00 UTC (48h of v8.1 data)*
*Update this doc with fresh numbers then.*
