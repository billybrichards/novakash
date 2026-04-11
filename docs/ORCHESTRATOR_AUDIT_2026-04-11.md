# Orchestrator Deep Clean-Architecture Audit

**File**: `engine/strategies/orchestrator.py`
**Branch**: `develop` @ `b90a39b`
**Date**: 2026-04-11
**LOC**: 3,579
**Auditor**: Clean-Architecture specialist agent
**Reference impl**: `margin_engine/domain/ports.py`, `margin_engine/main.py`
**Companion doc**: `docs/CLEAN_ARCHITECT_MIGRATION_PLAN.md` (Phase 0-8 plan for `engine/`)

---

## A. Method Inventory

Every method with line number, approximate LOC, single-sentence responsibility, its **ideal Clean Architecture layer**, and which layers it **actually mixes**.

| # | Line | Method | LOC | Responsibility | Ideal Layer | Actual Layers Mixed |
|---|------|--------|-----|---------------|-------------|-------------------|
| 1 | 84 | `__init__` | 346 | Construct all components (feeds, signals, strategies, clients, evaluators) from Settings | Infrastructure (composition root) | Infra + Adapter (reads `.env` file manually, resolves env vars bypassing pydantic) |
| 2 | 430 | `start` | 458 | Connect DB, exchange clients, start strategies, feed tasks, heartbeat, resolution polling, signal handlers | Infrastructure (composition root) | Infra + Adapter + Use Case (geoblock HTTP call L449, SQL DDL ensures L487-777, inline aiohttp L449-466) |
| 3 | 889 | `run` | 5 | Start + wait for shutdown | Infrastructure | Clean |
| 4 | 895 | `stop` | 73 | Graceful shutdown of all components | Infrastructure | Clean |
| 5 | 971 | `_handle_os_signal` | 4 | Set shutdown event on SIGINT/SIGTERM | Infrastructure | Clean |
| 6 | 978 | `_on_binance_trade` | 33 | Route Binance trade to aggregator, VPIN, regime, TWAP | Adapter (event router) | Adapter + Domain (inline TWAP window key parsing L1001-1010) |
| 7 | 1012 | `_start_cg_staggered` | 8 | Stagger CoinGlass feed startup to spread API load | Infrastructure | Clean |
| 8 | 1021 | `_coinglass_snapshot_recorder_loop` | 20 | Every 10s: record CG snapshots for all assets | Adapter (persistence scheduler) | Clean |
| 9 | 1042 | `_timesfm_forecast_recorder_loop` | 35 | Every 1s: fetch TimesFM forecast, record to DB | Adapter (persistence scheduler) | Adapter + Domain (inline window alignment math L1049-1051) |
| 10 | 1078 | `_on_oi_update` | 20 | CoinGlass OI to aggregator + cascade detector | Adapter (event router) | Clean |
| 11 | 1099 | `_on_polymarket_book` | 15 | Polymarket book to aggregator + arb scanner | Adapter (event router) | Clean |
| 12 | 1117 | `_on_vpin_signal` | 28 | VPIN signal to DB + strategies | Adapter (event router) | Adapter + Persistence (direct `self._db.write_signal` L1124) |
| 13 | 1146 | `_on_cascade_signal` | 28 | Cascade signal to DB + strategies | Adapter (event router) | Adapter + Persistence (direct `self._db.write_signal` L1153) |
| 14 | 1164 | `_on_arb_opportunities` | 23 | Arb opportunities to DB + strategies | Adapter (event router) | Adapter + Persistence (direct `self._db.write_signal` L1172) |
| 15 | 1188 | `_on_five_min_window` | ~300 | 5-min window signal handler: manage countdown, snapshot, Gamma price fetch, TWAP, TimesFM, Claude AI evaluation | Use Case (EvaluateWindow) | **ALL FOUR LAYERS** -- Domain (window math), Use Case (evaluation), Adapter (HTTP to Gamma API L1376, Anthropic API L1338), Infrastructure (DB writes L1419) |
| 16 | 1489 | `_on_fifteen_min_window` | ~57 | 15-min window handler -- delegates to five_min_strategy internals | Use Case routing | Adapter + Domain (reaches into `_five_min_strategy._pending_windows` L1512, `_recent_windows` L1514) |
| 17 | 1547 | `_evaluate_timesfm_window` | 17 | Route TimesFM window to strategy | Adapter (event router) | Adapter (reaches into strategy internals) |
| 18 | 1565 | `_on_order_resolution` | ~123 | OrderManager callback: update dedup set, record resolution to DB, send Telegram alerts, post-resolution AI | Use Case (ResolveOrder) | **ALL FOUR LAYERS** -- Domain (P&L calc), Use Case (resolution logic), Adapter (Telegram alert L1619, DB writes L1652), Infrastructure (asyncio.create_task) |
| 19 | 1689 | `_poly_fills_loop` | ~35 | Every 5 min: run PolyFillsReconciler.sync() | Infrastructure (scheduler) | Clean (delegates to reconciler) |
| 20 | 1725 | `_heartbeat_loop` | ~466 | Every 10s: system state update, mode toggle sync, feed status, SITREP Telegram message with stats/positions/skips | Use Case (PublishHeartbeat) | **ALL FOUR LAYERS** -- Domain (mode toggle logic L1791-1827), Use Case (state assembly), Adapter (direct SQL queries L1913-2060, Telegram message formatting L1858-2190), Infrastructure (inline `aiohttp` not used here but raw SQL) |
| 21 | 2192 | `_polymarket_reconcile_loop` | ~153 | Every 5 min: compare Polymarket data-api activity to local DB trades | Use Case (ReconcileTrades) | **THREE LAYERS** -- Use Case (reconciliation), Adapter (inline `aiohttp.get()` to `data-api.polymarket.com` L2212), Persistence (direct SQL L2230-2310) |
| 22 | 2346 | `_resolution_loop` | ~18 | Every 5s: poll OrderManager for resolved orders | Infrastructure (scheduler) | Clean (delegates to order_manager) |
| 23 | 2365 | `_redeemer_loop` | ~58 | Periodically redeem resolved Polymarket positions on-chain | Adapter (external service) | Adapter + Persistence (DB check L2372, DB write L2402) |
| 24 | 2424 | `_playwright_redeem_loop` | ~32 | Browser-based Polymarket redemption fallback | Adapter (external service) | Adapter + Persistence (DB check L2430, DB write L2436) |
| 25 | 2457 | `_playwright_balance_loop` | ~31 | Every 60s: scrape Polymarket balance via headless browser | Adapter (external service) | Adapter + Persistence (DB write L2467) |
| 26 | 2489 | `_playwright_screenshot_loop` | ~25 | Every 5 min: capture portfolio screenshot | Adapter (external service) | Adapter + Persistence (DB write L2496) |
| 27 | 2515 | `_market_state_loop` | ~33 | Consume aggregator stream, fan out MarketState to strategies | Adapter (event router) | Clean |
| 28 | 2550 | `_on_manual_trade_notify` | ~24 | PG LISTEN callback -- set asyncio event for manual trade fast path | Adapter (event handler) | Clean |
| 29 | 2575 | `_manual_trade_poller` | ~235 | Poll DB for pending manual trades, resolve token IDs, place CLOB orders | Use Case (ExecuteManualTrade) | **ALL FOUR LAYERS** -- Domain (direction mapping L2648), Use Case (execution orchestration), Adapter (CLOB `place_order` L2769, Telegram alerts L2732-2744, DB reads `_recent_windows` L2688), Persistence (direct SQL via `_db` methods L2644-2802) |
| 30 | 2812 | `_sot_reconciler_loop` | ~115 | Every 2 min: verify trades against Polymarket SOT | Infrastructure (scheduler) | Mostly clean (delegates to `CLOBReconciler.reconcile_manual_trades_sot`) |
| 31 | 2928 | `_position_monitor_loop` | ~230 | Every 30s: poll Polymarket positions API for resolutions | Use Case (MonitorPositions) | **ALL FOUR LAYERS** -- Domain (P&L determination L2973-2978), Use Case (resolution matching), Adapter (direct SQL L2987-3010, Telegram L3040-3140), Persistence (SQL via `self._db._pool.acquire()` L2986) |
| 32 | 3159 | `_shadow_resolution_loop` | ~308 | Every 30s: resolve shadow (un-traded) windows via Gamma API | Use Case (ResolveShadowTrades) | **ALL FOUR LAYERS** -- Domain (shadow P&L calc L3291-3296), Use Case (resolution logic), Adapter (inline `aiohttp` to Gamma API L3221-3237, Telegram L3317), Persistence (DB writes L3299-3313, direct SQL L3398-3454) |
| 33 | 3470 | `_staggered_execution_loop` | ~109 | G1/G3 guardrails: batch/stagger/best-signal selection for multi-asset windows | Use Case (StaggerExecution) | Use Case + Adapter (reaches into `_five_min_strategy._evaluate_window` L3561) |

