# Repo-Wide Clean Architecture Audit (v3)

**Date:** 2026-04-11
**Auditor:** Claude Opus 4.6 (automated, source-code-level review)
**Branch audited:** `develop` (239 commits ahead of `main`)
**Baseline:** `margin_engine/` (Clean Architecture reference implementation)
**Method:** Every file read at source level via `cat -n` -- not file-name inference.

---

## Executive Summary

The novakash repo contains **~112k LOC of Python** across 5 backend services and **~64k LOC of JSX/JS** in the frontend. The `margin_engine/` module is the gold standard -- clean 4-layer architecture with ports, adapters, domain entities, and use cases. The `engine/` module (Polymarket trading) is the inverse -- a 2,566-line god class (`db_client.py`), a 3,579-line orchestrator, a 3,109-line strategy, and domain logic scattered across execution, signal, and strategy layers with no separation of concerns. The `hub/` (FastAPI dashboard) is a pragmatic 3-layer MVC that works but has controller-level business logic and inline DDL. Peripheral services (`macro-observer`, `data-collector`) are fine as-is -- single-purpose scripts with no architectural ambition.

**Grading scale:** A (clean arch), B (pragmatic, minor debt), C (needs work), D (structural problems), F (god class / untestable)

| Module | LOC | Grade | Layer Violations | Test Coverage | Priority |
|--------|-----|-------|-----------------|---------------|----------|
| `margin_engine/` | 3,050 | **A** | None | 2 use-case test files | Reference |
| `hub/` | 12,983 | **B-** | Controller SQL, inline DDL | 4 test files | SOON |
| `engine/persistence/db_client.py` | 2,566 | **F** | God class, 45+ methods | 0 tests | **NOW** |
| `engine/strategies/orchestrator.py` | 3,579 | **D** | Wires + runs + coordinates + resumes + heartbeats | 0 tests | **NOW** |
| `engine/strategies/five_min_vpin.py` | 3,109 | **D** | Strategy + execution + DB writes + signal logic | 0 tests | **NOW** |
| `engine/reconciliation/` | 3,184 | **C+** | Direct DB, but well-bounded | 2 test files | SOON |
| `engine/execution/` | 3,089 | **C** | `polymarket_client.py` mixes paper+live | 1 test file | SOON |
| `engine/signals/` | 2,590 | **C+** | Clean interfaces, some `os.environ` reads | 5 test files | LATER |
| `engine/data/feeds/` | 2,432 | **B** | Reasonable feed abstractions | 1 test file | LATER |
| `engine/alerts/telegram.py` | 2,233 | **C-** | Formatting + HTTP + business logic | 0 tests | LATER |
| `engine/domain/` | 531 | **C** | Ports good; value objects are empty stubs | 0 tests | SOON |
| `macro-observer/` | 1,193 | **B** | Single-purpose script, DB-only interface | 0 tests | NEVER |
| `data-collector/` | 880 | **B+** | Clean single-purpose collector | 0 tests | NEVER |
| `frontend/src/` | 63,582 | **B-** | No shared API layer, some god pages | 0 tests | LATER |

---

## 1. Baseline: margin_engine/ (Grade: A)

### What "good" looks like

```
margin_engine/
  domain/
    entities/position.py    (306 LOC) -- state machine with invariants
    entities/portfolio.py   (160 LOC) -- aggregate root with risk gates
    value_objects.py        (572 LOC) -- frozen dataclasses, validation in __post_init__
    ports.py                (283 LOC) -- 7 abstract ports (Exchange, Signal, Probability,
                                        V4Snapshot, Alert, PositionRepository, Clock)
  use_cases/
    open_position.py        (706 LOC) -- 10-gate decision stack, v4+v2 paths
    manage_positions.py     (518 LOC) -- exit evaluation + continuation logic
  adapters/
    exchange/binance_margin.py, paper.py, hyperliquid_price_feed.py
    signal/ws_signal.py, probability_http.py, v4_snapshot_http.py
    alert/telegram.py
    persistence/pg_repository.py, pg_log_repository.py, pg_signal_repository.py
  infrastructure/
    config/settings.py      -- pydantic BaseSettings
    status_server.py        -- HTTP /status endpoint
  main.py                   (485 LOC) -- composition root, DI wiring
  tests/
    use_cases/test_mark_divergence_gate.py
    use_cases/test_open_position_macro_advisory.py
```

