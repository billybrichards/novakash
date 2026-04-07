# LIVE DATA RULES — Read Before Any Analysis

**Created:** April 7, 2026
**Why:** Agents were mixing backfill/paper data with live trade data, producing wrong conclusions.

---

## Rule 1: Never Mix Data Sources

| Source | What It Is | Use For |
|--------|-----------|---------|
| `trades` table | Real orders placed on Polymarket CLOB | **P&L, WR, actual performance** |
| `window_snapshots` | Every 5-min window evaluation | Signal accuracy, gate analysis |
| `gate_audit` | Per-checkpoint evaluation (19/window) | Gate tuning, timing analysis |
| Backfill data (pre-Apr 6) | Paper-era, different market conditions | **NEVER use for live WR claims** |

**The `trades` table is GROUND TRUTH for P&L.** Window_snapshots include skipped windows that were never traded.

## Rule 2: Always State Sample Size

- N < 50: **Noise.** Do not draw conclusions. Say "insufficient data."
- N = 50-200: **Directional.** Can suggest trends. Say "preliminary, N=X."
- N > 500: **Signal.** Can make claims. Say "N=X over Y hours."

## Rule 3: Same-Window Comparisons Only

To compare signal sources (Tiingo vs Binance vs Chainlink):
```sql
-- CORRECT: Compare on the SAME windows
SELECT 
  SUM(CASE WHEN (delta_tiingo > 0) = (UPPER(poly_winner) = 'UP') THEN 1 ELSE 0 END) as tiingo_correct,
  SUM(CASE WHEN (delta_binance > 0) = (UPPER(poly_winner) = 'UP') THEN 1 ELSE 0 END) as binance_correct,
  COUNT(*) as total
FROM window_snapshots
WHERE poly_winner IS NOT NULL AND delta_tiingo IS NOT NULL AND delta_binance IS NOT NULL;
```

**WRONG:** Comparing Tiingo accuracy on 30 windows vs Binance accuracy on 500 different windows.

## Rule 4: Regime WR Requires Context

The "86% CASCADE WR" from early analysis was **backfill data** (paper-era, trending market, days of data). Live CASCADE WR on Apr 7 was ~64% (11 windows). Both numbers are "correct" for their context — but you CANNOT use the 86% to predict live performance.

**Always say:** "CASCADE WR was X% over N windows in [time period] using [data source]."

## Rule 5: v2.2 Gate Is The Real Edge

As of Apr 7:
- **Without v2.2 gate:** 60% WR on live trades (negative EV at typical entry prices)
- **With v2.2 gate:** 100% WR on 6 trades (+$16.73)

The underlying signal (Tiingo delta at T-70) is ~57% accurate. v2.2 filters it to profitable trade selection. When analysing "should we change gates," always check v2.2's impact first.

## Current Engine Config (v8.1)

```
Delta source: Tiingo REST candles
v2.2 gate: ON for ALL offsets (most important filter)
Eval offsets: Every 10s from T-240 to T-60
Tight DECISIVE: CASCADE + delta≥5bp + v2.2 agree → $0.73 cap
Late offsets: v2.2 HIGH + agrees → dynamic cap
Bet fraction: 7.3%
Order pricing: cap mode (submit at $0.73, CLOB fills at market)
```

## Useful Queries

### Actual trade performance (GROUND TRUTH)
```sql
SELECT outcome, COUNT(*), ROUND(SUM(pnl_usd)::numeric, 2) as net_pnl
FROM trades WHERE outcome IS NOT NULL AND created_at > NOW() - INTERVAL '24 hours'
GROUP BY outcome;
```

### Signal accuracy by regime (from window_snapshots)
```sql
SELECT 
  CASE WHEN vpin >= 0.65 THEN 'CASCADE' WHEN vpin >= 0.55 THEN 'TRANSITION' ELSE 'NORMAL' END as regime,
  COUNT(*) as N,
  SUM(CASE WHEN UPPER(direction) = UPPER(poly_winner) THEN 1 ELSE 0 END) as correct,
  ROUND(100.0 * SUM(CASE WHEN UPPER(direction) = UPPER(poly_winner) THEN 1 ELSE 0 END) / COUNT(*), 1) as accuracy
FROM window_snapshots
WHERE poly_winner IS NOT NULL AND delta_source = 'tiingo_rest_candle'
GROUP BY 1;
```

### Gate block analysis
```sql
SELECT
  CASE 
    WHEN skip_reason LIKE '%v2.2%' THEN 'v2.2 gate'
    WHEN skip_reason LIKE '%not CASCADE%' THEN 'NOT CASCADE'
    WHEN skip_reason LIKE '%delta%' THEN 'DELTA SMALL'
    ELSE 'OTHER'
  END as gate,
  COUNT(*) as blocked,
  SUM(CASE WHEN UPPER(direction) = UPPER(poly_winner) THEN 1 ELSE 0 END) as missed_wins,
  SUM(CASE WHEN UPPER(direction) != UPPER(poly_winner) THEN 1 ELSE 0 END) as good_blocks
FROM window_snapshots
WHERE poly_winner IS NOT NULL AND trade_placed = false AND skip_reason IS NOT NULL
GROUP BY 1;
```

### Per-checkpoint analysis (gate_audit)
```sql
SELECT eval_offset, decision, gate_failed, vpin, delta_pct
FROM gate_audit
WHERE window_ts = X
ORDER BY eval_offset DESC;
```

## DB Tables Reference

| Table | Key | What's Stored |
|-------|-----|---------------|
| `trades` | order_id | Actual CLOB orders, fills, outcomes, P&L |
| `window_snapshots` | (window_ts, asset, timeframe) | Signal eval, direction, vpin, delta, skip_reason, poly_winner |
| `gate_audit` | (window_ts, asset, timeframe, eval_offset) | Per-checkpoint gate results (19 rows/window) |
| `ticks_clob` | (ts, window_ts) | CLOB order book prices every 2s |
| `macro_signals` | id | Macro observer bias/confidence every 60s |
| `post_resolution_analyses` | window_ts | AI analysis after oracle resolution |
| `telegram_notifications` | id | All Telegram notification history |
| `countdown_evaluations` | (window_ts, stage) | T-240/T-180/T-120/T-90 snapshots |