**Summary**: 33 methods. 12 methods (36%) mix 3+ layers. 7 methods are "clean" or near-clean.

---

## B. Dependency Graph

### What the Orchestrator instantiates directly in `__init__` (L84-427):

```
Orchestrator
  +-- DBClient(settings)                       # persistence
  +-- MarketAggregator()                       # data aggregation
  +-- TelegramAlerter(bot_token, chat_id, ...) # alerts
  +-- VPINCalculator(on_signal=callback)        # signal processing
  +-- CascadeDetector(on_signal=callback)       # signal processing
  +-- ArbScanner(fee_mult, on_opportunities=cb) # signal processing
  +-- RegimeClassifier()                        # signal processing
  +-- PolymarketClient(keys, paper_mode)        # exchange execution
  +-- OpinionClient(keys, paper_mode)           # exchange execution
  +-- OrderManager(db, bankroll, paper_mode, poly_client) # execution mgmt
  +-- RiskManager(order_manager, bankroll, paper_mode)    # risk mgmt
  +-- PositionRedeemer(rpc, private_key, ...)   # on-chain execution
  +-- PlaywrightService(gmail, headless)        # browser automation
  +-- CoinGlassEnhancedFeed * N (BTC/ETH/SOL/XRP) # data feed
  +-- ClaudeEvaluator(api_key, alerter, db)     # AI evaluation
  +-- PostResolutionEvaluator(api_key, db, alerter) # AI post-mortem
  +-- SubDollarArbStrategy(order_mgr, risk_mgr, poly) # strategy
  +-- VPINCascadeStrategy(order_mgr, risk_mgr, poly, opinion) # strategy
  +-- FiveMinVPINStrategy(order_mgr, risk_mgr, poly, vpin, alerter, ...) # strategy
  +-- Polymarket5MinFeed(assets, signal_offset, on_window_signal=cb) # data feed
  +-- Polymarket5MinFeed(duration=900, ...)     # 15-min data feed
  +-- TimesFMClient(base_url, timeout)          # ML forecast client
  +-- BinanceWebSocketFeed(symbol, on_trade=cb, on_liq=cb) # data feed
  +-- CoinGlassAPIFeed(api_key, symbol, on_oi=cb, on_liq=cb) # data feed
  +-- ChainlinkRPCFeed(rpc_url, on_price=cb)   # data feed
  +-- PolymarketWebSocketFeed(token_ids, on_book=cb) # data feed
  +-- TWAPTracker(max_windows=50)               # signal processing
```

