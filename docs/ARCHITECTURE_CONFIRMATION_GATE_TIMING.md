# Architecture Confirmation: Gate Timing vs Strategy Timing

## Executive Summary

✅ **The architecture IS clean and follows proper patterns**

✅ **Different strategies CAN have different timing windows**

✅ **The current behavior is CORRECT** - v4_down_only is working as designed

## The Architecture

### Two Separate Timing Controls

1. **Gate Timing (EvalOffsetBoundsGate)**
   - Location: `engine/signals/gates.py`
   - Config: `V10_6_MAX_EVAL_OFFSET` (env var)
   - Default: 180
   - Server: 120
   - **Applies to:** `v10_gate` strategy only

2. **Strategy Timing (V4DownOnlyStrategy)**
   - Location: `engine/adapters/strategies/v4_down_only_strategy.py`
   - Config: `_MAX_EVAL_OFFSET = 150` (hardcoded)
   - **Applies to:** `v4_down_only` strategy only

### Clean Separation

```
┌─────────────────────────────────────────────────────────┐
│  Engine Evaluates Window (T-240 → T-0)                 │
└─────────────────────────────────────────────────────────┘
                           │
           ┌───────────────┴───────────────┐
           │                               │
           ▼                               ▼
┌──────────────────┐          ┌──────────────────────┐
│ v10_gate         │          │ v4_down_only         │
│ Strategy         │          │ Strategy             │
├──────────────────┤          ├──────────────────────┤
│ V10 Gate Pipeline│          │ V4 Fusion +          │
│   - EvalOffset   │          │   Own Timing Check   │
│   - Source Agree │          │   _MIN=90            │
│   - Delta Magn   │          │   _MAX=150           │
│   - ...          │          │                      │
├──────────────────┤          ├──────────────────────┤
│ Timing: T-90-120 │          │ Timing: T-90-150     │
│ (from env var)   │          │ (hardcoded)          │
└──────────────────┘          └──────────────────────┘
```

## Current Behavior (CORRECT)

```
eval_offset=240: v4_down_only SKIP (too early, needs T-150+)
eval_offset=220: v4_down_only SKIP (too early)
eval_offset=200: v4_down_only SKIP (too early)
eval_offset=180: v4_down_only SKIP (too early)
eval_offset=150: v4_down_only CAN TRADE ✓
eval_offset=120: v4_down_only CAN TRADE ✓
eval_offset=90:  v4_down_only CAN TRADE ✓
eval_offset=60:  v4_down_only SKIP (too late)
```

**You're currently at T-240, T-220, etc.** → v4_down_only correctly SKIPs

**In ~1.5 minutes it will reach T-150** → v4_down_only can TRADE

## What's Actually Happening

The logs show:
```
v4_down_only: SKIP polymarket: timing=early — outside window
```

This is **v4_down_only's own timing check**, NOT the V10.6 gate!

The message format is different:
- V10.6 gate: `"V10.6: too early (T-240 > T-120)"`
- v4_down_only: `"polymarket: timing=early — outside window"`

## Architecture Score: 8/10 ✅

**Strengths:**
- ✅ Clean separation of concerns
- ✅ StrategyPort adapter pattern
- ✅ Independent timing per strategy
- ✅ Multiple strategies can coexist

**Weaknesses:**
- ⚠️ Strategy timing hardcoded (not configurable via env)
- ⚠️ Multiple timing controls can be confusing
- ⚠️ No central documentation of timing windows

## Recommendation

**DO NOTHING** - The system is working correctly!

The engine will reach T-150 in ~1.5 minutes. At that point, v4_down_only will:
1. Pass its timing check (T-150 is in range)
2. Evaluate Polymarket conditions (p_up, distance threshold)
3. Check CLOB prices
4. Place trade if all conditions are met

**Monitor at T-150:**
```bash
# Connect to Montreal
echo "tail -100 /home/novakash/engine.log | grep 'v4_down_only'" | \
  aws ec2-instance-connect ssh --instance-id i-0785ed930423ae9fd --region ca-central-1 --os-user novakash
```

If v4_down_only doesn't trade at T-150, investigate:
1. Polymarket price conditions
2. CLOB ask price gates  
3. Signal conditions (VPIN, delta, etc.)

## Files to Review

- `engine/signals/gates.py:191` - EvalOffsetBoundsGate (V10.6)
- `engine/adapters/strategies/v4_down_only_strategy.py:46` - V4 timing
- `engine/adapters/strategies/v10_gate_strategy.py:53` - V10 gate pipeline
- `docs/CLEAN_ARCHITECTURE_GATE_TIMING_ANALYSIS.md` - Full analysis
- `docs/MONTREAL_ENGINE_CONNECTION.md` - Server connection guide

---

**Conclusion:** Your architecture is sound. The system is working as designed. The skips you're seeing are correct behavior - v4_down_only is waiting for its T-150 window.
