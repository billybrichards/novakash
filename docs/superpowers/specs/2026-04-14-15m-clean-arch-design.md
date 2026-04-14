# 15-Minute BTC Strategy Expansion — Clean Architecture Design

**Date:** 2026-04-14  
**Branch:** `feature/15m-clean-arch`  
**Status:** Approved for implementation  
**Author:** clean-architect agent (via brainstorming session)

---

## 1. Goal

Expand the BTC prediction market trading engine from 5-minute to 15-minute Polymarket markets using the existing clean-architecture strategy registry. All 15m strategies start in GHOST mode (log decisions, never execute live trades).

---

## 2. What Already Exists (No Work Needed)

| Component | Status | Location |
|-----------|--------|----------|
| Polymarket 15m feed | ✅ Working | `engine/data/feeds/polymarket_5min.py` — `Polymarket5MinFeed(duration_secs=900)` |
| Orchestrator 15m handler | ✅ Exists | `engine/strategies/orchestrator.py` — `_on_fifteen_min_window()` |
| `FIFTEEN_MIN_ENABLED` env var | ✅ Wired | Set `FIFTEEN_MIN_ENABLED=true` on deploy |
| V4 snapshot 15m support | ✅ Ready | `/v4/snapshot` server returns `timescales["15m"]` block |
| 15m LightGBM model | ✅ Live | 427K predictions in `ticks_v2_probability` since Apr 8 |
| v3 composite scorer | ✅ Live | 330K rows at `timescale="15m"` since Apr 9 |
| `strategy_decisions.timeframe` column | ✅ VARCHAR | No migration needed |
| `ENGINE_USE_STRATEGY_REGISTRY=true` | ✅ Required | Must be set on deploy |

---

## 3. Architecture Decision: Option B — `window.timeframe` as Domain Property

### The Problem

The existing codebase derives timeframe in 5 different places via:
```python
"15m" if window.duration_secs == 900 else "5m"
```

This is an infrastructure detail (`duration_secs`) leaking into the strategy/use-case layers, duplicated across 5 files.

### The Solution

Add `timeframe` as a computed `@property` on `WindowInfo` — **one derivation site**, no duplication:

```python
# engine/data/feeds/polymarket_5min.py — add to WindowInfo dataclass
@property
def timeframe(self) -> str:
    """Semantic timeframe label derived from duration_secs."""
    return "15m" if self.duration_secs >= 900 else "5m"
```

All 5 downstream sites then read `window.timeframe` via `getattr(window, "timeframe", "5m")`. Zero breaking changes — the property is additive.

### Why Not Option A (patch 5 sites individually)?

Option A scatters `"15m" if window.duration_secs == 900 else "5m"` in 5+ locations. Five independent maintenance sites, all must stay in sync. Option B is both simpler and more elegant.

### Why Not Option C (multi-timeframe FullDataSurface)?

Over-engineering. Each strategy evaluates against one timeframe at a time. The surface already correctly handles this once `get_surface()` reads the right timescale key.

---

## 4. The 6 Blockers

### B1 — `data_surface.py:253` — `_fetch_v4()` only fetches 5m

**Current:**
```python
params = {"asset": "BTC", "timescale": "5m", "strategy": "polymarket_5m"}
self._cached_v4 = await resp.json()
```

**Fix (preferred — if server returns both timescales without filter):**
```python
params = {"asset": "BTC", "strategy": "polymarket_5m"}  # drop timescale filter
self._cached_v4 = await resp.json()
```

**Fix (fallback — if server requires timescale param):**
```python
async def _fetch_v4(self) -> None:
    if not self._session:
        return
    url = f"{self._v4_url}/v4/snapshot"
    try:
        # Fetch 5m (existing behavior)
        async with self._session.get(url, params={"asset": "BTC", "timescale": "5m", "strategy": "polymarket_5m"}) as resp:
            if resp.status == 200:
                self._cached_v4 = await resp.json()
                self._cached_v4_ts = time.time()
        # Fetch 15m — merge into cache
        async with self._session.get(url, params={"asset": "BTC", "timescale": "15m", "strategy": "polymarket_15m"}) as resp:
            if resp.status == 200:
                data_15m = await resp.json()
                ts_15m = (data_15m.get("timescales") or {}).get("15m")
                if ts_15m and self._cached_v4:
                    self._cached_v4.setdefault("timescales", {})["15m"] = ts_15m
    except Exception:
        pass  # Keep stale cache
```

