# Orchestrator Deep Clean Architecture Audit

**File**: `engine/strategies/orchestrator.py`
**Total LOC**: 3,579 (as measured; migration plan says 3,330 -- file has grown)
**Date**: 2026-04-11
**Auditor**: Claude Opus 4.6 (Clean Architecture specialist)
**Reference impl**: `margin_engine/` (ports-and-adapters, 483-line composition root)
**Companion doc**: `docs/CLEAN_ARCHITECT_MIGRATION_PLAN.md` (covers orchestrator at section 2.2 only)

---

## A. Method Inventory

Every method with line number, approximate LOC, responsibility, the Clean Architecture layer it **should** belong to, and which layers it **actually mixes**.

| # | Method | Line | LOC | Responsibility | Target Layer | Actual Layers Mixed |
|---|--------|------|-----|---------------|-------------|-------------------|
| 1 | `__init__` | 84 | 343 | Component creation, wiring, callback registration, env-var parsing, .env file parsing | Infrastructure (composition root) | Infrastructure + Adapter (inline `os.environ`/`.env` parsing, `aiohttp` import) |
| 2 | `start` | 430 | 458 | DB connect, feed start, task creation, geoblock check, schema migrations, trade recovery, reconciler wiring | Infrastructure (lifecycle) | Infrastructure + Adapter (inline `aiohttp` geoblock call L449-472) + Persistence (direct `self._db._pool` access L493,507,534,551,569) |
| 3 | `run` | 889 | 5 | Start + wait + stop | Infrastructure (lifecycle) | Clean |
| 4 | `stop` | 895 | 72 | Graceful shutdown of all components | Infrastructure (lifecycle) | Clean (orchestration only) |
| 5 | `_handle_os_signal` | 971 | 3 | Set shutdown event on SIGINT/SIGTERM | Infrastructure | Clean |
| 6 | `_on_binance_trade` | 978 | 33 | Route Binance trade to aggregator, VPIN, regime, TWAP, tick recorder, order manager | Interface Adapter (event router) | Adapter + Domain (TWAP tick injection with string parsing L1000-1010) |
| 7 | `_start_cg_staggered` | 1012 | 8 | Start CoinGlass feed with stagger delay | Infrastructure | Clean |
| 8 | `_coinglass_snapshot_recorder_loop` | 1021 | 20 | Periodic CoinGlass snapshot to tick recorder | Infrastructure (scheduled task) | Clean |
| 9 | `_timesfm_forecast_recorder_loop` | 1042 | 35 | 1s TimesFM forecast recording with window alignment | Infrastructure (scheduled task) | Domain (window-time math L1049-1051) |
| 10 | `_on_oi_update` | 1078 | 20 | Route OI snapshot to aggregator + cascade detector | Interface Adapter (event router) | Clean |
| 11 | `_on_polymarket_book` | 1099 | 15 | Route Polymarket book to aggregator + arb scanner | Interface Adapter (event router) | Clean |
| 12 | `_on_vpin_signal` | 1117 | 27 | Route VPIN signal to aggregator + DB + cascade | Interface Adapter (event router) | Adapter (DB write L1124-1130) |
| 13 | `_on_cascade_signal` | 1146 | 16 | Route cascade signal to aggregator + DB + Telegram | Interface Adapter (event router) | Adapter (DB write L1153-1159, Telegram L1162) |
| 14 | `_on_arb_opportunities` | 1164 | 22 | Route arb opps to aggregator + DB | Interface Adapter (event router) | Adapter (DB write L1172-1184) |
| 15 | `_on_five_min_window` | 1188 | 300 | Handle 5-min window signal: TWAP tracking, countdown notifications, snapshot DB writes, inline Anthropic API call, Gamma API price fetch, AI commentary | **GOD METHOD** | ALL FOUR: Domain (TWAP math, regime classification L1275-1276, P&L calc), Use Case (window evaluation dispatch L1481), Adapter (inline `aiohttp` Anthropic API L1337-1350, inline `aiohttp` Gamma API L1377-1391, DB writes L1419-1431), Infrastructure (scheduling, dedup state) |
| 16 | `_get_full_snapshot` (nested) | 1271 | 82 | Build signal snapshot for countdown notifications | Use Case + Adapter | Domain (regime classification, VPIN thresholds) + Adapter (TimesFM HTTP L1282, CoinGlass snapshot read, TWAP state access L1316-1321, inline Anthropic API L1337-1350) |
| 17 | `_on_fifteen_min_window` | 1489 | 55 | Handle 15-min window signal (duplicates _on_five_min_window logic) | Interface Adapter (event router) | Adapter + Domain (TWAP math, proxy price calc duplicated from L1226-1237) |
| 18 | `_evaluate_timesfm_window` | 1547 | 15 | Evaluate window with TimesFM-only strategy | Interface Adapter (dispatch) | Clean (delegates to strategy) |
| 19 | `_on_order_resolution` | 1565 | 122 | Handle order resolution: PnL recording, Telegram alerts, win/loss notification, AI outcome analysis | Use Case (resolution handling) | Domain (P&L direction logic L1604-1608) + Adapter (risk manager L1591, Telegram L1619-1638, inline Anthropic API via alerter L1667-1677) + Persistence (order manager internals L1602) |
| 20 | `_record_and_alert` (nested) | 1589 | 96 | Record PnL + send resolution alert + AI analysis | Use Case | Adapter (risk manager, Telegram, order manager internals) |
| 21 | `_send_outcome_ai` (nested) | 1667 | 10 | Fire AI outcome analysis task | Adapter | Clean (delegates to alerter) |
| 22 | `_poly_fills_loop` | 1689 | 35 | Periodic poly_fills reconciliation | Infrastructure (scheduled task) | Clean (delegates to reconciler) |
| 23 | `_heartbeat_loop` | 1725 | 466 | **SECOND GOD METHOD**: heartbeat, wallet sync, mode switching, feed status, sitrep generation with raw SQL, runtime config sync | ALL FOUR | Domain (regime classification L1953-1958, P&L calc, WR calc L2154) + Use Case (mode switching L1797-1862) + Adapter (raw SQL L1914-1922, L1967-2148, Telegram L2175) + Infrastructure (config sync L1741-1744) |
| 24 | `_label_prefix` (nested) | 2077 | 4 | Visual marker for orphan vs trigger resolution | Presentation | Clean |
| 25 | `_polymarket_reconcile_loop` | 2192 | 152 | Legacy reconciliation loop: inline HTTP to Polymarket data API, raw SQL | Use Case (reconciliation) | Adapter (inline `aiohttp` L2212-2224, raw SQL L2231-2310, hardcoded URL L2204-2206) |
| 26 | `_resolution_loop` | 2346 | 18 | Poll order resolutions every 5s | Infrastructure (scheduled task) | Clean (delegates to order manager) |
| 27 | `_redeemer_loop` | 2365 | 55 | Auto-redeem positions every 5 min | Infrastructure (scheduled task) | Adapter (DB write L2402, Telegram L2391-2397) |
| 28 | `_playwright_redeem_loop` | 2424 | 31 | Auto-redeem via Playwright every 5 min | Infrastructure (scheduled task) | Adapter (DB write, Telegram) |
| 29 | `_playwright_balance_loop` | 2457 | 30 | Poll balance via Playwright every 60s | Infrastructure (scheduled task) | Adapter (DB write L2467-2475, Playwright private attrs L2469) |
| 30 | `_playwright_screenshot_loop` | 2489 | 24 | Capture screenshot every 30s | Infrastructure (scheduled task) | Adapter (DB write, Playwright private attrs) |
| 31 | `_market_state_loop` | 2515 | 34 | Fan-out aggregator stream to strategies | Infrastructure (event loop) | Clean |
| 32 | `_on_manual_trade_notify` | 2550 | 14 | Handle pg_notify callback for manual trades | Interface Adapter (event handler) | Clean |
| 33 | `_manual_trade_poller` | 2575 | 235 | **THIRD GOD METHOD**: Poll pending manual trades, resolve token IDs from ring buffer or DB, submit FOK orders, paper/live branching, Telegram alerts | Use Case (manual trade execution) | Domain (direction mapping L2648) + Adapter (DB reads L2644,2704, CLOB order submission L2769-2775, Telegram L2791-2798) + Infrastructure (LISTEN/NOTIFY L2601-2604) |
| 34 | `_sot_reconciler_loop` | 2812 | 114 | Source-of-truth reconciliation for manual+automatic trades | Infrastructure (scheduled task) | Clean (mostly delegates to CLOBReconciler) |
| 35 | `_position_monitor_loop` | 2928 | 228 | Monitor Polymarket positions for resolutions, link to DB trades, send Telegram | Use Case (position resolution) | Adapter (raw SQL L2988-3024, L3046-3077, L3093-3111, Telegram L3137, inline `aiohttp` wallet balance L3081-3083) + Persistence (order manager internals L2929) |
| 36 | `_shadow_resolution_loop` | 3159 | 307 | Resolve oracle outcomes for skipped windows, compute shadow P&L, post-resolution AI analysis | Use Case (shadow resolution) | Adapter (inline `aiohttp` Gamma API L3221-3237, DB writes L3299-3306, L3401-3452, Telegram L3317-3325) + Domain (shadow P&L calc L3290-3296, confidence tier mapping L3206-3211) |
| 37 | `_staggered_execution_loop` | 3470 | 109 | G1/G3: batch and stagger window evaluation execution | Infrastructure (execution scheduler) | Domain (scoring logic L3511-3521) + Adapter (strategy internals L3561) |

