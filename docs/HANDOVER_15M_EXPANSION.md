# Handover: BTC 15-Minute Trading Expansion

**Status:** Ready to implement — infrastructure complete, ~15 lines of code to unlock.

---

## What's already working (no action needed)

- Polymarket 15m feed (`Polymarket5MinFeed(duration_secs=900)`) — fires window signals correctly
- 15m LightGBM model — 427K predictions in `ticks_v2_probability` since Apr 8, all 6 Δ buckets live
- v3 composite scorer — 330K rows at `timescale="15m"` since Apr 9
- V4 snapshot assembler — returns `timescales["15m"]` block when requested
- `strategy_decisions.timeframe` VARCHAR column — no migration needed
- `FIFTEEN_MIN_ENABLED=true` env var wired, feed starts on deploy

---

## The only real work: 5 hardcoded "5m" strings (15M-01)

Total effort: ~15 lines across 3 files.

### B1 + B2 — `engine/strategies/data_surface.py`

**Line 253** (`_fetch_v4`): hardcodes `timescale: "5m"`. 15m model output in snapshot `timescales["15m"]` is never fetched.

**Line 358** (`get_surface`): hardcodes `.get("5m", {})` and `timescale="5m"` in returned surface.

Fix:
```python
# _fetch_v4: loop over both timeframes
for tf in ["5m", "15m"]:
    params = {"asset": "BTC", "timescale": tf, "strategy": "polymarket_5m"}
    self._cached_v4[tf] = await resp.json()  # was single dict, now dict[str, dict]

# get_surface: derive from window
window_tf = "15m" if getattr(window, "duration_secs", 300) == 900 else "5m"
ts_data = (v4.get("timescales") or {}).get(window_tf, {})
timescale = window_tf  # in returned FullDataSurface
```

### B3 — `engine/strategies/registry.py` (`evaluate_all`)

No timescale filter → all 5 5m strategies fire on 15m windows with wrong timing gates.

Fix (add near top of strategy loop):
```python
window_tf = "15m" if getattr(window, "duration_secs", 300) == 900 else "5m"
if config.timescale != window_tf:
    continue
```

Also fix `_send_window_summary` where `"5m"` is hardcoded in the Telegram alert string.

### B4 — `engine/strategies/orchestrator.py` line 1736

`market_slug` hardcodes `"-updown-5m-"` — would try to execute a 15m trade into a 5m Polymarket market.

Fix:
```python
_tf = "15m" if window.duration_secs == 900 else "5m"
market_slug = f"{window.asset.lower()}-updown-{_tf}-{window.window_ts}"
```

### B5 — `engine/strategies/registry.py` (timeframe derivation)

`getattr(window, "timeframe", "5m")` fallback means all 15m `strategy_decisions` rows saved as `timeframe='5m'`.

Fix: replace fallback with derivation:
```python
window_tf = "15m" if getattr(window, "duration_secs", 300) == 900 else "5m"
```

---

## After fixing blockers: create 5 YAML strategy configs (15M-02)

In `engine/strategies/configs/`, create 5 new files with timing gates scaled 3x:

| File | Direction | Mode | Timing |
|------|-----------|------|--------|
| `v15m_down_only.yaml` | DOWN | GHOST | 270-450s |
| `v15m_up_asian.yaml` | UP | GHOST | 270-450s + session [23,0,1,2] |
| `v15m_up_basic.yaml` | UP | GHOST | 180-540s |
| `v15m_fusion.yaml` | Both | GHOST | custom hook (early >540, optimal 90-540) |
| `v15m_gate.yaml` | Both | GHOST | 15-900s, 8-gate pipeline |

Python hooks: `v15m_down_only.py` (CLOB sizing), `v15m_fusion.py` (scaled timing bands), `v15m_gate.py` (copy of v10_gate.py — identical classify_confidence logic).

---

## Deploy + verify (15M-03)

```bash
# env vars needed
FIFTEEN_MIN_ENABLED=true
FIFTEEN_MIN_ASSETS=BTC
ENGINE_USE_STRATEGY_REGISTRY=true
```

Verify after deploy:
```sql
-- should return rows with timeframe='15m' and eval_offset 270-450
SELECT strategy_id, timeframe, eval_offset, action
FROM strategy_decisions
WHERE timeframe = '15m'
ORDER BY created_at DESC
LIMIT 20;
```

---

## Full plan

`docs/BTC_15M_EXPANSION_PLAN.md` — all details, gate scaling table, risk assessment.

AuditChecklist items: `15M-01` through `15M-05`.

---

## Multi-asset expansion (ETH/SOL/XRP)

Not started — needs separate clean-arch plan. The feed already supports multiple assets (`SUPPORTED_ASSETS = ["BTC","ETH","SOL","DOGE","BNB","XRP"]`) but strategy registry, data surface, and execution are all BTC-only. Separate task.