**⚠️ Verify first:** Check whether dropping `timescale=5m` from the request still returns both timescale blocks. If yes, use the simple fix. If not, use the two-request fallback.

---

### B2 — `data_surface.py:368,397` — `get_surface()` hardcodes `"5m"`

**Current (line 368):**
```python
ts_data = (v4.get("timescales") or {}).get("5m", {})
```
**Current (line 397):**
```python
timescale="5m",
```

**Fix:**
```python
def get_surface(self, window: Any, eval_offset: Optional[int]) -> FullDataSurface:
    ...
    # Read timeframe from window (WindowInfo.timeframe property from Step 0)
    timeframe = getattr(window, "timeframe", "5m")
    ...
    # V4 snapshot: read correct timescale block
    v4 = self._cached_v4
    ts_data = {}
    if v4:
        ts_data = (v4.get("timescales") or {}).get(timeframe, {})
    ...
    return FullDataSurface(
        ...
        timescale=timeframe,  # was hardcoded "5m"
        ...
    )
```

Two targeted edits: line 368 and the `timescale=` kwarg in the `return FullDataSurface(...)` block.

---

### B3 — `registry.py:evaluate_all()` — No timescale filter (CRITICAL SAFETY GATE)

Without this fix, all 5m strategies fire on 15m windows with wrong timing gates. This is the highest-priority fix.

**Current (line 271):**
```python
for name, config in self._configs.items():
    if config.mode == "DISABLED":
        continue
    # all strategies fire on all windows
```

**Fix:**
```python
async def evaluate_all(self, window: Any, state: Any, ...) -> list[StrategyDecision]:
    ...
    window_tf = getattr(window, "timeframe", "5m")
    ...
    for name, config in self._configs.items():
        if config.mode == "DISABLED":
            continue
        # Only evaluate strategies matching the window's timeframe
        if config.timescale != window_tf:
            continue
```

Add `window_tf = getattr(window, "timeframe", "5m")` near the top of `evaluate_all()`, then add the 2-line guard inside the loop. The `StrategyConfig` already parses `timescale` from YAML (line 175).

---

### B4 — `orchestrator.py:1952` — `market_slug` hardcodes `"-5m-"`

**Current (line 1952):**
```python
market_slug=f"{window.asset.lower()}-updown-5m-{window.window_ts}",
```

**Fix:**
```python
_tf = getattr(window, "timeframe", "5m")
_v2_window_market = WindowMarket(
    condition_id=f"{window.asset}-{window.window_ts}",
    up_token_id=window.up_token_id,
    down_token_id=window.down_token_id,
    market_slug=f"{window.asset.lower()}-updown-{_tf}-{window.window_ts}",
)
```

---

### B5 — `registry.py` — `timeframe` in decision record always `"5m"`

**Current (line 293):**
```python
timeframe=getattr(window, "timeframe", "5m")
    if hasattr(window, "timeframe")
    else "5m",
```

**Status:** Already forward-compatible. After Step 0 adds `WindowInfo.timeframe` as a `@property`, `hasattr(window, "timeframe")` returns `True` and this code reads the correct value. **No change needed here** beyond Step 0.

---

### B6 — `orchestrator.py:2194` — `_on_fifteen_min_window` never calls strategy registry (CRITICAL GAP)

**This was NOT in the original handover doc.** The 15m CLOSING handler only queues to `_execution_queue` (the legacy `_five_min_strategy` path). The `StrategyRegistry.evaluate_all()` is only called from `_on_five_min_window`. Without this fix, the 5 new YAML configs will load but never evaluate.

