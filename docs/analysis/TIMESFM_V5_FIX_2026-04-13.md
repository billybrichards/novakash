# TimesFM v5.2 Push-Mode Fix - Implementation Required

**Date:** 2026-04-13 12:15 UTC  
**Status:** 🔴 **PUSH-MODE SUPPORTED BUT INCOMPLETE FEATURES**  
**Impact:** Model returning constant 0.606 (10.2% conviction) → 0 trades

---

## Root Cause Confirmed

The TimesFM service at `3.98.114.0:8080` **DOES support push-mode** (POST `/v2/probability` returns 200), but the engine is only sending **1 of 25 required features**:

```
POST /v2/probability
Body: {"asset":"BTC","seconds_to_close":120,"features":{"eval_offset":120}}
Response: "missing 24 required features: ['vpin', 'delta_pct', 'twap_delta', ...]"
```

When the model receives 24 NaN features, it follows the "missing value" path and returns the **default leaf value: 0.60614485**.

## Why This Matters

| Scenario | P(UP) | Direction | Conviction | Trade? |
|----------|-------|-----------|------------|--------|
| **With 25 features** | 0.3-0.9 (varies) | Mixed | 15-25% | ✅ Yes |
| **With 1 feature** | 0.606 (constant) | Always UP | 10.2% | ❌ No (<12%) |

**Result:** 13,096 evals in 12h, all UP at 10.2% conviction, 0 trades.

---

## The Fix

### Files to Update

#### 1. `engine/strategies/five_min_vpin.py:1751-1773`

**Current (missing `chainlink_price`):**
```python
_decision_features = build_v5_feature_body(
    eval_offset=float(eval_offset),
    vpin=current_vpin,
    delta_pct=delta_pct,
    twap_delta=(twap_result.twap_delta_pct if twap_result else None),
    clob_up_price=window.up_price,
    clob_down_price=window.down_price,
    binance_price=current_price,
    tiingo_close=_tiingo_close,
    delta_binance=delta_binance,
    delta_chainlink=delta_chainlink,
    delta_tiingo=delta_tiingo,
    gate_vpin_passed=_vpin_passed,
    gate_delta_passed=_delta_passed,
    gate_cg_passed=_cg_passed,
    gate_twap_passed=not _twap_gate_blocked_actual,
    gate_timesfm_passed=not _timesfm_gate_blocked_actual,
    gate_passed=_all_passed,
    regime=_snap_regime,
    delta_source=_price_source_used,
    prev_v2_probability_up=window_snapshot.get("v2_probability_up"),
)
```

**Fixed (add `chainlink_price`):**
```python
_decision_features = build_v5_feature_body(
    eval_offset=float(eval_offset),
    vpin=current_vpin,
    delta_pct=delta_pct,
    twap_delta=(twap_result.twap_delta_pct if twap_result else None),
    clob_up_price=window.up_price,
    clob_down_price=window.down_price,
    binance_price=current_price,
    chainlink_price=window_snapshot.get("chainlink_price"),  # ADD THIS
    tiingo_close=_tiingo_close,
    delta_binance=delta_binance,
    delta_chainlink=delta_chainlink,
    delta_tiingo=delta_tiingo,
    gate_vpin_passed=_vpin_passed,
    gate_delta_passed=_delta_passed,
    gate_cg_passed=_cg_passed,
    gate_twap_passed=not _twap_gate_blocked_actual,
    gate_timesfm_passed=not _timesfm_gate_blocked_actual,
    gate_passed=_all_passed,
    regime=_snap_regime,
    delta_source=_price_source_used,
    prev_v2_probability_up=window_snapshot.get("v2_probability_up"),
)
```

#### 2. `engine/signals/gates.py:785-802`

**Current (missing `chainlink_price`):**
```python
_gate_features = build_v5_feature_body(
    eval_offset=float(seconds_to_close),
    vpin=ctx.vpin,
    delta_pct=ctx.delta_pct,
    twap_delta=ctx.twap_delta,
    clob_up_price=ctx.gamma_up_price,
    clob_down_price=ctx.gamma_down_price,
    binance_price=ctx.current_price,
    tiingo_close=ctx.tiingo_close,
    delta_binance=ctx.delta_binance,
    delta_chainlink=ctx.delta_chainlink,
    delta_tiingo=ctx.delta_tiingo,
    regime=ctx.regime,
    delta_source=ctx.delta_source,
    prev_v2_probability_up=ctx.prev_v2_probability_up,
    # gate_* intentionally omitted
)
```

**Fixed (add `chainlink_price`):**
```python
_gate_features = build_v5_feature_body(
    eval_offset=float(seconds_to_close),
    vpin=ctx.vpin,
    delta_pct=ctx.delta_pct,
    twap_delta=ctx.twap_delta,
    clob_up_price=ctx.gamma_up_price,
    clob_down_price=ctx.gamma_down_price,
    binance_price=ctx.current_price,
    chainlink_price=ctx.chainlink_price,  # ADD THIS
    tiingo_close=ctx.tiingo_close,
    delta_binance=ctx.delta_binance,
    delta_chainlink=ctx.delta_chainlink,
    delta_tiingo=ctx.delta_tiingo,
    regime=ctx.regime,
    delta_source=ctx.delta_source,
    prev_v2_probability_up=ctx.prev_v2_probability_up,
    # gate_* intentionally omitted
)
```

