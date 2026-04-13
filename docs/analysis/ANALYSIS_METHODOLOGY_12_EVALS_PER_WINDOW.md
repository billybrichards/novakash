# Analysis Methodology: 12 Evaluations Per Window

**Date:** 2026-04-12 21:55 UTC  
**Purpose:** Clarify the 12 evaluations per window pattern and correct analysis methodology

---

## The 12 Windows Per Hour Pattern

### Trading Window Structure

Each 5-minute Polymarket window has **12 evaluation points**:

| Evaluation | Offset from Close | Time Before Close |
|------------|-------------------|-------------------|
| 1 | T-140 | 2:20 before close |
| 2 | T-138 | 2:18 before close |
| 3 | T-136 | 2:16 before close |
| 4 | T-134 | 2:14 before close |
| 5 | T-132 | 2:12 before close |
| 6 | T-130 | 2:10 before close |
| 7 | T-128 | 2:08 before close |
| 8 | T-126 | 2:06 before close |
| 9 | T-124 | 2:04 before close |
| 10 | T-122 | 2:02 before close |
| 11 | T-120 | 2:00 before close |
| 12 | T-118 | 1:58 before close |
| ... | ... | ... |
| N | T-90 | 1:30 before close |

**Key Points:**
- Evaluations occur every 2 seconds (2s ticks)
- But only 12 specific offsets are stored (T-140 to T-90)
- Each window is evaluated multiple times before a trade decision is made
- The live engine only trades ONCE per window (at the best offset)

### Why 12 Evaluations?

The model evaluates at each offset to:
1. **Capture early signals** (T-140) - see direction early but lower confidence
2. **Capture late signals** (T-90) - higher confidence but less time to execute
3. **Find optimal entry** - V4 fusion gate picks the best offset
4. **CLOB pricing** - Different offsets have different CLOB ask prices

**Result:** Each 5-minute window has 12 potential "trade opportunities" in the database, but only 1 actual trade executes.

---

## The Filter Cascade

```
Total evaluations per day:
  288 windows/day × 12 evaluations/window = 3,456 evaluations/day (theoretical)
  Actual: 18,701 evaluations/day (includes all strategies, all offsets)

DOWN-Only:
  11,621 evaluations (direction=DOWN, offset 90-140, conv≥12%)
  36 unique windows (first eval per window)
  40 actual trades (V4_DOWN_ONLY + CLOB + risk)
  Filter rate: 40/11,621 = 0.35%

Asian UP:
  1,770 evaluations (UP, offset 90-140, conv 15-20%, 23-02 UTC)
  3 unique windows (first eval per window)
  3 actual trades (V4_UP_ASIAN + time filter + risk)
  Filter rate: 3/1,770 = 0.17%
```

**Why such low filter rates?**
1. **V4 fusion gate** - Must pass regime check, cascade check, V3 composite
2. **CLOB sizing** - Only trades when CLOB ask provides good value
3. **Risk management** - Max exposure, daily limits, consecutive loss cooldown
4. **Execution** - FOK orders may not fill, network latency

---

## Correct Analysis Methods

### Method 1: Signal Quality (All Evaluations)

**Use Case:** "Is the model accurate at T-140?"

```sql
SELECT 
    se.eval_offset,
    COUNT(*) as n,
    ROUND(100.0 * SUM(CASE WHEN 
        (se.v2_direction='UP' AND ws.close_price > ws.open_price) OR
        (se.v2_direction='DOWN' AND ws.close_price < ws.open_price)
    THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) as accuracy
FROM signal_evaluations se
JOIN window_snapshots ws ON se.window_ts=ws.window_ts::bigint AND se.asset=ws.asset
WHERE se.asset='BTC' AND se.eval_offset BETWEEN 90 AND 140
  AND ws.close_price > 0 AND ws.open_price > 0
GROUP BY 1
ORDER BY 1 DESC;
```

**Result:** Accuracy by offset (all 11,621 DOWN evaluations)

**When to use:** Research, model tuning, understanding signal quality at different offsets

### Method 2: Strategy Design (Unique Windows)

**Use Case:** "How many windows have a valid DOWN signal?"

```sql
WITH down_evals AS (
    SELECT se.window_ts, se.eval_offset,
           ws.close_price, ws.open_price,
           ROW_NUMBER() OVER (PARTITION BY se.window_ts ORDER BY se.eval_offset DESC) as rn
    FROM signal_evaluations se
    JOIN window_snapshots ws ON se.window_ts=ws.window_ts::bigint AND se.asset=ws.asset
    WHERE se.asset='BTC' 
      AND se.eval_offset BETWEEN 90 AND 140
      AND se.v2_direction = 'DOWN'
      AND ABS(COALESCE(se.v2_probability_up,0.5)-0.5) >= 0.12
)
SELECT 
    COUNT(*) as windows_with_signal,
    ROUND(100.0 * SUM(CASE WHEN close_price < open_price THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) as wr
FROM down_evals
WHERE rn = 1;
```

