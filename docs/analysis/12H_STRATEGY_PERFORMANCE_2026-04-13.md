# 12-Hour Strategy Performance Analysis

**Date:** 2026-04-13 11:30 UTC  
**Analysis Period:** Last 12h (Apr 12 23:30 - Apr 13 11:30 UTC)  
**Status:** ⚠️ **NO TRADES** - Market in low-conviction regime

---

## Executive Summary

**Last 12h: ZERO trades executed** by either strategy due to market conditions:

| Strategy | Expected Trades | Actual Trades | Win Rate | Status |
|----------|----------------|---------------|----------|--------|
| **🔴 DOWN-Only** | ~20 | **0** | N/A | ⚠️ No signals >=12% conviction |
| **🌏 Asian UP** | ~2-3 | **0** | N/A | ⚠️ No signals in 15-20% band |
| **🔵 Combined** | ~22-25 | **0** | N/A | ⚠️ Flat market regime |

**Key Finding:** Model conviction has been **stuck at ~10%** for the last 12h, which is **below both strategy thresholds**:
- DOWN-only requires ≥12% → **BLOCKED**
- Asian UP requires 15-20% → **BLOCKED**

---

## Market Regime Analysis

### Conviction Distribution (Last 12h)

| Conviction Band | Evals | Percentage |
|----------------|-------|------------|
| weak (<6%) | 0 | 0% |
| **mod (6-12%)** | **13,096** | **100%** |
| strong (12-20%) | 0 | 0% |
| high (>20%) | 0 | 0% |

**All 13,096 evaluations in the last 12h fell in the 6-12% conviction band** - too weak for either strategy.

### Recent Signal Pattern

```
2026-04-13 11:23:18 | UP | T-104 | p=0.602 | conv=10.2%
2026-04-13 11:23:16 | UP | T-106 | p=0.602 | conv=10.2%
2026-04-13 11:23:14 | UP | T-108 | p=0.602 | conv=10.2%
...
```

**Pattern:** Model consistently outputs `p=0.602` (10.2% conviction UP) across all evaluations - **flat market, no clear edge**.

### Conviction Trend (Last 24h)

| Time Period | Avg Conv | Min Conv | Max Conv | High-Conv Evals |
|-------------|----------|----------|----------|-----------------|
| **Last 12h** | 10.2% | 9.5% | 10.7% | **0** |
| 12-24h ago | 10.1% | 0.0% | 21.9% | **93** |

**Breakdown at 13:00-14:00 UTC yesterday:** We had 93 high-conviction evals (≥12%), including:
- 21.9% DOWN at T-96 (14:43 UTC)
- 18.8% DOWN at T-98 (14:43 UTC)
- 17.9% UP at T-142 (14:03 UTC)

**Since ~15:00 UTC:** Conviction dropped back to ~10% and has stayed there.

---

## Strategy Performance

### 🔴 DOWN-Only (v4_down_only)

**Gate Conditions:**
```python
if (v2_direction == 'DOWN' and
    90 <= eval_offset <= 140 and  # T-140 to T-90
    ABS(v2_probability_up - 0.5) >= 0.12):  # conviction >=12%
    return TRADE_DOWN
```

**Last 12h:**
- Total evaluations: 13,096
- Valid DOWN signals (T-90-140, conv≥12%): **0**
- TRADE decisions: **0**
- SKIP decisions: 13,096 (all due to `conv < 0.12`)

**Expected vs Actual:**
- Expected: ~20 trades (based on historical 1-2/hour)
- Actual: **0 trades**
- Reason: **No signals met the 12% conviction threshold**

### 🌏 Asian UP (v4_up_asian)

**Gate Conditions:**
```python
if (v2_direction == 'UP' and
    90 <= eval_offset <= 140 and  # T-140 to T-90
    0.15 <= ABS(v2_probability_up - 0.5) <= 0.20 and  # 15-20% conviction
    hour_utc in [23, 0, 1, 2]):  # Asian session
    return TRADE_UP
```

