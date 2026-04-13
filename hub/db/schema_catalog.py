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
  planned    — Aspirational / not yet created in the DB. Listed in the
               catalog for completeness but the table does not exist yet.
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
        "sot_class": "SOT",
        "data_flow": "ticks_* -> gate_pipeline -> signal_evaluations -> trades",
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
        "sot_class": "SOT",
        "data_flow": "ticks_* -> gate_pipeline -> window_snapshots",
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
        "sot_class": "DERIVED",
        "data_flow": "elm_prediction_recorder -> ticks_elm_predictions",
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
        "sot_class": "SOT",
        "data_flow": "hub POST -> manual_trades -> orchestrator poller -> execution",
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
        "sot_class": "SOT",
        "data_flow": "hub POST -> manual_trade_snapshots (paired with manual_trades)",
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
        "sot_class": "SOT",
        "data_flow": "data-collector -> market_data -> engine token_id lookup",
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
        "sot_class": "SOT",
        "data_flow": "data-collector -> market_snapshots (append-only intra-window)",
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
        "sot_class": "SOT",
        "data_flow": "clob_feed -> clob_book_snapshots -> gate_pipeline + post-mortem",
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
        "status": "planned",
        "sot_class": "SOT",
        "data_flow": "clob_executor -> clob_execution_log -> fok_ladder_attempts",
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
        "status": "planned",
        "sot_class": "SOT",
        "data_flow": "clob_executor -> fok_ladder_attempts (child of clob_execution_log)",
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
        "status": "planned",
        "sot_class": "SOT",
        "data_flow": "order_router -> order_audit_log",
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
        "status": "planned",
        "sot_class": "SOT",
        "data_flow": "data-api reconciler -> poly_fills -> pnl (ground truth)",
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
        "status": "planned",
        "sot_class": "SOT",
        "data_flow": "polymarket public API -> poly_trade_history -> pnl reconciliation",
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
        "status": "planned",
        "sot_class": "DERIVED",
        "data_flow": "signal_evaluations + window_snapshots -> post_resolution_analyses",
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
        "status": "planned",
        "sot_class": "DERIVED",
        "data_flow": "ticks_* -> window_predictions (cross-source accuracy)",
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
        "sot_class": "LEGACY",
        "data_flow": "gate_pipeline -> gate_audit (superseded by signal_evaluations)",
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
        "status": "planned",
        "sot_class": "DERIVED",
        "data_flow": "trades INSERT trigger -> trade_bible (auto-populated)",
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
        "status": "planned",
        "sot_class": "SOT",
        "data_flow": "binance WS -> tick_recorder -> ticks_binance -> vpin calc",
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
        "status": "planned",
        "sot_class": "SOT",
        "data_flow": "coinglass API -> tick_recorder -> ticks_coinglass -> cg features",
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
        "status": "planned",
        "sot_class": "SOT",
        "data_flow": "gamma API -> tick_recorder -> ticks_gamma -> price discovery",
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
        "status": "planned",
        "sot_class": "SOT",
        "data_flow": "timesfm-service HTTP -> tick_recorder -> ticks_timesfm -> gate_timesfm",
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
        "status": "planned",
        "sot_class": "OPERATIONAL",
        "data_flow": "playwright session -> playwright_state -> hub dashboard",
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
        "status": "planned",
        "sot_class": "SOT",
        "data_flow": "playwright redeem sweep -> redeem_events (append-only log)",
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
        "status": "planned",
        "sot_class": "SOT",
        "data_flow": "clob_reconciler -> wallet_snapshots -> pnl wallet curve",
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
        "status": "planned",
        "sot_class": "SOT",
        "data_flow": "margin_engine gates -> margin_positions (open/close lifecycle)",
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
        "status": "planned",
        "sot_class": "SOT",
        "data_flow": "margin_engine -> margin_signals (composite score recording)",
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
        "status": "planned",
        "sot_class": "OPERATIONAL",
        "data_flow": "margin_engine -> margin_logs (structured log sink)",
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
    "strategy_decisions": {
        "service": "margin_engine",
        "category": "margin",
        "status": "planned",
        "sot_class": "SOT",
        "data_flow": "margin_engine V4 strategies -> strategy_decisions (per-strategy evaluations)",
        "purpose": (
            "Per-strategy evaluations at position entry time. Each V4 strategy "
            "that evaluates a position writes a row with its decision "
            "(TRADE_LONG/TRADE_SHORT/NO_TRADE), confidence, timescale, regime, "
            "size_mult, hold_minutes, rationale, and full v4_snapshot JSONB. "
            "Used for backtesting strategy performance and post-trade analysis."
        ),
        "writers": [
            "margin_engine/adapters/persistence/pg_strategy_decision_repository.py::AsyncStrategyDecisionRecorder",
            "margin_engine/use_cases/open_position.py (via strategy_decision_recorder)",
        ],
        "readers": [
            "hub/api/margin.py (GET /api/margin/strategy-decisions)",
            "margin_engine/adapters/persistence/pg_strategy_decision_repository.py::PgStrategyDecisionRepository.get_stats_by_strategy",
        ],
        "recency_column": "created_at",
        "docs": [
            "margin_engine/adapters/persistence/pg_strategy_decision_repository.py",
        ],
        "notes": (
            "Indexed on (position_id), (asset, created_at), (strategy_id). "
            "FK to margin_positions(id) with ON DELETE CASCADE. "
            "v4_snapshot JSONB captures full V4 context at decision time."
        ),
    },
    # ══════════════════════════════════════════════════════════════════════
    # MACRO OBSERVER — macro-observer/ domain
    # ══════════════════════════════════════════════════════════════════════
    "macro_signals": {
        "service": "macro-observer",
        "category": "macro",
        "status": "planned",
        "sot_class": "SOT",
        "data_flow": "macro-observer LLM -> macro_signals -> engine macro veto + margin gate",
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
        "status": "planned",
        "sot_class": "OPERATIONAL",
        "data_flow": "macro-observer calendar refresh -> macro_events -> event-proximity check",
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
        "sot_class": "OPERATIONAL",
        "data_flow": "hub auth -> users",
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
        "sot_class": "OPERATIONAL",
        "data_flow": "hub notes API -> notes (audit journal)",
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
        "sot_class": "SOT",
        "data_flow": "engine -> trades -> trade_bible trigger -> pnl (engine-reported)",
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
        "sot_class": "LEGACY",
        "data_flow": "engine (rare) -> signals (superseded by signal_evaluations)",
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
        "sot_class": "DERIVED",
        "data_flow": "pnl_service end-of-day rollup -> daily_pnl (will be replaced by poly_fills agg)",
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
        "sot_class": "OPERATIONAL",
        "data_flow": "hub + engine -> system_state (singleton mode/config source of truth)",
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
        "sot_class": "OPERATIONAL",
        "data_flow": "hub CRUD -> trading_configs (being replaced by config_keys/values)",
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
        "status": "planned",
        "sot_class": "SOT",
        "data_flow": "backtest API + scripts -> backtest_runs",
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
    # CONFIG V2 — CFG-02/03 (shipped, active)
    # ══════════════════════════════════════════════════════════════════════
    "config_keys": {
        "service": "hub",
        "category": "hub",
        "status": "active",
        "sot_class": "OPERATIONAL",
        "data_flow": "config_seed -> config_keys (CFG-02 structured catalog)",
        "purpose": (
            "CFG-02 structured config-key catalog. One row per known "
            "config key per service, with type, default, validation, "
            "and ownership. Created on every hub boot by config_seed."
        ),
        "writers": [
            "hub/db/config_seed.py (seed on boot)",
            "hub/db/config_schema.py (DDL)",
        ],
        "readers": [
            "hub/api/config_v2.py",
        ],
        "recency_column": None,
        "docs": [
            "docs/CONFIG_MIGRATION_PLAN.md",
        ],
        "notes": (
            "CFG-02 shipped. Table exists and is seeded on every hub "
            "boot. UNIQUE (service, key)."
        ),
    },
    "config_values": {
        "service": "hub",
        "category": "hub",
        "status": "active",
        "sot_class": "OPERATIONAL",
        "data_flow": "config API -> config_values (CFG-02 per-mode values)",
        "purpose": (
            "CFG-02 per-mode config value table. One row per "
            "(key, mode) with current value, last-changed-by, "
            "last-changed-at. Seeded on every hub boot."
        ),
        "writers": [
            "hub/db/config_seed.py (seed on boot)",
            "hub/api/config_v2.py (value updates)",
        ],
        "readers": [
            "hub/api/config_v2.py",
        ],
        "recency_column": None,
        "docs": [
            "docs/CONFIG_MIGRATION_PLAN.md",
        ],
        "notes": "CFG-02 shipped. Table exists and is seeded on every hub boot.",
    },
    "config_history": {
        "service": "hub",
        "category": "hub",
        "status": "active",
        "sot_class": "OPERATIONAL",
        "data_flow": "config API -> config_history (CFG-03 append-only audit log)",
        "purpose": (
            "CFG-03 append-only audit log of every config value change. "
            "Used for post-incident replay. One row per config mutation."
        ),
        "writers": [
            "hub/api/config_v2.py (writes on every config change)",
        ],
        "readers": [
            "hub/api/config_v2.py (history view)",
        ],
        "recency_column": None,
        "docs": [
            "docs/CONFIG_MIGRATION_PLAN.md",
        ],
        "notes": "CFG-03 shipped. Table exists. Append-only.",
    },
    # ══════════════════════════════════════════════════════════════════════
    # MISSING FROM CATALOG — added by data architecture audit 2026-04-11
    # ══════════════════════════════════════════════════════════════════════
    "countdown_evaluations": {
        "service": "engine",
        "category": "polymarket",
        "status": "planned",
        "sot_class": "DERIVED",
        "data_flow": "gate_pipeline -> countdown_evaluations (T-180/T-120/T-90/T-60 snapshots)",
        "purpose": (
            "Multi-stage countdown snapshot per window (T-180 / T-120 / "
            "T-90 / T-60). Captures gate state at each countdown stage "
            "so post-resolution analysis can see how the decision surface "
            "evolved as the window approached close."
        ),
        "writers": [
            "engine/persistence/db_client.py::write_countdown_evaluation",
            "engine/adapters/persistence/pg_signal_repo.py",
        ],
        "readers": [
            "hub/api/v58_monitor.py (countdown endpoints, window detail)",
        ],
        "recency_column": "evaluated_at",
        "docs": ["docs/DATA_ARCHITECTURE_AUDIT_2026-04-11.md"],
        "notes": (
            "Added by data architecture audit 2026-04-11. No CREATE TABLE "
            "DDL found in codebase. Needs composite index on (window_ts, stage)."
        ),
    },
    "telegram_notifications": {
        "service": "engine",
        "category": "polymarket",
        "status": "planned",
        "sot_class": "OPERATIONAL",
        "data_flow": "engine alerts + macro-observer -> telegram_notifications (dedup + audit)",
        "purpose": (
            "Deduplication and audit log for Telegram notifications. "
            "Prevents duplicate alerts for the same event and provides "
            "a queryable history of all notifications sent."
        ),
        "writers": ["engine/alerts/telegram.py", "macro-observer/observer.py"],
        "readers": ["macro-observer/observer.py (dedup check before sending)"],
        "recency_column": "created_at",
        "docs": ["docs/DATA_ARCHITECTURE_AUDIT_2026-04-11.md"],
        "notes": (
            "Added by data architecture audit 2026-04-11. DUAL-WRITER: "
            "engine + macro-observer (append-only, low risk). Needs "
            "composite dedup index on (bot_id, location, window_id)."
        ),
    },
    "analysis_docs": {
        "service": "hub",
        "category": "hub",
        "status": "planned",
        "sot_class": "OPERATIONAL",
        "data_flow": "hub analysis API -> analysis_docs (doc library)",
        "purpose": (
            "Analysis library documents. Stores longer-form analysis "
            "write-ups that complement the quick notes table."
        ),
        "writers": ["hub/api/analysis.py (POST /api/analysis)"],
        "readers": ["hub/api/analysis.py (GET /api/analysis)"],
        "recency_column": "created_at",
        "docs": ["docs/DATA_ARCHITECTURE_AUDIT_2026-04-11.md"],
        "notes": "Added by data architecture audit 2026-04-11. Needs GIN index on tags.",
    },
    "timesfm_forecasts": {
        "service": "timesfm-service",
        "category": "external",
        "status": "planned",
        "sot_class": "SOT",
        "data_flow": "timesfm-service -> timesfm_forecasts -> hub forecast page",
        "purpose": (
            "TimesFM forecast results written by the external "
            "timesfm-service and read by the hub forecast page. "
            "Cross-repo table."
        ),
        "writers": ["(see novakash-timesfm-repo)"],
        "readers": ["hub/api/forecast.py (5 SELECT queries for forecast display)"],
        "recency_column": "created_at",
        "docs": [
            "docs/DATA_ARCHITECTURE_AUDIT_2026-04-11.md",
            "see novakash-timesfm-repo",
        ],
        "notes": "Added by data architecture audit 2026-04-11. Cross-repo table.",
    },
    "ai_analyses": {
        "service": "hub",
        "category": "hub",
        "status": "legacy",
        "sot_class": "LEGACY",
        "data_flow": "no active writer -> ai_analyses -> macro-observer (silent failure)",
        "purpose": (
            "Claude pre-trade assessment summaries. No active writer. "
            "The only reader wraps the query in a bare except and silently fails."
        ),
        "writers": [],
        "readers": [
            "macro-observer/observer.py::fetch_recent_ai_analyses (bare except)"
        ],
        "recency_column": None,
        "docs": ["docs/DATA_ARCHITECTURE_AUDIT_2026-04-11.md"],
        "notes": (
            "Added by data architecture audit 2026-04-11. DEAD TABLE: "
            "no writer, no DDL. Candidate for DROP."
        ),
    },
    # ══════════════════════════════════════════════════════════════════════
    # EXTERNAL — different repo (timesfm-service)
    # ══════════════════════════════════════════════════════════════════════
    "ticks_v3_composite": {
        "service": "timesfm-service",
        "category": "external",
        "status": "planned",
        "sot_class": "SOT",
        "data_flow": "timesfm-service -> ticks_v3_composite (external DB)",
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
    "ticks_v4_decision": {
        "name": "ticks_v4_decision",
        "service": "timesfm-service",
        "category": "data",
        "status": "active",
        "sot_class": "SOT",
        "description": "Full V4 fusion surface per timescale — probability, HMM regime, V3 composite, consensus, macro, recommended action, sub_signals JSONB. Written by V4DBWriter every 5s.",
        "writers": ["timesfm-service"],
        "readers": ["hub/api/v58_monitor.py", "strategy lab"],
        "recency_column": "ts",
        "docs": "docs/DATA_FEEDS.md",
        "notes": "Activated 2026-04-12. Contains full snapshot_full JSONB for ML retraining.",
        "large": True,
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
    out: dict[str, int] = {"active": 0, "planned": 0, "legacy": 0, "deprecated": 0}
    for entry in SCHEMA_CATALOG.values():
        s = entry.get("status", "active")
        out[s] = out.get(s, 0) + 1
    return out


# ─── Schema entry shape (for documentation only) ────────────────────────────
#
# {
#     "service": str            — owning service name
#     "category": str           — polymarket | margin | macro | data | hub | exec | external
#     "status": str             — active | planned | legacy | deprecated
#     "sot_class": str          — SOT | DERIVED | CACHE | LEGACY | OPERATIONAL
#     "data_flow": str          — position in the data pipeline (e.g. "ticks_* -> signal_evaluations -> trades")
#     "purpose": str            — one-paragraph human-readable purpose
#     "writers": list[str]      — file paths + brief annotation
#     "readers": list[str]      — file paths + brief annotation
#     "recency_column": str|None — column name for "last write" lookup, or None
#     "docs": list[str]         — relative doc / source paths
#     "notes": str              — optional extra context
#     "large": bool             — if True, use pg_class.reltuples instead of COUNT(*)
# }


# ─── GATES_CATALOG ──────────────────────────────────────────────────────────
#
# NAV-01 / 2026-04-11 consolidation. Structured inventory of the Polymarket
# 5-minute engine's V10.6 8-gate decision pipeline + selected margin_engine
# v4 gates. Mirrors SCHEMA_CATALOG above but for decision logic.
#
# Why this exists: the user asked for a single place to answer "which gates
# consume which tables" because the system has grown organically. Each entry
# documents pipeline position, file:line, inputs, outputs, env flags,
# fail reasons, tables read, tables written. Hand-curated (same reason as
# SCHEMA_CATALOG — auto-discovery would be noisy and unreliable).
#
# Changes to a gate in code MUST be paired with a matching catalog update
# in the same PR.


GatesCatalogEntry = dict

GATES_CATALOG: dict[str, GatesCatalogEntry] = {
    "eval_offset_bounds": {
        "engine": "polymarket",
        "pipeline_position": "G0",
        "file": "engine/signals/gates.py",
        "class_name": "EvalOffsetBoundsGate",
        "status": "active",
        "purpose": (
            "V10.6 master safety gate (DS-01). Hard-blocks evaluations that "
            "happen too close to window close OR too far from it. Default-OFF "
            "behind V10_6_ENABLED — operator flips to activate."
        ),
        "inputs": ["GateContext.eval_offset"],
        "outputs": ["GateResult.data['offset', 'min', 'max']"],
        "env_flags": [
            "V10_6_ENABLED (master flag, default false)",
            "V10_6_MIN_EVAL_OFFSET (default 90)",
            "V10_6_MAX_EVAL_OFFSET (default 180)",
        ],
        "fail_reasons": ["too late", "too early", "missing eval_offset"],
        "tables_read": [],
        "tables_written": [],
        "docs": ["docs/V10_6_DECISION_SURFACE_PROPOSAL.md (timesfm repo)"],
        "notes": (
            "Namespaced V10_6_ to avoid collision with the existing "
            "V10_MIN_EVAL_OFFSET read by DuneConfidenceGate (opposite semantics)."
        ),
    },
    "source_agreement": {
        "engine": "polymarket",
        "pipeline_position": "G1",
        "file": "engine/signals/gates.py",
        "class_name": "SourceAgreementGate",
        "status": "active",
        "purpose": (
            "Direction consensus vote across Chainlink, Tiingo, Binance. Two "
            "modes selected at __init__: (A) legacy v11.1 2/3 majority or "
            "(B) DQ-01 spot-only mode dropping Binance. Mode B addresses the "
            "Binance 83.1% DOWN bias contaminating ~19.6% of windows."
        ),
        "inputs": [
            "GateContext.delta_chainlink",
            "GateContext.delta_tiingo",
            "GateContext.delta_binance",
        ],
        "outputs": [
            "GateContext.agreed_direction",
            "GateResult.data['cl_dir', 'ti_dir', 'bin_dir'?, 'direction', 'mode']",
        ],
        "env_flags": ["V11_POLY_SPOT_ONLY_CONSENSUS (default false, DQ-01 flag)"],
        "fail_reasons": [
            "missing CL or TI data",
            "spot disagree (spot-only mode)",
            "no majority (2-2 split edge case)",
        ],
        "tables_read": ["market_data (upstream — read by orchestrator into ctx)"],
        "tables_written": [],
        "docs": [
            "docs/CHANGELOG-DQ01-POLY-SPOT-ONLY-CONSENSUS.md",
            "docs/CHANGELOG-v11.1-SOURCE-AGREEMENT-2-3-MAJORITY.md",
        ],
        "notes": (
            "DQ-01 (PR #48) shipped 2026-04-11. Default OFF. When flag is on, "
            "result.data['mode']='spot_only' and bin_dir is ABSENT (proof that "
            "Binance was never read). Binance data still used by every other "
            "gate for VPIN / taker-flow / liquidations."
        ),
    },
    "delta_magnitude": {
        "engine": "polymarket",
        "pipeline_position": "G2",
        "file": "engine/signals/gates.py",
        "class_name": "DeltaMagnitudeGate",
        "status": "active",
        "purpose": (
            "v10.5 gate: blocks trades where |delta_pct| is too small. "
            "Direction agreement is meaningless if price hasn't moved. "
            "CASCADE regime is exempt."
        ),
        "inputs": ["GateContext.delta_pct", "GateContext.regime"],
        "outputs": ["GateResult.data['abs_delta', 'floor', 'regime']"],
        "env_flags": [
            "V10_MIN_DELTA_PCT",
            "V10_TRANSITION_MIN_DELTA (TRANSITION regime override)",
        ],
        "fail_reasons": ["|delta| < floor (regime)"],
        "tables_read": [],
        "tables_written": [],
        "docs": ["docs/V10_3_IMPLEMENTATION_PLAN.md"],
        "notes": "Evidence: 50 trades Apr 9 2026, |delta|<0.01% in TRANSITION = 0W/2L.",
    },
    "taker_flow": {
        "engine": "polymarket",
        "pipeline_position": "G3",
        "file": "engine/signals/gates.py",
        "class_name": "TakerFlowGate",
        "status": "active",
        "purpose": (
            "CoinGlass taker-flow alignment. Adjusts the DUNE confidence "
            "threshold based on whether CG taker-flow aligns with G1's "
            "agreed direction. Sets ctx.cg_threshold_modifier."
        ),
        "inputs": [
            "GateContext.agreed_direction (from G1)",
            "GateContext.cg_snapshot",
        ],
        "outputs": [
            "GateContext.cg_threshold_modifier",
            "GateContext.cg_confirms",
        ],
        "env_flags": ["V10_CG_TAKER_GATE (default false — hard-block when true)"],
        "fail_reasons": ["opposing taker flow (V10_CG_TAKER_GATE=true)"],
        "tables_read": ["ticks_coinglass (upstream into ctx.cg_snapshot)"],
        "tables_written": [],
        "docs": ["docs/V10_3_IMPLEMENTATION_PLAN.md"],
        "notes": (
            "Evidence: 719 trades, aligned = 81.7% WR, opposing = 58.3%. "
            "CA-03 smell: mutates GateContext. Phase 4 of the clean-architect "
            "migration plan fixes this."
        ),
    },
    "cg_confirmation": {
        "engine": "polymarket",
        "pipeline_position": "G4",
        "file": "engine/signals/gates.py",
        "class_name": "CGConfirmationGate",
        "status": "active",
        "purpose": (
            "Counts CoinGlass confirming signals (funding, OI, LS) that "
            "align with the agreed direction. Sets cg_confirms / cg_bonus "
            "for DuneConfidenceGate downstream."
        ),
        "inputs": ["GateContext.agreed_direction", "GateContext.cg_snapshot"],
        "outputs": ["GateContext.cg_confirms", "GateContext.cg_bonus"],
        "env_flags": [],
        "fail_reasons": [],
        "tables_read": ["ticks_coinglass"],
        "tables_written": [],
        "docs": ["docs/V10_3_IMPLEMENTATION_PLAN.md"],
        "notes": "CA-03 smell: mutates GateContext (cg_confirms, cg_bonus).",
    },
    "dune_confidence": {
        "engine": "polymarket",
        "pipeline_position": "G5",
        "file": "engine/signals/gates.py",
        "class_name": "DuneConfidenceGate",
        "status": "active",
        "purpose": (
            "Calibrated probability threshold gate. Calls timesfm v2 service "
            "with a prebuilt V5FeatureBody, receives a calibrated probability, "
            "and compares against a threshold adjusted by cg_threshold_modifier "
            "(G3) and cg_bonus (G4)."
        ),
        "inputs": [
            "GateContext.v5_features",
            "GateContext.cg_threshold_modifier",
            "GateContext.cg_bonus",
            "POST /v2/probability/5m (timesfm service)",
        ],
        "outputs": [
            "GateContext.dune_probability_up",
            "GateContext.dune_direction",
            "GateContext.dune_model_version",
        ],
        "env_flags": [
            "V10_DUNE_MODEL (oak=ELM v3, sequoia=v5.2)",
            "V10_MIN_EVAL_OFFSET (NOTE: MAX here — different from V10_6_MIN_EVAL_OFFSET in G0)",
        ],
        "fail_reasons": ["probability below threshold", "model service error"],
        "tables_read": [
            "ticks_v2_probability (via POST /v2/probability, timesfm service)",
        ],
        "tables_written": [
            "ticks_elm_predictions (via ELMPredictionRecorder background task, PE-06 fix)",
        ],
        "docs": ["docs/SEQUOIA_V5_GO_LIVE_LOG.md (timesfm repo)"],
        "notes": (
            "The V10_MIN_EVAL_OFFSET collision with G0 is the reason DS-01 "
            "introduced the V10_6_ namespace. PE-06 fixed the JSON quoting "
            "bug in the recorder background task."
        ),
    },
    "spread_gate": {
        "engine": "polymarket",
        "pipeline_position": "G6",
        "file": "engine/signals/gates.py",
        "class_name": "SpreadGate",
        "status": "active",
        "purpose": (
            "Rejects windows with wide Polymarket CLOB spreads (low "
            "liquidity / high slippage). Reads gamma_up_price and "
            "gamma_down_price from the context."
        ),
        "inputs": ["GateContext.gamma_up_price", "GateContext.gamma_down_price"],
        "outputs": ["GateResult.data['up_spread', 'down_spread']"],
        "env_flags": ["V10_MAX_SPREAD_CENTS (default 2c)"],
        "fail_reasons": ["spread too wide"],
        "tables_read": ["clob_book_snapshots (via ctx)"],
        "tables_written": [],
        "docs": [],
        "notes": "PE-01 (PR #26) ensured clob_feed writes complete rows for this gate.",
    },
    "dynamic_cap": {
        "engine": "polymarket",
        "pipeline_position": "G7",
        "file": "engine/signals/gates.py",
        "class_name": "DynamicCapGate",
        "status": "active",
        "purpose": (
            "Computes the dynamic entry cap based on VPIN, regime, and "
            "confidence. Last gate — if everything upstream passes, this "
            "produces the cap the executor uses to size the order."
        ),
        "inputs": [
            "GateContext.vpin",
            "GateContext.regime",
            "GateContext.dune_probability_up",
        ],
        "outputs": ["PipelineResult.cap"],
        "env_flags": ["V10_BASE_CAP", "V10_CAP_MULTIPLIERS_*"],
        "fail_reasons": ["cap below minimum"],
        "tables_read": [],
        "tables_written": [],
        "docs": [],
        "notes": "Last gate. If this passes, the strategy places the trade.",
    },
    # ── margin_engine v4 gates (inline in _execute_v4, catalogued for symmetry) ──
    "v4_gate_1_macro_advisory": {
        "engine": "margin_engine",
        "pipeline_position": "v4.1",
        "file": "margin_engine/use_cases/open_position.py",
        "class_name": "_execute_v4 (inline)",
        "status": "active",
        "purpose": (
            "Macro advisory direction gate — rejects trades that fight the "
            "macro lean (e.g. trying to go long during a known bearish "
            "funding regime)."
        ),
        "inputs": ["MacroPort.get_bias()"],
        "outputs": [],
        "env_flags": [],
        "fail_reasons": ["macro_disagree"],
        "tables_read": ["macro_signals"],
        "tables_written": [],
        "docs": [],
        "notes": "14 tests in test_open_position_macro_advisory.py.",
    },
    "v4_gate_9_5_mark_divergence": {
        "engine": "margin_engine",
        "pipeline_position": "v4.9.5",
        "file": "margin_engine/use_cases/open_position.py",
        "class_name": "_execute_v4 (inline, DQ-07 PR #45)",
        "status": "active",
        "purpose": (
            "Defensive mark-divergence gate (DQ-07). Inserted between v4 "
            "gate 9 (balance query) and v4 gate 10 (SL/TP math). Fetches "
            "exchange.get_mark() and rejects the trade if it diverges from "
            "V4Snapshot.last_price by more than v4_max_mark_divergence_bps. "
            "Default 0.0 = no-op; operator flips to activate."
        ),
        "inputs": ["V4Snapshot.last_price", "ExchangePort.get_mark()"],
        "outputs": [],
        "env_flags": ["MARGIN_V4_MAX_MARK_DIVERGENCE_BPS (default 0.0 = off)"],
        "fail_reasons": ["mark_divergence"],
        "tables_read": ["margin_positions", "(live exchange API)"],
        "tables_written": [],
        "docs": ["docs/AUDIT_PROGRESS.md (DQ-05 investigation + DQ-07 ship)"],
        "notes": (
            "Catches stale spot tick / HL basis spike / cross-region latency "
            "anchoring a perp entry at a bad absolute price. 4 new tests, "
            "18/18 margin_engine suite passing."
        ),
    },
}


def list_engines() -> list[str]:
    """Distinct engines in the gates catalog."""
    seen: list[str] = []
    for entry in GATES_CATALOG.values():
        eng = entry.get("engine", "unknown")
        if eng not in seen:
            seen.append(eng)
    return seen


def gates_by_table(table_name: str) -> list[str]:
    """All gate keys that read from a given table. Used by the /schema
    page to cross-reference tables ↔ consuming gates."""
    out: list[str] = []
    for key, entry in GATES_CATALOG.items():
        reads = entry.get("tables_read", [])
        if any(table_name in r for r in reads):
            out.append(key)
    return out


def tables_for_gate(gate_key: str) -> list[str]:
    """All tables a given gate reads from."""
    entry = GATES_CATALOG.get(gate_key, {})
    return list(entry.get("tables_read", []))
