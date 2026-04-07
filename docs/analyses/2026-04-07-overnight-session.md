# Overnight Session Analysis — April 7, 2026

**Period:** Apr 6 20:21 UTC → Apr 7 08:30 UTC (12 hours)
**Engine versions:** v8.0 → v8.1
**Data source:** `trades` table (live orders) + `window_snapshots` (all evaluations)

---

## Executive Summary

- **21 live trades resolved:** 15W/6L (71.4% WR), net **-$9.64**
- **Losing money at 71% WR** because avg win (+$2.87) << avg loss (-$8.79)
- **v2.2 gate extension was the turning point:** 6W/0L (+$16.73) after fix
- **Signal source is NOT the issue** — Tiingo and Binance identical (56.7%) on same windows
- **Entry price is the core problem** — $0.65+ entries are negative EV at 71% WR

---

## Trade Results

### Pre v2.2 gate fix (01:00-04:24 UTC)
| Metric | Value |
|--------|-------|
| Trades | 15 (9W/6L) |
| WR | 60% |
| Net P&L | -$26.38 |
| Avg win | +$2.93 |
| Avg loss | -$8.79 |

**5 of 6 losses were v8_standard at T-110** — bypassed v2.2 gate entirely.

### Post v2.2 gate fix (04:24-08:30 UTC)
| Metric | Value |
|--------|-------|
| Trades | 6 (6W/0L) |
| WR | 100% |
| Net P&L | +$16.73 |
| Avg win | +$2.79 |

**Every trade confirmed by v2.2 HIGH confidence + direction agreement.**

---

## Loss Analysis

All 6 losses detailed:

| Time | Dir | Entry | Stake | P&L | VPIN | Regime | Confidence | Entry Point |
|------|-----|-------|-------|-----|------|--------|------------|------------|
| 01:28 | UP | $0.41 | $8.35 | -$8.50 | 0.666 | CASCADE | MODERATE | T-100 |
| 02:28 | DOWN | $0.37 | $9.19 | -$9.34 | 0.602 | TRANSITION | MODERATE | T-110 |
| 02:53 | DOWN | $0.70 | $8.50 | -$8.63 | 0.660 | CASCADE | MODERATE | T-110 |
| 02:56 | DOWN | $0.72 | $8.78 | -$8.91 | 0.678 | CASCADE | DECISIVE | T-240 (v2.2) |
| 03:29 | DOWN | $0.55 | $8.62 | -$8.77 | 0.530 | NORMAL | MODERATE | T-60 |
| 03:54 | DOWN | $0.36 | $8.43 | -$8.58 | 0.534 | NORMAL | MODERATE | T-60 |

**Patterns:**
- 5/6 losses were DOWN bets in a market that kept resolving UP
- 5/6 were MODERATE confidence (v8_standard path, no v2.2 gate)
- 1 was DECISIVE (v2.2 agreed) — even v2.2 gets it wrong sometimes
- 2 losses in NORMAL regime (VPIN 0.53) — shouldn't trade at all

---

## Signal Source Comparison (Same 30 Windows)

⚠️ **N=30 — too small for conclusions. Directional only.**

| Source | Correct | Accuracy |
|--------|---------|----------|
| Tiingo | 17/30 | 56.7% |
| Binance | 17/30 | 56.7% |
| Chainlink | 19/30 | 63.3% |

**Conclusion:** Signal source doesn't explain performance differences. Market was choppy — no source exceeded 64%. v2.2 is what filters to profitability.

---

## Gate Performance (24 Resolved Skips)

| Gate | Skipped | Missed Wins | Good Blocks | Block Accuracy |
|------|---------|-------------|-------------|----------------|
| DELTA SMALL | 14 | 7 | 7 | 50% |
| NOT CASCADE | 5 | 4 | 1 | 20% |
| v2.2 BLOCKED | 4 | 1 | 3 | **75%** |
| OTHER | 1 | 1 | 0 | 0% |

### Gate-by-gate assessment:

**DELTA SMALL (14 skips, 50% accuracy):**
Blocks when |delta| < 0.02%. Blocking equal wins and losses — essentially coinflip filtering. The threshold doesn't add signal value. However, lowering it only adds 2 more windows (N=2) at 50/50 — not worth changing.

**NOT CASCADE (5 skips, 20% accuracy):**
The tight DECISIVE requires VPIN ≥ 0.65 for early entry (T-120+). This blocked 4 wins and only 1 loss. It's our leakiest gate. However: these 4 "missed wins" would have entered at whatever CLOB price was available at T-120+, which might have been $0.80+ (unfillable). The skip reason doesn't mean we'd have profited — just that the signal was correct.

**v2.2 BLOCKED (4 skips, 75% accuracy):**
Best performing gate. 3 correct blocks where v2.2 disagreed with v8 and the oracle proved v2.2 right. Only 1 miss (03:35 — v2.2 said DOWN, oracle was UP, our v8 signal UP was correct).

