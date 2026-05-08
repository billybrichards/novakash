# v9_2_super_lgb_only — Investigation Report
*Branch: feat/per_cell_param_overrides — 2026-05-08*

---

## 1. Summary

- **Bug A (confirmed, HIGH): `cell_param_overrides` was NEVER wired into `v9_2_super_lgb_only`.** Commits d7b48f9 and 2ecc4ce added `param_overrides_by_cell` support to v8, v9_ensemble, v12, v9_cascade_fade_late, and v9_1_cascade_fade_late — but explicitly skipped v9_2_super because it was tagged as a "canary". The hook (`evaluate_v9_2_super_lgb_only`) never calls `_get_cell_param_overrides()`. Any per-cell lgb_dist_min overrides pushed via `strategy_runtime_overrides` for v9_2_super are silently ignored — they land in `_gp._ACTIVE` but are never read.

- **Bug B (confirmed, MEDIUM): `CellPauseGate._resolve_direction()` cannot derive direction from `probability_lgb_v9_2`.** The fallback list is `["probability_lgb", "probability_lgb_v9_1", "probability_lgb_v12"]` — `probability_lgb_v9_2` is missing. When `surface.direction` is None (which it often is before direction is resolved inside the hook), the cell_pause gate resolves direction as None and passes silently (`direction unresolved (deferred to signal gate)`). This means the cell_pause gate applied to v9_2_super is effectively a no-op — it never blocks based on the v9_2 model's predicted direction.

- **Bug C (confirmed, LOW-MEDIUM): The qualifying-tick counter is keyed by `(strategy_id, window_ts, direction)` — NOT by cell.** If an operator pushes per-cell `ticks_n_thresholds` or `conviction_x_thresholds` overrides via `strategy_runtime_overrides`, those ARE read (via `_gp._ACTIVE.get()`), but they apply cohort-wide (same threshold for all cells within a `{vpin_regime}_{direction}` cohort), not per-cell. This is architecturally consistent but potentially a source of confusion if the operator expects cell-level N tuning.

- **Bug D (confirmed, HIGH for correctness): v9_2's `evaluate_v9_ensemble` delegation runs with the `v9_2_super_lgb_only` gate_params contextvar still active.** This is CORRECT and intentional — v9_ensemble reads `lgb_dist_min_up/down`, `block_cells`, `param_overrides_by_cell` etc. from `_gp._ACTIVE`, which at delegation time is v9_2's YAML params. The `param_overrides_by_cell` key is NOT in the v9_2 YAML, so it defaults to `{}` in v9_ensemble's gate 10 call. Any `param_overrides_by_cell` in the DB override row for v9_2 would reach gate 10 via the merged params — but only if the DB row key matches, which it might for `lgb_dist_min_*` keys. This is actually a partial interaction: cell overrides for `lgb_dist_min_*` in the v9_2 runtime override row WILL flow into v9_ensemble gate 10 (since it reads from `_gp._ACTIVE` which is set to v9_2's effective params). The v9_2 hook itself does NOT stamp the `cell_param_overrides_active` metadata key, so there is no observability that the override fired.

- **Sister-pair veto (confirmed SAFE by default): The veto defaults to inactive when sisters haven't fired.** `is_sister_pair_veto_active` returns `False` when either sister has no qualifying fire — it requires BOTH to have fired. On engine startup or during low-fire periods, the veto cannot accidentally block v9_2.

---

## 2. Findings

### A. Does v9_2_super honor `cell_param_overrides`?

**Short answer: NO — not explicitly, and only partially via delegation to v9_ensemble.**

Commit d7b48f9 (`feat(engine): per-cell parameter overrides for v9.x + v8 strategies`) modified:
- `engine/strategies/configs/v8_champion_lgb_only.py` — gate 6a
- `engine/strategies/configs/v9_ensemble.py` — gate 10
- Did NOT touch `engine/strategies/configs/v9_2_super_lgb_only.py`

Commit 2ecc4ce (`feat(engine): wire cascade_fade_late strategies via cell_param_overrides`) modified:
- `engine/strategies/configs/v9_1_cascade_fade_late.py` — +19 lines
- `engine/strategies/configs/v9_cascade_fade_late.py` — +19 lines
- Did NOT touch `engine/strategies/configs/v9_2_super_lgb_only.py`

