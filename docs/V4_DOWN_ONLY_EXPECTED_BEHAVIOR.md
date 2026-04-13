# V4 Down-Only Strategy - Expected Behavior

**Created:** 2026-04-13  
**Status:** Active monitoring

## Strategy Specification

### v4_down_only
- **Direction:** DOWN only (UP predictions always skipped)
- **Confidence:** dist ≥ 0.12 (|p_up - 0.5| ≥ 0.12)
- **Timing Window:** T-90 to T-150 seconds before window close (CRITICAL!)
- **Hours:** Any UTC hour
- **CLOB sizing:** 2.0× at clob_down_ask ≥ 0.55 · 1.5× at ≥0.35 · 1.0× below

### v4_up_asian  
- **Direction:** UP only
- **Confidence:** dist 0.15-0.20 (medium conviction band)
- **Timing:** T-90 to T-150
- **Hours:** 23:00-02:59 UTC (Asian session only)

## Expected Behavior

### Normal Operation
1. **Window Opens** (T=0 to T=300, where T=300 is window close)
2. **Evaluation Starts** at T-240 (4 minutes before close) - this is when the feed begins polling
3. **Tradeable Window** is T-150 to T-90 (2:30 to 1:30 minutes before close)
4. **Window Closes** at T=0

### What to Expect

**Every 2 minutes** (12 windows per hour), when conditions are met:
- TimesFM predicts DOWN with confidence ≥ 0.12 (dist from 0.5)
- We are in the T-90 to T-150 evaluation window
- v4_down_only will **TRADE**

**In normal market:**
- ~30-50 DOWN trades per day (varies with volatility)
- Flat market: ~5-15/day
- Trending day: 30-50+

**When NEITHER fires (most windows):**
- Model is flat (dist < 0.12 for DOWN)
- Signal is UP (outside Asian session for v4_up_asian)
- eval_offset outside T-90-150 (too early/late in window) - **THIS IS NORMAL**
- Regime = risk_off (V4 blocks all strategies)

## Current Issue (2026-04-13 13:44 UTC)

**Engine was dead for 1+ hour** (last log: 12:31:16, now: 13:44:00)
- Engine restarted mid-window at T-180
- Evaluation started too late, missed T-150 to T-90 window
- Now at T-33, window about to close

**Fix:** Engine needs to run continuously. The next window will be evaluated from T-240 onwards, and when it reaches T-150 to T-90, if DOWN signal is strong, v4_down_only will trade.

## TimesFM Status ✅

- **Working correctly:** Returns variable probabilities (0.96-0.98 DOWN)
- **NOT the old bug:** No longer constant 0.606
- **Latency:** ~1.3-1.6s per prediction
- **Direction:** Consistent DOWN predictions in current market

## Monitoring Checklist

### Daily Checks
- [ ] Engine process running (`pgrep -fa "python3 main.py"`)
- [ ] New windows being evaluated (window_ts changes every 5 minutes)
- [ ] eval_offset progressing T-240 → T-60
- [ ] v4_down_only seeing T-90 to T-150 window
- [ ] DOWN predictions (check TimesFM logs)

### Trade Expectations
- **When DOWN is predicted** with dist ≥ 0.12 AND we're in T-90-150 window → TRADE
- **When UP is predicted** → v4_down_only SKIP (by design)
- **When dist < 0.12** → SKIP (low conviction)
- **When outside T-90-150** → SKIP (timing gate)

### Alert Triggers
- Engine dead for > 10 minutes
- DOWN WR < 65% over 10+ trades
- No trades in 4+ hours during volatile market
- eval_offset stuck (not progressing)

## Logs to Monitor

```bash
# Engine running
pgrep -fa "python3 main.py"

# Current window and timing
grep "window.signal" /home/novakash/engine.log | tail -5

# TimesFM predictions
grep "timesfm.forecast_fetched" /home/novakash/engine.log | tail -10

# v4_down_only decisions
grep "v4_down_only" /home/novakash/engine.log | tail -20

# Window evaluations (check timing progression)
grep "eval_offset=" /home/novakash/engine.log | tail -30
```

## Key Takeaway

**The strategy works correctly when:**
1. Engine is running continuously
2. We evaluate windows from T-240 onwards  
3. We reach the T-90 to T-150 window
4. TimesFM predicts DOWN with dist ≥ 0.12

**Then v4_down_only WILL trade.**

The current skip is due to engine restart mid-window, not a strategy bug.