**Fix — add to `_on_fifteen_min_window` at the CLOSING state block (line 2194):**
```python
if state_value == "CLOSING":
    # Strategy registry v2: evaluate all 15m GHOST strategies
    try:
        state = await self._aggregator.get_state()
        if self._strategy_registry:
            try:
                _v2_window_market = None
                if getattr(window, "up_token_id", None) and getattr(window, "down_token_id", None):
                    from domain.value_objects import WindowMarket
                    _tf = getattr(window, "timeframe", "15m")
                    _v2_window_market = WindowMarket(
                        condition_id=f"{window.asset}-{window.window_ts}",
                        up_token_id=window.up_token_id,
                        down_token_id=window.down_token_id,
                        market_slug=f"{window.asset.lower()}-updown-{_tf}-{window.window_ts}",
                    )
                v2_decisions = await self._strategy_registry.evaluate_all(
                    window, state,
                    window_market=_v2_window_market,
                    current_btc_price=float(getattr(state, "btc_price", 0) or 0),
                    open_price=float(getattr(window, "open_price", 0) or 0),
                )
                for d in v2_decisions:
                    log.info(
                        "strategy_registry_v2.decision_15m",
                        strategy=d.strategy_id,
                        action=d.action,
                        direction=d.direction,
                        skip_reason=d.skip_reason,
                    )
            except Exception as exc:
                log.warning("strategy_registry_v2.eval_error_15m", error=str(exc)[:200])
    except Exception as exc:
        log.warning("fifteen_min.registry_eval_error", error=str(exc)[:200])

    # Keep legacy queue path
    await self._execution_queue.put((window, self._aggregator))
```

---

## 5. New YAML Strategy Configs

All go in `engine/strategies/configs/`. All `mode: GHOST`. Timing gates are 3× the 5m values (15m window = 3× 5m window).

### Non-timing gates are unchanged

| Gate | Value | Why |
|------|-------|-----|
| `delta_magnitude` | 0.0005 | Absolute price move, not time-dependent |
| `confidence.min_dist` | 0.10-0.15 | Model calibration independent of window duration |
| `spread` | 100bps | CLOB spread independent of window duration |
| `session_hours` | [23,0,1,2] | UTC hours don't change |

### `v15m_down_only.yaml`

```yaml
name: v15m_down_only
version: "1.0.0"
mode: GHOST
asset: BTC
timescale: 15m

gates:
  - type: timing
    params: { min_offset: 270, max_offset: 450 }
  - type: direction
    params: { direction: DOWN }
  - type: confidence
    params: { min_dist: 0.10 }
  - type: trade_advised
    params: {}
  - type: clob_sizing
    params:
      schedule:
        - { threshold: 0.55, modifier: 2.0, label: "strong_97pct" }
        - { threshold: 0.35, modifier: 1.2, label: "mild_88pct" }
        - { threshold: 0.25, modifier: 1.0, label: "contrarian_87pct" }
        - { threshold: 0.0, modifier: 0.0, label: "skip_sub25_53pct" }
      null_modifier: 1.5

sizing:
  type: custom
  fraction: 0.025
  max_collateral_pct: 0.10
  custom_hook: clob_sizing

hooks_file: v15m_down_only.py
```

### `v15m_up_asian.yaml`

```yaml
name: v15m_up_asian
version: "1.0.0"
mode: GHOST
asset: BTC
timescale: 15m

gates:
  - type: timing
    params: { min_offset: 270, max_offset: 450 }
  - type: direction
    params: { direction: UP }
  - type: confidence
    params: { min_dist: 0.10, max_dist: 0.20 }
  - type: session_hours
    params: { hours_utc: [23, 0, 1, 2] }
  - type: trade_advised
    params: {}

sizing:
  type: fixed_kelly
  fraction: 0.025
  max_collateral_pct: 0.05
```

### `v15m_up_basic.yaml`

```yaml
name: v15m_up_basic
version: "1.0.0"
mode: GHOST
asset: BTC
timescale: 15m

gates:
  - type: timing
    params: { min_offset: 180, max_offset: 540 }
  - type: direction
    params: { direction: UP }
  - type: confidence
    params: { min_dist: 0.15 }
  - type: trade_advised
    params: {}

sizing:
  type: fixed_kelly
  fraction: 0.025
  max_collateral_pct: 0.05
```

### `v15m_fusion.yaml`

```yaml
name: v15m_fusion
version: "1.0.0"
mode: GHOST
asset: BTC
timescale: 15m

gates: []

hooks_file: v15m_fusion.py
pre_gate_hook: evaluate_polymarket_v2

sizing:
  type: fixed_kelly
  fraction: 0.025
  max_collateral_pct: 0.10
```

### `v15m_gate.yaml`