### Summary Statistics

- **Total methods**: 37 (including 4 nested)
- **Methods that are "clean" (single layer)**: 10 (`run`, `stop`, `_handle_os_signal`, `_start_cg_staggered`, `_coinglass_snapshot_recorder_loop`, `_on_oi_update`, `_on_polymarket_book`, `_evaluate_timesfm_window`, `_resolution_loop`, `_market_state_loop`)
- **Methods that violate the dependency rule**: 27 (73%)
- **God methods (>100 LOC, 3+ layers)**: 5 (`__init__`, `_on_five_min_window`, `_heartbeat_loop`, `_manual_trade_poller`, `_shadow_resolution_loop`)
- **Methods with inline HTTP calls**: 5 (`start`, `_on_five_min_window`, `_polymarket_reconcile_loop`, `_position_monitor_loop`, `_shadow_resolution_loop`)
- **Methods with raw SQL**: 4 (`_heartbeat_loop`, `_polymarket_reconcile_loop`, `_position_monitor_loop`, `_shadow_resolution_loop`)

---

## B. Dependency Graph

### B.1 What Orchestrator Instantiates (in `__init__` + `start`)

```
Orchestrator
  |
  |-- DBClient(settings)                          # Persistence
  |-- MarketAggregator()                          # Data aggregation
  |-- TelegramAlerter(bot_token, chat_id, ...)    # Alerts
  |-- VPINCalculator(on_signal=callback)          # Signal processing
  |-- CascadeDetector(on_signal=callback)         # Signal processing
  |-- ArbScanner(fee_mult, on_opportunities=cb)   # Signal processing
  |-- RegimeClassifier()                          # Signal processing
  |-- TWAPTracker(max_windows=50)                 # Signal processing
  |-- PolymarketClient(private_key, ...)          # Exchange client
  |-- OpinionClient(api_key, ...)                 # Exchange client
  |-- OrderManager(db, bankroll, ...)             # Order management
  |-- RiskManager(order_manager, bankroll, ...)   # Risk management
  |-- PositionRedeemer(rpc_url, ...)              # Execution (import at line 199)
  |-- PlaywrightService(gmail, ...)               # Browser automation (conditional)
  |-- CoinGlassEnhancedFeed(api_key, symbol)      # Data feed (x4 assets)
  |-- ClaudeEvaluator(api_key, alerter, db)       # AI evaluation (conditional)
  |-- PostResolutionEvaluator(api_key, db, alert) # AI evaluation (conditional)
  |-- SubDollarArbStrategy(om, rm, poly)          # Strategy
  |-- VPINCascadeStrategy(om, rm, poly, opinion)  # Strategy
  |-- Polymarket5MinFeed(assets, signal_offset..) # Data feed (conditional)
  |-- FiveMinVPINStrategy(om, rm, poly, vpin...)  # Strategy (conditional)
  |-- TimesFMClient(base_url, timeout)            # ML client (conditional)
  |-- TimesFMOnlyStrategy(...)                    # Strategy (conditional, unused v5.8+)
  |-- TimesFMMultiEntryStrategy(...)              # Strategy (conditional, unused v5.8+)
  |-- TimesFMV2Client(base_url)                   # ML client (line 354, injected)
  |-- BinanceWebSocketFeed(symbol, on_trade=cb)   # Data feed
  |-- CoinGlassAPIFeed(api_key, symbol, ...)      # Data feed (conditional)
  |-- ChainlinkRPCFeed(rpc_url, on_price=cb)      # Data feed (conditional)
  |-- PolymarketWebSocketFeed(token_ids, on_book)  # Data feed
  |
  |-- [start() creates]:
  |   |-- TickRecorder(pool=self._db._pool)       # Persistence (accesses private pool)
  |   |-- ChainlinkFeed(rpc_url, pool=db._pool)   # Data feed
  |   |-- TiingoFeed(api_key, pool=db._pool)       # Data feed (env var + .env parsing)
  |   |-- CLOBFeed(poly_client, db_pool, ...)      # Data feed
  |   |-- ELMPredictionRecorder(elm, db_pool, ..)  # Persistence (line 679, import)
  |   |-- PolyTradeHistoryReconciler(poly, pool..)  # Reconciliation (line 692, import)
  |   |-- PolyFillsReconciler(pool, funder)         # Reconciliation (line 709)
  |   |-- CLOBReconciler(poly, pool, alerter, ..)   # Reconciliation (line 796, import)
```

