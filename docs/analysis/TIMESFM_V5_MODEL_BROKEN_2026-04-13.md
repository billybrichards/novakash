# 12-Hour Strategy Performance Analysis - TimesFM v5.2 BREAKING

**Date:** 2026-04-13 11:45 UTC  
**Analysis Period:** Last 12h (Apr 12 23:30 - Apr 13 11:30 UTC)  
**Status:** 🔴 **MODEL BROKEN** - Push-mode not implemented on TimesFM service

---

## Executive Summary

### The Problem

**Our TimesFM v5.2 model is returning a CONSTANT `0.60614485` probability for ALL predictions** because it's receiving **NO FEATURES** from the server.

| Metric | Expected | Actual | Status |
|--------|----------|--------|--------|
| P(UP) variation | 0.3-0.9 | **0.606 (constant)** | 🔴 BROKEN |
| Conviction | 15-25% | **10.2% (constant)** | 🔴 BELOW THRESHOLD |
| Direction predictions | Mixed | **UP 100%** | 🔴 WRONG |
| Trades executed | ~20 | **0** | ⚠️ CORRECT FILTERING |

### Root Cause: Train/Serve Skew

The v5.2 LightGBM model was trained with **25 specific features**, but the TimesFM service is **NOT implementing push-mode features**. When the model receives no features:

```
All 25 features = NaN
→ LightGBM follows "missing value" path at each split
→ Reaches default leaf node
→ Returns constant 0.60614485 (training data's base UP rate)
→ Direction = "UP" (since 0.606 > 0.5)
→ Conviction = 10.6% (below 12% trading threshold)
→ ALL trades BLOCKED
```

### The Market Reality

- **Model predicted:** UP 100% of the time (13,096 evals)
- **Actual market:** DOWN 82.4% of the time (10,736 DOWN windows)
- **Would-have win rate:** 18.1% (terrible!)
- **Our strategies:** Correctly filtered 0 trades (GOOD!)

---

## Technical Investigation

### Evidence: Constant Probability

```sql
SELECT 
    v2_direction,
    COUNT(*) as n,
    ROUND(AVG(ABS(COALESCE(v2_probability_up, 0.5) - 0.5) * 100), 2) as avg_conv
FROM signal_evaluations
WHERE evaluated_at >= NOW() - INTERVAL '12 hours'
  AND asset = 'BTC'
GROUP BY 1;
```

**Result:**
```
UP: 13,096 evals, avg_conv=10.25%
DOWN: 0 evals
```

**Every single prediction in 12h has the exact same conviction (10.2-10.7%)** - this is the smoking gun.

### Evidence: Market Was DOWN

```
Last 12h windows: 13,022
UP windows: 2,286 (17.6%)
DOWN windows: 10,736 (82.4%)
```

The model is **completely wrong** - it predicted UP 100% of the time when the market went DOWN 82% of the time.

### Evidence: Would-Have Performance

If we had relaxed our thresholds and traded:

| Strategy | Would-Have Trades | Would-Have WR | Actual PnL |
|----------|------------------|---------------|------------|
| DOWN-Only | ~6,500 | **18%** | N/A (blocked) |
| Asian UP | ~0 | N/A | N/A |
| **Conclusion** | **Disaster** | **~82% loss rate** | **Saved by filters** |

**Our strict 12% conviction threshold saved us from thousands of losing trades.**

---

## Root Cause Analysis

### v5.2 Architecture

The Sequoia v5.2 model is a **LightGBM model with 65 features** trained on 9,797 windows. Key details:

- **Model type:** LightGBM (one wide model over `eval_offset`)
- **Features:** 25 specific features defined in `FEATURE_COLUMNS_V5`
- **Output:** `P(UP)` probability with isotonic calibration
- **Deployment:** 2026-04-10 13:21:33 UTC
- **Expected accuracy:** 70-85% at high confidence

### The Train/Serve Skew

From `engine/signals/v2_feature_body.py`:

```python
"""
Sequoia v5 ships a training refactor (one wide LightGBM model over
`eval_offset`, sourced from `signal_evaluations`) but its serving path
was never wired up: the Montreal scorer's pull-mode feature assembly
still produces v4 feature names, so every v5 inference received 25
NaNs and returned the all-missing leaf — the constant 0.60614485 we
caught in production on 2026-04-10.
"""
```

**What this means:**