The cascade-fade-late siblings have this pattern at lines 80-112 of their hooks:
```python
_cell_overrides_v9_1_cfl = _get_cell_param_overrides(
    params=_gp.get_dict("param_overrides_by_cell", default={}),
    direction=_cell_direction,
    eval_offset=getattr(surface, "eval_offset", None),
    regime=getattr(surface, "regime", None),
    window_ts=getattr(surface, "window_ts", None),
)
```
And they stamp `cell_param_overrides_active` in decision metadata.

`v9_2_super_lgb_only.py` has NONE of this. No import of `_get_cell_param_overrides`. No call to it. No metadata stamping.

**What does work (partial):** When v9_2 delegates to `_evaluate_v9(surface)`, that call runs with `_gp._ACTIVE` still set to v9_2's effective gate_params (merged YAML + DB override). v9_ensemble's gate 10 (`lgb_safety_floor`) calls `_get_cell_param_overrides(params=_gp.get_dict("param_overrides_by_cell", default={}), ...)`, so if the v9_2 DB override row contains `"param_overrides_by_cell": {...}`, those overrides WILL be applied to `lgb_dist_min_up/down` inside gate 10 of v9_ensemble. However:
1. The cell key used is `{direction}:{t_band}:{vpin_regime}:{session}` where `direction` is derived from the v9.2 probability swap — this should be correct.
2. No metadata is stamped on v9_2 decisions to indicate a cell override fired.
3. The test suite has no test covering this path.

**File references:**
- `engine/strategies/configs/v9_2_super_lgb_only.py`: no import of `get_cell_param_overrides` anywhere
- `engine/strategies/configs/v9_1_cascade_fade_late.py:31,86-91`: shows the pattern v9_2 is missing
- `engine/strategies/configs/v9_ensemble.py:54,1062-1068`: gate 10 cell override (does fire for v9_2 via delegation)

---

### B. Does the `cohort_key` in v9_2 map correctly onto the `cell_key` used by block_cells/cell_pause/cell_param_overrides?

**Two different key spaces — partially overlapping.**

The v9_2 cohort key is: `"{vpin_regime}_{direction}"` e.g. `"TRANSITION_UP"`.
This is used only for the non-consecutive tick-count N lookup. It is NOT the cell key used by block_cells/cell_pause/cell_param_overrides.

The cell key used by `cell_param_overrides.get_cell_param_overrides()` is:
`"{direction}:{t_band}:{regime}:{session}"` e.g. `"UP:T-121-180:TRANSITION:eu_am"`.

These have the same `direction` and `regime` components but differ in structure:
- cohort_key has no `t_band` or `session` dimensions
- cell_key has no combined `{regime}_{direction}` format

The `CellPauseGate` uses `(strategy_id, direction, t_band, regime, session)` for its lookup — again the `cell_key` space, not the cohort_key space.

**Conclusion:** The two key spaces do not conflict — they serve different purposes. The cohort_key feeds only the N-tick counter and conviction threshold lookups in `_ticks_n_for_cohort` / `_conviction_x_for_cohort`. The cell_key feeds block_cells, cell_pause, and cell_param_overrides. There is no mapping mismatch at the key-lookup level; the gap is that v9_2 never calls the cell_key APIs at all (per finding A).

**File references:**
- `engine/strategies/configs/v9_2_super_lgb_only.py:62,106-107,166-172`: cohort_key construction
- `engine/strategies/gates/cell_param_overrides.py:144-145`: cell_key construction
- `engine/strategies/gates/cell_pause.py:106-116`: cell_pause lookup dimensions

---

### C. The qualifying-tick counter is NOT keyed by cell — does this interact badly with cell_param_overrides?

**Yes, there is a semantic gap, but it is not a bug per se.**

The counter `_v9_2_qualifying_tick_count` is keyed by `"{strategy_id}:{window_ts}:{direction}"` (file `v9_2_super_lgb_only.py:107`). This means N is the same for all windows at the same `(window_ts, direction)` regardless of which cell (t_band, session) the eval falls in.

If an operator tried to push per-cell N overrides via `strategy_runtime_overrides`, they would need to put them in `ticks_n_thresholds`, which is a dict keyed by `{vpin_regime}_{direction}` (cohort), not by cell. The actual lookup at line 168-172:

```python
def _ticks_n_for_cohort(cohort_key: str) -> int:
    params = _gp._ACTIVE.get()
    raw = params.get("ticks_n_thresholds", {})
    if isinstance(raw, dict) and cohort_key in raw:
        return int(raw[cohort_key])
    return _DEFAULT_TICKS_N.get(cohort_key, 8)
```

