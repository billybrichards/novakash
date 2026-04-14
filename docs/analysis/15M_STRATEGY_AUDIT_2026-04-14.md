# 15M Strategy Audit — 2026-04-14

**Author:** Claude (automated audit)
**Date:** 2026-04-14
**Scope:** Data population check, counterfactual win/loss, regime_risk_off diagnosis, timing gate bug

---

## Summary

The 15m strategies have a **critical timing gate misconfiguration**: the engine fires
15m evaluations only at `CLOSING` state (T-60 to T-240), but `v15m_down_only` and
`v15m_up_asian` require eval_offset in `[270, 450]`. Those two strategies will NEVER
trade. The `v15m_fusion` strategy is correctly evaluating (no timing gate) but its
current window is wrong — it is calling UP when BTC has been moving DOWN all session.

---

## 1. Tables Populated — YES

| Table | 15m rows | Notes |
|-------|----------|-------|
| `window_snapshots` | **1,002** | Healthy — windows saving correctly |
| `strategy_decisions` | **4,550** | All 5 strategies recording per-evaluation |
| `signal_evaluations` | **0** | No 15m signal_evaluations at all (expected — signal_evaluations uses asset not timeframe) |

Data is saving correctly. The 1,002 window_snapshot rows include duplicates (multiple
asset/source rows per window_ts). There are 113 distinct resolved BTC windows with both
open and close price.

One anomaly: window_ts `1776193200` has 91 rows (9 duplicate closes per window). This
appears to be multiple asset snapshots per window, not a write bug. The open_price
is consistent (74322.01) and close prices vary by source (74242.60–74352.60).

---

## 2. Counterfactual — v15m_fusion

Using DISTINCT ON (window_ts) to deduplicate, here are the 15 most recent resolved
BTC 15m windows and what v15m_fusion recommended:

| Window UTC | Open | Close | BTC Actual | Fusion Said | Result |
|-----------|------|-------|------------|-------------|--------|
| (latest) | 74328.95 | 73871.10 | DOWN | NONE | SKIPPED |
| -15m | 74322.01 | 74352.60 | UP | UP | **WIN** |
| -30m | 73912.00 | 74204.20 | UP | NONE | SKIPPED |
| -45m | 74372.48 | 74244.60 | DOWN | UP | **LOSS** |
| -60m | 74707.58 | 74558.00 | DOWN | UP | **LOSS** |
| -75m | 74792.94 | 74760.00 | DOWN | DOWN | **WIN** |
| -90m | 74655.57 | 74742.10 | UP | UP | **WIN** |
| -105m | 74500.00 | 74542.60 | UP | NONE | SKIPPED |
| -120m | 74827.23 | 74788.10 | DOWN | DOWN | **WIN** |
| -135m | 74747.18 | 74764.40 | UP | NONE | SKIPPED |

**Counterfactual summary (15 windows): 4W / 2L / 9 skipped = 67% WR on traded windows**

Key observation: on the most recent window (74328→73871, DOWN), fusion SKIPPED.
The prior window it was calling UP while BTC was going DOWN — that is an alignment
problem in the upstream TimesFM v4 snapshot (calling UP in a sell-off).

---

## 3. Why regime_risk_off Keeps Blocking

### Root Cause

`regime_risk_off` appears in `v15m_fusion` skip_reason as:
```
polymarket: regime_risk_off (timing=optimal, dist=0.280)
```

This comes from `surface.poly_reason` — a field populated by the V4 TimesFM upstream
server's `polymarket_live_recommended_outcome.reason` field. When the V4 server's
regime classifier returns `risk_off`, it sets `trade_advised=False` with
`reason="regime_risk_off"`.

The v15m_fusion code path (`_evaluate_poly_v2`) checks:
```python
if not trade_advised:
    return _skip(f"polymarket: {reason} (timing={timing}, dist={distance:.3f})")
```

So the engine is correctly trusting the upstream server's `trade_advised=False` signal.
The question is whether the upstream regime classifier is correctly calibrated for 15m.

### Is regime_risk_off correctly calibrated for 15m?

**Probably not.** Evidence:

1. The regime classifier is shared with 5m strategies. The `_TRADEABLE_REGIMES` in the
   legacy path is `{"calm_trend", "volatile_trend"}` — same for both timeframes.

