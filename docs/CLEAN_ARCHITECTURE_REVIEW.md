# Clean Architecture Review -- Novakash Engine

**Date:** 2026-04-08
**Reviewer:** Claude Opus 4.6 (Clean Architecture Specialist)
**Scope:** `engine/` directory -- all files listed in request
**Goal:** Actionable refactoring plan for v10 maintainability

---

## Executive Summary

The engine is a working, profitable trading system with real money on the line. That context matters -- the recommendations below prioritize stability and incremental improvement over rewrites. The biggest wins come from extracting responsibilities out of the two God Objects (`orchestrator.py` and `five_min_vpin.py`), not from theoretical purity.

**Critical findings:**
1. **Hardcoded API key** in `five_min_vpin.py` line 306 (Tiingo key in source)
2. **Hardcoded wallet address** in `orchestrator.py` line 2015 (Polymarket reconcile URL)
3. **Hardcoded IP address** `3.98.114.0` in 28 files for ML model server
4. Two 2500+ line files that each do 6-8 distinct jobs
5. v8/v9/v10 gate logic interleaved with fallback chains that are difficult to reason about
6. Duplicated CoinGlass veto logic (gates.py vs five_min_vpin.py lines 1774-1841)

---

## File-by-File Assessment

### 1. `engine/strategies/five_min_vpin.py` (~2,500 lines)

**Single Responsibility: FAILING**
This file does at least 7 distinct jobs:
- Window evaluation and delta calculation (lines 243-470) -- ~230 lines of multi-source price fetching
- v10 DUNE gate pipeline invocation (lines 547-614)
- v9 source agreement gate logic (lines 616-735) -- duplicates `gates.py`
- Signal evaluation with CG veto (lines 1629-1930) -- duplicates `gates.py`
- Window snapshot construction and DB writes (lines 772-998) -- ~230 lines of dict building
- Order execution with FOK/GTC fallback (lines 2059-2500) -- ~440 lines
- Skip history tracking and Telegram notification (lines 1330-1530) -- ~200 lines

**Dependencies: PROBLEMATIC**
- Line 55: `import os as _os` -- redundant, `os` already imported on line 23
- Line 288: `from config.runtime_config import runtime as _rt_cfg` -- imported inside method body, already available from line 35
- Line 556: `from signals.gates import ...` -- conditional import inside method, should be top-level
- Lines 2117, 2237: `import aiohttp as _aiohttp` -- repeatedly imported inline in different methods

**State Management: CONCERNING**
- `self._last_skip_reason` (line 601, 637, 704, etc.) -- mutable string used as cross-method communication channel. Set in one place, read in another, reset in a third. This is the source of several subtle bugs where the wrong skip reason propagates.
- `self._window_eval_history` (line 141) -- unbounded dict, cleaned only by timestamp heuristic (line 1396-1406)
- `self._v9_disagree_notified` (line 654) -- dynamically created set via `hasattr` check, cleaned by parsing window keys
- `self._recent_windows` (line 169) -- dynamically created via `hasattr`, accessed by orchestrator directly

**Error Handling: MIXED**
- Lines 341, 360-361, 533-534: Exceptions swallowed with `pass` or bare `except Exception` on critical price fetches. A Tiingo API failure silently falls through to Chainlink, which silently falls through to Binance, with no audit trail of which source was actually used.
- Line 897-898: v2 probability fetch failure logged at WARNING level with truncated error, then silently continues -- correct behavior for non-critical path.

**SECURITY -- CRITICAL:**
- **Line 306:** `_tiingo_api_key = "3f4456e457a4184d76c58a1320d8e1b214c3ab16"` -- hardcoded API key in source code. This must be moved to environment variables immediately.

**Dead Code:**
- Line 1970: `DEAD_CODE_REMOVED = True` -- marker variable, harmless but useless
- Lines 1965-1968: Duplicate comment block ("Guardrail Helpers" appears twice, lines 1965 and 1972)
- Line 74-75: `V81_ENTRY_CAPS` dict is declared but only used for backward compat -- can be removed if nothing reads it
- Lines 1907-1908: `timesfm_agreement = None` followed by passing it to log but never using it for logic

