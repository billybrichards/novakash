# Agent Handover: 15m Clean-Arch Implementation

**Branch:** `feature/15m-clean-arch`  
**Date:** 2026-04-14  
**Full spec:** `docs/superpowers/specs/2026-04-14-15m-clean-arch-design.md`

---

## What You Are Doing

Expanding the BTC trading engine to evaluate 15-minute Polymarket prediction markets. All infrastructure is live — you are fixing hardcoded strings and creating YAML configs. All new strategies are GHOST (log only, never execute).

**Total scope:** ~40 lines changed across 6 existing files, 8 new files created.

---

## Do These Steps In Order

**Each step is safe and backward-compatible with the live 5m path.**

---

### STEP 0 — Add `timeframe` property to `WindowInfo`

**File:** `engine/data/feeds/polymarket_5min.py`

Find the `WindowInfo` dataclass (has fields like `asset`, `window_ts`, `duration_secs`). Add this `@property` method:

```python
@property
def timeframe(self) -> str:
    """Semantic timeframe derived from duration_secs."""
    return "15m" if self.duration_secs >= 900 else "5m"
```

This is purely additive. No existing code breaks. This one property resolves B5 for free.

---

### STEP 1 — Fix `_fetch_v4()` to fetch 15m data (B1)

**File:** `engine/strategies/data_surface.py` around line 253

**First, verify:** Does the V4 snapshot endpoint return both timescales if you drop the `timescale=5m` param? Run this check or ask Billy:
```bash
curl "http://<v4-url>/v4/snapshot?asset=BTC&strategy=polymarket_5m" | python3 -m json.tool | grep -A5 '"15m"'
```

**If both timescales return without the filter** — simplest fix:
```python
params = {"asset": "BTC", "strategy": "polymarket_5m"}  # drop timescale=5m
```

**If server requires timescale param** — add a second fetch and merge:
```python
async def _fetch_v4(self) -> None:
    if not self._session:
        return
    url = f"{self._v4_url}/v4/snapshot"
    try:
        async with self._session.get(url, params={"asset": "BTC", "timescale": "5m", "strategy": "polymarket_5m"}) as resp:
            if resp.status == 200:
                self._cached_v4 = await resp.json()
                self._cached_v4_ts = time.time()
        # Merge 15m timescale into existing cache
        async with self._session.get(url, params={"asset": "BTC", "timescale": "15m", "strategy": "polymarket_15m"}) as resp:
            if resp.status == 200:
                data_15m = await resp.json()
                ts_15m = (data_15m.get("timescales") or {}).get("15m")
                if ts_15m and self._cached_v4:
                    self._cached_v4.setdefault("timescales", {})["15m"] = ts_15m
    except Exception:
        pass
```

---

### STEP 2 — Fix `get_surface()` to read correct timescale (B2)

**File:** `engine/strategies/data_surface.py`

Find the `get_surface()` method. Make two targeted edits:

**Edit 1** — around line 366-368, change the `ts_data` lookup:
```python
# Before:
ts_data = (v4.get("timescales") or {}).get("5m", {})

# After:
timeframe = getattr(window, "timeframe", "5m")
ts_data = (v4.get("timescales") or {}).get(timeframe, {})
```

**Edit 2** — in the `return FullDataSurface(...)` block, around line 397:
```python
# Before:
timescale="5m",

# After:
timescale=timeframe,
```

That's it. `timeframe` is already defined from Edit 1. Default is `"5m"`, so all existing 5m callers are unaffected.

---

### STEP 3 — Add timescale filter to `evaluate_all()` (B3 — SAFETY GATE)

**File:** `engine/strategies/registry.py`

Find `evaluate_all()`. Add `window_tf` derivation near the top, then add the 2-line guard inside the strategy loop.

```python
async def evaluate_all(self, window: Any, state: Any, ...) -> list[StrategyDecision]:
    eval_offset = getattr(window, "eval_offset", None)
    window_ts = getattr(window, "window_ts", 0)
    window_tf = getattr(window, "timeframe", "5m")  # ADD THIS LINE
    surface = self._data_surface.get_surface(window, eval_offset)

    decisions = []
    for name, config in self._configs.items():
        if config.mode == "DISABLED":
            continue
        if config.timescale != window_tf:   # ADD THIS LINE
            continue                         # ADD THIS LINE
        try:
            ...
```

This is the critical safety gate. Without it, 5m strategies fire on 15m windows. Implement this **before** adding the YAML configs.

---

### STEP 4 — Fix `market_slug` in orchestrator (B4)

**File:** `engine/strategies/orchestrator.py`