**Total concrete dependencies**: ~35 classes instantiated directly (zero interfaces/ports).

### B.2 What Orchestrator Reads From

| Source | Access Pattern | Locations |
|--------|---------------|-----------|
| `self._db._pool` (private attr) | Direct asyncpg pool access for raw SQL | L493, L507, L534, L551, L569, L681, L690, L707, L740, L913-1927, L2230, L2305, L2859, L2987, L3046, L3093, L3401 |
| `self._aggregator._state` (private attr) | Direct state access bypassing getter | L1273 |
| `self._order_manager._orders` (private attr) | Direct dict access | L1927 |
| `self._order_manager._current_btc_price` (private) | Direct field read | L1602, L2172 |
| `self._poly_client.paper_mode` (public) | Direct field read for mode comparison | L1799, L2753 |
| `self._playwright._logged_in` (private) | Direct field access | L2469, L2497 |
| `self._playwright._browser_alive` (private) | Direct field access | L2469, L2497 |
| `self._alerter._anthropic_api_key` (private) | Direct field access for AI call guard | L1324 |
| `self._alerter._paper_mode` (private) | Direct field mutation | L1822 |
| `self._risk_manager._paper_mode` (private) | Direct field mutation | L1819 |
| `self._five_min_strategy._vpin` (private) | VPIN access via strategy internal | L1272, L1609 |
| `self._five_min_strategy._timesfm` (private) | TimesFM client via strategy internal | L1279-1282 |
| `self._five_min_strategy._pending_windows` (private) | Direct list mutation | L1211, L1512 |
| `self._five_min_strategy._recent_windows` (private) | Direct list access/mutation | L1213-1214, L1486-1487, L1514-1515, L1542-1543, L2688 |
| `self._five_min_strategy._window_eval_history` (private) | Direct dict access | L3351 |
| `self._five_min_strategy._evaluate_window` (private) | Direct method call | L1481, L3561 |
| `self._five_min_strategy._timesfm_v2` (private) | Injected attribute | L355, L677 |
| `self._five_min_strategy._tick_recorder` (private) | Injected attribute | L498 |
| `os.environ` / `.env` file parsing | Inline env var reads bypassing Settings | L295-326, L351-358, L365-366, L524-532, L792, L1700-1701, L2868 |
| `runtime` (global singleton) | Config hot-reload reads | L1741-1744, L1954-1957, L3510, L3545 |

### B.3 What Orchestrator Passes Down (dependency injection)

The orchestrator passes concrete instances, never interfaces:

```
FiveMinVPINStrategy receives:
    order_manager (concrete OrderManager)
    risk_manager (concrete RiskManager)
    poly_client (concrete PolymarketClient)
    vpin_calculator (concrete VPINCalculator)
    alerter (concrete TelegramAlerter)
    cg_enhanced (concrete CoinGlassEnhancedFeed)
    cg_feeds (dict of concrete feeds)
    claude_evaluator (concrete ClaudeEvaluator)
    db_client (concrete DBClient)
    geoblock_check_fn (lambda reading Orchestrator private state)
    twap_tracker (concrete TWAPTracker)
    timesfm_client (concrete TimesFMClient)
```

---

## C. Layer Violations

### C.1 Inline HTTP Calls (should be behind an adapter port)