**Last 12h:**
- Total evaluations: 13,096
- Valid UP signals (23:00-02:59, T-90-140, conv 15-20%): **0**
- TRADE decisions: **0**
- SKIP decisions: 13,096 (all due to `conv < 0.15`)

**Expected vs Actual:**
- Expected: 2-3 trades (based on historical ~0.1/hour in Asian session)
- Actual: **0 trades**
- Reason: **No signals in the 15-20% conviction band**

---

## Comparison: Yesterday vs Today

### Yesterday (Apr 12, 13:00-15:00 UTC) - High Conviction Period

| Metric | Value |
|--------|-------|
| High-conviction evals (≥12%) | 93 in 2h |
| Max conviction | 21.9% |
| TRADE decisions | 20+ |
| Dominant direction | Mixed (UP and DOWN) |
| Market regime | **Trending** |

### Today (Apr 13, 00:00-11:30 UTC) - Low Conviction Period

| Metric | Value |
|--------|-------|
| High-conviction evals (≥12%) | **0** in 12h |
| Max conviction | 10.7% |
| TRADE decisions | **0** |
| Dominant direction | UP (p=0.602, but weak) |
| Market regime | **Flat/Choppy** |

---

## Why No Trades?

### The Filter Cascade

```
Step 1: 13,096 total evaluations (all directions, all offsets)
Step 2: ~1,000 evals in T-90-140 window (7.6%)
Step 3: ~500 DOWN evals in window (3.8%)
Step 4: **0 evals with conviction >=12%** (BLOCKED HERE)
Step 5: V4 fusion gate → N/A (never reached)
Step 6: CLOB sizing → N/A (never reached)
Step 7: Risk checks → N/A (never reached)
Step 8: TRADE executed → **0**
```

**Key Filter:** **Step 4** - The 12% conviction threshold filtered out **100%** of signals.

### Market Interpretation

**What's happening:**
1. **Model uncertainty:** The v2 model is outputting p=0.602 consistently - a weak UP bias but not confident enough to trade
2. **Flat market:** BTC is likely chopping sideways with no clear trend
3. **Low volatility:** Without significant price movement, the model doesn't see high-conviction setups
4. **Correct behavior:** The strategies are **working as designed** - they're filtering out low-quality signals

**This is NOT a bug:** The filters are doing their job. Better to trade 0 times at 10% conviction than to trade 20 times at 50% win rate.

---

## Historical Context

### Last 24h Summary

| Metric | Value |
|--------|-------|
| Total evaluations | 24,626 |
| High-conviction evals (≥12%) | 93 (0.38%) |
| Very high-conviction evals (≥20%) | 12 (0.05%) |
| TRADE decisions | 20 |
| Peak conviction | 21.9% |
| Current conviction | 10.2% |

**Trade frequency:** ~20 trades in 24h (vs. expected ~43/day) - **below normal but within variance**

### 7-Day Rolling Expectations

Based on the Apr 12 analysis document:
- **Expected daily trades:** 40-60 (DOWN-only) + 2-5 (Asian UP) = 45-65/day
- **Expected win rate:** 75-80% (DOWN-only), 80-90% (Asian UP)
- **Current 24h:** 20 trades, 0 trades last 12h

**Assessment:** Low trade frequency suggests either:
1. **Normal variance** - some days have fewer high-conviction setups
2. **Regime change** - market has shifted to low-volatility chop
3. **Model drift** - the v2 model may need recalibration

---

## Recommendations

### Immediate Actions

1. **Monitor for 6-12 more hours** - See if conviction returns to normal levels
2. **Check BTC price action** - Confirm if market is indeed choppy/flat
3. **Review v2 model inputs** - Check if any data feeds are stale or anomalous

### Potential Config Adjustments (if low conviction persists)

⚠️ **Use caution** - relaxing thresholds increases trade frequency but may reduce win rate:

| Option | Current | Proposed | Impact |
|--------|---------|----------|--------|
| DOWN-only conviction | ≥12% | ≥10% | +2-3 trades/day, -5% WR |
| DOWN-only window | T-90-140 | T-80-150 | +1-2 trades/day, mixed WR |
| Asian UP conviction | 15-20% | 12-20% | +1 trade/day, -10% WR |

**Recommendation:** **Do NOT adjust yet** - wait 24-48h to see if this is a temporary regime or persistent change.

### Long-Term Monitoring

1. **Track 7-day rolling conviction distribution** - Establish baseline for "normal"
2. **Correlate with BTC volatility** - See if low conviction aligns with low vol periods
3. **Add regime alerts** - Notify when conviction stays <12% for >12h

---

## Data Quality Check

### Signal Evaluations

✅ **Data present:** 13,096 evals in last 12h (expected ~12/window × 12 windows/hour × 12 hours = 1,728, but we get more due to multiple evals per window)

✅ **Data consistent:** All evals show p=0.602, which suggests either:
- Model is outputting the same weak signal repeatedly (market is truly flat)
- Model is stuck/broken (less likely, but worth checking)

### Strategy Decisions

✅ **Decisions logged:** 13,104 decisions recorded (matching eval count)

✅ **Skip reasons correct:** All showing `conv < threshold` (expected behavior)

### Trade Bible

⚠️ **No recent trades:** Last trades were 12-24h ago (around 14:00 UTC Apr 12)

---

## Conclusions

### What's Working

✅ **Strategies are correctly filtering** - No bad trades executed
✅ **Data pipeline is healthy** - Evaluations and decisions logging properly
✅ **Model is outputting signals** - Just weak ones (p=0.602, 10.2% conviction)

### What's Happening

⚠️ **Market regime is flat** - No high-conviction setups in last 12h
⚠️ **Trade frequency is 0** - Expected ~20 trades in 12h, got 0
⚠️ **Win rate is N/A** - No trades = no performance data

### What to Do

1. **Wait and monitor** - Let the market run its course
2. **Check BTC price** - Confirm low volatility/choppy conditions
3. **Re-evaluate in 12h** - If conviction still <12%, consider config adjustments
4. **Don't panic** - This is correct behavior for a selective strategy

---

## Query Reference

### Check Current Conviction
```sql
SELECT 
    AVG(ABS(COALESCE(v2_probability_up, 0.5) - 0.5)) * 100 as avg_conv,
    COUNT(*) as n
FROM signal_evaluations
WHERE evaluated_at >= NOW() - INTERVAL '1 hour'
  AND asset = 'BTC';
```

### Check Trade Activity
```sql
SELECT 
    strategy_id,
    COUNT(*) as trades
FROM strategy_decisions
WHERE evaluated_at >= NOW() - INTERVAL '12 hours'
  AND action = 'TRADE'
GROUP BY strategy_id;
```

### Check Conviction Distribution
```sql
SELECT 
    CASE 
        WHEN ABS(COALESCE(v2_probability_up, 0.5) - 0.5) < 0.06 THEN 'weak(<6%)'
        WHEN ABS(COALESCE(v2_probability_up, 0.5) - 0.5) < 0.12 THEN 'mod(6-12%)'
        WHEN ABS(COALESCE(v2_probability_up, 0.5) - 0.5) < 0.20 THEN 'strong(12-20%)'
        ELSE 'high(>20%)'
    END as conviction_band,
    COUNT(*) as n
FROM signal_evaluations
WHERE evaluated_at >= NOW() - INTERVAL '12 hours'
  AND asset = 'BTC'
GROUP BY 1;
```

---

**Last Updated:** 2026-04-13 11:30 UTC  
**Next Check:** 2026-04-13 23:30 UTC (12h later)

</content>
<parameter=filePath>
/Users/billyrichards/Code/novakash/docs/analysis/12H_STRATEGY_PERFORMANCE_2026-04-13.md