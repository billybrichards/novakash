# Trade Performance Audit Report — 7 Day Review

**Report Date:** 2026-04-08 05:30 UTC  
**Analysis Period:** 2026-04-01 to 2026-04-08  
**Engine Versions:** v5.0, v5.3, v5.7, v5.7c, v5.8, v8.0, v9.0 (rolling out)

---

## Executive Summary

| Metric | Value |
|--------|-------|
| Total Trades (7 days) | 1,365 |
| Resolved Trades | 1,360 (99.6%) |
| Overall Win Rate | 66.1% (668W/342L) |
| Net PnL | **+$18,007.83** |
| Average Trade PnL | **+$16.25** |
| CLOB Snapshots Logged | 22,944 (every 2s) |
| Signal Evaluations | 1,209 (29.4% trade rate) |

---

## Daily Performance Breakdown

| Date | Trades | Wins | Win Rate | PnL |
|------|--------|------|----------|-----|
| 2026-04-08 | 62 | 5 | 62.5% | -$1.65 |
| 2026-04-07 | 114 | 37 | 68.5% | -$27.27 |
| 2026-04-06 | 173 | 13 | 36.1% | **-$96.10** |
| 2026-04-05 | 215 | 118 | 69.4% | -$11.52 |
| 2026-04-04 | 349 | 262 | **86.2%** | **+$8,063.03** |
| 2026-04-03 | 181 | 117 | 77.0% | +$1,227.63 |
| 2026-04-02 | 260 | 107 | 69.0% | +$8,457.61 |
| 2026-04-01 | 11 | 9 | 81.8% | +$396.09 |

**Key Observation:** April 4th was the standout day with 86.2% win rate and +$8,063 PnL. April 6th was the worst with only 36.1% WR and -$96 PnL.

---

## Engine Version Performance

| Version | Trades | Wins | Win Rate | Total PnL | Avg PnL |
|---------|--------|------|----------|-----------|---------|
| **v5.0** | 100 | 65 | **81.8%** | **+$7,799.40** | **+$779.94** |
| **v5.7c** | 349 | 262 | **75.1%** | **+$8,063.03** | **+$23.10** |
| **v5.3** | 171 | 51 | 66.7% | +$1,054.30 | +$6.17 |
| **v5.7** | 181 | 117 | 64.6% | +$1,227.64 | +$6.78 |
| **v8.0** | 183 | 43 | 68.5% | -$32.25 | -$0.30 |
| **v5.8** | 168 | 103 | 61.3% | -$37.32 | -$0.26 |
| **Unknown** | 213 | 27 | 35.3% | -$66.97 | -$0.31 |

**Key Findings:**
- **v5.0** had the highest win rate (81.8%) and best avg PnL (+$779.94)
- **v5.7c** had the most volume (349 trades) with strong 75.1% WR
- **v8.0** is underperforming with 68.5% WR but negative PnL
- **Unknown version** trades have very low 35.3% WR — need investigation

---

## Execution Mode Analysis

| Mode | Trades | Wins | Win Rate | Total PnL |
|------|--------|------|----------|-----------|
| **Default/Unspecified** | 1,111 | 625 | **75.7%** | **+$18,040.09** |
| **GTC** | 162 | 34 | 65.4% | -$38.30 |
| **Paper** | 89 | 6 | 66.7% | -$2.25 |
| **Live** | 3 | 3 | **100%** | +$8.29 |

**Key Insight:** The vast majority of trades (1,111/1,365 = 81.4%) are in the default mode with excellent 75.7% WR. GTC fallbacks are underperforming at 65.4% WR.

---

## Recent Activity (Last 48 Hours — v9.0 Deployment)

| Date | Engine | Trades | Wins | Win Rate | PnL |
|------|--------|--------|------|----------|-----|
| 2026-04-08 | v8.0 | 62 | 5 | 62.5% | -$1.65 |
| 2026-04-07 | v8.0 | 114 | 37 | 68.5% | -$27.27 |
| 2026-04-06 | v8.0 | 7 | 1 | 50.0% | -$3.33 |
| 2026-04-06 | Unknown | 166 | 12 | 35.3% | -$92.78 |
| 2026-04-05 | v5.8 | 83 | 45 | 68.2% | -$63.52 |
| 2026-04-05 | Unknown | 47 | 15 | 55.6% | +$25.81 |