| Line(s) | Target | Purpose | Fix |
|---------|--------|---------|-----|
| 449-472 | `https://polymarket.com/api/geoblock` | G6 geoblock check on startup | Extract to `GeoblockPort` |
| 1337-1350 | `https://api.anthropic.com/v1/messages` | AI commentary for countdown snapshots | Extract to `AICommentaryPort` (or use existing `ClaudeEvaluator`) |
| 1377-1391 | `https://gamma-api.polymarket.com/events?slug=...` | Fresh Gamma prices for snapshot notifications | Extract to `GammaMarketPort` |
| 2204-2224 | `https://data-api.polymarket.com/activity?user=...` | Legacy reconciliation loop | Extract to `PolymarketActivityPort` (hardcoded wallet address!) |
| 3218-3237 | `https://gamma-api.polymarket.com/events?slug=...` | Shadow resolution oracle check | Extract to `GammaMarketPort` (same as above, duplicated) |
| 3414-3417 | `https://gamma-api.polymarket.com/events?slug=...` | Window prediction resolution | Extract to `GammaMarketPort` (THIRD copy) |

**Security note**: Line 2204-2206 hardcodes the funder wallet address `0x181d2ed714e0f7fe9c6e4f13711376edaab25e10` in a URL string. This is also available from `self._settings.poly_funder_address`.

### C.2 Direct SQL (should be behind a repository adapter)

| Line(s) | Table(s) | Operation | Fix |
|---------|----------|-----------|-----|
| 1914-1922 | `trade_bible` | SELECT win/loss counts | `TradeRepository.get_daily_record()` |
| 1927 | `self._order_manager._orders` | Direct dict access for open positions | `OrderRepository.get_open_position_value()` |
| 1967-2148 | `trades`, `window_snapshots`, `trade_bible` | Multiple SELECTs for sitrep: recent trades, recent skips, wins/losses, pending positions | `TradeRepository.get_sitrep_data()` |
| 2231-2240 | `trades` | SELECT recent trades for reconciliation | `TradeRepository.get_recent_live()` |
| 2305-2310 | `trades` | UPDATE entry_price | `TradeRepository.update_entry_price()` |
| 2988-3024 | `trades` | SELECT + UPDATE for position monitor resolution | `TradeRepository.link_resolution()` |
| 3046-3077 | `trades` | SELECT metadata for signal details | `TradeRepository.get_trade_metadata()` |
| 3093-3111 | `trades` | SELECT most recent resolved trade for display | `TradeRepository.get_latest_resolved()` |
| 3401-3452 | `window_predictions`, `window_snapshots` | SELECT unresolved + UPDATE oracle winner | `WindowRepository.resolve_predictions()` |

### C.3 Business Logic That Should Be in a Use Case

| Line(s) | Logic | Target Use Case |
|---------|-------|----------------|
| 1218-1237 | TWAP proxy price inference from token ratios | `EvaluateWindowUseCase` (window observation) |
| 1271-1352 | Full signal snapshot building (VPIN, regime, TimesFM, TWAP, CoinGlass, AI commentary) | `BuildWindowSnapshotUseCase` or inline in `EvaluateWindowUseCase` |
| 1354-1458 | Countdown stage dispatch and DB persistence | `WindowCountdownUseCase` |
| 1565-1685 | Order resolution handling: P&L direction logic, streak calculation, Telegram dispatch | `ResolveOrderUseCase` |
| 1787-1862 | Paper/live mode switching with component mutation | `SwitchModeUseCase` |
| 1879-2176 | Sitrep building: aggregate stats, SQL queries, formatting | `PublishHeartbeatUseCase` (from migration plan) |
| 2192-2344 | Legacy reconciliation: API fetch + DB comparison + updates | `ReconcilePositionsUseCase` (from migration plan) |
| 2575-2810 | Manual trade execution: token lookup, order submission, fallback logic | `ExecuteManualTradeUseCase` (from migration plan) |
| 2928-3155 | Position monitor: CLOB position poll + trade linking + alerting | `MonitorPositionsUseCase` |
| 3159-3466 | Shadow resolution: oracle check + P&L calc + AI analysis | `ResolveShadowTradesUseCase` |
| 3470-3579 | Staggered execution: window batching + scoring + dispatch | `StaggeredExecutionUseCase` |

### C.4 Env Var / .env File Parsing Outside Settings

The orchestrator has its own parallel config system that bypasses `engine/config/settings.py`:

| Line(s) | Variable | Issue |
|---------|----------|-------|
| 295-326 | `TIMESFM_ENABLED`, `TIMESFM_URL`, `TIMESFM_MIN_CONFIDENCE` | Reads `os.environ` then manually parses `.env` file (3 separate open/read/parse blocks!) |
| 351-352 | `V2_EARLY_ENTRY_ENABLED` | `os.environ.get()` |
| 354 | `TIMESFM_V2_URL` | `os.environ.get()` |
| 365-366 | `FIFTEEN_MIN_ENABLED`, `FIFTEEN_MIN_ASSETS` | `os.environ.get()` |
| 524-532 | `TIINGO_API_KEY` | `os.environ.get()` + `.env` file fallback |
| 792 | `RECONCILER_ENABLED` | `os.environ.get()` |
| 1700-1701 | `POLY_FILLS_SYNC_INTERVAL_S`, `POLY_FILLS_LOOKBACK_HOURS` | `os.environ.get()` |
| 2868 | `SOT_RECONCILER_INTERVAL` | `os.environ.get()` |

All of these should be fields on `Settings` (pydantic-settings handles env var + .env automatically).

---

## D. State Ownership

### D.1 All `self._` Fields