This reads from `_gp._ACTIVE` at call time (runtime-tuneable) — so an operator can override thresholds per cohort via DB. But they cannot set a different N for e.g. `UP:T-121-180:TRANSITION:eu_am` vs `UP:T-61-120:TRANSITION:eu_am`. That granularity is not supported by design.

**The more consequential issue:** The tick counter accumulates BEFORE the base gate check (step 7, then step 8 delegates). When v9_ensemble's base gates SKIP (e.g. delta_gate), `reset_qualifying_ticks_v9_2` is called (line 354). This means a base-gate SKIP resets the counter back to zero — the N-tick requirement must be satisfied continuously within a window where ALL base gates would also have passed. This is strict. If the delta gate or oracle gate flap over the eval band, it can prevent the counter from ever reaching N even when v9_2 conviction is persistently high.

**File references:**
- `engine/strategies/configs/v9_2_super_lgb_only.py:103,107,327-331,352-354`

---

### D. Order of operations — does tick accumulation happen before or after base-gate checks?

**The tick is accumulated BEFORE v9_ensemble gates run, BUT resets if base gates SKIP.**

The flow in `evaluate_v9_2_super_lgb_only` (lines 312-380):
1. `conviction_x` and `ticks_n` are read (live from gate_params)
2. `count_qualifying_tick_v9_2()` is called — increments if conviction >= X
3. `_evaluate_v9(surface)` is called (all base gates run)
4. If base gates SKIP → `reset_qualifying_ticks_v9_2()` — counter CLEARED
5. If base gates TRADE → check `tick_count >= ticks_n`

**The subtle bug here:** When a tick qualifies (conviction >= X) but the base gates also SKIP (e.g., oracle disagreement), step 4 resets the counter. The qualifying tick that was counted at step 2 is lost. For the cohort to fire, the strategy needs N qualifying ticks that ALSO ALL have passing base gates. This is correct as designed (the non-consecutive gate is "N ticks where everything else is also green"), but it means:
- In noisy oracle-direction hours, the counter never builds up
- An operator looking at `v9_2_tick_count` in the metadata will see counts well below N because each oracle-gate miss zeroes out the accumulator

**This is the most likely cause of "isn't firing properly."** If oracle direction gate or fill band gate are flapping (common during TRANSITION regime), the tick counter is perpetually reset. In the v9.1 sibling strategy (which uses `check_confirmation_v9`), the consecutive-tick counter is also reset on base-gate SKIPs, but N=3 for v9.1 vs N=8-12 for v9.2 — the probability of reaching N without a gap is much lower.

**File references:**
- `engine/strategies/configs/v9_2_super_lgb_only.py:312-354` — accumulate then check then reset

---

### E. Sister-pair veto — can it over-fire or default to "active"?

**No. The veto is SAFE by default — it requires BOTH sisters to have fired recently in the opposite direction.**

`is_sister_pair_veto_active()` at `engine/strategies/sister_veto_bus.py:137-189`:
- If either sister has NO qualifying fires → returns `False` immediately (line 179)
- Requires the most recent fire from each sister to be in the OPPOSITE direction
- Default window is ±20 seconds from `current_window_ts`

The bus starts empty at engine boot. `publish_sister_fire` is only called when `v9_cascade_fade_late` or `v9_1_cascade_fade_late` return TRADE. If these sisters are not firing at all (GHOST mode, or their own gates are failing), the bus stays empty and the veto can NEVER activate.

**However, there is one subtle concern:** If the cascade-fade sisters have been promoted to LIVE and are actively firing CASCADE×DOWN, the bus may have recent entries. If v9_2_super is simultaneously trying to fire UP in a CASCADE regime and the sisters just fired DOWN, both sisters firing DOWN opposite to v9_2's UP would trigger the veto. This is by design and the correct behavior per hub notes #394/#395.

**Risk:** The 20s agreement window means a sister fire from a slightly different window_ts (within 20 seconds) can veto v9_2. In fast-moving markets where window_ts increments are exactly 300s, this window is unlikely to mis-fire. But if window_ts values have jitter, a sister fire from a previous window could veto the current one. The `abs(r.window_ts - current) <= cutoff` at line 134 is symmetric — a fire 19s ago in window_ts coordinates counts.