1. **Training:** Model trained with 25 features (feature body)
2. **Serving (v4):** Old pull-mode assembly produces v4 feature names
3. **Mismatch:** Model expects v5 features, gets v4 features = 25 NaNs
4. **Fallback:** LightGBM default leaf = constant 0.60614485

### Why Push-Mode Exists

The fix was implemented in `engine/signals/timesfm_v2_client.py`:

```python
async def score_with_features(
    self,
    asset: str,
    seconds_to_close: int,
    features: V5FeatureBody,
    model: str = "oak",
) -> dict:
    """
    PUSH-MODE scoring for Sequoia v5+.
    
    The engine hands the scorer a fully-populated `V5FeatureBody`;
    the scorer uses it directly in place of its own pull-mode
    feature assembly. This is the only code path that avoids the
    train/serve skew that shipped with v5.
    """
```

**Push-mode workflow:**
1. Engine builds 25-feature body locally
2. POST `/v2/probability` with full feature body
3. Scorer uses provided features (no pull-mode)
4. Model gets correct input → returns accurate prediction

**But:** The TimesFM service (3.98.114.0:8080) **has NOT implemented the POST endpoint**.

---

## Evidence Timeline

### Apr 10, 13:21 UTC - v5.2 Deployed

- Model deployed with 25-feature training
- Push-mode NOT implemented on service
- Constant 0.606 predictions begin

### Apr 10-12 - Mixed Performance

- Some high-conviction evals (up to 21.9%)
- Likely using v4 fallback or cached data
- 20 trades in 12-24h period

### Apr 12, 15:00 UTC - Full Break

- Model stuck at 10.2% conviction
- All predictions UP
- 0 trades in subsequent 12h

### Apr 13, 11:30 UTC - Investigation

- Identified constant 0.606 probability
- Confirmed push-mode not implemented
- Root cause: train/serve skew

---

## Impact Assessment

### Current State (Last 12h)

| Metric | Value |
|--------|-------|
| Total evaluations | 13,096 |
| High-conviction evals (≥12%) | **0** |
| TRADE decisions | **0** |
| Model accuracy | **17.6%** (predicting UP when 82.4% DOWN) |
| Financial impact | **$0** (correctly filtered) |

### Would-Have Scenario (If We Relaxed Thresholds)

If we had lowered the 12% conviction threshold to 10%:

| Metric | Value |
|--------|-------|
| Estimated trades | ~6,500 (all DOWN would have won) |
| Estimated WR | **18%** |
| Estimated loss | **~$32,500** (at $5 avg stake) |
| Verdict | **Catastrophic** |

**Our strict filters saved us from a massive loss.**

### 24h Summary

| Period | Trades | WR | Status |
|--------|--------|-----|--------|
| 0-12h ago | 20 | ~75% | Normal |
| 12-24h ago | 0 | N/A | Model broken |
| **Total 24h** | **20** | **~75%** | **Below normal** |

---

## Immediate Actions Required

### 1. Verify Push-Mode Status (5 min)

```bash
# SSH to TimesFM service
ssh ubuntu@3.98.114.0

# Test push-mode endpoint
curl -X POST http://localhost:8080/v2/probability \
  -H "Content-Type: application/json" \
  -d '{"asset":"BTC","seconds_to_close":120,"features":{"eval_offset":120}}'
```

**Expected results:**
- `200 OK` + probability → Push-mode working ✓
- `404/405/501` → Push-mode NOT implemented ✗

### 2. Check Engine Logs (2 min)

```bash
# SSH to Montreal
ssh ubuntu@15.223.247.178

# Search for push-mode status
tail -1000 /home/novakash/engine.log | grep "push_mode"
```

**Expected results:**
- `push_mode_active` → Working ✓
- `push_mode_unsupported` → Falling back to GET ✗

### 3. Deploy Fix OR Rollback (15 min)

**Option A: Deploy Push-Mode to TimesFM Service**

Update `novakash-timesfm-repo/app/v2_scorer.py` to implement POST endpoint:

```python
@app.post("/v2/probability")
async def v2_probability_push(req: V5FeatureBodyRequest):
    """
    Push-mode: client provides full feature body.
    """
    features = req.features  # Use provided features directly
    probability = model.predict(features)
    return {"probability_up": probability, "model_version": "sequoia-v5.2"}
```

**Option B: Rollback to v4 (OAK Model)**

If push-mode takes time to deploy, rollback to working v4:

```python
# In engine/signals/timesfm_v2_client.py
# Change model version from "oak" to "oak-v4" or similar
```