**Specific Issues:**
- Lines 562-563: `if 'delta_chainlink' in locals()` -- fragile pattern that checks whether a variable was defined earlier in the function. This means the function's control flow is so complex that the author can't guarantee which variables exist at which point.
- Lines 1370-1376: Same `if 'X' in locals()` pattern repeated for `_cl_dir`, `_ti_dir`, etc.
- Lines 722-733: `if not ((_v9_agreement and _v9_source_agree is False) or (_v9_caps and _v9_tier and "SKIP" in _v9_tier))` -- triple-negative boolean expression that is very hard to reason about.

---

### 2. `engine/strategies/orchestrator.py` (~3,074 lines)

**Single Responsibility: FAILING -- God Object**
The orchestrator owns at minimum 12 distinct responsibilities:
1. Component construction and DI wiring (lines 84-408) -- 325 lines in `__init__`
2. Startup sequencing (lines 410-780) -- 370 lines
3. Graceful shutdown (lines 787-852)
4. Feed callback routing (lines 862-998)
5. Signal callback routing (lines 1000-1069)
6. 5-minute window lifecycle management with countdown snapshots (lines 1071-1373) -- 300 lines of inline Telegram + DB + AI in `_on_five_min_window`
7. Resolution callback with AI analysis (lines 1448-1563)
8. 5-minute SITREP generation (lines 1720-1987) -- 270 lines of raw SQL and Telegram formatting inside `_heartbeat_loop`
9. Polymarket reconciliation (lines 2001-2153)
10. Position monitor with trade linking (lines 2454-2648) -- raw SQL inside loop
11. Shadow trade resolution with API calls (lines 2651-2958) -- 300 lines
12. Staggered execution queue (lines 2962-3074)

**Dependencies: PROBLEMATIC**
- Lines 276-307: Manual `.env` file parsing duplicated 3 times for `TIMESFM_ENABLED`, `TIMESFM_URL`, and `TIMESFM_MIN_CONFIDENCE`. This should use the Settings pydantic model.
- Line 328: `self._five_min_strategy._timesfm = self._timesfm_client` -- direct attribute injection bypassing constructor. Fragile coupling.
- Line 336: `self._five_min_strategy._timesfm_v2 = TimesFMV2Client(...)` -- same pattern.
- Line 1097: `self._five_min_strategy._pending_windows.append(window)` -- orchestrator directly manipulates strategy internal state.
- Line 1581: `self._db._pool` -- orchestrator accesses private `_pool` attribute of DBClient in 15+ places for raw SQL queries.

**State Management: CONCERNING**
- Lines 1137-1140: `if not hasattr(self, '_countdown_sent'): self._countdown_sent = {}` -- dynamically created attributes are a maintenance hazard.
- Line 1569-1575: Counter variables `_wallet_check_counter`, `_sitrep_counter` as local closure state in the heartbeat loop -- functional but brittle on restart.
- Lines 2460-2461: `_resolved_conditions: set = set()` and `_first_run = True` -- position monitor local state means restarts lose the "known resolved" set, though the `_first_run` guard handles this.

**SECURITY -- CRITICAL:**
- **Line 2015:** Hardcoded wallet address in URL: `"https://data-api.polymarket.com/activity?user=0x181d2ed714e0f7fe9c6e4f13711376edaab25e10&limit=20"`. This should come from `settings.poly_funder_address`.

**Error Handling: MIXED**
- Lines 1755-1771: Raw SQL in heartbeat with broad `try/except Exception: pass` -- a query failure silently results in zero wins/losses in the SITREP.
- Lines 2639-2640: `log.debug("position_monitor.error", ...)` -- actual errors logged at DEBUG level, making them invisible in production.

**Dead Code:**
- Lines 2233-2264: `_playwright_redeem_loop` -- appears to be replaced by `_redeemer_loop` (Builder Relayer). Both exist but only one is started based on config.
- Lines 1432-1446: `_evaluate_timesfm_window` -- TimesFM standalone strategy was disabled in v5.8 per comments.

**Inline API Calls:**
- Lines 1220-1236: Raw `aiohttp.ClientSession` + Anthropic API call constructed inline inside `_on_five_min_window`. This should be delegated to the existing `ClaudeEvaluator` or `DualAIAssessment`.
- Lines 1260-1276: Another raw Gamma API call inline inside the countdown snapshot helper.