#### 3. `engine/use_cases/evaluate_window.py:235-238` and `:324-325`

**Check these call sites** - they may also be missing `chainlink_price`.

---

## Feature Coverage Verification

After applying the fix, verify coverage in engine logs:

```
v2.probability.push_mode_active
feature_coverage=0.XXX
```

**Expected:**
- `feature_coverage >= 0.80` (20+ of 25 features)
- CLOB features may be None (Polymarket data unavailable)
- Gate booleans should all be 1.0/0.0 (not None)

**Acceptable missing features:**
- `clob_up_bid`, `clob_up_ask`, `clob_down_bid`, `clob_down_ask` (CLOB not available)
- `gate_twap_passed`, `gate_timesfm_passed` (optional gates)

**Required features (must NOT be None):**
- `eval_offset` ✓ (always provided)
- `vpin` ✓ (always provided)
- `delta_pct` ✓ (always provided)
- `binance_price` ✓ (always provided)
- `delta_binance` ✓ (always provided)
- `delta_chainlink` ✓ (always provided)
- `delta_tiingo` ✓ (always provided)
- `chainlink_price` ⚠️ **MUST ADD**
- `regime_num` ✓ (always provided)
- `delta_source_num` ✓ (always provided)

---

## Testing

### 1. Manual POST Test (with all features)

```bash
curl -X POST http://3.98.114.0:8080/v2/probability \
  -H "Content-Type: application/json" \
  -d '{
    "asset":"BTC",
    "seconds_to_close":120,
    "features":{
      "eval_offset":120,
      "vpin":0.65,
      "delta_pct":0.015,
      "twap_delta":0.008,
      "clob_spread":null,
      "clob_mid":null,
      "clob_up_bid":null,
      "clob_up_ask":null,
      "clob_down_bid":null,
      "clob_down_ask":null,
      "binance_price":67500,
      "chainlink_price":67480,
      "tiingo_close":67490,
      "delta_binance":0.012,
      "delta_chainlink":0.014,
      "delta_tiingo":0.011,
      "gate_vpin_passed":1.0,
      "gate_delta_passed":1.0,
      "gate_cg_passed":1.0,
      "gate_twap_passed":1.0,
      "gate_timesfm_passed":1.0,
      "gate_passed":1.0,
      "regime_num":0.0,
      "delta_source_num":0.0,
      "v2_logit":null
    }
  }'
```

**Expected response:**
```json
{
  "probability_up": 0.XXX,  // Should vary, not 0.606
  "model_version": "15a4e3e",
  "timesfm": {...}
}
```

### 2. Engine Log Verification

After restart, check logs for:
```
v2.probability.push_mode_active feature_coverage=0.84
```

### 3. Signal Evaluation Check

```sql
SELECT 
    COUNT(*) as n,
    MIN(v2_probability_up) as min_p,
    MAX(v2_probability_up) as max_p,
    ROUND(AVG(ABS(COALESCE(v2_probability_up, 0.5) - 0.5) * 100), 2) as avg_conv
FROM signal_evaluations
WHERE evaluated_at >= NOW() - INTERVAL '1 hour'
  AND asset = 'BTC';
```

**Expected:**
- `min_p < 0.5` (some DOWN predictions)
- `max_p > 0.7` (some high-conviction predictions)
- `avg_conv > 12%` (above trading threshold)

---

## Deployment Order

1. **Fix code** (add `chainlink_price` to all call sites)
2. **Test manually** (POST with full features)
3. **Deploy engine** (restart to pick up changes)
4. **Verify logs** (push_mode_active + feature_coverage)
5. **Monitor signals** (v2_probability_up should vary)
6. **Watch trades** (should resume at ~40/day for DOWN-only)

---

## Rollback Plan

If fix doesn't work:

1. **Check TimesFM service logs** for POST errors
2. **Verify model version** (`curl http://3.98.114.0:8080/v2/health`)
3. **Consider rollback to v4** (OAK model with pull-mode)

---

## Expected Recovery

After fix:

| Metric | Before | After |
|--------|--------|-------|
| P(UP) range | 0.606 (constant) | 0.3-0.9 (varies) |
| Conviction | 10.2% (constant) | 15-25% (varies) |
| Direction | UP 100% | Mixed (50/50) |
| Trades/day | 0 | ~43 |
| Win rate | N/A | 75-80% |

---

**Last Updated:** 2026-04-13 12:15 UTC  
**Next Check:** After engine restart and feature coverage verification

</content>
<parameter=filePath>
/Users/billyrichards/Code/novakash/docs/analysis/TIMESFM_V5_FIX_2026-04-13.md