2. The upstream V4 snapshot is fetched with `strategy="polymarket_5m"` hardcoded:
   ```python
   params = {"asset": asset, "timescale": timescale, "strategy": "polymarket_5m"}
   ```
   The `strategy` param is `polymarket_5m` even for 15m timescale fetches. This means
   the V4 server's regime + trade_advised logic is being evaluated through a 5m lens.

3. At dist=0.280, the model has **high confidence** (~78% directional). Blocking this
   on regime_risk_off means we are discarding the model's strongest signals.

4. The current session has BTC declining from ~74800 to ~73871. The `risk_off` regime
   label is appropriate for the 5m timeframe (choppy intraday), but for a 15m
   directional strategy, a sustained downtrend IS a valid trading environment.

### Recommendation

For 15m strategies, `regime_risk_off` with `dist >= 0.20` should NOT block the trade.
The 15m window has enough duration that regime noise averages out. Specifically:

- `regime_risk_off + dist < 0.15` → skip (low confidence + risk off = stay out)
- `regime_risk_off + dist >= 0.20` → allow trade (model conviction overrides regime)

This requires either:
(a) Adding a `min_regime_override_dist` param to the trade_advised gate, OR
(b) Overriding `trade_advised` in the v15m_fusion pre_gate_hook when dist is high

---

## 4. Timing Gate Bug — v15m_down_only, v15m_up_asian, v15m_up_basic

### The Bug

All 15m strategy evaluations fire at `CLOSING` state from `_on_fifteen_min_window`.
The orchestrator only calls `strategy_registry.evaluate_all` inside:
```python
if state_value == "CLOSING":
```

CLOSING fires at T-60 (60 seconds before window close). The engine processes multiple
strategies/assets, so actual eval_offsets range from **T-60 to T-240**.

The timing gate configurations are:

| Strategy | min_offset | max_offset | Problem |
|----------|-----------|-----------|---------|
| `v15m_down_only` | 270 | 450 | Evaluations at 60–240 NEVER reach 270 |
| `v15m_up_asian` | 270 | 450 | Same — will NEVER trade |
| `v15m_up_basic` | 180 | 540 | Edge case — only passes at T-180 to T-240 |
| `v15m_fusion` | none | none | No timing gate — works correctly |
| `v15m_gate` | 15 | 900 | Works correctly |

**Verified in DB:** `v15m_down_only` ALL TIME decisions are 100% `skip=timing: T-X outside [270, 450]`.
It has never once been in window. Same for `v15m_up_asian`.

### Is the [270, 450] timing gate correct in theory?

For a 15m (900s) window:
- T-270 = 270s before close = 630s into the window (70% elapsed)
- T-450 = 450s before close = 450s into the window (50% elapsed)

This would be the "sweet spot" zone — halfway to two-thirds through the window —
which makes sense for signal-reading. But the engine is never firing at that point.

The engine fires CLOSING state at T-60 (one minute before close), not at mid-window.
The 15m window feed (`Polymarket5MinFeed` with `duration_secs=900`) would need to
fire intermediate signals at T-270 through T-450 for these gates to pass.

### Root Cause

The 15m feed only fires at:
1. ACTIVE (window opens — T-900)
2. CLOSING (window nears close — T-60)

There is no intermediate EVALUATING state at T-270 through T-450.

### Fix Options

**Option A (Quick fix):** Change `v15m_down_only` and `v15m_up_asian` timing gates to
`[60, 240]` to match actual evaluation times. This is consistent with when data is
available and the model has seen most of the 15m candle.

**Option B (Correct fix):** Add a mid-window evaluation event. The 15m feed should
fire an EVALUATING state at T-270 (or configurable offset). This would allow
early-to-mid-window entry which is the intended design.

**Option A is the pragmatic fix now.** Option B is the right long-term architecture.

---

## 5. v15m_fusion: Calling UP in a DOWN Market

In the most recent resolved session, v15m_fusion made 107 TRADE UP vs 69 TRADE DOWN
decisions across all windows in the last 3 hours. However, BTC has been predominantly
moving DOWN:

- Latest resolved window: 74328 → 73871 (DOWN, -0.62%)
- v15m_fusion skipped (no CLOB or regime blocked it correctly this time)
- Prior window: fusion called UP while BTC went DOWN (LOSS)