---

### 3. `engine/signals/gates.py` (~290 lines) -- NEW, CLEAN

**Single Responsibility: PASSING**
Each gate does one thing. The pipeline composes them. This is the model for v10.

**Dependencies: CLEAN**
- Only imports `structlog`, `os`, `dataclasses`, `typing`, `Protocol`. No framework dependencies.

**State Management: GOOD**
- Gates are stateless. Context is passed via `GateContext` dataclass.
- One concern: `SourceAgreementGate.evaluate()` mutates `ctx.agreed_direction` (line 119). This is a side effect on the input parameter -- works but violates the principle that gates should return results, not mutate context. Consider returning the direction in `GateResult.data` and having the pipeline propagate it.

**Error Handling: GOOD**
- DUNE API errors pass through with clear reason (line 173).
- Missing data returns explicit "pass-through" results (line 157).

**Specific Issues:**
- Line 143: `self._min_p = float(os.environ.get("V10_DUNE_MIN_P", str(min_p)))` -- reading env vars in constructor is fine for configuration, but means the gate can't be reconfigured at runtime without reconstruction.
- Lines 315-319: `DynamicCapGate.__init__` reads 4 env vars. These should come from a config object or constructor args, not environment.

**Verdict:** This file is the architectural template for v10. Keep it clean.

---

### 4. `engine/execution/fok_ladder.py` (~228 lines) -- CLEAN

**Single Responsibility: PASSING**
Does exactly one thing: two-shot FAK price ladder execution.

**Dependencies: CLEAN**
- Only depends on `PolymarketClient` via TYPE_CHECKING import (no runtime coupling to implementation).

**State Management: GOOD**
- Stateless class -- all state is in the `FOKResult` return value.

**Error Handling: GOOD**
- Lines 166-181: Specific exception handling for "no orders found to match" (normal) vs "invalid amounts" (precision) vs unexpected errors. Each has appropriate logging and graceful degradation.

**Specific Issues:**
- Lines 83-85: `os.environ.get("ORDER_TYPE", "FAK")` read at execution time, not construction time. This means the order type can change between calls, which is either a feature or a bug depending on intent.
- Line 223: `_calc_size` has a 100-iteration loop to fix maker_amount precision -- works but could be replaced with a direct calculation.

**Verdict:** No changes needed. This is the standard for new execution code.

---

### 5. `engine/execution/polymarket_client.py` (~1,201 lines)

**Single Responsibility: ACCEPTABLE**
Handles both paper and live order placement. The paper simulation is tightly coupled to the live interface, which is the correct approach for a trading client.

**Dependencies: ACCEPTABLE**
- Imports `py_clob_client` only in live mode methods (lazy loading). Good.
- Lines 92-99: Reads `.env` file directly as fallback for `LIVE_TRADING_ENABLED`. Same pattern as orchestrator -- should use Settings.

**State Management: ACCEPTABLE**
- `self._paper_orders` dict for paper mode tracking.
- `self._live_first_trade_warned` for one-time warning -- appropriate safety measure.

**Error Handling: GOOD**
- Line 268: Hard cap with `ValueError` if stake exceeds `LIVE_MAX_TRADE_USD`.
- Lines 274-289: First live trade warning via `RuntimeWarning` -- good safety net.

**Specific Issues:**
- Lines 312-313: `is_15m = "15m" in market_slug` -- fragile string parsing for timeframe detection. Should be an explicit parameter.
- Lines 334-343: GTD expiry calculation by parsing window_ts from market_slug string. Fragile -- if slug format changes, this silently produces wrong expiry.
- Lines 373-378: Same maker_amount precision loop as `fok_ladder.py` -- duplicated code.

---

### 6. `engine/signals/timesfm_v2_client.py` (~90 lines) -- CLEAN

**Single Responsibility: PASSING**
HTTP client for v2.2 calibrated probability API. Does exactly one thing.

**Dependencies: CLEAN**
- `aiohttp` for HTTP, `logging` for output. Minimal and appropriate.

