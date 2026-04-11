# CHANGELOG-v11.2-REGIME-HYSTERESIS

## Overview

**Version:** v11.2  
**Date:** 2026-04-10 18:18 UTC  
**Author:** Novakash2

### Summary

Added hysteresis to regime classification to prevent frequent regime flipping (LOW_VOL ↔ TRENDING) caused by the 60-price lookback being too sensitive to short-term volatility changes.

---

## Changes

### 1. Increased Lookback Window

**Before:** 60 prices (~5 minutes at 5-second sampling)  
**After:** 120 prices (~10 minutes at 5-second sampling)

**Impact:** Smoother volatility calculation, less sensitive to short-term price movements.

```python
# Before
_HISTORY_MAXLEN: int = 60  # 60 prices ≈ 5 minutes at 5 s intervals

# After
_HISTORY_MAXLEN: int = 120  # 120 prices ≈ 10 minutes at 5 s intervals
```

---

### 2. Added Hysteresis Logic

**Before:** Immediate regime change on any volatility threshold crossing  
**After:** Requires 2 consecutive regime flips before switching

**Impact:** Prevents flip-flopping when volatility hovers near threshold boundaries.

```python
# New constant
_HYSTERESIS_COUNT: int = 2  # Require 2 consecutive flips to change regime

# New state tracking
self._consecutive_flips: int = 0  # Track consecutive regime flips
self._pending_regime: Optional[str] = None  # Pending regime change
```

**New Method:**
```python
def _check_regime_change(self, new_regime: str) -> bool:
    """Check if regime change should be committed (with hysteresis)."""
    if new_regime == self._current_regime:
        self._consecutive_flips = 0
        self._pending_regime = None
        return False
    
    # Different regime - check hysteresis
    if self._pending_regime == new_regime:
        # Same pending regime, increment counter
        self._consecutive_flips += 1
    else:
        # New pending regime
        self._consecutive_flips = 1
        self._pending_regime = new_regime
    
    # Commit if we have enough consecutive flips
    return self._consecutive_flips >= _HYSTERESIS_COUNT
```

---

### 3. Updated Logging

**Before:** Logs every regime change immediately  
**After:** Logs intermediate updates as debug, only logs committed changes as info

**Impact:** Cleaner logs, better visibility into regime stability.

```python
# Before
if changed:
    self._log.info("regime_change", ...)
else:
    self._log.debug("regime_update", ...)

# After
if changed:
    self._log.info("regime_change", ...)
else:
    self._log.debug("regime_update", ..., consecutive_flips=...)
    return  # No regime change logged
```

---

## Problem Solved

### Before (60-price lookback, no hysteresis)

```
18:05:36  regime=LOW_VOL  vol_pct=0.0007
18:05:38  regime=TRENDING  vol_pct=0.0007
18:05:39  regime=LOW_VOL  vol_pct=0.0007
18:05:41  regime=TRENDING  vol_pct=0.0007
18:05:43  regime=LOW_VOL  vol_pct=0.0005
```

**Issue:** Regime flipping every 2-4 seconds despite similar volatility levels. This caused:
- Inconsistent regime-based gating decisions
- Noisy log output
- Unstable trading behavior

---

### After (120-price lookback, 2-flip hysteresis)

Expected behavior:
```
18:05:36  regime=LOW_VOL  vol_pct=0.0007
18:05:38  regime_update  consecutive_flips=1  (pending TRENDING)
18:05:39  regime=TRENDING  vol_pct=0.0007  (committed after 2 flips)
18:05:41  regime_update  consecutive_flips=1  (pending LOW_VOL)
18:05:43  regime_update  consecutive_flips=2  (committed LOW_VOL)
18:05:45  regime=LOW_VOL  vol_pct=0.0005
```

**Improvement:** Regime only changes after consistent signal over ~20 seconds (2 consecutive 10-second windows).

---

## Files Changed

| File | Change |
|------|--------|
| `engine/signals/regime_classifier.py` | 120-price lookback, hysteresis logic |

---

## Deployment

### Environment Changes

None required. This is a code-only change.

### Steps

1. ✅ Code committed to `develop` branch
2. ✅ Documentation created (`docs/CHANGELOG-v11.2-REGIME-HYSTERESIS.md`)
3. ⏳ Pull on Montreal: `git pull origin develop`
4. ⏳ Restart engine: `pkill -9 python3 main.py && python3 main.py`

---

## Testing

### Before Restart

Monitor for continued regime flipping:
```bash
tail -f /home/novakash/engine.log | grep regime_change
```

### After Restart

Expected:
- Fewer `regime_change` log entries
- More `regime_update` entries with `consecutive_flips` tracking
- Longer periods between regime changes

### Validation

After 1 hour:
- Count `regime_change` entries (should be < 10/hour for normal market conditions)
- Check for consecutive flips in logs: `grep consecutive_flips | tail -20`

---

## Rollback

If issues arise:
1. Revert commit: `git revert <commit-hash>`
2. Pull on Montreal: `git pull origin develop`
3. Restart engine

---

## Related

- v11.0: Dynamic TimesFM confidence gating (`ee1b10fe`)
- v11.1: 2/3 majority source agreement (`cc4fbaf4`)
- PR #12: Double-gating bug fix (`1744fde6`)