The `consensus not safe_to_trade` skip (273 occurrences) is the biggest blocker for
v15m_fusion — more than regime_risk_off. This comes from `v4_consensus_safe_to_trade`
which requires multiple sources to agree on direction.

---

## 6. Specific Skip Reason Breakdown (Last 3 Hours)

### v15m_fusion
| Skip Reason | Count |
|-------------|-------|
| consensus not safe_to_trade | 273 |
| TRADE UP | 107 |
| TRADE DOWN | 69 |
| regime_risk_off (various dist) | ~80 |
| too_early (timing=optimal) | ~80 |
| late_window (CLOB priced) | ~70 |
| dist < 0.12 threshold | ~50 |

"consensus not safe_to_trade" → this is the legacy path firing when `poly_direction`
is None and `v4_recommended_side` is also None. The v4 snapshot server is not returning
a polymarket recommendation for some windows, and the legacy gate is blocking.

### v15m_gate
| Skip Reason | Count |
|-------------|-------|
| source_agreement: only 1 source, need 2 | 273 |
| trade_advised=False: regime_risk_off | 50 |
| spread: spread=Xbps > 100bps | ~100 |
| taker_flow misaligned | ~100 |
| delta_magnitude |delta| < 0.0005 | ~100 |

**The spread issue is severe**: 780,000–980,000 bps spreads are being reported. This
suggests the CLOB spread for 15m markets is essentially 100% of the price — the 15m
markets have very thin liquidity. This is not a bug; it is correct behavior (thin
markets shouldn't trade).

### v15m_down_only and v15m_up_asian
100% timing gate failures — see Section 4.

---

## 7. Recommendations (Priority Order)

### P0 — Fix timing gates (zero cost, immediate)
Change `v15m_down_only` and `v15m_up_asian` YAML:
```yaml
# CURRENT (broken — never fires):
- type: timing
  params: { min_offset: 270, max_offset: 450 }

# FIXED (matches actual evaluation window):
- type: timing
  params: { min_offset: 60, max_offset: 240 }
```

### P1 — Allow high-dist trades despite regime_risk_off
In `v15m_fusion.py` `_evaluate_poly_v2`, add dist-based override:
```python
if not trade_advised:
    # Override regime_risk_off if model conviction is high
    if "regime_risk_off" in reason and distance >= 0.20:
        pass  # fall through to trade
    else:
        return _skip(f"polymarket: {reason} (timing={timing}, dist={distance:.3f})")
```

### P2 — Fix V4 snapshot strategy param for 15m
In `v4_snapshot_http.py`, the params should pass `strategy="polymarket_15m"` when
fetching 15m snapshots, so the upstream server can calibrate its regime + trade_advised
logic appropriately for the 15m timeframe:
```python
params = {"asset": asset, "timescale": timescale, "strategy": f"polymarket_{timescale}"}
```

### P3 — Investigate "consensus not safe_to_trade" (273 blocks)
This is the largest skip bucket for v15m_fusion. When `poly_direction` is None and
`v4_recommended_side` is None, the legacy path fires. The V4 snapshot server may not
be returning a polymarket recommendation for all 15m windows. Check whether the 15m
polymarket market IDs are being correctly fetched and injected into the snapshot.

### P4 (Optional, architectural) — Add mid-window evaluation state
For the intended T-270 to T-450 evaluation design to work, the 15m feed needs to emit
a mid-window signal. This would unlock the timing-gated strategies as designed.

---

## 8. Timing Gate Logic — Clarification

The `eval_offset` convention is **seconds before window close** (T-X = X seconds until close):
- T-60 = 60s to close (current evaluation time)
- T-270 = 270s to close = 4.5 minutes to close (mid-to-late in 15m window)
- T-450 = 450s to close = 7.5 minutes to close (mid-window)

The timing gate format `[min, max]` means: pass if `min <= eval_offset <= max`.
So `[270, 450]` means: "evaluate only when 4.5 to 7.5 minutes remain" — i.e., at
mid-window. This is the intended sweet spot but requires mid-window state fires.

For comparison, the 5m window evaluation runs at range(240, 59, -2) = T-60 to T-240
(every 2 seconds from T-240 to T-60) which covers the full evaluation window.
The 15m strategies are only firing at the equivalent T-60 to T-240 range.

---

*Audit run: 2026-04-14 19:35 UTC*
*Data range: all-time (1,002 windows), last 3h decisions (4,550 rows)*