**State Management: GOOD**
- Session management with lazy creation (line 41).

**Specific Issues:**
- Line 24: `_DEFAULT_URL = os.environ.get("TIMESFM_V2_URL", "http://3.98.114.0:8080")` -- hardcoded IP at module level. This IP appears in 28 files. Should be a single constant in `config/constants.py` or better yet, only in `.env`.
- No retry logic for transient failures. For a critical trading signal, consider adding 1 retry with exponential backoff.

**Verdict:** Clean file, minor improvements only.

---

### 7. `engine/persistence/db_client.py` (~1,000+ lines)

**Single Responsibility: BORDERLINE**
It is a persistence layer, but it does too many things:
- Trade writes (core)
- Signal writes (core)
- System state management (should be separate)
- Playwright state management (should be separate)
- Window snapshot writes with 82(!) positional parameters (lines 646-684)
- Schema migration via `ensure_*_tables()` methods (should be separate)
- Read helpers for prices, macros, CLOB data

**Dependencies: CLEAN**
- `asyncpg` for PostgreSQL, `structlog` for logging. Appropriate.

**State Management: GOOD**
- Connection pool pattern is correct.
- `_assert_pool()` guard on all methods.

**Error Handling: MIXED**
- Lines 283-285: `update_gamma_prices` catches all exceptions with `pass`. If the update fails, no one knows.
- Lines 147, 196, 248: Core methods properly re-raise after logging. Good.
- Line 272-273: `update_heartbeat` correctly doesn't re-raise (heartbeat failure is non-fatal). Good comment.

**Specific Issues:**
- Lines 646-684: `write_window_snapshot` takes 82 positional parameters (`$1` through `$82`). This is extremely fragile -- adding or removing a column requires updating parameter indices everywhere. Should use named parameters or a dict-based approach.
- Lines 370-396: `ensure_playwright_tables` creates tables inline with SQL strings. This should be in an Alembic migration.
- Lines 547-589: `ensure_window_tables` has 30+ `ALTER TABLE ADD COLUMN IF NOT EXISTS` calls for schema evolution. This works but is a maintenance burden that grows linearly.

---

### 8. `engine/alerts/telegram.py` (~1,000+ lines)

**Single Responsibility: BORDERLINE**
This is both a notification system AND an AI analysis system. The `DualAIAssessment` class and all the AI-related prompting should be extracted.

**Dependencies: ACCEPTABLE**
- `aiohttp` for Telegram API and AI calls. Reasonable for a notification service.

**State Management: ACCEPTABLE**
- Session counters (`_session_wins`, `_session_losses`, `_session_pnl`) are dynamically created via `hasattr` checks (line 937). Should be initialized in `__init__`.
- `_db_client` set via `set_db_client()` after construction -- necessary for chicken-and-egg initialization.

**Error Handling: GOOD**
- Every `send_*` method wraps in try/except and returns None on failure. Telegram failures never crash the engine. Correct.

**Specific Issues:**
- Line 112: `DualAIAssessment` class instantiated inline. This AI assessment logic (Claude + Qwen fallback) should be its own module.
- Lines 349-439: `_group_reasons()` is a 90-line function defined inside `send_window_summary()`. This is complex presentation logic that should be a standalone utility.
- Lines 129-274: `send_trade_decision_detailed` is 145 lines of string building. Hard to maintain when notification format changes.

---

## Prioritized Refactoring Recommendations

### P0 -- Security (Do Immediately)

| # | Issue | File | Line | Fix |
|---|-------|------|------|-----|
| 1 | **Hardcoded Tiingo API key** | `five_min_vpin.py` | 306 | Move to `Settings.tiingo_api_key` and env var `TIINGO_API_KEY` |
| 2 | **Hardcoded wallet address** | `orchestrator.py` | 2015 | Use `self._settings.poly_funder_address` |
| 3 | **Hardcoded ML server IP** | 28 files | various | Single constant in `config/constants.py` or env var only |

### P1 -- High Impact, Low Risk (v10 Sprint 1)

