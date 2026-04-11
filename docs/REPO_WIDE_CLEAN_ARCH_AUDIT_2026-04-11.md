# Repo-Wide Clean Architecture Audit

**Date:** 2026-04-11
**Auditor:** Claude (automated, code-read-based)
**Scope:** All modules EXCLUDING `engine/strategies/` (covered separately)
**Reference architecture:** `margin_engine/` (clean-arch baseline)

---

## Executive Summary

The `margin_engine/` module is a genuine Clean Architecture implementation: domain entities with invariants, abstract ports, adapter implementations, use-case orchestration, and a composition root that wires everything via manual DI. It is the gold standard in this repo.

The rest of the codebase is **working production code that has never been through an architectural pass**. The Polymarket engine (`engine/`) is a monolith grown organically over ~6 months of rapid iteration. The hub is a FastAPI CRUD layer with routes that mix SQL, business logic, and presentation in single files. Neither is broken -- both ship value -- but they carry significant structural debt.

**Total LOC audited (Python + JS/JSX, excluding strategies):**

| Module | LOC | Tests LOC | Test Coverage |
|--------|-----|-----------|---------------|
| hub/ | ~12,434 | 911 | Minimal (config only) |
| engine/ (excl strategies) | ~19,510 | 4,937 | Moderate (targeted) |
| margin_engine/ (reference) | ~3,800 | 818 | Domain + use-case |
| frontend/src/ | ~39,311 | 0 | None |
| macro-observer/ | 1,193 | 0 | None |
| data-collector/ | 880 | 0 | None |
| **TOTAL** | **~77,128** | **6,666** | |

---

## Reference Architecture: margin_engine/

Before scoring the rest of the repo, here is what "good" looks like in this codebase:

```
margin_engine/
  domain/
    entities/position.py      -- Position with state machine (PENDING_ENTRY -> OPEN -> CLOSED)
    entities/portfolio.py     -- Portfolio risk rules
    value_objects.py          -- Money, Price, CompositeSignal, ProbabilitySignal (frozen, validated)
    ports.py                  -- ExchangePort, SignalPort, AlertPort, PositionRepository, ClockPort (ABCs)
  use_cases/
    open_position.py          -- 10-gate decision stack, orchestrates domain + ports
    manage_positions.py       -- Stop/TP/trailing/continuation logic
  adapters/
    exchange/binance_margin.py, paper.py, hyperliquid_price_feed.py
    persistence/pg_repository.py  -- Implements PositionRepository
    signal/ws_signal.py, probability_http.py, v4_snapshot_http.py
    alert/telegram.py
  infrastructure/
    config/settings.py
    status_server.py
  main.py                     -- Composition root, manual DI
```

**Key patterns to match:**
- Domain layer has ZERO external imports (no asyncpg, no httpx, no FastAPI)
- Ports are abstract base classes in `domain/ports.py`
- Adapters implement ports and live in `adapters/`
- Use cases depend only on domain types and port interfaces
- `main.py` is the ONLY file that knows about all layers (composition root)
- Value objects are `frozen=True` dataclasses with `__post_init__` validation
- Entity state transitions enforce invariants (`confirm_entry` checks state == PENDING_ENTRY)

---

## Module-by-Module Findings

### 1. hub/ (FastAPI Dashboard Backend)

**LOC:** 12,434 (21 route files, 4 DB files, 3 auth files, 3 service files)
**Architecture:** Flat FastAPI with SQLAlchemy ORM + raw SQL mix

#### Structure

```
hub/
  api/           -- 21 route files (route handlers with inline SQL and business logic)
  db/            -- ORM models, database.py, config_seed.py, schema_catalog.py
  auth/          -- JWT + middleware (clean, small)
  services/      -- 3 service files (dashboard, pnl, signal)
  ws/            -- WebSocket feed
  main.py        -- FastAPI app + inline migrations (130+ lines of DDL)
```

#### Findings

