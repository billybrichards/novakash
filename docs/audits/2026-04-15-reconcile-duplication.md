# Reconcile pipeline audit — 2026-04-15

Audit trigger: review of `clean_architecture_python_guide.md` + observed
Telegram spam from multiple resolution paths. Goal: identify duplicate
reconcile functions/processes before they cause divergent state.

## Inventory

Current reconcile-related code, grouped by concern.

### Classes

| File | Class | LOC | Status |
|---|---|---|---|
| `engine/reconciliation/reconciler.py` | `CLOBReconciler` | 101 KB (~2500 LOC) | **LEGACY** — pre-clean-arch monolith |
| `engine/reconciliation/poly_fills_reconciler.py` | `PolyFillsReconciler` | 15.5 KB | Active — reconciles Polymarket CLOB fills into `trades` |
| `engine/reconciliation/poly_trade_history.py` | `PolyTradeHistoryReconciler` | 6.2 KB | Active — reconciles historical activity API |
| `engine/reconciliation/state.py` | `ReconcilerState` | 1.2 KB | Shared dedup state container |
| `engine/use_cases/reconcile_positions.py` | `ReconcilePositionsUseCase` | 17.5 KB | **NEW** — clean-arch replacement for `CLOBReconciler._resolve_position` |

### Orchestrator loops

| Loop | Owner | Interval | Calls |
|---|---|---|---|
| `_polymarket_reconcile_loop` (orchestrator.py:3398) | Orchestrator | 5 min | `PolyTradeHistoryReconciler`, activity API |
| `_sot_reconciler_loop` (orchestrator.py:4173) | Orchestrator | 2 min | `CLOBReconciler` (legacy) + `ReconcilePositionsUseCase` (when `ENGINE_USE_RECONCILE_UC=true`) |

## Overlaps

### 1. Position resolution (`CLOBReconciler` vs `ReconcilePositionsUseCase`)

**Same work, two implementations**:

- `CLOBReconciler._resolve_position` (reconciler.py:756) — matches position to trade by token_id, updates outcome/pnl/resolved_at, sends per-trade Telegram notification.
- `ReconcilePositionsUseCase.resolve_one` (reconcile_positions.py:200) — same three-tier matching (exact → prefix → cost fallback), same resolution writes, same notification (now batched after PR #182).

**Gating**: `_sot_reconciler_loop` runs EITHER the use case OR the legacy class based on `ENGINE_USE_RECONCILE_UC` env. Both paths coexist.

**Risk**:
- Forgotten-env-var drift: bug fixed in one path, not the other.
- Spam amplification: legacy class still sends per-trade alerts from the orphan-check branch (reconciler.py:703-743); PR #182 only batched the use-case path.

**Recommended**: retire `CLOBReconciler._resolve_position` + its callers. Move any remaining unique behaviour (orphan-check alert format at reconciler.py:726-739) into the use case or its own small adapter. Target file-size drop: ~800 LOC from `reconciler.py`.

### 2. Polymarket activity reconciliation (`PolyTradeHistoryReconciler` vs `_polymarket_reconcile_loop`)

**Same source, two consumers**:

- `_polymarket_reconcile_loop` (orchestrator.py:3398) — polls `data-api.polymarket.com/activity?user=...` directly in the orchestrator, compares to DB, corrects mismatches.
- `PolyTradeHistoryReconciler` (reconciliation/poly_trade_history.py) — instantiated at orchestrator.py:1017, does similar activity-API-backed reconciliation.

**Risk**: Two loops hit the same external endpoint, risk of double-correction or write-race on the same trade row.

**Recommended**: pick one. The use-case-style reconciler is the clean-arch shape; fold the inline loop logic into a `ReconcileActivityUseCase` and delete `_polymarket_reconcile_loop`.

### 3. Telegram alerting (pre-#182)

Resolution notifications fired from **three** independent sites:

1. `CLOBReconciler._resolve_position._send_trade_alert` (reconciler.py:703-743)
2. `ReconcilePositionsUseCase._notify_resolution` (reconcile_positions.py:334) — **batched in PR #182**
3. `ReconcilePositionsUseCase._send_resolution_alert` (reconcile_positions.py:354) — **batched in PR #182**

PR #182 only addresses sites 2 and 3. Site 1 will continue to spam if `ENGINE_USE_RECONCILE_UC=false` or the legacy orphan-check branch runs.

**Recommended**: after retiring `CLOBReconciler` (see overlap 1), site 1 disappears with it.

## Non-duplicates (confirmed distinct concerns)

- `PolyFillsReconciler` vs `ReconcilePositionsUseCase` — PolyFills reconciles CLOB **fill events** into trades (trade creation path); the use case reconciles **position outcomes** (resolution path). Different stages.
- `ReconcilerState` (state.py) — shared dedup state, used by multiple reconcilers intentionally.

## Recommendations (prioritised)

1. **P0 — retire `CLOBReconciler` dual-path** (biggest blast radius): move orphan-resolution alert to the use case, delete the class, flip `ENGINE_USE_RECONCILE_UC` to always-on then remove the flag. Eliminates alert site #1 and prevents forgotten-env-var bugs.
2. **P1 — consolidate activity reconciliation**: fold `_polymarket_reconcile_loop` logic into a `ReconcileActivityUseCase`, delete the inline loop.
3. **P2 — extract `_send_window_summary` grouping** — **DONE in PR C (#186)**.
4. **P2 — batch resolution alerts** — **DONE in PR #182** (only for the use-case path; Site 1 still needs P0 above).

## References

- Audit trigger: `clean_architecture_python_guide.md`
- Related PRs: #181 (grouping fix), #182 (alert batching), #183 (cooldown hygiene), #184 (frontend crash), #185 (redeem_attempts), #186 (clean-arch extraction)
- Port definitions: `engine/domain/ports.py`
- Use-case boundary: `engine/use_cases/`