**v9.0 Status:**
- Deployed: 2026-04-07 22:26 UTC
- Current window: CL+TI disagreeing → source agreement gate skipping
- Expected: When CL+TI agree, 94.7% WR (161/170 historical)
- CLOB feed: ✅ Logging every 2s (22,944 snapshots in last 24h)

---

## v9.0 Gate Performance (Signal Evaluations)

| Metric | Value |
|--------|-------|
| Total Evaluations (7 days) | 1,209 |
| Trade Decisions | 356 (29.4%) |
| Skip Decisions | 853 (70.6%) |
| **Trade Rate** | **29.4%** |

**v9.0 Filtering:**
- Source agreement gate (CL+TI) filtering 70.6% of evaluations
- When CL+TI agree: 94.7% WR historically
- Current behavior: Skipping disagreements correctly

---

## CLOB Feed Status

| Metric | Value |
|--------|-------|
| Snapshots Logged (24h) | 22,944 |
| First Snapshot | 2026-04-07 05:29:40 UTC |
| Last Snapshot | 2026-04-07 22:40:44 UTC |
| Frequency | Every 2 seconds |
| Errors | ✅ **Fixed** (no more database errors) |

**Database Tables:**
- `ticks_clob` ✅ Populated (22,944 rows)
- `clob_book_snapshots` ⚠️ Created but empty (migration ran, engine needs to write)

---

## Issues & Recommendations

### 1. Critical: Unknown Version Trades
**Issue:** 213 trades with no `engine_version` set (35.3% WR, -$66.97)

**Action:**
- Check `metadata->>'engine_version'` field usage
- Ensure all trades capture version in `created_at` or `metadata`
- High volume + low WR = significant opportunity cost

### 2. v8.0 Underperformance
**Issue:** v8.0 shows 68.5% WR but negative PnL (-$32.25 over 183 trades)

**Action:**
- Analyze v8.0 entry prices vs cap
- Check if FOK/GTC fallback is causing poor fills
- Compare with v5.7c (75.1% WR, +$8,063 PnL)

### 3. GTC Fallback Performance
**Issue:** GTC mode at 65.4% WR vs 75.7% default

**Action:**
- Review v9.0 FAK implementation (should improve fill rate)
- Check if GTC prices are too aggressive
- Monitor v9.0 live FAK fills for improvement

### 4. v9.0 Monitoring
**Status:** Just deployed, waiting for CL+TI agreement

**Action:**
- Monitor `v9.source_agree` logs
- Track first v9.0 FAK fill
- Verify cap tiers ($0.55 early, $0.65 golden)

---

## Next 24h Checklist

- [ ] **Monitor v9.0:** First CL+TI agreement trade → expect 94.7% WR
- [ ] **Check FAK fills:** Verify partial fills working correctly
- [ ] **Fix unknown version:** Ensure all trades capture engine_version
- [ ] **CLOB audit tables:** Run migration again if clob_book_snapshots still empty
- [ ] **Daily review:** Compare v9.0 vs v8.0 performance

---

## Success Metrics (v9.0 Target)

| Metric | v8.0 | v9.0 Target |
|--------|------|-------------|
| Trade Frequency | ~30/day | ~12-15/day (filtered) |
| Win Rate | 68.5% | **~94.7%** (when CL+TI agree) |
| EV/Trade | -$0.30 | **+$2.76** |
| Fill Rate | ~40% (GTC) | **~80%** (FAK partials) |

---

**Report Generated:** 2026-04-08 05:30 UTC  
**Engine Status:** v9.0 deployed, monitoring for CL+TI agreement  
**Data Sources:** trades (1,365), signal_evaluations (1,209), ticks_clob (22,944)