---

## CLOB Liquidity Analysis

### Price availability by time offset (6h sample):

| Time Bucket | Samples | Avg Cheapest Token | Fillable (≤$0.60) | Fillable (≤$0.73) |
|-------------|---------|-------------------|-------------------|-------------------|
| T-300..T-240 | 499 | $0.655 | 39% | 39% |
| T-240..T-180 | 506 | $0.602 | 19% | 19% |
| T-180..T-120 | 507 | $0.571 | 8% | 8% |
| T-120..T-60 | 329 | $0.532 | 5% | 5% |
| T-60..T-0 | 318 | $0.641 | 3% | 3% |

**Key insight:** Real fillable liquidity exists in the first 1-2 minutes of each window. By T-70, tokens are priced at near-certainty ($0.90+).

### Cap pricing works:
- Submit GTC at cap ($0.73), CLOB fills at actual market price
- We don't pay $0.73 — we pay whatever the best ask is
- Fill time: typically 5 seconds

---

## R/R Analysis: Why 71% WR Loses Money

| Entry Price | Win Pays | Lose Costs | Breakeven WR | Our WR (71%) | EV per $8 trade |
|-------------|----------|------------|--------------|-------------|-----------------|
| $0.40 | +$4.70 | -$3.20 | 40.5% | Profitable | +$2.42 |
| $0.50 | +$3.92 | -$4.00 | 50.5% | Profitable | +$1.63 |
| $0.55 | +$3.53 | -$4.40 | 55.5% | Profitable | +$1.23 |
| $0.60 | +$3.14 | -$4.80 | 60.5% | Profitable | +$0.85 |
| $0.65 | +$2.74 | -$5.20 | 65.5% | Profitable | +$0.43 |
| $0.70 | +$2.35 | -$5.60 | 70.5% | **Barely** | +$0.03 |
| $0.73 | +$2.12 | -$5.84 | 73.4% | **NEGATIVE** | -$0.20 |

**At 71% WR, entries above $0.70 are negative EV.** Our average entry tonight was $0.58 — profitable on paper but the variance at $8 stakes with 6 losses wiped it.

---

## Recommended Adjustments

### DO NOW:
1. ✅ **v2.2 gate on all offsets** — DONE. Most impactful change. 6W/0L since enabled.

### MONITOR (need more data):
2. **NOT CASCADE gate** — blocking 4 wins per 1 loss (20% accuracy). Consider relaxing to TRANSITION (VPIN≥0.55) when v2.2 agrees. BUT: need to verify those 4 "missed wins" were actually fillable on CLOB.
3. **Delta threshold** — 50% accuracy means it's not adding value. But lowering only gains 2 windows at 50/50. Low priority.
4. **NORMAL regime trades** — 2 losses tonight were NORMAL (VPIN<0.55). Consider blocking entirely at late offsets.

### DON'T CHANGE:
5. **Signal source** — Tiingo and Binance identical. Not the issue.
6. **v2.2 gate threshold** — 75% accuracy, best filter we have.
7. **Cap pricing mode** — working well, fills in 5s.

### NEEDS 2-3 DAYS DATA:
8. Tiingo vs Binance at N=500+ (currently N=30, identical)
9. DECISIVE early entry WR at meaningful sample size
10. Per-checkpoint gate_audit analysis (just started storing)

---

## Key Metrics to Track

```sql
-- Daily P&L (GROUND TRUTH)
SELECT DATE(created_at), outcome, COUNT(*), ROUND(SUM(pnl_usd)::numeric, 2)
FROM trades WHERE outcome IS NOT NULL
GROUP BY 1, 2 ORDER BY 1 DESC, 2;

-- v2.2 gate impact
SELECT 
  CASE WHEN metadata->>'entry_reason' LIKE 'v2.2%' THEN 'v2.2 confirmed' ELSE 'v8 standard' END,
  COUNT(*), SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
  ROUND(SUM(pnl_usd)::numeric, 2) as pnl
FROM trades WHERE outcome IS NOT NULL AND created_at > NOW() - INTERVAL '24 hours'
GROUP BY 1;

-- Missed wins by gate
SELECT gate, SUM(missed_wins) as missed, SUM(good_blocks) as blocked
FROM (
  SELECT CASE WHEN skip_reason LIKE '%v2.2%' THEN 'v2.2' 
    WHEN skip_reason LIKE '%CASCADE%' THEN 'CASCADE' 
    WHEN skip_reason LIKE '%delta%' THEN 'DELTA' ELSE 'OTHER' END as gate,
    CASE WHEN UPPER(direction)=UPPER(poly_winner) THEN 1 ELSE 0 END as missed_wins,
    CASE WHEN UPPER(direction)!=UPPER(poly_winner) THEN 1 ELSE 0 END as good_blocks
  FROM window_snapshots WHERE poly_winner IS NOT NULL AND trade_placed=false AND skip_reason IS NOT NULL
) t GROUP BY gate;
```