| Field | Line | Mutable? | Who Writes | Who Reads | Duplicated? |
|-------|------|----------|------------|-----------|-------------|
| `_settings` | 85 | YES (paper_mode mutated L1815) | `__init__`, `_heartbeat_loop` | Everywhere | Settings should be immutable |
| `_shutdown_event` | 86 | YES (set) | `_handle_os_signal` | All loops | No |
| `_tasks` | 87 | YES (append) | `start`, `_heartbeat_loop` | `stop` | No |
| `_execution_queue` | 90 | YES (put/get) | `_on_fifteen_min_window`, `_staggered_execution_loop` | `_staggered_execution_loop` | No |
| `_geoblock_active` | 91 | YES | `start` (geoblock check) | `FiveMinVPINStrategy` via lambda | No |
| `_manual_trade_notify_event` | 102 | YES (set/clear) | `_on_manual_trade_notify`, `_manual_trade_poller` | `_manual_trade_poller` | No |
| `_resolved_by_order_manager` | 107 | YES (add) | `_on_order_resolution` | `_position_monitor_loop` | YES: dedup state also in `CLOBReconciler._known_resolved` and `FiveMinVPINStrategy._traded_windows` |
| `_reconciler` | 110 | YES | `start` | `stop`, `_sot_reconciler_loop` | No |
| `_twap_tracker` | 113 | YES (add_tick, start_window) | `_on_binance_trade`, `_on_five_min_window`, `_on_fifteen_min_window` | `_on_five_min_window`, `_get_full_snapshot` | No |
| `_timesfm_client` | 116 | YES (set in ctor) | `__init__` | `_timesfm_forecast_recorder_loop`, injected into strategy | No |
| `_timesfm_strategy` | 117 | NO (Optional) | `__init__` | `start`, `stop`, `_evaluate_timesfm_window` | Unused since v5.8 |
| `_timesfm_multi` | 118 | NO (Optional) | `__init__` | `start`, `stop` | Unused since v5.8 |
| `_db` | 123 | YES (connect/close) | `__init__`, `start` | ~40 locations | No |
| `_tick_recorder` | 125 | YES | `start` | `_on_binance_trade`, `_on_five_min_window`, snapshot loops | Injected into strategy L498 |
| `_aggregator` | 128 | YES (state updates) | `__init__` | Callbacks, `_market_state_loop`, `_get_full_snapshot` | No |
| `_alerter` | 131 | YES (set_risk_manager, set_poly_client, set_location, set_db_client, _paper_mode mutation) | `__init__`, `start`, `_heartbeat_loop` | ~20 locations | No |
| `_vpin_calc` | 142 | YES (on_trade, warm_start) | `__init__`, `start` | `_on_binance_trade`, `_staggered_execution_loop` | No |
| `_cascade` | 147 | YES (update) | `__init__` | `_on_oi_update`, `_on_vpin_signal` | No |
| `_arb_scanner` | 152 | YES | `__init__` | `_on_polymarket_book` | No |
| `_regime` | 158 | YES (on_price) | `__init__` | `_on_binance_trade`, `_heartbeat_loop` | No |
| `_poly_client` | 161 | YES (connect, paper_mode mutated L1814) | `__init__`, `_heartbeat_loop` | `start`, manual trade, redeemer, reconciler | No |
| `_opinion_client` | 169 | YES (connect/disconnect) | `__init__` | `start`, `stop`, `_heartbeat_loop` | No |
| `_order_manager` | 176 | YES | `__init__` | Callbacks, `_heartbeat_loop`, `_resolution_loop` | No |
| `_risk_manager` | 187 | YES (_paper_mode mutated L1819) | `__init__`, `_heartbeat_loop` | `_on_order_resolution`, `_heartbeat_loop` | No |
| `_redeemer` | 200 | YES (_paper_mode mutated L1852) | `__init__` | `start`, `_redeemer_loop`, `_heartbeat_loop` | No |
| `_playwright` | 209 | YES (start/stop) | `__init__` | `start`, playwright loops | No |
| `_cg_enhanced` | 219 | NO (ref to BTC feed) | `__init__` | Strategy, heartbeat | Alias for `_cg_feeds["BTC"]` |
| `_cg_feeds` | 220 | YES (per-asset dict) | `__init__` | `start`, snapshot loop | No |
| `_claude_evaluator` | 233 | NO (Optional) | `__init__` | Strategy injection | No |
| `_post_resolution_evaluator` | 243 | NO (Optional) | `__init__` | `_shadow_resolution_loop` | No |
| `_arb_strategy` | 253 | NO | `__init__` | `start`, `stop`, `_market_state_loop` | No |
| `_cascade_strategy` | 258 | NO | `__init__` | `start`, `stop`, `_market_state_loop` | No |
| `_five_min_strategy` | 266 | YES (private attrs injected L347,355,498) | `__init__`, `start` | ~25 locations via private attrs | YES: internal state accessed by orchestrator |
| `_five_min_feed` | 268 | NO (Optional) | `__init__` | `start`, `stop` | No |
| `_fifteen_min_feed` | 364 | NO (Optional) | `__init__` | `start`, `stop` | No |
| `_binance_feed` | 378 | YES (start/stop) | `__init__` | `start`, `stop`, `_heartbeat_loop` | No |
| `_coinglass_feed` | 384 | NO (Optional) | `__init__` | `start`, `stop`, `_heartbeat_loop` | No |
| `_chainlink_feed` | 396 | NO (Optional) | `__init__` | `start`, `stop`, `_heartbeat_loop` | No |
| `_chainlink_multi_feed` | 408 | YES | `start` | `start`, `stop` | No |
| `_tiingo_feed` | 412 | YES | `start` | `start`, `stop` | No |
| `_clob_feed` | 414 | YES | `start` | `start`, `stop` | No |
| `_polymarket_feed` | 423 | NO | `__init__` | `start`, `stop`, `_heartbeat_loop` | No |
| `_countdown_sent` | 1253 | YES (dict of sets) | `_on_five_min_window` | `_on_five_min_window` | Not `__init__`-declared -- dynamically created via `hasattr` check |
| `_poly_fills_reconciler` | 709 | NO (Optional) | `start` | `_poly_fills_loop` | Not `__init__`-declared |