**Key patterns the engine/ SHOULD adopt:**

1. **Ports in domain layer** -- abstract base classes defining interfaces, zero framework imports.
   `margin_engine/domain/ports.py` declares `ExchangePort`, `SignalPort`, `ProbabilityPort`,
   `V4SnapshotPort`, `AlertPort`, `PositionRepository`, `ClockPort` -- all `abc.ABC` subclasses.
   The only imports are from `margin_engine.domain.entities` and `margin_engine.domain.value_objects`.

2. **Value objects with validation** -- `Money` forbids negative/NaN/Inf in `__post_init__`,
   `Price` enforces positive finite, `CompositeSignal` bounds score to [-1, 1] and validates
   timescale. `V4Snapshot.from_dict()` is a defensive parser that never raises on missing keys.

3. **Entity state machines** -- `Position.confirm_entry()` guards `state != PENDING_ENTRY`,
   `request_exit()` guards `state != OPEN`, `confirm_exit()` guards `state != PENDING_EXIT`.
   `_compute_pnl()` uses exchange ground truth when available, falls back to estimate.

4. **Composition root** -- `main.py` wires all adapters to ports with explicit constructor
   injection. No import cycles. No framework types leak into domain.

5. **Use cases as orchestrators** -- `OpenPositionUseCase.execute()` walks a 10-gate stack,
   calls `ExchangePort`, `PositionRepository`, `AlertPort` -- never touches SQL or HTTP.

6. **Adapter isolation** -- `PgPositionRepository` implements `PositionRepository` port,
   owns all SQL. `BinanceMarginAdapter` implements `ExchangePort`, owns all API calls.

---

## 2. hub/ -- FastAPI Dashboard Backend (Grade: B-)

### Structure

```
hub/
  api/          -- 19 route files (~8,900 LOC total)
  auth/         -- JWT + middleware (96+40+89 = 225 LOC)
  db/           -- database.py (202), models.py (196), config_schema.py (152), config_seed.py
  services/     -- dashboard_service.py (162), pnl_service.py (145), signal_service.py (132)
  ws/           -- live_feed.py (140)
  main.py       -- FastAPI app + inline DDL migrations (183 LOC)
  tests/        -- 4 test files
```

### Findings

**Layer violations (Medium):**

- `hub/main.py::lifespan` has **82 lines of inline DDL** (CREATE TABLE, ALTER TABLE, CREATE INDEX,
  INSERT seed data) in the app startup handler. Also calls `ensure_manual_trades_table()`,
  `ensure_manual_trade_snapshots_table()`, `ensure_config_tables()`, and `seed_config_keys()`.

- Several API route files contain direct SQLAlchemy queries in route handlers rather than
  delegating to services. `dashboard.py` has both patterns -- `get_dashboard()` delegates to
  `DashboardService`, but `get_vpin_history()`, `get_equity_curve()`, `get_daily_pnl_chart()`,
  `get_stats()`, and `get_trades_chart()` all contain inline `select()` queries.

- `api/v58_monitor.py` contains `ensure_manual_trades_table()` and
  `ensure_manual_trade_snapshots_table()` -- DDL functions inside a route module.

**Coupling (Low-Medium):**

- Services accept `AsyncSession` directly -- coupled to SQLAlchemy. Prevents unit testing without DB.
- `db/database.py` contains `_PoolProxy` -- hand-rolled asyncpg adapter wrapping SQLAlchemy's
  `raw_connection()`. Bridges two persistence paradigms in one file.

**Good patterns:**

- Service layer exists (`DashboardService`, `PnLService`, `SignalService`) with clear methods
- Pydantic request/response schemas in `auth/routes.py`
- `config_schema.py` + `config_seed.py` are well-structured with idempotent DDL
- DSN normalization is tested (`test_database_dsn_normalize.py`)
- Auth middleware correctly implemented as FastAPI dependency