**File references:**
- `engine/strategies/sister_veto_bus.py:137-189` — veto logic
- `engine/strategies/sister_veto_bus.py:124-134` — `get_recent_sister_fires` lookup
- `engine/strategies/configs/v9_1_cascade_fade_late.py:135-143` — publish_sister_fire on TRADE

---

### F. Test coverage for v9_2_super with cell_param_overrides

**Confirmed gap: ZERO test coverage.**

`engine/tests/unit/strategies/test_v9_2_super_lgb_only.py` has 849 lines. The `_bind_gate_params` fixture (lines 134-222) does NOT include `param_overrides_by_cell` or `block_cells` in the params dict.

Searching the entire test file for these terms finds nothing:
```
grep "cell_param_overrides\|param_overrides_by_cell\|block_cells" test_v9_2_super_lgb_only.py
# → (empty)
```

Compare with siblings:
- `test_v9_1_cascade_fade_late_cell_overrides.py` — 280 lines, 10 tests specifically for cell override relax/tighten/no-match/metadata
- `test_v9_cascade_fade_late_cell_overrides.py` — 274 lines, 10 equivalent tests
- `test_v12_lgb_combo_cell_overrides.py` — exists

There is NO equivalent file `test_v9_2_super_lgb_only_cell_overrides.py`.

Additionally, the `_bind_gate_params` fixture does NOT include `sister_pair_veto` in the params dict, meaning tests run without any sister-veto params bound — the `_sister_veto_enabled()` call falls through to `params.get("sister_pair_veto", {})` returning `{}`, and `bool({}.get("enabled", True))` = `True`. So the veto IS enabled in tests but via in-memory default, not from params. The bus starts empty so veto can't fire. This is acceptable for unit tests but means there's no test where the veto would actually fire with bus state.

**File references:**
- `engine/tests/unit/strategies/test_v9_2_super_lgb_only.py:134-222` — _bind_gate_params
- `engine/tests/unit/strategies/configs/test_v9_1_cascade_fade_late_cell_overrides.py` — parity reference

---

## 3. Likely Root Causes (ranked by confidence)

### Root Cause #1: Tick counter reset on base-gate SKIPs prevents N from being reached (HIGH confidence)

The non-consecutive N-of-M gate resets to zero whenever v9_ensemble returns SKIP. For N=8 (most cohorts) or N=12 (TRANSITION_DOWN, CASCADE_DOWN), the strategy needs 8+ consecutive evaluations where ALL of the following are true simultaneously:
- Conviction >= 0.85
- Delta gate passes (chainlink moving with the predicted direction)
- Oracle direction agrees (chainlink + tiingo both agree)
- Fill band passes
- Post-loss cooldown not active

In TRANSITION regime at a volatile hour, oracle direction often disagrees (chainlink/tiingo in opposite directions), resetting the counter. Across a typical 5-minute window with ~6-8 evals, reaching N=8 without a single reset from oracle disagreement or delta misalignment is challenging.

**Evidence path:** `v9_2_super_lgb_only.py:327-354` — tick incremented BEFORE base gate eval, then reset if base gates SKIP.

### Root Cause #2: `cell_param_overrides` not wired in v9_2 hook (HIGH confidence, but more a missing feature than a "not firing" cause)

v9_2 never calls `_get_cell_param_overrides()` explicitly in the hook. Cell-level `lgb_dist_min` overrides set via `strategy_runtime_overrides` for v9_2 ARE partially applied (via the v9_ensemble gate 10 delegation), but:
- No metadata is stamped to confirm this
- The hook-level `conviction_x_thresholds` and `ticks_n_thresholds` overrides can only be per-cohort, never per-cell
- If an operator tries to write a `param_overrides_by_cell` DB override expecting v9_2 to honor it in the same way the cascade-fade strategies do, they'll get partial/invisible behavior

**Evidence path:** `v9_2_super_lgb_only.py` has no import of `get_cell_param_overrides`; compare with `v9_1_cascade_fade_late.py:31,86-92`.

### Root Cause #3: `CellPauseGate` direction resolution misses `probability_lgb_v9_2` (MEDIUM confidence)

