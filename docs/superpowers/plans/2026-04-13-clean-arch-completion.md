# Clean Architecture Completion Plan

**Date:** 2026-04-13
**Branch:** `clean-arch-polymarket`
**Worktree:** `/Users/billyrichards/Code/novakash-clean-arch`
**Reviewer:** Claude Opus 4.6

---

## 1. Current State Assessment

### What Actually Exists (more than described in task)

The worktree is significantly further along than the task description suggests. Here is the real inventory:

**Domain Layer (COMPLETE, ~2,500 lines)**
- 21 files across `domain/`
- Value objects: `StrategyContext` (30+ fields), `StrategyDecision`, `V4Snapshot`, `GateContext` (100+ fields), `GateResult`, `PipelineResult`, plus 15+ supporting VOs
- Ports: 15 ports in `domain/ports.py` (MarketFeedPort, ConsensusPricePort, SignalRepository, PolymarketClientPort, AlerterPort, Clock, WindowStateRepository, ConfigPort, TradeRepository, RiskManagerPort, SystemStateRepository, ManualTradeRepository, StrategyPort, V4SnapshotPort, StrategyDecisionRepository)
- Entities: `IStrategy` (abstract), `IGate` (abstract)
- Services: `GatePipeline` with 8 concrete gates
- Enums: 5 enums (Action, Direction, Confidence, Regime, StrategyMode)
- Exceptions: 8 domain exceptions

**Use Cases (PARTIALLY COMPLETE, 5 files, ~1,420 lines)**
- `evaluate_strategies.py` (520 lines) -- PRODUCTION WIRED, runs all registered strategies
- `evaluate_window.py` (900 lines) -- PRODUCTION WIRED, full V10 pipeline + window evaluation
- `execute_manual_trade.py` (281 lines) -- NOT wired, clean implementation
- `publish_heartbeat.py` (240 lines) -- NOT wired, clean implementation
- `reconcile_positions.py` (245 lines) -- NOT wired, clean implementation

**Adapter Strategies (COMPLETE, 4 strategies)**
- `v4_fusion_strategy.py` (356 lines) -- parent class, polymarket_v2 + legacy paths
- `v4_down_only_strategy.py` (211 lines) -- production LIVE, DOWN-only + CLOB sizing
- `v4_up_asian_strategy.py` (119 lines) -- NON-FUNCTIONAL (0 trades)
- `v10_gate_strategy.py` (184 lines) -- wraps domain GatePipeline

**Adapter Persistence (COMPLETE, 5 repositories, ~2,785 lines)**
- `pg_signal_repo.py` (660 lines)
- `pg_strategy_decisions.py` (163 lines)
- `pg_system_repo.py` (418 lines)
- `pg_trade_repo.py` (675 lines)
- `pg_window_repo.py` (845 lines)

**Adapter External Services**
- `v4_snapshot_http.py` -- V4SnapshotPort adapter
- `adapters/alert/telegram.py` -- AlerterPort adapter
- `adapters/clock/system_clock.py` -- Clock adapter
- `adapters/consensus/three_source.py` -- ConsensusPricePort adapter
- `adapters/market_feed/` -- 4 feed adapters (binance_ws, chainlink_db, tiingo_db, tiingo_rest)
- `adapters/polymarket/` -- live_client.py, paper_client.py
- `adapters/prediction/` -- timesfm_v1.py, timesfm_v2.py

**Tests (PARTIAL, ~1,796 lines)**
- `tests/unit/use_cases/` -- 5 test files (evaluate_strategies, evaluate_window, execute_manual_trade, publish_heartbeat, reconcile_positions)
- `tests/unit/strategies/` -- 2 test files (v10_gate, v4_fusion)
- `tests/unit/domain/` -- 1 test file (value_objects)
- `tests/unit/signals/` -- 1 test file (gate_pipeline_immutable)

**Composition Root**
- The orchestrator (`strategies/orchestrator.py`, 3,888 lines) already wires `EvaluateStrategiesUseCase` and `EvaluateWindowUseCase` into the production loop
- Strategy registration uses `StrategyRegistration` VOs
- Hot-reload support via `runtime_config`