Search for `updown-5m-` (there are two instances — fix the one inside `_on_five_min_window` around line 1952, in the `WindowMarket` construction block):

```python
# Before:
market_slug=f"{window.asset.lower()}-updown-5m-{window.window_ts}",

# After:
_tf = getattr(window, "timeframe", "5m")
...
market_slug=f"{window.asset.lower()}-updown-{_tf}-{window.window_ts}",
```

Note: `_tf` must be defined before the `WindowMarket(...)` call. Add it in the `if getattr(window, "up_token_id", None)` block, just before constructing `_v2_window_market`.

---

### STEP 5 — Wire 15m CLOSING state → strategy registry (B6 — CRITICAL GAP)

**File:** `engine/strategies/orchestrator.py`

Find `_on_fifteen_min_window()`. Find the `if state_value == "CLOSING":` block (around line 2194). **Before** the `await self._execution_queue.put(...)` line, insert:

```python
if state_value == "CLOSING":
    # Strategy registry v2: evaluate all 15m GHOST strategies
    try:
        _reg_state = await self._aggregator.get_state()
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
                    window, _reg_state,
                    window_market=_v2_window_market,
                    current_btc_price=float(getattr(_reg_state, "btc_price", 0) or 0),
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

    # Keep legacy queue path (unchanged)
    await self._execution_queue.put((window, self._aggregator))
```

This is the missing link. Without it, YAML configs load but never run.

---

### STEP 6 — Create YAML strategy configs

Create these files in `engine/strategies/configs/`. Copy from spec doc or the exact YAML below.

**`v15m_down_only.yaml`:**
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

**`v15m_up_asian.yaml`:**
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

**`v15m_up_basic.yaml`:**
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

**`v15m_fusion.yaml`:**
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

**`v15m_gate.yaml`:**
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

### STEP 7 — Create Python hook files

**`v15m_down_only.py`** — Copy `v4_down_only.py` exactly. No changes needed (CLOB sizing logic is timeframe-independent).

**`v15m_gate.py`** — Copy `v10_gate.py`. Change one string: `"v10_DUNE_"` → `"v15m_DUNE_"` in the `SizingResult` return label.

**`v15m_fusion.py`** — Fork `v4_fusion.py`:
1. Change `_STRATEGY_ID = "v15m_fusion"` and `_VERSION = "1.0.0"`
2. In `_evaluate_poly_v2`: replace the `timing = surface.poly_timing or "unknown"` line with eval_offset-based derivation:

```python
# Replace server timing label with offset-based derivation (server may be 5m-calibrated)
offset = surface.eval_offset or 0
if offset > 540:
    timing = "early"
elif offset >= 90:
    timing = "optimal"
elif offset >= 15:
    timing = "late_window"
else:
    timing = "expired"
```

3. Everything else in `v4_fusion.py` stays identical.

---

## Deploy Instructions

```bash
# Required env vars on Montreal
FIFTEEN_MIN_ENABLED=true
FIFTEEN_MIN_ASSETS=BTC
ENGINE_USE_STRATEGY_REGISTRY=true
```

Deploy via the standard path (rsync to Montreal, restart engine). Do NOT push to develop directly — open a PR.

---

## Verify After Deploy

```sql
-- Should return 15m rows within first window cycle (~15 minutes)
SELECT strategy_id, timeframe, eval_offset, action, direction, skip_reason
FROM strategy_decisions
WHERE timeframe = '15m'
ORDER BY created_at DESC
LIMIT 20;

-- Confirm no cross-contamination
SELECT strategy_id, timeframe, count(*)
FROM strategy_decisions
WHERE created_at > NOW() - INTERVAL '1 hour'
GROUP BY strategy_id, timeframe
ORDER BY strategy_id;
-- v4_* and v10_* must ONLY appear with timeframe='5m'
-- v15m_* must ONLY appear with timeframe='15m'
```

---

## What NOT To Do

- Do NOT change any 5m YAML configs or their hooks
- Do NOT promote any v15m_* strategy to LIVE — GHOST only, Billy reviews first
- Do NOT push directly to `develop` — PR only
- Do NOT skip Step 3 (B3 safety gate) before creating the YAML files
- Do NOT auto-promote models or thresholds — Billy's explicit approval required

---

## Open Question for Implementation

Before implementing Step 1, verify the V4 snapshot endpoint behavior:
- Does `GET /v4/snapshot?asset=BTC&strategy=polymarket_5m` (without `timescale=`) return both `timescales["5m"]` and `timescales["15m"]`?
- If yes → use the simple single-request fix
- If no → use the two-request merge approach

This is the only unknown. Everything else is deterministic.