**Tests:** 4 files -- config schema/seed/API, DSN normalization. No tests for dashboard,
trades, PnL, signals, auth routes, or WebSocket.

### Recommendations

| Item | Effort | Priority |
|------|--------|----------|
| Extract inline DDL from `main.py` into `db/migrations.py` | 30 min | SOON |
| Move direct SQL from route handlers into service layer | 2 hrs | SOON |
| Extract `ensure_*_table` DDL from `v58_monitor.py` into `db/` | 30 min | SOON |
| Add route-level tests (FastAPI TestClient) for critical endpoints | 4 hrs | LATER |

---

## 3. engine/persistence/db_client.py (Grade: F)

### The Problem

**2,566 lines. 45+ public methods. One class.** This is the single worst Clean Architecture
violation in the repo.

`DBClient` handles ALL persistence for the engine:

- **Trade writes:** `write_trade()`, `resolve_trade()`, `get_open_trades()`, `get_recent_trades()`,
  `update_trade_field()`, `get_trade_by_order_id()`
- **Signal writes:** `write_signal()`, `write_vpin()`, `write_cascade()`,
  `write_arb_opportunity()`, `write_signal_evaluation()`
- **System state:** `write_system_state()`, `get_system_state()`
- **Window snapshots:** `write_window_snapshot()`, `store_post_resolution()`,
  `get_eval_ticks_for_window()`
- **Gate audit:** `write_gate_audit()`
- **CLOB execution logging:** `write_clob_execution_log()`, `write_fok_ladder_attempt()`,
  `write_clob_book_snapshot()`
- **Manual trade polling:** `get_pending_manual_trades()`, `update_manual_trade_status()`
- **PostgreSQL LISTEN/NOTIFY:** `listen()`, `stop_listening()`, `ensure_listening()` (lines 79-195)
- **Daily PnL, redemption tracking, wallet balance reads**

Every strategy, reconciler, and alerter imports `DBClient` directly. This makes the entire
engine untestable without a running PostgreSQL database.

### Comparison to margin_engine

| Concern | `margin_engine/` | `engine/` |
|---------|-----------------|-----------|
| Position persistence | `PgPositionRepository` (1 file, implements port) | `DBClient.write_trade` + `resolve_trade` + ... (2,566-line file) |
| Signal persistence | `PgSignalRepository` (1 file) | `DBClient.write_signal` + `write_vpin` + `write_cascade` + ... |
| Log persistence | `PgLogRepository` (1 file) | `DBClient.write_system_state` |
| Interface | Abstract port in `domain/ports.py` | Concrete class, no interface |

### Migration Path

Split into 6 repository adapters matching `engine/domain/ports.py`:

1. **`PgSignalRepository`** implementing `SignalRepository`
2. **`PgWindowStateRepository`** implementing `WindowStateRepository`
3. **`PgTradeRepository`** -- trade CRUD
4. **`PgSystemStateRepository`** -- system state + heartbeat
5. **`PgManualTradeRepository`** -- manual trades + LISTEN/NOTIFY
6. **`PgClobExecutionLogger`** -- CLOB execution audit trail

Each takes `asyncpg.Pool`, implements its port, owns its SQL. Pool shared; SQL isolated.

**Effort:** 8-12 hours (mechanical extraction, no behavior change)
**Risk:** Low -- each method moves independently
**Priority:** **NOW** -- blocks testability for everything else

---

## 4. engine/strategies/orchestrator.py (Grade: D)

### The Problem

**3,579 lines.** Composition root + event loop + heartbeat publisher + resolution poller +
manual trade executor + mode switcher + graceful shutdown -- all in one class.

Imports **29 modules** (all feeds, all strategies, alerts, execution, persistence, evaluation,
browser automation). Instantiates everything internally. No dependency injection. Cannot be
tested without all external services running.

### Comparison to margin_engine

| Concern | `margin_engine/main.py` | `engine/strategies/orchestrator.py` |
|---------|------------------------|--------------------------------------|
| LOC | 485 | 3,579 |
| Imports | 12 | 29+ |
| Responsibility | Wire + run | Wire + run + heartbeat + resolve + manual trades + mode switch + shutdown |
| DI approach | Constructor injection | Direct instantiation |