### D.2 Critical State Issues

1. **Triple dedup state**: `self._resolved_by_order_manager` (orchestrator), `CLOBReconciler._known_resolved` (reconciler), `FiveMinVPINStrategy._traded_windows` (strategy) -- all track "has this window been acted on" with no invariant keeping them consistent.

2. **Mutable settings**: `self._settings.paper_mode` is mutated at L1815 during mode switching. Pydantic `BaseSettings` objects should be treated as immutable. Mutating them means any other code reading `settings.paper_mode` sees a different value than what was loaded at startup.

3. **Cross-component state mutation**: The heartbeat loop mutates private attributes on 4 different components during mode switching (L1814-1822): `_poly_client.paper_mode`, `_settings.paper_mode`, `_risk_manager._paper_mode`, `_alerter._paper_mode`. This is a fragile pattern -- any new component that reads `paper_mode` from its own copy will be inconsistent until the next heartbeat.

4. **Dynamically-created attributes**: `_countdown_sent` (L1252-1253) is created via `hasattr` check, not in `__init__`. This means any code that accesses it before the first `_on_five_min_window` call will get an `AttributeError`.

---

## E. Coupling to five_min_vpin.py

The orchestrator reaches into `FiveMinVPINStrategy` internals in **25 places**:

### E.1 Private Attribute Injection (setter-less)

| Line | Access | Purpose |
|------|--------|---------|
| 347 | `self._five_min_strategy._timesfm = self._timesfm_client` | Inject TimesFM v1 client |
| 355 | `self._five_min_strategy._timesfm_v2 = TimesFMV2Client(...)` | Inject TimesFM v2.2 client |
| 498 | `self._five_min_strategy._tick_recorder = self._tick_recorder` | Inject tick recorder |

These bypass the strategy's constructor entirely. If `FiveMinVPINStrategy.__init__` is refactored to validate its dependencies, these injections would silently create attributes that don't participate in validation.

### E.2 Private Attribute Reads

| Line | Access | Purpose |
|------|--------|---------|
| 677 | `hasattr(self._five_min_strategy, '_timesfm_v2') and self._five_min_strategy._timesfm_v2` | Guard for ELM recorder |
| 680 | `self._five_min_strategy._timesfm_v2` | Pass v2 client to ELM recorder |
| 1272 | `self._five_min_strategy._vpin.current_vpin` | Read VPIN for snapshot |
| 1279 | `self._five_min_strategy._timesfm` | Guard for TimesFM forecast |
| 1282 | `self._five_min_strategy._timesfm.get_forecast(...)` | Call TimesFM via strategy |
| 1609 | `self._five_min_strategy._vpin.current_vpin` | Read VPIN for resolution alert |
| 2688 | `self._five_min_strategy._recent_windows` | Iterate for token ID lookup |
| 3351 | `self._five_min_strategy._window_eval_history.get(...)` | Read eval history for post-resolution AI |

### E.3 Private Attribute Mutation

| Line | Access | Purpose |
|------|--------|---------|
| 1211 | `self._five_min_strategy._pending_windows.append(window)` | Push window into strategy's queue |
| 1213-1214 | `self._five_min_strategy._recent_windows = []` / `.append(window)` | Initialize + push into recent windows |
| 1486-1487 | `self._five_min_strategy._recent_windows = ...[-20:]` | Trim recent windows |
| 1512-1515 | Same pattern for 15-min windows | Duplicate of above |
| 1542-1543 | Same trimming pattern for 15-min | Duplicate of above |

### E.4 Private Method Calls

| Line | Access | Purpose |
|------|--------|---------|
| 1481 | `self._five_min_strategy._evaluate_window(window, state)` | Direct evaluation dispatch |
| 3561 | `self._five_min_strategy._evaluate_window(w, state)` | Staggered execution dispatch |

### E.5 Summary of Coupling Issues

1. **No public API contract**: The orchestrator treats `FiveMinVPINStrategy` as a bag of mutable state, not as a component with a defined interface. The strategy has no formal `push_window()`, `get_vpin()`, or `get_eval_history()` public methods.

2. **Bidirectional knowledge**: The orchestrator knows the internal structure of the strategy (what fields it has, their types, their semantics), and the strategy knows it will be injected into by the orchestrator (via `Optional[...] = None` constructor params + post-init attribute setting).

3. **Duplication**: Window pushing logic (append to `_pending_windows` + `_recent_windows`, trim to 20) is duplicated between `_on_five_min_window` and `_on_fifteen_min_window`.

---

## F. Decomposition Plan

### F.1 What the Migration Plan Covers

The existing `docs/CLEAN_ARCHITECT_MIGRATION_PLAN.md` identifies four target use cases:
1. `EvaluateWindowUseCase` -- primarily from `five_min_vpin.py`
2. `ExecuteManualTradeUseCase` -- from orchestrator `_manual_trade_poller`
3. `ReconcilePositionsUseCase` -- from orchestrator reconciler loops
4. `PublishHeartbeatUseCase` -- from orchestrator `_heartbeat_loop`

### F.2 What the Migration Plan Misses

The following orchestrator responsibilities are NOT covered in the migration plan:

| Responsibility | Current Method(s) | Proposed Use Case / Adapter | Priority |
|---------------|-------------------|----------------------------|----------|
| **Shadow trade resolution** | `_shadow_resolution_loop` (307 LOC) | `ResolveShadowTradesUseCase` | HIGH -- contains inline HTTP (Gamma API x3), raw SQL, domain logic (P&L calc), and AI dispatch |
| **Position monitoring** | `_position_monitor_loop` (228 LOC) | `MonitorPositionsUseCase` or merge into `ReconcilePositionsUseCase` | HIGH -- contains raw SQL, inline HTTP, dedup state |
| **Order resolution callback** | `_on_order_resolution` (122 LOC) | `ResolveOrderUseCase` | MEDIUM -- contains domain logic (direction, P&L), Telegram dispatch, AI analysis |
| **Window countdown notifications** | `_on_five_min_window` (300 LOC) | `WindowCountdownNotifier` (adapter) or `BuildWindowSnapshotUseCase` | MEDIUM -- contains inline Anthropic API, Gamma API, DB writes |
| **Mode switching** | Lines 1787-1862 in `_heartbeat_loop` | `SwitchModeUseCase` | MEDIUM -- mutates 4+ components' private state |
| **Staggered execution** | `_staggered_execution_loop` (109 LOC) | `StaggeredExecutionUseCase` or collapse into composition root scheduling | LOW -- mostly infrastructure |
| **Geoblock check** | Lines 447-472 in `start` | `GeoblockPort` adapter | LOW -- one-shot at startup |
| **Legacy reconcile loop** | `_polymarket_reconcile_loop` (152 LOC) | DELETE (replaced by `CLOBReconciler`) | LOW -- legacy fallback |
| **Playwright loops** | 3 loops, ~85 LOC total | `PlaywrightAdapter` implementing a port | LOW -- ancillary feature |
| **Env var parsing** | 8 locations, ~80 LOC | Move to `Settings` fields | LOW -- config debt, tracked as CFG-01 |

### F.3 Detailed Method-to-Use-Case Mapping

#### Phase 3 additions (not in migration plan)

```
_shadow_resolution_loop  -->  ResolveShadowTradesUseCase
  Port: GammaMarketPort.get_oracle_outcome(asset, timeframe, window_ts) -> OracleOutcome | None
  Port: WindowRepository.get_unresolved_shadow_windows(minutes_back) -> list[ShadowWindow]
  Port: WindowRepository.update_shadow_resolution(window_ts, ...) -> None
  Port: AIAnalysisPort.analyse_window(window_ts, ...) -> None  (fire-and-forget)
  Domain: shadow P&L calculation (pure function, ~10 LOC)
```

```
_position_monitor_loop  -->  MonitorPositionsUseCase
  Port: PolymarketPositionPort.get_position_outcomes() -> dict[str, PositionOutcome]
  Port: TradeRepository.link_resolution(trade_id, outcome, pnl) -> None
  Port: AlerterPort.send_resolution_alert(...) -> None
  Domain: dedup tracking (set of resolved condition IDs)
```

```
_on_order_resolution + _record_and_alert  -->  ResolveOrderUseCase
  Port: RiskPort.record_outcome(pnl_usd) -> None
  Port: AlerterPort.send_window_resolution(...) -> None
  Port: AIAnalysisPort.send_outcome_analysis(...) -> None
  Domain: direction inference from outcome (pure function)
```

```
_on_five_min_window (countdown part)  -->  WindowCountdownUseCase
  Port: SignalSnapshotPort.get_snapshot(window) -> WindowSnapshot
  Port: AlerterPort.send_window_snapshot(...) -> None
  Port: WindowRepository.write_countdown_evaluation(...) -> None
  Domain: countdown stage classification (pure function)
```

#### Phase 6/7 additions (extend migration plan)

```
Mode switching (in _heartbeat_loop)  -->  SwitchModeUseCase
  Port: ModePort.get_target_mode() -> PaperOrLive
  Port: ExchangePort.set_paper_mode(bool) -> None
  Port: AlerterPort.send_mode_switch(...) -> None
```

### F.4 Orchestrator Residual (Post-Migration Target)

After extraction, `orchestrator.py` should shrink to ~500 LOC containing ONLY:
- `__init__`: Wire ports and use cases (move to `infrastructure/main.py`)
- `start`: Start feeds and background loops
- `run`: Start + wait + stop
- `stop`: Graceful shutdown
- `_handle_os_signal`: Set shutdown event
- Feed callback routers (`_on_binance_trade`, `_on_oi_update`, etc.): 1-3 lines each, delegating to use cases
- Background loop shells: Each loop becomes ~15 LOC calling a use case method inside a `while not shutdown` wrapper

---

## G. Risk Matrix

Each method rated for migration risk based on: (a) is it on the live trading hot path? (b) does it have test coverage? (c) how many external systems does it touch? (d) is there a rollback path?

### G.1 DANGEROUS -- Live Trading Hot Path, Must Not Regress

| Method | Risk | Reason | Mitigation |
|--------|------|--------|------------|
| `_on_order_resolution` | **CRITICAL** | Directly affects P&L recording and bankroll tracking. Bug = wrong bankroll = wrong position sizing = compounding losses. | Shadow-deploy new use case alongside old callback; compare outputs for 48h before cutover. |
| `_manual_trade_poller` | **CRITICAL** | Executes real CLOB orders with real money. Token ID lookup failure = failed trades or wrong-side trades. | Feature-flag new use case; keep old poller as fallback for 1 week. |
| `_heartbeat_loop` (mode switching) | **HIGH** | Flips paper/live mode. Bug = accidentally trading real money. | Extract mode switching into separate method/use case FIRST; add integration test that verifies paper_mode consistency across all components after a switch. |
| `_heartbeat_loop` (wallet sync) | **HIGH** | Syncs bankroll from real Polymarket wallet. Wrong sync = wrong risk limits. | Extract wallet sync into `SyncBankrollUseCase`; unit test the sync logic. |
| `_staggered_execution_loop` | **HIGH** | Dispatches window evaluations that lead to CLOB orders. | Move last; ensure `_evaluate_window` call site doesn't change semantics. |
| `__init__` (FiveMinVPINStrategy wiring) | **HIGH** | Strategy constructor + post-init injections. Break this = strategy doesn't start = no trades. | Refactor strategy to accept all deps in constructor; add smoke test that verifies strategy starts. |

