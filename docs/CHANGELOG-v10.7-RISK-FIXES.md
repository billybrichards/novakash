# CHANGELOG-v10.7-RISK-FIXES

**Date:** 2026-04-10 19:45 UTC  
**Version:** v10.7.1  
**Branch:** develop

---

## Summary

v10.7.1 activates the risk management features that were implemented but not yet enabled:

1. **Kill Switch Auto-Resume** - After 30 min cooldown, kill switch clears automatically
2. **Consecutive Loss Cooldown** - 10 min pause after 3 consecutive losses
3. **Volatility-Based Sizing** - Reduced stakes in choppy/high-volatility markets

**No code changes required** - all features already implemented in codebase. Only environment variable activation needed.

---

## Changes

### Environment Variables (Add to .env)

```bash
# === v10.7 Risk Fixes ===
KILL_AUTO_RESUME_MINUTES=30
CONSECUTIVE_LOSS_COOLDOWN=3
COOLDOWN_SECONDS=600

# v10.7 Volatility-Based Sizing
V10_VOL_SIZING_ENABLED=true
V10_VOL_HIGH_THRESHOLD=0.004
V10_VOL_HIGH_MULT=0.50
V10_VOL_NORMAL_THRESHOLD=0.002
V10_VOL_NORMAL_MULT=0.75
V10_VOL_LOW_MULT=1.0
```

### Code Changes (Already Implemented)

| File | Feature | Lines |
|------|---------|-------|
| `engine/execution/risk_manager.py` | Kill auto-resume | 193-207 |
| `engine/execution/risk_manager.py` | Consecutive loss cooldown | 129-135 |
| `engine/config/runtime_config.py` | Runtime defaults | N/A (already set) |

---

## Impact

### Kill Auto-Resume (KILL_AUTO_RESUME_MINUTES=30)

**Before:**
- Kill switch persists until manual `resume()` call
- Blocked trades continue to miss even after wallet recovery
- Yesterday: 21 trades blocked (19W/2L = 90.5% WR, ~$30 lost profit)

**After:**
- Kill switch auto-clears after 30 min cooldown
- Drawdown check still gates trading if bankroll hasn't recovered
- Once wallet topped up or positions resolve, trading resumes automatically

**Log Pattern:**
```
risk.kill_auto_resumed minutes=30 drawdown="14.2%"
```

### Consecutive Loss Cooldown (CONSECUTIVE_LOSS_COOLDOWN=3, COOLDOWN_SECONDS=600)

**Before:**
- No pause during loss streaks
- Today: 4 losses in a row (18:28-18:37 UTC, -$8.49)

**After:**
- 10 min pause after 3 consecutive losses
- Prevents extended loss streaks during choppy sessions
- Stakes reset to normal after cooldown

**Log Pattern:**
```
risk.cooldown_triggered losses=3
```

### Volatility-Based Sizing (V10_VOL_SIZING_ENABLED=true)

**Before:**
- Flat stakes regardless of market conditions
- Full stakes in choppy/high-volatility markets

**After:**
- High vol (>0.4% 5-min range): 50% stake
- Normal vol (0.2-0.4%): 75% stake
- Low vol (<0.2%): 100% stake

**Log Pattern:**
```
v10.trade cap=$X.XX vol_regime=HIGH
```

---

## Deployment

### Step 1: Add Environment Variables

```bash
# On Montreal
cd /home/novakash/novakash/engine

# Backup current .env
cp .env .env.backup.pre-v10.7

# Add v10.7 settings (append to end of .env)
cat >> .env << 'EOF'

# === v10.7 Risk Fixes ===
KILL_AUTO_RESUME_MINUTES=30
CONSECUTIVE_LOSS_COOLDOWN=3
COOLDOWN_SECONDS=600

# v10.7 Volatility-Based Sizing
V10_VOL_SIZING_ENABLED=true
V10_VOL_HIGH_THRESHOLD=0.004
V10_VOL_HIGH_MULT=0.50
V10_VOL_NORMAL_THRESHOLD=0.002
V10_VOL_NORMAL_MULT=0.75
V10_VOL_LOW_MULT=1.0
EOF
```