`cell_pause.py:156-166` tries to derive direction from `probability_lgb`, `probability_lgb_v9_1`, `probability_lgb_v12` — not `probability_lgb_v9_2`. On the v9_2 surface, `probability_lgb` is the prod LGB (not the v9_2 value). The cell_pause gate will derive direction from the PRODUCTION LGB probability, not the v9_2 probability. This means the cell that gets checked in `cell_pauses` may be the wrong one if prod LGB and v9_2 disagree on direction. In practice, since the evaluate hook only runs after the surface probability_lgb is temporarily swapped, and the cell_pause gate runs AFTER the hook returns (as a post-hook gate), this is actually fine — the swap has been restored by then. However, if `surface.direction` is not set (common in v9 strategies that determine direction inside the hook), the fallback chain will use the prod LGB, not v9_2.

**The real issue:** If `surface.direction` is None AND `probability_lgb` is None (unlikely) OR if `probability_lgb` points the opposite direction from v9_2, the cell_pause gate will either miss or check the wrong cell.

**Evidence path:** `engine/strategies/gates/cell_pause.py:155-166` — missing `probability_lgb_v9_2` in fallback list.

### Root Cause #4: Mode is GHOST — no trades produced regardless (KNOWN, by design)

`v9_2_super_lgb_only.yaml:44` — `status: GHOST`. Unless Billy flipped it to LIVE via runtime override, all TRADE decisions are logged but never executed. "Isn't firing properly" might mean the strategy is producing GHOST TRADE signals that don't appear in trades table (by design).

---

## 4. RDS Queries to Run

Run on: `novakash-pg-prod.cpmisy2asv71.ca-central-1.rds.amazonaws.com` / db `novakash`

---

### Q1: Skip reason distribution over last 7 days

What is actually blocking v9_2_super? Dominated by cohort_below_threshold (good) vs oracle_direction (bad — means base gates are killing the tick counter) vs cell_pause:

```sql
SELECT
    skip_reason,
    COUNT(*) AS n,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM signal_evaluations
WHERE strategy_id = 'v9_2_super_lgb_only'
  AND created_at >= NOW() - INTERVAL '7 days'
  AND skip_reason IS NOT NULL
GROUP BY skip_reason
ORDER BY n DESC
LIMIT 30;
```

---

### Q2: Skip reasons bucketed by (v9_2_cohort × hour_utc) — find mass-skipping cells

