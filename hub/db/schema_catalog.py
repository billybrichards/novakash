"""
schema_catalog — Curated, hand-maintained inventory of every database table
in the novakash trading system (SCHEMA-01).

Why this exists
---------------
The data system grew organically across multiple services (Polymarket
engine, margin engine, macro observer, data collector, hub) and nobody
had a single view of:

  * which tables still matter and which are dead weight
  * which service writes each table and which services read it
  * which tables are authoritative sources of truth vs caches / mirrors
  * whether a table is active, legacy (still queried but superseded), or
    deprecated (no live writers, retained for replay only)

The hub /api/v58/schema/tables endpoint joins this static catalog with
runtime data (row counts, last write timestamp) and exposes it on the
frontend at /schema.

Why a hardcoded dict instead of a discovery script
--------------------------------------------------
We deliberately do NOT auto-discover tables from pg_catalog. Reasons:

1. Auto-discovery would surface mystery tables as "???" — defeats the
   purpose of having an inventory.
2. Human-curated metadata (purpose, writer, reader, active/legacy
   status, docs links) is the whole point. That can only live in code.
3. The catalog is reviewed via PRs. Every new table forces a doc update.
4. Stale legacy tables that still have rows would pollute the view if
   discovered. Here they stay listed with status="legacy" or
   "deprecated" and a clear notes field explaining why.

Adding a new table
------------------
1. Add an entry below following the SchemaEntry shape.
2. Set status to "active", "legacy", or "deprecated".
3. List the actual writer files (with relative paths) so on-call agents
   can grep to the source. Same for readers.
4. Reference any design docs in `docs` so the operator can click through.
5. If the table has a meaningful "last write time" column (a recency
   timestamp like created_at, updated_at, ts), put its name in
   `recency_column` so the live endpoint can compute "last write".
6. If the table is huge (>1M rows expected), set `large=True` so the
   endpoint uses pg_class.reltuples instead of SELECT COUNT(*).

Status definitions
------------------
  active     — Currently written + read by live services. The default.
  legacy     — Still has data + may still be read for analysis, but the
               canonical source has moved elsewhere. New code should NOT
               write here.
  deprecated — No live writers and no live readers. Retained for replay
               or historical analysis only. Can be dropped after a
               retention window.

Categories
----------
  polymarket  — Polymarket engine domain (signal_evaluations, etc.)
  margin      — Binance margin / Hyperliquid perp domain
  macro       — Macro observer (regime, funding, basis, calendar)
  data        — Raw data feeds (ticks, market data, snapshots)
  hub         — Hub-only tables (auth, notes, configs)
  exec        — Execution audit (CLOB, FOK ladder, fills)
  external    — Lives in a different DB / repo (timesfm-service)

Each entry is a flat dict — no Pydantic, no SQLAlchemy. The hub endpoint
serialises it directly.
"""

from __future__ import annotations

from typing import Any

# Type alias for clarity. A SchemaEntry has the keys documented at the
# bottom of this file.
SchemaEntry = dict[str, Any]


# ─── The catalog ──────────────────────────────────────────────────────────
# Keys are the actual Postgres table names. Order is purely cosmetic — the
# hub endpoint groups by category for the frontend.