```yaml
name: v15m_gate
version: "1.0.0"
mode: GHOST
asset: BTC
timescale: 15m

gates:
  - type: timing
    params: { min_offset: 15, max_offset: 900 }
  - type: source_agreement
    params: { min_sources: 2, spot_only: false }
  - type: delta_magnitude
    params: { min_threshold: 0.0005 }
  - type: taker_flow
    params: {}
  - type: cg_confirmation
    params: { oi_threshold: 0.01, liq_threshold: 1000000 }
  - type: confidence
    params: { min_dist: 0.12 }
  - type: spread
    params: { max_spread_bps: 100 }
  - type: dynamic_cap
    params: { default_cap: 0.65 }
  - type: trade_advised
    params: {}

hooks_file: v15m_gate.py
post_gate_hook: classify_confidence

sizing:
  type: fixed_kelly
  fraction: 0.025
  max_collateral_pct: 0.10
```

---

## 6. New Python Hook Files

### `v15m_down_only.py`

Direct copy of `engine/strategies/configs/v4_down_only.py` with:
- `_STRATEGY_ID = "v15m_down_only"` (no version constant needed, file is minimal)

The CLOB sizing logic is identical — CLOB prices are the same regardless of window duration.

### `v15m_gate.py`

Direct copy of `engine/strategies/configs/v10_gate.py` with:
- Label changed from `"v10_DUNE_"` to `"v15m_DUNE_"` in the `SizingResult` return

The confidence classification (`HIGH if max(p, 1-p) > 0.75`) is timeframe-independent.

### `v15m_fusion.py`

Fork of `engine/strategies/configs/v4_fusion.py` with:
- `_STRATEGY_ID = "v15m_fusion"`
- `_VERSION = "1.0.0"`
- Timing thresholds in `_evaluate_poly_v2` scaled 3×:

```python
# v15m_fusion.py: Override timing from eval_offset directly
# (don't trust surface.poly_timing — server may return 5m-calibrated labels)
def _evaluate_poly_v2(surface: "FullDataSurface") -> StrategyDecision:
    direction = surface.poly_direction
    trade_advised = surface.poly_trade_advised or False
    confidence = surface.poly_confidence or 0.5
    distance = surface.poly_confidence_distance or abs(confidence - 0.5)
    reason = surface.poly_reason or "unknown"
    max_entry = surface.poly_max_entry_price

    # Compute timing from eval_offset (15m-calibrated, not server's 5m labels)
    offset = surface.eval_offset or 0
    if offset > 540:
        timing = "early"
    elif offset >= 90:
        timing = "optimal"
    elif offset >= 15:
        timing = "late_window"
    else:
        timing = "expired"

    # ... rest identical to v4_fusion.py _evaluate_poly_v2 ...
    # except: CLOB divergence threshold stays at 0.04 (unchanged)
    # and: late_window guard uses same divergence logic
```

**Key:** The rest of `_evaluate_poly_v2`, `_evaluate_poly_legacy`, `_evaluate_legacy`, and `_skip` are copy-paste identical to `v4_fusion.py`. Only the timing derivation changes.

---

## 7. Complete File List

### Modify (6 files)

| File | Location | Change |
|------|----------|--------|
| `polymarket_5min.py` | `engine/data/feeds/` | Add `@property timeframe` to `WindowInfo` |
| `data_surface.py` | `engine/strategies/` | B1: multi-timeframe `_fetch_v4()` |
| `data_surface.py` | `engine/strategies/` | B2: timeframe-aware `get_surface()` (2 lines) |
| `registry.py` | `engine/strategies/` | B3: timescale filter in `evaluate_all()` (2 lines) |
| `orchestrator.py` | `engine/strategies/` | B4: dynamic `market_slug` (~2 lines) |
| `orchestrator.py` | `engine/strategies/` | B6: wire 15m CLOSING → registry (~30 lines) |

### Create (8 files)

| File | Location |
|------|----------|
| `v15m_down_only.yaml` | `engine/strategies/configs/` |
| `v15m_down_only.py` | `engine/strategies/configs/` |
| `v15m_up_asian.yaml` | `engine/strategies/configs/` |
| `v15m_up_basic.yaml` | `engine/strategies/configs/` |
| `v15m_fusion.yaml` | `engine/strategies/configs/` |
| `v15m_fusion.py` | `engine/strategies/configs/` |
| `v15m_gate.yaml` | `engine/strategies/configs/` |
| `v15m_gate.py` | `engine/strategies/configs/` |

