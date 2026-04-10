# v11.0 Dynamic Confidence Gating — Implementation

## Overview
- **Problem**: v5/SEQUOIA outputs P(UP)=0.606 consistently, but current v2.2 gate requires p > 0.65 (HIGH confidence)
- **Solution**: Use TimesFM confidence to dynamically adjust the probability threshold
- **Impact**: Unblocks valid v5 signals at P(UP)=0.606 when timesfm.confidence >= 0.90

## Changes

### 1. Environment Variables (Montreal .env)

```bash
# Dynamic confidence gating (v11.0)
V10_CASCADE_MIN_CONF=0.90        # TimesFM confidence threshold for bonus
V10_CASCADE_CONF_BONUS=0.05      # Bonus applied when confidence >= 0.90
V10_TRANSITION_MIN_P=0.58        # Lowered from 0.70
V10_CASCADE_MIN_P=0.55           # Lowered from 0.67
V10_NORMAL_MIN_P=0.55            # Lowered from 0.60
V81_CAP_T60=0.73                 # Raised from 0.65
V10_DUNE_CAP_CEILING=0.73        # Raised from 0.68
```

### 2. Gate Logic (five_min_vpin.py)

**Current v8.1 gate (blocking 0.606):**
```python
# Gate 1: v2.2 must be HIGH confidence (p > 0.65 or p < 0.35)
elif not _v2_high:
    signal = None
    self._last_skip_reason = f"v2.2 LOW conf ({_v2_p:.2f}) at T-{eval_offset}"
```

**New v11.0 gate (dynamic confidence adjustment):**
```python
# v11.0: Dynamic confidence gating
_v2_conf = float(_v2_result.get("timesfm", {}).get("confidence", 0.93))
_v2_high = _v2_p > 0.65 or _v2_p < 0.35  # Old HIGH confidence check

# New: Dynamic threshold based on TimesFM confidence
if _v2_conf >= 0.90:
    _v2_effective_threshold = 0.55  # Allow P(UP) >= 0.55 when conf >= 0.90
    _v2_dynamic_high = _v2_p >= _v2_effective_threshold
elif _v2_conf >= 0.80:
    _v2_effective_threshold = 0.58
    _v2_dynamic_high = _v2_p >= _v2_effective_threshold
elif _v2_conf >= 0.70:
    _v2_effective_threshold = 0.60
    _v2_dynamic_high = _v2_p >= _v2_effective_threshold
else:
    _v2_effective_threshold = 0.65
    _v2_dynamic_high = _v2_p > 0.65 or _v2_p < 0.35  # Old HIGH confidence

# Gate 1: v2.2 must be dynamic HIGH confidence OR agree strongly
if not (_v2_dynamic_high or (_v2_agrees and _v2_p >= _v2_effective_threshold)):
    signal = None
    self._last_skip_reason = f"v2.2 conf={_v2_conf:.2f}, P={_v2_p:.3f} < {_v2_effective_threshold:.2f} at T-{eval_offset}"
```

### 3. Database Schema (Already Exists)
- `window_snapshots.timesfm_confidence` — already added in v6.0
- No migration needed

### 4. Deployment Checklist

**Phase 1 (Env Changes — Immediate):**
- [ ] `V10_CASCADE_MIN_P=0.55`
- [ ] `V10_TRANSITION_MIN_P=0.58`
- [ ] `V10_NORMAL_MIN_P=0.55`
- [ ] `V81_CAP_T60=0.73`
- [ ] `V10_DUNE_CAP_CEILING=0.73`

**Phase 2 (Code Changes — Dynamic Gating):**
- [ ] Add confidence bonus logic to five_min_vpin.py
- [ ] Store `timesfm.confidence` from v2.2 API response
- [ ] Test locally/on staging

**Phase 3 (Risk Management):**
- [ ] `KILL_AUTO_RESUME_MINUTES=30` (optional)
- [ ] `CONSECUTIVE_LOSS_COOLDOWN=3` (already default)
- [ ] `COOLDOWN_SECONDS=900` (already default)

## Expected Impact

| Scenario | Old Gate | New Gate (v11.0) | Result |
|----------|----------|------------------|--------|
| P=0.606, conf=0.93 | BLOCKED (not HIGH conf) | **ALLOWED** (conf >= 0.90, threshold=0.55) | ✅ Pass |
| P=0.606, conf=0.70 | BLOCKED | ALLOWED (threshold=0.60) | ✅ Pass |
| P=0.606, conf=0.50 | BLOCKED | BLOCKED (threshold=0.65) | ❌ Block |
| P=0.70, conf=0.93 | ALLOWED | ALLOWED | ✅ Pass |

## Testing Plan

1. **Pre-deployment**: Test with historical data (Apr 9-10 windows)
2. **Post-deployment**: Monitor first 24h:
   - Fill rate for P=0.606 signals
   - Win rate of newly-unblocked trades
   - TimesFM confidence distribution

## Changelog Entry

```markdown
## v11.0 — Dynamic Confidence Gating (2026-04-10)

### Features
- **Dynamic confidence gating**: Use TimesFM confidence to adjust v2.2 probability threshold
  - conf >= 0.90: Allow P(UP) >= 0.55 (was 0.65)
  - conf >= 0.80: Allow P(UP) >= 0.58
  - conf >= 0.70: Allow P(UP) >= 0.60
  - conf < 0.70: Require HIGH confidence (p > 0.65 or p < 0.35)

### Configuration
- `V10_CASCADE_MIN_P=0.55` (was 0.67)
- `V10_TRANSITION_MIN_P=0.58` (was 0.70)
- `V10_NORMAL_MIN_P=0.55` (was 0.60)
- `V81_CAP_T60=0.73` (was 0.65)
- `V10_DUNE_CAP_CEILING=0.73` (was 0.68)
- `V10_CASCADE_MIN_CONF=0.90` (new)
- `V10_CASCADE_CONF_BONUS=0.05` (new)

### Impact
- Unblocks v5/SEQUOIA signals at P(UP)=0.606 when timesfm.confidence >= 0.90
- Maintains quality control via confidence-based threshold adjustment
- Expected fill rate increase: ~20-30% more valid signals processed
```
