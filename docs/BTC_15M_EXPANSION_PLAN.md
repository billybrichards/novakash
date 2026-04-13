# BTC 15-Minute Trading Expansion Plan

**Date:** 2026-04-13
**Status:** Planning — no code changes yet
**Depends on:** Strategy Engine v2 (live), v3 composite scorer (live), 15m feed (exists, untested with registry)

---

## Executive Summary

The BTC 5m clean architecture (5 strategies, 14 gates, registry, data surface, execution UC) is production-ready. Expanding to 15m requires fixing 5 hardcoded `"5m"` references, creating 5 new YAML strategy configs with scaled timing gates, and adding a timescale filter to the registry. Most infrastructure already exists — Polymarket 15m feed, 15m model training slot, v4 snapshot 15m support.

**Estimated effort:** 8-10 hours across 4 phases.

---

## Current 5m Architecture

### Strategies

| Strategy | Direction | Mode | Timing Gate | Key Feature |
|----------|-----------|------|-------------|-------------|
| v4_down_only | DOWN | LIVE | T-90 to T-150 | CLOB sizing from 897K sample |
| v4_up_asian | UP | GHOST | T-90 to T-150 | Asian session (23:00-02:59 UTC) |
| v4_up_basic | UP | GHOST | T-60 to T-180 | Wide window, validation |
| v4_fusion | Both | GHOST | Custom hook | 3 eval paths (poly_v2, poly_legacy, legacy) |
| v10_gate | Both | GHOST | T-5 to T-300 | 8-gate DUNE pipeline |

### Pipeline Flow

```
Polymarket5MinFeed (duration=300s)
  → window signal at eval offsets (T-240 → T-60, every 2s)
  → orchestrator._on_five_min_window
  → _execution_queue
  → StrategyRegistry.evaluate_all(window, state)
  → DataSurfaceManager.get_surface() ← HARDCODES "5m"
  → each strategy's gate pipeline
  → LIVE + TRADE → ExecuteTradeUseCase
  → strategy_decisions table
```

---

## What Exists for 15m Already

| Component | Status | Notes |
|-----------|--------|-------|
| Polymarket 15m feed | ✅ Working | `Polymarket5MinFeed(duration_secs=900)`, correct slug generation |
| `_on_fifteen_min_window` handler | ✅ Exists | Orchestrator lines 1863-1914 |
| `FIFTEEN_MIN_ENABLED` env var | ✅ Wired | + `FIFTEEN_MIN_ASSETS` |
| V4 snapshot 15m support | ✅ Ready | `v4_snapshot_assembler` supports `timescale="15m"` |
| 15m model registry | ✅ Loaded | `_v2_15m_registry`, `_v2_15m_scorer` in main.py |
| 15m training pipeline | ✅ Ready | `DELTA_BUCKETS_BY_TIMEFRAME["15m"] = (60, 120, 180, 300, 480, 720)` |
| Retrain matrix | ✅ Included | `timeframe: "15m"` in `retrain.yml` |
| `strategy_decisions.timeframe` column | ✅ VARCHAR | No migration needed |
| `FullDataSurface.timescale` field | ✅ Exists | Currently always set to `"5m"` |

---

## 5 Critical Blockers (Hardcoded "5m")

These must be fixed before 15m strategies evaluate correctly:

### B1. `DataSurfaceManager._fetch_v4()` — line 253
```python
# CURRENT (broken for 15m):
params = {"asset": "BTC", "timescale": "5m", "strategy": "polymarket_5m"}

# FIX: fetch both timeframes
for tf in ["5m", "15m"]:
    params = {"asset": "BTC", "timescale": tf, "strategy": "polymarket_5m"}
    self._cached_v4[tf] = await resp.json()
```

### B2. `DataSurfaceManager.get_surface()` — line 358
```python
# CURRENT:
ts_data = (v4.get("timescales") or {}).get("5m", {})
timescale="5m"  # line 388

# FIX:
window_tf = "15m" if getattr(window, "duration_secs", 300) == 900 else "5m"
ts_data = (v4.get("timescales") or {}).get(window_tf, {})
timescale=window_tf
```

### B3. `StrategyRegistry.evaluate_all()` — no timescale filter
All 5m strategies will fire on 15m windows with wrong timing gates.
```python
# FIX: add filter
window_tf = "15m" if getattr(window, "duration_secs", 300) == 900 else "5m"
if config.timescale != window_tf:
    continue
```

### B4. Orchestrator `market_slug` — line 1736
```python
# CURRENT:
market_slug=f"{window.asset.lower()}-updown-5m-{window.window_ts}"

# FIX:
_tf = "15m" if window.duration_secs == 900 else "5m"
market_slug=f"{window.asset.lower()}-updown-{_tf}-{window.window_ts}"
```