### What `start()` instantiates additionally (L430-887):

```
start()
  +-- TickRecorder(pool=self._db._pool)         # persistence
  +-- ChainlinkFeed(rpc_url, pool)              # data feed + DB
  +-- TiingoFeed(api_key, pool)                 # data feed + DB
  +-- CLOBFeed(poly_client, db_pool, polymarket_feed) # data feed + DB
  +-- ELMPredictionRecorder(elm_client, db_pool, shutdown) # persistence
  +-- PolyTradeHistoryReconciler(poly, db, alerter, shutdown) # reconciliation
  +-- PolyFillsReconciler(pool, funder_address)  # reconciliation
  +-- CLOBReconciler(poly, db_pool, alerter, shutdown) # reconciliation
```

### What the Orchestrator reads from (runtime):

```
Settings (pydantic)           -- constructor arg, read-only
os.environ                    -- read directly in 12+ places (bypasses Settings)
runtime_config.runtime        -- hot-reloadable flags (imported globally)
.env file (manual parse)      -- read directly for TIMESFM_*, TIINGO_API_KEY (L297-326, L524-532)
self._db._pool                -- leaks internal pool handle 14+ times
self._order_manager._current_btc_price  -- reaches into private field (L1602)
self._five_min_strategy._*    -- reaches into 8+ private fields (see Section E)
self._risk_manager.get_status()  -- reads status dict
self._twap_tracker._windows   -- reaches into private dict (L1316)
```

---

## C. Layer Violations

### C1. Inline HTTP calls (Infrastructure leaking into Use Case methods)

| Line(s) | URL | Method | Issue |
|---------|-----|--------|-------|
| 449-466 | `https://polymarket.com/api/geoblock` | `start()` | Geoblock check via raw `aiohttp` -- should be a `GeoblockPort` |
| 1336-1352 | `https://api.anthropic.com/v1/messages` | `_on_five_min_window()` | Inline Claude API call for window evaluation -- should use `ClaudeEvaluator` |
| 1376-1395 | `https://gamma-api.polymarket.com/events?slug=...` | `_on_five_min_window()` | Inline Gamma API for best-ask pricing -- should be `PolymarketClientPort.get_book()` |
| 2203-2224 | `https://data-api.polymarket.com/activity` | `_polymarket_reconcile_loop()` | Inline activity API -- should be adapter method |
| 3218-3244 | `https://gamma-api.polymarket.com/events?slug=...` | `_shadow_resolution_loop()` | Inline Gamma API for oracle resolution -- should be `PolymarketClientPort.get_window_market()` |
| 3413-3452 | `https://gamma-api.polymarket.com/events?slug=...` | `_shadow_resolution_loop()` | Duplicate Gamma API call for window_predictions resolution |

**Total**: 6 distinct inline HTTP call sites. 3 are to the same Gamma API endpoint (duplicated logic).