### What Actually Exists in `application/ports/`

7 port interfaces that DUPLICATE domain ports:
- `IStrategyRepository`, `ISignalRepository`, `IWindowRepository`, `IPriceFeed`, `IDUNEClient`, `IPolymarketClient`, `IConfigPort`

These are vestigial -- the domain-layer ports in `domain/ports.py` are the canonical interfaces. The application-layer ports are unused.

---

## 2. Domain Layer Review

### Quality Assessment: GOOD with caveats

**Strengths:**
1. Zero external dependencies -- pure Python dataclasses only
2. Frozen/immutable value objects throughout
3. Clean port definitions with detailed docstrings
4. Gate pipeline is well-structured with short-circuit evaluation
5. `StrategyContext` captures the full decision surface (30+ fields)
6. `GateContext` captures the full signal surface (100+ fields for v2/v3/v4 data)

**Issues Found:**

1. **DUPLICATE VALUE OBJECTS**: There are two parallel definitions of core types:
   - `domain/value_objects.py` (root file, 478 lines) -- used by `domain/ports.py`
   - `domain/value_objects/strategy_types.py` -- used by strategies and use cases
   - `domain/value_objects/signal_types.py` -- used by gates

   These define the SAME types (`StrategyContext`, `StrategyDecision`, `V4Snapshot`, etc.) with slightly different field sets. The root `value_objects.py` has `EvaluateStrategiesResult` as a mutable dataclass (missing `frozen=True`), while `strategy_types.py` has it as frozen. This is a reconciliation hazard.

   **FIX**: Delete `domain/value_objects.py` (root). Update `domain/ports.py` to import from `domain/value_objects/` package. The package `__init__.py` should re-export everything.

2. **STUB VALUE OBJECTS**: Several VOs in `domain/value_objects.py` are still stubs with `pass` bodies:
   - `Tick`, `WindowClose`, `DeltaSet`, `SignalEvaluation`, `ClobSnapshot`, `GateAuditRow`, `WindowSnapshot`, `OrderBook`, `TradeDecision`, `SkipSummary`

   These are referenced by port method signatures but have no fields. Any adapter calling these ports would need to construct empty frozen objects.

   **FIX**: Flesh out stub VOs before wiring adapters. Each one needs the fields that the corresponding repository/port method requires.

3. **`GateContext` vs `StrategyContext` overlap**: Both are "bag of market data" objects. `GateContext` (270+ fields with v3 multi-horizon) is used by gates. `StrategyContext` (30+ fields) is used by strategies. The V10GateStrategy adapter has to manually map between them (`_build_gate_context`). This is acceptable for now but consider whether `GateContext` should be a superset of `StrategyContext` to avoid the mapping boiler plate.

4. **`cg_snapshot: Optional[object]` in StrategyContext**: Using `object` type annotation loses all type safety. Should be `Optional[dict]` (matching the actual usage) or a proper `CoinGlassSnapshot` VO.

5. **APPLICATION PORTS ARE DEAD CODE**: The 7 files in `application/ports/` duplicate domain ports and are imported by nothing. Delete them.

6. **Gate `evaluate()` is sync, but V10GateStrategy wraps it async**: The `IGate.evaluate()` signature is synchronous, but `V10GateStrategy.evaluate()` is async. The production `signals/gates.py` GatePipeline has an async `evaluate()` method. The domain `GatePipeline` has a sync `evaluate()`. This means the domain pipeline cannot call gates that need async (like the DuneConfidenceGate which calls the DUNE model). The V10GateStrategy in the adapter layer currently imports from the OLD `signals/gates.py` (which has async), not from the domain.

   **FIX**: Either make domain gates async-compatible, or accept that the V10GateStrategy will always use the production gates module directly (which is the current state and is acceptable for the adapter layer).

---

## 3. Architecture Violations to Fix

### Priority 1: Eliminate Duplicate Types

The root `domain/value_objects.py` and the package `domain/value_objects/` both exist. The package is the proper one. Fix the import chain:

```
domain/ports.py
  currently imports from: domain.value_objects (root file)
  should import from:     domain.value_objects.strategy_types, domain.value_objects.signal_types, etc.
```

### Priority 2: Clean Up Application Layer

Delete `engine/application/` entirely (7 unused port files + empty `dto/` and `use_cases/` dirs). The real use cases are in `engine/use_cases/` and the real ports are in `domain/ports.py`.

### Priority 3: Use Cases Have Infrastructure Leakage

`evaluate_strategies.py` imports:
- `config.runtime_config` (infrastructure concern)
- `os.environ` directly for feature flags

`evaluate_window.py` imports:
- `config.constants`, `config.runtime_config` (infrastructure)
- `data.feeds.polymarket_5min.WindowInfo` (infrastructure type)
- `data.models.MarketState` (infrastructure type)
- `signals.gates.*` (adapter-layer gate implementations)
- `aiohttp` directly for Tiingo API calls

These are the biggest violations. The use cases should depend ONLY on domain ports and VOs.

**Pragmatic fix**: This is a trading system in production. The `evaluate_window.py` use case is 900 lines of deeply entangled logic that was extracted from the orchestrator. Rewriting it cleanly is a multi-day effort. Accept it as "application layer with known debt" and focus new work on the cleaner patterns (like `execute_manual_trade.py` and `reconcile_positions.py` which are properly clean).

---

## 4. Remaining Work -- Prioritized Implementation Plan

### Phase A: Housekeeping (1-2 hours)

**Goal**: Clean up the duplicate types and dead code before building on top.

1. **Reconcile value objects**
   - Ensure `domain/value_objects/__init__.py` re-exports all types from sub-modules
   - Update `domain/ports.py` to import from the package, not the root file
   - Delete `domain/value_objects.py` (root file) or convert it to a pure re-export module
   - Verify no import breakage

2. **Delete dead application ports**
   - Remove `engine/application/ports/` (7 files) -- unused
   - Remove empty `engine/application/dto/` and `engine/application/use_cases/`
   - If `engine/application/__init__.py` is the only survivor, consider deleting the whole directory

3. **Fix `EvaluateStrategiesResult` mutability**
   - The version in `domain/value_objects.py` uses `@dataclass` (mutable)
   - The version in `strategy_types.py` uses `@dataclass(frozen=True)` (correct)
   - Ensure only the frozen version survives

### Phase B: v4_up_basic Strategy (2-3 hours)

**Goal**: Implement the new UP strategy that replaces the broken v4_up_asian.

**Location**: `engine/adapters/strategies/v4_up_basic_strategy.py`

**Implementation** (follows the spec in `docs/V4_UP_BASIC_STRATEGY.md`):

```python
class V4UpBasicStrategy(V4FusionStrategy):
    strategy_id = "v4_up_basic"
    version = "1.0.0"

    # Core gates (from spec):
    # 1. Direction: UP only
    # 2. Confidence: dist >= 0.10 (p_up >= 0.60)
    # 3. Timing: T-60 to T-180 (wider than DOWN's T-90-T-150)
    # 4. No session restriction (all hours)
    # 5. Override parent's timing=early block (same pattern as v4_down_only)
    # 6. Override parent's confidence < 0.12 block (same pattern as v4_down_only)
```

**Key difference from spec**: The spec's pseudocode references `ctx.timesfm_direction` which doesn't exist on `StrategyContext`. The TimesFM gate should be deferred to Phase D (post-validation). Start with the simpler version.

**Registration**: Add to orchestrator's strategy list with `mode="GHOST"` initially.

**Testing**:
- Test UP signal trades at T-120 (happy path)
- Test DOWN signal skips
- Test timing gate boundaries (T-59, T-60, T-180, T-181)
- Test confidence gate (dist=0.09 skip, dist=0.10 trade)
- Test parent timing=early override for T-150-180 range
- Test parent confidence=0.12 override for 0.10-0.12 range

### Phase C: Wire Remaining Use Cases (4-6 hours)

**Goal**: Wire `execute_manual_trade`, `publish_heartbeat`, and `reconcile_positions` into the orchestrator alongside the existing god-class code.