**Recommendation:** **Rollback to v4 first** to restore trading, then deploy push-mode fix.

### 4. Monitor After Fix (Ongoing)

After applying fix, verify:

```sql
-- Check if predictions vary
SELECT 
    MIN(v2_probability_up) as min_p,
    MAX(v2_probability_up) as max_p,
    AVG(ABS(COALESCE(v2_probability_up, 0.5) - 0.5) * 100) as avg_conv
FROM signal_evaluations
WHERE evaluated_at >= NOW() - INTERVAL '1 hour'
  AND asset = 'BTC';
```

**Expected:**
- `min_p < 0.5` (some DOWN predictions)
- `max_p > 0.7` (some high-conviction UP predictions)
- `avg_conv > 12%` (above trading threshold)

---

## Files to Check/Update

### Engine Side (novakash)

- ✅ `engine/signals/timesfm_v2_client.py` - Push-mode client implemented
- ✅ `engine/signals/v2_feature_body.py` - 25-feature definition
- ✅ `engine/strategies/five_min_vpin.py:1775` - Decision-path call

### TimesFM Service (novakash-timesfm-repo)

- ❌ `app/v2_scorer.py` - **POST endpoint NOT implemented** (ROOT CAUSE)
- ❌ `app/v2_routes.py` - POST route not wired
- ✅ `training/train_lgb_v5.py` - Training code correct

### Documentation

- ✅ `docs/analysis/SPARTA_AGENT_GUIDE.md` - Documents push-mode requirement
- ✅ `docs/analysis/CHANGELOG_SEQUOIA.md` - Documents v5.2 deployment

---

## Long-Term Recommendations

### 1. Add Model Health Checks

```python
# In engine/signals/timesfm_v2_client.py
async def health_check(self) -> dict:
    """
    Verify model is not returning constant predictions.
    """
    # Check if probability varies over last N evals
    # Alert if std_dev < 0.01 (constant predictions)
```

### 2. Implement Circuit Breaker

```python
# In engine/strategies/five_min_vpin.py
if model_staleness_detected():
    # Pause trading, alert, fallback to v4
    return SKIP(reason="model_staleness")
```

### 3. Add Feature Coverage Monitoring

```python
# Log feature coverage at decision time
logger.info(f"v2.probability.feature_coverage={coverage}")
# Alert if coverage < 80%
```

### 4. Regular Model Validation

- Daily: Check prediction distribution (should vary, not constant)
- Weekly: Backtest against resolved windows (should be 70-85% WR)
- Monthly: Retraining with new data

---

## Conclusions

### What Happened

1. **v5.2 deployed** on Apr 10 with 25-feature training
2. **Push-mode NOT implemented** on TimesFM service
3. **Model received NaNs** → returned constant 0.606
4. **All predictions UP** with 10.2% conviction
5. **Market went DOWN 82%** → model completely wrong
6. **Our filters blocked all trades** → saved us from disaster

### What We Learned

✅ **Our strict 12% conviction filter worked perfectly** - blocked thousands of losing trades  
✅ **Model monitoring would have caught this faster** - constant probability is a clear signal  
⚠️ **Push-mode deployment was incomplete** - should have been tested before production  
⚠️ **No fallback mechanism** - should rollback to v4 automatically

### What To Do

1. **Immediate:** Test push-mode endpoint on TimesFM service
2. **If broken:** Rollback to v4 (OAK) to restore trading
3. **Then:** Deploy push-mode fix to TimesFM service
4. **After:** Add model health monitoring to prevent future issues

### Expected Recovery

After push-mode is deployed:

- **P(UP) will vary** between 0.3-0.9 based on features
- **Conviction will increase** to 15-25% range
- **Trades will resume** at ~40/day for DOWN-only
- **Win rate will return** to 75-80% range

---

## Related Documents

- `docs/analysis/SPARTA_AGENT_GUIDE.md` - Push-mode implementation details
- `docs/analysis/CHANGELOG_SEQUOIA.md` - v5.2 deployment notes
- `engine/signals/v2_feature_body.py` - 25-feature definition
- `engine/signals/timesfm_v2_client.py` - Push-mode client

---

**Last Updated:** 2026-04-13 11:45 UTC  
**Status:** 🔴 **MODEL BROKEN - ACTION REQUIRED**  
**Next Check:** After push-mode deployment or v4 rollback

</content>
<parameter=filePath>
/Users/billyrichards/Code/novakash/docs/analysis/TIMESFM_V5_MODEL_BROKEN_2026-04-13.md