### C2. Direct SQL (Persistence leaking into orchestration methods)

The orchestrator accesses `self._db._pool` directly (bypassing the `DBClient` abstraction) in these methods:

| Line(s) | Method | SQL Operation |
|---------|--------|--------------|
| 493 | `start()` | `TickRecorder(pool=self._db._pool)` -- leaks pool |
| 507, 534, 557, 569, 681, 694, 710, 798 | `start()` | Pool handle passed to 8 components |
| 1740-1742 | `_heartbeat_loop()` | `self._db._pool` passed to alerter |
| 1913-1990 | `_heartbeat_loop()` | Raw `conn.fetch()` -- 3 separate SQL queries for recent trades, skips |
| 1965-1985 | `_heartbeat_loop()` | Raw `conn.fetch()` for recent trade outcomes |
| 2230-2310 | `_polymarket_reconcile_loop()` | Raw `conn.fetch()` + `conn.execute()` for trade comparison and update |
| 2986-3010 | `_position_monitor_loop()` | Raw `conn.fetchrow()` + `conn.execute()` for resolution matching |
| 3398-3454 | `_shadow_resolution_loop()` | Raw `conn.fetch()` + `conn.execute()` for window_predictions |

**Total**: 8+ methods access `self._db._pool` directly. The `DBClient` abstraction is bypassed whenever the orchestrator needs joins or complex queries.

### C3. Business logic that should be in use cases

| Line(s) | Logic | Target Use Case |
|---------|-------|----------------|
| 1188-1487 | Window countdown, evaluation scheduling, TWAP start, snapshot assembly, Gamma price fetch, entry cap selection | `EvaluateWindowUseCase` |
| 1565-1686 | Resolution matching, P&L computation, win/loss determination, alert assembly | `ResolveOrderUseCase` |
| 1725-2190 | System state assembly, mode toggle sync, kill switch detection, heartbeat SITREP formatting | `PublishHeartbeatUseCase` |
| 2575-2810 | Manual trade token resolution, CLOB order placement, paper fill simulation | `ExecuteManualTradeUseCase` |
| 2928-3155 | Position outcome polling, resolution matching by token_id, trade status update | `MonitorPositionsUseCase` |
| 3159-3466 | Shadow trade resolution, shadow P&L computation, prediction outcome resolution | `ResolveShadowTradesUseCase` |

### C4. Direct `os.environ` reads (bypassing Settings/ConfigPort)

| Line | Variable | Comment |
|------|----------|---------|
| 205 | `BUILDER_KEY` | Fallback for settings field |
| 295 | `TIMESFM_ENABLED` | Not in Settings |
| 306 | `TIMESFM_URL` | Duplicates settings.timesfm_url |
| 317 | `TIMESFM_MIN_CONFIDENCE` | Duplicates settings.timesfm_min_confidence |
| 351 | `V2_EARLY_ENTRY_ENABLED` | Not in Settings |
| 354 | `TIMESFM_V2_URL` | Not in Settings |
| 365 | `FIFTEEN_MIN_ENABLED` | Not in Settings |
| 366 | `FIFTEEN_MIN_ASSETS` | Not in Settings |
| 524 | `TIINGO_API_KEY` | Not in Settings |
| 792 | `RECONCILER_ENABLED` | Not in Settings |
| 868 | `SOT_RECONCILER_INTERVAL` | Not in Settings |
| 1700 | `POLY_FILLS_SYNC_INTERVAL_S` | Not in Settings |
| 1701 | `POLY_FILLS_LOOKBACK_HOURS` | Not in Settings |

Additionally, lines 297-326 manually parse `.env` file as a fallback for env vars, duplicating pydantic-settings' own `.env` loading.

---

## D. State Ownership

Every `self._*` field, its mutability, who reads/writes it, and whether it is duplicated elsewhere.

### D1. Core infrastructure state (set once in `__init__`, stable)

| Field | Type | Mutable? | Writers | Readers | Duplicated? |
|-------|------|----------|---------|---------|-------------|
| `_settings` | `Settings` | No | `__init__` | everywhere | No |
| `_shutdown_event` | `asyncio.Event` | Set once | `_handle_os_signal` | all loops | No |
| `_tasks` | `list[Task]` | Yes (append) | `start()` | `stop()` | No |
| `_db` | `DBClient` | No | `__init__` | 50+ reads | No, but `_db._pool` is leaked to 8+ components |
| `_aggregator` | `MarketAggregator` | No (internal state is mutable) | `__init__` | feeds, state loop | No |
| `_alerter` | `TelegramAlerter` | No (mutated via `set_*`) | `__init__` + `start()` | 20+ alert calls | No |

### D2. Feed instances (set once in `__init__` or `start()`)