### Migration Path

1. Extract composition root into `engine/infrastructure/di.py`
2. Extract `PublishHeartbeatUseCase` (~200 lines)
3. Extract `ResolvePositionsUseCase` (~300 lines)
4. Extract `ExecuteManualTradeUseCase` (~250 lines)
5. Slim orchestrator to ~500 lines

**Effort:** 16-24 hours (incremental -- one extraction per PR)
**Risk:** Medium -- live system
**Priority:** **NOW** (extract incrementally)

---

## 5. engine/strategies/five_min_vpin.py (Grade: D)

### The Problem

**3,109 lines.** Strategy + execution engine + DB writer + signal consumer + risk checker.

Directly imports: `FOKLadder`, `PolymarketClient`, `DBClient` (8+ write ops), `RiskManager`,
`OrderManager`, `WindowEvaluator`, `TimesFMClient`, `VPINCalculator`, `TelegramAlerter`.

Module-level `os.environ` reads for dynamic caps.

### Migration Path

1. Extract `EvaluateWindowUseCase` -- signals -> TradeDecision
2. Extract `ExecuteTradeUseCase` -- TradeDecision -> order via `PolymarketClientPort`
3. Extract `RecordTradeUseCase` -- execution result -> persist via repositories
4. Strategy becomes thin coordinator

**Effort:** 16-24 hours | **Risk:** High | **Priority:** **NOW** (after db_client split)

---

## 6. engine/reconciliation/ (Grade: C+)

**3,184 LOC total.** `reconciler.py` (2,371 LOC) handles too much: wallet polling, position
tracking, resolution, SOT reconciliation, trade_bible enrichment, Telegram alerts.
`PolyFillsReconciler` (413 LOC) is well-bounded with clear 5-step pipeline.
`ReconciliationSummary` is a proper value object. Tests exist for SOT reconciliation.

| Item | Effort | Priority |
|------|--------|----------|
| Extract wallet-polling into own adapter | 2 hrs | SOON |
| Inject alerter via port | 1 hr | SOON |
| Split reconciler into resolution vs SOT | 4 hrs | LATER |

---

## 7. engine/execution/ (Grade: C)

**3,089 LOC total.** `polymarket_client.py` (1,638 LOC) mixes paper+live with `if self._paper_mode:`
branches. `risk_manager.py` (285 LOC) is clean. `fok_ladder.py` (237 LOC) reads `os.environ`.
`order_manager.py` (840 LOC) has `Order` dataclass + paper resolution logic.

| Item | Effort | Priority |
|------|--------|----------|
| Split polymarket_client into Paper + Live adapters | 4 hrs | SOON |
| Replace `os.environ` in fok_ladder with constructor params | 30 min | SOON |

---

## 8. engine/signals/ (Grade: C+)

**2,590 LOC total.** `gates.py` (1,226 LOC) is the bright spot -- Protocol-based pipeline,
clean chain-of-responsibility, proper value objects. `vpin.py` (252 LOC) clean calculator.
`timesfm_client.py`/`v2_client.py` are natural adapter candidates. 5 test files -- best-tested
module in engine.

| Item | Effort | Priority |
|------|--------|----------|
| Define `ForecastPort` for TimesFM clients | 30 min | LATER |
| No changes to `gates.py` | -- | NEVER |

---

## 9. engine/data/feeds/ (Grade: B)

**2,432 LOC** across 10 feed classes. Each owns connection lifecycle, provides data via
callbacks/async. `MarketFeedPort` in `domain/ports.py` defines target interface. Natural
adapter candidates. Fine to leave as-is for now.

---

## 10. engine/alerts/telegram.py (Grade: C-)

**2,233 lines.** Formatting + HTTP + chart generation + business logic. 20+ methods vs 4 in
`AlerterPort`. Contains `_format_v103_block()` for gate/CG/threshold formatting, `sendPhoto`
multipart upload, and all alert composition.

| Item | Effort | Priority |
|------|--------|----------|
| Implement `AlerterPort` adapter delegating to existing | 2 hrs | LATER |
| Extract formatting into pure `TelegramFormatter` | 3 hrs | LATER |