SCHEMA_CATALOG: dict[str, SchemaEntry] = {

    # ══════════════════════════════════════════════════════════════════════
    # POLYMARKET — engine/ domain
    # ══════════════════════════════════════════════════════════════════════

    "signal_evaluations": {
        "service": "engine",
        "category": "polymarket",
        "status": "active",
        "purpose": (
            "Per-window gate decision + outcome (one row per window per "
            "eval_offset). Primary backtest evidence base — the 865-outcome "
            "dataset that the v10.6 decision-surface proposal was derived "
            "from. Captures clob_*, binance/tiingo/chainlink prices, all "
            "deltas, OAK quantiles, every gate verdict (vpin / delta / cg "
            "/ twap / timesfm), final pass/fail, and the chosen decision."
        ),
        "writers": [
            "engine/persistence/db_client.py::write_signal_evaluation (UPSERT on (window_ts, asset, timeframe, eval_offset))",
            "engine/strategies/five_min_vpin.py (drives the call from inside the gate pipeline)",
        ],
        "readers": [
            "hub/api/v58_monitor.py (UI-01 gate heartbeat + audit endpoints)",
            "scripts/export_truth_dataset.py (offline backtest evidence pull)",
            "engine/strategies/five_min_vpin.py (re-reads its own writes for last-N stats)",
        ],
        "recency_column": "evaluated_at",
        "docs": [
            "docs/V10_3_IMPLEMENTATION_PLAN.md",
            "docs/AUDIT_PROGRESS.md",
            "docs/v10_4_proposal.html",
        ],
        "notes": (
            "G0..G7 gate pipeline order is reflected in the gate_*_passed "
            "columns. v2_probability_up + v2_quantiles capture the OAK "
            "model surface at decision time. evaluated_at is bumped on "
            "ON CONFLICT update so re-evaluations show up as 'recent'."
        ),
        "large": True,
    },

    "window_snapshots": {
        "service": "engine",
        "category": "polymarket",
        "status": "active",
        "purpose": (
            "Coarser per-window snapshot — one row per resolved window. "
            "Holds the engine's final view: open/close, vpin, regime, all "
            "Coinglass fields, TWAP fields, TimesFM fields, market liq, v8 "
            "FOK execution metadata, and the post-resolution analysis "
            "summary. Drives the dashboard window-results page and is the "
            "join key for trades that fired in that window."
        ),
        "writers": [
            "engine/persistence/db_client.py::ensure_window_tables / upsert_window_snapshot",
            "engine/strategies/five_min_vpin.py (one row per resolved window)",
        ],
        "readers": [
            "hub/api/v58_monitor.py (windows, outcomes, accuracy endpoints)",
            "hub/db/models.py (table reflection for v2_* columns)",
            "frontend/src/pages/WindowResults.jsx (via /api/v58/windows)",
        ],
        "recency_column": "created_at",
        "docs": [
            "docs/V10_3_IMPLEMENTATION_PLAN.md",
            "docs/V10_15M_IMPLEMENTATION_PLAN.md",
        ],
        "notes": (
            "Schema is grown additively via ALTER TABLE ADD COLUMN IF NOT "
            "EXISTS — see ensure_window_tables for the full v5.7/v6.0/v8.0 "
            "column list. UNIQUE (window_ts, asset, timeframe)."
        ),
        "large": True,
    },

    "ticks_elm_predictions": {
        "service": "engine",
        "category": "polymarket",
        "status": "active",
        "purpose": (
            "Sequoia v5.2 / ELM model probability predictions. Legacy "
            "'elm' name retained from PE-06 fix because the table existed "
            "before the rename. Background passive sweep — one row per "
            "asset per delta bucket per poll (~30s). NOT consumed by the "
            "strategy decision path; v5 trains from signal_evaluations."
        ),
        "writers": [
            "engine/data/feeds/elm_prediction_recorder.py",
        ],
        "readers": [
            "frontend/src/pages/data-surfaces/V3Surface.jsx (diagnostic chart)",
        ],
        "recency_column": "ts",
        "docs": [
            "docs/superpowers/specs/2026-04-09-elm-v4-proposal.md",
        ],
        "notes": (
            "Diagnostic only. Pull-mode feature assembly is broken on v5; "
            "expect all-NaN rows during v5 promotion. Resumed varying "
            "outputs after the rollback to v4. Do not switch this recorder "
            "to push-mode without a feature source."
        ),
        "large": True,
    },

    "manual_trades": {
        "service": "engine",
        "category": "polymarket",
        "status": "active",
        "purpose": (
            "Operator-placed manual trades (paper or live). One row per "
            "click of the ManualTradePanel. The hub writes the row, the "
            "Montreal engine pollers pick it up to route the order. Carries "
            "outcome_direction + pnl_usd after resolution."
        ),
        "writers": [
            "hub/api/v58_monitor.py (POST /api/v58/manual-trade)",
            "engine/strategies/orchestrator.py (status updates from the poller)",
        ],
        "readers": [
            "engine/strategies/orchestrator.py::orchestrator_loop (manual trade pollers)",
            "hub/api/v58_monitor.py (GET /api/v58/manual-trades)",
            "frontend/src/pages/execution-hq/components/ManualTradePanel.jsx",
        ],
        "recency_column": "created_at",
        "docs": [
            "docs/AUDIT_PROGRESS.md (LT-02, LT-03 entries)",
        ],
        "notes": (
            "LT-02 fix: orchestrator falls back to market_data table for "
            "token_ids when the in-memory ring buffer misses. LT-03 paired "
            "snapshot row in manual_trade_snapshots."
        ),
    },

    "manual_trade_snapshots": {
        "service": "engine",
        "category": "polymarket",
        "status": "active",
        "purpose": (
            "LT-03 decision-snapshot table. One row per manual_trades row. "
            "JSONB captures full v4 fusion surface, v3 composite snapshot, "
            "last-5 outcomes, macro bias, VPIN, gate pipeline verdicts, and "
            "engine_would_have_done so post-resolution we can tell whether "
            "the operator or the engine was right and where they disagree."
        ),
        "writers": [
            "hub/api/v58_monitor.py (POST /api/v58/manual-trade — paired insert with manual_trades)",
        ],
        "readers": [
            "hub/api/v58_monitor.py (GET /api/v58/manual-trade-snapshots)",
            "frontend/src/pages/execution-hq/components/ManualTradePanel.jsx (operator rationale field)",
        ],
        "recency_column": "taken_at",
        "docs": [
            "docs/AUDIT_PROGRESS.md (LT-03 entry)",
        ],
        "notes": (
            "JSONB columns let us capture the full surface without forcing "
            "a schema for every field. operator_rationale TEXT is the "
            "free-form justification typed in the panel."
        ),
    },

    "market_data": {
        "service": "data-collector",
        "category": "data",
        "status": "active",
        "purpose": (
            "One row per Polymarket window — slug, condition_id, up/down "
            "prices, best bid/ask, spread, volume, liquidity, up_token_id, "
            "down_token_id, oracle open/close. UPSERTed by data-collector "
            "on every snapshot. The DB-fallback source for orchestrator's "
            "LT-02 token_id lookup when the in-memory ring buffer misses."
        ),
        "writers": [
            "data-collector/collector.py::upsert_market",
            "data-collector/backfill.py (historical fill-in)",
        ],
        "readers": [
            "engine/persistence/db_client.py::get_token_ids_from_market_data (LT-02 fallback)",
            "engine/strategies/orchestrator.py (token_id lookup before manual trade execution)",
        ],
        "recency_column": "last_snapshot_at",
        "docs": [
            "docs/AUDIT_PROGRESS.md (LT-02 entry)",
        ],
        "notes": (
            "UNIQUE (window_ts, asset, timeframe). snapshot_count + "
            "last_snapshot_at let us see how often the collector revisited "
            "each window."
        ),
        "large": True,
    },

    "market_snapshots": {
        "service": "data-collector",
        "category": "data",
        "status": "active",
        "purpose": (
            "Append-only intra-window price snapshots — multiple readings "
            "per window (every collector poll). Useful for debugging "
            "snapshot timing and replaying intra-window price evolution."
        ),
        "writers": [
            "data-collector/collector.py",
        ],
        "readers": [
            "scripts/backtest_5min_markets.py (historical replay)",
        ],
        "recency_column": "snapshot_at",
        "docs": [],
        "notes": "No primary uniqueness — pure append log.",
        "large": True,
    },

    "clob_book_snapshots": {
        "service": "engine",
        "category": "exec",
        "status": "active",
        "purpose": (
            "CLOB order book snapshots per window. PE-01 fix added this. "
            "Captures top-of-book + depth for both UP and DOWN tokens, "
            "plus top-5 levels in JSONB. Used for execution post-mortems "
            "and FOK ladder analysis."
        ),
        "writers": [
            "engine/data/feeds/clob_feed.py (Montreal-only — geo-blocked elsewhere)",
        ],
        "readers": [
            "engine/strategies/five_min_vpin.py (intra-window CLOB state for the gate pipeline)",
            "hub/api/v58_monitor.py (window-detail endpoints)",
        ],
        "recency_column": "ts",
        "docs": [
            "migrations/add_clob_execution_audit_tables.sql",
        ],
        "notes": "UNIQUE (window_ts, up_token_id, down_token_id, ts).",
        "large": True,
    },

    "clob_execution_log": {
        "service": "engine",
        "category": "exec",
        "status": "active",
        "purpose": (
            "Every FOK / GTC order placement attempt: target price, max/"
            "min cap, CLOB state at submission, fok_attempt_num, fill "
            "result, latency. Built for debugging FOK ladder behavior."
        ),
        "writers": [
            "engine/execution/clob_executor.py",
        ],
        "readers": [
            "scripts/analyze_fok_fills.py",
        ],
        "recency_column": "ts",
        "docs": [
            "migrations/add_clob_execution_audit_tables.sql",
        ],
        "notes": "Pairs with fok_ladder_attempts via execution_log_id FK.",
        "large": True,
    },

    "fok_ladder_attempts": {
        "service": "engine",
        "category": "exec",
        "status": "active",
        "purpose": (
            "Per-attempt rows for the FOK ladder. One row per ladder rung, "
            "linked back to the parent clob_execution_log row."
        ),
        "writers": [
            "engine/execution/clob_executor.py",
        ],
        "readers": [
            "scripts/analyze_fok_fills.py",
        ],
        "recency_column": "ts",
        "docs": [
            "migrations/add_clob_execution_audit_tables.sql",
        ],
        "notes": "FK execution_log_id → clob_execution_log(id). UNIQUE (execution_log_id, attempt_num).",
    },

    "order_audit_log": {
        "service": "engine",
        "category": "exec",
        "status": "active",
        "purpose": (
            "Cross-mode order submission audit (FOK / GTC / GTD). One row "
            "per order submitted, tracking status transitions and fill "
            "details. Broader than clob_execution_log because it covers "
            "all execution modes including paper."
        ),
        "writers": [
            "engine/execution/order_router.py",
        ],
        "readers": [
            "hub/api/v58_monitor.py (audit endpoints)",
        ],
        "recency_column": "ts",
        "docs": [
            "migrations/add_clob_execution_audit_tables.sql",
        ],
        "notes": "UNIQUE on order_id.",
    },

    "poly_fills": {
        "service": "engine",
        "category": "exec",
        "status": "active",
        "purpose": (
            "Authoritative source-of-truth record of every Polymarket CLOB "
            "fill, sourced from data-api.polymarket.com. Append-only and "
            "tagged by source ('data-api' / 'clob-api' / 'on-chain' / "
            "'engine-reported') so we can detect engine-vs-reality "
            "discrepancies. THIS is the table to read for true P&L."
        ),
        "writers": [
            "hub/db/migrations/versions/20260410_01_poly_fills.sql (table creation)",
            "engine/reconciliation/poly_fills_reconciler.py (fill ingest from data-api)",
        ],
        "readers": [
            "hub/api/pnl.py (true P&L computation)",
            "hub/api/v58_monitor.py (multi-fill bug detection)",
        ],
        "recency_column": "verified_at",
        "docs": [
            "hub/db/migrations/versions/20260410_01_poly_fills.sql",
            "docs/RECONCILIATION-SERVICE-DESIGN.md",
        ],
        "notes": (
            "Append-only. Unique by transaction_hash. is_multi_fill flag "
            "marks rows that hit the multi-fill bug. trade_bible_id NULL "
            "= orphan fill (engine never tracked the trade)."
        ),
    },

    "poly_trade_history": {
        "service": "engine",
        "category": "polymarket",
        "status": "active",
        "purpose": (
            "Polymarket trade history snapshot — fetched every 5min from "
            "Polymarket public API for the proxy wallet. Append-only by "
            "fill_id. Used to reconcile engine-side trades with what "
            "actually shows in the wallet history."
        ),
        "writers": [
            "engine/reconciliation/poly_trade_history.py",
        ],
        "readers": [
            "hub/api/pnl.py",
        ],
        "recency_column": "fetched_at",
        "docs": [],
        "notes": "Sibling to poly_fills but sourced from a different endpoint.",
    },

    "post_resolution_analyses": {
        "service": "engine",
        "category": "polymarket",
        "status": "active",
        "purpose": (
            "AI-written post-resolution analysis per window — what the "
            "oracle said, n_ticks, missed_profit_usd, blocked_loss_usd, "
            "cap_too_tight flag, gate recommendation, and the LLM "
            "narrative. One row per resolved window."
        ),
        "writers": [
            "engine/persistence/db_client.py::ensure_post_resolution_analysis",
            "engine/strategies/orchestrator.py (post-resolution sweep)",
        ],
        "readers": [
            "hub/api/v58_monitor.py (per-window detail panels)",
            "frontend/src/pages/WindowResults.jsx",
        ],
        "recency_column": "analysed_at",
        "docs": [],
        "notes": "UNIQUE (window_ts, asset, timeframe).",
    },

    "window_predictions": {
        "service": "engine",
        "category": "polymarket",
        "status": "active",
        "purpose": (
            "Tiingo + Chainlink directional prediction per window — was "
            "Tiingo right? Was Chainlink right? Was our v2 signal right? "
            "Used for cross-source accuracy reporting."
        ),
        "writers": [
            "engine/persistence/db_client.py::ensure_window_predictions_table",
        ],
        "readers": [
            "hub/api/v58_monitor.py (accuracy endpoints)",
        ],
        "recency_column": "created_at",
        "docs": [],
        "notes": "UNIQUE (window_ts, asset, timeframe).",
    },

    "gate_audit": {
        "service": "engine",
        "category": "polymarket",
        "status": "legacy",
        "purpose": (
            "v8.0 per-window gate pass/fail audit trail. Predates "
            "signal_evaluations which captures the same information at "
            "finer granularity (per eval_offset rather than per window). "
            "Still written as a backup but new analysis should read "
            "signal_evaluations instead."
        ),
        "writers": [
            "engine/persistence/db_client.py::write_gate_audit",
        ],
        "readers": [
            "scripts/export_truth_dataset.py (cross-check vs signal_evaluations)",
        ],
        "recency_column": "evaluated_at",
        "docs": [
            "migrations/add_gate_audit_table.sql",
        ],
        "notes": (
            "UNIQUE (window_ts, asset, timeframe). Superseded by "
            "signal_evaluations which has the eval_offset axis."
        ),
    },

    "trade_bible": {
        "service": "engine",
        "category": "polymarket",
        "status": "active",
        "purpose": (
            "Auto-populated derived table joining trades + entry_reason "
            "metadata, with config_version (v10/v9/v8/v2.2), eval_tier "
            "(DUNE_NORMAL / GOLDEN / EARLY_CASCADE / etc.), and per-trade "
            "outcome. Populated by trigger on trades INSERT/UPDATE."
        ),
        "writers": [
            "migrations/populate_trade_bible.sql (creates the table + trigger)",
            "engine (indirect — trigger fires on trades writes)",
        ],
        "readers": [
            "hub/api/pnl.py (config-version P&L breakdowns)",
            "hub/api/v58_monitor.py (eval_tier accuracy)",
            "scripts/export_truth_dataset.py",
        ],
        "recency_column": "bible_created_at",
        "docs": [
            "migrations/populate_trade_bible.sql",
        ],
        "notes": (
            "READ-ONLY in practice — never INSERT directly. The trigger "
            "extracts config_version + eval_tier from trades.metadata->>"
            "'entry_reason'. Note: this is a derived view of poly_fills + "
            "trades; for true P&L always read poly_fills directly."
        ),
    },

    "ticks_binance": {
        "service": "engine",
        "category": "data",
        "status": "active",
        "purpose": (
            "Buffered Binance aggTrade ticks (price, qty, is_buyer_maker, "
            "vpin). Batch-flushed every 1s by the tick recorder. Source "
            "data for VPIN computation and intra-window microstructure."
        ),
        "writers": [
            "engine/persistence/tick_recorder.py::record_binance_tick",
        ],
        "readers": [
            "engine/signals/vpin.py",
            "scripts/replay_ticks.py",
        ],
        "recency_column": "ts",
        "docs": [
            "docs/DATA_FEEDS.md",
        ],
        "notes": "Very high write volume — large=True. Indexed (asset, ts DESC).",
        "large": True,
    },

    "ticks_coinglass": {
        "service": "engine",
        "category": "data",
        "status": "active",
        "purpose": (
            "Coinglass derivatives data: OI, liquidations, long/short %, "
            "funding, top-trader %, taker buy/sell. One row per poll per "
            "asset. Source for the v8 cg_* gate columns."
        ),
        "writers": [
            "engine/persistence/tick_recorder.py::record_coinglass_tick",
        ],
        "readers": [
            "engine/signals/coinglass_features.py",
        ],
        "recency_column": "ts",
        "docs": [
            "docs/DATA_FEEDS.md",
        ],
        "large": True,
    },

    "ticks_gamma": {
        "service": "engine",
        "category": "data",
        "status": "active",
        "purpose": (
            "Polymarket gamma-API price ticks per window: up_price, "
            "down_price, slug, up/down token ids, source attribution. "
            "Higher-level than CLOB but cheaper to fetch."
        ),
        "writers": [
            "engine/persistence/tick_recorder.py::record_gamma_tick",
        ],
        "readers": [
            "engine/strategies/five_min_vpin.py",
        ],
        "recency_column": "ts",
        "docs": [],
        "large": True,
    },

    "ticks_timesfm": {
        "service": "engine",
        "category": "data",
        "status": "active",
        "purpose": (
            "TimesFM forecast ticks per window: predicted_close, p10/p50/"
            "p90 quantiles, direction, confidence, fetch latency. Drives "
            "the timesfm_* columns on window_snapshots."
        ),
        "writers": [
            "engine/persistence/tick_recorder.py::record_timesfm_tick",
        ],
        "readers": [
            "engine/strategies/five_min_vpin.py (gate_timesfm logic)",
            "hub/api/v58_monitor.py",
        ],
        "recency_column": "ts",
        "docs": [
            "see novakash-timesfm-repo for the upstream service",
        ],
        "notes": (
            "Sourced from the TimesFM service (different repo). The repo "
            "boundary means schema changes here require coordinated "
            "deploys."
        ),
        "large": True,
    },

    "playwright_state": {
        "service": "engine",
        "category": "polymarket",
        "status": "active",
        "purpose": (
            "Single-row snapshot of the Playwright browser session: "
            "logged_in, browser_alive, USDC balance, positions JSON, "
            "redeemable JSON, latest screenshot. The hub reads this to "
            "render the live wallet panel."
        ),
        "writers": [
            "engine/persistence/db_client.py::update_playwright_state",
            "engine/polymarket_browser/* (Playwright session manager)",
        ],
        "readers": [
            "hub/api/playwright.py",
            "frontend/src/pages/PlaywrightDashboard.jsx",
        ],
        "recency_column": "updated_at",
        "docs": [],
        "notes": "Singleton row id=1 with CHECK constraint.",
    },

    "redeem_events": {
        "service": "engine",
        "category": "polymarket",
        "status": "active",
        "purpose": (
            "Append-only log of redeem sweeps run via the Playwright "
            "browser — redeemed_count, failed_count, total_value, full "
            "details JSON."
        ),
        "writers": [
            "engine/persistence/db_client.py::write_redeem_event",
        ],
        "readers": [
            "hub/api/playwright.py",
        ],
        "recency_column": "created_at",
        "docs": [],
    },

    "wallet_snapshots": {
        "service": "engine",
        "category": "polymarket",
        "status": "active",
        "purpose": (
            "USDC balance polled directly from the Polymarket CLOB by the "
            "reconciler. Append-only timeseries used to plot wallet "
            "balance over time independent of trade-level P&L."
        ),
        "writers": [
            "engine/reconciliation/clob_reconciler.py",
        ],
        "readers": [
            "hub/api/pnl.py (wallet curve)",
            "frontend/src/pages/PnL.jsx",
        ],
        "recency_column": "recorded_at",
        "docs": [
            "migrations/add_wallet_snapshots.sql",
        ],
    },

    # ══════════════════════════════════════════════════════════════════════
    # MARGIN ENGINE — margin_engine/ domain
    # ══════════════════════════════════════════════════════════════════════

    "margin_positions": {
        "service": "margin_engine",
        "category": "margin",
        "status": "active",
        "purpose": (
            "Open + closed margin positions on Binance margin / Hyperliquid. "
            "One row per position id. Tracks side, leverage, entry_price, "
            "notional, collateral, SL/TP, exit price, exit reason, realised "
            "P&L, plus PR-B v4 audit snapshot fields (regime, macro_bias, "
            "expected_move_bps, composite_v3, consensus_safe at entry)."
        ),
        "writers": [
            "margin_engine/adapters/persistence/pg_repository.py::PgPositionRepository.save",
        ],
        "readers": [
            "hub/api/margin.py",
            "frontend/src/pages/margin-engine/MarginEngine.jsx",
            "margin_engine/domain/* (position lifecycle)",
        ],
        "recency_column": "opened_at",
        "docs": [
            "docs/superpowers/specs/2026-04-09-v1-composite-binance-design.md",
        ],
        "notes": (
            "Schema is grown via additive ALTER TABLE migrations applied "
            "at boot. Partial index on closed_at for the Trade Timeline "
            "tab's most-recent-closed query."
        ),
    },

    "margin_signals": {
        "service": "margin_engine",
        "category": "margin",
        "status": "active",
        "purpose": (
            "Per-tick signal evaluations for the perp trader — composite "
            "score, broken-out elm/cascade/taker/oi/funding/vpin/momentum, "
            "cascade FSM state (strength, tau1, tau2, exhaustion_t), full "
            "signals_json + cascade_json for whatever wasn't extracted."
        ),
        "writers": [
            "margin_engine/adapters/persistence/pg_signal_repository.py::PgSignalRepository.write_batch",
        ],
        "readers": [
            "hub/api/margin.py (composite chart)",
            "frontend/src/pages/CompositeSignals.jsx",
        ],
        "recency_column": "ts",
        "docs": [],
        "notes": "Indexed on (timescale, ts) for per-horizon scans.",
        "large": True,
    },

    "margin_logs": {
        "service": "margin_engine",
        "category": "margin",
        "status": "active",
        "purpose": (
            "Async-flushed structured log records from the margin engine — "
            "level, logger, message, extra JSONB. Mirrors stdout but is "
            "queryable from the hub."
        ),
        "writers": [
            "margin_engine/adapters/persistence/pg_log_repository.py::PgLogRepository.write_batch",
        ],
        "readers": [
            "hub/api/margin.py (log tail endpoint)",
            "frontend/src/pages/margin-engine/MarginEngine.jsx",
        ],
        "recency_column": "ts",
        "docs": [],
        "large": True,
    },

    # ══════════════════════════════════════════════════════════════════════
    # MACRO OBSERVER — macro-observer/ domain
    # ══════════════════════════════════════════════════════════════════════

    "macro_signals": {
        "service": "macro-observer",
        "category": "macro",
        "status": "active",
        "purpose": (
            "Macro regime / funding / basis snapshots emitted by the LLM "
            "observer. bias (BULL/BEAR/NEUTRAL), confidence (0-100), "
            "direction_gate, threshold_modifier, size_modifier, "
            "override_active flag. Inputs (oracle ratios, BTC deltas, "
            "exchange spreads, funding, OI delta) logged for replay. "
            "timescale_map JSONB carries per-horizon bias (5m/15m/1h/4h)."
        ),
        "writers": [
            "macro-observer/observer.py::init_db + emit_signal",
        ],
        "readers": [
            "engine/strategies/five_min_vpin.py (macro veto / sizing modifier)",
            "margin_engine/* (per-timescale macro gate)",
            "hub/api/v58_monitor.py (macro panel)",
            "frontend/src/pages/data-surfaces/V4Surface.jsx",
        ],
        "recency_column": "created_at",
        "docs": [
            "migrations/add_macro_observer_tables.sql",
            "migrations/add_macro_signals_timescale_map.sql",
        ],
        "notes": (
            "Phase 2 added timescale_map JSONB so per-horizon bias is "
            "preserved. NULL timescale_map = pre-Phase-2 row."
        ),
    },

    "macro_events": {
        "service": "macro-observer",
        "category": "macro",
        "status": "active",
        "purpose": (
            "Pre-loaded economic calendar — Fed / CPI / FOMC events with "
            "impact rating (LOW / MEDIUM / HIGH / EXTREME) and "
            "actual/forecast values. Lets the macro observer flag "
            "'upcoming_event' in its bias output."
        ),
        "writers": [
            "macro-observer/observer.py (calendar refresh task)",
        ],
        "readers": [
            "macro-observer/observer.py (event-proximity check)",
            "hub/api/v58_monitor.py (event banner)",
        ],
        "recency_column": "event_time",
        "docs": [
            "migrations/add_macro_observer_tables.sql",
        ],
    },

    # ══════════════════════════════════════════════════════════════════════
    # HUB — hub/ domain
    # ══════════════════════════════════════════════════════════════════════

    "users": {
        "service": "hub",
        "category": "hub",
        "status": "active",
        "purpose": (
            "Dashboard authentication accounts. Username + bcrypt hashed "
            "password. Tiny — typically just the operator + a few audit "
            "viewers."
        ),
        "writers": [
            "hub/auth/routes.py (registration / password change)",
        ],
        "readers": [
            "hub/auth/routes.py (login)",
            "hub/auth/middleware.py",
        ],
        "recency_column": "created_at",
        "docs": [
            "hub/db/schema.sql",
        ],
    },

    "notes": {
        "service": "hub",
        "category": "hub",
        "status": "active",
        "purpose": (
            "DB-backed audit journal — observations, TODOs, working notes "
            "added during long audit sessions. Survives frontend "
            "redeploys. Filterable by status (open / archived) and tag. "
            "Drives the /notes page (NT-01)."
        ),
        "writers": [
            "hub/api/notes.py (POST/PATCH/DELETE /api/notes)",
        ],
        "readers": [
            "hub/api/notes.py (GET /api/notes)",
            "frontend/src/pages/Notes.jsx",
        ],
        "recency_column": "updated_at",
        "docs": [
            "frontend/src/pages/Notes.jsx",
        ],
        "notes": (
            "Migrated in hub/main.py::lifespan with a seed row so the page "
            "isn't empty on first deploy."
        ),
    },

    "trades": {
        "service": "hub",
        "category": "hub",
        "status": "active",
        "purpose": (
            "Hub-side trade record — order_id, strategy, venue, "
            "market_slug, direction (YES/NO/ARB), entry_price, stake, "
            "fee, status, outcome, payout, pnl. Mirrors what the engine "
            "reported. The trigger on this table populates trade_bible."
        ),
        "writers": [
            "hub/api/trades.py (engine POSTs trade events)",
            "engine/persistence/db_client.py::insert_trade",
        ],
        "readers": [
            "hub/api/trades.py (GET endpoints)",
            "hub/api/pnl.py",
            "frontend/src/pages/Trades.jsx",
            "trade_bible trigger (auto-populates on INSERT/UPDATE)",
        ],
        "recency_column": "created_at",
        "docs": [
            "hub/db/schema.sql",
        ],
        "notes": (
            "Engine-reported P&L lives here, but for true P&L always "
            "read poly_fills which is the on-chain source of truth."
        ),
    },

    "signals": {
        "service": "hub",
        "category": "hub",
        "status": "legacy",
        "purpose": (
            "Generic signal-event log — VPIN, cascade, arb, regime "
            "snapshots dumped as JSONB. Predates the structured "
            "signal_evaluations table on the engine side."
        ),
        "writers": [
            "engine (rare — most signals now live in signal_evaluations)",
        ],
        "readers": [
            "hub/api/signals.py",
        ],
        "recency_column": "created_at",
        "docs": [
            "hub/db/schema.sql",
        ],
        "notes": (
            "New code should write to signal_evaluations or "
            "margin_signals instead. Retained for backward-compat with "
            "the /signals page."
        ),
    },

    "daily_pnl": {
        "service": "hub",
        "category": "hub",
        "status": "legacy",
        "purpose": (
            "Pre-aggregated daily P&L stats — total_pnl, wins, losses, "
            "win_rate, bankroll_end, strategy_breakdown JSONB. Designed "
            "for fast charting before the live aggregations got fast "
            "enough."
        ),
        "writers": [
            "hub/services/pnl_service.py (end-of-day rollup)",
        ],
        "readers": [
            "hub/services/dashboard_service.py",
            "frontend/src/pages/PnL.jsx",
        ],
        "recency_column": "date",
        "docs": [
            "hub/db/schema.sql",
        ],
        "notes": (
            "Likely to be replaced by live aggregations against poly_fills "
            "now that fills are reliable. Keep filling for now to avoid "
            "breaking the historical chart."
        ),
    },

    "system_state": {
        "service": "hub",
        "category": "hub",
        "status": "active",
        "purpose": (
            "Single-row k/v store for live_enabled, paper_enabled, mode, "
            "active_paper_config_id, active_live_config_id. The HOT "
            "RELOAD source of truth for trading mode — orchestrator.py "
            "polls this on every heartbeat (STOP-01 lesson: .env values "
            "are ignored if DB says otherwise)."
        ),
        "writers": [
            "hub/api/system.py (mode toggle)",
            "hub/api/trading_config.py (active config selection)",
            "engine/strategies/orchestrator.py (heartbeat updates)",
        ],
        "readers": [
            "engine/strategies/orchestrator.py (mode + active config polling)",
            "hub/api/system.py (status endpoints)",
            "hub/api/dashboard.py",
        ],
        "recency_column": "updated_at",
        "docs": [
            "hub/db/schema.sql",
            "docs/AUDIT_PROGRESS.md (STOP-01 entry)",
        ],
        "notes": (
            "Singleton id=1 with CHECK constraint. NEVER bypass this "
            "table to flip mode — that's how STOP-01 happened."
        ),
    },

    "trading_configs": {
        "service": "hub",
        "category": "hub",
        "status": "legacy",
        "purpose": (
            "DB-backed trading config overlay — name, version, JSONB "
            "config blob, mode (paper/live), is_active, is_approved, "
            "approver, parent_id for cloning. Will be superseded by "
            "CFG-01 (config_keys / config_values / config_history) once "
            "CFG-02/03 ship — see CONFIG_MIGRATION_PLAN.md."
        ),
        "writers": [
            "hub/api/trading_config.py (CRUD)",
        ],
        "readers": [
            "hub/api/trading_config.py",
            "engine/config/loader.py (overlay onto YAML defaults)",
            "frontend/src/pages/TradingConfig.jsx",
        ],
        "recency_column": "updated_at",
        "docs": [
            "hub/db/schema.sql",
            "hub/db/migrations/004_trading_configs.sql",
            "docs/CONFIG_MIGRATION_PLAN.md",
        ],
        "notes": (
            "Active row picked via system_state.active_paper_config_id / "
            "active_live_config_id. The whole config blob lives in JSONB "
            "which is why CFG-01/02/03 will move it into a structured "
            "key/value layout."
        ),
    },

    "backtest_runs": {
        "service": "hub",
        "category": "hub",
        "status": "active",
        "purpose": (
            "Stored backtest results — strategy, start/end, total_pnl, "
            "num_trades, win_rate, sharpe, max_drawdown, params + full "
            "trades_json. Used for run-to-run comparison."
        ),
        "writers": [
            "hub/api/backtest.py (POST /api/backtest/run)",
            "scripts/backtest_*.py (offline runs)",
        ],
        "readers": [
            "hub/api/backtest.py",
            "frontend/src/pages/StrategyAnalysis.jsx",
        ],
        "recency_column": "created_at",
        "docs": [
            "hub/db/schema.sql",
        ],
    },

    # ══════════════════════════════════════════════════════════════════════
    # PLANNED — not yet shipped
    # ══════════════════════════════════════════════════════════════════════

    "config_keys": {
        "service": "hub",
        "category": "hub",
        "status": "deprecated",
        "purpose": (
            "PLANNED (CFG-02): structured config-key catalog. Not yet "
            "created. When CFG-02 lands, this will hold one row per known "
            "config key with type, default, validation, and ownership."
        ),
        "writers": ["(planned — see CONFIG_MIGRATION_PLAN.md)"],
        "readers": ["(planned)"],
        "recency_column": None,
        "docs": [
            "docs/CONFIG_MIGRATION_PLAN.md",
        ],
        "notes": (
            "Listed here so the inventory matches the design doc. Will "
            "flip to status='active' when CFG-02 ships."
        ),
    },

    "config_values": {
        "service": "hub",
        "category": "hub",
        "status": "deprecated",
        "purpose": (
            "PLANNED (CFG-02): per-mode value table. One row per "
            "(key, mode) with current value, last-changed-by, "
            "last-changed-at."
        ),
        "writers": ["(planned)"],
        "readers": ["(planned)"],
        "recency_column": None,
        "docs": [
            "docs/CONFIG_MIGRATION_PLAN.md",
        ],
        "notes": "Planned. Not yet created.",
    },

    "config_history": {
        "service": "hub",
        "category": "hub",
        "status": "deprecated",
        "purpose": (
            "PLANNED (CFG-03): full append-only audit log of every config "
            "value change. Used for post-incident replay."
        ),
        "writers": ["(planned)"],
        "readers": ["(planned)"],
        "recency_column": None,
        "docs": [
            "docs/CONFIG_MIGRATION_PLAN.md",
        ],
        "notes": "Planned. Not yet created.",
    },

    # ══════════════════════════════════════════════════════════════════════
    # EXTERNAL — different repo (timesfm-service)
    # ══════════════════════════════════════════════════════════════════════

    "ticks_v3_composite": {
        "service": "timesfm-service",
        "category": "external",
        "status": "active",
        "purpose": (
            "v3 composite scorer outputs. Lives in the timesfm-service "
            "DB, NOT in the main novakash trader DB. Listed here only as "
            "a stub so the inventory is complete — see the "
            "novakash-timesfm-repo for the authoritative schema."
        ),
        "writers": ["(see novakash-timesfm-repo)"],
        "readers": ["(see novakash-timesfm-repo)"],
        "recency_column": None,
        "docs": [
            "see novakash-timesfm-repo",
        ],
        "notes": (
            "Different DB / different repo. The hub does not run live "
            "queries against this table — it only documents its existence."
        ),
    },
}