These three use cases are ALREADY cleanly implemented. They just need:

1. **Adapter implementations** for their port dependencies that don't already exist:
   - `ManualTradeRepository` -- needs a PG adapter (wrap existing DBClient methods)
   - `RiskManagerPort` -- needs an adapter wrapping `execution.risk_manager.RiskManager`

2. **Feature-flagged wiring** in the orchestrator:
   ```python
   # In Orchestrator.__init__:
   if os.environ.get("USE_CLEAN_MANUAL_TRADE", "false") == "true":
       self._manual_trade_uc = ExecuteManualTradeUseCase(...)
   ```

3. **Parallel run** with decision comparison logging for 48 hours.

**Existing adapters that can be reused**:
- `adapters/alert/telegram.py` -> AlerterPort
- `adapters/clock/system_clock.py` -> Clock
- `adapters/persistence/pg_trade_repo.py` -> TradeRepository
- `adapters/persistence/pg_window_repo.py` -> WindowStateRepository
- `adapters/persistence/pg_system_repo.py` -> SystemStateRepository
- `adapters/polymarket/live_client.py` + `paper_client.py` -> PolymarketClientPort

### Phase D: Domain Gate Tests (3-4 hours)

**Goal**: Comprehensive test coverage for the 8 domain gates + pipeline.

Current: `test_gate_pipeline_immutable.py` (229 lines) -- tests pipeline behavior.
Missing: Individual gate tests.

**Test matrix** (8 gates x ~8 cases each = ~64 tests):

| Gate | Key Cases |
|------|-----------|
| EvalOffsetBounds | None, <5 (expired), 5 (boundary), 150 (mid), 300 (boundary), >300 (too early) |
| SourceAgreement | All agree UP, all agree DOWN, 2/3 agree, all disagree, 1 source only, 0 sources |
| DeltaMagnitude | Above threshold, below threshold, exactly at threshold, zero delta |
| TakerFlow | Aligned, opposed (still passes), no data (pass-through) |
| CGConfirmation | OI+liq confirm, OI only, liq only, neither, no CG data |
| DuneConfidence | High confidence, low confidence, no probability, V4 vs v2 fallback |
| Spread | Narrow spread, wide spread, no CLOB data (pass-through), one-sided |
| DynamicCap | High confidence cap, medium, low, no V4 data |

### Phase E: Strategy Tests (2-3 hours)

**Goal**: Test all 5 strategy implementations.

Current: `test_v4_fusion_strategy.py` (210 lines), `test_v10_gate_strategy.py` (165 lines).
Missing: v4_down_only, v4_up_asian, v4_up_basic tests.

**Priority order**:
1. `v4_down_only` -- production strategy, highest risk
2. `v4_up_basic` -- new strategy, needs validation before deployment
3. `v4_up_asian` -- broken, low priority (being replaced)

**Key test cases for v4_down_only**:
- DOWN signal in T-90-150 window -> TRADE with CLOB sizing
- UP signal -> SKIP (direction filter)
- DOWN signal outside timing window -> SKIP
- CLOB ask >= 0.55 -> 2.0x sizing
- CLOB ask 0.35-0.55 -> 1.2x sizing
- CLOB ask < 0.25 -> SKIP (not tradeable)
- Null CLOB -> 1.5x sizing (strong moves)
- Parent timing=early override for T-90-150
- Parent confidence=0.12 override for dist>=0.10
- Max collateral cap at 10%

### Phase F: Integration Wiring Cleanup (2-3 hours)

**Goal**: Ensure the composition root in the orchestrator is clean.

The orchestrator is 3,888 lines. The strategy wiring section already creates `EvaluateStrategiesUseCase` with proper DI. But the wiring is buried in the constructor among thousands of lines.

**Action items**:
1. Extract a `_build_strategy_registry()` method in the orchestrator
2. Extract a `_build_use_cases()` method
3. Add `v4_up_basic` to the strategy registry (GHOST mode)
4. Add feature flags for the remaining 3 use cases
5. Document the wiring in a docstring

### Phase G: Stub VO Completion (1-2 hours)

**Goal**: Flesh out the 10 stub value objects that have `pass` bodies.