| # | Recommendation | Effort | Impact |
|---|---------------|--------|--------|
| 4 | **Extract DeltaCalculator** from `five_min_vpin.py` lines 281-467. Create `engine/signals/delta_calculator.py` with methods `compute_multi_source_delta(window, db) -> DeltaResult`. This is a pure data transformation with no business logic coupling -- safe to extract. | 2h | Removes 200 lines from the God Method |
| 5 | **Extract WindowSnapshotBuilder** from `five_min_vpin.py` lines 772-998. Create `engine/persistence/snapshot_builder.py` that takes signal + window + delta + CG data and returns a flat dict. Currently 230 lines of dict construction inline. | 2h | Removes 230 lines, makes snapshot schema explicit |
| 6 | **Extract SitrepGenerator** from `orchestrator.py` lines 1720-1987. Create `engine/alerts/sitrep.py`. The 270-line SITREP builder has zero coupling to orchestrator logic -- it only reads from DB and risk_manager. | 2h | Removes 270 lines from heartbeat loop |
| 7 | **Extract PositionMonitor** from `orchestrator.py` lines 2454-2648. Create `engine/execution/position_monitor.py`. Self-contained polling loop with its own state (`_resolved_conditions`). | 2h | Removes 200 lines, testable in isolation |
| 8 | **Extract ShadowResolver** from `orchestrator.py` lines 2651-2958. Create `engine/execution/shadow_resolver.py`. Already a standalone loop with no orchestrator coupling beyond `self._db`, `self._alerter`. | 2h | Removes 300 lines, testable in isolation |
| 9 | **Unify CoinGlass veto logic**. The CG veto in `gates.py` lines 216-300 and `five_min_vpin.py` lines 1774-1841 implement the same logic with different thresholds. When v10 gates are enabled, the inline veto is dead code. Remove it and use only the gate pipeline. | 1h | Eliminates duplicated business logic |

### P2 -- Medium Impact, Medium Risk (v10 Sprint 2)

| # | Recommendation | Effort | Impact |
|---|---------------|--------|--------|
| 10 | **Remove v8/v9 fallback paths** once v10 DUNE gates are validated. Currently `five_min_vpin.py` has a `_v10_enabled` flag (line 550) that controls whether v10 gates run or v9 inline gates run. Once v10 is proven, delete lines 614-735 (v9 agreement), lines 682-735 (v9 caps), and lines 1218-1329 (v8.1 early entry gates). This removes ~400 lines. | 3h | Major complexity reduction |
| 11 | **Replace `_last_skip_reason` string passing** with a proper result type. Create `@dataclass class EvaluationResult: signal: Optional[FiveMinSignal]; skip_reason: str; gate_results: list`. Return this from `_evaluate_window` instead of setting/reading `self._last_skip_reason` across method boundaries. | 3h | Eliminates the most error-prone state management pattern |
| 12 | **Replace 82-parameter snapshot INSERT** in `db_client.py` with a dict-based approach using `asyncpg`'s `$1::jsonb` or at minimum named parameter mapping. The current positional approach will break silently if parameters shift. | 2h | Prevents insidious DB write bugs |
| 13 | **Extract countdown snapshot logic** from `orchestrator.py` `_on_five_min_window` (lines 1128-1343). This 215-line block manages T-240/210/180/150/120/90/70 snapshot notifications with inline Gamma API calls and Claude AI prompts. It should be a `CountdownNotifier` class. | 3h | Removes 215 lines from orchestrator |
| 14 | **Consolidate .env file parsing**. `orchestrator.py` has 3 copies of manual `.env` parsing (lines 279-307). `polymarket_client.py` has another (lines 92-99). All should use the existing `Settings` pydantic model. | 1h | DRY, single source of truth for config |

### P3 -- Low Priority, Architectural Improvements (Post v10)