| Finding | Severity | Detail |
|---------|----------|--------|
| **v58_monitor.py is 3,514 LOC** | HIGH | Single file with DDL migrations, route handlers, raw SQL queries, business logic for manual trades, SOT reconciliation helpers, and Telegram proxying. This is a god-file. |
| **Raw SQL throughout route handlers** | MEDIUM | v58_monitor has ~110 `text()` / `execute()` calls. trading_config has a `_DBShim` class that converts asyncpg-style `$1` params to SQLAlchemy `:p1` params -- an adapter within an adapter. |
| **Migrations in main.py lifespan** | MEDIUM | ~80 lines of inline `CREATE TABLE IF NOT EXISTS` and `ALTER TABLE ADD COLUMN IF NOT EXISTS` in the FastAPI lifespan handler. Should be in Alembic migrations. |
| **Mixed ORM + raw SQL** | MEDIUM | Some routes use SQLAlchemy ORM (dashboard.py uses `select(Trade)`), others use raw `text()` SQL (v58_monitor, trading_config). No consistency. |
| **No domain layer** | MEDIUM | The hub has no domain entities or business rules. ORM models serve as both DB schema and business objects. |
| **3 service files vs 21 route files** | LOW | Only `dashboard_service.py`, `pnl_service.py`, `signal_service.py` exist. The other 18 route files contain business logic inline. |
| **Auth is clean** | GOOD | `auth/jwt.py` (96 LOC), `auth/middleware.py` (41 LOC) -- small, focused, correctly separated. |
| **Proxy pattern is clean** | GOOD | `margin.py` (515 LOC) is a thin HTTP proxy to the margin engine and TimesFM service. `_proxy_get` / `_proxy_post` helpers are well-factored. |
| **schema_catalog.py is 1,519 LOC** | LOW | Large but static data (DB table inventory). Fine as-is. |
| **config_seed.py is 732 LOC** | LOW | Seed data for 142+ config keys. Inherently large. |

#### Coupling

- Hub imports nothing from engine or margin_engine (GOOD -- decoupled via DB and HTTP)
- Hub talks to engine via PostgreSQL reads and HTTP proxy to margin engine status server

#### Test Coverage

- 911 LOC of tests: `test_config_seed.py`, `test_config_api.py`, `test_config_schema.py`, `test_database_dsn_normalize.py`
- No tests for any route handler (dashboard, trades, v58, trading_config, margin proxy)
- No tests for services

#### Refactoring Risk: MEDIUM
#### Priority: SOON (v58_monitor.py split, extract services from routes)

---

### 2. engine/reconciliation/ (2,742 LOC)

| File | LOC |
|------|-----|
| reconciler.py | 2,105 |
| poly_fills_reconciler.py | 413 |
| poly_trade_history.py | 166 |
| state.py | 53 |

#### Findings

| Finding | Severity | Detail |
|---------|----------|--------|
| **reconciler.py is 2,105 LOC** | HIGH | God-class `CLOBReconciler` handles: wallet polling, position tracking, trade resolution, SOT reconciliation, Telegram reporting, GTC fill detection, orphan order cleanup. At least 4 distinct responsibilities. |
| **Tight coupling to PolymarketClient + TelegramAlerter + DBClient** | MEDIUM | Constructor takes concrete types (via TYPE_CHECKING), not interfaces. Line 1158 does `from persistence.db_client import DBClient` inside a method. |
| **state.py is well-separated** | GOOD | `ReconcilerState`, `OpenPosition`, `RestingOrder`, `WalletSnapshot` are clean dataclasses. This IS a domain model -- it just is not labeled as one. |
| **poly_fills_reconciler.py** | OK | 413 LOC, separate from the main reconciler. Clean enough responsibility. |
| **ReconciliationSummary** | GOOD | Clean result type returned from reconciliation passes. |

#### Coupling

- Imports from: `reconciliation.state`, `alerts.telegram` (TYPE_CHECKING), `execution.polymarket_client` (TYPE_CHECKING), `persistence.db_client` (runtime, line 1158)
- The TYPE_CHECKING pattern reduces runtime coupling but the dependency is still conceptual

#### Test Coverage

- `test_reconcile_trades_sot.py` (812 LOC), `test_reconcile_manual_trades_sot.py` (610 LOC)
- Good focused testing of the SOT reconciliation logic

#### Refactoring Risk: HIGH (core money path, deeply intertwined with Polymarket API)
#### Priority: SOON (split CLOBReconciler into 3-4 focused classes)

---

### 3. engine/persistence/ (3,004 LOC)

| File | LOC |
|------|-----|
| db_client.py | 2,546 |
| tick_recorder.py | 458 |

#### Findings