| Field | Type | Mutable? | Writers | Readers |
|-------|------|----------|---------|---------|
| `_binance_feed` | `BinanceWebSocketFeed` | No | `__init__` | `start/stop` |
| `_coinglass_feed` | `CoinGlassAPIFeed` | No | `__init__` | `start/stop` |
| `_cg_enhanced` | `CoinGlassEnhancedFeed` | No | `__init__` | five_min_strategy |
| `_cg_feeds` | `dict[str, CoinGlassEnhancedFeed]` | No | `__init__` | recorder loop |
| `_chainlink_feed` | `ChainlinkRPCFeed` | No | `__init__` | `start/stop` |
| `_chainlink_multi_feed` | `ChainlinkFeed` | No | `start()` | `start/stop` |
| `_tiingo_feed` | `TiingoFeed` | No | `start()` | `start/stop` |
| `_clob_feed` | `CLOBFeed` | No | `start()` | `start/stop` |
| `_polymarket_feed` | `PolymarketWebSocketFeed` | No | `__init__` | `start/stop` |
| `_five_min_feed` | `Polymarket5MinFeed` | No | `__init__` | `start/stop` |
| `_fifteen_min_feed` | `Polymarket5MinFeed` | No | `__init__` | `start/stop` |

### D3. Signal processors and clients

| Field | Type | Mutable? | Writers | Readers |
|-------|------|----------|---------|---------|
| `_vpin_calc` | `VPINCalculator` | No (internal state is mutable) | `__init__` | `_on_binance_trade`, staggered exec |
| `_cascade` | `CascadeDetector` | No (internal state is mutable) | `__init__` | `_on_oi_update` |
| `_arb_scanner` | `ArbScanner` | No | `__init__` | `_on_polymarket_book` |
| `_regime` | `RegimeClassifier` | No (internal state is mutable) | `__init__` | `_on_binance_trade` |
| `_twap_tracker` | `TWAPTracker` | Yes (windows are added/read) | `__init__` | `_on_five_min_window`, `_on_binance_trade`, five_min_strategy (via injection) |
| `_timesfm_client` | `TimesFMClient` | No | `__init__` | forecast recorder, five_min_strategy (injected) |
| `_timesfm_strategy` | `TimesFMOnlyStrategy` | No | `__init__` (always None currently) | `start/stop` |
| `_timesfm_multi` | `TimesFMMultiEntryStrategy` | No | `__init__` (always None currently) | `start/stop` |

### D4. Execution and risk

| Field | Type | Mutable? | Writers | Readers |
|-------|------|----------|---------|---------|
| `_poly_client` | `PolymarketClient` | No | `__init__` | order_manager, strategies, reconciler, manual trade poller |
| `_opinion_client` | `OpinionClient` | No | `__init__` | cascade_strategy |
| `_order_manager` | `OrderManager` | No (internal state is mutable) | `__init__` | strategies, resolution callback, heartbeat |
| `_risk_manager` | `RiskManager` | No (internal state is mutable) | `__init__` | strategies, heartbeat |
| `_redeemer` | `PositionRedeemer` | No | `__init__` | redeemer loop |
| `_playwright` | `PlaywrightService` | No | `__init__` | playwright loops |

### D5. Strategies

| Field | Type | Mutable? | Writers | Readers | Coupling Issue |
|-------|------|----------|---------|---------|----------------|
| `_arb_strategy` | `SubDollarArbStrategy` | No | `__init__` | `start/stop`, market state | None |
| `_cascade_strategy` | `VPINCascadeStrategy` | No | `__init__` | `start/stop`, market state | None |
| `_five_min_strategy` | `FiveMinVPINStrategy` | No | `__init__` | **24+ reads of private fields** | **SEVERE** -- see Section E |

### D6. Mutable orchestration state

| Field | Type | Mutable? | Writers | Readers | Duplicated? |
|-------|------|----------|---------|---------|-------------|
| `_execution_queue` | `asyncio.Queue` | Yes | `_on_five_min_window` (put) | `_staggered_execution_loop` (get) | No |
| `_geoblock_active` | `bool` | Yes | `start()` | five_min_strategy via lambda | No |
| `_manual_trade_notify_event` | `asyncio.Event` | Yes | `_on_manual_trade_notify` | `_manual_trade_poller` | No |
| `_resolved_by_order_manager` | `set` | Yes (add) | `_on_order_resolution` | `_position_monitor_loop` | Duplicated in `CLOBReconciler._known_resolved` |
| `_reconciler` | `CLOBReconciler` | Set once | `start()` | `stop()`, `_sot_reconciler_loop` | No |
| `_tick_recorder` | `TickRecorder` | Set once | `start()` | feed callbacks, five_min_strategy (injected) | No |
| `_countdown_sent` | `dict` (implied) | Yes | `_on_five_min_window` | `_on_five_min_window` | Undefined in `__init__` -- first access is via `get` with default, may raise `AttributeError` in edge cases |
| `_claude_evaluator` | `ClaudeEvaluator` | No | `__init__` | `_on_five_min_window` | No |
| `_post_resolution_evaluator` | `PostResolutionEvaluator` | No | `__init__` | `_shadow_resolution_loop` | No |
| `_poly_fills_reconciler` | `PolyFillsReconciler` | Set once | `start()` | `_poly_fills_loop` | No |