Cross-tab to see if a specific cohort × hour combo is mass-skipping (oracle_direction skips in a specific cell confirm root cause #1):

```sql
SELECT
    v9_2_cohort,
    EXTRACT(HOUR FROM to_timestamp(window_ts) AT TIME ZONE 'UTC') AS hour_utc,
    skip_reason,
    COUNT(*) AS n
FROM signal_evaluations
WHERE strategy_id = 'v9_2_super_lgb_only'
  AND created_at >= NOW() - INTERVAL '7 days'
  AND skip_reason IS NOT NULL
GROUP BY v9_2_cohort, hour_utc, skip_reason
HAVING COUNT(*) > 10
ORDER BY n DESC
LIMIT 50;
```

---

### Q3: Current strategy_runtime_overrides for v9_2_super_lgb_only

Confirm what (if anything) is currently set. Check for mode flip, param_overrides_by_cell, ticks_n_thresholds, etc.:

```sql
SELECT
    strategy_id,
    mode,
    params,
    updated_at,
    updated_by,
    updated_reason
FROM strategy_runtime_overrides
WHERE strategy_id IN ('v9_2_super_lgb_only', 'v9_cascade_fade_late', 'v9_1_cascade_fade_late')
ORDER BY strategy_id;
```

---

### Q4: cell_pauses rows for v9_2_super_lgb_only — is it stuck paused?

```sql
SELECT
    strategy_id,
    direction,
    t_band,
    regime,
    session,
    paused_at,
    pause_until,
    reason,
    released_at
FROM cell_pauses
WHERE strategy_id = 'v9_2_super_lgb_only'
ORDER BY paused_at DESC
LIMIT 20;
```

Active pauses only:
```sql
SELECT
    strategy_id, direction, t_band, regime, session,
    paused_at, pause_until, reason
FROM cell_pauses
WHERE strategy_id = 'v9_2_super_lgb_only'
  AND released_at IS NULL
  AND pause_until > NOW();
```

---

### Q5: Did the qualifying-tick counter ever reach N? How many times has v9_2 actually fired TRADE?

Check how many rows have `v9_2_gate_fired = TRUE` (these are actual TRADE fires from v9_2):

```sql
SELECT
    v9_2_cohort,
    COUNT(*) AS gate_fired_count,
    MIN(to_timestamp(window_ts) AT TIME ZONE 'UTC') AS first_fire,
    MAX(to_timestamp(window_ts) AT TIME ZONE 'UTC') AS last_fire,
    AVG(v9_2_conviction) AS avg_conviction
FROM signal_evaluations
WHERE strategy_id = 'v9_2_super_lgb_only'
  AND v9_2_gate_fired = TRUE
  AND created_at >= NOW() - INTERVAL '7 days'
GROUP BY v9_2_cohort
ORDER BY gate_fired_count DESC;
```

---

### Q6: Max tick count seen per cohort — is the counter ever getting close to N?

Check the distribution of `v9_2_tick_count` values in the metadata JSON to see if the counter is building up near the threshold or being reset to 1 repeatedly:

```sql
SELECT
    v9_2_cohort,
    (metadata->>'v9_2_tick_count')::int AS tick_count,
    (metadata->>'v9_2_tick_threshold')::int AS tick_threshold,
    COUNT(*) AS n
FROM signal_evaluations
WHERE strategy_id = 'v9_2_super_lgb_only'
  AND created_at >= NOW() - INTERVAL '7 days'
  AND metadata ? 'v9_2_tick_count'
  AND skip_reason = 'cohort_below_threshold'
GROUP BY v9_2_cohort, tick_count, tick_threshold
ORDER BY v9_2_cohort, tick_count;
```

If all counts cluster at 1 (i.e. the counter always resets to 1 before next check), that confirms root cause #1 — base gate SKIPs are flushing the accumulator every tick.

---

### Q7: Sister veto bus — are the cascade-fade sisters firing and triggering the veto?

```sql
SELECT
    strategy_id,
    direction,
    COUNT(*) AS fires_last_7d,
    MIN(to_timestamp(window_ts) AT TIME ZONE 'UTC') AS first,
    MAX(to_timestamp(window_ts) AT TIME ZONE 'UTC') AS last
FROM strategy_decisions
WHERE strategy_id IN ('v9_cascade_fade_late', 'v9_1_cascade_fade_late')
  AND action = 'TRADE'
  AND created_at >= NOW() - INTERVAL '7 days'
GROUP BY strategy_id, direction;
```

And see how many v9_2 fires were killed by sister veto:
```sql
SELECT COUNT(*)
FROM signal_evaluations
WHERE strategy_id = 'v9_2_super_lgb_only'
  AND skip_reason = 'sister_pair_veto'
  AND created_at >= NOW() - INTERVAL '7 days';
```

---

## 5. Proposed Fix Plan

### Fix 1 (MEDIUM priority — code bug — needs PR): Wire `cell_param_overrides` into v9_2_super hook

**File:** `engine/strategies/configs/v9_2_super_lgb_only.py`

Add import at top:
```python
from strategies.gates.cell_param_overrides import get_cell_param_overrides as _get_cell_param_overrides
```

After the qualifying-tick accumulation and before `_evaluate_v9(surface)` call (around line 333), add:
```python
_cell_overrides_v9_2 = _get_cell_param_overrides(
    params=_gp.get_dict("param_overrides_by_cell", default={}),
    direction=pred_direction,
    eval_offset=getattr(surface, "eval_offset", None),
    regime=vpin_regime,
    window_ts=window_ts,
)
```

Stamp in TRADE metadata:
```python
if _cell_overrides_v9_2:
    meta["cell_param_overrides_active"] = _cell_overrides_v9_2
    meta["cell_param_overrides_direction"] = pred_direction
```

This is parity with v9_1_cascade_fade_late (lines 86-92 and 109-111). Also add `param_overrides_by_cell: {}` to the YAML `gate_params` block (as a no-op default) for docuemntation.

Add corresponding test file `test_v9_2_super_lgb_only_cell_overrides.py` mirroring `test_v9_1_cascade_fade_late_cell_overrides.py`.

---

### Fix 2 (HIGH priority — code bug — needs PR): Add `probability_lgb_v9_2` to `CellPauseGate._resolve_direction()` fallback list

**File:** `engine/strategies/gates/cell_pause.py:155-166`

Change:
```python
for fld in (
    "probability_lgb",
    "probability_lgb_v9_1",
    "probability_lgb_v12",
):
```
To:
```python
for fld in (
    "probability_lgb",
    "probability_lgb_v9_2",
    "probability_lgb_v9_1",
    "probability_lgb_v12",
):
```

This ensures that when v9_2_super's cell_pause gate runs (post-hook, after swap is restored), if `surface.direction` is None the gate correctly uses the v9_2 model's predicted direction rather than the prod LGB direction.

---

### Fix 3 (INVESTIGATION / potentially design fix — needs data): Review whether N=8-12 with base-gate-reset semantics is too strict

This is not a code bug but a **design question** to validate with the RDS queries above. If Q6 shows tick counts clustering at 1 (constant reset), then either:
- **(a) Expected behavior** — the strategy is working as designed but market conditions don't meet the bar. No fix needed.
- **(b) N threshold too high** — lower N via runtime override: `UPDATE strategy_runtime_overrides SET params = '{"ticks_n_thresholds": {"NORMAL_UP": 4, "NORMAL_DOWN": 4, "CASCADE_UP": 4, "CASCADE_DOWN": 6, "TRANSITION_UP": 4, "TRANSITION_DOWN": 6}}'::jsonb WHERE strategy_id = 'v9_2_super_lgb_only';` (YAML/config change — no code change required)
- **(c) Base gate is too aggressive** — identify which gate resets most (Q2) and tune that gate for v9_2 specifically

Priority: data-first. Run Q6 before deciding.

---

### Fix 4 (LOW priority — observability): Stamp `cell_param_overrides_active` metadata even when the override is applied through v9_ensemble gate 10

The indirect path (v9_2 gate_params → v9_ensemble gate 10 → `_get_cell_param_overrides`) does work for `lgb_dist_min` overrides but leaves no metadata trace on the v9_2 decision. This makes it invisible in the Hub dashboard. Fix by adding the explicit call as described in Fix 1.

---

## 6. Open Questions / Risks

1. **Is `probability_lgb_v9_2` actually being emitted by the timesfm-service?** The YAML comment lists it as a dependency: *"timesfm-service: emit `probability_lgb_v9_2` in /v4/snapshot response"*. If this field is None in production (model not loaded), 100% of evaluations skip with `v9_2_model_not_loaded`. RDS query: `SELECT COUNT(*), SUM(CASE WHEN probability_lgb_v9_2 IS NULL THEN 1 ELSE 0 END) FROM signal_evaluations WHERE strategy_id = 'v9_2_super_lgb_only' AND created_at > NOW() - INTERVAL '1d';`

2. **Window_ts clock skew between v9_2 and the sisters.** The sister-veto bus uses `surface.window_ts` (epoch seconds). If v9_2 evaluates at a slightly different `window_ts` than the cascade-fade sisters (due to market data latency or different evaluation ordering), the ±20s window in `is_sister_pair_veto_active` might not cover it. This is hard to verify from code alone — needs production logs.

3. **Post-hook gate ordering.** The YAML gates list for v9_2 is `[chainlink_freshness, cell_pause]`. These run as post-hook gates (after `evaluate_v9_2_super_lgb_only` returns TRADE). If either fires, the decision is re-labeled as SKIP with `post_hook_gate: cell_pause: <reason>` — which would show up in the DB with that skip_reason format. Q1 above should reveal if this is happening.

4. **Engine mode is GHOST by default.** Unless the runtime override has flipped v9_2 to LIVE, all TRADE decisions are GHOST only. "Not firing" could simply mean "producing GHOST trades that aren't being executed." Clarify with Billy whether the complaint is about signal_evaluations fires (TRADE signals) or actual executed trades.

5. **`_v9_2_qualifying_tick_count` memory leak on long-running engines.** The dict accumulates entries forever (one per unique `{strategy_id}:{window_ts}:{direction}` combo). Over months with ~288 windows/day, this is ~600 entries/day. Not critical but worth a TTL cleanup. Not related to "not firing."

6. **The `eval_offset_min: 60` in the YAML is not enforced by the hook.** The hook delegates to v9_ensemble which reads `min_offset_sec` from `_gp._ACTIVE`. `eval_offset_min: 60` in the YAML would need to be `min_offset_sec: 60` to be read by v9_ensemble's gate 1 timing check. The YAML has BOTH `eval_offset_min: 60` AND `min_offset_sec: 45` — the former appears to be a documentation note (for the analysis band) while the latter is the operative gate. This is confusing. The gate 1 timing check uses `min_offset_sec=45`, not `eval_offset_min=60`. This is not a bug in itself but worth cleaning up to avoid operator confusion.

---

*Investigation completed: 2026-05-08 — branch feat/per_cell_param_overrides*