| Finding | Severity | Detail |
|---------|----------|--------|
| **db_client.py is 2,546 LOC with 57 async methods** | CRITICAL | This is the single worst Clean Architecture violation in the repo. It is a god-class that handles persistence for: trades, signals, system state, window snapshots, countdown evaluations, gate audits, manual trades, SOT columns, shadow trades, post-resolution analysis, CLOB execution logs, FOK ladder attempts, CLOB book snapshots, window predictions, feed status, playwright state, gamma prices, redeem events. **Every aggregate in the system writes through one class.** |
| **50 `async with self._pool.acquire()` calls** | HIGH | Every method repeats the connection acquisition pattern. No repository abstraction, no unit-of-work pattern. |
| **LISTEN/NOTIFY for manual trades** | MEDIUM | Good pattern (real-time notification between hub and engine), but mixed into the god-class instead of being a dedicated adapter. |
| **No port/interface pattern** | HIGH | Unlike margin_engine where `PositionRepository` is an ABC in the domain layer, db_client.py is a concrete class with no interface. Impossible to mock for testing without patching. |
| **tick_recorder.py** | OK | 458 LOC, async buffered writer. Reasonable single responsibility. |

#### Coupling

- Imports `config.settings.Settings` and `execution.order_manager.Order` at module level
- Every module in the engine depends on this single class

#### Test Coverage

- No direct tests for db_client.py
- Indirectly tested through integration tests that hit the database

#### Refactoring Risk: HIGH (every feature in the engine depends on this class)
#### Priority: NOW (split into per-aggregate repositories following margin_engine pattern)

---

### 4. engine/execution/ (3,574 LOC)

| File | LOC |
|------|-----|
| polymarket_client.py | 1,638 |
| order_manager.py | 764 |
| redeemer.py | 645 |
| risk_manager.py | 285 |
| fok_ladder.py | 237 |
| opinion_client.py | 284 |

#### Findings

| Finding | Severity | Detail |
|---------|----------|--------|
| **polymarket_client.py is 1,638 LOC** | MEDIUM | Combines: paper-mode simulation, live CLOB order signing, order status querying, token ID resolution, book fetching. The `PolyOrderStatus` dataclass (SOT result type) is well-designed. |
| **Paper mode + live mode in one class** | MEDIUM | `PolymarketClient` has an `if self.paper_mode:` branch in nearly every method. margin_engine handles this via separate `PaperExchangeAdapter` / `BinanceMarginAdapter` classes behind `ExchangePort`. |
| **No port/interface** | HIGH | No abstract `ExchangePort` equivalent. The strategy layer calls `PolymarketClient` directly. |
| **order_manager.py** | OK | 764 LOC. `Order` dataclass + `OrderStatus` enum + tracking logic. Some paper-mode resolution logic that should live elsewhere. |
| **risk_manager.py** | GOOD | 285 LOC, clean single responsibility (7-gate risk approval). Uses `runtime` config but no domain coupling. The cleanest file in engine/execution/. |
| **fok_ladder.py** | GOOD | 237 LOC, focused FOK execution strategy. Clean. |
| **redeemer.py** | OK | 645 LOC. Polymarket redemption sweep logic. Specialized but isolated. |

#### Coupling

- `order_manager.py` has no cross-module imports (GOOD)
- `risk_manager.py` imports only `config.runtime_config` (GOOD)
- `polymarket_client.py` is self-contained but large

#### Test Coverage

- `test_risk_manager.py` (364 LOC) -- good
- No tests for polymarket_client, order_manager, fok_ladder, redeemer

#### Refactoring Risk: MEDIUM (paper/live split is the main task)
#### Priority: SOON (extract paper mode into separate adapter)

---

### 5. engine/signals/ (3,893 LOC)

| File | LOC |
|------|-----|
| gates.py | 1,226 |
| twap_delta.py | 549 |
| v2_feature_body.py | 470 |
| window_evaluator.py | 327 |
| cascade_detector.py | 290 |
| timesfm_v2_client.py | 267 |
| timesfm_client.py | 268 |
| vpin.py | 252 |
| arb_scanner.py | 246 |
| regime_classifier.py | 196 |
| sizing.py | 99 |

#### Findings

