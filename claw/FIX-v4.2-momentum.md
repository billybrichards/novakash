# v4.2 Fix Plan — All-Momentum Direction

**Date:** 2026-04-03  
**Priority:** URGENT — current v4.1 is betting WRONG DIRECTION in NORMAL + TRANSITION regimes  
**Confirmed by:** 7-day, 2,016-market Polymarket oracle cross-validation

---

## The Bug

v4.1 uses CONTRARIAN direction in NORMAL (VPIN < 0.55) and TRANSITION (VPIN 0.55-0.65) regimes. This is based on a misapplication of De Nicola (2021).

**De Nicola's finding:** negative autocorrelation BETWEEN consecutive 5-min windows  
**What v4.1 does:** bets against the delta WITHIN the same window  
**These are different things.** Within a window, the T-60 delta predicts the oracle 97%+ of the time.

## 7-Day Evidence (2,016 markets)

Within-window (T-60 delta → same window oracle):
- d>=0.08%: **Momentum 97.1%** vs Contrarian 2.9%
- d>=0.10%: **Momentum 98.1%** vs Contrarian 1.9%

Between-window (this window → next window):
- d>=0.08%: Persists 47.2% vs **Reverses 52.8%** ← De Nicola effect exists HERE
- CASCADE regime: **Persists 57.1%** vs Reverses 42.9% ← momentum persists in cascade

---

## Code Changes Required

### 1. `_evaluate_signal()` — ALL regimes use MOMENTUM

```python
# BEFORE (v4.1 — BROKEN for NORMAL + TRANSITION):
if current_vpin >= 0.65:
    direction = "UP" if delta_pct > 0 else "DOWN"   # momentum ✅
elif current_vpin >= 0.55:
    direction = "DOWN" if delta_pct > 0 else "UP"   # contrarian ❌
else:
    direction = "DOWN" if delta_pct > 0 else "UP"   # contrarian ❌

# AFTER (v4.2 — ALL MOMENTUM):
direction = "UP" if delta_pct > 0 else "DOWN"  # ALWAYS momentum
# Regime only affects delta threshold and bet sizing
```

### 2. Per-regime delta thresholds (keep from v4.1, adjust)

| Regime | VPIN | Direction | Min Delta | Rationale |
|--------|------|-----------|-----------|-----------|
| CALM | < 0.45 | SKIP | — | No informed flow |
| NORMAL | 0.45-0.55 | MOMENTUM | 0.08% | Standard signal |
| TRANSITION | 0.55-0.65 | MOMENTUM | 0.05% | More informed = more reliable |
| CASCADE | >= 0.65 | MOMENTUM | 0.03% | VPIN IS the signal |

### 3. Confidence function — simplify

Current confidence function only returns HIGH if delta > 0.10%. Most of our d>=0.08% trades get MODERATE. Since momentum is 97%+ correct at d>=0.08%, the confidence gating is too aggressive.

**Fix:** At d>=0.08% with VPIN >= 0.45, confidence should be at least MODERATE (never blocked).

### 4. Remove unnecessary skip conditions

The `evaluate.skip reason='no edge'` log shows trades being skipped at deltas like -0.047%. In TRANSITION regime (VPIN 0.55-0.65), this should be a valid trade at the 0.05% threshold. The old contrarian logic with 0.12% bar was blocking these.

---

## Expected Impact

**Before (v4.1):** ~15% WR in NORMAL regime (betting wrong direction)  
**After (v4.2):** ~97% WR at d>=0.08%, ~93% at d>=0.05%, ~90% at d>=0.03%

**The WR won't actually be 97% in practice** because:
1. Token pricing: at T-60, market may have already priced in the move
2. Fill quality: GTC limit orders may not fill if price moves
3. Oracle timing differences
4. But should be dramatically better than 15%

---

## Implementation Steps

1. Change direction to ALWAYS MOMENTUM
2. Adjust per-regime delta thresholds (lower in high-VPIN)
3. Fix confidence function (don't block valid signals)
4. Push to develop → auto-deploy
5. Monitor first 10 trades for correct direction
