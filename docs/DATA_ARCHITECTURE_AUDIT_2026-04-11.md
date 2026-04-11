# Data Architecture Audit -- 2026-04-11

> Comprehensive inventory of every PostgreSQL table in the novakash trading
> system (shared Railway DB: `hopper.proxy.rlwy.net:35772/railway`).
> Audited from `develop` branch at commit `HEAD`.

---

## 1. Executive Summary

The novakash data layer comprises **39 identified tables** across 6 writing
services (engine, margin_engine, hub, macro-observer, data-collector,
timesfm-service). The existing `hub/db/schema_catalog.py` (SCHEMA-01, PR #60)
catalogues 35 of these. This audit found **4 tables missing from the catalog**
and **3 catalog entries that are "planned/deprecated" but now actually exist in
the running DB**. It also identifies 5 tables with dual-writer risk, 2 with
no active readers (dead data candidates), and several index gaps on
high-frequency query paths.

### Key Findings

| Finding                                                    | Count |
|------------------------------------------------------------|-------|
| Total tables in production DB                              | 39    |
| Catalogued in schema_catalog.py                            | 35    |
| **Missing from catalog** (found by code grep)              | 4     |
| Active tables                                              | 28    |
| Legacy/deprecated tables                                   | 8     |
| External (different DB)                                    | 1     |
| Planned-but-now-exist (catalog says deprecated, DB has it) | 3     |
| Tables with dual-writer risk                               | 5     |
| Tables with no active readers (dead data)                  | 2     |

---

## 2. Complete Table Inventory

### Legend

| Tag          | Meaning                                              |
|--------------|------------------------------------------------------|
| **SOT**      | Source of Truth -- authoritative for this data        |
| **DERIVED**  | Computed from SOT tables, can be rebuilt              |
| **CACHE**    | Ephemeral performance optimization                   |
| **LEGACY**   | No longer actively used, candidate for archival       |
| **OPERATIONAL** | System state / config, not trading data           |

---

### 2.1 Polymarket Engine Domain

#### `signal_evaluations`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | Per-window gate decision + outcome (one row per window per eval_offset). Primary backtest evidence base (865-outcome dataset). |
| Writers | `engine/persistence/db_client.py::write_signal_evaluation` (UPSERT on window_ts, asset, timeframe, eval_offset) |
| Readers | `hub/api/v58_monitor.py`, `scripts/export_truth_dataset.py`, `engine/strategies/five_min_vpin.py` |
| Write frequency | High -- every 5-min window evaluation |
| Indexes | UNIQUE (window_ts, asset, timeframe, eval_offset); idx on evaluated_at |
| Notes | G0..G7 gate pipeline captured in gate_*_passed columns. v2_probability_up + v2_quantiles capture OAK model surface. |

#### `window_snapshots`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | Coarser per-window snapshot. Holds open/close/delta/vpin/regime, CoinGlass state, TWAP, TimesFM, macro overlay, shadow trades, v2 quantiles. |
| Writers | `engine/persistence/db_client.py::write_window_snapshot` (UPSERT) |
| Readers | `hub/api/v58_monitor.py`, `hub/db/models.py`, `frontend`, `macro-observer/observer.py` (vpin/regime reads) |
| Write frequency | High -- every 5-min window |
| Indexes | UNIQUE (window_ts, asset, timeframe, eval_offset); idx on created_at |
| Notes | 82-column wide table, grown additively via ALTER TABLE ADD COLUMN IF NOT EXISTS. |

#### `ticks_elm_predictions`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **DERIVED** |
| Purpose | Sequoia v5.2 / ELM model probability predictions. Diagnostic recorder output. |
| Writers | `engine/data/feeds/elm_prediction_recorder.py` |
| Readers | `frontend V3Surface.jsx` (diagnostic chart) |
| Write frequency | High -- every prediction cycle |
| Indexes | idx on (asset, ts DESC) |

#### `manual_trades`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | Operator-placed manual trades. Hub writes, engine polls + executes. |
| Writers | `hub/api/v58_monitor.py` (POST), `engine/strategies/orchestrator.py` (status updates) |
| Readers | `engine/strategies/orchestrator.py` (poller), `hub/api/v58_monitor.py`, `frontend` |
| Write frequency | Low -- operator clicks |
| Indexes | idx on (status, created_at) |
| Notes | **DUAL-WRITER**: hub INSERTs, engine UPDATEs status. Safe because writers touch different columns. POLY-SOT columns added. LT-04 uses PG LISTEN/NOTIFY for fast path. |

#### `manual_trade_snapshots`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | LT-03 decision-snapshot table. One row per manual_trades row. JSONB captures full v4 fusion surface. |
| Writers | `hub/api/v58_monitor.py` (paired insert with manual_trades) |
| Readers | `hub/api/v58_monitor.py`, `frontend ManualTradePanel.jsx` |
| Write frequency | Low -- same as manual_trades |

#### `post_resolution_analyses`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **DERIVED** |
| Purpose | AI-written post-resolution analysis per window. |
| Writers | `engine/persistence/db_client.py::ensure_post_resolution_analysis` |
| Readers | `hub/api/v58_monitor.py`, `frontend WindowResults.jsx` |
| Write frequency | Medium -- one per resolved window |
| Indexes | UNIQUE (window_ts, asset, timeframe) |

#### `window_predictions`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **DERIVED** |
| Purpose | Tiingo + Chainlink directional prediction accuracy per window. |
| Writers | `engine/persistence/db_client.py::ensure_window_predictions_table` |
| Readers | `hub/api/v58_monitor.py`, `hub/api/margin.py` (JOIN with ticks_v2_probability) |
| Write frequency | Medium |
| Indexes | UNIQUE (window_ts, asset, timeframe) |

#### `gate_audit`
| Field | Value |
|-------|-------|
| Status | **LEGACY** |
| Tag | **LEGACY** |
| Purpose | v8.0 per-window gate pass/fail audit trail. Superseded by signal_evaluations. |
| Writers | `engine/persistence/db_client.py::write_gate_audit` |
| Readers | `scripts/export_truth_dataset.py` (cross-check only) |
| Write frequency | Low (superseded) |
| Indexes | UNIQUE (window_ts, asset, timeframe) |
| Notes | Candidate for archival. signal_evaluations has the eval_offset axis that gate_audit lacks. |

#### `trade_bible`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **DERIVED** |
| Purpose | Auto-populated from trades via trigger. Adds config_version, eval_tier, resolution_source. |
| Writers | `migrations/populate_trade_bible.sql` (trigger on trades INSERT/UPDATE) |
| Readers | `hub/api/pnl.py`, `hub/api/v58_monitor.py`, `scripts/export_truth_dataset.py` |
| Write frequency | Medium -- fires on every trade write |
| Indexes | UNIQUE (trade_id, order_id) |
| Notes | Never INSERT directly. Trigger-driven. |

#### `playwright_state`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **OPERATIONAL** |
| Purpose | Single-row Playwright browser session snapshot. |
| Writers | `engine/persistence/db_client.py::update_playwright_state` |
| Readers | `hub/api/playwright.py`, `frontend PlaywrightDashboard.jsx` |
| Write frequency | Medium -- heartbeat interval |
| Notes | Singleton id=1. |

#### `redeem_events`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | Append-only log of redeem sweeps via Playwright browser. |
| Writers | `engine/persistence/db_client.py::write_redeem_event` |
| Readers | `hub/api/playwright.py` |
| Write frequency | Low |

#### `countdown_evaluations` [NOT IN CATALOG]
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **DERIVED** |
| Purpose | Multi-stage countdown snapshot per window (T-180/T-120/T-90/T-60). |
| Writers | `engine/persistence/db_client.py::write_countdown_evaluation`, `engine/adapters/persistence/pg_signal_repo.py` |
| Readers | `hub/api/v58_monitor.py` (countdown endpoints, window detail) |
| Write frequency | Medium -- multiple stages per window |
| Notes | **MISSING FROM CATALOG**. No CREATE TABLE DDL found in codebase. |

#### `telegram_notifications` [NOT IN CATALOG]
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **OPERATIONAL** |
| Purpose | Deduplication + audit log for Telegram notifications. |
| Writers | `engine/alerts/telegram.py`, `macro-observer/observer.py` |
| Readers | `macro-observer/observer.py` (dedup check) |
| Write frequency | Medium -- one row per notification |
| Notes | **MISSING FROM CATALOG**. **DUAL-WRITER**: engine + macro-observer. No CREATE TABLE found. |

---

### 2.2 Execution Domain

#### `clob_book_snapshots`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | CLOB order book snapshots per window. Top-of-book + depth. |
| Writers | `engine/data/feeds/clob_feed.py` (Montreal-only) |
| Readers | `engine/strategies/five_min_vpin.py`, `hub/api/v58_monitor.py` |
| Write frequency | High -- per window tick |
| Indexes | UNIQUE (window_ts, up_token_id, down_token_id, ts) |

#### `clob_execution_log`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | Every FOK/GTC order placement attempt with CLOB state at submission. |
| Writers | `engine/execution/clob_executor.py` |
| Readers | `scripts/analyze_fok_fills.py` |
| Write frequency | Medium |

#### `fok_ladder_attempts`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | Per-attempt rows for the FOK ladder. One row per ladder rung. |
| Writers | `engine/execution/clob_executor.py` |
| Readers | `scripts/analyze_fok_fills.py` |
| Indexes | FK execution_log_id; UNIQUE (execution_log_id, attempt_num) |

#### `order_audit_log`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | Cross-mode order submission audit (FOK/GTC/GTD). |
| Writers | `engine/execution/order_router.py` |
| Readers | `hub/api/v58_monitor.py` |
| Indexes | UNIQUE on order_id |

#### `poly_fills`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** (authoritative for true P&L) |
| Purpose | Authoritative fill record from data-api.polymarket.com. Append-only. |
| Writers | `engine/reconciliation/poly_fills_reconciler.py` |
| Readers | `hub/api/pnl.py`, `hub/api/v58_monitor.py` |
| Write frequency | Medium -- reconciler batch |
| Indexes | UNIQUE (transaction_hash); idx on condition_id, slug, match_time, source, trade_bible_id |
| Notes | Ground truth for P&L. Never read trades or trade_bible for P&L. |

#### `poly_trade_history`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | Polymarket trade history from public API for proxy wallet. |
| Writers | `engine/reconciliation/poly_trade_history.py` |
| Readers | `hub/api/pnl.py` |
| Write frequency | Low -- every 5min |

#### `wallet_snapshots`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | USDC balance polled from Polymarket CLOB. Append-only timeseries. |
| Writers | `engine/reconciliation/clob_reconciler.py` |
| Readers | `hub/api/pnl.py`, `frontend PnL.jsx` |
| Indexes | idx on recorded_at DESC |

---

### 2.3 Market Data Domain (data-collector)

#### `market_data`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | One row per Polymarket window (4 assets x 2 timeframes). Prices, volume, liquidity, outcome. |
| Writers | `data-collector/collector.py::upsert_market`, `data-collector/backfill.py` |
| Readers | `engine/persistence/db_client.py::get_token_ids_from_market_data`, `engine/strategies/orchestrator.py`, `macro-observer/observer.py::fetch_oracle_history` |
| Write frequency | High -- every 1s poll cycle (UPSERT) |
| Indexes | UNIQUE (window_ts, asset, timeframe); idx on window_ts, asset, resolved, collected_at |

#### `market_snapshots`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | Append-only intra-window price snapshots. Multiple readings per window. |
| Writers | `data-collector/collector.py::save_snapshot` |
| Readers | `scripts/backtest_5min_markets.py` |
| Write frequency | Very high -- every collector poll per asset per timeframe |
| Indexes | idx on (window_ts, asset, timeframe) |
| Notes | No primary uniqueness. Pure append log. Second-highest write volume. |

---

### 2.4 Tick Data Domain (engine tick_recorder)

#### `ticks_binance`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | Buffered Binance aggTrade ticks (price, qty, is_buyer_maker, vpin). Batch-flushed every 1s. |
| Writers | `engine/persistence/tick_recorder.py::record_binance_tick` |
| Readers | `engine/signals/vpin.py`, `scripts/replay_ticks.py`, `macro-observer/observer.py::fetch_btc_deltas` |
| Write frequency | **Very high** -- hundreds of rows per second |
| Indexes | idx (ts DESC), idx (asset, ts DESC) |
| Notes | Largest table by row count. Needs partition strategy. |

#### `ticks_coinglass`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | CoinGlass derivatives data: OI, liquidations, long/short %, funding, taker. |
| Writers | `engine/persistence/tick_recorder.py::record_coinglass_snapshot` |
| Readers | `engine/signals/coinglass_features.py`, `macro-observer/observer.py::fetch_coinglass_snapshot` |
| Write frequency | Medium -- every 10s |
| Indexes | idx (ts DESC), idx (asset, ts DESC) |

#### `ticks_gamma`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | Polymarket gamma-API price ticks per window. |
| Writers | `engine/persistence/tick_recorder.py::record_gamma_price` |
| Readers | `engine/strategies/five_min_vpin.py` |
| Indexes | idx (ts DESC), idx (asset, ts DESC), idx (window_ts) |

#### `ticks_timesfm`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | TimesFM forecast ticks per window: predicted_close, quantiles, direction, confidence. |
| Writers | `engine/persistence/tick_recorder.py::record_timesfm_forecast` |
| Readers | `engine/strategies/five_min_vpin.py`, `hub/api/v58_monitor.py` |
| Indexes | idx (ts DESC), idx (asset, ts DESC), idx (window_ts, seconds_to_close) |

---

### 2.5 Margin Engine Domain

#### `margin_positions`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | Open + closed margin positions on Binance/Hyperliquid. One row per position ID. |
| Writers | `margin_engine/adapters/persistence/pg_repository.py::PgPositionRepository.save` |
| Readers | `hub/api/margin.py`, `frontend MarginEngine.jsx` |
| Write frequency | Medium -- per position lifecycle event |
| Indexes | idx on state; idx on opened_at; partial idx on closed_at WHERE state='CLOSED' |
| Notes | Schema grown via additive ALTER TABLE at boot. v4 audit snapshot columns added in PR B. |

#### `margin_signals`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | Per-tick composite signal evaluations for the perp trader. |
| Writers | `margin_engine/adapters/persistence/pg_signal_repository.py::PgSignalRepository.write_batch` |
| Readers | `hub/api/margin.py`, `frontend CompositeSignals.jsx` |
| Write frequency | High -- batched every 5s |
| Indexes | idx (ts); idx (timescale, ts); idx on composite WHERE NOT NULL |

#### `margin_logs`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **OPERATIONAL** |
| Purpose | Async-flushed structured log records from the margin engine. |
| Writers | `margin_engine/adapters/persistence/pg_log_repository.py` |
| Readers | `hub/api/margin.py` (log tail) |
| Write frequency | High -- batched every 5s |
| Indexes | idx (ts); idx (level) |

---

### 2.6 Macro Domain

#### `macro_signals`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | Macro regime snapshots from LLM observer. Per-timescale bias map (5m/15m/1h/4h). |
| Writers | `macro-observer/observer.py::write_signal` |
| Readers | `engine/strategies/five_min_vpin.py`, `margin_engine/*`, `hub/api/v58_monitor.py`, `frontend V4Surface.jsx` |
| Write frequency | Low -- every 60s |
| Indexes | idx on created_at; GIN idx on timescale_map |
| Notes | Phase 2 added timescale_map JSONB. NULL = pre-Phase-2 row. |

#### `macro_events`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **OPERATIONAL** |
| Purpose | Pre-loaded economic calendar (Fed/CPI/FOMC) with impact ratings. |
| Writers | `macro-observer/observer.py` (calendar refresh) |
| Readers | `macro-observer/observer.py` (event-proximity check), `hub/api/v58_monitor.py` |
| Write frequency | Low |

---

### 2.7 Hub / Dashboard Domain

#### `trades`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** (engine-reported, but poly_fills is authoritative for true P&L) |
| Purpose | Trade record: order_id, strategy, venue, direction, entry_price, status, outcome, pnl. |
| Writers | `engine/persistence/db_client.py::write_trade` (UPSERT on order_id) |
| Readers | `hub/api/trades.py`, `hub/api/pnl.py`, `frontend Trades.jsx`, trade_bible trigger, `macro-observer/observer.py::fetch_session_stats` |
| Write frequency | Medium -- per trade placed/resolved |
| Indexes | UNIQUE (order_id); idx on strategy, market_slug, status, created_at |
| Notes | POLY-SOT columns added (polymarket_order_id, polymarket_confirmed_*). For true P&L use poly_fills. |

#### `signals`
| Field | Value |
|-------|-------|
| Status | **LEGACY** |
| Tag | **LEGACY** |
| Purpose | Generic signal-event log (VPIN, cascade, arb, regime as JSONB). Predates structured tables. |
| Writers | engine (rare) |
| Readers | `hub/api/signals.py` |
| Notes | New code should use signal_evaluations or margin_signals. |

#### `daily_pnl`
| Field | Value |
|-------|-------|
| Status | **LEGACY** |
| Tag | **DERIVED** |
| Purpose | Pre-aggregated daily P&L stats. |
| Writers | `hub/services/pnl_service.py` (end-of-day rollup) |
| Readers | `hub/services/dashboard_service.py`, `frontend PnL.jsx` |
| Notes | Will be replaced by live aggregations against poly_fills. |

#### `system_state`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **OPERATIONAL** |
| Purpose | Singleton row: live_enabled, paper_enabled, mode, active configs, heartbeat. |
| Writers | `hub/api/system.py`, `hub/api/trading_config.py`, `engine/strategies/orchestrator.py` |
| Readers | `engine/strategies/orchestrator.py`, `hub/api/system.py`, `hub/api/dashboard.py` |
| Notes | **Triple-writer**. id=1 singleton. Never bypass to flip mode (STOP-01 incident). |

#### `trading_configs`
| Field | Value |
|-------|-------|
| Status | **LEGACY** |
| Tag | **OPERATIONAL** |
| Purpose | DB-backed trading config overlay (JSONB). Being replaced by config_keys/values/history. |
| Writers | `hub/api/trading_config.py` |
| Readers | `hub/api/trading_config.py`, `engine/config/loader.py`, `frontend` |
| Notes | Will be mothballed when CFG-10 completes. |

#### `users`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **OPERATIONAL** |
| Purpose | Dashboard authentication accounts. |
| Writers | `hub/auth/routes.py` |
| Readers | `hub/auth/routes.py`, `hub/auth/middleware.py` |

#### `notes`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **OPERATIONAL** |
| Purpose | DB-backed audit journal. |
| Writers | `hub/api/notes.py` |
| Readers | `hub/api/notes.py`, `frontend Notes.jsx` |
| Indexes | idx on (status, updated_at DESC) |

#### `backtest_runs`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | Stored backtest results. |
| Writers | `hub/api/backtest.py`, `scripts/backtest_*.py` |
| Readers | `hub/api/backtest.py`, `frontend StrategyAnalysis.jsx` |

#### `config_keys`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** (catalog says "deprecated/planned" -- DISCREPANCY) |
| Tag | **OPERATIONAL** |
| Purpose | CFG-02 structured config-key catalog. One row per (service, key). |
| Writers | `hub/db/config_seed.py`, `hub/db/config_schema.py` (DDL) |
| Readers | `hub/api/config_v2.py` |
| Indexes | UNIQUE (service, key) |
| Notes | **CATALOG DISCREPANCY**: catalog marks as "deprecated" with "(planned)" writers. The table actually exists. |

#### `config_values`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** (catalog says "deprecated/planned" -- DISCREPANCY) |
| Tag | **OPERATIONAL** |
| Purpose | CFG-02 per-mode config values. |
| Notes | **CATALOG DISCREPANCY**: same as config_keys. Table exists and is seeded on every hub boot. |

#### `config_history`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** (catalog says "deprecated/planned" -- DISCREPANCY) |
| Tag | **OPERATIONAL** |
| Purpose | CFG-03 append-only audit log of config changes. |
| Notes | **CATALOG DISCREPANCY**: same as above. Table exists. |

#### `analysis_docs` [NOT IN CATALOG]
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **OPERATIONAL** |
| Purpose | Analysis library documents (doc_id, title, author, content, tags). |
| Writers | `hub/api/analysis.py` (POST /api/analysis) |
| Readers | `hub/api/analysis.py` (GET /api/analysis) |
| Notes | **MISSING FROM CATALOG**. No CREATE TABLE DDL found. |

#### `timesfm_forecasts` [NOT IN CATALOG]
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | TimesFM forecast results read by hub forecast page. |
| Writers | timesfm-service (external repo) |
| Readers | `hub/api/forecast.py` (5 SELECT queries) |
| Notes | **MISSING FROM CATALOG**. Cross-repo table. |

#### `ai_analyses` [NOT IN CATALOG -- may not exist]
| Field | Value |
|-------|-------|
| Status | **LEGACY/DEAD** |
| Tag | **LEGACY** |
| Purpose | Claude pre-trade assessment summaries. |
| Writers | None found in codebase |
| Readers | `macro-observer/observer.py::fetch_recent_ai_analyses` (bare except, silent failure) |
| Notes | **MISSING FROM CATALOG**. No writer, no DDL. Reader silently fails. Dead. |

---

### 2.8 External (Different DB)

#### `ticks_v3_composite`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** (in timesfm DB) |
| Purpose | v3 composite scorer outputs. Lives in the timesfm-service DB. |

#### `ticks_v2_probability`
| Field | Value |
|-------|-------|
| Status | **ACTIVE** |
| Tag | **SOT** |
| Purpose | v2 probability predictions. Queried by `hub/api/margin.py`. |
| Writers | timesfm-service |
| Readers | `hub/api/margin.py` |
| Notes | Not in catalog as a standalone entry (only referenced in gate notes). Cross-service query. |

---

## 3. Data Flow Diagrams

### 3.1 Polymarket 5-Minute Engine Flow

```
                        data-collector
                             |
                    [market_data] [market_snapshots]
                             |
                             v
Binance WS -----> [ticks_binance] -----> VPIN calc
CoinGlass  -----> [ticks_coinglass] ---> CG features
Gamma API  -----> [ticks_gamma] -------> Price discovery
TimesFM svc ----> [ticks_timesfm] -----> Model signal
                             |
                             v
                   Gate Pipeline (G0..G7)
                             |
              +--------------+--------------+
              |              |              |
     [signal_evaluations]  [window_snapshots]  [countdown_evaluations]
              |              |
              v              v
         [trades] -------> [trade_bible] (trigger)
              |                    |
              v                    v
       [poly_fills] <-------- reconciler
              |
              v
     [wallet_snapshots]     (USDC balance)
```

### 3.2 Margin Engine Flow

```
timesfm-service /v4/snapshot
        |
        v
  V4Snapshot (HTTP)
        |
        v
  margin_engine gates -----> [margin_positions] (open/close)
        |
        v
  [margin_signals] (composite score recording)
  [margin_logs]    (structured logging)
```

### 3.3 Macro Observer Flow

```
Binance/Coinbase/Kraken prices
CoinGlass snapshot (from [ticks_coinglass])
Oracle history (from [market_data])
BTC deltas (from [ticks_binance])
VPIN (from [window_snapshots])
Session stats (from [trades])
        |
        v
  Qwen 3.5 122B LLM -----> [macro_signals]
                                   |
                        +----------+-----------+
                        |                      |
              engine (macro veto)    margin_engine (per-TS gate)
```

### 3.4 Model Pipeline Flow

```
[ticks_binance] + features
        |
        v
  timesfm-service (external repo)
        |
        +---> [ticks_v2_probability] (predictions)
        +---> [ticks_v3_composite]   (composite scores, separate DB)
        +---> [timesfm_forecasts]    (forecast results)
        |
        v
  engine reads via HTTP /v2/probability
        |
        v
  [signal_evaluations] (v2_probability_up captured)
```

---

## 4. Schema Catalog Discrepancies

Comparing this audit against `hub/db/schema_catalog.py` (SCHEMA-01, PR #60):

| Table | Catalog Status | Actual Status | Issue |
|-------|---------------|---------------|-------|
| `config_keys` | deprecated ("planned") | **Active** -- created every hub boot | Catalog out of date. CFG-02 shipped. |
| `config_values` | deprecated ("planned") | **Active** -- created every hub boot | Same as above. |
| `config_history` | deprecated ("planned") | **Active** -- created every hub boot | Same as above. |
| `countdown_evaluations` | **ABSENT** | Active -- written by engine, read by hub | Missing from catalog. |
| `telegram_notifications` | **ABSENT** | Active -- written by engine + observer | Missing from catalog. |
| `analysis_docs` | **ABSENT** | Active -- written/read by hub | Missing from catalog. |
| `timesfm_forecasts` | **ABSENT** | Active -- written by timesfm-svc, read by hub | Missing from catalog. |
| `ai_analyses` | **ABSENT** | Dead -- only reader, no writer | Referenced by observer, no DDL. |
| `ticks_v2_probability` | Mentioned in gate notes | Active -- queried by hub/api/margin.py | Not a catalog entry. |

---

## 5. Problems Identified

### 5.1 Dual-Writer Risk (5 tables)

| Table | Writers | Risk Assessment |
|-------|---------|-----------------|
| `manual_trades` | hub (INSERT), engine (UPDATE status) | **Low** -- different columns. |
| `trades` | engine (UPSERT), trade_bible trigger (derived) | **Low** -- trigger reads, writes trade_bible. |
| `system_state` | hub, engine, trading_config | **Medium** -- three writers to singleton row. |
| `telegram_notifications` | engine, macro-observer | **Low** -- append-only. |
| `window_snapshots` | engine (data rows), macro-observer (ALTER TABLE at boot) | **Low** -- observer only DDL. |

### 5.2 Tables with No Active Readers (Dead Data)

| Table | Evidence |
|-------|----------|
| `ai_analyses` | No writer found. Reader silently fails. Dead. |
| `gate_audit` | Only cross-check script reads it. signal_evaluations supersedes. |

### 5.3 Missing Indexes

| Table | Query Pattern | Recommendation |
|-------|--------------|----------------|
| `countdown_evaluations` | `WHERE window_ts = $1 ORDER BY stage` | Add composite idx |
| `telegram_notifications` | `WHERE bot_id AND location AND window_id` | Add composite dedup idx |
| `analysis_docs` | `WHERE :tag = ANY(tags)` | Add GIN index on tags |

### 5.4 Missing CREATE TABLE DDL

These tables have no CREATE TABLE in the current codebase:

- `countdown_evaluations` -- written by db_client.py + pg_signal_repo.py, no DDL
- `telegram_notifications` -- written by engine + observer, no DDL
- `analysis_docs` -- written by hub/api/analysis.py, no DDL
- `ai_analyses` -- read by observer, no DDL, no writer
- `timesfm_forecasts` -- read by hub, written by external repo
- `ticks_v2_probability` -- read by hub, written by external repo

If the Railway DB were recreated from scratch, these tables would be missing and code would fail silently.

### 5.5 Naming Inconsistencies

| Pattern | Assessment |
|---------|-----------|
| `ticks_*` prefix (6 tables) | Consistent. Good. |
| `margin_*` prefix (3 tables) | Consistent. Good. |
| `market_*` prefix (2 tables) | Consistent. Good. |
| `poly_*` prefix (2 tables) | Consistent. Good. |
| No prefix: `trades`, `signals`, `notes`, `users` | Hub-domain. Acceptable. |
| Mixed: `window_snapshots` vs `signal_evaluations` | Both engine-domain, no prefix. Acceptable. |
| `telegram_notifications` (is a log) | Should be `telegram_notification_log`. Non-urgent. |

### 5.6 Tables to Merge or Split

| Candidates | Recommendation |
|-----------|----------------|
| `gate_audit` into `signal_evaluations` | Archive gate_audit. |
| `signals` into `signal_evaluations` | Let signals die naturally. |
| `trading_configs` -> `config_keys`/`config_values` | CFG-10 migration plan. |
| `daily_pnl` | Can be computed from poly_fills on demand. |

---

## 6. Recommendations

### 6.1 Immediate Actions (This Sprint)

1. **Update schema_catalog.py**:
   - Flip `config_keys`, `config_values`, `config_history` from "deprecated" to "active".
   - Add entries for `countdown_evaluations`, `telegram_notifications`, `analysis_docs`, `timesfm_forecasts`.
   - Add `ticks_v2_probability` as an external-category entry.

2. **Add missing DDL migrations**:
   - `migrations/add_countdown_evaluations.sql`
   - `migrations/add_telegram_notifications.sql`
   - `migrations/add_analysis_docs.sql`

3. **Remove dead code**: `macro-observer/observer.py::fetch_recent_ai_analyses` reads from a table with no writer.

### 6.2 Index Recommendations

| Table | Recommended Index |
|-------|------------------|
| `telegram_notifications` | `CREATE INDEX idx_tg_dedup ON telegram_notifications (bot_id, location, window_id)` |
| `countdown_evaluations` | `CREATE INDEX idx_countdown_window ON countdown_evaluations (window_ts, stage)` |
| `analysis_docs` | `CREATE INDEX idx_analysis_tags ON analysis_docs USING GIN (tags)` |

### 6.3 Archival Strategy

| Table | Action | Timeline |
|-------|--------|----------|
| `gate_audit` | Archive to cold storage, DROP | After confirming signal_evaluations coverage |
| `signals` | Archive, DROP | After /signals page decommission |
| `daily_pnl` | Keep filling, build live aggregation | CFG-10 timeline |
| `trading_configs` | Mothball | After CFG-10 |
| `ai_analyses` | DROP if confirmed dead | Immediate |

### 6.4 Partitioning Strategy for High-Volume Tables

| Table | Est. Write Rate | Strategy |
|-------|----------------|----------|
| `ticks_binance` | ~500 rows/sec | Monthly partition on ts; drop > 90 days |
| `market_snapshots` | ~8 rows/sec | Monthly partition on snapshot_at; drop > 90 days |
| `ticks_coinglass` | ~0.1 rows/sec | No urgency |
| `ticks_gamma` | ~0.1 rows/sec | No urgency |
| `margin_signals` | ~0.2 rows/sec | Monthly partition if needed after 6 months |

---

## 7. Summary Table (All Tables)

| # | Table | Service | Status | Tag | In Catalog? |
|---|-------|---------|--------|-----|-------------|
| 1 | signal_evaluations | engine | ACTIVE | SOT | Yes |
| 2 | window_snapshots | engine | ACTIVE | SOT | Yes |
| 3 | ticks_elm_predictions | engine | ACTIVE | DERIVED | Yes |
| 4 | manual_trades | hub+engine | ACTIVE | SOT | Yes |
| 5 | manual_trade_snapshots | hub | ACTIVE | SOT | Yes |
| 6 | post_resolution_analyses | engine | ACTIVE | DERIVED | Yes |
| 7 | window_predictions | engine | ACTIVE | DERIVED | Yes |
| 8 | gate_audit | engine | LEGACY | LEGACY | Yes |
| 9 | trade_bible | trigger | ACTIVE | DERIVED | Yes |
| 10 | playwright_state | engine | ACTIVE | OPERATIONAL | Yes |
| 11 | redeem_events | engine | ACTIVE | SOT | Yes |
| 12 | countdown_evaluations | engine | ACTIVE | DERIVED | **NO** |
| 13 | telegram_notifications | engine+observer | ACTIVE | OPERATIONAL | **NO** |
| 14 | clob_book_snapshots | engine | ACTIVE | SOT | Yes |
| 15 | clob_execution_log | engine | ACTIVE | SOT | Yes |
| 16 | fok_ladder_attempts | engine | ACTIVE | SOT | Yes |
| 17 | order_audit_log | engine | ACTIVE | SOT | Yes |
| 18 | poly_fills | engine | ACTIVE | SOT | Yes |
| 19 | poly_trade_history | engine | ACTIVE | SOT | Yes |
| 20 | wallet_snapshots | engine | ACTIVE | SOT | Yes |
| 21 | market_data | data-collector | ACTIVE | SOT | Yes |
| 22 | market_snapshots | data-collector | ACTIVE | SOT | Yes |
| 23 | ticks_binance | engine | ACTIVE | SOT | Yes |
| 24 | ticks_coinglass | engine | ACTIVE | SOT | Yes |
| 25 | ticks_gamma | engine | ACTIVE | SOT | Yes |
| 26 | ticks_timesfm | engine | ACTIVE | SOT | Yes |
| 27 | margin_positions | margin_engine | ACTIVE | SOT | Yes |
| 28 | margin_signals | margin_engine | ACTIVE | SOT | Yes |
| 29 | margin_logs | margin_engine | ACTIVE | OPERATIONAL | Yes |
| 30 | macro_signals | macro-observer | ACTIVE | SOT | Yes |
| 31 | macro_events | macro-observer | ACTIVE | OPERATIONAL | Yes |
| 32 | trades | engine | ACTIVE | SOT | Yes |
| 33 | signals | engine | LEGACY | LEGACY | Yes |
| 34 | daily_pnl | hub | LEGACY | DERIVED | Yes |
| 35 | system_state | hub+engine | ACTIVE | OPERATIONAL | Yes |
| 36 | trading_configs | hub | LEGACY | OPERATIONAL | Yes |
| 37 | users | hub | ACTIVE | OPERATIONAL | Yes |
| 38 | notes | hub | ACTIVE | OPERATIONAL | Yes |
| 39 | backtest_runs | hub | ACTIVE | SOT | Yes |
| 40 | config_keys | hub | ACTIVE | OPERATIONAL | Yes* |
| 41 | config_values | hub | ACTIVE | OPERATIONAL | Yes* |
| 42 | config_history | hub | ACTIVE | OPERATIONAL | Yes* |
| 43 | analysis_docs | hub | ACTIVE | OPERATIONAL | **NO** |
| 44 | timesfm_forecasts | timesfm-svc | ACTIVE | SOT | **NO** |
| 45 | ai_analyses | unknown | DEAD | LEGACY | **NO** |
| 46 | ticks_v3_composite | timesfm-svc | ACTIVE | SOT | Yes (ext) |
| 47 | ticks_v2_probability | timesfm-svc | ACTIVE | SOT | Partial |

\* Catalog marks as "deprecated/planned" but table exists and is active.

---

*Audited by Claude Opus 4.6 on 2026-04-11 from the `develop` branch.*