| Finding | Severity | Detail |
|---------|----------|--------|
| **gates.py is well-designed** | GOOD | 1,226 LOC. Uses Protocol for `Gate` interface, `GateResult`/`PipelineResult`/`GateContext` dataclasses. Pipeline pattern with compose-ability. This is close to clean architecture for the Polymarket side. |
| **vpin.py is pure domain logic** | GOOD | 252 LOC. `VPINCalculator` takes `AggTrade` events, computes VPIN from volume buckets. Only domain import is `data.models.AggTrade`. Could be in a `domain/` folder. |
| **window_evaluator.py** | GOOD | 327 LOC. Clean composite signal evaluator with tiered confidence thresholds. Pure computation, no side effects. |
| **timesfm_client.py and timesfm_v2_client.py** | OK | HTTP clients for the TimesFM forecast service. These ARE adapters, just not labeled as such. |
| **cascade_detector.py** | OK | 290 LOC. FSM-based cascade detection. Pure domain logic. |
| **v2_feature_body.py** | OK | 470 LOC. Feature engineering for the v2 model input. Domain-adjacent. |
| **Minimal cross-module coupling** | GOOD | `vpin.py` only imports `data.models`. `gates.py` only imports `config.runtime_config` and stdlib. Most files are self-contained. |

#### Coupling

- `vpin.py` imports `data.models.AggTrade` and `config.runtime_config`
- `gates.py` imports only `config.runtime_config`
- `timesfm_client.py` and `timesfm_v2_client.py` import only `aiohttp`
- Generally low coupling -- the cleanest module in the engine

#### Test Coverage

- `test_vpin.py` (196 LOC), `test_arb_scanner.py` (286 LOC), `test_cascade.py` (279 LOC), `test_v2_feature_body.py` (449 LOC), `test_source_agreement_spot_only.py` (346 LOC), `test_eval_offset_bounds_gate.py` (275 LOC)
- Good coverage for the core signal logic

#### Refactoring Risk: LOW
#### Priority: LATER (already the cleanest module; minor labeling improvements)

---

### 6. engine/data/feeds/ (2,765 LOC)

| File | LOC |
|------|-----|
| polymarket_5min.py | 571 |
| coinglass_enhanced.py | 409 |
| chainlink_feed.py | 219 |
| tiingo_feed.py | 213 |
| coinglass_api.py | 204 |
| polymarket_ws.py | 186 |
| clob_feed.py | 159 |
| elm_prediction_recorder.py | 157 |
| binance_ws.py | 155 |
| chainlink_rpc.py | 140 |

#### Findings

| Finding | Severity | Detail |
|---------|----------|--------|
| **All feeds follow a consistent pattern** | GOOD | `start()` / `stop()` lifecycle, async poll loop or WebSocket subscription, structured logging. |
| **binance_ws.py** | GOOD | 155 LOC. Clean WS feed with typed callbacks (`on_trade`, `on_book`, `on_liquidation`). Auto-reconnect with exponential backoff. |
| **polymarket_5min.py** | OK | 571 LOC. Market discovery + window lifecycle + state machine. Complex but necessary complexity. |
| **clob_feed.py** | MEDIUM | 159 LOC. Directly accesses `self._poly._clob_client` (private attribute of PolymarketClient). Should go through a public interface. |
| **No port/interface pattern** | MEDIUM | Feeds are concrete classes. margin_engine's `SignalPort` is the model to follow. |

#### Coupling

- `clob_feed.py` reaches into `polymarket_client._clob_client` (private)
- `polymarket_5min.py` couples to Gamma API specifics
- Most feeds are isolated (take a config, produce events)

#### Test Coverage

- `test_elm_prediction_recorder.py` (301 LOC)
- No tests for binance_ws, clob_feed, polymarket_5min, or any of the price feeds

#### Refactoring Risk: LOW
#### Priority: LATER (working, isolated, low coupling)

---

### 7. engine/alerts/ (2,943 LOC)

| File | LOC |
|------|-----|
| telegram.py | 2,233 |
| window_chart.py | 307 |
| chart_generator.py | 272 |
| telegram_v2.py | 130 |

#### Findings

| Finding | Severity | Detail |
|---------|----------|--------|
| **telegram.py is 2,233 LOC** | HIGH | `TelegramAlerter` combines: message formatting, Bot API HTTP calls, chart attachment, throttling, error handling. At least 3 responsibilities (formatting, transport, throttling). |
| **telegram_v2.py** | GOOD | 130 LOC. Pure formatting functions, no side effects. This is what the formatting layer should look like. |
| **chart_generator.py and window_chart.py** | OK | matplotlib-based chart generation. Isolated responsibility. |
| **No AlertPort interface** | MEDIUM | margin_engine has `AlertPort` ABC. The Polymarket engine calls `TelegramAlerter` directly. |