### D7. Missing from `__init__` (late-bound or implicit)

| Field | First Use | Issue |
|-------|-----------|-------|
| `_countdown_sent` | L1268 | Referenced via `self._countdown_sent.get(...)` but never initialized in `__init__`. Works because Python dicts tolerate `.get()` on missing keys, but the dict itself is never created -- this is likely a bug or the field exists on an inherited class. |
| `_poly_fills_reconciler` | L709 | Only created if conditions met in `start()` |

---

## E. Coupling to `five_min_vpin.py`

The orchestrator reaches into `FiveMinVPINStrategy` private fields **24+ times** across 7 methods. This is the single worst coupling in the codebase.

### E1. Private field injection (orchestrator writes to strategy internals)

| Line | Access | Direction | Severity |
|------|--------|-----------|----------|
| 347 | `self._five_min_strategy._timesfm = self._timesfm_client` | Write | **HIGH** -- injects dependency after construction |
| 355 | `self._five_min_strategy._timesfm_v2 = TimesFMV2Client(...)` | Write | **HIGH** -- injects dependency after construction |
| 498 | `self._five_min_strategy._tick_recorder = self._tick_recorder` | Write | **HIGH** -- injects dependency after construction |

**Root cause**: The strategy's constructor runs before `start()` connects the DB pool. The orchestrator patches private fields post-construction rather than using a setter or deferred initialization pattern.

### E2. Private field reads (orchestrator reads strategy internals)

| Line | Access | Method | What it reads |
|------|--------|--------|--------------|
| 677 | `self._five_min_strategy._timesfm_v2` | `start()` | Checks if v2 client was injected |
| 1211 | `self._five_min_strategy._pending_windows.append(window)` | `_on_five_min_window` | **Writes** to internal pending queue |
| 1213-1214 | `self._five_min_strategy._recent_windows` | `_on_five_min_window` | **Writes** to internal ring buffer |
| 1272 | `self._five_min_strategy._vpin.current_vpin` | `_on_five_min_window` | Reads VPIN through strategy's private ref |
| 1279-1282 | `self._five_min_strategy._timesfm.get_forecast(...)` | `_on_five_min_window` | Calls method on injected private field |
| 1481 | `self._five_min_strategy._evaluate_window(window, state)` | `_on_five_min_window` | Calls private evaluation method |
| 1486-1487 | `self._five_min_strategy._recent_windows` | `_on_five_min_window` | Trims ring buffer |
| 1512-1515 | `self._five_min_strategy._pending_windows`, `_recent_windows` | `_on_fifteen_min_window` | Same pattern as above |
| 1542-1543 | `self._five_min_strategy._recent_windows` | `_on_fifteen_min_window` | Trims ring buffer |
| 1609 | `self._five_min_strategy._vpin.current_vpin` | `_on_order_resolution` | Reads VPIN for alert metadata |
| 2688 | `self._five_min_strategy._recent_windows` | `_manual_trade_poller` | Token ID lookup from ring buffer |
| 3351 | `self._five_min_strategy._window_eval_history` | `_shadow_resolution_loop` | Reads eval history for post-resolution AI |
| 3561 | `self._five_min_strategy._evaluate_window(w, state)` | `_staggered_execution_loop` | Calls private evaluation method |

### E3. Summary of coupling

- **3 post-construction field injections** (write to private fields)
- **10+ private field reads** (bypass public API)
- **2 private method calls** (`_evaluate_window`)
- The orchestrator and strategy are **effectively one class split across two files**
- Neither can be tested independently

### E4. What the migration plan says vs what exists

The migration plan (Section 5.2) proposes "Extract `EvaluateWindowUseCase`" and "Lift window ring-buffer into `WindowStateRepository`". This audit confirms those are correct but finds the coupling is deeper than the plan acknowledges:
- The orchestrator also **writes** to the strategy's `_pending_windows` and `_recent_windows` directly
- The orchestrator reads `_vpin` through the strategy instead of having its own reference
- The `_staggered_execution_loop` calls `_evaluate_window` directly, making it impossible to replace the strategy without changing the orchestrator

---

## F. Decomposition Plan

### F1. Use Cases to extract (matches and extends migration plan)