These are needed if/when the SignalRepository and other ports get properly wired. The current adapter implementations (pg_signal_repo.py etc.) work with raw dicts, bypassing the VOs. When we want to enforce type safety at the boundary, these need real fields.

**Order of priority** (by which ones are needed soonest):
1. `GateAuditRow` -- used by `SignalRepository.write_gate_audit()`
2. `SignalEvaluation` -- used by `SignalRepository.write_signal_evaluation()`
3. `WindowSnapshot` -- used by `SignalRepository.write_window_snapshot()`
4. `ClobSnapshot` -- used by `SignalRepository.write_clob_snapshot()`
5. `DeltaSet` -- used by `ConsensusPricePort.get_deltas()`
6. `Tick` -- used by `MarketFeedPort.get_latest_tick()`
7. `WindowClose` -- used by `MarketFeedPort.subscribe_window_close()`
8. `OrderBook` -- used by `PolymarketClientPort.get_book()`
9. `TradeDecision` -- used by `AlerterPort.send_trade_alert()`
10. `SkipSummary` -- used by `AlerterPort.send_skip_summary()`

---

## 5. Implementation Timeline

| Phase | Effort | Dependencies | Deliverables |
|-------|--------|-------------|-------------|
| A: Housekeeping | 1-2h | None | Clean imports, no dead code |
| B: v4_up_basic | 2-3h | Phase A | New strategy + tests, GHOST mode |
| C: Wire use cases | 4-6h | Phase A | 3 use cases feature-flagged in orchestrator |
| D: Gate tests | 3-4h | None (parallel) | ~64 gate unit tests |
| E: Strategy tests | 2-3h | Phase B | ~30 strategy unit tests |
| F: Wiring cleanup | 2-3h | Phases A-C | Clean composition root |
| G: Stub VOs | 1-2h | None (parallel) | 10 VOs fleshed out |

**Total: 15-23 hours** (roughly 3-4 focused sessions)

**Critical path**: A -> B -> (register in orchestrator) -> deploy as GHOST -> 3-5 days paper validation

**Parallelizable**: D and G can run alongside B and C.

---

## 6. Deployment Strategy

### Step 1: Deploy v4_up_basic in GHOST mode (Day 1)

```python
# In orchestrator strategy registry:
StrategyRegistration(
    strategy_id="v4_up_basic",
    mode="GHOST",     # Evaluate but don't execute
    enabled=True,
    priority=30,
)
```

All decisions get written to `strategy_decisions` table. No trades placed. Monitor via:
```sql
SELECT action, COUNT(*), AVG(confidence_score)
FROM strategy_decisions
WHERE strategy_id = 'v4_up_basic'
  AND evaluated_at > NOW() - INTERVAL '24 hours'
GROUP BY action;
```

### Step 2: Validate decision quality (Days 2-5)

- Check v4_up_basic generates 5-15 TRADE decisions per day
- Cross-reference against resolved windows to estimate WR
- Compare confidence distribution against v4_down_only

### Step 3: Promote to LIVE paper mode (Day 6)

```python
# Via runtime config or env var:
V4_UP_BASIC_MODE=LIVE
PAPER_MODE=true
```

Executes paper trades. Monitor fill rates, CLOB pricing, and sizing.

### Step 4: Live deployment (Day 10+)

Only after:
- 100+ paper trades executed
- Estimated WR >= 65%
- No decision mismatches or errors
- Sizing behavior validated against CLOB data

---

## 7. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Import breakage from Phase A cleanup | Medium | High | Run full test suite after each file change |
| v4_up_basic WR below 65% | Medium | Low | GHOST mode catches this before any capital at risk |
| Orchestrator wiring breaks existing strategies | Low | Critical | Feature flags for ALL new code paths |
| Performance regression from running 5 strategies per eval | Low | Medium | Strategies run in parallel via asyncio.gather, 5s timeout |
| Stub VO field mismatch with actual DB schema | Medium | Low | Compare against pg_*_repo.py SQL column lists |

---

## 8. Architecture Debt Register

These items are acceptable for now but should be addressed eventually:

| ID | Debt | Location | Priority |
|----|------|----------|----------|
| TD-01 | `evaluate_window.py` imports infrastructure types directly | `use_cases/evaluate_window.py` | Low (legacy path, will be replaced) |
| TD-02 | `evaluate_strategies.py` reads `os.environ` for feature flags | `use_cases/evaluate_strategies.py` | Medium (should use ConfigPort) |
| TD-03 | `StrategyContext.cg_snapshot` typed as `Optional[object]` | `domain/value_objects/strategy_types.py` | Low (works, just loses type info) |
| TD-04 | V10GateStrategy imports from `signals/gates.py` (production code) not domain | `adapters/strategies/v10_gate_strategy.py` | Low (acceptable for adapter layer) |
| TD-05 | Orchestrator is 3,888 lines (god class) | `strategies/orchestrator.py` | High (but not blocking) |
| TD-06 | `domain/value_objects.py` root file duplicates package types | `domain/value_objects.py` | High (fix in Phase A) |
| TD-07 | `application/ports/` directory is dead code | `application/ports/` | High (fix in Phase A) |

---

## 9. Success Criteria

Phase A-B complete when:
- [ ] Zero import errors across the codebase
- [ ] No duplicate type definitions
- [ ] v4_up_basic strategy passes all unit tests
- [ ] v4_up_basic registered in orchestrator as GHOST
- [ ] Existing v4_down_only and v10_gate strategies unaffected

Full completion when:
- [ ] All 5 strategies have unit tests (30+ tests)
- [ ] All 8 gates have unit tests (64+ tests)
- [ ] 3 remaining use cases wired with feature flags
- [ ] v4_up_basic validated with 100+ GHOST decisions
- [ ] Zero regressions in existing production behavior

---

## 10. Key File Reference

### Domain Layer (read these first)
- `/Users/billyrichards/Code/novakash-clean-arch/engine/domain/value_objects/strategy_types.py` -- StrategyContext, StrategyDecision (canonical)
- `/Users/billyrichards/Code/novakash-clean-arch/engine/domain/value_objects/signal_types.py` -- GateContext, V4Snapshot, GateResult (canonical)
- `/Users/billyrichards/Code/novakash-clean-arch/engine/domain/ports.py` -- 15 port interfaces
- `/Users/billyrichards/Code/novakash-clean-arch/engine/domain/entities/strategy.py` -- IStrategy abstract base
- `/Users/billyrichards/Code/novakash-clean-arch/engine/domain/services/gate_pipeline.py` -- 8 gates + pipeline

### Use Cases (the actual application layer)
- `/Users/billyrichards/Code/novakash-clean-arch/engine/use_cases/evaluate_strategies.py` -- multi-strategy orchestration (WIRED)
- `/Users/billyrichards/Code/novakash-clean-arch/engine/use_cases/evaluate_window.py` -- V10 pipeline (WIRED, has debt)
- `/Users/billyrichards/Code/novakash-clean-arch/engine/use_cases/execute_manual_trade.py` -- CLEAN, not wired
- `/Users/billyrichards/Code/novakash-clean-arch/engine/use_cases/publish_heartbeat.py` -- CLEAN, not wired
- `/Users/billyrichards/Code/novakash-clean-arch/engine/use_cases/reconcile_positions.py` -- CLEAN, not wired

### Strategies (adapter layer)
- `/Users/billyrichards/Code/novakash-clean-arch/engine/adapters/strategies/v4_fusion_strategy.py` -- parent class
- `/Users/billyrichards/Code/novakash-clean-arch/engine/adapters/strategies/v4_down_only_strategy.py` -- production LIVE
- `/Users/billyrichards/Code/novakash-clean-arch/engine/adapters/strategies/v4_up_asian_strategy.py` -- BROKEN (being replaced)
- `/Users/billyrichards/Code/novakash-clean-arch/engine/adapters/strategies/v10_gate_strategy.py` -- gate pipeline adapter

### Composition Root
- `/Users/billyrichards/Code/novakash-clean-arch/engine/strategies/orchestrator.py` -- 3,888 line god class, wires everything