#### Coupling

- `telegram.py` imports `data.models.CascadeSignal` and `execution.order_manager.Order` (TYPE_CHECKING)
- Transport and formatting are coupled in one class

#### Test Coverage

- No tests

#### Refactoring Risk: LOW (alerts are non-critical path)
#### Priority: LATER (split formatting from transport, but low urgency)

---

### 8. macro-observer/ (1,193 LOC, single file)

#### Findings

| Finding | Severity | Detail |
|---------|----------|--------|
| **Single-file service** | OK | 1,193 LOC in `observer.py`. For a standalone Railway service this is acceptable. |
| **Self-hosted LLM integration** | GOOD | Clean Qwen 3.5 integration via OpenAI-compatible API. Good documentation of the reasoning-model gotcha. |
| **DB is the only interface** | GOOD | Writes to `macro_signals` table. Engine reads from same table. No direct coupling. |
| **No tests** | MEDIUM | The LLM prompt and JSON parsing are testable but untested. |

#### Coupling

- Only couples to PostgreSQL via asyncpg. Completely isolated from other modules.

#### Refactoring Risk: LOW
#### Priority: NEVER (working, isolated, small)

---

### 9. data-collector/ (880 LOC)

| File | LOC |
|------|-----|
| collector.py | 563 |
| backfill.py | 317 |

#### Findings

| Finding | Severity | Detail |
|---------|----------|--------|
| **Simple data pipeline** | OK | Polls Gamma API, writes to `market_data` table. Rate-limit aware. |
| **DB as only interface** | GOOD | No coupling to any other module. |
| **No tests** | LOW | Straightforward ETL -- low risk of logic bugs. |

#### Coupling

- Only PostgreSQL and Gamma API HTTP calls

#### Refactoring Risk: LOW
#### Priority: NEVER (working, isolated, small)

---

### 10. frontend/src/ (39,311 LOC)

| Category | LOC | File Count |
|----------|-----|------------|
| Pages | ~28,000 | 35+ pages |
| Components | ~5,500 | 22 components |
| Auth | ~200 | 3 files |
| Hooks | ~150 | 2 hooks |
| Lib (API, WS, utils) | ~660 | 4 files |

#### Findings

| Finding | Severity | Detail |
|---------|----------|--------|
| **V58Monitor.jsx is 3,118 LOC** | HIGH | Single component with inline API calls, data transformation, rendering, and local state management for the entire execution HQ monitoring view. |
| **Two API client patterns** | MEDIUM | `lib/api.js` creates a module-level axios instance; `hooks/useApi.js` creates an auth-aware hook-based instance. Pages use both inconsistently. |
| **No shared data layer** | MEDIUM | Each page fetches its own data with inline `api.get()` calls. No React Query, SWR, or centralized data store. |
| **No tests** | MEDIUM | Zero test files in frontend/src/. |
| **execution-hq/ is well-structured** | GOOD | Decomposed into sub-components: `LiveTab`, `RetroTab`, `GateHeartbeat`, `ManualTradePanel`, etc. This is the direction the rest should follow. |
| **Page sizes vary wildly** | INFO | From 29 LOC (StatusBadge) to 3,118 LOC (V58Monitor). Average page is ~500 LOC. |
| **data-surfaces/ pages** | OK | V1Surface through V4Surface are large (700-900 LOC each) but inherently complex data visualization. |

#### Coupling

- Frontend only talks to hub via HTTP API (GOOD -- fully decoupled)
- No direct imports from backend code

#### Refactoring Risk: LOW (UI changes do not affect trading logic)
#### Priority: LATER (extract data hooks, split large pages)

---

## Summary Table