| Use Case | Source Lines | LOC | Migration Plan Coverage | Priority |
|----------|------------|-----|------------------------|----------|
| `EvaluateWindowUseCase` | 1188-1487 | ~300 | Section 5.2 (partial) | **P0** -- hottest path |
| `ExecuteManualTradeUseCase` | 2575-2810 | ~235 | Section 5.6 (mentioned) | **P1** -- live money |
| `PublishHeartbeatUseCase` | 1725-2190 | ~466 | Section 5.8 (mentioned) | P2 -- large but low risk |
| `ResolveOrderUseCase` | 1565-1686 | ~123 | Section 5.5 (partial, "oracle resolution") | **P1** -- live money |
| `ResolveShadowTradesUseCase` | 3159-3466 | ~308 | Not mentioned | P2 -- read-only |
| `MonitorPositionsUseCase` | 2928-3155 | ~230 | Not mentioned | P2 -- replaced by CLOB reconciler |
| `ReconcileTradesUseCase` | 2192-2340 | ~153 | Not mentioned (legacy) | P3 -- being deprecated |
| `StaggerExecutionUseCase` | 3470-3579 | ~109 | Section 5.3 (guardrails) | P2 |
| `RedeemPositionsUseCase` | 2365-2455 | ~90 | Not mentioned | P3 |

### F2. Adapter extractions

| Adapter | Source Lines | Current Inline Code | Target |
|---------|------------|-------------------|--------|
| `GammaApiAdapter` | 1376-1395, 3218-3244, 3413-3452 | 3 duplicated Gamma API call sites | Implements `PolymarketClientPort.get_window_market()` |
| `GeoblockAdapter` | 449-466 | Inline aiohttp geoblock check | Standalone adapter or part of `PolymarketClientPort` |
| `DataApiAdapter` | 2203-2224 | Inline Polymarket data-api activity fetch | Implements reconciliation read port |

### F3. What the migration plan missed

1. **`_on_five_min_window` is 300 LOC, not "just a callback"**. The plan treats it as a routing function but it contains full window countdown logic, snapshot assembly, Gamma price fetch, TWAP start, Claude AI evaluation, and entry cap selection. This is the actual `EvaluateWindowUseCase` body.

2. **`_manual_trade_poller` is a full use case (235 LOC)**. The plan mentions "manual trade execution" under Section 5.6 but doesn't call out the token ID resolution fallback chain (ring buffer -> market_data DB -> fail), the paper/live fork, or the POLY-SOT persistence.

3. **`_position_monitor_loop` and `_shadow_resolution_loop` are undocumented**. Combined 538 LOC of use-case-level logic with inline HTTP and SQL. The migration plan doesn't mention either.

4. **The heartbeat is 466 LOC of mixed concerns**. The plan mentions "heartbeat" once. In reality it contains mode toggle sync (reading from DB, comparing paper/live state, triggering kill switch), feed health checks, wallet balance assembly, position formatting, and a multi-section Telegram SITREP builder. This should be at least `PublishHeartbeatUseCase` + `SyncModeTogglesUseCase`.

5. **The `_countdown_sent` state appears uninitialized**. Line 1268 references `self._countdown_sent.get(...)` but this field is never set in `__init__`. This may cause an `AttributeError` on the first 5-min window if the field doesn't exist on a parent class.

6. **Three post-construction injections into five_min_strategy** (L347, L355, L498). The plan proposes dependency injection but doesn't call out that the current code monkey-patches private fields after the constructor returns.

---

## G. Risk Matrix

### G1. DANGEROUS to move (live money at stake)

| Method | Lines | Risk | Why |
|--------|-------|------|-----|
| `_manual_trade_poller` | 2575-2810 | **CRITICAL** | Places CLOB orders with real USDC. Token ID resolution chain is fragile. POLY-SOT persistence is the safety net. Any regression means lost trades or double-execution. |
| `_on_order_resolution` | 1565-1686 | **HIGH** | Callback from OrderManager on every fill/resolution. Writes outcome to DB, fires Telegram. If this breaks, trade outcomes are lost and the operator has no visibility. |
| `_redeemer_loop` | 2365-2455 | **HIGH** | Triggers on-chain transactions to redeem resolved positions. Double-redeem is protected by DB flag but a regression could waste gas or miss redemptions. |
| `_sot_reconciler_loop` | 2812-2927 | **HIGH** | Safety net that detects missed fills. If this stops running silently, diverged trades accumulate undetected. |
| `_on_five_min_window` (execution path) | 1188-1487 | **HIGH** | The trade evaluation and execution pipeline. A refactor bug could cause missed trades or wrong-direction trades. |

### G2. MODERATE risk (data integrity, not money)