### G.2 MODERATE -- Affects Observability/Reconciliation, Errors Detectable

| Method | Risk | Reason | Mitigation |
|--------|------|--------|------------|
| `_position_monitor_loop` | **MODERATE** | Detects resolutions and links to DB trades. Bug = missed resolution alerts (not missed money -- reconciler catches that). | Shadow-deploy; compare alert output. |
| `_shadow_resolution_loop` | **MODERATE** | Shadow P&L tracking only -- no real money at risk. But AI analysis costs real API credits. | Extract to use case; disable AI dispatch during migration. |
| `_heartbeat_loop` (sitrep) | **MODERATE** | Telegram sitrep formatting. Bug = garbled messages (annoying, not dangerous). | Extract formatting to `SitrepBuilder` helper; unit test. |
| `_sot_reconciler_loop` | **LOW-MODERATE** | Delegates to CLOBReconciler. Orchestrator code is thin. | Low-touch extraction. |
| `_poly_fills_loop` | **LOW** | Delegates to PolyFillsReconciler. Orchestrator code is thin. | Low-touch extraction. |

### G.3 SAFE -- No Live Trading Impact

| Method | Risk | Reason | Mitigation |
|--------|------|--------|------------|
| `start` (schema migrations) | **LOW** | `ensure_*` calls are idempotent DDL. | No change needed during migration. |
| `stop` | **LOW** | Shutdown sequence. Bug = unclean shutdown (restart fixes it). | Leave as-is until final cleanup. |
| `_on_five_min_window` (countdown notifications) | **LOW** | Telegram notifications only. No trading impact. | Extract freely. |
| `_on_fifteen_min_window` | **LOW** | Same as above + queues to staggered execution. | Extract with `_on_five_min_window`. |
| `_on_binance_trade` | **LOW** | Pure routing. | Leave as thin router. |
| `_on_vpin_signal` / `_on_cascade_signal` / `_on_arb_opportunities` | **LOW** | Pure routing + DB writes. | Leave as thin routers. |
| `_coinglass_snapshot_recorder_loop` / `_timesfm_forecast_recorder_loop` | **LOW** | Telemetry only. | Leave as-is or extract to adapter. |
| `_playwright_*_loop` (3 methods) | **LOW** | Browser automation, ancillary feature. | Extract last. |
| `_polymarket_reconcile_loop` | **LOW** | Legacy, already superseded by CLOBReconciler. | Delete. |
| `_redeemer_loop` / `_playwright_redeem_loop` | **LOW** | Redemption is post-settlement. | Extract freely. |
| `_market_state_loop` | **LOW** | Clean fan-out, no business logic. | Leave as-is. |
| `_evaluate_timesfm_window` | **LOW** | Unused since v5.8 (TimesFM standalone disabled). | Delete or leave dormant. |
| Env var parsing (8 locations) | **LOW** | Config debt. | Move to `Settings` in a separate PR. |

### G.4 Recommended Migration Order

Based on the risk matrix, the safest order to extract methods from orchestrator.py:

1. **Config cleanup** (LOW risk): Move all env var parsing to `Settings`. Zero behavior change.
2. **Delete dead code** (LOW risk): `_evaluate_timesfm_window`, `_polymarket_reconcile_loop` (if CLOBReconciler is always-on), unused `_timesfm_strategy`/`_timesfm_multi` fields.
3. **Extract countdown notifications** (LOW risk): `_on_five_min_window` countdown stage logic to `WindowCountdownNotifier` adapter.
4. **Extract shadow resolution** (MODERATE risk): `_shadow_resolution_loop` to `ResolveShadowTradesUseCase` + `GammaMarketPort`.
5. **Extract heartbeat sitrep** (MODERATE risk): Sitrep building from `_heartbeat_loop` to `PublishHeartbeatUseCase`.
6. **Extract position monitor** (MODERATE risk): `_position_monitor_loop` to `MonitorPositionsUseCase`.
7. **Extract mode switching** (HIGH risk): Mode-switch logic from `_heartbeat_loop` to `SwitchModeUseCase`. Requires integration test.
8. **Refactor FiveMinVPINStrategy coupling** (HIGH risk): Replace all 25 private-attribute accesses with public methods on the strategy.
9. **Extract manual trade poller** (CRITICAL risk): `_manual_trade_poller` to `ExecuteManualTradeUseCase`. Feature-flag.
10. **Extract order resolution** (CRITICAL risk): `_on_order_resolution` to `ResolveOrderUseCase`. Shadow-deploy.

---

## Appendix: Comparison with margin_engine Reference

| Aspect | margin_engine (reference) | engine/orchestrator.py (current) |
|--------|--------------------------|----------------------------------|
| Composition root | `main.py`, 483 LOC, procedural async function | `Orchestrator.__init__`, 343 LOC class method |
| Port interfaces | 7 abstract ports in `domain/ports.py` | 0 ports, 35+ concrete deps |
| Use cases | 2 (`OpenPositionUseCase`, `ManagePositionsUseCase`) | 0 (all logic inline in orchestrator) |
| Main loop | 12 LOC: `open_uc.execute()` + `manage_uc.tick()` | ~3,500 LOC across 37 methods |
| Inline HTTP | 0 | 6 unique HTTP endpoints called inline |
| Raw SQL | 0 (all via repository adapters) | ~15 raw SQL blocks |
| Private attr access on deps | 0 | 25+ accesses on `_five_min_strategy` alone |
| Env var parsing outside Settings | 0 | 8 locations |
| Testability | Use cases testable with mock ports | Untestable without running the full system |