| # | Recommendation | Effort | Impact |
|---|---------------|--------|--------|
| 15 | **Split `Orchestrator.__init__`** (325 lines) into a builder pattern or factory methods: `_create_feeds()`, `_create_strategies()`, `_create_execution()`. The constructor is doing too much, making it impossible to test components in isolation. | 4h | Testability |
| 16 | **Extract AI assessment** from `telegram.py` into `engine/evaluation/dual_ai.py`. The `DualAIAssessment` class and all AI prompting (Claude + Qwen fallback) is presentation-layer AI, not notification logic. | 2h | Separation of concerns |
| 17 | **Add retry logic** to `timesfm_v2_client.py`. For a signal that gates real money trades, a single timeout should not silently skip the gate. Add 1 retry with 2s backoff. | 1h | Reliability |
| 18 | **Replace `hasattr` checks** for dynamically created attributes: `_countdown_sent` (orchestrator.py:1137), `_recent_windows` (five_min_vpin.py:169), `_v9_disagree_notified` (five_min_vpin.py:654), `_session_wins` (telegram.py:937). Initialize all in `__init__`. | 30m | Predictability |
| 19 | **Fix DEBUG-level error logging** in `orchestrator.py` line 2640: `log.debug("position_monitor.error", ...)`. Actual errors should be `log.error` or `log.warning`. | 5m | Observability |
| 20 | **Remove duplicate maker_amount precision loop**. Both `fok_ladder.py` (line 223) and `polymarket_client.py` (lines 373-378) have identical logic. Extract to a shared `clob_utils.calc_clob_size(price, stake) -> float`. | 30m | DRY |

---

## Dependency Graph Issues

```
orchestrator.py
  |-- five_min_vpin.py (direct attribute manipulation: ._pending_windows, ._recent_windows, ._timesfm, ._timesfm_v2, ._tick_recorder)
  |-- db_client.py (accesses ._pool directly in 15+ places for raw SQL)
  |-- telegram.py (accesses ._anthropic_api_key for inline AI calls)
  |-- risk_manager.py (accesses ._paper_mode directly)

five_min_vpin.py
  |-- gates.py (conditional import, only when V10_DUNE_ENABLED)
  |-- db_client.py (for price lookups, snapshot writes)
  |-- telegram.py (for window/trade notifications)
  |-- fok_ladder.py (for execution)
  |-- polymarket_client.py (for order placement)
```

The main coupling issue is the orchestrator directly manipulating internal state of the strategy object. This means:
- You cannot test `FiveMinVPINStrategy` without stubbing the attributes the orchestrator injects
- Order of attribute injection matters and is undocumented
- The strategy's `__init__` signature does not reflect its actual dependencies

**Fix:** Pass all dependencies through the constructor. Where timing prevents this (e.g., DB pool not ready at init time), use an explicit `inject_late_dependencies(timesfm_v2, tick_recorder)` method instead of direct attribute assignment.

---

## What v10 Should Look Like

The `gates.py` file demonstrates the target architecture:

```
Window Signal arrives
    |
    v
DeltaCalculator.compute(window, sources) -> DeltaResult
    |
    v
GatePipeline.evaluate(GateContext) -> PipelineResult
    |-- SourceAgreementGate
    |-- DuneConfidenceGate
    |-- CoinGlassVetoGate
    |-- DynamicCapGate
    |
    v
if passed: ExecutionService.execute(signal, cap) -> ExecutionResult
    |-- FOKLadder (live)
    |-- GTC fallback (live)
    |-- Paper simulation (paper)
    |
    v
SnapshotBuilder.build(window, delta, gates, signal) -> dict
    |
    v
DBClient.write_window_snapshot(snapshot)
NotificationService.send_window_report(snapshot)
```

Each box is a single file, testable in isolation, with clear input/output contracts. The orchestrator's only job is to wire them together and manage lifecycle.

---

## Summary

| Category | Score | Notes |
|----------|-------|-------|
| `gates.py` | A | Model for v10 architecture |
| `fok_ladder.py` | A | Clean, focused, well-tested |
| `timesfm_v2_client.py` | A- | Clean, needs retry logic |
| `polymarket_client.py` | B | Solid but some string parsing fragility |
| `telegram.py` | B- | Works but mixes notification + AI + presentation |
| `db_client.py` | B- | Core is solid, snapshot method is fragile |
| `five_min_vpin.py` | C | God Method, duplicated logic, hardcoded key |
| `orchestrator.py` | C- | God Object, 12 responsibilities, raw SQL inline |

The path to a maintainable v10: extract 5-6 focused modules from the two C-grade files, delete the v8/v9 fallback paths once v10 gates are validated, and fix the three security issues immediately.