| Method | Lines | Risk | Why |
|--------|-------|------|-----|
| `_heartbeat_loop` | 1725-2190 | **MODERATE** | Mode toggle sync (L1791-1827) controls paper/live switching. A bug could flip a paper engine to live mode or vice versa. The heartbeat SITREP is operator visibility -- losing it is a monitoring gap, not a money loss. |
| `_position_monitor_loop` | 2928-3155 | **MODERATE** | Legacy -- being replaced by CLOB reconciler. Already disabled when `RECONCILER_ENABLED=true`. Moving it is low risk because the new code path doesn't use it. |
| `_shadow_resolution_loop` | 3159-3466 | **MODERATE** | Read-only from Polymarket's perspective. Shadow P&L is observational, not financial. A bug would corrupt shadow analytics but not real trades. |
| `_polymarket_reconcile_loop` | 2192-2340 | **LOW** | Legacy, gated behind `RECONCILER_ENABLED=false`. Not active in production. |

### G3. SAFE to move (pure infrastructure or low-stakes)

| Method | Lines | Risk | Why |
|--------|-------|------|-----|
| `start` / `stop` / `run` | 430-967 | **LOW** | Composition root. Refactoring this is structural -- the components being wired don't change. Main risk is ordering bugs (feed started before DB is connected). |
| `_market_state_loop` | 2515-2548 | **LOW** | Pure fan-out, no business logic. |
| `_staggered_execution_loop` | 3470-3579 | **LOW** | Guardrail wrapper. The execution itself is delegated to `_evaluate_window`. |
| `_coinglass_snapshot_recorder_loop` | 1021-1040 | **LOW** | Telemetry recorder. No trading impact. |
| `_timesfm_forecast_recorder_loop` | 1042-1076 | **LOW** | Telemetry recorder. No trading impact. |
| `_poly_fills_loop` | 1689-1724 | **LOW** | Delegates to `PolyFillsReconciler.sync()`. Pure scheduler. |
| `_playwright_*` methods | 2424-2514 | **LOW** | Browser automation. No trading logic. |
| `_on_binance_trade` | 978-1010 | **LOW** | Event router with no business decisions. |
| `_on_oi_update`, `_on_polymarket_book` | 1078-1113 | **LOW** | Event routers. |
| `_on_vpin_signal`, `_on_cascade_signal`, `_on_arb_opportunities` | 1117-1186 | **LOW** | Signal recorders and routers. |

---

## Recommended Migration Sequence

Based on the risk matrix and coupling analysis, the recommended order is:

1. **Phase 0 (done)**: `engine/domain/ports.py` and `engine/domain/value_objects.py` -- already landed on develop.

2. **Phase 1: Decouple five_min_strategy** (Section E). Add public methods to `FiveMinVPINStrategy`:
   - `inject_timesfm(client)`, `inject_timesfm_v2(client)`, `inject_tick_recorder(recorder)` -- replace monkey-patching
   - `add_window(window)` -- replace direct `_pending_windows.append()`
   - `get_recent_window_by_ts(ts)` -- replace direct `_recent_windows` scan
   - `evaluate_window(window, state)` -- make `_evaluate_window` public
   - `get_current_vpin()` -- expose without leaking `_vpin`

3. **Phase 2: Extract safe methods first** (G3 methods). Move telemetry loops, fan-out, and playwright into standalone adapter classes. This builds confidence without touching hot paths.

4. **Phase 3: Extract `EvaluateWindowUseCase`** from `_on_five_min_window`. This is the largest single method (300 LOC) and the most architecturally important. Requires Phase 1 to decouple strategy internals first.

5. **Phase 4: Extract `ExecuteManualTradeUseCase`** and `ResolveOrderUseCase`. Both handle live money and must be extracted with full test coverage using the `InMemoryPolymarketClient` and `InMemoryWindowStateRepository` from `engine/domain/ports.py`.

6. **Phase 5: Extract heartbeat and reconciliation use cases**. Large but lower risk. `PublishHeartbeatUseCase` consumes state from all other components and should be extracted last.

7. **Phase 6: Slim the Orchestrator to pure composition root** (~200 LOC). At this point the orchestrator only creates components, wires callbacks, manages task lifecycle, and handles shutdown -- matching `margin_engine/main.py`'s shape.

---

## Metrics Summary

| Metric | Value |
|--------|-------|
| Total LOC | 3,579 |
| Methods | 33 |
| Methods mixing 3+ layers | 12 (36%) |
| Inline HTTP call sites | 6 |
| Direct SQL access sites | 8+ |
| `os.environ` reads bypassing Settings | 13 |
| Manual `.env` parse sites | 3 |
| Private field accesses into five_min_strategy | 24+ |
| Post-construction monkey-patches | 3 |
| `self._*` state fields | 40+ |
| `self._db._pool` leaks | 14+ |
| Inline Gamma API duplications | 3 (same endpoint) |
| Use cases embedded in orchestrator | 9 |
| Use cases not covered by migration plan | 4 |