**Result:** 36 windows with DOWN signal, 94.4% WR

**When to use:** Strategy design, expected trade frequency, gate optimization

### Method 3: Performance Tracking (Actual Trades)

**Use Case:** "What's my real PnL?"

```sql
SELECT 
    strategy,
    COUNT(*) as trades,
    SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
    ROUND(100.0 * wins / COUNT(*), 1) as wr,
    SUM(pnl) as total_pnl,
    ROUND(AVG(stake), 2) as avg_stake
FROM trade_bible
WHERE created_at >= NOW() - INTERVAL '24 hours'
GROUP BY strategy;
```

**Result:** 40 trades, 77% WR, actual PnL

**When to use:** Live performance monitoring, PnL tracking, risk management

---

## Common Mistakes

### Mistake 1: Using Evaluations as Trades

**Wrong:**
```sql
SELECT COUNT(*) as trades, -- WRONG! This is evaluations
       SUM(wins)/COUNT(*) as wr
FROM signal_evaluations
WHERE direction='DOWN'
-- Result: 11,621 trades, 92.5% WR (WRONG!)
```

**Right:**
```sql
SELECT COUNT(*) as trades, -- CORRECT
       SUM(wins)/COUNT(*) as wr
FROM trade_bible
WHERE strategy='v4_down_only'
-- Result: 40 trades, 77% WR (CORRECT!)
```

### Mistake 2: Ignoring the 12 Evaluations Per Window

**Wrong:**
- Assume each row in signal_evaluations = 1 trade
- Expect 11,621 trades/day for DOWN-Only
- Calculate WR using all evaluations

**Right:**
- Each window has 12 evaluations
- Only 1 trade per window (at best offset)
- Expect ~40 trades/day for DOWN-Only
- Use unique windows or trade_bible for WR

### Mistake 3: Confusing Signal Quality with Trading Performance

**Signal Quality (Evaluations):**
- 92.5% WR at 11,621 evaluations
- Model is accurate at predicting direction
- Good for model tuning

**Trading Performance (Trades):**
- 77% WR at 40 trades
- Real PnL after all filters
- Good for PnL tracking

**Both are correct for their purpose!**

---

## Expected Values (Corrected)

### DOWN-Only Strategy

| Metric | Per Day | Per Hour | Per Window |
|--------|---------|----------|------------|
| Windows evaluated | 288 | 12 | 1 |
| DOWN evaluations | ~1,728 | ~72 | ~12 |
| Windows with signal | 36-40 | ~1.5 | ~0.13 |
| Actual trades | 35-45 | ~1.7 | ~0.15 |
| Win rate | 75-80% | 75-80% | 75-80% |

### Asian UP Strategy

| Metric | Per Day | Per Hour | Per Window |
|--------|---------|----------|------------|
| Windows evaluated | 48 (4 hours) | 12 | 1 |
| UP evaluations | ~180 | ~7.5 | ~12 |
| Windows with signal | 2-5 | ~0.2 | ~0.02 |
| Actual trades | 2-5 | ~0.1 | ~0.01 |
| Win rate | 80-90% | 80-90% | 80-90% |

**Key Insight:** Asian UP is ~10x more selective than DOWN-Only (3 trades vs 40 trades per day).

---

## Summary

1. **12 Evaluations Per Window:** Each 5-minute window has 12 evaluation points (T-140 to T-90)
2. **Evaluations ≠ Trades:** signal_evaluations has ALL evaluations, not just trades
3. **Filter Cascade:** Only 0.2-0.3% of evaluations become actual trades
4. **Correct Method:** Use unique windows or trade_bible for performance analysis
5. **Expected Frequency:** ~40 trades/day for DOWN-Only, ~3 trades/day for Asian UP

**When analyzing data:**
- **Signal quality:** Use all evaluations (signal_evaluations)
- **Strategy design:** Use unique windows (ROW_NUMBER() partition by window_ts)
- **Performance tracking:** Use actual trades (trade_bible)

---

**Related Docs:**
- `SIGNAL_EVAL_RUNBOOK.md` - Full analysis queries
- `EVALUATIONS_VS_TRADES_2026-04-12.md` - Detailed analysis
- `24H_STRATEGY_PERFORMANCE_2026-04-12.md` - Live performance

**Last Updated:** 2026-04-12 21:55 UTC
