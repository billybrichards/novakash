# Clean Architecture Phases 2 & 3 — Registry as Primary Execution

**Date:** 2026-04-16
**Context:** Registry execution path is confirmed primary on Montreal. five_min_vpin.py still exists and still owns signal evaluation. This plan removes dead execution code and migrates the signal layer.

---

## Status Quo Audit (2026-04-15 23:07 UTC)

### ✅ What IS working (registry is primary)

| Check | Evidence | Status |
|---|---|---|
| `LEGACY_EXECUTION_DISABLED=true` in .env | Verified via SSH on Montreal | ✅ |
| `ENGINE_REGISTRY_EXECUTE=true` in .env | Verified | ✅ |
| `ExecuteTradeUseCase` wired at startup | `orchestrator.execute_trade_uc_wired paper_mode=False` in log | ✅ |
| `ReconcilePositionsUseCase` wired | `orchestrator.reconcile_uc_wired` in log | ✅ (after PR #207) |
| Real orders placed today via registry path | 5 `place_order.live_submitted` events, all via `registry.executed success=True` → `ExecutionResult.execution_mode=gtc_resting` | ✅ |
| Trade alerts → Telegram | 3 `telegram.strategy_trade_sent` events (matches 3 of today's trades) | ✅ |
| Trades written to DB | Hub API `/api/trades` returns today's 5 orders with matching order_ids | ✅ |
| Legacy `_execute_trade` call sites | All hit `legacy_execute_trade.removed` stub (13 warnings), no actual execution | ✅ |

**Bottom line: registry IS the primary path. Today's trades all flowed through ExecuteTradeUseCase. Legacy path does NOTHING.**

### ⚠️ Gaps found during audit

1. **`strategy_id` column NULL in trades table** — Hub API shows `strategy_id=null` for all 5 orders. `TradeRecorder.record_trade()` isn't persisting the strategy identity.
2. **67 `registry.executed success=False mode=none` events with no root-cause logs** — `execute_trade.py` uses stdlib `logger.info("event", extra={...})` which the structlog stdlib bridge drops. Same bug I already fixed in `publish_heartbeat.py` (use `exc_info=True` / positional args / switch to structlog).
3. **`legacy_execute_trade.removed` stubs still fire 13×/session** — dead stub call sites inside `five_min_vpin.py` (lines 315, 1187, 2569) that should be deleted.
4. **`FiveMinVPINStrategy` still owns signal evaluation** — TimesFM v2/v8 calls, source-agreement (Chainlink/Tiingo/Binance), window summary building, VPIN integration. Not yet migrated to clean-arch.

---

## Phase 2 — Delete dead execution code from five_min_vpin.py

**Goal:** Remove every execution-related method and call site inside `five_min_vpin.py` now that nothing uses them. File shrinks significantly but keeps signal evaluation.

**Scope:** 3–5 PRs, small and surgical.

### Tasks

#### Task 2.1: Delete `legacy_execute_trade.removed` stubs
**Files:** `engine/strategies/five_min_vpin.py` (lines 315, 1187, 2569)

These stubs exist only to log a warning. No caller should hit them. Audit caller sites, verify they're all gated by `LEGACY_EXECUTION_DISABLED`, delete the stubs + the caller branches entirely.

- [ ] **Step 1** — Read context around lines 315, 1187, 2569 and map the caller paths
- [ ] **Step 2** — For each caller, delete the `if not LEGACY_EXECUTION_DISABLED` branch (it's always true now)
- [ ] **Step 3** — Delete the stub itself
- [ ] **Step 4** — Run `pytest engine/tests/ -k five_min` — must still pass
- [ ] **Step 5** — Commit: `refactor(five_min_vpin): drop legacy_execute_trade stubs (always-removed branches)`

#### Task 2.2: Delete the legacy `_execute_trade()` private method
**Files:** `engine/strategies/five_min_vpin.py`

Find the full `_execute_trade` method body (the one mentioned in execute_trade.py:3 as the thing being replaced). Confirm zero callers remain post-Task 2.1, then delete.

- [ ] **Step 1** — `grep -n "_execute_trade" engine/strategies/five_min_vpin.py` → enumerate all refs
- [ ] **Step 2** — Read the method body
- [ ] **Step 3** — Verify every call site is gated behind `LEGACY_EXECUTION_DISABLED` check (all eliminated in Task 2.1)
- [ ] **Step 4** — Delete the method + any now-orphan helper methods (`_calculate_stake_legacy`, `_build_trade_record_legacy`, etc.)
- [ ] **Step 5** — Delete unused imports that were only used by the deleted methods
- [ ] **Step 6** — Run `pytest engine/tests/ -q` — all existing pass
- [ ] **Step 7** — Commit: `refactor(five_min_vpin): delete legacy _execute_trade — replaced by ExecuteTradeUseCase`

#### Task 2.3: Delete legacy FAK-ladder / RFQ / GTC helpers in five_min_vpin.py
**Files:** `engine/strategies/five_min_vpin.py`

Those are duplicated in `adapters/execution/fak_ladder_executor.py`. Delete.

- [ ] **Step 1** — Identify helpers: `_try_fak_ladder`, `_try_rfq_fallback`, `_try_gtc_fallback` or similar
- [ ] **Step 2** — Verify they're only called by the already-deleted `_execute_trade`
- [ ] **Step 3** — Delete
- [ ] **Step 4** — Commit: `refactor(five_min_vpin): delete FAK/RFQ/GTC helpers — moved to FAKLadderExecutor adapter`

#### Task 2.4: Fix stdlib→structlog logging in execute_trade.py (audit gap #2)
**Files:** `engine/use_cases/execute_trade.py`

Replace `logger.info("event", extra={...})` with positional `logger.info("event: %s %s", k1, v1, exc_info=True)` OR switch to `structlog.get_logger(__name__)`. This is the same bug I fixed in publish_heartbeat.py.

- [ ] **Step 1** — `grep -n "logger\." engine/use_cases/execute_trade.py` — 8 call sites
- [ ] **Step 2** — Switch to structlog: replace `import logging` → `import structlog`, `logger = logging.getLogger(__name__)` → `log = structlog.get_logger(__name__)`
- [ ] **Step 3** — Rewrite each `logger.info("x", extra={...})` as `log.info("x", **fields)`
- [ ] **Step 4** — Verify a failing execute() surfaces the real reason (dedup, risk_blocked, guardrail, no_token_id, execution_error) in structured fields
- [ ] **Step 5** — Commit: `fix(exec): switch execute_trade logger to structlog so failure reasons render`

Post-merge validation: next day's logs should show 0 mystery `mode=none` failures — each will have a `failure_reason`.

#### Task 2.5: Fix `strategy_id` NULL in trades table (audit gap #1)
**Files:** `engine/adapters/execution/trade_recorder.py`

The `record_trade(decision, result, stake)` call has `decision.strategy_id` but DBTradeRecorder isn't persisting it. Trace the DB write path, find the column mapping gap, wire it through.

- [ ] **Step 1** — Read `DBTradeRecorder.record_trade` implementation
- [ ] **Step 2** — Identify which DB call writes the row (likely `order_manager.record_order` or `db.insert_trade`)
- [ ] **Step 3** — Add `strategy_id=decision.strategy_id, strategy_version=decision.strategy_version` to the column map
- [ ] **Step 4** — Verify `trades` table has columns or add them via alembic migration
- [ ] **Step 5** — Test: after next trade, Hub API `/api/trades` shows populated `strategy_id`
- [ ] **Step 6** — Commit: `fix(trade_recorder): persist strategy_id + version to trades table`

#### Task 2.6: Add CI regression test for registry-primary execution path
**Files:** `engine/tests/integration/test_execute_trade_e2e.py` (new)

End-to-end test with synthetic StrategyDecision → ExecuteTradeUseCase.execute() → PaperExecutor → TradeRecorder + AlerterPort mocks. Asserts:
- `record_trade` called exactly once with populated strategy_id
- `send_trade_alert` called with correct payload
- `window_state.mark_traded` called
- ExecutionResult.success = True

- [ ] **Step 1** — Write test using existing `AsyncMock` pattern from unit tests
- [ ] **Step 2** — Add to CI `engine-tests` job (currently only import-smoke runs)
- [ ] **Step 3** — Commit: `test(exec): e2e coverage for ExecuteTradeUseCase happy path`

### Phase 2 exit criteria

- `five_min_vpin.py` line count reduced (currently 3100, target <2000 after execution removal)
- Zero `legacy_execute_trade.removed` warnings in Montreal engine.log for a 24h run
- All 67 `registry.executed success=False` events have visible root causes (`failure_reason` in structlog fields)
- `trades.strategy_id` column populated for new trades in Hub API
- CI green with new e2e test

---

## Phase 3 — Extract signal evaluation into clean-arch use case

**Goal:** Move the remaining load-bearing logic out of `FiveMinVPINStrategy` into a proper `EvaluateWindowUseCase` + domain services. After this, `five_min_vpin.py` becomes a thin coordinator (or is deleted entirely if the coordinator duty moves elsewhere).

**Scope:** 3–4 PRs, each larger than Phase 2 tasks because this is real logic migration.

### What's still in five_min_vpin.py after Phase 2

Based on the 3100-line file today:
1. **Signal evaluation pipeline** — TimesFM v2 / v8.1 early gate / v9 source-agreement / confidence scoring
2. **Window queue management** — `append_pending_window`, `append_recent_window`, `trim_recent_windows`
3. **CoinGlass warning / regime classification helpers**
4. **Window summary snapshots to `window_snapshots` DB table**
5. **Integration with aggregator + strategy registry**

Phase 2 just removed the execution part. Phase 3 migrates (1)–(4).

### Tasks

#### Task 3.1: Define `EvaluateWindowUseCase` contract
**Files:** `engine/use_cases/evaluate_window.py` (already exists as stub), `engine/use_cases/ports/`

The existing `test_evaluate_window.py` suggests a stub use case exists. Audit it + formalize the contract.

Port design (interfaces it needs):
- `TimesFMClientPort` — predict_direction(window, offset) → prediction
- `MultiSourceDeltaPort` — compute_consensus(binance, tiingo, chainlink) → (direction, confidence_flag)
- `VPINCalculatorPort` — current_vpin() → float
- `CoinGlassWarningPort` — check_warnings(direction) → list[str]
- `WindowSnapshotRepository` — already exists in domain/ports

- [ ] **Step 1** — Read `engine/use_cases/evaluate_window.py` current state
- [ ] **Step 2** — Define the 4 ports above in `use_cases/ports/`
- [ ] **Step 3** — Write the use case input/output dataclasses
- [ ] **Step 4** — Commit: `feat(arch): define EvaluateWindowUseCase contract + ports`

#### Task 3.2: Implement signal-evaluation logic in EvaluateWindowUseCase
**Files:** `engine/use_cases/evaluate_window.py`

Port the `FiveMinVPINStrategy.on_window()` evaluation body (the non-execution part that was left behind after Phase 2).

- [ ] **Step 1** — Read the eval body in five_min_vpin.py
- [ ] **Step 2** — Port line-by-line using the injected ports instead of direct imports
- [ ] **Step 3** — Preserve behavior byte-for-byte
- [ ] **Step 4** — Unit tests with mocked ports for each decision branch (skip reasons)
- [ ] **Step 5** — Commit: `feat(arch): port signal evaluation from FiveMinVPINStrategy to EvaluateWindowUseCase`

#### Task 3.3: Write adapter implementations for the 4 ports
**Files:** `engine/adapters/prediction/timesfm_client.py` (exists), `engine/adapters/consensus/three_source.py` (exists), new adapters for VPIN + CoinGlass

Most adapters already exist. Wrap them in the new port contracts.

- [ ] Per-adapter: write wrapper class implementing the port, delegating to existing implementation. Commit per adapter.

#### Task 3.4: Wire EvaluateWindowUseCase into composition + switch call site
**Files:** `engine/infrastructure/composition.py`, `engine/infrastructure/runtime.py`

Replace the `FiveMinVPINStrategy` instantiation + usage with `EvaluateWindowUseCase`. Keep the strategy class as a thin wrapper OR delete entirely depending on what other code still touches it.

- [ ] **Step 1** — Grep all `FiveMinVPINStrategy` refs outside the file
- [ ] **Step 2** — Migrate each ref to use the use case
- [ ] **Step 3** — Keep `on_window()` no-op stub for `ProcessFiveMinWindowUseCase` compat (or migrate that too)
- [ ] **Step 4** — Commit: `refactor(arch): wire EvaluateWindowUseCase — FiveMinVPINStrategy now thin glue`

#### Task 3.5: Delete FiveMinVPINStrategy entirely (stretch goal)
**Files:** `engine/strategies/five_min_vpin.py` — DELETE

Only if (3.4) successfully eliminated every non-use-case reference.

- [ ] **Step 1** — `grep -rn "FiveMinVPINStrategy\|from strategies.five_min_vpin"` → must return 0 results outside the file itself
- [ ] **Step 2** — `git rm engine/strategies/five_min_vpin.py`
- [ ] **Step 3** — Remove from `engine/strategies/__init__.py`
- [ ] **Step 4** — Commit: `refactor(arch): delete five_min_vpin.py — all logic migrated to clean-arch use cases`

### Phase 3 exit criteria

- `five_min_vpin.py` deleted (or ≤ 200 lines of pure glue if we keep a shim)
- `EvaluateWindowUseCase` has ≥ 90% unit-test coverage with mocked ports
- Production signal evaluation on Montreal runs through the use case for 24h with no regression in decision distribution (skip reasons, trade counts match pre-migration baseline within 5%)
- Composition root assembles everything with explicit port wiring (no implicit module-level imports doing work)

---

## Risk Management

**What can go wrong + mitigations:**

| Risk | Mitigation |
|---|---|
| Deleting a legacy method breaks a subtle non-exec caller we missed | Run the full Montreal engine for 1h paper-mode after each Phase 2 task. No errors, no silent behavior drift. |
| Phase 3 signal-eval migration introduces decision drift | Run OLD path + NEW path in parallel in shadow mode for 24h, diff the decisions. Only promote when ≥99% match. |
| Trade alerts stop firing mid-migration | Add integration test that instantiates the full composition root + sends a mock trade, asserts alert sent |
| Phase 2 Task 2.4 (logging fix) exposes pre-existing bugs now made visible | Good — we want them visible. Triage each one, open an issue, fix or accept. |

**Rollback plan:** every PR is a single squash-merge → `git revert <sha>` on develop → CI/CD redeploys Montreal in ~4 min.

---

## Dependencies / order of operations

```
Task 2.1 → 2.2 → 2.3  (linear, each deletes more code)
Task 2.4 (independent, can land any time)
Task 2.5 (independent)
Task 2.6 (after 2.4 so structlog fields are visible to tests)
────────── Phase 2 ships ──────────
Task 3.1 → 3.2 → 3.3 → 3.4 → 3.5  (linear)
```

Phase 2 should take 1–2 sessions. Phase 3 is bigger — 3–5 sessions depending on test depth.

---

## Open questions

1. **Does `FiveMinVPINStrategy` have other responsibilities beyond signal eval?** Need a file-wide read before Phase 3 Task 3.2 to be sure we're not leaving orphan behavior.
2. **`v4_fusion` strategy — does it also use `FiveMinVPINStrategy` internally, or is it purely registry-driven?** The YAML + hook setup suggests registry-driven, but confirm.
3. **Should `EvaluateWindowUseCase` be per-timeframe (5m/15m) or generic?** 15m has its own `fifteen_min.py` file — unclear if Phase 3 covers both. Likely yes, with a shared `EvaluateWindowUseCase` and timeframe-specific adapters.

---

*Author: Claude (Sonnet 4.6) via clean-architect agent pattern. Ready for human review before starting Phase 2 Task 2.1.*