| Module | LOC | Arch Score | Test Score | Coupling | Risk | Priority |
|--------|-----|------------|------------|----------|------|----------|
| margin_engine/ (reference) | 3,800 | A | B+ | Low | -- | -- |
| engine/signals/ | 3,893 | B+ | B | Low | LOW | LATER |
| engine/data/feeds/ | 2,765 | B | D | Low | LOW | LATER |
| engine/execution/ | 3,574 | C | D+ | Medium | MEDIUM | SOON |
| engine/alerts/ | 2,943 | C- | F | Low | LOW | LATER |
| engine/reconciliation/ | 2,742 | C- | B- | High | HIGH | SOON |
| engine/persistence/ | 3,004 | F | F | Critical | HIGH | NOW |
| hub/ | 12,434 | D+ | D | Medium | MEDIUM | SOON |
| frontend/src/ | 39,311 | C- | F | Low | LOW | LATER |
| macro-observer/ | 1,193 | C+ | F | None | LOW | NEVER |
| data-collector/ | 880 | C+ | F | None | LOW | NEVER |

**Scoring key:**
- **Arch Score:** Clean Architecture adherence (A=ports/adapters/domain, F=god-class monolith)
- **Test Score:** Coverage of critical paths (A=domain+use-case+integration, F=none)
- **Coupling:** How tightly bound to other modules (Critical=everything depends on it)

---

## Priority-Ordered Recommendations

### NOW (blocking further development velocity)

#### 1. Split `engine/persistence/db_client.py` (2,546 LOC, 57 methods)

**Why:** Every feature addition touches this file. It is the single highest-coupling point in the repo. It conflates 10+ aggregate boundaries into one class with no interface.

**Target state (follow margin_engine pattern):**
```
engine/
  domain/
    ports.py               -- TradeRepository, SignalRepository, SystemStateRepository (ABCs)
  adapters/
    persistence/
      pg_trade_repository.py
      pg_signal_repository.py
      pg_system_state_repository.py
      pg_window_repository.py
      pg_manual_trade_repository.py
      pg_gate_audit_repository.py
```

**Estimated effort:** 2-3 days
**Risk mitigation:** Keep db_client.py as a facade that delegates to the new repositories during migration. Remove facade once all call sites are updated.

### SOON (within next 2 sprints)

#### 2. Split `hub/api/v58_monitor.py` (3,514 LOC)

**Why:** Single file with DDL migrations, route handlers, raw SQL, business logic. Hardest file in the hub to reason about.

**Target state:**
```
hub/
  api/v58/
    routes.py              -- Thin route handlers
    manual_trade_routes.py -- Manual trade endpoints
  services/
    v58_service.py         -- Business logic
    manual_trade_service.py
  db/
    migrations/v58.py      -- DDL migrations (or move to Alembic)
```

**Estimated effort:** 1-2 days

#### 3. Split `engine/reconciliation/reconciler.py` (2,105 LOC)

**Why:** Core money path that handles wallet tracking, trade resolution, SOT reconciliation, and reporting in one class.

**Target state:**
```
engine/reconciliation/
  wallet_tracker.py        -- Polls wallet balance, tracks snapshots
  trade_resolver.py        -- Matches resolutions to trades
  sot_reconciler.py        -- Existing SOT logic (already partially extracted)
  report_builder.py        -- Telegram reconciliation reports
  reconciler.py            -- Orchestrator that composes the above
```

**Estimated effort:** 2 days

#### 4. Extract paper mode from `engine/execution/polymarket_client.py`

**Why:** Paper and live mode interleave throughout 1,638 LOC. margin_engine separates them into `PaperExchangeAdapter` / `BinanceMarginAdapter` behind `ExchangePort`.

**Target state:**
```
engine/execution/
  ports.py                 -- ExchangePort ABC
  polymarket_client.py     -- Live mode only
  paper_client.py          -- Paper simulation
```

**Estimated effort:** 1 day

#### 5. Move hub inline migrations to Alembic

**Why:** 80+ lines of DDL in `main.py` lifespan + DDL helpers scattered across `v58_monitor.py`. Every hub boot runs ALTER TABLE statements. Should be tracked migrations.

**Estimated effort:** 1 day

### LATER (quality-of-life, no urgency)

#### 6. Add port interfaces to engine/signals/

The signals module is already clean. Adding `Protocol` interfaces (like gates.py already does) and moving pure-logic classes to a `domain/` folder would formalize what already works.

#### 7. Split `engine/alerts/telegram.py` (2,233 LOC)

Separate formatting (pure functions) from transport (HTTP to Telegram API) from throttling. telegram_v2.py already shows the right pattern.

#### 8. Frontend data layer

Extract API calls from pages into custom hooks or React Query. Split V58Monitor.jsx (3,118 LOC) into sub-components following the execution-hq/ pattern.

#### 9. Hub service extraction