# ─── Convenience accessors ─────────────────────────────────────────────────

def list_categories() -> list[str]:
    """Distinct categories in catalog order."""
    seen: list[str] = []
    for entry in SCHEMA_CATALOG.values():
        cat = entry.get("category", "uncategorised")
        if cat not in seen:
            seen.append(cat)
    return seen


def list_services() -> list[str]:
    """Distinct services in catalog order."""
    seen: list[str] = []
    for entry in SCHEMA_CATALOG.values():
        svc = entry.get("service", "unknown")
        if svc not in seen:
            seen.append(svc)
    return seen


def status_breakdown() -> dict[str, int]:
    """Counts of tables by status."""
    out: dict[str, int] = {"active": 0, "legacy": 0, "deprecated": 0}
    for entry in SCHEMA_CATALOG.values():
        s = entry.get("status", "active")
        out[s] = out.get(s, 0) + 1
    return out


# ─── Schema entry shape (for documentation only) ────────────────────────────
#
# {
#     "service": str            — owning service name
#     "category": str           — polymarket | margin | macro | data | hub | exec | external
#     "status": str             — active | legacy | deprecated
#     "purpose": str            — one-paragraph human-readable purpose
#     "writers": list[str]      — file paths + brief annotation
#     "readers": list[str]      — file paths + brief annotation
#     "recency_column": str|None — column name for "last write" lookup, or None
#     "docs": list[str]         — relative doc / source paths
#     "notes": str              — optional extra context
#     "large": bool             — if True, use pg_class.reltuples instead of COUNT(*)
# }