### Step 2: Restart Engine

```bash
# Kill current engine
pkill -9 python3 main.py
sleep 2

# Restart
cd /home/novakash/novakash/engine
nohup python3 main.py > /home/novakash/engine.log 2>&1 &

# Verify running
ps aux | grep 'python3 main.py' | grep -v grep
```

### Step 3: Verify Deployment

```bash
# Check env vars loaded
grep 'KILL_AUTO_RESUME\|VOL_SIZING' /home/novakash/novakash/engine/.env

# Check engine logs
tail -50 /home/novakash/engine.log | grep -E 'startup|v10|risk'

# Monitor first trade
tail -100 /home/novakash/engine.log | grep 'v10.trade'
```

---

## Rollback

If issues arise:

```bash
# Restore backup
cd /home/novakash/novakash/engine
cp .env.backup.pre-v10.7 .env

# Restart engine
pkill -9 python3 main.py
sleep 2
nohup python3 main.py > /home/novakash/engine.log 2>&1 &
```

---

## Monitoring (First 24h)

### Key Metrics

| Metric | Target | Watch For |
|--------|--------|-----------|
| **Kill Auto-Resumes** | 0-2/day | `risk.kill_auto_resumed` in logs |
| **Cooldown Triggers** | 1-3/day | `risk.cooldown_triggered` in logs |
| **Volatility Sizing** | Variable | `vol_regime=HIGH/LOW/NORMAL` in trade logs |
| **Overall WR** | ≥60% | v11.1 2/3 majority trades |

### Log Patterns

**Kill Auto-Resume:**
```
2026-04-XXTXX:XX:XXZ risk.kill_auto_resumed minutes=30 drawdown="X.X%"
```

**Cooldown Trigger:**
```
2026-04-XXTXX:XX:XXZ risk.cooldown_triggered losses=3
```

**Volatility Sizing:**
```
2026-04-XXTXX:XX:XXZ v10.trade cap=$X.XX vol_regime=HIGH
```

---

## Related Issues

### Issue: Trade Bible Outdated
- **Status:** Missing 17 v11.1 trades (18:04-19:38 UTC)
- **Action:** Regenerate after 24h v11.1 data
- **Location:** `docs/truth_dataset/20260410-115338/trade_bible.csv`

### Issue: CLOB Write Error
- **Status:** `'the server expects 10 arguments for this query, 11 were passed'`
- **Frequency:** Occasional
- **Impact:** Non-blocking (trade still executes)
- **Action:** Investigate after v10.7 deployment

---

## Performance Context

### Pre-v10.7 (Trade Bible, Apr 9-10, 130 Trades)

| Regime | Trades | WR | PnL |
|--------|--------|-----|-----|
| **TRANSITION** | 66 | 65.2% | - |
| **NORMAL** | 36 | 72.2% | - |
| **CASCADE** | 26 | 53.8% | - |
| **TOTAL** | 130 | 65.4% | - |

### Today v11.1 (18:04-19:38 UTC, 17 Trades)

| Regime | Trades | Resolved | WR |
|--------|--------|----------|-----|
| **TRANSITION** | 9 | 4L | 0% (n=4) |
| **NORMAL** | 5 | 1W | 100% (n=1) |
| **CASCADE** | 3 | 0 | - |

**Note:** Today's sample is too small (17 trades) - variance will mislead. Wait 24h for proper evaluation.

---

## References

- **v10.7 Proposal:** `docs/v10_7_config_proposal.md`
- **v10.7 Plan:** `docs/v10_7_consolidated_plan.md`
- **Deployment Guide:** `docs/DEPLOYMENT.md`
- **Handover Doc:** `memory/v10.7-deployment-handover.md` (this session)

---

## Sign-Off

| Item | Status |
|------|--------|
| Code Implemented | ✅ |
| Env Vars Documented | ✅ |
| Deployment Steps | ✅ |
| Rollback Plan | ✅ |
| Monitoring Plan | ✅ |
| Handover Doc | ✅ |

**Ready for deployment. Awaiting approval.**