Move business logic from route handlers into service classes. dashboard.py already delegates to `DashboardService` -- extend this pattern to trades, signals, paper, etc.

### NEVER (leave as-is)

#### 10. macro-observer/ and data-collector/

Both are standalone Railway services with DB-only coupling. They are small (1,193 and 880 LOC), isolated, and working. Refactoring them provides zero ROI.

#### 11. hub/db/schema_catalog.py and hub/db/config_seed.py

Large files (1,519 and 732 LOC) but they are static data definitions. Their size is inherent to the data they describe.

---

## Quick Wins (under 1 hour each)

1. **Add `__all__` to engine/persistence/__init__.py** listing the public API. Documents what the module exports.

2. **Move DDL constants out of v58_monitor.py** into `hub/db/migrations/manual_trades.py`. The route file should not contain `CREATE TABLE`.

3. **Create `engine/domain/` directory** with `__init__.py`. Move `reconciliation/state.py` there as the first domain model file. No code changes needed beyond the import path.

4. **Add type hints to CLOBReconciler constructor.** Replace bare `poly_client`, `db_pool`, `alerter` with protocol or ABC references.

5. **Remove `_DBShim` from trading_config.py.** Convert the 5 raw-SQL calls to SQLAlchemy ORM or pure `text()` with named params. The shim converts between two query styles unnecessarily.

6. **Fix `clob_feed.py` private attribute access.** Replace `self._poly._clob_client` with a public method on `PolymarketClient` (e.g., `get_order_book(token_id)`).

---

## Debt That Is Fine to Leave

| Item | Reason |
|------|--------|
| `engine/signals/` using `config.runtime_config` directly | Runtime config is a singleton read. Not worth abstracting for the Polymarket engine's lifecycle. |
| `hub/auth/` structure | 137 LOC total, clean, working. Not worth restructuring. |
| `hub/api/margin.py` proxy pattern | Thin passthrough is the correct architecture for a BFF (backend-for-frontend). |
| `engine/data/feeds/` concrete classes | These are infrastructure adapters by nature. Adding ABCs would be over-engineering for feeds that will never be swapped. |
| Frontend JSX page sizes | Large pages are acceptable when they represent complex, self-contained views. The data-surfaces are inherently 700-900 LOC. |
| `macro-observer/observer.py` single-file | The service does exactly one thing. A single file is correct. |

---

## Comparison to margin_engine/ Patterns

| Pattern | margin_engine | engine (Polymarket) | hub |
|---------|--------------|-------------------|-----|
| Domain entities with invariants | Position (state machine, validated transitions) | reconciliation/state.py (dataclasses, no invariants) | ORM models (no validation) |
| Value objects (frozen, validated) | Money, Price, CompositeSignal, ProbabilitySignal | WindowSignal, GateResult, GateContext (good) | None |
| Port interfaces (ABCs/Protocols) | 7 ports in domain/ports.py | gates.py uses Protocol for Gate | None |
| Repository pattern | PgPositionRepository implements PositionRepository | db_client.py (god-class, no interface) | SQLAlchemy session passed directly to routes |
| Use cases | OpenPositionUseCase, ManagePositionsUseCase | Strategies folder (excluded from audit) | Routes contain business logic inline |
| Composition root | main.py (manual DI, single file) | main.py (procedural wiring, large) | main.py (lifespan with inline migrations) |
| Adapter separation | Paper vs Binance behind ExchangePort | Paper/live interleaved in PolymarketClient | Proxy pattern (clean for BFF role) |
| Test strategy | Domain + use-case tests with mocked ports | Targeted tests for signals + reconciliation | Config tests only |

---

## Closing Notes

The repo has two architecturally distinct halves:

1. **margin_engine/** is a textbook Clean Architecture implementation. It was designed with ports, adapters, and a domain layer from the start. Any new module in the repo should follow its patterns.

2. **engine/** (Polymarket) and **hub/** grew organically over 6 months of rapid trading iteration. They are production-grade in reliability but architectural debt has accumulated. The signals module (`engine/signals/`) is the closest to clean architecture in the Polymarket side, with `gates.py` using Protocol-based pipeline composition.

The single most impactful refactoring target is `engine/persistence/db_client.py`. Splitting it into per-aggregate repositories with port interfaces would cascade improved testability and reduced coupling across the entire Polymarket engine. This is the recommendation with the highest ROI.