**Total:** ~40 lines changed, 8 new files. B5 (timeframe in decision record) is auto-resolved by Step 0.

---

## 8. Safe Execution Order

Must be followed to keep 5m path working at every step.

**Phase A — Domain foundation (zero risk)**
1. Add `WindowInfo.timeframe` property to `polymarket_5min.py` → purely additive

**Phase B — Data surface (backward-compatible)**
2. B1: `_fetch_v4()` multi-timeframe fetch → 5m data identical, 15m added
3. B2: `get_surface()` timeframe-aware → defaults to `"5m"`, no behavior change for existing callers

**Phase C — Safety gate (before adding any 15m configs)**
4. B3: Timescale filter in `registry.evaluate_all()` → blocks cross-timeframe contamination

**Phase D — Orchestrator wiring**
5. B4: `market_slug` dynamic timeframe derivation
6. B6: Wire 15m CLOSING → strategy registry

**Phase E — Strategy configs (additive, GHOST-only)**
7. Create all 5 YAML configs + 3 Python hooks

---

## 9. Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| 5m strategies fire on 15m windows | HIGH | B3 safety gate — implement before configs |
| V4 server returns only filtered timescale | MEDIUM | Test endpoint first; use two-request fallback |
| 15m fusion timing labels wrong (server 5m-calibrated) | MEDIUM | v15m_fusion.py computes timing from `eval_offset` directly |
| `_on_fifteen_min_window` never reaches registry | CRITICAL | B6 fix — this was the missing link |
| 15m Polymarket markets have different liquidity | MEDIUM | CLOB sizing gate + GHOST mode shadow period |
| 15m model has fewer training samples (3× less frequent) | MEDIUM | 7-day shadow minimum, Billy reviews before LIVE promotion |

---

## 10. Verification Checklist

After deploy with `FIFTEEN_MIN_ENABLED=true ENGINE_USE_STRATEGY_REGISTRY=true`:

```sql
-- 1. Check 15m decisions are flowing
SELECT strategy_id, timeframe, eval_offset, action, direction
FROM strategy_decisions
WHERE timeframe = '15m'
ORDER BY created_at DESC
LIMIT 20;

-- 2. Confirm eval_offset in 270-450 range for down_only/up_asian
SELECT strategy_id, min(eval_offset), max(eval_offset), count(*)
FROM strategy_decisions
WHERE timeframe = '15m'
  AND created_at > NOW() - INTERVAL '1 hour'
GROUP BY strategy_id;

-- 3. Confirm 5m strategies NOT contaminated by 15m windows
SELECT strategy_id, timeframe, count(*)
FROM strategy_decisions
WHERE created_at > NOW() - INTERVAL '1 hour'
GROUP BY strategy_id, timeframe;
-- Expected: v4_* + v10_* only appear with timeframe='5m'
--           v15m_* only appear with timeframe='15m'

-- 4. Confirm v2_probability_up is non-null (15m model loaded)
-- (check via logs: look for surface.v2_probability_up in strategy_registry_v2.decision_15m events)
```

---

## 11. Success Criteria

1. 5 GHOST strategies writing `strategy_decisions` rows with `timeframe='15m'`
2. `eval_offset` values in expected ranges per strategy (e.g. 270-450 for down_only)
3. `v2_probability_up` non-null in 15m surface (15m model feeding correctly)
4. Zero 5m strategy contamination on 15m windows (B3 verified)
5. After 7-day shadow: v15m_down_only WR > 55% on resolved windows
6. Billy explicitly approves first LIVE promotion — no auto-promotion

---

## 12. Future Work (Out of Scope)

- **Multi-asset expansion** (ETH/SOL/XRP): separate plan needed — strategy registry, data surface, and execution are all BTC-only
- **15m eval offsets**: currently fires only at T-60 (CLOSING state). Could add multi-offset evaluation (T-720 to T-60) for richer signal history
- **`_send_window_summary` hardcoded "5m"**: fix Telegram summary to show `5m`/`15m` label