---

## 11. engine/domain/ (Grade: C)

**ports.py (383 LOC) is excellent:** 8 abstract ports modeled after `margin_engine/domain/ports.py`.
`MarketFeedPort`, `ConsensusPricePort`, `SignalRepository`, `PolymarketClientPort`, `AlerterPort`,
`Clock`, `WindowStateRepository`, `ConfigPort`. Clean docstrings, no framework imports.

**value_objects.py (148 LOC) is entirely empty stubs:** 17 frozen dataclasses, ALL with `pass`
bodies. "Phase 0 deliverable" -- Phase 1 never happened. Engine still uses raw dicts + `DBClient`.

| Item | Effort | Priority |
|------|--------|----------|
| Populate VOs with fields from existing code | 4 hrs | **SOON** |
| Wire at least one adapter to prove the pattern | 2 hrs | **SOON** |

---

## 12. macro-observer/ (Grade: B)

**1,193 LOC, single file.** Railway service: polls prices, calls Qwen 3.5 LLM, writes
`macro_signals` row. DB-only interface. v2 per-timescale bias added 2026-04-11. No debt worth addressing.

---

## 13. data-collector/ (Grade: B+)

**880 LOC across 2 files.** Polymarket 5m/15m data collector. Rate-limit aware, idempotent
upserts, heartbeat healthcheck. No debt worth addressing.

---

## 14. frontend/src/ (Grade: B-)

**63,582 LOC** across 87 JSX/JS files. Good: `useApi()` hook, `AuthContext`, component
decomposition in `execution-hq/` (14 components). Bad: god pages (`V58Monitor` 3,118 LOC,
`Schema` 1,457 LOC, `TradingConfig` 1,373 LOC), zero tests, dual API patterns, no TypeScript.

| Item | Effort | Priority |
|------|--------|----------|
| Consolidate API patterns | 2 hrs | LATER |
| Break up god pages | 8 hrs | LATER |
| Add test framework + smoke tests | 4 hrs | LATER |

---

## Priority-Ordered Recommendations

### NOW (blocks everything else)

| # | Item | Module | Effort | Impact |
|---|------|--------|--------|--------|
| 1 | **Split `db_client.py` into repository adapters** | engine/persistence | 8-12 hrs | Unblocks testability |
| 2 | **Populate `value_objects.py` stubs** with fields | engine/domain | 4 hrs | Unblocks adapter wiring |
| 3 | **Wire first adapter** end-to-end | engine/domain + adapters | 2 hrs | Proves the pattern |

### SOON (next 2-4 weeks)

| # | Item | Module | Effort | Impact |
|---|------|--------|--------|--------|
| 4 | Extract `PublishHeartbeatUseCase` from orchestrator | engine/strategies | 4 hrs | Slim orchestrator |
| 5 | Extract `ResolvePositionsUseCase` from orchestrator | engine/strategies | 4 hrs | Slim orchestrator |
| 6 | Split `polymarket_client.py` into Paper + Live | engine/execution | 4 hrs | Paper-only testing |
| 7 | Extract inline DDL from `hub/main.py` | hub | 30 min | Quick win |
| 8 | Move SQL from hub route handlers to services | hub/api | 2 hrs | Proper MVC |
| 9 | Inject alerter via port in reconciler | engine/reconciliation | 1 hr | Cleaner DI |

### LATER (when there's bandwidth)

| # | Item | Module | Effort | Impact |
|---|------|--------|--------|--------|
| 10 | Slim `five_min_vpin.py` into Use Case + Execution + Recording | engine/strategies | 16 hrs | Major refactor |
| 11 | Extract Telegram formatting into pure functions | engine/alerts | 3 hrs | Testable formatting |
| 12 | Have feeds implement `MarketFeedPort` | engine/data/feeds | 3 hrs | Completes wiring |
| 13 | Break up frontend god pages | frontend | 8 hrs | Maintainability |
| 14 | Add frontend test framework | frontend | 4 hrs | Regression safety |
| 15 | Define `ForecastPort` for TimesFM clients | engine/domain | 30 min | Completes ports |

### NEVER (debt that's fine to keep)