### B5. `WindowInfo` has no `.timeframe` attribute
All 15m `strategy_decisions` records get stored as `timeframe='5m'`.
```python
# FIX: derive in registry
window_tf = "15m" if getattr(window, "duration_secs", 300) == 900 else "5m"
```

---

## Proposed 15m Strategy Set

### Timing Gate Scaling (3x for 3x window duration)

| Gate Context | 5m Value | 15m Value | Ratio |
|-------------|----------|-----------|-------|
| down/asian optimal | 90-150s | 270-450s | 3x |
| up_basic wide | 60-180s | 180-540s | 3x |
| v10-style full | 5-300s | 15-900s | 3x |
| fusion "early" | >180s | >540s | 3x |
| fusion "optimal" | 30-180s | 90-540s | 3x |
| fusion "late" | 5-30s | 15-90s | 3x |
| fusion "expired" | <5s | <15s | 3x |

### Non-Timing Gates (unchanged)

| Gate | Value | Why |
|------|-------|-----|
| delta_magnitude | 0.0005 | Absolute price move, not time-dependent |
| confidence | 0.10-0.12 | Model calibration independent of window |
| spread | 100bps | CLOB spread independent of window |
| session_hours | [23,0,1,2] | UTC hours don't change |

### 5 New Strategies

1. **v15m_down_only** — DOWN, GHOST, timing 270-450s, CLOB sizing hook
2. **v15m_up_asian** — UP, GHOST, timing 270-450s, Asian session
3. **v15m_up_basic** — UP, GHOST, timing 180-540s, global sessions
4. **v15m_fusion** — Both, GHOST, custom hook with rescaled timing bands
5. **v15m_gate** — Both, GHOST, 8-gate pipeline with 15m thresholds

All start GHOST. Promote to LIVE only after Billy reviews shadow performance.

---

## Implementation Phases

### Phase 1: Fix Blockers + Data Surface (2-3 hours)

| Task | File | Change |
|------|------|--------|
| B1 | `engine/strategies/data_surface.py` | Multi-timeframe `_fetch_v4()` |
| B2 | `engine/strategies/data_surface.py` | Timeframe-aware `get_surface()` |
| B3 | `engine/strategies/registry.py` | Add `timescale` filter in `evaluate_all()` |
| B4 | `engine/strategies/orchestrator.py` | Dynamic `market_slug` |
| B5 | `engine/strategies/registry.py` | Derive `timeframe` from `window.duration_secs` |
| B6 | `engine/strategies/registry.py` | Fix `_send_window_summary` hardcoded "5m" |

### Phase 2: Strategy Configs + Hooks (2-3 hours)

| Task | Files Created |
|------|--------------|
| 5 YAML configs | `engine/strategies/configs/v15m_*.yaml` |
| 3 Python hooks | `engine/strategies/configs/v15m_down_only.py`, `v15m_fusion.py`, `v15m_gate.py` |
| 5 Markdown specs | `engine/strategies/configs/v15m_*.md` |

### Phase 3: Signal Capture + Verification (1-2 hours)

| Task | Detail |
|------|--------|
| Deploy GHOST | `FIFTEEN_MIN_ENABLED=true`, all 5 strategies GHOST |
| Verify DB | Query `strategy_decisions WHERE timeframe='15m'` |
| Verify timing | Confirm `eval_offset` values in 270-450 range |
| Verify surface | Confirm `v2_probability_up` is non-null (15m model loaded) |

### Phase 4: Model Training + Go-Live (2+ hours + 7-day wait)

| Task | Detail |
|------|--------|
| Train 15m model | Trigger retrain with `timeframe=15m` if not already trained |
| Shadow period | 7 days minimum GHOST |
| Billy reviews | Dashboard comparison: 15m strategies vs 5m strategies |
| Promote first strategy | v15m_down_only → LIVE (if Billy approves) |

---

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| 15m Polymarket markets have different liquidity | Medium | CLOB sizing gate with conservative schedule |
| 15m model has less training data (3x fewer windows) | Medium | Extend lookback to 14d+, share CoinGlass/VPIN features |
| Timing gates miscalibrated for 15m | Low | Start GHOST, validate eval_offset distribution |
| 5m strategies accidentally firing on 15m windows | High | B3 fix (timescale filter) blocks this |
| 15m model not trained yet | Low | Retrain pipeline ready, manual trigger |

---

## Success Criteria

1. 5 GHOST strategies writing `strategy_decisions` with `timeframe='15m'`
2. `eval_offset` values in expected ranges per strategy
3. `v2_probability_up` non-null in 15m surface
4. No 5m strategy decisions contaminated by 15m windows (B3 verified)
5. After 7-day shadow: v15m_down_only WR > 55% on resolved windows
6. Billy explicitly approves first LIVE promotion