| Item | Reason |
|------|--------|
| `macro-observer/observer.py` | DB-only interface, single purpose |
| `data-collector/` | Works perfectly, no benefit from layering |
| `engine/signals/gates.py` | Already Protocol-based, clean chain-of-responsibility |
| `engine/execution/risk_manager.py` | 285 LOC, clean single responsibility |
| Hub auth layer | Correct FastAPI dependency pattern |
| `engine/signals/vpin.py` | 252 LOC, focused, testable |

---

## Quick Wins (<1 hour each)

1. **Replace `os.environ` reads in `fok_ladder.py`** with constructor params (30 min)
2. **Extract `ensure_*_table` DDL** from `hub/api/v58_monitor.py` into `hub/db/` (30 min)
3. **Extract inline DDL** from `hub/main.py::lifespan` into `hub/db/migrations.py` (30 min)
4. **Add `__all__` exports** to `engine/domain/value_objects.py` once populated (15 min)
5. **Define `ForecastPort`** in `engine/domain/ports.py` for TimesFM clients (30 min)

---

## Test Coverage Summary

| Module | Test Files | Covered | Critical Gaps |
|--------|-----------|---------|---------------|
| `margin_engine/` | 2 | Macro advisory, mark divergence | Entity tests, adapter integration |
| `engine/` | 12 | VPIN, arb, cascade, risk, reconciliation, v2, gates, timesfm v2 | **orchestrator, five_min_vpin, db_client, polymarket_client, telegram, feeds** |
| `hub/` | 4 | Config schema/seed/API, DSN | **Dashboard, trades, PnL, signals, auth, WS** |
| `frontend/` | 0 | Nothing | Everything |
| `macro-observer/` | 0 | -- | Acceptable |
| `data-collector/` | 0 | -- | Acceptable |

---

## Comparison: margin_engine/ vs engine/

| Aspect | `margin_engine/` (A) | `engine/` (D) |
|--------|---------------------|---------------|
| **Domain layer** | 572-line VOs with validation, 283-line ports | 148-line empty stubs, 383-line ports (unused) |
| **Entity model** | `Position` state machine, `Portfolio` aggregate root | No entities -- state in strategies + `Order` |
| **Persistence** | 3 repository adapters implementing ports | 1 god class (2,566 LOC, 45+ methods) |
| **Composition root** | `main.py` (485 LOC) -- clean DI | `orchestrator.py` (3,579 LOC) -- everything |
| **Use cases** | 2 focused (1,224 LOC total) | Strategies ARE use cases (3,109-3,579 LOC each) |
| **Adapters** | Exchange, Signal, Alert, Persistence (<300 LOC each) | Everything calls everything directly |
| **Config** | `MarginSettings` (pydantic, 80 LOC) | `Settings` + `runtime_config` (430 LOC) |
| **Testability** | Use cases testable with mock ports | Nothing testable without PG + live APIs |

---

## Architecture Debt Heat Map

```
CRITICAL  [========================================] engine/persistence/db_client.py      (2,566 LOC god class)
CRITICAL  [====================================    ] engine/strategies/orchestrator.py     (3,579 LOC mega-coordinator)
HIGH      [=================================       ] engine/strategies/five_min_vpin.py    (3,109 LOC strategy-as-everything)
MEDIUM    [========================                ] engine/reconciliation/reconciler.py   (2,371 LOC, direct DB)
MEDIUM    [======================                  ] engine/execution/polymarket_client.py (1,638 LOC, paper+live mixed)
MEDIUM    [====================                    ] engine/alerts/telegram.py             (2,233 LOC, format+transport+logic)
LOW       [================                        ] hub/main.py (82 lines inline DDL)
LOW       [==============                          ] hub/api/* (controller-level SQL)
LOW       [============                            ] engine/domain/value_objects.py (empty stubs)
NONE      [                                        ] margin_engine/*
NONE      [                                        ] engine/signals/gates.py
NONE      [                                        ] macro-observer/*
NONE      [                                        ] data-collector/*
```

---

*Generated by Claude Opus 4.6 (1M context) on 2026-04-11. Read-only audit -- no code changes made.*
