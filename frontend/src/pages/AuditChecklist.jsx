/**
 * AuditChecklist.jsx — Big Audit Session tracking page.
 *
 * Static data page (no API). Renders the audit taxonomy, severity, file:line
 * citations, and live/done status for the deep clean-architect audit
 * covering:
 *   - Data-quality bugs in the Polymarket engine
 *   - V10.6 decision surface rollout
 *   - V4 fusion surface adoption (Polymarket engine side)
 *   - Clean-architect migration (engine/ → margin_engine/ patterns)
 *   - Production error regressions (PR #18 + pre-existing)
 *
 * Update STATUS and PROGRESS_NOTES in-file as tasks land. No backend writes.
 */

import { useMemo, useState } from 'react';

// ─── Theme ────────────────────────────────────────────────────────────────
const T = {
  bg: '#050914',
  card: 'rgba(15, 23, 42, 0.8)',
  cardBorder: 'rgba(51, 65, 85, 1)',
  headerBg: 'rgba(30, 41, 59, 1)',
  text: 'rgba(203, 213, 225, 1)',
  textMuted: 'rgba(100, 116, 139, 1)',
  textDim: 'rgba(71, 85, 105, 1)',
  cyan: '#06b6d4',
  green: '#10b981',
  red: '#ef4444',
  amber: '#f59e0b',
  purple: '#a855f7',
  blue: '#3b82f6',
  white: '#fff',
  mono: "'JetBrains Mono', 'Fira Code', monospace",
};

const SEVERITY_COLOR = {
  CRITICAL: T.red,
  HIGH: T.amber,
  MEDIUM: T.cyan,
  LOW: T.textMuted,
};

const STATUS_COLOR = {
  OPEN: T.red,
  IN_PROGRESS: T.amber,
  DONE: T.green,
  BLOCKED: T.purple,
  INFO: T.cyan,
};

// ─── Audit Data ───────────────────────────────────────────────────────────
// Edit this block as tasks progress. The page re-renders statically.

const SESSION_META = {
  title: 'Clean-Architect Audit · 2026-04-11',
  summary:
    'Deep audit of the Polymarket engine (engine/) against the margin_engine/ reference architecture, the v4 fusion surface on novakash-timesfm-repo, and PR #18 reconciler regressions. Covers data-quality, decision-surface gaps, production errors, v1-v4 observability surfaces, and engine CI/CD automation.',
  startedAt: '2026-04-11',
  progressLog: 'docs/AUDIT_PROGRESS.md',
  repos: [
    { name: 'novakash', branch: 'develop', head: '6816f86' },
    { name: 'novakash-timesfm-repo', branch: 'main', head: 'af51523' },
  ],
};

const CATEGORIES = [
  {
    id: 'data-quality',
    title: 'Data Quality — Price References',
    color: T.red,
    description:
      'Venue-specific price reference bugs. Polymarket engine (engine/) resolves against oracle spot and needs spot-aligned deltas. margin_engine trades Hyperliquid perps and needs perp/mark-aligned deltas. Mixing the two contaminates signals regardless of model quality. Tracked as two tasks: DQ-01 (Polymarket) and DQ-05 (margin_engine).',
  },
  {
    id: 'production-errors',
    title: 'Production Errors · Regressions',
    color: T.amber,
    description:
      'Active error streams in engine.log on Montreal. Includes pre-existing bugs and a regression from PR #18 (reconciler type deduction).',
  },
  {
    id: 'decision-surface',
    title: 'V10.6 Decision Surface',
    color: T.cyan,
    description:
      'The 865-outcome proposal commit c3a6cbd is documentation-only. Thresholds, offset bounds, UP penalty, confidence haircut, proportional sizing are NOT in engine code.',
  },
  {
    id: 'v4-adoption',
    title: 'V4 Fusion Surface · Polymarket Engine',
    color: T.purple,
    description:
      'margin_engine/ uses the 10-gate v4 stack (PR #16). The Polymarket engine (engine/) still does not call v4 at all — grep finds zero references.',
  },
  {
    id: 'clean-architect',
    title: 'Clean-Architect Migration',
    color: T.blue,
    description:
      '3096-line five_min_vpin.py is the single biggest source of architectural debt. margin_engine/ has ports/adapters/use-cases/value-objects — this is the reference to migrate toward.',
  },
  {
    id: 'frontend',
    title: 'Frontend & Observability',
    color: T.green,
    description:
      'V4Panel landed in PR #22 on the /margin page. This audit page ships next. Both observe paper-mode margin_engine; the Polymarket engine has no equivalent surface.',
  },
  {
    id: 'ci-cd',
    title: 'CI/CD · Montreal Automation',
    color: '#f97316',
    description:
      'docs/CI_CD.md (6816f86) explicitly flags engine/ as the only major service without a GitHub Actions deploy workflow. The deploy-macro-observer.yml ~200-line template is the canonical pattern to port. Engine currently relies on Railway git-watcher auto-deploy with no smoke test, no secrets check, no post-deploy health probe, no rollback, and has been observed CRASHED in recent history.',
  },
  {
    id: 'signal-optimization',
    title: 'Signal Optimization · CLOB + Direction Analysis',
    color: '#22d3ee',
    description:
      'Data-driven signal improvements from 897K-sample analysis (2026-04-12). Key finding: DOWN predictions have 76–99% WR; UP predictions have 1.5–53% WR. CLOB data required for full edge — fixed in PR #136. Gates SIG-03/SIG-04 to be implemented next.',
  },
  {
    id: 'config-migration',
    title: 'CFG · DB-backed config migration',
    color: T.cyan,
    description:
      'Full migration of runtime configuration from .env files to a DB-backed store with hot-reload, audit trail, and a /config UI. Tracked in docs/CONFIG_MIGRATION_PLAN.md (CFG-01). Phase 0/1 (CFG-02/03/05) ships read-only schema + read API + read UI. Phase 1 (CFG-04/06) adds writes + admin claim. Phase 2 (CFG-07/08/10) wires per-service loaders + flips SKIP_DB_CONFIG_SYNC. Phase 3 (CFG-11) cleans up legacy .env reads.',
  },
  {
    id: 'ml-training-data',
    title: 'ML Training Data Audit · 2026-04-13',
    color: '#f472b6',
    description:
      'Full inventory of every data asset, database table, prediction surface, signal, and Polymarket outcome available for ML training. Covers v1 (TimesFM), v2 (LightGBM), v3 (composite), v4 (decision surface), gate audit trail, and reconciled outcome labels. Target: 500+ labeled window-outcome pairs per Δ bucket for reliable model retraining. Explored via automated agent across novakash/develop + novakash-timesfm-repo/main.',
  },
  {
    id: 'btc-15m-expansion',
    title: 'BTC 15-Minute Trading Expansion · 2026-04-13',
    color: '#818cf8',
    description:
      'Expand the 5-strategy clean architecture to BTC 15-minute Polymarket markets. 5 critical hardcoded "5m" blockers to fix, 5 new YAML strategy configs (v15m_down/up_asian/up_basic/fusion/gate), timing gates scaled 3x. Most infrastructure already exists (15m feed, model slot, V4 snapshot). Plan: docs/BTC_15M_EXPANSION_PLAN.md. All new strategies start GHOST — promotion only with Billy approval.',
  },
];

const TASKS = [
  // ── data-quality ─────────────────────────────────────────────────────────
  {
    id: 'DQ-01',
    category: 'data-quality',
    severity: 'CRITICAL',
    status: 'DONE',
    title: 'Polymarket engine spot-only consensus vote behind V11_POLY_SPOT_ONLY_CONSENSUS flag',
    files: [
      { path: 'engine/signals/gates.py', line: 281, repo: 'novakash' },
      { path: 'engine/tests/test_source_agreement_spot_only.py', line: 1, repo: 'novakash' },
      { path: 'docs/CHANGELOG-DQ01-POLY-SPOT-ONLY-CONSENSUS.md', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'v11.1 changelog table: Binance shows 16.9% UP / 83.1% DOWN — strong systematic DOWN bias, not a market signal.',
      'Most common disagreement pattern CL=UP TI=DOWN BIN=DOWN (19.6% of all evals) passes as DOWN under the 2/3 rule — biased source sides with lean-DOWN TI against balanced CL.',
      'User flagged on 2026-04-11: "really terrible trade decisions ... we noted a down after 2 consecutive previous up markets".',
      'Polymarket resolves via oracle against BTC/USD spot, so direction signals for this engine must be measured against spot. Binance futures WS is still correct for VPIN / taker-flow / liquidations, but wrong for the consensus vote.',
      'Shipped behind V11_POLY_SPOT_ONLY_CONSENSUS (default false). When set to true, SourceAgreementGate ignores delta_binance and requires CL + TI unanimous agreement. Zero-behaviour-change on merge.',
      'New tests: 16 cases covering default-off preservation, enabled-mode votes, spot-disagree failure, BIN-None tolerance, and case-insensitive flag parsing. 23/23 pass including the sibling DS-01 suite.',
    ],
    fix: 'SHIPPED — SourceAgreementGate.__init__ reads V11_POLY_SPOT_ONLY_CONSENSUS at engine start. Flag false = v11.1 2/3 majority (CL+TI+BIN) unchanged. Flag true = unanimous CL+TI only, Binance dropped from the vote but kept in every other gate. Operator activation: set env var on Montreal host and restart engine. Rollback: unset env var and restart.',
    progressNotes: [
      { date: '2026-04-11', note: 'Correction: initial diagnosis ("drop delta_binance universally") was too broad. The margin_engine trades Hyperliquid perps and wants perp references. The fix is venue-specific: Polymarket engine → spot only, margin_engine → perp/mark only. Split into DQ-01 (Polymarket, this task) and DQ-05 (margin_engine pricing audit).' },
      { date: '2026-04-11', note: 'DONE — shipped PR against develop with SourceAgreementGate two-mode implementation. Default OFF per the DS-01 / DQ-07 feature-flag precedent. 23/23 tests passing (16 new DQ-01 + 7 existing DS-01). Zero scope leak into margin_engine/ or hub/ or frontend/. Activation requires operator to flip V11_POLY_SPOT_ONLY_CONSENSUS=true on /home/novakash/novakash/engine/.env and restart.' },
    ],
  },
  {
    id: 'DQ-02',
    category: 'data-quality',
    severity: 'MEDIUM',
    status: 'INFO',
    title: 'delta_chainlink offset against spot reference',
    files: [
      { path: 'engine/strategies/five_min_vpin.py', line: 422, repo: 'novakash' },
    ],
    evidence: [
      'Chainlink price comes from polygon oracle, but denominator is window.open_price (Binance spot)',
      'Measured avg_chainlink = +0.0454% systematic offset (same period as DQ-01)',
      'Less severe than DQ-01 but same shape: mismatched numerator/denominator',
    ],
    fix: 'Normalise denominator: either use chainlink_open at window start OR fetch spot reference aligned in time. Treat as follow-up to DQ-01.',
  },
  {
    id: 'DQ-03',
    category: 'data-quality',
    severity: 'HIGH',
    status: 'OPEN',
    title: 'price_source_disagreement is logged, not gated',
    files: [
      { path: 'engine/strategies/five_min_vpin.py', line: 440, repo: 'novakash' },
      { path: 'engine/strategies/five_min_vpin.py', line: 493, repo: 'novakash' },
    ],
    evidence: [
      '280+/hr warnings in engine.log but no corresponding hard skip',
      '_price_confidence_flag = "LOW" is set then never read in _execute_trade',
      'No consensus check unless DELTA_PRICE_SOURCE == "consensus" (off by default)',
    ],
    fix: 'Create engine/domain/price_consensus.py with frozen dataclass, agreement_score() method, hard threshold gate in the use case. Skip trade if score < 0.95 (not downgrade).',
  },
  {
    id: 'DQ-04',
    category: 'data-quality',
    severity: 'LOW',
    status: 'INFO',
    title: 'v2_model_version NULL on 106/6408 recent evals (1.7%)',
    files: [
      { path: 'engine/strategies/five_min_vpin.py', line: 2013, repo: 'novakash' },
    ],
    evidence: [
      'Query: SELECT COUNT(*) FROM signal_evaluations WHERE v2_model_version IS NULL',
      'Some code path writes v2_probability_up without the version string',
      'Likely the POST→GET fallback path not stamping version correctly',
    ],
    fix: 'Grep INSERT sites for signal_evaluations, confirm v2_model_version is always set when v2_probability_up is.',
  },
  {
    id: 'DQ-05',
    category: 'data-quality',
    severity: 'HIGH',
    status: 'DONE',
    title: 'margin_engine pricing audit — CLOSED (false alarm, narrower real bug is DQ-06)',
    files: [
      { path: 'margin_engine/use_cases/open_position.py', line: 412, repo: 'novakash' },
      { path: 'margin_engine/domain/value_objects.py', line: 459, repo: 'novakash' },
    ],
    evidence: [
      'Background Agent D (dispatched 2026-04-11 T-13:05 UTC) produced a full READ-ONLY audit of the margin_engine v4 price reference path.',
      'VERDICT: DQ-05 as originally stated is a FALSE ALARM. consensus.reference_price is parsed into the Consensus dataclass at value_objects.py:459 but NEVER READ by any margin_engine use case. The only field of the v4 snapshot that _execute_v4 consumes for pricing is v4.last_price, used once at open_position.py:412 as the denominator of the SL/TP ratio math in _sl_tp_from_quantiles.',
      'v4.last_price IS sourced from Binance SPOT (wss://stream.binance.com:9443/ws/btcusdt@trade — traced through app/main.py:71 → app/assets.py:20 → app/price_feed.py:23 in novakash-timesfm-repo), but the ratio (last_price - p10)/last_price is dimensionless and therefore internally consistent regardless of venue basis.',
      'Realised PnL comes from self._exchange.get_mark() and self._exchange.close_position() — NOT v4 — so v4\'s spot-native last_price never propagates to the PnL numbers.',
      'get_mark() implementations: BinanceMarginAdapter uses Binance SPOT ticker (api.binance.com bookTicker); PaperExchangeAdapter uses bid/ask modelled around _last_price.',
      'No HyperliquidMarginAdapter exchange adapter exists in the tree (verified by ls margin_engine/adapters/exchange/). hyperliquid_price_feed.py is NOT an ExchangePort implementation — it\'s a read-only price source plumbed into PaperExchangeAdapter via the price_getter kwarg.',
    ],
    fix: 'CLOSED as false alarm. Agent D traced every use of v4 snapshot fields in _execute_v4() and manage_positions.py — the engine never treats v4 prices as PnL references. The related real bug (paper+binance branch creates PaperExchangeAdapter with no price_getter so _last_price stays at 80000.0 default) is tracked separately as DQ-06. The defensive mark-divergence gate Agent D recommends is tracked as DQ-07.',
    progressNotes: [
      { date: '2026-04-11', note: 'Background Agent D completed READ-ONLY audit. Full report in docs/AUDIT_PROGRESS.md. Key finding: consensus.reference_price is unused; v4.last_price is spot-native but only used in dimensionless ratio math. The mark-divergence fix path (option b) is tracked as DQ-07. The paper+binance price_getter null bug (not in the original DQ-05 scope) is tracked as DQ-06 — HIGH severity because it may have been polluting paper-mode PnL numbers since PR #16 shipped.' },
    ],
  },
  {
    id: 'DQ-06',
    category: 'data-quality',
    severity: 'HIGH',
    status: 'DONE',
    title: 'margin_engine paper+binance wiring creates PaperExchangeAdapter with no price_getter',
    files: [
      { path: 'margin_engine/main.py', line: 84, repo: 'novakash' },
      { path: 'margin_engine/adapters/exchange/paper.py', line: 137, repo: 'novakash' },
      { path: 'margin_engine/infrastructure/config/settings.py', line: 31, repo: 'novakash' },
      { path: '.github/workflows/deploy-margin-engine.yml', line: 79, repo: 'novakash' },
    ],
    evidence: [
      'main.py:84-97 is the `paper + binance` wiring branch. It constructs PaperExchangeAdapter(starting_balance, spread_bps, fee_rate) with NO price_getter argument.',
      'When price_getter is unset, PaperExchangeAdapter._last_price stays at the 80000.0 default and never updates. Every get_mark() and get_current_price() returns bid/ask around the frozen $80k constant.',
      'main.py:99-123 is the `paper + hyperliquid` branch. IT correctly spins up HyperliquidPriceFeed and wires it via price_getter=price_feed.get_price. This is the ONLY branch where paper-mode PnL is computed against real market moves.',
      'settings.py:31: exchange_venue defaults to "binance", so the paper+binance branch is the DEFAULT path.',
      'deploy-margin-engine.yml:79 sets MARGIN_PAPER_MODE=true on every deploy but does NOT set MARGIN_EXCHANGE_VENUE. CI set_env helper is append-or-update, not remove — so the actual venue depends on whatever was previously in /opt/margin-engine/.env on the host.',
      'If the host .env does not explicitly have MARGIN_EXCHANGE_VENUE=hyperliquid, every paper trade since PR #16 has been pricing against a flat $80,000 — invalidating the entire v4 strategy validation campaign that\'s supposed to prove the engine is safe to flip to live.',
      'Zero real-money risk: MARGIN_PAPER_MODE=true is CI-hardset, and no BinanceMarginAdapter credentials are installed on the host.',
    ],
    fix: 'User clarified 2026-04-11: the correct paper venue is HYPERLIQUID, not binance. The default in settings.py:31 (`binance`) is wrong and the CI workflow does not explicitly set `MARGIN_EXCHANGE_VENUE` so whatever venue was last on the host sticks. Fix in two parts: (1) Update .github/workflows/deploy-margin-engine.yml to set_env MARGIN_EXCHANGE_VENUE=hyperliquid on every deploy — same pattern as the existing MARGIN_PAPER_MODE=true + MARGIN_ENGINE_USE_V4_ACTIONS=true templates (deploy-margin-engine.yml:79-89). Idempotent — if the host already has it, the CI updates in place; if not, it appends. (2) Update settings.py:31 default from "binance" to "hyperliquid" so a future operator running the engine locally without an .env also gets the correct wiring by default. (3) Optional hardening: add a startup assertion in main.py:84 that errors out if paper+binance branch is hit — "paper venue must be hyperliquid, binance paper wiring is broken because PaperExchangeAdapter has no price_getter on that branch". Forces the bug to be loud instead of silent.',
    progressNotes: [
      { date: '2026-04-11', note: 'Discovered by Agent D (DQ-05 investigation) as a higher-priority finding than the nominal DQ-05 hypothesis. User confirmed 2026-04-11: the paper venue should be hyperliquid (not binance). The fix is now just a CI template update to explicitly set MARGIN_EXCHANGE_VENUE=hyperliquid on every deploy, plus a settings.py default flip for local-dev safety. Will be done in the engine-edits worktree before any other trading-engine-touching work.' },
      { date: '2026-04-11', note: 'FIXED in PR #35 (merged at 1c5b047). Three-layer defense: (1) settings.py:31 default flipped binance → hyperliquid. (2) main.py:84 startup RuntimeError raises if paper+binance branch is hit, bypass via MARGIN_ALLOW_BROKEN_PAPER_BINANCE=1. (3) deploy-margin-engine.yml now runs `set_env MARGIN_EXCHANGE_VENUE hyperliquid` on every deploy. CI deploy ran green (42s) and margin_engine .env is now pinned to hyperliquid on the eu-west-2 host.' },
    ],
  },
  {
    id: 'DQ-07',
    category: 'data-quality',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'margin_engine: add defensive mark_divergence gate to v4 pipeline',
    files: [
      { path: 'margin_engine/use_cases/open_position.py', line: 380, repo: 'novakash' },
      { path: 'margin_engine/infrastructure/config/settings.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Agent D recommended as the cleanest incremental fix for the spot-vs-perp concern raised by DQ-05: insert an eleventh gate between the existing gate 9 (balance query) and gate 10 (SL/TP math) that fetches self._exchange.get_mark() and rejects the trade if it diverges from v4.last_price by more than v4_max_mark_divergence_bps (default 20bps).',
      'Catches three failure modes: (a) stale Binance spot WS tick in v4 assembler, (b) Hyperliquid-specific basis spike vs spot, (c) cross-region latency between Montreal v4 and eu-west-2 margin_engine.',
      'Zero behavior change for trades where v4.last_price and exchange mark agree (the common case). Only fires when something is genuinely wrong.',
    ],
    fix: 'New gate class in margin_engine/use_cases/open_position.py::_execute_v4 after balance check. New setting v4_max_mark_divergence_bps (default 20.0) in settings.py. New test case in tests/ constructing a V4Snapshot with last_price=80000 and a stub ExchangePort returning get_current_price=80200 (25bps), asserting trade is rejected with skip_reason="mark_divergence". Deploy with threshold=1000 first to verify no-op, then tighten to 20.',
    progressNotes: [
      { date: '2026-04-11', note: 'Shipped PR #45 — defensive mark_divergence gate (default OFF). 4 new tests + 14 existing tests pass (18/18 margin_engine suite green). Operator flips MARGIN_V4_MAX_MARK_DIVERGENCE_BPS=20 on the host to activate.' },
    ],
  },
  {
    id: 'POLY-SOT',
    category: 'data-quality',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Polymarket CLOB as source-of-truth for manual_trades',
    files: [
      { path: 'migrations/add_manual_trades_sot_columns.sql', line: 1, repo: 'novakash' },
      { path: 'engine/persistence/db_client.py', line: 1162, repo: 'novakash' },
      { path: 'engine/execution/polymarket_client.py', line: 38, repo: 'novakash' },
      { path: 'engine/reconciliation/reconciler.py', line: 1102, repo: 'novakash' },
      { path: 'engine/strategies/orchestrator.py', line: 805, repo: 'novakash' },
      { path: 'engine/tests/test_reconcile_manual_trades_sot.py', line: 1, repo: 'novakash' },
      { path: 'hub/api/v58_monitor.py', line: 1965, repo: 'novakash' },
      { path: 'frontend/src/pages/execution-hq/components/sot.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/execution-hq/components/TradeTicker.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/execution-hq/components/ManualTradePanel.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/execution-hq/ExecutionHQ.jsx', line: 277, repo: 'novakash' },
    ],
    evidence: [
      'User request 2026-04-11: "implement similar database defensibility source of truth stuff we have for the options trading live mode margin engine binance hyperliquid to make sure what happens on exchange is source of truth tags etc make sure our data system has that clearly for polymarket going forward".',
      'Reference pattern: margin_engine/use_cases/manage_positions.py calls self._exchange.get_mark() every tick and uses fill.fill_price from self._exchange.place_market_order(...) — the exchange is the authoritative record. ExchangePort protocol formalises this.',
      'Pre-PR gap: orchestrator.py::_manual_trade_poller (~line 2514) called poly_client.place_order() and immediately wrote status="open" to manual_trades. If place_order timed out, retried, or partially executed, the engine DB happily claimed success while Polymarket may never have booked the trade. There was no SOT field distinguishing engine_recorded_status from polymarket_confirmed_status.',
      'Failure mode the user explicitly flagged: clicking Execute, Polymarket API hiccup, no Telegram alert, no row update — operator has no idea the trade did or didnt land.',
    ],
    fix: 'SHIPPED. (1) Schema: 8 new columns on manual_trades — polymarket_order_id, polymarket_confirmed_status, polymarket_confirmed_fill_price, polymarket_confirmed_size, polymarket_confirmed_at, polymarket_last_verified_at, sot_reconciliation_state, sot_reconciliation_notes. Idempotent ALTER TABLE migration + ensure_manual_trades_sot_columns helper on both engine (DBClient) and hub (ensure_manual_trades_table). (2) PolymarketClient: new typed PolyOrderStatus dataclass + get_order_status_sot(order_id)->Optional[PolyOrderStatus] + list_recent_orders(since,limit) helpers that hide Polymarket lowercase/uppercase status, multiple field names for size_matched, and 404=None semantics. (3) Reconciler: new CLOBReconciler.reconcile_manual_trades_sot() method walks the 5-state decision matrix (agrees | unreconciled | engine_optimistic | polymarket_only | diverged) with 0.5% price tolerance. Fires Telegram alerts on engine_optimistic / polymarket_only / diverged. Per-trade alert dedupe so the same row only screams once per engine restart. (4) Orchestrator: new _sot_reconciler_loop runs every 2 minutes (configurable via SOT_RECONCILER_INTERVAL env var), always-on in both paper and live mode. _manual_trade_poller now persists clob_order_id into the new polymarket_order_id column on every trade. (5) Hub: new GET /api/v58/manual-trades-sot?limit=50 endpoint joins manual_trades with the SOT columns + returns counts dict for the dashboard. (6) Frontend: shared sot.jsx helper renders colour-coded chips (green agrees / yellow unreconciled / red engine_optimistic|diverged|polymarket_only). TradeTicker prepends manual SOT chips to the always-visible scroll strip. ManualTradePanel polls /manual-trades-sot every 30s and shows the last 5 chips in the trade panel. ExecutionHQ fetches the SOT rows in parallel with hqData and passes them to the ticker. (7) Tests: 12 new pytest cases in test_reconcile_manual_trades_sot.py covering every state path (agrees, engine_optimistic, diverged, unreconciled, polymarket_only, no-order-id old/recent, dedupe, paper synthetic ID, fetch error preserves prior state). 23 existing tests still pass.',
    progressNotes: [
      { date: '2026-04-11', note: 'SHIPPED in PR feat/poly-sot-reconciler. Mirrors the margin_engine ExchangePort pattern for Polymarket manual trades. Always-on in paper + live. Within 2 minutes of any engine_optimistic / diverged / polymarket_only event the reconciler fires a Telegram alert and the frontend ticker shows a red chip. Test plan: 12 unit tests covering every decision branch + dedupe + transient-error preservation. Operator activation: nothing — runs automatically on next engine restart, schema migration is idempotent and the hub auto-applies it on its own lifespan startup.' },
    ],
  },
  {
    id: 'POLY-SOT-b',
    category: 'data-quality',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Extend POLY-SOT to automatic engine trades (`trades` table)',
    files: [
      { path: 'migrations/add_trades_sot_columns.sql', line: 1, repo: 'novakash' },
      { path: 'engine/persistence/db_client.py', line: 1515, repo: 'novakash' },
      { path: 'engine/reconciliation/reconciler.py', line: 1125, repo: 'novakash' },
      { path: 'engine/strategies/orchestrator.py', line: 770, repo: 'novakash' },
      { path: 'engine/tests/test_reconcile_trades_sot.py', line: 1, repo: 'novakash' },
      { path: 'hub/api/v58_monitor.py', line: 2110, repo: 'novakash' },
      { path: 'frontend/src/pages/execution-hq/components/TradeTicker.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/execution-hq/ExecutionHQ.jsx', line: 70, repo: 'novakash' },
    ],
    evidence: [
      'POLY-SOT Phase 1 (PR #62) only covered the operator manual_trades table. The engine writes automatic trades to a different table — `trades` — that had no SOT columns and no reconciler pass.',
      'Failure mode: an automatic engine trade that times out / partial-fills / fails on the CLOB would still get status=FILLED in the trades table without anything cross-checking against Polymarket.',
    ],
    fix: 'SHIPPED. (1) Schema: 8 new columns on `trades` mirroring manual_trades — polymarket_order_id, polymarket_confirmed_status, polymarket_confirmed_fill_price, polymarket_confirmed_size, polymarket_confirmed_at, polymarket_last_verified_at, sot_reconciliation_state, sot_reconciliation_notes. New ensure_trades_sot_columns helper on both engine (DBClient) and hub. (2) Reconciler: extracted shared `_compare_to_polymarket` helper that both reconcile_manual_trades_sot and reconcile_trades_sot call — single source of truth for the decision matrix. New `reconcile_trades_sot` walks the trades table via `_TradesPoolDBClient` adapter. (3) Orchestrator: existing `_sot_reconciler_loop` now walks both tables in the same pass (single asyncio task). (4) Telegram dedupe key namespaced by table — `manual_trades:42` vs `trades:42` — so the same numeric ID across tables doesn\'t collide. (5) Hub: new GET /api/v58/trades-sot?limit=50 endpoint returning live automatic-trade rows with their SOT fields. (6) Frontend: TradeTicker.jsx accepts a new `sotRows` prop (in addition to `manualSotRows`); ExecutionHQ.jsx fetches /v58/trades-sot in parallel and passes it through. Same green/yellow/red chip style with an `AUTO` prefix to distinguish from `MANUAL`. (7) Tests: 12 new pytest cases in test_reconcile_trades_sot.py mirroring the manual_trades suite + a cross-table dedupe test verifying manual #42 and trades #42 are independent.',
    progressNotes: [
      { date: '2026-04-11', note: 'SHIPPED in PR #66. Operator activation: nothing — runs automatically on next engine restart, schema migration is idempotent and the hub auto-applies it on its own lifespan startup. Existing 12 POLY-SOT Phase 1 tests still pass unmodified; 12 new POLY-SOT-b tests + 3 POLY-SOT-c backfill tests also pass.' },
    ],
  },
  {
    id: 'POLY-SOT-c',
    category: 'data-quality',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'One-shot historical backfill for SOT reconciliation',
    files: [
      { path: 'engine/scripts/backfill_sot_reconciliation.py', line: 1, repo: 'novakash' },
      { path: 'engine/tests/test_reconcile_trades_sot.py', line: 540, repo: 'novakash' },
    ],
    evidence: [
      'The forward POLY-SOT reconciler only stamps rows written after its merge timestamp. Every historical manual_trades row written before PR #62, and every historical trades row written before this PR, has sot_reconciliation_state = NULL. Without a backfill the dashboard would show "unreconciled" forever for legacy rows.',
    ],
    fix: 'SHIPPED. New one-shot script engine/scripts/backfill_sot_reconciliation.py walks both tables, calls poly_client.get_order_status_sot() for rows that have an order ID, and tags each row using the same `_compare_to_polymarket` helper as the forward reconciler. Rows older than 24h with no order ID get a new terminal state `no_order_id`. Younger rows are skipped so the forward reconciler can pick them up. Rate-limited (100ms between calls) to avoid hammering the CLOB on a catch-up burst. Dry-run mode (`--dry-run`) prints decisions without writing. Idempotent — re-runs are no-ops because the WHERE clause filters on `sot_reconciliation_state IS NULL`. Exit codes: 0 success / 1 fatal / 2 partial. Operator command: `python3 scripts/backfill_sot_reconciliation.py --table both --dry-run` then without --dry-run after review. Runs on the Montreal box (geo restriction). 3 new pytest cases verify the row-decision logic.',
    progressNotes: [
      { date: '2026-04-11', note: 'SHIPPED in PR #66. Deployment checklist item added to docs/AUDIT_PROGRESS.md "Next up" section so the operator remembers to run the backfill on the Montreal box after merge. Run command: `cd /home/novakash/novakash/engine && python3 scripts/backfill_sot_reconciliation.py --table both --dry-run` → review → rerun without --dry-run.' },
    ],
  },

  // ── production-errors ───────────────────────────────────────────────────
  {
    id: 'PE-01',
    category: 'production-errors',
    severity: 'HIGH',
    status: 'DONE',
    title: 'clob_feed.write_error — 1090/hour since 2026-04-07',
    files: [
      { path: 'engine/data/feeds/clob_feed.py', line: 130, repo: 'novakash' },
    ],
    evidence: [
      'Error: "the server expects 10 arguments for this query, 11 were passed"',
      'clob_book_snapshots INSERT column list was missing `ts` — 11 columns vs 11 Python args while VALUES had only $1..$10',
      'clob_book_snapshots row count was 0 for 4 days (silent since 2026-04-07)',
      'Bug introduced in commit e3d026c 2026-04-07 — not a PR #18 regression',
      'Primary ticks_clob INSERT (lines 112-127) was always fine; only comprehensive snapshot table was dead',
    ],
    fix: 'Add `ts,` at the top of column list, add `$11` to VALUES clause. Now matches the 12-column / 12-value / 11-param pattern of the ticks_clob INSERT above.',
    progressNotes: [
      { date: '2026-04-11', note: 'Fixed in PR #26 rev-2. 15-line diff in engine/data/feeds/clob_feed.py with inline comment tagging PE-01. Needs deploy-engine workflow + Montreal restart to verify clob_book_snapshots row count > 0.' },
      { date: '2026-04-11', note: 'VERIFIED LIVE. After Montreal host reboot (INC-01) + git pull develop + scripts/restart_engine.sh, engine has been running for 7 min with clob_feed.write_error count = 0. Previous error rate was 1090/hour. PE-01 fix is live in production.' },
    ],
  },
  {
    id: 'PE-02',
    category: 'production-errors',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'reconciler.resolve_db_error — PR #18 regression (4/hour)',
    files: [
      { path: 'engine/reconciliation/reconciler.py', line: 765, repo: 'novakash' },
    ],
    evidence: [
      'Error: "inconsistent types deduced for parameter $1 — text versus character varying"',
      'Bidirectional prefix-match LIKE used $1 and $2 instead of $1::text in both sides',
      'Working pattern at lines 185-186 uses single parameter with explicit cast',
      'Each failure was a silent reconciler match miss for the runtime fast-path',
    ],
    fix: 'Replaced `LIKE $1 || \'%\' OR $2 LIKE metadata->>\'token_id\' || \'%\'` with `LIKE $1::text || \'%\' OR $1::text LIKE metadata->>\'token_id\' || \'%\'`. Single parameter, explicit cast, matches the working pattern at lines 185-186.',
    progressNotes: [
      { date: '2026-04-11', note: 'Fixed in PR #26 rev-2 alongside PE-01. Needs engine restart via scripts/restart_engine.sh to clear the error stream. Will be verified automatically once CI-01 lands and every deploy runs a post-deploy error-signature probe.' },
      { date: '2026-04-11', note: 'VERIFIED LIVE. After the same restart that verified PE-01, reconciler.resolve_db_error count = 0 across the last 5000 log lines. Previous error rate was 4/hour. PE-02 fix is live in production.' },
    ],
  },
  {
    id: 'PE-03',
    category: 'production-errors',
    severity: 'LOW',
    status: 'INFO',
    title: 'reconciler.orphan_fills_error — transient Polymarket API noise',
    files: [
      { path: 'engine/reconciliation/reconciler.py', line: 456, repo: 'novakash' },
    ],
    evidence: [
      'PolyApiException[status_code=None, error_message=Request exception!]',
      '1/hour, handler already catches and continues with fills=[]',
      'Not a bug; log noise from Polymarket API connection drops',
    ],
    fix: 'No code change. Consider downgrading log level from warning to debug.',
  },
  {
    id: 'PE-05',
    category: 'production-errors',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'reconciler.resolve_db_error STILL firing — second CASE WHEN $1 type ambiguity',
    files: [
      { path: 'engine/reconciliation/reconciler.py', line: 824, repo: 'novakash' },
    ],
    evidence: [
      'After PR #26 (PE-02 fix) and the 12:22 UTC engine restart, the reconciler.resolve_db_error signature came back at 2 errors in 20 minutes (6/hour)',
      'Investigation revealed my PE-02 fix addressed the prefix-match fallback (lines 765-776) but MISSED a second instance of the same bug class in the UPDATE query at lines 824-834',
      'The UPDATE used `$1` in two incompatible contexts: assignment (`SET outcome = $1` — column type deduced) AND comparison inside a CASE WHEN (`CASE WHEN $1 = \'WIN\'` — literal-comparison deduces text). If the `outcome` column is declared varchar, asyncpg fails with "inconsistent types deduced for parameter $1 — text versus character varying"',
      'Observed in production at 2026-04-11 12:37:50 UTC and 12:42:36 UTC with condition_ids 0x6a79489fc86780cf52 and 0xd8eb483a4613119414',
      'Same symptom as PE-02: a silent reconciler match miss for the runtime fast-path — the trade resolves via the EXACT match at line 745 but then fails to get UPDATE-tagged with outcome/pnl',
      'PE-02 verified the fix was in place on the host (`\\$1::text || \'%\'` at lines 772-773) but this second instance was in a different query',
    ],
    fix: 'Drop the inline CASE WHEN, use the pre-computed `status` variable from line 720 as a fourth parameter. The resulting UPDATE has each placeholder in exactly one type context: `SET outcome = $1, pnl_usd = $2, resolved_at = NOW(), status = $3 WHERE id = $4 AND outcome IS NULL`. Matches the working pattern already used at line 613 and line 958 elsewhere in the same file.',
    progressNotes: [
      { date: '2026-04-11', note: 'Found during the final engine health spot-check after PR #28 merge. The `status` variable is already pre-computed at line 720 as `"RESOLVED_WIN" if outcome == "WIN" else "RESOLVED_LOSS"`, so the CASE WHEN was redundant anyway. Fixed in PR #29, 15-line diff including inline `PE-05 fix:` comment block explaining the type-deduction bug. Still needs Montreal git pull + engine restart to verify 0 errors in production.' },
      { date: '2026-04-11', note: 'VERIFIED LIVE. Post-PR#29 deploy at 12:49:31 UTC, engine ran for 13+ minutes before next health check showed reconciler.resolve_db_error=0 across ~10k log lines. Combined with PE-01 (clob_feed.write_error=0) and PE-02 (the prefix-match variant also staying at 0), all three fixes from PR #26 and PR #29 are confirmed clean in production.' },
    ],
  },
  {
    id: 'PE-06',
    category: 'production-errors',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'Sequoia v5.2 prediction recorder — invalid JSON quoting (renamed from PE-04)',
    files: [
      { path: 'engine/data/feeds/elm_prediction_recorder.py', line: 129, repo: 'novakash' },
      { path: 'engine/tests/test_elm_prediction_recorder.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Root cause: _record_sweep serialised feature_freshness_ms with str(result.get("feature_freshness_ms", {})) which emits Python repr with single quotes. Postgres JSONB parser rejects with "invalid input syntax for type json — Token \' is invalid.".',
      'Impact: the recorder\'s executemany was wrapped in a try/except that caught the write error and logged elm_recorder.write_error, silently dropping 16 rows (4 assets × 4 deltas) every 30 seconds. Started firing at 2026-04-11 12:29:46 UTC.',
      'NOT a trading bug (recorder is observability-only) but BIASED the V10.6 865-outcome backtest evidence base by dropping predictions in an unknown pattern.',
      'The current model family is Sequoia v5.2 — "ELM" is legacy naming kept in file/class/log names, renaming tracked separately as SQ-01.',
      'Bug-class audit: grepped all engine/ for str(dict)/repr(dict) in SQL contexts — only the one instance found. No additional variants. Other JSONB writers in engine/persistence/db_client.py already use json.dumps() + $N::jsonb correctly.',
    ],
    fix: 'PR #30 by background Agent A: replace str(result.get("feature_freshness_ms", {})) with json.dumps(freshness) where freshness = result.get("feature_freshness_ms") or {} (defensively handles None). SQL placeholder $7::jsonb was already correct. Pattern now matches engine/persistence/db_client.py conventions. Added test_elm_prediction_recorder.py with 5 cases: valid-JSON round-trip, single-quote-in-value, nested single-quoted strings, missing-field default, executemany batch sanity. Tests fail against unfixed code (3/5 raise json.JSONDecodeError matching the Postgres error) and pass against the fix.',
    progressNotes: [
      { date: '2026-04-11', note: 'Renamed from PE-04 to PE-06 to match the new Sequoia v5.2 naming context. Dispatched as a background agent task (Agent A) with explicit instruction to grep the whole engine/ tree for sibling bug-class instances — a lesson learned from PE-02/PE-05 where I fixed one instance and missed another. Agent found no siblings. PR #30 merged at c9f341b. Deployment still pending Montreal git pull + engine restart.' },
    ],
  },
  {
    id: 'INC-01',
    category: 'production-errors',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Montreal host network outage → engine crash (2026-04-11 11:05–12:11 UTC)',
    files: [
      { path: '/home/novakash/engine-20260411-122257.log', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Engine stopped responding at 12:00:54 UTC after 55 min of cascading network failures',
      'Symptoms (11:05 → 12:00): tiingo_feed.poll_error (empty), chainlink.poll_error (empty), coinglass.poll_error (empty), v2.probability.timeout, binance_ws.disconnected "timed out during opening handshake", polymarket_ws.disconnected same',
      'Smoking gun at 11:54:06 UTC: chainlink_feed.asset_error "Temporary failure in name resolution" for polygon-bor-rpc.publicnode.com — DNS broken on the host itself',
      'Also: db.write_window_snapshot_failed → even asyncpg DB writes failing',
      'Post-crash state: process dead, sshd banner exchange timing out (host networking wedged), SSM agent unregistered, EC2 status checks "ok" (kernel healthy, userspace broken)',
      'Recovery: aws ec2 reboot-instances i-0785ed930423ae9fd at 12:11:11 UTC. Rebooted in ~1 min, sshd responsive at 12:17 UTC, engine restarted cleanly at 12:23 UTC via git pull develop + scripts/restart_engine.sh',
      'Root cause: unclear whether this was a networking blip at the AWS edge or an internal runaway process exhausting FDs/sockets on the t3.medium',
      'Current state: engine back trading, PE-01 + PE-02 fixes verified live (0 errors each)',
    ],
    fix: 'CI-01 error-signature gate (PR #27) will catch the next similar event automatically. Additional hardening candidates for a future task: (a) add a simple systemd unit wrapping scripts/restart_engine.sh so crash recovery is automatic, (b) enable SSM agent + the Systems Manager instance role so we have a second remote path when sshd wedges, (c) CloudWatch alarm on engine.log write silence >120s.',
    progressNotes: [
      { date: '2026-04-11', note: 'Engine back trading at 12:23 UTC. 7 minutes of clean runtime confirmed via tail of engine.log: clob_feed.prices firing every 2-3s, chainlink_feed.written 4 rows every 5s, window.change at 12:30:00 (new 5m window 1775910600 opened at $72861.85), window.monitoring_started for BTC-1775910600. PE-01/PE-02 error-signature gates both at 0.' },
    ],
  },

  // ── decision-surface ────────────────────────────────────────────────────
  {
    id: 'DS-01',
    category: 'decision-surface',
    severity: 'HIGH',
    status: 'DONE',
    title: 'V10.6 decision surface is docs-only, not deployed',
    files: [
      { path: 'docs/V10_6_DECISION_SURFACE_PROPOSAL.md', line: 1, repo: 'novakash-timesfm-repo' },
      { path: 'engine/signals/gates.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Commit c3a6cbd is documentation only (0 code changes to engine/)',
      'Proposal defines grid: V10_MIN_EVAL_OFFSET=90, V10_MAX_EVAL_OFFSET=180, V10_NORMAL_MIN_P=0.68, V10_CASCADE_MIN_P=0.65, V10_TRANSITION_MIN_P=0.68',
      'Proposal defines UP_PENALTY=0.03, NORMAL_HAIRCUT=0.04, CASCADE_HAIRCUT=0.04, CAP_SCALE_BASE=0.25 → 1.0 at 0.85',
      'Engine currently uses v10.5-era DUNE tier taxonomy (DECISIVE/HIGH/MODERATE/SPIKE), not the regime×offset grid',
      'trade_bible.config_version stuck at `v10` for 173/173 trades last 48h',
    ],
    fix: 'Implement V10.6 as new gates in engine/signals/gates.py: EvalOffsetBoundsGate, PerRegimeMinPGate. Apply UP_PENALTY and CONFIDENCE_HAIRCUT in probability check. Replace _execute_trade sizing with proportional scaling per §3.5 of proposal doc.',
    progressNotes: [
      { date: '2026-04-11', note: 'Shipped PR #33 — V10.6 EvalOffsetBoundsGate (default OFF). 7 new tests pass. Namespaced under V10_6_ENABLED / V10_6_MIN_EVAL_OFFSET / V10_6_MAX_EVAL_OFFSET to avoid collision with existing V10_MIN_EVAL_OFFSET in DuneConfidenceGate.' },
    ],
  },
  {
    id: 'DS-02',
    category: 'decision-surface',
    severity: 'MEDIUM',
    status: 'INFO',
    title: '73% WR target: audit expected vs actual',
    files: [
      { path: 'docs/V10_6_DECISION_SURFACE_PROPOSAL.md', line: 27, repo: 'novakash-timesfm-repo' },
    ],
    evidence: [
      'Proposal references historical "morning session 73% WR" but doc targets break-even (~66% WR on filtered set)',
      '865 resolved BTC outcomes + 85805 Sequoia v4 predictions cited',
      'NORMAL T-120 sweet spot: 62.82% accuracy, skill +6.32pp',
      'Catastrophe: 80% of trades at 0.65-0.70 confidence bucket (158/198), ROI -12.07%',
      'T-180-240 is the killer: 47.62% WR, -33.96% ROI',
    ],
    fix: 'Not a fix, context. Phase-2 rollout goal is to get trades into [90,180] offset band first, then target 66% WR.',
  },
  {
    id: 'DS-03',
    category: 'decision-surface',
    severity: 'MEDIUM',
    status: 'OPEN',
    title: 'Counter-factual: V10.6 on last 10 trades = 50% WR',
    files: [],
    evidence: [
      'V10.6 offset bounds [90,180] would have SKIPPED 4 of last 10 trades (3 losses, 1 win)',
      'Of the 6 it would have taken: 3W/3L = 50% WR',
      'Still below 71.4% breakeven at current odds',
      'Gap is not solvable by V10.6 alone — needs DQ-01 price fix first',
    ],
    fix: 'Ship DQ-01 + PE-01 + PE-02 FIRST. Then ship V10.6. Then re-measure. Do not ship V10.6 on top of corrupted inputs.',
  },

  // ── v4-adoption ─────────────────────────────────────────────────────────
  {
    id: 'V4-01',
    category: 'v4-adoption',
    severity: 'HIGH',
    status: 'OPEN',
    title: 'Polymarket engine does not consume v4 fusion surface',
    files: [
      { path: 'engine/strategies/five_min_vpin.py', line: 1, repo: 'novakash' },
      { path: 'margin_engine/use_cases/open_position.py', line: 158, repo: 'novakash' },
    ],
    evidence: [
      'grep -r "v4/snapshot|v4_snapshot|V4SnapshotPort" engine/ → ZERO matches',
      'margin_engine has a 10-gate v4 stack wired in open_position.py:158 (PR #16)',
      'CI templates MARGIN_ENGINE_USE_V4_ACTIONS=true into /opt/margin-engine/.env on every deploy',
      'The Polymarket engine is the one losing money, and it is NOT using v4',
    ],
    fix: 'Port the v4 gate dispatcher pattern from margin_engine/use_cases/open_position.py into engine/strategies/five_min_vpin.py (or better, into a new engine/use_cases/open_five_min_position.py). Add V4SnapshotPort adapter. Gate behind ENGINE_POLY_USE_V4_ACTIONS.',
  },
  {
    id: 'V4-02',
    category: 'v4-adoption',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'V4 gates wired into margin_engine (PR #16)',
    files: [
      { path: 'margin_engine/use_cases/open_position.py', line: 158, repo: 'novakash' },
      { path: 'margin_engine/use_cases/manage_positions.py', line: 125, repo: 'novakash' },
    ],
    evidence: [
      'Commit 195ae12 — 10-gate stack: consensus.safe_to_trade → macro.direction_gate → event guard → regime → conviction → fee wall → balance → quantile SL/TP → reward/risk',
      'bet_fraction scaled by macro.size_modifier (0.5-1.5x)',
      'SL/TP derived from p10/p90 quantiles with 20/30bp floors',
      '6 new exit reasons: PROBABILITY_REVERSAL, REGIME_DETERIORATED, CONSENSUS_FAIL, MACRO_GATE_FLIP, EVENT_GUARD, CASCADE_EXHAUSTED',
      'Position entity has 8 v4 audit fields stamped at entry',
      'Folded re-prediction continuation (v4 path re-walks gates on expiry instead of closing)',
    ],
    fix: 'Already shipped. Monitor paper-mode performance via V4Panel on /margin page.',
  },
  {
    id: 'V4-03',
    category: 'v4-adoption',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'V4 consensus now has 6 real sources (Phase 3b)',
    files: [
      { path: 'app/v2_tiingo_poller.py', line: 1, repo: 'novakash-timesfm-repo' },
      { path: 'app/v2_chainlink_poller.py', line: 1, repo: 'novakash-timesfm-repo' },
      { path: 'app/v2_coinglass_poller.py', line: 1, repo: 'novakash-timesfm-repo' },
    ],
    evidence: [
      'Commit 034b058 added V2TiingoPoller and V2ChainlinkPoller',
      'CoinGlass upgraded to v4 API (7 endpoints polled concurrently at 10s)',
      '/v4/consensus now sources from: Binance WS, Coinbase, Kraken, CoinGlass, Tiingo, Chainlink Polygon',
      'Previous "reserved" fields for Tiingo/Chainlink are now live',
    ],
    fix: 'Already shipped. V4 consensus is more trustworthy than the legacy engine\'s local 3-source agreement check.',
  },

  // ── clean-architect ─────────────────────────────────────────────────────
  {
    id: 'CA-01',
    category: 'clean-architect',
    severity: 'CRITICAL',
    status: 'IN_PROGRESS',
    title: 'five_min_vpin.py is a 3096-line god class — Phases 0-3 shipped, Phase 4+ remaining',
    files: [
      { path: 'engine/strategies/five_min_vpin.py', line: 1, repo: 'novakash' },
      { path: 'engine/domain/ports.py', line: 1, repo: 'novakash' },
      { path: 'engine/domain/value_objects.py', line: 1, repo: 'novakash' },
      { path: 'engine/use_cases/evaluate_window.py', line: 1, repo: 'novakash' },
      { path: 'engine/use_cases/execute_manual_trade.py', line: 1, repo: 'novakash' },
      { path: 'engine/use_cases/publish_heartbeat.py', line: 1, repo: 'novakash' },
      { path: 'engine/use_cases/reconcile_positions.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      '3096 LOC, 28 methods, 328 self._ fields',
      '_evaluate_window() alone is ~1500 lines',
      'Embeds Tiingo REST, Chainlink RPC, CoinGlass, DUNE, FOK ladder, Telegram alerts',
      '13-parameter constructor (7 optional None defaults)',
      'No tests for the strategy itself (too coupled to mock)',
      'Phases 0-3 now shipped: ports.py (8 protocols), value_objects.py (22 VOs with validation), 14 adapter shims, 4 use cases extracted with 49 total tests.',
    ],
    fix: 'Remaining: Phase 4 wiring (swap god-class methods for use-case calls behind feature flag), Phase 5+ full integration. Target <500 LOC for the orchestration class.',
    progressNotes: [
      { date: '2026-04-11', note: 'Plan doc shipped PR #51 — docs/CLEAN_ARCHITECT_MIGRATION_PLAN.md (1159 lines, 9 phases). Phase 0 (ports.py scaffold) is queued in the SPARTA doc Appendix D future task queue for next agent pickup.' },
      { date: '2026-04-11', note: 'PR #75 — Phase 0 ports.py scaffold shipped.' },
      { date: '2026-04-11', note: 'PR #80 — db_client.py split into 4 per-aggregate repos.' },
      { date: '2026-04-11', note: 'PR #83 — persistence adapters wired to domain port interfaces.' },
      { date: '2026-04-11', note: 'PR #92 — Phase 2 adapter shims for all remaining ports (14 files).' },
      { date: '2026-04-11', note: 'PR #99 — Phase 1 fill 22 value object stubs with real fields and validation.' },
      { date: '2026-04-11', note: 'PR #101 — 3 remaining use cases extracted (execute_manual_trade, publish_heartbeat, reconcile_positions) + 4 new ports + VO field updates. 36 tests.' },
      { date: '2026-04-11', note: 'PR #103 — Phase 3 EvaluateWindowUseCase extraction (flagged off, 13 tests). Core _evaluate_window logic now in engine/use_cases/evaluate_window.py behind feature flag. Biggest single shrink of the god class.' },
      { date: '2026-04-13', note: 'Phase 4+ superseded by Strategy Engine v2 (CA-07). Config-first registry eliminates need for further god-class extraction. Design spec: docs/superpowers/specs/2026-04-13-strategy-engine-v2-design.md' },
    ],
  },
  {
    id: 'CA-02',
    category: 'clean-architect',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Ports/adapters layer in engine/ — fully wired',
    files: [
      { path: 'engine/domain/ports.py', line: 1, repo: 'novakash' },
      { path: 'engine/adapters/persistence/pg_window_repo.py', line: 1, repo: 'novakash' },
      { path: 'engine/adapters/polymarket/live_client.py', line: 1, repo: 'novakash' },
      { path: 'engine/adapters/polymarket/paper_client.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'engine/domain/ports.py now defines 8+ port protocols (PR #75)',
      'PR #83 wired persistence adapters to domain port interfaces',
      'PR #87 extracted TiingoRestAdapter (removed hardcoded API key — security fix)',
      'PR #92 shipped adapter shims for all remaining ports (14 files)',
      'PR #93 split polymarket_client.py into paper/live adapter classes',
    ],
    fix: 'SHIPPED across PRs #75, #83, #87, #92, #93. engine/ now has a full ports/adapters layer matching the margin_engine/ pattern.',
    progressNotes: [
      { date: '2026-04-11', note: 'Plan doc shipped PR #51 — specific to the 8 port protocols defined in §4.' },
      { date: '2026-04-11', note: 'PR #75 — Phase 0 ports.py scaffold with 8 protocol definitions.' },
      { date: '2026-04-11', note: 'PR #83 — persistence adapters wired to domain port interfaces.' },
      { date: '2026-04-11', note: 'PR #87 — security fix: removed hardcoded Tiingo API key, extracted TiingoRestAdapter (CA-02).' },
      { date: '2026-04-11', note: 'PR #92 — Phase 2 adapter shims for all remaining ports (14 files). All ports now have concrete adapter implementations.' },
      { date: '2026-04-11', note: 'PR #93 — polymarket_client.py split into paper/live adapter classes in engine/adapters/polymarket/.' },
    ],
  },
  {
    id: 'CA-03',
    category: 'clean-architect',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'Gate context is immutable — delta fold pipeline shipped',
    files: [
      { path: 'engine/signals/gates.py', line: 45, repo: 'novakash' },
      { path: 'engine/tests/unit/signals/test_gate_pipeline_immutable.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Previously: each gate mutated ctx.cg_confirms, ctx.cg_modifier, ctx.cg_bonus in place',
      'DuneConfidenceGate read modifiers set by TakerFlowGate — implicit ordering dependency',
      'Now: GateContext is frozen. Each gate returns a GateResult with deltas. Use case composes results via fold pipeline.',
    ],
    fix: 'SHIPPED in PR #95. Immutable GateContext with delta fold pipeline. 229-line test suite verifies pipeline composition is order-independent for commutative deltas.',
    progressNotes: [
      { date: '2026-04-11', note: 'Plan doc shipped PR #51 — mutable GateContext mutations documented in Phase 4.' },
      { date: '2026-04-11', note: 'PR #95 — immutable GateContext with delta fold pipeline. Tests in engine/tests/unit/signals/test_gate_pipeline_immutable.py (229 lines). Zero behaviour change — gates produce identical outputs, just via immutable composition instead of mutation.' },
    ],
  },
  {
    id: 'CA-04',
    category: 'clean-architect',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'Window dedup state — single-owner WindowStateRepository shipped',
    files: [
      { path: 'engine/adapters/persistence/pg_window_repo.py', line: 1, repo: 'novakash' },
      { path: 'engine/domain/ports.py', line: 1, repo: 'novakash' },
      { path: 'engine/tests/test_pg_window_state_repo.py', line: 1, repo: 'novakash' },
      { path: 'migrations/add_window_states_table.sql', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Previously: strategy owned _traded_windows (in-memory set), reconciler owned _known_resolved (separate tracking)',
      'No invariant guaranteeing both stayed consistent',
      'Now: WindowStateRepositoryPort defines the single-owner contract. PgWindowStateRepo implements it with a dedicated window_states table.',
    ],
    fix: 'SHIPPED in PR #100. WindowStateRepository as single owner of traded/resolved state. DB-backed via migrations/add_window_states_table.sql. Both strategy and reconciler depend on the port. 82-line test suite.',
    progressNotes: [
      { date: '2026-04-11', note: 'Plan doc shipped PR #51 — WindowStateRepository extraction documented in Phase 5.' },
      { date: '2026-04-11', note: 'PR #100 — Phase 5 WindowStateRepository implemented. PgWindowStateRepo in engine/adapters/persistence/pg_window_repo.py. New window_states table (idempotent migration). Tests in engine/tests/test_pg_window_state_repo.py. Strategy + reconciler now share a single source of truth for traded/resolved window state.' },
    ],
  },
  {
    id: 'SQ-01',
    category: 'clean-architect',
    severity: 'LOW',
    status: 'OPEN',
    title: 'Sequoia v5.2 rename cleanup — 4-PR rollout plan drafted by Agent E',
    files: [
      { path: 'engine/data/feeds/elm_prediction_recorder.py', line: 1, repo: 'novakash' },
      { path: 'engine/strategies/orchestrator.py', line: 665, repo: 'novakash' },
      { path: 'engine/tests/test_elm_prediction_recorder.py', line: 1, repo: 'novakash' },
      { path: 'app/v3_composite_scorer.py', line: 65, repo: 'novakash-timesfm-repo' },
      { path: 'tasks/todo.md', line: 78, repo: 'novakash' },
    ],
    evidence: [
      'Agent E (READ-ONLY) completed a full audit: ~43 ELM references across 9 files in novakash/engine, plus 4 DB column refs in novakash/margin_engine, 2 frontend files, and 6 files in novakash-timesfm-repo on `main`.',
      'Recommendation: go UNBRANDED not Sequoia* — the engine convention is already versioned+unbranded (timesfm_v2_client.py, v2_feature_body.py), and the model family has already turned over 5 times: OAK → CEDAR → DUNE → ELM → SEQUOIA v4 → v5. Any brand name will go stale the same way ELM did.',
      'Red flag: ticks_elm_predictions table is created lazily by _ensure_table() at runtime with a hardcoded DDL string. Renaming the DDL string creates a second orphaned table. PR 1 must NOT touch the table name.',
      'Red flag: "elm" is a signal key in the JSON response of novakash-timesfm-repo/app/v3_composite_scorer.py. Any wire-format rename requires the dual-emit pattern already sketched in tasks/todo.md:78-82 — which is a cross-repo, cross-branch, cross-database migration. Deferred.',
      'Red flag: the CI-01 error-signature gate in .github/workflows/deploy-engine.yml does NOT grep for elm_recorder.write_error. So there is no CI gate protecting PE-06 from regression. Agent E flagged this and recommended adding prediction_recorder.write_error (new) + elm_recorder.write_error (transition) to the check list. Tracked separately as CI-02.',
      'Agent E recommends UNBRANDED target names: class PredictionRecorder (not SequoiaPredictionRecorder), file prediction_recorder.py, kwarg model_client, log component "prediction_recorder".',
    ],
    fix: 'Four-PR rollout plan from Agent E: PR 1 (LOW risk, ~60 line changes): cosmetic rename of class/file/kwargs/comments in novakash engine, keeping DB + log events + signal keys intact. PR 2 (MEDIUM risk, operator coordination): structured log event rename (elm_recorder.* → prediction_recorder.*), bundled with CI-01 gate update (CI-02 covers that). PR 3 (HIGH effort, cross-repo): dual-emit signal key "elm" → "prediction" across novakash + novakash-timesfm-repo + frontend + margin_engine. PR 4: DB column renames — DEFER INDEFINITELY, zero user-visible value and requires downtime. Full file list with per-file rename instructions in docs/AUDIT_PROGRESS.md Agent E report.',
    progressNotes: [
      { date: '2026-04-11', note: 'Agent E READ-ONLY investigation complete. Full 4-PR rollout plan with file:line citations + per-category risk assessment integrated into docs/AUDIT_PROGRESS.md. Status flipped back from IN_PROGRESS to OPEN because the actual rename work hasn\'t started yet — it\'s now planned, but scheduled behind higher-priority engine edits (DQ-06, DS-01 activation flag, DQ-01 rollout). PR 1 (low-risk cosmetic) can ship any time; PR 2 requires CI-02 first; PR 3 is a multi-week coordination; PR 4 should never happen.' },
      { date: '2026-04-11', note: 'PR 1 of 4 (cosmetic rename) queued in SPARTA doc Appendix D future task queue. CI-02 (PR #49) added zero-tolerance signatures on elm_recorder.write_error + .query_error so any rename of those log events will fail the deploy gate — must be done in the same commit as the CI signature update.' }
    ],
  },
  {
    id: 'CI-02',
    category: 'ci-cd',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'Add prediction/elm recorder signatures to CI-01 error-signature gate',
    files: [
      { path: '.github/workflows/deploy-engine.yml', line: 240, repo: 'novakash' },
    ],
    evidence: [
      'Agent E flagged that the CI-01 error-signature gate I shipped in PR #28 (deploy-engine.yml step 12) does NOT grep for elm_recorder.write_error or prediction_recorder.write_error.',
      'This means there is currently NO CI protection against PE-06 regressions. If a future engine commit reintroduces the JSON quoting bug or any similar observability-path failure, the deploy workflow will pass silently.',
      'The gap matters particularly for SQ-01 PR 2 (log event rename) — if that lands without updating this gate at the same time, the rename will succeed but the gate will keep grepping for the old event name and never fire even when the new event name has a bug.',
    ],
    fix: 'Add to the check_signature list in deploy-engine.yml around line 240: (a) `check_signature "elm_recorder.write_error" 0` as a transition check now, (b) `check_signature "prediction_recorder.write_error" 0` ahead of SQ-01 PR 2, (c) remove the elm_recorder entry after SQ-01 PR 2 has been stable for 1 week. Small surgical PR — 4-line diff.',
    progressNotes: [
      { date: '2026-04-11', note: 'Shipped PR #49 — extended deploy-engine.yml error-signature gate to cover PE-06 signatures (elm_recorder.write_error + elm_recorder.query_error, both threshold 0). Closes the observability gap that let PE-06 fire 16x/30s for days undetected.' },
    ],
  },
  {
    id: 'CI-03',
    category: 'ci-cd',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Engine auto-deploy to Montreal via GitHub Actions (36 secrets configured)',
    files: [
      { path: '.github/workflows/deploy-engine.yml', line: 1, repo: 'novakash' },
      { path: 'docs/CI_CD_SETUP.md', line: 1, repo: 'novakash' },
      { path: 'docs/MONTREAL_DEPLOYMENT_TROUBLESHOOTING.md', line: 1, repo: 'novakash' },
      { path: 'docs/v2-oak-integration-audit.md', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'docs/CI_CD.md (6816f86) explicitly flagged engine/ as the only major service without a GitHub Actions deploy workflow',
      'Engine relied on Railway git-watcher auto-deploy with no smoke test, no secrets check, no post-deploy health probe, no rollback',
      'Engine observed CRASHED in recent history (INC-01 network outage) requiring manual recovery',
      'PR #18 (reconciler) and PE-01/PE-02 fixes had no automated deployment path — required manual SSH to Montreal',
      '36 GitHub Actions secrets configured: ENGINE_SSH_KEY, ENGINE_HOST, DATABASE_URL, COINGLASS_API_KEY, TELEGRAM_*, POLY_*, BINANCE_*, BUILDER_*, RELAYER_*, TIINGO_API_KEY, CHAINLINK_BTC_USD, PAPER_MODE, ANTHROPIC_API_KEY, POLYGON_RPC_URL',
    ],
    fix: 'SHIPPED in PR #155 (commit 068478e). Workflow: (1) Python syntax check on PRs (engine/main.py, five_min_vpin.py, orchestrator.py), (2) Auto-deploy on push to develop, (3) SSH to novakash@15.223.247.178, (4) Rsync engine/ and scripts/, (5) Write .env from GitHub secrets, (6) Kill existing engine process, (7) Start new engine process, (8) Health probes: process count = 1 + error signature scan (last 10k lines of engine.log). Error thresholds: clob_feed.write_error=0, reconciler.resolve_db_error=0, prediction_recorder.write_error=0, etc. Documentation: docs/CI_CD_SETUP.md (secrets configuration guide), docs/MONTREAL_DEPLOYMENT_TROUBLESHOOTING.md (server troubleshooting). Workflow triggers on engine/** changes, scripts/restart_engine.sh, or .github/workflows/deploy-engine.yml changes.',
    progressNotes: [
      { date: '2026-04-13', note: 'SHIPPED via GitHub Actions secrets configuration (36 total) + deploy-engine.yml update. Workflow validates Python syntax on PRs, auto-deploys to Montreal on push to develop, runs post-deploy health checks. All secrets added via gh CLI: ENGINE_SSH_KEY (deploy key), ENGINE_HOST (15.223.247.178), and all environment variables from Montreal\'s .env. Deploy key retrieved from Montreal via EC2 Instance Connect. Workflow uses novakash user (not ubuntu) for all SSH operations. Future engine changes auto-deploy on push to develop.' },
    ],
  },
  {
    id: 'DEP-02',
    category: 'ci-cd',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Migrate hub from Railway to AWS Montreal (latency + CI/CD closure)',
    files: [
      { path: 'hub/main.py', line: 1, repo: 'novakash' },
      { path: 'hub/Dockerfile', line: 1, repo: 'novakash' },
      { path: '.github/workflows/deploy-hub.yml', line: 1, repo: 'novakash' },
      { path: 'frontend/nginx.conf', line: 15, repo: 'novakash' },
    ],
    evidence: [
      'User request 2026-04-11: "the time from me clicking trade and it going through needs to be near instant suggest you bring hub onto aws like everything else as part of cicd proper as part of to do list in audit etc and sort that in bg".',
      'Current latency chain: Frontend (AWS nginx) → Railway hub (cross-region hop) → Montreal Postgres (second cross-region) → engine poll every ~5-10s → Montreal polymarket_client. Hub-on-Railway adds ~500ms-1s of cross-region RTT to every trade click before it even reaches the engine poll loop.',
      'CI/CD gap: hub is the LAST service still on Railway git-watcher deploy with no GitHub Actions workflow, no import smoke test, no post-deploy health probe. docs/CI_CD.md has flagged it since the macro-observer and data-collector migrations.',
      'Target: hub runs on the same Montreal box (3.98.114.0) as timesfm-service, macro-observer, data-collector. Port 8091 to avoid clash with timesfm (8080) and margin_engine (8090 is eu-west-2 only).',
      'Architecture preserved: hub STILL only writes to manual_trades DB; Montreal engine STILL polls + executes; Polymarket API calls STILL come from the engine, never the hub. This migration changes WHERE the hub runs, not WHAT it does.',
    ],
    fix: 'Background Agent H dispatched to create the AWS deploy infrastructure WITHOUT cutting over: (1) hub/Dockerfile, (2) hub/docker-compose.yml mirroring the macro-observer pattern, (3) .github/workflows/deploy-hub.yml port of deploy-macro-observer.yml with the 8-step pattern (secrets check, SSH key, rsync, .env template, docker compose up, healthcheck, log tail), (4) hub/.env.example documenting all env vars the hub reads, (5) docs/CI_CD.md update with the hub row. Cutover (frontend nginx upstream flip + Railway teardown) is a SEPARATE follow-up PR after this infrastructure lands and proves healthy in parallel to the running Railway hub.',
    progressNotes: [
      { date: '2026-04-11', note: 'Agent H dispatched in background worktree. Will produce a PR adding the deploy infrastructure only — no Railway cutover, no nginx.conf changes, no frontend disruption. Operator can review, merge, wait for the deploy workflow to prove the AWS hub healthy, then flip the nginx upstream + tear down Railway in a second PR.' },
      { date: '2026-04-11', note: 'Shipped PR #44 — hub migration infrastructure (Dockerfile.aws, docker-compose.yml, deploy-hub.yml workflow, docs/CI_CD.md cutover plan). Both hubs (Railway + AWS) run in parallel; operator flips frontend nginx upstream to promote AWS.' }
    ],
  },
  {
    id: 'LT-04',
    category: 'ci-cd',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Reduce manual trade click-to-execute latency to near-instant',
    files: [
      { path: 'engine/strategies/orchestrator.py', line: 2525, repo: 'novakash' },
      { path: 'engine/persistence/db_client.py', line: 75, repo: 'novakash' },
      { path: 'hub/api/v58_monitor.py', line: 1641, repo: 'novakash' },
      { path: 'engine/tests/test_manual_trade_fast_path.py', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/execution-hq/components/ManualTradePanel.jsx', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'User request 2026-04-11: "the time from me clicking trade and it going through needs to be near instant".',
      'Current latency chain: click → frontend POST → nginx → Railway hub (cross-region hop ~500ms-1s) → Postgres write → engine polls every ~5-10s → engine executes → Polymarket API call → fill returned.',
      'DEP-02 (hub migration) addresses the hub-to-Montreal RTT portion. LT-04 addresses the engine polling interval.',
      'Current poll: orchestrator.py:2515 polls poll_pending_live_trades() on whatever the main loop tick cadence is. Confirmed as 1s. Switched to PostgreSQL LISTEN/NOTIFY (hybrid event-driven + 1s safety-net poll) which drops NOTIFY-to-execute latency to tens of milliseconds.',
      'LISTEN/NOTIFY pattern: hub INSERTs a row → hub emits `SELECT pg_notify(\'manual_trade_pending\', trade_id)` → engine has a pinned asyncpg connection that LISTENs on that channel → `asyncio.Event` fires → poll loop\'s `await asyncio.wait_for(event.wait(), timeout=1)` returns immediately.',
    ],
    fix: 'Shipped via PR (LT-04). Engine side: new listen()/ensure_listening()/stop_listening() on DBClient that opens a dedicated asyncpg connection and calls add_listener(\'manual_trade_pending\', callback). Orchestrator._manual_trade_poller now does `await asyncio.wait_for(self._manual_trade_notify_event.wait(), timeout=1)` and clears the event each tick. Hub side: v58_monitor.post_manual_trade emits `SELECT pg_notify(\'manual_trade_pending\', :trade_id)` after the INSERT commit (wrapped in try/except — NOTIFY failure is non-fatal because the 1s poll still picks the row up). Safety-net preserved: if the LISTEN connection dies, ensure_listening re-opens it on the next tick and the 1s poll still fires in the meantime. LT-02 DB fallback preserved — the fast path uses the exact same token_id lookup code. New test file engine/tests/test_manual_trade_fast_path.py pins down all 5 invariants (7 tests, all passing).',
    progressNotes: [
      { date: '2026-04-11', note: 'Investigation confirmed poll was already 1s, not 5-10s — orchestrator.py:2535 called `await asyncio.sleep(1)` at the top of each iteration. So the ceiling latency was already 1s worst-case, not 5-10s. Still worth shipping LISTEN/NOTIFY for the sub-100ms happy path, especially once DEP-02 moves the hub to the same AWS region as the engine DB connection.' },
      { date: '2026-04-11', note: 'Chose Option A (PostgreSQL LISTEN/NOTIFY) over Option B (HTTP kick) because the engine has no web server — adding one just for a single internal endpoint is overkill. NOTIFY fits naturally into the existing asyncpg stack and avoids opening a new port on the Montreal host. Hybrid design keeps the 1s poll as a safety net so any LISTEN connection failure is zero-regression vs pre-LT-04.' },
      { date: '2026-04-11', note: 'Latency: NOTIFY-to-place_order measured <500ms in unit tests (FakePolyClient returns instantly — real Polymarket round-trip is ~200-500ms irreducible floor). Test suite: 7 tests all green (happy path, stale NOTIFY safe no-op, dropped LISTEN → poll fallback, LT-02 DB fallback regression check, multiple NOTIFYs batch-drained, channel-name sanity check).' },
    ],
  },

  // ── frontend ────────────────────────────────────────────────────────────
  {
    id: 'FE-01',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'V4Panel added to /margin page (PR #22)',
    files: [
      { path: 'frontend/src/pages/margin-engine/components/V4Panel.jsx', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Commit 7170a50 shipped V4Panel to margin-engine page',
      'Renders macro bias, direction gate, consensus health, per-timescale cards',
      'Each card shows verdict (LONG/SHORT/SKIP), p_up, expected_move, regime, conviction, gate skip reason',
      'Auto-refreshes every 5s via /api/v4/snapshot proxy in hub/api/margin.py',
    ],
    fix: 'Already shipped. This is the reference pattern for V4-centric UI.',
  },
  {
    id: 'FE-02',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'Audit checklist page (this page)',
    files: [
      { path: 'frontend/src/pages/AuditChecklist.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/App.jsx', line: 33, repo: 'novakash' },
      { path: 'frontend/src/components/Layout.jsx', line: 14, repo: 'novakash' },
    ],
    evidence: [
      'New /audit route with static task data',
      'Categorized checklist with severity, status, file:line citations',
      'Deploys automatically on push to develop via deploy-frontend.yml',
      'Rev-2 adds progressNotes rendering, CI/CD category, 5 new tasks (CI-01, FE-04..07), and docs/AUDIT_PROGRESS.md pointer',
    ],
    fix: 'Shipped in PR #26. Rev-2 adds the progress tracking mechanism and the CI/CD + v1-v4 surface task seeds requested in the 2026-04-11 session.',
    progressNotes: [
      { date: '2026-04-11', note: 'PR #26 opened; local vite build green; rendered end-to-end via playwright on the dev server and confirmed filter + expand interactions. Merged → deploy-frontend.yml serves it at /audit on ${AWS_FRONTEND_HOST}.' },
      { date: '2026-04-11', note: 'Rev-2: progressNotes rendering added, new CI-CD category, CI-01 + FE-04..07 seeded, PE-01/PE-02/FE-02 marked DONE with completion notes.' },
    ],
  },
  {
    id: 'FE-03',
    category: 'frontend',
    severity: 'LOW',
    status: 'IN_PROGRESS',
    title: 'No observability surface for Polymarket (legacy) engine',
    files: [
      { path: 'frontend/src/pages/Dashboard.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/polymarket/Monitor.jsx', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'margin_engine has rich /margin page with V4Panel',
      'Polymarket engine surfaces are split across /dashboard, /signals, /v58, /execution-hq',
      'No single "engine_state + v4_snapshot_if_used + gate_failures" panel',
      'Operator has to cross-check logs to understand why a trade was skipped',
    ],
    fix: 'After V4-01 lands, build a Polymarket mirror of V4Panel showing what the engine saw when it skipped/entered a window.',
    progressNotes: [
      { date: '2026-04-13', note: 'Clean-arch rewrite using strategy_decisions._ctx. Monitor.jsx now reads v4_snapshot, consensus, macro, gate_results from /api/v58/strategy-decisions._ctx. Tracked as FE-11.' },
    ],
  },
  {
    id: 'FE-04',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'V1 data surface page (/data/v1) — legacy timesfm point forecast',
    files: [
      { path: 'frontend/src/pages/data-surfaces/V1Surface.jsx', line: 1, repo: 'novakash' },
      { path: 'hub/api/margin.py', line: 105, repo: 'novakash' },
    ],
    evidence: [
      'No frontend page currently shows the original v1 TimesFM point forecast (direction + confidence)',
      '/timesfm page is about v2 probability, not the v1 raw forecast',
      'Operators cannot see what v1 was predicting without log hunting',
    ],
    fix: 'New /data/v1 page rendering asset selector + 60-step point forecast line chart + confidence bars + the last 10 v1 predictions vs oracle outcomes. Proxy through hub/api/margin.py → timesfm /v1/forecast or nearest equivalent. Reuse the margin-engine dark theme (T constants).',
    progressNotes: [
      { date: '2026-04-11', note: 'Shipped PR #32 — /data/v1 V1Surface page with hand-rolled SVG quantile envelope chart. Hub proxies via new /api/v1/forecast + /api/v1/health. Defensive card for non-BTC / 404 / 502 / 503.' },
    ],
  },
  {
    id: 'FE-05',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'V2 data surface page (/data/v2) — LightGBM probability + quantiles',
    files: [
      { path: 'frontend/src/pages/data-surfaces/V2Surface.jsx', line: 1, repo: 'novakash' },
      { path: 'hub/api/margin.py', line: 105, repo: 'novakash' },
      { path: 'app/v2_routes.py', line: 1, repo: 'novakash-timesfm-repo' },
    ],
    evidence: [
      '/v2/probability returns LightGBM calibrated scalar + full quantile surface',
      'Current /timesfm page exists but is cramped and mixes v2 with other concerns',
      'No way to visually diagnose v2 constant-leaf / train-serve skew at a glance (which was the v5 cutover bug)',
    ],
    fix: 'New /data/v2 page: 5m/15m/1h timescale tabs; p_up gauge; raw vs calibrated probability; quantile fan chart (p10/p25/p50/p75/p90); push-mode feature table showing the 25 v5 features that were actually sent to the scorer; last 20 predictions with hit/miss. Expose model_version, feature_sha, last_inference_ms, and raw probability variance over the last 1000 inferences so drift is visible.',
    progressNotes: [
      { date: '2026-04-11', note: 'Shipped PR #32 — /data/v2 V2Surface page with Sequoia v5.2 calibrated probability gauge, timescale tabs, quantile fan, last-20 history strip, feature freshness grid, model SHA chip. Hub proxies /v2/probability endpoints.' },
    ],
  },
  {
    id: 'FE-06',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'V3 data surface page (/data/v3) — composite signal + regime',
    files: [
      { path: 'frontend/src/pages/data-surfaces/V3Surface.jsx', line: 1, repo: 'novakash' },
      { path: 'hub/api/margin.py', line: 105, repo: 'novakash' },
    ],
    evidence: [
      '/v3/snapshot already proxied in hub/api/margin.py:105 and consumed by SignalPanel',
      'SignalPanel shows the 7 sub-signals but not the full 9-timescale composite map',
      'Cascade exhaustion_t, alignment across timescales, and v3 regime classifier logic are not surfaced',
    ],
    fix: 'New /data/v3 page: 9-timescale heatmap of composite_v3; 7-signal radar chart per timescale (elm/cascade/taker/oi/funding/vpin/momentum); cascade FSM timeline with exhaustion_t; regime history strip (NORMAL/TRANSITION/CASCADE/CHOPPY/NO_EDGE); alignment bar across short-term timescales. Makes the v3 regime classifier inspectable without reading Python source.',
    progressNotes: [
      { date: '2026-04-11', note: 'Shipped PR #32 — /data/v3 V3Surface page with 9-timescale composite heatmap, per-timescale sub-signal bars, cascade FSM chips, model-lineage chip. Hub proxies /v3/snapshot.' },
    ],
  },
  {
    id: 'FE-07',
    category: 'frontend',
    severity: 'HIGH',
    status: 'DONE',
    title: 'V4 data surface page (/data/v4) — fusion snapshot + per-source health',
    files: [
      { path: 'frontend/src/pages/data-surfaces/V4Surface.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/App.jsx', line: 34, repo: 'novakash' },
      { path: 'frontend/src/components/Layout.jsx', line: 40, repo: 'novakash' },
      { path: 'hub/api/margin.py', line: 125, repo: 'novakash' },
    ],
    evidence: [
      '/v4/snapshot is the richest surface in the stack and was previously only embedded in /margin',
      'New /data/v4 page polls /api/v4/snapshot every 4s with asset selector (BTC/ETH/SOL/XRP)',
      'ConsensusStrip: per-source chips with price + age_ms, max/mean divergence, agreement score, safe_to_trade verdict',
      'MacroCard: Qwen bias + confidence + direction gate + size/threshold modifiers + reasoning + per-timescale bias map',
      'EventsTimeline: upcoming macro events coloured by impact (EXTREME/HIGH/MEDIUM/LOW) with minutes-to-go',
      'Per-timescale grid: p_up vs raw, expected_move, vol_forecast, compact quantile fan (p10-p50-p90), regime + cascade + conviction chips, gate-stack reason line',
      'Raw JSON peek at the bottom for diagnostics',
    ],
    fix: 'Shipped the dedicated /data/v4 page in PR #26 rev-2. Next: FE-04/05/06 (v1, v2, v3 equivalents). Stretch: wire /v4/orderflow liquidation pressure into the footer once the assembler exposes it reliably.',
    progressNotes: [
      { date: '2026-04-11', note: 'Built in PR #26 rev-2 alongside the checklist extension. Reuses the theme tokens from V4Panel but gets the full viewport. Nav entry "V4 Fusion 🧭" added under ANALYSIS in the sidebar, route /data/v4 wired in App.jsx. Data path: useApi → /api/v4/snapshot → hub/api/margin.py:125 → TIMESFM_URL /v4/snapshot.' },
    ],
  },
  {
    id: 'DEP-01',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'DONE',
    title: '/deployments AWS services overview page',
    files: [
      { path: 'frontend/src/pages/Deployments.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/App.jsx', line: 34, repo: 'novakash' },
      { path: 'frontend/src/components/Layout.jsx', line: 58, repo: 'novakash' },
      { path: 'docs/CI_CD.md', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Static registry mirroring docs/CI_CD.md — one card per service with repo/branch/host/workflow/secrets/notes',
      'CI/CD status chips: active (green) | drafted (amber) | legacy-Railway (orange)',
      'Live health probes via existing hub endpoints (15s interval) for services that expose them: timesfm via /v4/snapshot, margin-engine via /margin/status, frontend via direct fetch /, hub via /api/system/status',
      'Services without reachable health endpoints (engine, macro-observer, data-collector) show workflow state only — authoritative truth lives in GitHub Actions',
      'Status summary strip: TOTAL / ACTIVE / DRAFTED / LEGACY counts',
      'Refresh button + lastRefresh timestamp',
      'Footer points at docs/CI_CD.md as the authoritative spec + /audit for in-flight tasks',
      '7 services registered: timesfm, macro-observer, data-collector, margin-engine, hub, frontend, engine',
    ],
    fix: 'Shipped in PR #27 rev-2. Future iterations should pull most-recent GitHub Actions workflow run status + CI-01 error-signature counts from the GH Actions REST API once CI-01 starts firing.',
    progressNotes: [
      { date: '2026-04-11', note: 'Built during the post-INC-01 engine recovery session. Route wired under SYSTEM sidebar section with "🚀 Deployments" label. Built on top of the CI-01 PR #27 branch so it ships alongside deploy-engine.yml (which it visualises).' },
    ],
  },
  {
    id: 'SCHEMA-01',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'DB schema viewer page — all tables, purposes, active/legacy status',
    files: [
      { path: 'hub/api/schema.py', line: 1, repo: 'novakash' },
      { path: 'hub/db/schema_catalog.py', line: 1, repo: 'novakash' },
      { path: 'hub/main.py', line: 37, repo: 'novakash' },
      { path: 'frontend/src/pages/Schema.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/App.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/components/Layout.jsx', line: 93, repo: 'novakash' },
    ],
    evidence: [
      'User request 2026-04-11: "add a page to the front end clearly showing the data structure all the tables their uses whether they are legacy or active etc so everything is properly trackable ya know".',
      'Problem: the data system has grown organically across engine / margin_engine / macro-observer / data-collector / hub / timesfm-service and nobody had a single view of which tables still matter, which are dead weight, which are written by which services, and which are authoritative sources vs caches.',
      'Solution: hand-curated SCHEMA_CATALOG dict in hub/db/schema_catalog.py (one entry per table — service, category, status, purpose, writers, readers, recency_column, docs, notes, large flag). Hub endpoint joins the static catalog with live runtime data (row_count via COUNT(*) or pg_class.reltuples for large tables, last write via MAX(recency_column)). Frontend Schema.jsx renders the full inventory with filter bar + expandable detail cards + write/read dependency map.',
      'Inventory (41 tables): 34 active, 4 legacy, 3 deprecated. Categories: polymarket, margin, macro, data, exec, hub, external. Services: engine, data-collector, margin_engine, macro-observer, hub, timesfm-service.',
      'Legacy tables flagged: signals (superseded by signal_evaluations), daily_pnl (superseded by live poly_fills aggregation), trading_configs (superseded by upcoming CFG-01), gate_audit (superseded by signal_evaluations eval_offset granularity).',
      'Deprecated (planned, not yet created): config_keys, config_values, config_history — CFG-02/03 in flight. Listed as stubs so the inventory matches the design doc.',
      'External stub: ticks_v3_composite — lives in novakash-timesfm-repo DB, not in the main trader DB, documented with a pointer only.',
      'Safety: table names are validated as [a-zA-Z0-9_]+ before interpolation into COUNT(*) queries (no parameterisation possible for identifiers). All live queries run under SET LOCAL statement_timeout = 300ms so the endpoint stays bounded.',
      'Endpoints: GET /api/v58/schema/summary (header counts), GET /api/v58/schema/tables (list with runtime stats), GET /api/v58/schema/tables/{name} (detail with column list from information_schema.columns).',
      'Route: /schema registered in App.jsx and wired into Layout.jsx SYSTEM section as "🗄️ DB Schema" — alongside /deployments and /audit.',
    ],
    fix: 'Shipped as feat(hub+frontend): SCHEMA-01 — /schema page showing all DB tables + active/legacy status. Architecture note: catalog lives as a hardcoded Python dict (not a seeded DB table) so it gets maintained via PR review rather than drifting from code. Any new table that lands in a service MUST be followed by a PR adding an entry to schema_catalog.py — reviewers can enforce this as a discipline. Future work: (1) auto-detect tables present in pg_catalog but missing from SCHEMA_CATALOG and raise a warning chip; (2) ERD diagram mode; (3) per-column read/write inference by grepping the repo.',
    progressNotes: [
      { date: '2026-04-11', note: 'Built during the same post-INC-01 session as NT-01 and /deployments. User ask was short and unambiguous so we went straight to a hand-curated catalog rather than an auto-discovery endpoint. 41 tables inventoried by reading CREATE TABLE statements across engine/persistence, margin_engine/adapters/persistence, macro-observer/observer.py, data-collector/collector.py, hub/db/schema.sql, hub/db/migrations, and migrations/*.sql. DO NOT auto-discover: legacy tables would show up as ??? which defeats the purpose.' },
    ],
  },
  {
    id: 'NT-01',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'DONE',
    title: '/notes Session observations + to-do page (DB-backed)',
    files: [
      { path: 'frontend/src/pages/Notes.jsx', line: 1, repo: 'novakash' },
      { path: 'hub/api/notes.py', line: 1, repo: 'novakash' },
      { path: 'hub/db/models.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'User request 2026-04-11: "add a frontend page called notes and have it as a db table of notes and things observations etc as we work". Added as FIRST entry in POLYMARKET sidebar section.',
      'DB-backed so notes persist across frontend redeploys and can be added/edited/deleted at runtime.',
      'Usage: Claude + user will post notes during audit sessions. Think of it as a session journal that the /audit page references for deeper context.',
      'Agent F shipped: SQLAlchemy model in hub/db/models.py, FastAPI CRUD routes in hub/api/notes.py, frontend Notes.jsx with form + list view + optimistic updates, sidebar nav entry at position 0.',
      'Merged as PR #36 (commit b35163d) at 14:15 UTC. Deploys via deploy-frontend.yml on push.',
    ],
    fix: 'Built by background Agent F in PR #36. 1153 additions across 6 files. Includes seed note so the page isn\'t empty on first load. Future-work pointers in the PR body: markdown renderer, @mentions linking to audit task IDs, WebSocket push for multi-user.',
    progressNotes: [
      { date: '2026-04-11', note: 'Dispatched as Agent F after user request during the post-INC-01 session.' },
      { date: '2026-04-11', note: 'DONE. PR #36 merged at b35163d. Will be visible on /notes once deploy-frontend.yml completes.' },
    ],
  },
  {
    id: 'STOP-01',
    category: 'production-errors',
    severity: 'CRITICAL',
    status: 'DONE',
    title: 'EMERGENCY: live trading paused (user reported bad decisions)',
    files: [
      { path: 'engine/strategies/orchestrator.py', line: 1755, repo: 'novakash' },
      { path: '/home/novakash/novakash/engine/.env', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'User reported 2026-04-11 14:56 UTC: "please pause live trading till we have finished our fixes we are making some really terrible trade decisions". Observed pattern: "a DOWN after 2 consecutive previous UP markets and other indicators in my view felt obvious it was either going up or down when we voted up or down respectively".',
      'Engine was in LIVE mode: PID 8478 running with LIVE_TRADING_ENABLED=true, PAPER_MODE=false.',
      'First attempted pause by flipping .env values (LIVE_TRADING_ENABLED=false + PAPER_MODE=true) + scripts/restart_engine.sh, but the restart wrapper timed out. After restart, engine auto-switched PAPER → LIVE at 15:06 UTC because orchestrator.py:1755 polls system_state.paper_enabled/live_enabled from DB on every heartbeat. .env changes are ignored if DB says otherwise.',
      'Second attempt: direct pkill + user manually flipped the UI toggle which updated system_state.paper_enabled=true/live_enabled=false in DB. Restarted engine in paper mode at 14:10 UTC (PID 13549).',
      'VERIFIED paused: system_state row shows paper_enabled=t/live_enabled=f, 0 is_live trades in the last 5 min, place_order.requested log lines show paper_mode=True, place_order.paper_filled confirms orders go through paper simulation not Polymarket CLOB.',
    ],
    fix: 'Incident resolved. Engine now running in PAPER mode. Root cause of the override: orchestrator.py has a DB-backed mode-toggle heartbeat that takes precedence over .env on every tick. Fix going forward: always flip the mode via the UI toggle (or direct system_state UPDATE), not via .env. The UI toggle flipped at 14:08:43 UTC took effect on the next engine restart.',
    progressNotes: [
      { date: '2026-04-11', note: 'Pause completed. Live trading stopped. Engine running in PAPER mode via system_state DB toggle. Root cause: orchestrator.py:1755 DB-backed mode heartbeat overrides .env. Document this in the SPARTA guide so future agents know the correct pause procedure is: UI toggle → DB update → next heartbeat picks up.' },
    ],
  },
  {
    id: 'LT-02',
    category: 'frontend',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Live trade panel broken end-to-end — root cause + DB fallback',
    files: [
      { path: 'engine/strategies/orchestrator.py', line: 2554, repo: 'novakash' },
      { path: 'engine/persistence/db_client.py', line: 1120, repo: 'novakash' },
      { path: 'frontend/src/pages/execution-hq/components/ManualTradePanel.jsx', line: 1, repo: 'novakash' },
      { path: 'hub/api/v58_monitor.py', line: 1292, repo: 'novakash' },
    ],
    evidence: [
      'User report 2026-04-11: "the live trade path doesnt seem to currently work i tried it".',
      'ROOT CAUSE: manual_trades table had exactly 1 row with status="failed_no_token" from 13:48:43 UTC — the user\'s attempted trade. orchestrator.py:2554 looked up the CLOB token_id ONLY from the in-memory FiveMinVPINStrategy._recent_windows ring buffer. Ring buffer miss → silently mark failed_no_token → continue, no Telegram alert.',
      'The ring buffer is volatile: empty after restart, populates from latest Polymarket market fetch, stale windows age out within minutes. The user\'s click timing put the target window outside the buffer when the engine looked it up.',
      'PR #42 fix: add a DB fallback via new get_token_ids_from_market_data() helper that queries the market_data table (UPSERTed per window by data-collector on Montreal). ±60s tolerance on window_ts match. Add a Telegram warning alert on total failure so the operator knows the trade didn\'t land.',
      'Hub architecture preserved: the hub is on Railway and MUST NEVER call Polymarket directly. Fix stays on the Montreal engine side — hub still only writes to manual_trades DB row.',
    ],
    fix: 'Shipped in PR #42 (merged at 4549a08, deployed to Montreal 14:34 UTC). Verified live on host: get_token_ids_from_market_data present in db_client.py, ring_buffer_miss_fetching_from_db log event present in orchestrator.py. Engine restarted, PID 15606 running 5.6% CPU. Next: user tests by clicking the trade panel on a fresh 5m window and confirms the Telegram notification fires with either success or the new FAILED alert message.',
    progressNotes: [
      { date: '2026-04-11', note: 'Investigation: SELECT from manual_trades revealed exactly 1 row with status=failed_no_token, no engine log entries for pending_trades_poll. Traced to orchestrator.py:2554 ring buffer lookup. Read market_data table schema in data-collector/collector.py:62 — has up_token_id + down_token_id columns, perfect fallback source.' },
      { date: '2026-04-11', note: 'FIXED in PR #42. Added DB fallback + Telegram alert on total failure. Preserves hub-never-calls-Polymarket architecture (hub stays on Railway, only Montreal engine executes). Deployed to Montreal 14:34 UTC, verified via SSH grep.' },
    ],
  },
  {
    id: 'LT-03',
    category: 'frontend',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Decision-snapshot DB for manual trades (operator-vs-engine ground truth)',
    files: [
      { path: 'hub/db/models.py', line: 1, repo: 'novakash' },
      { path: 'hub/api/v58_monitor.py', line: 1292, repo: 'novakash' },
      { path: 'frontend/src/pages/execution-hq/components/ManualTradePanel.jsx', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'User request 2026-04-11: "i want the system to have a db set up where whenever i trade it takes a huge data snapshot and records outcome so can see if i am right etc".',
      'Purpose: when the operator manually trades (paper or live), record the complete state at the moment of decision — all v4 snapshot fields, gate pipeline verdicts, recent window outcomes, VPIN, composite_v3, macro bias, consensus health — so after resolution we can see which decisions (engine vs operator) were right and why.',
      'User\'s motivating anecdote: "we noted a down after 2 consecutive previous up markets and other indicators in my view felt obvious it was either going up or down when we voted up or down respectively". The context that makes a decision "obviously right" to the operator isn\'t being captured anywhere today.',
      'Schema sketch: `manual_trade_snapshots (trade_id, window_ts, taken_at, v4_snapshot JSONB, v3_snapshot JSONB, last_5_window_outcomes JSONB, operator_rationale TEXT, operator_direction CHAR, engine_would_have_done CHAR, engine_gate_reason TEXT, resolved_outcome CHAR, pnl_usd NUMERIC, created_at TIMESTAMPTZ)`. JSONB captures the full context so future analysis can slice it any way.',
    ],
    fix: 'Phase 1: add manual_trade_snapshots table via hub/db/models.py + migration in hub/main.py::lifespan. Phase 2: in POST /api/v58/manual-trade, after the INSERT into manual_trades, also INSERT a corresponding row into manual_trade_snapshots with the full v4 snapshot + v3 snapshot + last 5 window outcomes + what the engine would have decided (read from signal_evaluations for the current window_ts). Phase 3: frontend — add an "operator rationale" textarea to ManualTradePanel so the user can type "felt obvious UP after 2 previous DOWNs". Phase 4: /audit page (or a new /decision-review) shows side-by-side operator vs engine decisions with resolved outcomes.',
    progressNotes: [
      { date: '2026-04-11', note: 'Shipped PR #47 — manual_trade_snapshots DB table + operator_rationale field + _capture_trade_snapshot helper wired into POST /v58/manual-trade. Snapshot failure isolated from trade execution via try/except after commit. New GET /v58/manual-trade-snapshots endpoint.' },
    ],
  },
  {
    id: 'UI-02',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'Multi-market HQ monitors (BTC/ETH/SOL/XRP × 5m/15m)',
    files: [
      { path: 'frontend/src/pages/execution-hq/ExecutionHQ.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/execution-hq/components/LiveTab.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/components/Layout.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/App.jsx', line: 1, repo: 'novakash' },
      { path: 'hub/api/v58_monitor.py', line: 2610, repo: 'novakash' },
    ],
    evidence: [
      'User request 2026-04-11: "please also make it so we can increase the markers for the live trading in the future to btc 5m and 15m and the other 3 assets too etc (should have a diff hq monitor for each) but just bare that in mind whilst you get all this working".',
      'Today\'s ExecutionHQ is BTC-5m only. Scaling up means: (a) engine needs to trade multiple (asset, timeframe) windows concurrently, (b) each window pair needs its own decision state visible at a glance, (c) operator needs to see all 8 combinations (BTC/ETH/SOL/XRP × 5m/15m) on one dashboard or switch quickly.',
      'Engine already has FIVE_MIN_ASSETS env var supporting multi-asset 5m. FIFTEEN_MIN_ENABLED + FIFTEEN_MIN_ASSETS for 15m. Historical config: both exist but 15m path is less exercised.',
      'Frontend options: (a) one /execution-hq page with 8 columns (one per market pair), (b) separate /hq/<asset>-<timeframe> routes, (c) one /markets-overview page with 8 tiles + click-through to detail view.',
    ],
    fix: 'SHIPPED: (Phase 1) Extended /api/v58/execution-hq to accept asset + timeframe query params with enum validation (asset in {btc,eth,sol,xrp}, timeframe in {5m,15m}). All internal queries filter on asset/timeframe — window_snapshots, signal_evaluations, trades (via market_slug ILIKE prefix), v9/v10 stats. Empty arrays when the collector hasn\'t populated a combo (no 500). Legacy /api/v58/execution-hq without params still defaults to BTC 5m. (Phase 2) New route /execution-hq/:asset/:timeframe with 8 routes for all combos, legacy /execution-hq redirects to /execution-hq/btc/5m. ManualTradePanel is gated to BTC 5m only; other 7 are monitor-only surfaces with a clean "no data yet" banner when the pair has no window_snapshots. (Phase 3) Sidebar nav becomes a collapsible accordion under the Execution HQ parent item with 8 children, auto-expanded when the current route matches a child, BTC 5m flagged with a LIVE pill. (Phase 4 / overview tile grid at /markets: deferred — operator can just open 8 tabs.)',
    progressNotes: [
      { date: '2026-04-11', note: 'Shipped UI-02 Phases 1–3. Hub endpoint now accepts asset/timeframe with validation + backward compat. Frontend has 8 routes (btc/eth/sol/xrp × 5m/15m), a sidebar accordion, per-page title, empty-data banner, and ManualTradePanel gated to BTC 5m to avoid cross-market trades. Engine/margin_engine untouched (zero scope leak). Deferred: /markets overview tile grid + multi-market manual trading (both low priority until the non-BTC data-collector path is verified populating signal_evaluations).' },
    ],
  },
  {
    id: 'UI-01',
    category: 'frontend',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Gate heartbeat + trade decision observability (upgrade Execution HQ)',
    files: [
      { path: 'frontend/src/pages/execution-hq/ExecutionHQ.jsx', line: 1, repo: 'novakash' },
      { path: 'hub/api/v58_monitor.py', line: 1, repo: 'novakash' },
      { path: 'engine/strategies/five_min_vpin.py', line: 680, repo: 'novakash' },
    ],
    evidence: [
      'User request 2026-04-11: "make sure there is a front end page that very clearly like the old execution hq or maybe upgrade that so i can very clearly see the gate heartbeat etc and trade decision".',
      'The existing /execution-hq page exists but has not been verified as current — I need to read it first before deciding upgrade-in-place vs new page.',
      'The DS-01 V10.6 EvalOffsetBoundsGate just landed (PR #33) adding G0 to the now-8-gate pipeline. Operator needs a real-time view of which gate is blocking and why, and what the current gate-stack decision is for every window close.',
      'Prior art: V4Panel.jsx on /margin renders per-timescale gate stack verdicts (side/conviction/reason). The Polymarket engine has no equivalent — its gates only emit structured logs.',
      'Data source options: (a) add a `/api/engine/gate-stack?limit=100` hub endpoint that returns the last 100 gate evaluations joined from signal_evaluations; (b) have the engine push gate decisions to a new ticks_gate_pipeline table that the frontend polls; (c) tail the engine.log via a hub-side grep. Option (a) is cleanest — the signal_evaluations table already has columns for every gate_passed boolean.',
    ],
    fix: 'Phase 1: Read the existing /execution-hq page + hub/api/v58_monitor.py to understand what\'s already there. If it\'s mostly useful, upgrade it in place with a new "Gate Heartbeat" section driven by a new /api/engine/gate-stack endpoint that selects the last 100 rows from signal_evaluations with gate_* columns. If /execution-hq is stale, build a new /gate-heartbeat page that is purely this surface. Phase 2: Add a "Live Decision" strip at the top showing the current 5m window\'s gate path as it ticks through. Phase 3 (stretch): click-through from each gate in the strip to its config values (env var + current threshold) for easy diagnosis.',
    progressNotes: [
      { date: '2026-04-11', note: 'Shipped PR #46 — V10.6 gate heartbeat section in Execution HQ Live tab. 8-gate current-window strip + TRADE/SKIP decision pill + last-20 rail + aggregate breakdown of gate_failed shares across last 50 evals. Piggybacks on existing 10s fetchData() poll.' },
    ],
  },
  {
    id: 'LT-01',
    category: 'frontend',
    severity: 'HIGH',
    status: 'OPEN',
    title: 'Live trading panel — execute trades from frontend (Montreal rules, auth-gated)',
    files: [
      { path: 'frontend/src/pages/LiveTrading.jsx', line: 1, repo: 'novakash' },
      { path: 'hub/api/paper.py', line: 1, repo: 'novakash' },
      { path: 'engine/execution/polymarket_client.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'User request 2026-04-11: "make sure things like the live trading panel and ability to execute trades montreal rules from the front end also works".',
      'The existing /live route points at LiveTrading.jsx — I need to read it first to understand what exists today.',
      'Montreal rules context: the user has been using "Montreal rules" for SSH-based engine ops. For live trading execution from the frontend, it probably means the trade must be authenticated, rate-limited, and confirmed via an explicit operator action — not a fire-and-forget API call.',
      'Security model questions (need user input before any code is written): (a) which engine executes — Polymarket engine on Montreal, or margin_engine on eu-west-2? (b) paper mode or live mode? If live, what are the stake caps? (c) how does the operator authenticate beyond the existing JWT? (d) confirmation UX: modal dialog with explicit "yes I want to stake $X on Y outcome"? (e) rate limit: N trades per hour max?',
      'Highest safety: ship the panel in PAPER ONLY mode first — operator can click to trigger a paper trade, see it go through the full gate pipeline, observe fill + resolve. Then later add a separate LIVE mode behind additional confirmation + an explicit operator-only env flag.',
    ],
    fix: 'Phase 0 (this task): read the existing LiveTrading.jsx + hub/api/paper.py to understand what\'s already there. Propose a security model to the user before writing any code. Phase 1 (paper only): ensure the panel can trigger a paper trade through the real gate pipeline and display the full trace. Phase 2 (live, deferred): add confirmation modal + rate limiter + operator-only env flag + stake cap enforcement. Phase 2 is NOT in scope without explicit user approval of the security model.',
    progressNotes: [
      { date: '2026-04-11', note: 'Added at user request 2026-04-11. Deliberately split into Phase 0 (read existing) + Phase 1 (paper only) + Phase 2 (live, deferred) — live execution from a web UI is a real-money security concern and I will not ship Phase 2 without the user explicitly approving the security model.' },
    ],
  },

  // ── ci-cd ───────────────────────────────────────────────────────────────
  {
    id: 'CI-01',
    category: 'ci-cd',
    severity: 'HIGH',
    status: 'IN_PROGRESS',
    title: 'Montreal CI/CD automation for engine/ (port deploy-macro-observer.yml pattern)',
    files: [
      { path: '.github/workflows/deploy-engine.yml', line: 1, repo: 'novakash' },
      { path: 'docs/CI_CD.md', line: 20, repo: 'novakash' },
      { path: '.github/workflows/deploy-macro-observer.yml', line: 1, repo: 'novakash' },
      { path: 'scripts/restart_engine.sh', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'docs/CI_CD.md (6816f86) flags engine/ as "the only major service without a GitHub Actions deploy workflow"',
      'Engine currently relies on Railway git-watcher auto-deploy with no smoke test, no secrets check, no post-deploy health probe, no rollback path',
      'docs/CI_CD.md: "has been observed CRASHED or FAILED in recent deploy history"',
      'Workflow drafted: 1 job, 13 steps, 15 env secrets, valid YAML confirmed via python3 yaml.safe_load',
      'Key differences from the macro-observer template: engine is NOT in Docker (raw python3 process via scripts/restart_engine.sh), two-user host (ssh as ubuntu, engine runs as novakash via sudo -u novakash), post-deploy health probe is pgrep process-count + log-grep instead of docker healthcheck',
      'Error-signature gate thresholds encode expected post-PR #26 state: clob_feed.write_error=0, reconciler.resolve_db_error=0, orphan_fills_error<=5, price_source_disagreement<=30 (will tighten to <5 after DQ-01 ships)',
      'Requires 15 new GitHub Actions secrets: ENGINE_HOST, ENGINE_SSH_KEY, plus engine runtime credentials (DATABASE_URL, COINGLASS_API_KEY, BINANCE_*, POLY_*, TELEGRAM_*)',
    ],
    fix: 'Draft shipped in PR #27. IN_PROGRESS because the workflow only verifies on first fire against the real host — until ENGINE_HOST + ENGINE_SSH_KEY are populated in Actions secrets and a push to develop exercises the deploy, this is drafted-not-proven. Move to DONE after: (a) ENGINE_SSH_KEY bootstrapped onto the novakash-montreal-vnc host authorized_keys for ubuntu user, (b) first manual workflow_dispatch succeeds end-to-end, (c) PE-01/PE-02 error-signature gate passes against the live log.',
    progressNotes: [
      { date: '2026-04-11', note: 'Drafted .github/workflows/deploy-engine.yml on branch claude/ci/deploy-engine-montreal. 13 steps: checkout, Require runtime secrets, Write SSH key, Ensure host directories, Rsync engine, Rsync scripts, Reset host .env, Template .env from secrets, Restart via scripts/restart_engine.sh, Wait 45s, Process-count health probe, Error-signature log-grep gate, Tail recent logs. Uses injection-defence pattern (env: pull-up for all secrets, --rsync-path="sudo rsync" for novakash-owned paths). Non-secret runtime flags (V10_*, FIVE_MIN_*, LIVE_TRADING_ENABLED, thresholds) are intentionally NOT templated — they stay hand-managed on the host because they change more often than CI deploy cadence. Waiting on operator action to (a) bootstrap ENGINE_SSH_KEY onto the novakash-montreal-vnc box authorized_keys and (b) add the 15 secrets to billybrichards/novakash Actions secrets.' },
      { date: '2026-04-11', note: 'PR #71 — deploy-engine.yml wired up, ENGINE_HOST set. ENGINE_SSH_KEY still needed.' },
      { date: '2026-04-11', note: 'PR #84 — excluded clean-arch dirs (engine/domain/, engine/use_cases/, engine/adapters/) from deploy-engine path filter so refactor PRs don\'t trigger unnecessary deploys.' },
    ],
  },

  // ── config-migration (CFG) ───────────────────────────────────────────────
  {
    id: 'CFG-01',
    category: 'config-migration',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'Full DB-backed config migration plan (this doc)',
    files: [
      { path: 'docs/CONFIG_MIGRATION_PLAN.md', line: 1, repo: 'novakash' },
    ],
    evidence: [
      '1243-line plan landed via PR #53. Inventories all 142+ runtime config keys across engine, margin_engine, hub, data-collector, macro-observer, timesfm-service.',
      'Splits keys into .env-only (secrets, infrastructure, bootstrap flags) vs DB-managed (trading behaviour, thresholds, gates). Decision rules in §2.3.',
      'Defines new tables (config_keys, config_values, config_history) in §5, hub API surface in §7, frontend UX in §8, phasing in §10.',
      'Captures the gates.py __init__-capture problem and proposes restart_required=TRUE as the Phase 1 mitigation (§6.3).',
      'Risk matrix §10 covers hot-reload races, DB outage degrade-safe behaviour, secret exclusion, and bootstrap chicken-and-egg.',
    ],
    fix: 'PROPOSAL ONLY — no code changes in PR #53. The plan kicks off CFG-02..CFG-11 implementation work.',
    progressNotes: [
      { date: '2026-04-11', note: 'Plan merged via PR #53 on develop. CFG-02/03/05 implementation ships in the follow-up PR (this audit page update tracks both PRs).' },
    ],
  },
  {
    id: 'CFG-02',
    category: 'config-migration',
    severity: 'HIGH',
    status: 'DONE',
    title: 'config_keys + config_values + config_history DB schema + seed migration',
    files: [
      { path: 'hub/db/config_schema.py', line: 1, repo: 'novakash' },
      { path: 'hub/db/config_seed.py', line: 1, repo: 'novakash' },
      { path: 'hub/main.py', line: 100, repo: 'novakash' },
      { path: 'hub/tests/test_config_schema.py', line: 1, repo: 'novakash' },
      { path: 'hub/tests/test_config_seed.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'New module hub/db/config_schema.py exposes ensure_config_tables() that creates all three tables + the deferrable unique constraint + two indexes via IF NOT EXISTS / DO blocks. Idempotent on re-deploy.',
      'New module hub/db/config_seed.py contains 175 seed rows across engine (111), margin_engine (51), data-collector (7), macro-observer (6). The plan §4.7 says ~142 — the literal §4.1.x and §4.2.2 tables sum to 175 (the plan summary uses approximate counts). Hub registers 0 keys for v1; timesfm registers 0 (read-only display deferred to CFG-13).',
      'Secret-exclusion gate enforced via SECRET_PATTERN regex in validate_seed() — any key matching .*_(API_KEY|SECRET|TOKEN|PASSWORD|PRIVATE_KEY|PASSPHRASE|FUNDER_ADDRESS|WALLET_KEY)$ aborts the seed before it touches the DB.',
      'Idempotent UPSERT preserves operator-set current_value rows: ON CONFLICT (service, key) DO UPDATE SET only refreshes developer-owned fields (type/default/description/category) and never touches current_value.',
      'Hub lifespan in main.py wires both ensure_config_tables() and seed_config_keys() into the same migration block as ensure_manual_trades_table().',
      'Tests: 23 unit tests cover schema DDL contents, validate_seed secret rejection, seed_summary counts, idempotency, and the V10_* restart_required flag invariant. All passing.',
    ],
    fix: 'SHIPPED — DDL + seed + tests + main.py wiring. New tables coexist alongside trading_configs without touching it. SKIP_DB_CONFIG_SYNC remains true on prod, so the new tables are pure additions with zero behaviour change in production.',
    progressNotes: [
      { date: '2026-04-11', note: 'DONE in PR for CFG-02/03/05. Seed loads 175 keys: engine 111, margin_engine 51, data-collector 7, macro-observer 6, hub 0 (v1), timesfm 0 (deferred to CFG-13).' },
    ],
  },
  {
    id: 'CFG-03',
    category: 'config-migration',
    severity: 'HIGH',
    status: 'DONE',
    title: 'hub /api/v58/config* read endpoints (GET schema/values/history/services)',
    files: [
      { path: 'hub/api/config_v2.py', line: 1, repo: 'novakash' },
      { path: 'hub/main.py', line: 35, repo: 'novakash' },
      { path: 'hub/tests/test_config_api.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'New router hub/api/config_v2.py mounts under /api/v58/config/* and exposes four GET endpoints: /services, / (per-service with values), /schema (per-service no values), /history (per-key audit log).',
      'All endpoints sit behind Depends(get_current_user) — same JWT auth wall the rest of the hub uses. CFG-06 will gate writes on an admin claim.',
      'POST /api/v58/config returns 501 Not Implemented with a message pointing at CFG-04, so the OpenAPI doc surfaces "coming soon" instead of a 404.',
      'Type coercion (TEXT in DB → real bool/int/float on the wire) lives in _coerce_value() and is exhaustively tested for bool / int / float / enum / string / failure-passthrough.',
      'Tests: 15 unit tests cover the four GET endpoints, the 501 POST stub, type coercion, the unknown-service empty-tab behaviour, and the unknown-key 404. All passing against a mock SQLAlchemy session.',
    ],
    fix: 'SHIPPED — read-only API, no DB writes. Wired into hub/main.py app.include_router() block. Operator can hit GET /api/v58/config?service=engine after the next hub deploy.',
    progressNotes: [
      { date: '2026-04-11', note: 'DONE alongside CFG-02 in the same PR. Endpoints exercised by hub/tests/test_config_api.py via FastAPI TestClient with the auth dependency overridden.' },
    ],
  },
  {
    id: 'CFG-04',
    category: 'config-migration',
    severity: 'HIGH',
    status: 'OPEN',
    title: 'hub /api/v58/config POST upsert/rollback/reset + history append',
    files: [
      { path: 'hub/api/config_v2.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'CFG-02/03/05 ship the schema + read API + read UI. CFG-04 adds the three POST endpoints planned in CONFIG_MIGRATION_PLAN.md §7.2.',
      'The POST /api/v58/config stub already returns 501 with a CFG-04 pointer so the OpenAPI surface is clear about where writes will land.',
      'Each write path must be transactional: config_values UPSERT and config_history INSERT in one transaction, with the history INSERT being the source of truth (never UPDATE / never DELETE).',
    ],
    fix: 'TODO — add POST /upsert (single-key write), POST /rollback (revert via history_id), POST /reset (back to default_value). All three append to config_history in the same transaction. Coercion against config_keys.value_type before INSERT.',
    progressNotes: [
      { date: '2026-04-11', note: 'Write endpoints queued in SPARTA Appendix D future task queue. Depends on CFG-02/03/05 which shipped as PR #61 (read-only surface). Stub POST at /api/v58/config returns 501 pointing at this task.' },
    ],
  },
  {
    id: 'CFG-05',
    category: 'config-migration',
    severity: 'HIGH',
    status: 'DONE',
    title: 'frontend /config page (read-only first) with widgets + history drawer',
    files: [
      { path: 'frontend/src/pages/Config.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/LegacyConfig.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/App.jsx', line: 67, repo: 'novakash' },
      { path: 'frontend/src/components/Layout.jsx', line: 90, repo: 'novakash' },
    ],
    evidence: [
      'New page frontend/src/pages/Config.jsx (~500 lines) calls GET /api/v58/config/services + /api/v58/config?service=X. Sidebar lists the services with key counts. Main pane groups keys by category in collapsible sections.',
      'Per-key row shows: key name, type badge, restart-required chip, read-only chip, current value (or — when at default). Click to expand and reveal description, default, category, last-set-by, last-set-at.',
      'Filters: text search across key name + description, category dropdown, service-tab in the sidebar.',
      'Phase banner explicitly says "CFG-02/03/05: read-only schema view. Write access ships in CFG-04 (next PR)." so operators know not to expect editing yet.',
      'Old 13-key Config.jsx renamed to LegacyConfig.jsx and routed at /legacy-config to preserve any in-flight bookmarks. The /config route now points at the new page; /trading-config still hosts the 25-key bundle editor.',
      'Layout.jsx sidebar gains the new "Config" entry with isNew flag and demotes the bundle editor to "Trading Cfg" so the new entry is the primary one.',
    ],
    fix: 'SHIPPED — read-only frontend with full schema browse + filter + per-key expand. History drawer is a placeholder (the GET /history endpoint exists but the drawer UI lands in CFG-06 alongside the rollback button).',
    progressNotes: [
      { date: '2026-04-11', note: 'DONE — Config.jsx + App.jsx + Layout.jsx + LegacyConfig.jsx rename all in the CFG-02/03/05 PR. Build passes.' },
    ],
  },
  {
    id: 'CFG-06',
    category: 'config-migration',
    severity: 'HIGH',
    status: 'OPEN',
    title: 'frontend /config editable (admin only) + optimistic concurrency check',
    files: [
      { path: 'frontend/src/pages/Config.jsx', line: 1, repo: 'novakash' },
      { path: 'hub/auth/jwt.py', line: 22, repo: 'novakash' },
    ],
    evidence: [
      'CFG-05 ships read-only. CFG-06 adds the per-key edit widgets + Save button + comment textarea + history drawer + Rollback button.',
      'Plan §10.3 calls for an If-Unchanged-Since header on writes so two operators editing the same key produce a 409 instead of last-write-wins.',
      'Plan §13 open question 2: who gets the admin claim? Either everyone-authenticated (ship tomorrow) or admin-claim-on-JWT (need a hub/auth/jwt.py change first).',
    ],
    fix: 'TODO after CFG-04 — depends on the write endpoints landing first. Then this is a frontend-only change to flip widgets from read-only to editable based on an admin claim, plus the optimistic concurrency check.',
    progressNotes: [
      { date: '2026-04-11', note: 'Frontend edit widgets queued in SPARTA Appendix D. Depends on CFG-04 landing the write endpoints first.' },
    ],
  },
  {
    id: 'CFG-07',
    category: 'config-migration',
    severity: 'CRITICAL',
    status: 'OPEN',
    title: 'engine service-side DBConfigLoader with TTL cache + safe degrade',
    files: [
      { path: 'engine/config/runtime_config.py', line: 1, repo: 'novakash' },
      { path: 'engine/config/db_config_loader.py', line: 1, repo: 'novakash' },
      { path: 'engine/strategies/orchestrator.py', line: 1755, repo: 'novakash' },
    ],
    evidence: [
      'CONFIG_MIGRATION_PLAN.md §6.1 specifies the loader contract: TTL cache, degrade-safe fallback (cache OR env OR compile-time default), never-raise get(), per-tick refresh.',
      'CFG-07 must not break the existing runtime_config.py public attribute surface — downstream code (five_min_vpin.py, orchestrator.py) reads attributes directly and we cannot afford a 200-file diff.',
      'Currently SKIP_DB_CONFIG_SYNC=true on prod. CFG-07 ships with the loader wired in but the skip flag still on, so the deploy is zero-risk. Operator flips the flag on a low-traffic window, monitors, rollback by re-flipping.',
    ],
    fix: 'TODO — add engine/config/db_config_loader.py per the §6.1 pseudocode, wire boot() + tick() into orchestrator heartbeat, swap runtime_config.py internals to read from config_values instead of trading_configs.config JSONB. Keep SKIP_DB_CONFIG_SYNC semantics intact.',
    progressNotes: [
      { date: '2026-04-11', note: 'Engine service-side loader with TTL cache + safe degrade queued in SPARTA Appendix D. Detailed task spec with scope limits.' },
    ],
  },
  {
    id: 'CFG-07b',
    category: 'config-migration',
    severity: 'HIGH',
    status: 'OPEN',
    title: 'engine gates.py hot-reload refactor (remove __init__-capture)',
    files: [
      { path: 'engine/signals/gates.py', line: 201, repo: 'novakash' },
    ],
    evidence: [
      'Plan §6.3 — 8 of 9 gate classes capture env vars at __init__ time. A DB config change does not propagate to the gates until the Python process restarts.',
      'CFG-02 marks all V10_* / V11_* keys as restart_required=TRUE so the UI surfaces a warning badge until CFG-07b lands.',
      'Refactor: each gate reads runtime.get(...) at evaluate() time instead of __init__ time. ~9 files, ~9 test updates, 2-day job.',
    ],
    fix: 'TODO after CFG-07 — refactor each gate class one at a time, flip restart_required=FALSE in the seed as each gate becomes hot-reloadable.',
  },
  {
    id: 'CFG-08',
    category: 'config-migration',
    severity: 'HIGH',
    status: 'OPEN',
    title: 'margin_engine service-side DBConfigLoader wiring',
    files: [
      { path: 'margin_engine/infrastructure/config/db_config_loader.py', line: 1, repo: 'novakash' },
      { path: 'margin_engine/infrastructure/config/settings.py', line: 1, repo: 'novakash' },
      { path: 'margin_engine/main.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Plan §6.2.2 — margin_engine has clean pydantic settings, easier to retrofit than engine. Each pydantic field becomes a property backed by the loader.',
      'Margin engine still paper-only (DQ-06 fixed), so the cutover is lower-risk than engine.',
      'Plan §13 question 9 recommends margin_engine first.',
    ],
    fix: 'TODO after CFG-04. Same loader contract as CFG-07.',
    progressNotes: [
      { date: '2026-04-11', note: 'margin_engine service-side loader queued in SPARTA Appendix D.' },
    ],
  },
  {
    id: 'CFG-09',
    category: 'config-migration',
    severity: 'MEDIUM',
    status: 'OPEN',
    title: 'hub service-side loader (self-referential — tricky, mostly no-op for v1)',
    files: [
      { path: 'hub/services/db_config_loader.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Plan §6.2.3 — the hub authors config but does not consume DB-backed config in v1. CFG-09 is a no-op stub task that exists to track the self-referential risk.',
      'When the hub grows its first DB-managed tunable (not yet — see plan §4.3), this task becomes real.',
    ],
    fix: 'TODO — defer until the hub actually has a DB-managed tunable. Until then this is a placeholder.',
    progressNotes: [
      { date: '2026-04-11', note: 'Hub self-referential config loader (chicken-and-egg problem called out) queued in SPARTA Appendix D.' },
    ],
  },
  {
    id: 'CFG-10',
    category: 'config-migration',
    severity: 'CRITICAL',
    status: 'OPEN',
    title: 'migration cutover per service (flip SKIP_DB_CONFIG_SYNC; include macro+data)',
    files: [
      { path: '.github/workflows/deploy-engine.yml', line: 1, repo: 'novakash' },
      { path: '.github/workflows/deploy-margin-engine.yml', line: 1, repo: 'novakash' },
      { path: 'macro-observer/observer.py', line: 1, repo: 'novakash' },
      { path: 'data-collector/collector.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Plan §10 — Phase 2 cutover per service. After CFG-07/08 ship the loader, this task flips SKIP_DB_CONFIG_SYNC=false on each host one at a time, monitors, rollback by re-flipping.',
      'macro-observer and data-collector are tiny surfaces (6 + 7 keys) and can ship in one PR after CFG-07.',
    ],
    fix: 'TODO — operator coordination. Per-service flips, not all-at-once.',
    progressNotes: [
      { date: '2026-04-11', note: 'Per-service migration cutover queued in SPARTA Appendix D. Plan recommends margin_engine first (lowest blast radius, cleanest pydantic BaseSettings existing).' },
    ],
  },
  {
    id: 'CFG-11',
    category: 'config-migration',
    severity: 'MEDIUM',
    status: 'OPEN',
    title: 'frontend audit: retire legacy /config + /trading-config; add cross-links',
    files: [
      { path: 'frontend/src/pages/LegacyConfig.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/TradingConfig.jsx', line: 1, repo: 'novakash' },
      { path: 'hub/api/config.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'After CFG-10 lands the per-service cutover, the legacy /trading-config bundle editor and the hub/api/config.py mini-API are dead weight.',
      'Plan §11.7 lists the retirement candidates: LegacyConfig.jsx (13-key page, already renamed in CFG-05), TradingConfig.jsx (25-key bundle editor), hub/api/config.py (13-key whitelist endpoint).',
      'Add ⚙ configure links from ExecutionHQ / MarginEngine / V1-V4 surfaces / Deployments to the relevant /config?service=... tab.',
    ],
    fix: 'TODO after CFG-10. Mostly delete-only work plus a small set of cross-link additions.',
    progressNotes: [
      { date: '2026-04-11', note: 'Legacy tab retirement queued in SPARTA Appendix D. Depends on CFG-02/03/05 being the primary surface.' },
    ],
  },
  // ── FE-08 (shipped PR #54) — sidebar rename hotfix ─────────────────────
  {
    id: 'FE-08',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'DONE',
    title: '/live sidebar entry mislabelled "Live Trading" — operator lands on wrong page when trading resumes',
    files: [
      { path: 'frontend/src/components/Layout.jsx', line: 53, repo: 'novakash' },
    ],
    evidence: [
      'Frontend audit (PR #52, docs/FRONTEND_AUDIT_2026-04-11.md) flagged /live as the most dangerous stale page.',
      'Sidebar labelled "💰 Live Trading" but the page is a v7-era wallet / PnL summary with NO manual trade button.',
      'Canonical manual-trade path is /execution-hq → Live tab → ManualTradePanel (LT-02 / LT-03).',
    ],
    fix: 'Renamed the sidebar entry to "💼 Wallet & PnL". 1-file, 6-line hotfix. Does not retire /live route (still a functional wallet view).',
    progressNotes: [
      { date: '2026-04-11', note: 'Shipped PR #54. vite build green. Operator safety gap closed — no more mistaking /live for the trade-placement path.' },
    ],
  },
  // ── NAV-01 (this PR) — nav streamlining + gates catalog ────────────────
  {
    id: 'NAV-01',
    category: 'frontend',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Nav streamlining + legacy labels + data-source tooltips + Gates & Signals catalog',
    files: [
      { path: 'frontend/src/components/Layout.jsx', line: 27, repo: 'novakash' },
      { path: 'frontend/src/pages/Schema.jsx', line: 759, repo: 'novakash' },
      { path: 'hub/db/schema_catalog.py', line: 1184, repo: 'novakash' },
      { path: 'hub/api/schema.py', line: 449, repo: 'novakash' },
    ],
    evidence: [
      'User feedback 2026-04-11: "there are so many pages i lose track!" — 28+ sidebar entries with no clear active/legacy distinction.',
      'User feedback 2026-04-11: "make sure its very clear what ui components correspond to what data source" — no single place answering "which page shows what data".',
      'User feedback 2026-04-11: "have we cleaned up all the tables and stuff and added an overview ... we have a LOT of tables and have had historic issues knowing whats real and whats not" — schema overview exists (SCHEMA-01) but no equivalent for gates / signals.',
      'Frontend audit (PR #52) flagged 5 partial / 5 stale / 1 pure-mock pages mixed into the primary sidebar sections.',
    ],
    fix: 'Consolidation PR ships 5-section nav (LIVE TRADING, MARGIN ENGINE, DATA SURFACES, OPS & SYSTEM, LEGACY), every entry gets a dataSource field rendered as an HTML title tooltip, legacy entries render greyscale + strikethrough + legacy chip, Assembler1 is promoted to primary DATA SURFACES entry, V1/V2/V3/V4 Surfaces kept as per-layer references. GATES_CATALOG in hub/db/schema_catalog.py documents all 8 V10.6 gates + 2 margin_engine v4 inline gates with inputs/outputs/env_flags/fail_reasons/tables_read/tables_written. New /api/v58/schema/gates endpoint + "Gates & Signals" tab on /schema page with accordion UI and cross-reference tables. Bulk AuditChecklist status flip for 21 stale rows.',
    progressNotes: [
      { date: '2026-04-11', note: 'Shipped this consolidation PR. vite build green. 5-section nav + GATES_CATALOG (10 entries) + /schema Gates tab all live.' },
    ],
  },
  // ── FACTORY-01 (this PR) — Factory Floor SIGNAL column clarity ────────
  {
    id: 'FACTORY-01',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'Factory Floor SIGNAL column — clarify source and meaning',
    files: [
      { path: 'frontend/src/pages/FactoryFloor.jsx', line: 1407, repo: 'novakash' },
      { path: 'docs/FACTORY_FLOOR_SIGNAL_SOURCE.md', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'User feedback 2026-04-11: "the factory floor looks GREAT i notice from the table there would be lots of trades that we would win !! a few losses but this is good !! what signal is that exactly? not super clear"',
      'The RECENT FLOW TIMELINE table on /factory rendered UP/DOWN under a column labelled SIGNAL with no indication of whether this was the DUNE model direction, the source-agreement vote, or the final pipeline decision. No tooltips on any header cell.',
      'Tracing the data flow (window_snapshots.direction -> hub/api/v58_monitor.py:1037 -> _row_to_window at :335 -> FactoryFloor.jsx:1449) confirmed the value is signal.direction from the 5m VPIN strategy, which on the v10.5+ prod path is ctx.agreed_direction from SourceAgreementGate (engine/signals/gates.py:281-420).',
    ],
    fix: 'Relabelled the SIGNAL header to "SIGNAL▸DIR", widened the SIGNAL/ACTUAL columns from 46px to 54px, and added native HTML title tooltips to every header cell (TIME, SIGNAL, ACTUAL, SRC, GATES, REASON, RESULT) naming the DB column and engine file:line that populates it. Added per-row tooltips on SIGNAL and ACTUAL cells with the specific value and resolution source. Added a small legend strip above the table with plain-English one-liners. Research note shipped at docs/FACTORY_FLOOR_SIGNAL_SOURCE.md with the full trace: SIGNAL = SourceAgreementGate 2/3 vote (CL+TI+BIN), ACTUAL = Polymarket trades.outcome with Binance open→close fallback. Single-file frontend-only change, no engine/hub edits.',
    progressNotes: [
      { date: '2026-04-11', note: 'Shipped this PR. Hover any header cell in RECENT FLOW TIMELINE to see the definitive column definition + file:line citation. Build green.' },
      { date: '2026-04-11', note: 'PR #69 — signal column clarity with tooltips merged to develop.' },
    ],
  },
  {
    id: 'LIVE-TOGGLE-AUDIT',
    category: 'ci-cd',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Docs-only toggle path audit with GREEN/YELLOW/RED verdicts',
    files: [{ path: 'docs/LIVE_TOGGLE_AUDIT.md', line: 1, repo: 'novakash' }],
    evidence: [
      'The live/paper toggle path spans orchestrator.py DB heartbeat, .env vars, UI toggle, system_state — no document mapped the full path.',
      'STOP-01 showed .env changes are silently overridden by DB state on every heartbeat tick.',
    ],
    fix: 'PR #72 shipped docs/LIVE_TOGGLE_AUDIT.md mapping every toggle mechanism with GREEN/YELLOW/RED safety verdicts.',
    progressNotes: [{ date: '2026-04-11', note: 'PR #72 merged to develop.' }],
  },
  {
    id: 'UI-04',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'WindowsTable per-window aggregation view on Factory Floor + Execution HQ',
    files: [
      { path: 'frontend/src/pages/FactoryFloor.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/execution-hq/ExecutionHQ.jsx', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Factory Floor and Execution HQ lacked a per-window aggregation view.',
      'Operator had to mentally group rows by window_ts.',
    ],
    fix: 'PR #74 added WindowsTable: per-window aggregation with signal direction, gate pass/fail, trade decision, outcome, PnL.',
    progressNotes: [{ date: '2026-04-11', note: 'PR #74 merged to develop.' }],
  },
  {
    id: 'POLY-SOT-d',
    category: 'data-quality',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Reconciler rewrite using poly_fills on-chain SOT',
    files: [
      { path: 'engine/reconciliation/reconciler.py', line: 1, repo: 'novakash' },
      { path: 'engine/persistence/db_client.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'POLY-SOT a/b/c used CLOB API. POLY-SOT-d rewrites to use poly_fills as the on-chain source of truth.',
      'CLOB API can lag. On-chain poly_fills are the definitive record.',
    ],
    fix: 'SHIPPED in PR #70. Reconciler now sources truth from poly_fills. Backfill script available for operator to run on Montreal.',
    progressNotes: [
      { date: '2026-04-11', note: 'PR #70 merged to develop. Reconciler rewrite complete — poly_fills is now the on-chain SOT for trade reconciliation. Backfill run pending on Montreal (operator must SSH and run scripts/backfill_sot_reconciliation.py).' },
    ],
  },
  {
    id: 'DATA-ARCH-01',
    category: 'clean-architect',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Data architecture audit — 39 tables with SOT/DERIVED/CACHE/LEGACY/OPERATIONAL tags',
    files: [{ path: 'docs/DATA_ARCHITECTURE_AUDIT.md', line: 1, repo: 'novakash' }],
    evidence: [
      '39+ tables across 6 services with no architectural role classification.',
      'Needed for clean-architect migration to distinguish sources of truth from caches.',
    ],
    fix: 'PR #81 shipped docs/DATA_ARCHITECTURE_AUDIT.md — all 39 tables tagged with roles and migration notes.',
    progressNotes: [{ date: '2026-04-11', note: 'PR #81 merged. Canonical data-layer reference.' }],
  },
  {
    id: 'ORCH-AUDIT-01',
    category: 'clean-architect',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Orchestrator deep audit — 33 methods, 9 use cases, risk matrix',
    files: [
      { path: 'docs/ORCHESTRATOR_AUDIT.md', line: 1, repo: 'novakash' },
      { path: 'engine/strategies/orchestrator.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'orchestrator.py has 33 methods mixing 9 concerns in a single class.',
      'CA-01 needs a method-level map before extraction can begin safely.',
    ],
    fix: 'PR #79 shipped docs/ORCHESTRATOR_AUDIT.md — 33 methods grouped into 9 use cases with extraction risk.',
    progressNotes: [{ date: '2026-04-11', note: 'PR #79 merged. Extraction guide for CA-01.' }],
  },
  {
    id: 'REPO-AUDIT-01',
    category: 'clean-architect',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Repo-wide clean-architecture audit — 10 modules graded',
    files: [{ path: 'docs/REPO_CLEAN_ARCH_AUDIT.md', line: 1, repo: 'novakash' }],
    evidence: [
      '10 modules with varying quality. No consistent grading rubric existed.',
      'Migration needs to know which modules are exemplars vs which need work.',
    ],
    fix: 'PR #77 shipped docs/REPO_CLEAN_ARCH_AUDIT.md — margin_engine A, hub B, frontend B, engine D.',
    progressNotes: [{ date: '2026-04-11', note: 'PR #77 merged. Module-level quality map.' }],
  },
  // ── clean-arch late-night blitz (PRs #82-103) ─────────────────────────────
  {
    id: 'REPO-AUDIT-02',
    category: 'clean-architect',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Repo-wide clean-architecture audit v3 — source-level, all 14 modules',
    files: [{ path: 'docs/REPO_CLEAN_ARCH_AUDIT.md', line: 1, repo: 'novakash' }],
    evidence: [
      'V1 (PR #77) covered 10 modules. V3 (PR #82) deepened to source-level analysis across all 14.',
    ],
    fix: 'PR #82 merged. Upgraded the repo-wide audit to source-level depth covering all 14 modules.',
    progressNotes: [{ date: '2026-04-11', note: 'PR #82 merged to develop. Source-level audit across all 14 modules.' }],
  },
  {
    id: 'CA-05',
    category: 'clean-architect',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Decouple orchestrator from five_min_vpin private internals',
    files: [
      { path: 'engine/strategies/orchestrator.py', line: 1, repo: 'novakash' },
      { path: 'engine/strategies/five_min_vpin.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'orchestrator.py directly accessed five_min_vpin._private methods and internal state.',
      'Blocked clean extraction of use cases — orchestrator coupling held back CA-01 refactor.',
    ],
    fix: 'SHIPPED in PR #86. Orchestrator now uses public accessors instead of reaching into five_min_vpin internals. Prerequisite for CA-01 Phase 3+ extractions.',
    progressNotes: [{ date: '2026-04-11', note: 'PR #86 merged to develop. Decoupled orchestrator from strategy private internals.' }],
  },
  {
    id: 'CA-06',
    category: 'clean-architect',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'Audit quick wins — DDL extraction, _DBShim removal, public accessors',
    files: [
      { path: 'hub/db/migrations/v58_monitor_ddl.py', line: 1, repo: 'novakash' },
      { path: 'hub/api/v58_monitor.py', line: 1, repo: 'novakash' },
      { path: 'hub/api/trading_config.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Quick-win items from the repo-wide audit: inline DDL strings in v58_monitor.py, _DBShim helper class coupling, private field access patterns.',
    ],
    fix: 'SHIPPED in PR #96. DDL extracted to hub/db/migrations/v58_monitor_ddl.py, _DBShim removed, public accessors added.',
    progressNotes: [{ date: '2026-04-11', note: 'PR #96 merged to develop. Audit quick wins QW1-QW5 implemented.' }],
  },
  {
    id: 'FE-09',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'Strategy badges and data source labels on all frontend pages',
    files: [
      { path: 'frontend/src/pages/', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Multiple frontend pages lacked clear strategy badges (Polymarket vs margin engine) and data source labels.',
      'User feedback: "make sure its very clear what ui components correspond to what data source".',
    ],
    fix: 'SHIPPED in PR #90. Strategy badges and data source labels added to all frontend pages for clarity.',
    progressNotes: [{ date: '2026-04-11', note: 'PR #90 merged to develop. All pages now show which strategy and data source they correspond to.' }],
  },
  {
    id: 'FE-10',
    category: 'frontend',
    severity: 'HIGH',
    status: 'IN_PROGRESS',
    title: 'Gate Pipeline Monitor — per-window strategy comparison table',
    files: [{ path: 'frontend/src/pages/polymarket/GatePipelineMonitor.jsx', line: 1, repo: 'novakash' }],
    evidence: [
      'Previously empty page showing "No strategy decisions found"',
      'Should show per-window table: all 5 strategies, gates, actual direction, WIN/LOSS',
      'Data: /api/v58/strategy-decisions grouped by window_ts',
    ],
    fix: 'Rewrite as window history table with gate results per strategy per window.',
    progressNotes: [{ date: '2026-04-13', note: 'Rewriting in fix/command-center-data-display branch. Per-window table with DN/ASIAN/V4F/V10 columns, filter pills, WIN/LOSS markers, sweet-spot best-decision picker.' }],
  },
  {
    id: 'FE-11',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'IN_PROGRESS',
    title: 'Monitor page clean-arch update — replace pre-v2 signals with strategy_decisions._ctx',
    files: [{ path: 'frontend/src/pages/polymarket/Monitor.jsx', line: 1, repo: 'novakash' }],
    evidence: [
      'Monitor showed Sequoia V5.2, SRC AGREEMENT etc — all NO DATA with Strategy Engine v2',
      'FE-03 audit item called for this: engine_state + v4_snapshot + gate_failures panel',
    ],
    fix: 'Rewire Monitor to read from /api/v58/strategy-decisions._ctx for all signal display.',
    progressNotes: [{ date: '2026-04-13', note: 'Fix in fix/command-center-data-display branch. Monitor.jsx now uses latestCtx from strategy-decisions._ctx for V4 Snapshot, Consensus, Macro sections.' }],
  },
  {
    id: 'SCHEMA-02',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'Schema catalog sync with data architecture audit',
    files: [
      { path: 'hub/db/schema_catalog.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'DATA-ARCH-01 (PR #81) documented 39 tables with roles. The hub schema_catalog.py was out of sync with these findings.',
    ],
    fix: 'SHIPPED in PR #89. schema_catalog.py updated to match the data architecture audit — table roles, writer/reader services, and status flags all aligned.',
    progressNotes: [{ date: '2026-04-11', note: 'PR #89 merged to develop. Schema catalog now consistent with DATA-ARCH-01 audit.' }],
  },
  // ── 2026-04-12 session: frontend redesign + signal fixes + regime classifier ──
  {
    id: 'DEP-02-CUTOVER',
    category: 'ci-cd',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Hub migration cutover — nginx upstream Railway → AWS Montreal',
    files: [
      { path: 'frontend/nginx.conf', line: 16, repo: 'novakash' },
      { path: 'frontend/.env.production', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'DEP-02 shipped the AWS hub infrastructure (PR #44) but never cut over.',
      'Frontend was still proxying through Railway (cross-region +500ms-1s RTT).',
      'AWS hub verified healthy: /health returns {"status":"ok"}, SG port 8091 opened.',
    ],
    fix: 'SHIPPED in PR #104. nginx upstream flipped to 3.98.114.0:8091 (HTTP, co-located). Railway stays alive as rollback.',
    progressNotes: [{ date: '2026-04-12', note: 'PR #104 merged. Security group sgr-0e5b75d772d0b6d63 opened port 8091. Manual trade latency now sub-100ms hub→engine.' }],
  },
  {
    id: 'SCHEMA-FIX',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'Schema page honest labels — PLANNED/ACTIVE/LEGACY instead of misleading "ACTIVE + NOT IN DB"',
    files: [
      { path: 'hub/db/schema_catalog.py', line: 1, repo: 'novakash' },
      { path: 'hub/api/schema.py', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/Schema.jsx', line: 1, repo: 'novakash' },
    ],
    evidence: [
      '39/46 tables showed "ACTIVE + NOT IN DB" — misleading.',
      'Tables that don\'t exist in DB were still marked status="active" in schema_catalog.py.',
    ],
    fix: 'SHIPPED in PR #106. 26 tables flipped to "planned". Frontend renders AMBER PLANNED, BLUE ACTIVE (EMPTY), GREY LEGACY chips.',
    progressNotes: [{ date: '2026-04-12', note: 'PR #106 merged. Schema page now shows honest status for all 46 tables.' }],
  },
  {
    id: 'SQ-01-PR1',
    category: 'clean-architect',
    severity: 'LOW',
    status: 'DONE',
    title: 'SQ-01 PR 1: elm_prediction_recorder → prediction_recorder (cosmetic rename)',
    files: [
      { path: 'engine/data/feeds/prediction_recorder.py', line: 1, repo: 'novakash' },
      { path: 'engine/tests/test_prediction_recorder.py', line: 1, repo: 'novakash' },
      { path: 'engine/strategies/orchestrator.py', line: 676, repo: 'novakash' },
      { path: '.github/workflows/deploy-engine.yml', line: 242, repo: 'novakash' },
    ],
    evidence: [
      'Model family evolved OAK → CEDAR → DUNE → ELM → SEQUOIA v5. "elm" naming was stale.',
      'CI gate signatures and log event names updated atomically to prevent deploy gate breakage.',
      'DB table ticks_elm_predictions preserved (renaming would orphan it).',
    ],
    fix: 'SHIPPED in PR #105. Class, file, log events, CI gate all renamed. DB table untouched.',
    progressNotes: [{ date: '2026-04-12', note: 'PR #105 merged. 4 files changed, all 5 tests pass.' }],
  },
  {
    id: 'REGIME-HMM',
    category: 'clean-architect',
    severity: 'HIGH',
    status: 'DONE',
    title: 'HMM regime classifier — 4-state Markov engine replacing hardcoded if/else',
    files: [
      { path: 'app/v3_regime_classifier.py', line: 1, repo: 'novakash-timesfm-repo' },
      { path: 'app/v4_snapshot_assembler.py', line: 1569, repo: 'novakash-timesfm-repo' },
      { path: 'tests/test_regime_classifier.py', line: 1, repo: 'novakash-timesfm-repo' },
    ],
    evidence: [
      'Deterministic regime classifier returned CHOPPY for 100% of samples.',
      'Thresholds were too tight and inputs (elm) were previously null.',
      'New 4-state Gaussian HMM: calm_trend, volatile_trend, chop, risk_off.',
      'Transition matrix + persistence probability + confidence score per classification.',
    ],
    fix: 'SHIPPED in timesfm PR #67. 702 lines added. Wired into /v4/snapshot and /v4/regime. Falls back to deterministic on failure. 24 tests.',
    progressNotes: [{ date: '2026-04-12', note: 'timesfm PR #67 merged. V4 snapshot now returns regime_confidence, regime_persistence, regime_transition fields.' }],
  },
  {
    id: 'S3-FIX',
    category: 'data-quality',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Alt-coin consensus fix — coinbase/kraken gated to BTC-only',
    files: [
      { path: 'app/v4_snapshot_assembler.py', line: 496, repo: 'novakash-timesfm-repo' },
    ],
    evidence: [
      'ETH/SOL/XRP consensus used BTC prices from coinbase/kraken (macro_signals is BTC-only).',
      'Caused 300,000+ bps divergence → permanent safe_to_trade=false for all alts.',
    ],
    fix: 'SHIPPED in timesfm PR #68. coinbase/kraken price extraction gated to if asset_l == "btc".',
    progressNotes: [{ date: '2026-04-12', note: 'timesfm PR #68 merged. Alt-coins no longer poisoned with BTC prices.' }],
  },
  {
    id: 'S5-FIX',
    category: 'data-quality',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'V4 quantiles propagation fix — fallback to forecast cache',
    files: [
      { path: 'app/v4_snapshot_assembler.py', line: 1361, repo: 'novakash-timesfm-repo' },
    ],
    evidence: [
      'V4 timescale envelope returned empty quantiles {} despite V2 having full data.',
      'Race condition: scorer timesfm object empty when forecast cache getter returns during scoring.',
    ],
    fix: 'SHIPPED in timesfm PR #68. Added fallback to assembler forecast_cache_getter when scorer lacks quantiles.',
    progressNotes: [{ date: '2026-04-12', note: 'timesfm PR #68 merged. Quantiles now propagate into V4 envelope.' }],
  },
  {
    id: 'S4-FIX',
    category: 'data-quality',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'Consensus sources S4 — Tiingo + Chainlink API keys wired into deploy',
    files: [
      { path: '.github/workflows/ci.yml', line: 149, repo: 'novakash-timesfm-repo' },
    ],
    evidence: [
      'Tiingo and Chainlink pollers existed but had no API credentials on the Montreal box.',
      'BTC consensus was 3/6 sources. Should be 5/6 after this fix.',
      'CoinGlass mark price remains unavailable (API limitation).',
    ],
    fix: 'SHIPPED in timesfm PR #69. TIINGO_API_KEY + POLYGON_RPC_URL secrets set and templated into deploy .env.',
    progressNotes: [{ date: '2026-04-12', note: 'timesfm PR #69 merged. Next deploy activates tiingo + chainlink consensus. BTC consensus 3/6 → 5/6.' }],
  },
  {
    id: 'FE-REDESIGN-MONITOR',
    category: 'frontend',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Polymarket Monitor — 5-band trading dashboard with Data Health strip',
    files: [
      { path: 'frontend/src/pages/polymarket/Monitor.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/polymarket/components/StatusBar.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/polymarket/components/DataHealthStrip.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/polymarket/components/SignalSurface.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/polymarket/components/GatePipeline.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/polymarket/components/RecentFlow.jsx', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Old frontend had 28+ sidebar entries, no clear hierarchy, broken sources hidden behind green checkmarks.',
      'New Monitor shows honest RED/YELLOW/GREEN per signal source, actual gate values vs thresholds.',
      'Sub-signals labeled "Sequoia v5.2" not "elm". V4 regime now shows HMM classification.',
    ],
    fix: 'SHIPPED in PR #107. 9 new files, 1430 lines. Route /polymarket/monitor. No existing pages modified.',
    progressNotes: [{ date: '2026-04-12', note: 'PR #107 merged. New POLYMARKET section in sidebar. Polls 7 API endpoints every 10s.' }],
  },

  // ── clean-architect: dead code removal ──────────────────────────────────
  {
    id: 'CLEANUP-01',
    category: 'clean-architect',
    severity: 'LOW',
    status: 'OPEN',
    title: 'Dead code removal -- legacy pages, stale version strings, unused imports',
    files: [
      { path: 'frontend/src/pages/Indicators.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/Recommendations.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/Learn.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/AnalysisLibrary.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/Changelog.jsx', line: 1, repo: 'novakash' },
      { path: 'engine/alerts/telegram_v2.py', line: 1, repo: 'novakash' },
      { path: 'engine/strategies/orchestrator.py', line: 680, repo: 'novakash' },
      { path: 'engine/use_cases/evaluate_window.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Pages to retire: /indicators, /recommendations, /learn, /analysis, /changelog -- all folded into Notes page or superseded by Polymarket Monitor + Strategy Lab.',
      'engine/alerts/telegram_v2.py (stale formatters) -- zero imports from any engine/*.py module. Referenced only in docs/CLAUDE.md and docs/ARCHITECTURE.md.',
      'Stale import in orchestrator.py line 680: `elm_client=self._five_min_strategy.timesfm_v2_client` -- uses the old "elm" name after the SQ-01 rename to "prediction_recorder" (PR #105).',
      'engine/use_cases/evaluate_window.py -- the old EvaluateWindowUseCase can be removed once ENGINE_USE_STRATEGY_PORT lands and StrategyPort is stable. Currently behind ENGINE_USE_CLEAN_EVALUATE_WINDOW flag.',
      'Old config pages (frontend/src/pages/Config.jsx or similar) can be retired once CFG-06 (DB-backed config UI) ships.',
      'Layout.jsx sidebar still links /indicators (line 202), /recommendations (line 209), /changelog (line 223) -- routes exist but pages are vestigial.',
      'TWAP_OVERRIDE_ENABLED, TWAP_GAMMA_GATE_ENABLED, TIMESFM_AGREEMENT_ENABLED are permanently disabled -- runtime_config.py still reads them. Consider removing after 30 days of inactivity.',
      'V9_CAPS_ENABLED / V9_SOURCE_AGREEMENT code paths in five_min_vpin.py are dead when V10_DUNE_ENABLED=true (v10 pipeline supersedes). Keep for rollback but mark as legacy.',
    ],
    fix: 'Batch cleanup PR: (1) delete 5 vestigial pages + remove routes from Layout.jsx, (2) delete telegram_v2.py, (3) fix elm_client naming in orchestrator.py, (4) remove permanently-disabled gate flag reads after 30-day grace period.',
    progressNotes: [{ date: '2026-04-12', note: 'Inventory complete. See docs/FEATURE_FLAG_ACTIVATION_GUIDE.md section 3 for the full "do not activate" list.' }],
  },

  // ── ci-cd: feature flag activation ──────────────────────────────────────
  {
    id: 'FLAG-ACTIVATE-01',
    category: 'ci-cd',
    severity: 'HIGH',
    status: 'OPEN',
    title: 'Activate feature flags for 2026-04-11/12 session work',
    files: [
      { path: 'docs/FEATURE_FLAG_ACTIVATION_GUIDE.md', line: 1, repo: 'novakash' },
      { path: '.github/workflows/deploy-engine.yml', line: 206, repo: 'novakash' },
      { path: '.github/workflows/deploy-margin-engine.yml', line: 79, repo: 'novakash' },
      { path: 'engine/config/runtime_config.py', line: 201, repo: 'novakash' },
      { path: 'engine/signals/gates.py', line: 248, repo: 'novakash' },
      { path: 'margin_engine/infrastructure/config/settings.py', line: 69, repo: 'novakash' },
    ],
    evidence: [
      'deploy-engine.yml already templates V10_6_ENABLED=true, V11_POLY_SPOT_ONLY_CONSENSUS=true, ENGINE_USE_CLEAN_EVALUATE_WINDOW=true -- but these need 48h paper-mode validation before live activation.',
      'V10_6_ENABLED (EvalOffsetBoundsGate): blocks trades outside T-90..180 band. 865-outcome evidence shows T-180-240 = 47.62% WR / -33.96% ROI. Gate defaults OFF, operator flips to ON.',
      'V11_POLY_SPOT_ONLY_CONSENSUS (SourceAgreementGate): drops Binance from consensus vote, requires unanimous CL+TI. Eliminates systematic DOWN bias from Binance futures.',
      'ENGINE_USE_CLEAN_EVALUATE_WINDOW (EvaluateWindowUseCase): routes _evaluate_window through the clean-arch use case. 13 unit tests pass. Zero behavior change when flag is off.',
      'MARGIN_V4_MAX_MARK_DIVERGENCE_BPS: DQ-07 mark divergence gate. Default 0.0 = no-op. Set to 20 bps after validation.',
      'MARGIN_V4_MACRO_MODE already safe at "advisory" (default). No action needed unless reverting to "veto".',
      'Full activation guide with interaction matrix: docs/FEATURE_FLAG_ACTIVATION_GUIDE.md',
    ],
    fix: '(1) Confirm deploy-engine.yml templates are landing on Montreal .env via SSH grep. (2) Monitor engine.log for 48h in paper mode. (3) If no regressions, confirm PAPER_MODE=false + LIVE_TRADING_ENABLED=true for live activation. (4) Set MARGIN_V4_MAX_MARK_DIVERGENCE_BPS=20 on margin engine host after 48h observation.',
    progressNotes: [
      { date: '2026-04-12', note: 'Feature flag activation guide written (docs/FEATURE_FLAG_ACTIVATION_GUIDE.md). deploy-engine.yml already templates all "activate after validation" flags. Next step: run 48h paper-mode observation and check telemetry.' },
      { date: '2026-04-12', note: 'All flags activated on Montreal via SSH (paper mode). V10_6_ENABLED, V11_POLY_SPOT_ONLY_CONSENSUS, ENGINE_USE_CLEAN_EVALUATE_WINDOW, TIMESFM_ENABLED, V9_CAPS_ENABLED, ENGINE_USE_STRATEGY_PORT, V4_FUSION_ENABLED, V4_FUSION_MODE=GHOST, FIVE_MIN_MIN_DELTA_PCT=0.001 all set. Engine running with both strategies evaluating.' },
    ],
  },
  // ── 2026-04-12 session 3: venue-aware V4, prediction surface, fixes ─────────
  {
    id: 'FE-REDESIGN-COMPLETE',
    category: 'frontend',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Full Polymarket frontend suite — 6 pages including prediction surface + window analysis',
    files: [
      { path: 'frontend/src/pages/polymarket/Overview.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/polymarket/LiveFloor.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/polymarket/components/WindowAnalysisModal.jsx', line: 1, repo: 'novakash' },
    ],
    evidence: [
      '6 Polymarket pages: Overview, Monitor, Floor, Evaluate, Strategy Lab, Configure.',
      'Overview: prediction accuracy surface by eval_offset, strategy performance cards.',
      'Floor: live BTC price SVG chart + V10/V4 strategy decisions side-by-side.',
      'Window Analysis Modal: per-window prediction timeline, confidence chart, strategy action strips, best entry.',
      'Strategy Lab: gate replay (Tab A) + shadow comparison V10 vs V4 (Tab C) + gate impact (Tab B).',
    ],
    fix: 'SHIPPED across PRs #107, #109, #110, #117, #121, #122, #123. Build passes.',
    progressNotes: [{ date: '2026-04-12', note: 'PRs #121-#123 merged. 3-section nav (Polymarket/Margin/System), light/dark theme. Window Analysis Modal wired into Evaluate + Strategy Lab.' }],
  },
  {
    id: 'V4-POLYMARKET-RECOMMENDATION',
    category: 'data-quality',
    severity: 'HIGH',
    status: 'DONE',
    title: 'V4 surface venue-aware: polymarket_live_recommended_outcome + margin_recommended_action rename',
    files: [
      { path: 'app/v4_snapshot_assembler.py', line: 1628, repo: 'novakash-timesfm-repo' },
      { path: 'engine/adapters/strategies/v4_fusion_strategy.py', line: 1, repo: 'novakash' },
      { path: 'engine/adapters/v4_snapshot_http.py', line: 64, repo: 'novakash' },
    ],
    evidence: [
      'recommended_action was designed for margin engine (quantile SL/TP), wrong for Polymarket binary markets.',
      'New polymarket_live_recommended_outcome block: direction, trade_advised, confidence_distance, regime, timing, extras.',
      'Time-aware: early/optimal/late/expired based on seconds_to_close.',
      'margin_recommended_action is the renamed existing field (backward compat alias kept).',
    ],
    fix: 'SHIPPED in timesfm PR #73. V4FusionStrategy reads polymarket_outcome field. Backward compat preserved.',
    progressNotes: [{ date: '2026-04-12', note: 'timesfm PR #73 merged. Engine parses new field, V4 GHOST now produces real TRADE decisions.' }],
  },
  {
    id: 'V4-ALLOW-ALL-BUG',
    category: 'production-errors',
    severity: 'HIGH',
    status: 'DONE',
    title: 'V4FusionStrategy macro gate bug — ALLOW_ALL incorrectly blocked all directions',
    files: [{ path: 'engine/adapters/strategies/v4_fusion_strategy.py', line: 177, repo: 'novakash' }],
    evidence: [
      '"macro direction_gate=ALLOW_ALL vs UP" was blocking V4 GHOST on every evaluation.',
      'Code compared "ALLOW_ALL" != "UP" → True → SKIP. ALLOW_ALL should always pass.',
      'V4 produced 0 TRADE decisions despite valid signals.',
    ],
    fix: 'SHIPPED direct to develop. Now only LONG_ONLY/SHORT_ONLY filter directions. ALLOW_ALL passes through.',
    progressNotes: [{ date: '2026-04-12', note: 'Fixed and deployed. V4 GHOST now produces action=TRADE when conditions are met.' }],
  },
  {
    id: 'SIGNAL-EVAL-COLD-START',
    category: 'production-errors',
    severity: 'HIGH',
    status: 'DONE',
    title: 'signal_evaluations cold_start — StrategyPort path did not write evaluations',
    files: [{ path: 'engine/use_cases/evaluate_strategies.py', line: 183, repo: 'novakash' }],
    evidence: [
      'StrategyPort path wrote to strategy_decisions but NOT signal_evaluations.',
      'V2 probability endpoint returns 503 when signal_evaluations are stale (>staleness window).',
      '503 → DUNE gate cannot evaluate → V10 blocks on dune_confidence → no paper trades.',
      'Chicken-and-egg: needed signal_evaluations to get probabilities to write signal_evaluations.',
    ],
    fix: 'SHIPPED direct to develop. EvaluateStrategiesUseCase now writes signal_evaluations after V10 evaluation.',
    progressNotes: [{ date: '2026-04-12', note: 'Fixed and deployed. signal_evaluations fresh again (verified via hub API). V2 probability returning 0.600.' }],
  },
  {
    id: 'TELEGRAM-SPAM-FIX',
    category: 'production-errors',
    severity: 'HIGH',
    status: 'DONE',
    title: 'Telegram spam — window summary fired on every 2-second eval tick',
    files: [{ path: 'engine/strategies/orchestrator.py', line: 1585, repo: 'novakash' }],
    evidence: [
      'send_window_summary was called on every strategy evaluation when all strategies skipped.',
      '150 evals per 5-min window × every 2s = 150 Telegram messages per window.',
    ],
    fix: 'SHIPPED direct to develop. Only fires when eval_offset <= 30 (last 30s of window). One summary per window.',
    progressNotes: [{ date: '2026-04-12', note: 'Fixed and deployed. Telegram now gets one window summary at close, not 150 spam messages.' }],
  },
  {
    id: 'PREDICTION-SURFACE',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'Prediction surface: accuracy-by-offset and window analysis for strategy optimization',
    files: [
      { path: 'frontend/src/pages/polymarket/Overview.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/polymarket/components/WindowAnalysisModal.jsx', line: 1, repo: 'novakash' },
      { path: 'hub/api/v58_monitor.py', line: 1, repo: 'novakash' },
      { path: 'docs/superpowers/specs/2026-04-12-window-analysis-design.md', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Aggregates signal_evaluations by 10s eval_offset buckets to show prediction accuracy at each T-offset.',
      'Shows (1) did we guess right? (2) did we trade right? for each strategy at each offset.',
      'Window analysis modal: per-window prediction timeline, best entry point, confidence chart.',
      'Hub endpoints: /v58/prediction-surface and /v58/window-analysis/{window_ts}.',
    ],
    fix: 'SHIPPED in PRs #122 + #123. Prediction surface at /polymarket/overview. Window modal accessible from Evaluate + Strategy Lab.',
    progressNotes: [{ date: '2026-04-12', note: 'PRs #122-#123 merged. Design doc at docs/superpowers/specs/2026-04-12-window-analysis-design.md.' }],
  },
  // ── 2026-04-12 session 4: window analysis deep dive + go-live findings ─────
  {
    id: 'WINDOW-ANALYSIS-01',
    category: 'data-quality',
    severity: 'HIGH',
    status: 'OPEN',
    title: '⚡ LATEST: Magic window T-120-T-150. T-90 cliff confirmed. V4 0W/20L at T-60.',
    files: [
      { path: 'docs/analysis/trading_window_analysis_2026-04-12.md', line: 1, repo: 'novakash' },
      { path: 'docs/analysis/run_window_analysis.py', line: 1, repo: 'novakash' },
      { path: 'engine/adapters/strategies/v4_fusion_strategy.py', line: 158, repo: 'novakash' },
    ],
    evidence: [
      '⚡ CONFIRMED x2 (70,272 windows). Ground truth = close_price vs open_price.',
      'SWEET SPOT T-120 to T-150: accuracy peaks 55.5% at T-135. Trade only strong/high confidence bands (65.1% and 64.3% WR).',
      'CLIFF at T-90: accuracy drops to 48.7% — BELOW 50%. CLOB already priced outcome at T-90. Never trade below T-90.',
      'T-60: 45.3% — actively anti-predictive. V4 paper proved this: 0W/20L, all at T-60, predicting UP while market went DOWN.',
      'CLOB direction asymmetry (T90-150, dist>=0.12): DOWN acc=90.3% EV=+0.330. UP acc=20.8% EV=-0.375.',
      '⚠️ CAVEAT: 84% DOWN dataset (bearish BTC period). 90% WR for DOWN = trend bias + edge combined. Need neutral period test.',
      'IMMEDIATE BUG: V4FusionStrategy ignores poly_timing field. Fires at T-60 (timing=late) even though analysis says never trade below T-90.',
      'Only trade confidence_distance >= 0.12 (strong/high bands). mod=37.7% and weak=32.5% = anti-predictive at all offsets.',
    ],
    fix: '(1) Fix V4FusionStrategy._evaluate_polymarket_v2() to skip when poly_timing==\'late\' or eval_offset<90. (2) Add ClobAskGate: only trade direction when its CLOB ask <= $0.58. (3) Test on neutral BTC period (use ticks_chainlink to identify bull periods). Script: docs/analysis/run_window_analysis.py.',
    progressNotes: [
      { date: '2026-04-12', note: 'First analysis run: sweet spot T-120-T-150 found, bearish dataset caveat flagged.' },
      { date: '2026-04-12', note: '⚡ CONFIRMED x2 on 70,272 windows. V4 paper 0W/20L proves T-60 is wrong. Critical fix: V4FusionStrategy must respect poly_timing=late gate. Run run_window_analysis.py to re-verify after neutral-period data accumulates.' },
      { date: '2026-04-12', note: 'PR #133: V3 composite timestamp issue noted — workaround applied using /v3/snapshot API endpoint instead of ticks_v3_composite join (avoids ts vs window_ts alignment bug).' },
    ],
  },
  {
    id: 'SIGNAL-CLOB-EDGE-GATE',
    category: 'data-quality',
    severity: 'HIGH',
    status: 'OPEN',
    title: 'Improved accuracy: gate on (Sequoia confidence - CLOB implied probability) = true edge',
    files: [
      { path: 'engine/adapters/strategies/v4_fusion_strategy.py', line: 1, repo: 'novakash' },
      { path: 'engine/signals/gates.py', line: 1, repo: 'novakash' },
      { path: 'docs/analysis/run_window_analysis.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Both Sequoia confidence AND CLOB ask converge toward outcome as window closes — but CLOB moves faster (order flow real-time, Sequoia lags).',
      'True edge = gap between what Sequoia knows vs what CLOB has already priced: (p_up - clob_implied_prob) where clob_implied = 1 - clob_up_ask.',
      'At T-60: Sequoia dist=0.213 (more confident) but CLOB ask=0.454 (already priced DOWN). CLOB is AHEAD of signal — no edge.',
      'At T-120: Sequoia 65% UP confidence, CLOB ask=0.58 (implies 58% UP) → 7pp edge → real trade opportunity.',
      'This single gate would filter most T-60 losses: if CLOB implies same/stronger direction than Sequoia, market already knows. Skip.',
      'Gate formula: if predicting UP, only trade when p_up > (1 - clob_up_ask) + threshold. If predicting DOWN, only trade when (1-p_up) > (1 - clob_down_ask) + threshold.',
      'Threshold of 0.03-0.05 (3-5pp) would filter late-window CLOB-efficient entries while keeping T-120-T-150 trades intact.',
    ],
    fix: 'TODO: (1) Analyse historical data: compute (p_up - clob_implied) at each eval. Find threshold that maximises accuracy. (2) Add ClobEdgeGate to V4FusionStrategy: skip when Sequoia edge over CLOB < threshold. (3) Optionally add to V10 gate stack as replacement/supplement for current cap gate. Run run_window_analysis.py with new column.',
    progressNotes: [
      { date: '2026-04-12', note: 'Insight: edge = Sequoia confidence vs CLOB implied prob gap. Both converge toward close but CLOB faster. Gate on the gap not absolute confidence. Proposed as TODO.' },
      { date: '2026-04-12', note: 'PR #133: V3 timestamp alignment issue addressed — V3 composite now reads from /v3/snapshot API endpoint (timescales.5m.composite) instead of ticks_v3_composite join. Avoids ts/window_ts mismatch.' },
    ],
  },
  {
    id: 'V4-TIMING-BUG',
    category: 'production-errors',
    severity: 'CRITICAL',
    status: 'OPEN',
    title: '⚡ V4 ignores poly_timing=late — fires at T-60 when analysis says never trade below T-90',
    files: [
      { path: 'engine/adapters/strategies/v4_fusion_strategy.py', line: 158, repo: 'novakash' },
    ],
    evidence: [
      'V4 paper trading: 0W/20L in first 6h. All trades at T-60 predicting UP while market went DOWN.',
      'Root cause: _evaluate_polymarket_v2() checks trade_advised=True but ignores poly_timing field.',
      'polymarket_live_recommended_outcome.timing returns "early"/"optimal"/"late"/"expired" based on seconds_to_close.',
      'Analysis confirmed: accuracy at T-60=45.3% (anti-predictive), T-90=48.7% (below 50%). Only T-120-T-150 has edge.',
      'At T-60, CLOB has already priced outcome. Sequoia signal lags market — predicts UP while smart money already pushed price DOWN.',
    ],
    fix: 'In _evaluate_polymarket_v2(): add check before trade — if poly.get("timing") in ("late","expired","early"): return self._skip("polymarket: timing={timing} out of window"). Only trade when timing=="optimal". One-line fix.',
    progressNotes: [
      { date: '2026-04-12', note: '⚡ CRITICAL — V4 burning paper money at wrong offset. Fix immediately before V4 goes live.' },
    ],
  },
  {
    id: 'CA-EXEC-INDEPENDENCE',
    category: 'clean-architect',
    severity: 'MEDIUM',
    status: 'OPEN',
    title: 'V4FusionStrategy still uses five_min_vpin._execute_trade (CA-01 Phase 4)',
    files: [
      { path: 'engine/strategies/five_min_vpin.py', line: 261, repo: 'novakash' },
      { path: 'engine/adapters/strategies/v4_fusion_strategy.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'V4 strategy decision goes through _sp_trade_decision shortcut into five_min_vpin._execute_trade.',
      'Execution is functional but architecturally coupled — V4 cannot have its own execution behaviour.',
      'ExecuteTradeUseCase needs to be extracted from five_min_vpin (CA-01 Phase 4 in design doc).',
      'ClobAskGate not yet a first-class V4 gate — needs to be added to V4FusionStrategy.',
    ],
    fix: 'Extract _execute_trade into ExecuteTradeUseCase (CA-01 Phase 4). Add ClobAskGate and ConfidenceDistanceGate to V4FusionStrategy. See docs/superpowers/specs/2026-04-12-strategy-port-design.md Phase 4.',
    progressNotes: [{ date: '2026-04-12', note: '_sp_trade_decision shortcut makes V4 functional for live trading but not fully independent.' }],
  },
  // ── FE-MONITOR-01: Monitor page 5 bugs diagnosed 2026-04-12 ──────────────
  {
    id: 'FE-MONITOR-01',
    category: 'frontend',
    severity: 'HIGH',
    status: 'OPEN',
    title: 'Monitor page: 5 bugs — no dual-strategy in timeline, NO DATA sub-signals, double gates, bankroll stale',
    files: [
      { path: 'frontend/src/pages/polymarket/components/RecentFlow.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/polymarket/components/SignalSurface.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/polymarket/components/GatePipeline.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/polymarket/components/DataHealthStrip.jsx', line: 1, repo: 'novakash' },
      { path: 'frontend/src/pages/polymarket/components/StatusBar.jsx', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Bug 1: RecentFlow reads window_snapshots via /api/v58/outcomes — shows V10 only. strategy_decisions (dual V10+V4) never wired into RecentFlow.',
      'Bug 2: Sequoia v5.2 NO DATA + sub-signals all "—". V4 snapshot nests sub_signals under timescales.5m.sub_signals but SignalSurface reads v4Snapshot.sub_signals directly — parse miss.',
      'Bug 3: Gate pipeline shows 8 gates twice. GatePipeline.jsx renders hardcoded 8-gate strip AND dynamic gate_results from heartbeat — two loops both fire.',
      'Bug 4: SRC Agreement NO DATA in Data Health Strip but PASS in gate pipeline. Health strip reads V4 consensus, gate pipeline reads V10 heartbeat — two different sources disagree.',
      'Bug 5: Bankroll shows $93.06 but actual CLOB wallet = $19.37. StatusBar reads system_state.bankroll from DB (stale). Needs sync to on-chain USDC balance.',
    ],
    fix: 'FE-MONITOR-01a: Wire strategy_decisions into RecentFlow (add V4 column, show LIVE/GHOST side-by-side). FE-MONITOR-01b: Fix SignalSurface to read timescales.5m.sub_signals from V4 snapshot. FE-MONITOR-01c: Fix GatePipeline double render — deduplicate gate lists. FE-MONITOR-01d: Unify SRC Agreement source in DataHealthStrip. FE-MONITOR-01e: Sync bankroll to on-chain CLOB wallet balance via PolymarketClient.get_balance_allowance() on startup.',
    progressNotes: [
      { date: '2026-04-12', note: 'Diagnosed from screenshot. All 5 bugs identified with root cause. Not yet fixed.' },
      { date: '2026-04-12', note: 'PR #133: FE-MONITOR-01a–01e all fixed. DataHealthStrip p_up uses 4-path fallback chain (v4Snapshot.timescales.5m.probability_up → flat → heartbeat → window). V3 composite reads v3Snapshot.timescales.5m.composite from /v3/snapshot API. Sub-signals, gate dedup, bankroll, SRC agreement also resolved.' },
    ],
  },
  {
    id: 'SIG-01',
    category: 'signal-optimization',
    severity: 'CRITICAL',
    status: 'IN_PROGRESS',
    title: 'CLOB feed fix deployed — V4 now has CLOB data in paper mode',
    files: [
      { path: 'engine/data/feeds/clob_feed.py', line: 63, repo: 'novakash' },
      { path: 'engine/execution/polymarket_client.py', line: 153, repo: 'novakash' },
    ],
    evidence: [
      'Bug: CLOB feed had self._poly.paper_mode check — completely disabled in paper mode (default)',
      'Result: CLOB data only 5.8% coverage in signal_evaluations',
      'V4 placed 503 trades in 4h with ALL CLOB ask NULL — trading blind',
      'CLOB feed now works in paper mode via get_clob_order_book() method',
    ],
    fix: 'FIXED: Removed paper_mode check from CLOBFeed._poll(). Added PolymarketClient.get_clob_order_book() that works in both paper and live mode. Expected CLOB coverage: 80-90% at T-120-150.',
    progressNotes: [
      { date: '2026-04-12', note: 'FIXED in PR #__. Deployed to worktree/audit. CLOB feed now runs in paper mode. Expected WR improvement: 51% → 65%+.' },
    ],
  },
  {
    id: 'SIG-02',
    category: 'signal-optimization',
    severity: 'CRITICAL',
    status: 'OPEN',
    title: 'DOWN-ONLY strategy: 99% WR on contrarian DOWN predictions',
    files: [
      { path: 'docs/analysis/DOWN_ONLY_STRATEGY_2026-04-12.md', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Analysis of 897,503 signal evaluations (T-90-150, conf>=0.12)',
      'DOWN predictions with clob_ask >0.75: 99.0% WR (175,261 trades)',
      'DOWN predictions all ranges: 76-99% WR (451,873 trades)',
      'UP predictions all ranges: 1.5-53% WR (345,630 trades)',
      'Retail traders have strong UP bias — DOWN tokens overpriced',
    ],
    fix: 'RECOMMENDATION: Trade DOWN ONLY. Skip ALL UP predictions. Size 2.0x for contrarian (clob_ask >=0.75). Expected WR: 76-99%. See docs/analysis/DOWN_ONLY_STRATEGY_2026-04-12.md for full analysis.',
    progressNotes: [
      { date: '2026-04-12', note: 'CRITICAL FINDING: DOWN-only strategy has 99% WR at contrarian ranges. UP predictions are unprofitable (1.5-53% WR). This is an exploit of retail UP bias, not a model signal. Requires V4 strategy update to implement DOWN-only filter.' },
    ],
  },
  {
    id: 'SIG-03',
    category: 'signal-optimization',
    severity: 'HIGH',
    status: 'OPEN',
    title: 'DirectionFilterGate: skip all UP predictions (1.5-53% WR)',
    files: [
      { path: 'engine/signals/gates.py', line: 1, repo: 'novakash' },
      { path: 'engine/strategies/five_min_vpin.py', line: 850, repo: 'novakash' },
    ],
    evidence: [
      '897,503-sample analysis (2026-04-12): UP predictions have 1.5–53% WR across all CLOB bands',
      'DOWN predictions have 76–99% WR across all CLOB bands',
      'Existing gate pipeline (gates.py) is pluggable — new gates implement Gate protocol',
      'DirectionFilterGate is ~20 lines: if direction == UP: return GateResult(passed=False)',
    ],
    fix: 'Add DirectionFilterGate to engine/signals/gates.py. Insert as G1.5 (after SourceAgreementGate, before DeltaMagnitudeGate). Add V4_DOWN_ONLY env flag for soft rollout. See docs/analysis/DOWN_ONLY_STRATEGY_2026-04-12.md for implementation spec.',
    progressNotes: [
      { date: '2026-04-12', note: 'Gate design specified in DOWN_ONLY_STRATEGY_2026-04-12.md. Architecture: plugs into existing Gate protocol in gates.py, ~20 lines. TODO: implement and wire into V4 pipeline.' },
    ],
  },
  {
    id: 'SIG-03b',
    category: 'signal-optimization',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'v4_down_only timing override: trade T-90 to T-150 window (90.3% WR validated)',
    files: [
      { path: 'engine/adapters/strategies/v4_down_only_strategy.py', line: 70, repo: 'novakash' },
      { path: 'engine/adapters/strategies/v4_fusion_strategy.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      '897K-sample analysis validated 90.3% WR for DOWN predictions at T-90 to T-150 eval_offset',
      'V4FusionStrategy parent class has timing check that blocks at T-180 (timing="early" until T-180)',
      'Polymarket API returns timing="early" until T-180, but v4_down_only wants to trade at T-150',
      'Parent blocks before v4_down_only can apply its T-90 to T-150 window',
      'Root cause: V4FusionStrategy._evaluate_polymarket_v2() checks timing field and skips "early"/"expired"',
    ],
    fix: 'SHIPPED — Created timing override in v4_down_only_strategy.py (commit d8b1856). Detects "timing=early" skip from parent, re-evaluates as TRADE when 90 <= eval_offset <= 150. Deployed to Montreal server (15.223.247.178) with V10_6_MAX_EVAL_OFFSET=150. Engine running in paper mode, evaluating at T-100 to T-92. Waiting for DOWN signal in T-90 to T-150 window to see actual TRADE.',
    progressNotes: [
      { date: '2026-04-13', note: 'SHIPPED via timing override in v4_down_only_strategy.py (lines 70-114). Parent V4FusionStrategy blocks at T-180 with timing="early", v4_down_only overrides to allow T-90 to T-150 trading. Committed to develop (d8b1856), deployed to Montreal. V10_6_MAX_EVAL_OFFSET updated from 120 to 150. Engine evaluating correctly at T-100 to T-92, waiting for DOWN signal to execute TRADE.' },
      { date: '2026-04-13', note: 'Timing override hack will be eliminated by CA-07 — each strategy owns its TimingGate independently in the config-first system.' },
    ],
  },
  {
    id: 'SIG-04',
    category: 'signal-optimization',
    severity: 'HIGH',
    status: 'OPEN',
    title: 'CLOBSizingGate: size UP to 2x based on clob_down_ask (76–99% WR)',
    files: [
      { path: 'engine/signals/gates.py', line: 1, repo: 'novakash' },
      { path: 'engine/strategies/five_min_vpin.py', line: 850, repo: 'novakash' },
    ],
    evidence: [
      'clob_down_ask >= 0.75: 99% WR (175K samples) — both model and market agree DOWN',
      'clob_down_ask 0.55–0.75: 98% WR (112K samples)',
      'clob_down_ask 0.35–0.55: 92% WR (87K samples)',
      'clob_down_ask < 0.35: 76% WR (177K samples) — genuine contrarian',
      'CLOB data now available in paper mode after PR #136 fix',
    ],
    fix: 'Add CLOBSizingGate to engine/signals/gates.py. Insert as G6.5 (after SpreadGate). Sets ctx.size_modifier: 2.0x at >=0.75, 1.5x at 0.55–0.75, 1.2x at 0.35–0.55, 1.0x below. Add size_modifier field to GateContext. See docs/analysis/DOWN_ONLY_STRATEGY_2026-04-12.md.',
    progressNotes: [
      { date: '2026-04-12', note: 'Sizing schedule determined from 897K sample analysis. Gate design specified in DOWN_ONLY_STRATEGY_2026-04-12.md. Requires SIG-03 (DirectionFilterGate) to be implemented first — sizing only meaningful for DOWN predictions.' },
      { date: '2026-04-13', note: 'CLOBSizingGate now part of reusable gate library in CA-07. Implemented as engine/strategies/gates/clob_sizing.py.' },
    ],
  },
  {
    id: 'MULTI-ACCOUNT-01',
    category: 'signal-optimization',
    severity: 'MEDIUM',
    status: 'OPEN',
    title: 'Multi-Polymarket-account support: run multiple LIVE strategies in parallel',
    files: [
      { path: 'frontend/src/pages/polymarket/StrategyLab.jsx', line: 56, repo: 'novakash' },
      { path: 'engine/strategies/orchestrator.py', line: 365, repo: 'novakash' },
      { path: 'engine/execution/polymarket_client.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Currently enforced: only 1 LIVE strategy per Polymarket account (prevents duplicate order placement)',
      'StrategyLab UI blocks setting a second strategy to LIVE with an inline error message',
      'Each strategy shares the same PolymarketClient, so LIVE=two strategies = two order sets on one account',
      'Fix: instantiate a separate PolymarketClient per strategy, each with its own wallet credentials',
    ],
    fix: 'Add multi-account config: POLY_ACCOUNT_1_KEY, POLY_ACCOUNT_2_KEY, POLY_ACCOUNT_3_KEY. Map each strategy to an account in trading_configs. Instantiate separate PolymarketClient per registered strategy in orchestrator. Each account manages its own order flow independently. Then allow multiple LIVE strategies in StrategyLab UI.',
    progressNotes: [
      { date: '2026-04-12', note: 'Single-LIVE enforcement added to StrategyLab UI (StrategyConfigPanel). Tooltip explains the restriction. This task tracks the multi-account work needed to lift it.' },
    ],
  },

  // ── Strategy Engine v2 (2026-04-13) ─────────────────────────────────────

  // CA-07: Strategy Engine v2
  {
    id: 'CA-07',
    category: 'clean-architect',
    severity: 'CRITICAL',
    status: 'IN_PROGRESS',
    title: 'Strategy Engine v2 — config-first registry replaces inheritance chain',
    files: [
      { path: 'engine/strategies/registry.py', line: 1, repo: 'novakash' },
      { path: 'engine/strategies/data_surface.py', line: 1, repo: 'novakash' },
      { path: 'engine/strategies/gates/', line: 1, repo: 'novakash' },
      { path: 'engine/strategies/configs/', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'V4FusionStrategy inheritance chain causes timing override hacks (SIG-03b)',
      'V4 snapshot fetched via new HTTP session per eval tick (100-5000ms blocking)',
      'V3 composites not wired into StrategyContext (hardcoded None)',
      'Adding new strategies requires Python class inheritance, not config',
      'v4_up_asian broken (0 trades) — would be a YAML config fix with new system',
    ],
    fix: 'Config-first strategy registry with YAML definitions, reusable gate library (16 gates), FullDataSurface frozen dataclass, DataSurfaceManager background pre-fetch. Design spec: docs/superpowers/specs/2026-04-13-strategy-engine-v2-design.md',
    progressNotes: [
      { date: '2026-04-13', note: 'Design spec written. Implementation starting in feat/strategy-engine-v2 branch.' },
      { date: '2026-04-13', note: 'SHIPPED — PRs #157 (engine: 40 files, 14 gates, 5 configs, 86 tests), #158 (frontend: shared constants, 3 new pages), #160 (ExecuteTradeUseCase: 6 files, 21 tests). Deployed to Montreal. All 5 strategies evaluating in GHOST. v4_down_only + v4_fusion producing TRADE decisions aligned with old system.' },
    ],
  },
  {
    id: 'SCHEMA-03',
    category: 'data-quality',
    severity: 'MEDIUM',
    status: 'DONE',
    title: 'window_snapshots missing actual_direction column — oracle resolution broken',
    files: [
      { path: 'engine/persistence/db_client.py', line: 1265, repo: 'novakash' },
      { path: 'migrations/add_actual_direction_to_window_snapshots.sql', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Engine log: db.get_oracle_outcome_failed error="column actual_direction does not exist"',
      'Column referenced by get_oracle_outcome(), orchestrator resolution alerts, Telegram outcome analysis',
      'Shadow resolution cannot record oracle direction without this column',
    ],
    fix: 'SHIPPED — ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS actual_direction TEXT; Applied to Railway PostgreSQL. No engine restart needed.',
    progressNotes: [
      { date: '2026-04-13', note: 'Discovered during Strategy Engine v2 deployment. Migration applied directly to Railway DB. Error stopped immediately.' },
    ],
  },

  // CA-08: Data Surface Layer
  {
    id: 'CA-08',
    category: 'clean-architect',
    severity: 'HIGH',
    status: 'IN_PROGRESS',
    title: 'Data Surface Layer — 1Hz fresh in-memory cache eliminates blocking I/O',
    files: [
      { path: 'engine/strategies/data_surface.py', line: 1, repo: 'novakash' },
      { path: 'engine/data/feeds/tiingo_feed.py', line: 1, repo: 'novakash' },
      { path: 'engine/data/feeds/chainlink_feed.py', line: 1, repo: 'novakash' },
      { path: 'engine/data/feeds/clob_feed.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'V4 snapshot: 100-5000ms blocking HTTP per eval (new aiohttp session each call)',
      'Tiingo delta: DB query per eval instead of in-memory read',
      'Chainlink delta: DB query per eval (5-35s stale)',
      'CLOB bid/ask: DB query per eval instead of in-memory read',
      'V3 composites: missing entirely (hardcoded None in context)',
      'Total _build_context latency: 200-5000ms → target <5ms',
    ],
    fix: 'Persistent HTTP session + 2s background V4 pre-fetch. In-memory caches on Tiingo/Chainlink/CLOB feeds. V3 composites extracted from V4 snapshot. Zero I/O at decision time.',
    progressNotes: [
      { date: '2026-04-13', note: 'Data freshness analysis complete. Implementation starting with feed in-memory caches.' },
    ],
  },

  {
    id: 'CA-10',
    category: 'clean-architect',
    severity: 'HIGH',
    status: 'OPEN',
    title: 'Decommission legacy execution — remove FiveMinVPINStrategy._execute_trade (536 LOC)',
    files: [
      { path: 'engine/strategies/five_min_vpin.py', line: 3178, repo: 'novakash' },
      { path: 'engine/strategies/orchestrator.py', line: 736, repo: 'novakash' },
      { path: 'engine/use_cases/evaluate_window.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Old FiveMinVPINStrategy._execute_trade is 536 LOC of coupled execution logic',
      'Strategy Engine v2 ExecuteTradeUseCase replaces it with clean 10-step flow (PR #160)',
      'LEGACY_EXECUTION_DISABLED=true gates the old path — new registry is sole executor',
      'Old SITREP still shows legacy v2.2 labels instead of strategy names',
      'five_min_vpin.py is 3600+ LOC god class — after removing _execute_trade it drops to ~3000',
      'Old orchestrator._sp_trade_decision shortcut can be removed once validated',
    ],
    fix: 'After 1 week of v2 paper validation with zero mismatches: (1) Remove _execute_trade + _sp_trade_decision, (2) Update SITREP to read from registry decisions, (3) Remove LEGACY_EXECUTION_DISABLED flag, (4) Update Telegram alerts to use new format exclusively.',
    progressNotes: [
      { date: '2026-04-13', note: 'LEGACY_EXECUTION_DISABLED flag added (commit 7505a89). New ExecuteTradeUseCase deployed (PR #160). Both systems running in parallel for validation.' },
    ],
  },

  // CA-09: Domain layer reconciliation
  {
    id: 'CA-09',
    category: 'clean-architect',
    severity: 'HIGH',
    status: 'OPEN',
    title: 'Domain layer reconciliation — delete duplicates, merge worktree types',
    files: [
      { path: 'engine/domain/value_objects.py', line: 1, repo: 'novakash' },
      { path: 'engine/application/ports/', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'domain/value_objects.py (root) and domain/value_objects/ (package) define same types differently',
      'EvaluateStrategiesResult is mutable in root file, frozen in package — reconciliation hazard',
      'application/ports/ (7 files) duplicates domain/ports.py and is imported by nothing',
      '10 stub VOs with pass bodies need real fields',
    ],
    fix: 'Delete root value_objects.py (or convert to re-exports). Delete application/ports/. Merge worktree domain types with develop canonical types.',
    progressNotes: [],
  },

  // SIG-05: v4_up_basic
  {
    id: 'SIG-05',
    category: 'signal-optimization',
    severity: 'HIGH',
    status: 'IN_PROGRESS',
    title: 'v4_up_basic strategy — global UP, dist>=0.10, T-60-180, all hours',
    files: [
      { path: 'engine/strategies/configs/v4_up_basic.yaml', line: 1, repo: 'novakash' },
      { path: 'engine/strategies/configs/v4_up_basic.md', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'v4_up_asian has 0 trades from 19,490 decisions — thresholds too restrictive',
      'All UP signals in 0.60-0.65 range, current threshold requires >= 0.62',
      'Non-Asian hours have 5x more high-confidence UP signals than Asian session',
      'Expected: 70-80% WR, 5-15 trades/day',
    ],
    fix: 'Config-defined strategy: UP direction, T-60 to T-180 timing, dist >= 0.10 confidence, all hours. Deploy as GHOST first for 3-5 days paper validation.',
    progressNotes: [
      { date: '2026-04-13', note: 'Spec in docs/V4_UP_BASIC_STRATEGY.md. Implementation via strategy engine v2 config system.' },
    ],
  },

  // SIG-06: v4_up_asian fix
  {
    id: 'SIG-06',
    category: 'signal-optimization',
    severity: 'MEDIUM',
    status: 'IN_PROGRESS',
    title: 'v4_up_asian fix — relax thresholds via config (dist 0.10-0.20, was 0.15-0.20)',
    files: [
      { path: 'engine/strategies/configs/v4_up_asian.yaml', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'Current dist >= 0.15 eliminates 100% of available signals (all in 0.60-0.65 range)',
      'Relaxing to dist >= 0.10 captures 88.9% of signals',
      'Strategy engine v2 makes this a YAML config change, no Python code needed',
    ],
    fix: 'Update v4_up_asian.yaml: min_dist from 0.15 to 0.10. Deploy as GHOST for validation.',
    progressNotes: [
      { date: '2026-04-13', note: 'Threshold analysis complete. Fix is a single YAML field change in strategy engine v2.' },
    ],
  },

  // DATA-FRESH-01: V3 enablement
  {
    id: 'DATA-FRESH-01',
    category: 'data-quality',
    severity: 'HIGH',
    status: 'OPEN',
    title: 'Enable V3 on timesfm service — 7 sub-signals + 9 timescale composites currently zeroed',
    files: [
      { path: 'app/main.py', line: 142, repo: 'novakash-timesfm-repo' },
    ],
    evidence: [
      'V3_ENABLED=false in production — all sub-signals, composite, cascade fields are zero/empty',
      'HMM regime classifier running without its 7-signal input ensemble',
      'Regime gate decisions degraded without V3 data',
    ],
    fix: 'Set V3_ENABLED=true on timesfm service environment. Monitor for stability. V3 data will flow into FullDataSurface via V4 snapshot.',
    progressNotes: [],
  },

  // ── ml-training-data ─────────────────────────────────────────────────────

  {
    id: 'ML-01',
    category: 'ml-training-data',
    title: 'Primary label source: window_snapshots + trades',
    severity: 'INFO',
    status: 'OPEN',
    file: 'engine/persistence/db_client.py',
    summary:
      'window_snapshots is the core ML table (~80+ columns). outcome, poly_winner, pnl_usd are the label columns. trades.outcome (WIN/LOSS) is the trade-level label. Both join on (asset, window_ts). Resolution lag: 1h–24h depending on Polymarket market type.',
    symptoms: [
      'window_snapshots: delta_pct, vpin, regime, confidence, gate columns + resolved outcome',
      'trades: entry_price, stake_usd, fill_price, pnl_usd, outcome, execution_mode',
      'gate_audit: per-gate pass/fail, skip_reason, decision — per window',
      'strategy_decisions: per-strategy action, direction, confidence, skip_reason (v2 lab)',
    ],
    fix: 'Join window_snapshots + gate_audit on (asset, window_ts) → parquet. Filter outcome IS NOT NULL. Target 500+ rows per Δ bucket.',
    progressNotes: ['Explored 2026-04-13. ~53 usable labeled rows as of Apr 5. Need ~500+ for LightGBM retraining.'],
  },
  {
    id: 'ML-02',
    category: 'ml-training-data',
    title: 'Feature corpus: 43+ columns across 6 tick sources',
    severity: 'INFO',
    status: 'OPEN',
    file: 'engine/data/',
    summary:
      'Training features come from 6 tick tables polled at different cadences. All stored in Railway PostgreSQL. Main sources: ticks_binance (1s, VPIN), ticks_coinglass (10s, OI/liq/funding), ticks_tiingo (2s, bid/ask), ticks_chainlink (5s, oracle price), ticks_gamma (5m, Polymarket implied), ticks_timesfm (1s, v1 forecast).',
    symptoms: [
      'ticks_binance: price, quantity, is_buyer_maker, vpin — 1Hz',
      'ticks_coinglass: oi_usd, liq_long_usd, liq_short_usd, taker_buy_usd, funding_rate, long_short_ratio — 10s',
      'ticks_tiingo: bid_price, ask_price, last_price — 2s per asset',
      'ticks_chainlink: price (oracle, Polygon) — 5s',
      'ticks_gamma: up_price, down_price, token_ids — per window',
      'ticks_timesfm: direction, confidence, p10/p50/p90, spread — 1s (v1 model output)',
    ],
    fix: 'Feature engineering: align all tick sources to window_ts by backward-looking window join. Use feature_freshness_ms to filter stale features.',
    progressNotes: [],
  },
  {
    id: 'ML-03',
    category: 'ml-training-data',
    title: 'Prediction surfaces: v1→v4 all recorded',
    severity: 'INFO',
    status: 'OPEN',
    file: 'engine/data/feeds/prediction_recorder.py',
    summary:
      'v1 (TimesFM 2.5, 1s): ticks_timesfm. v2 (LightGBM per-Δ-bucket, 30s): ticks_elm_predictions + ticks_v2_probability. v3 (composite 9-timescale): ticks_v3_composite. v4 (full decision surface 96+ cols): ticks_v4_decision. All keyed on (asset, window_ts, seconds_to_close). v2 artifacts in S3 as diffable LightGBM .txt + isotonic .json.',
    symptoms: [
      'v1 output: direction, confidence, predicted_close, p10/p50/p90, spread',
      'v2 output: probability_up (calibrated), probability_raw, model_version, features JSONB',
      'v3 output: composite_score, elm/cascade/taker/oi/funding/vpin/momentum signals',
      'v4 output: action_side, action_conviction, edge_bps, macro_bias, poly_implied_prob_up, liq_pressure',
      'S3 artifacts: s3://bbrnovakash-models-do-not-delete/v2/btc/current.json + per-Δ booster + isotonic',
    ],
    fix: 'v2 retraining pipeline ready (training/build_dataset.py → train_lgb.py → calibration.py → upload). Run when 500+ labeled rows available.',
    progressNotes: ['v4 decision surface has 96+ columns. Full audit in docs/DATA_ARCHITECTURE_AUDIT_2026-04-11.md'],
  },
  {
    id: 'ML-04',
    category: 'ml-training-data',
    title: 'Training gap: ~53 labeled rows (need 500+)',
    severity: 'HIGH',
    status: 'OPEN',
    file: 'training/build_dataset.py',
    summary:
      'As of 2026-04-05, only ~53 usable window-outcome pairs exist (smoke-test threshold). LightGBM walk-forward training requires 500+ per Δ bucket (30/60/90/120/180/240s) for statistical significance. At ~288 windows/day × 30 days = ~8,640 windows/month, full training corpus expected late Apr / early May 2026.',
    symptoms: [
      'Current training: smoke-test mode only (meta_*.json has warning field)',
      'Walk-forward splits need 60% train / 20% val / 20% test (chronological)',
      'Base rate computed on train split only — no peeking',
      'Gate: accuracy > base_rate + margin before --promote allowed',
    ],
    fix: 'Continue live data collection. Run training/build_dataset.py --dry-run weekly to track row count. Trigger full retrain at 500+ rows.',
    progressNotes: ['Estimated date: late April 2026 for initial reliable v2 retrain.'],
  },
  {
    id: 'ML-05',
    category: 'ml-training-data',
    title: 'Gate audit trail: per-gate pass/fail available for feature engineering',
    severity: 'INFO',
    status: 'OPEN',
    file: 'engine/persistence/db_client.py',
    summary:
      'gate_audit table records which of 5 gates passed/failed per window: gate_vpin, gate_delta, gate_cg, gate_floor, gate_cap. gate_failed gives first blocking gate name. skip_reason gives text reason. This is a rich training signal: skip_reason is predictable from pre-trade features and can improve gate threshold tuning.',
    symptoms: [
      'gate_audit: gate_vpin, gate_delta, gate_cg, gate_floor, gate_cap (BOOL each)',
      'gate_failed: name of first failed gate (categorical label)',
      'decision: TRADE / SKIP',
      'skip_reason: text description',
      'window_ts + asset + timeframe: join key',
    ],
    fix: 'Train multi-class classifier: features → skip_reason. Analyze: which gate blocks most trades? Which features predict gate failure? Use for threshold tuning.',
    progressNotes: [],
  },
  {
    id: 'ML-06',
    category: 'ml-training-data',
    title: 'Outcome reconciliation: paper trades now resolved via oracle (PR #162)',
    severity: 'INFO',
    status: 'DONE',
    file: 'engine/use_cases/reconcile_positions.py',
    summary:
      'Paper trades previously sat in OPEN status with no WIN/LOSS outcome — contaminating win rate calculations. PR #162 (merged 2026-04-13) wires ReconcilePositionsUseCase: paper trades resolve via window_snapshots.actual_direction (Chainlink oracle). Live trades resolve via Polymarket CLOB positions API (ENGINE_USE_RECONCILE_UC=true). Outcome data from paper trading is now clean and usable as ML labels.',
    symptoms: [
      'engine/use_cases/reconcile_positions.py: execute() + _resolve_paper_batch()',
      'Paper path: actual_direction (oracle) → WIN if trade.direction matches',
      'Live path: PositionOutcome from Polymarket API → resolve_one()',
      'Cadence: every 2 minutes via _sot_reconciler_loop',
    ],
    fix: 'Already fixed. Monitor reconcile_uc.complete log events on Montreal. Paper trade outcomes now flow into trades.outcome for ML label collection.',
    progressNotes: ['PR #162 merged 2026-04-13. ENGINE_USE_RECONCILE_UC=true in deploy-engine.yml.'],
  },
  {
    id: 'ML-07',
    category: 'ml-training-data',
    title: 'Missing: cross-market alignment, Deribit/Bybit liquidations, feature SLAs',
    severity: 'MEDIUM',
    status: 'OPEN',
    file: 'engine/data/',
    summary:
      'Three data gaps limit ML feature quality: (1) Margin venue orderflow not in ticks_* tables. (2) Deribit/Bybit liquidations only partial — CoinGlass Binance-only. (3) No explicit feature freshness SLA tracking beyond ad-hoc feature_freshness_ms JSONB. Also: ticks_coinglass 10s cadence may be too coarse for T-30 Δ-bucket predictions.',
    symptoms: [
      'No ticks_margin or ticks_hyperliquid tables',
      'CoinGlass limited to Binance perpetuals',
      'feature_freshness_ms exists in some tables but not standardized across all',
      'Chainlink oracle lag not explicitly measured',
    ],
    fix: 'Phase 3b: add Hyperliquid orderflow adapter. Phase 4: add Deribit/Bybit via CoinGlass multi-exchange API. Standardize feature_freshness_ms across all tick tables.',
    progressNotes: [],
  },

  // ── ml-training-data: TimesFM depth audit ────────────────────────────────

  {
    id: 'ML-08',
    category: 'ml-training-data',
    title: 'TimesFM depth audit: context 2048→8192 (1-line change, +1-3pp)',
    severity: 'HIGH',
    status: 'DONE',
    file: 'novakash-timesfm-repo/app/main.py:103',
    summary:
      'TimesFM 2.5 currently uses 2048 ticks (34 min context). Model supports 8192 (137 min). One-line change: max_context=8192 in main.py + extend price_feed buffer_size. Captures longer regime transitions. Cost: ~200ms extra latency (700→900ms), acceptable for 1Hz refresh.',
    symptoms: [
      'main.py:103 — max_context=2048 (hardcoded)',
      'price_feed.py — buffer_size matches context length',
      'Model capacity: 8192 ticks with same arch, just more memory',
      '137 min captures full intra-day regime cycles vs 34 min current',
    ],
    fix: 'Change max_context=8192, extend buffer_size. Profile CPU/memory. Deploy to Montreal. A/B shadow comparison.',
    progressNotes: ['Identified 2026-04-13. Highest ROI change — trivial effort, measurable gain.'],
  },
  {
    id: 'ML-09',
    category: 'ml-training-data',
    title: 'TimesFM: dynamic horizon already works (NOT fixed 60s)',
    severity: 'INFO',
    status: 'DONE',
    file: 'novakash-timesfm-repo/app/forecaster.py',
    summary:
      'CLARIFICATION: TimesFM already predicts dynamically to close. Each call passes horizon=seconds_to_close — at T-180 it forecasts 180 steps, at T-60 it forecasts 60 steps. This is correct and working. The multi-horizon ENSEMBLE idea is different: at T-180, run BOTH 180-step and 60-step forecasts simultaneously to get agreement signals (short-range vs long-range consensus).',
    symptoms: [
      'forecaster.py: forecast(horizon=seconds_to_close) — dynamic per call',
      'v2_scorer.py: passes seconds_to_close from window state',
      'timesfm_client.py: get_forecast(seconds_to_close=N) — capped at 600s',
      'NOT a fixed horizon — adapts to where we are in the window',
    ],
    fix: 'Current behavior is correct. Multi-horizon ensemble (run 2-3 horizons per call for agreement signal) is a separate enhancement tracked as ML-12.',
    progressNotes: ['Confirmed 2026-04-13. Dynamic horizon working as designed.'],
  },
  {
    id: 'ML-10',
    category: 'ml-training-data',
    title: 'TimesFM: fine-tuning on 2088+ Polymarket windows (+4-8pp)',
    severity: 'HIGH',
    status: 'OPEN',
    file: 'novakash-timesfm-repo/training/',
    summary:
      'TimesFM 2.5 supports HuggingFace fine-tuning. We have 2088+ labeled Polymarket 5-min windows in Railway. Zero-shot gives 58% direction accuracy. Fine-tuned on our domain data: estimated +4-8pp (per TimesFM paper domain adaptation results). This is the biggest accuracy unlock and nobody else has this labeled dataset.',
    symptoms: [
      'Current: pure zero-shot, no domain adaptation',
      '2088+ labeled windows available (window_snapshots with outcome)',
      'Split: 1500 train / 400 val / 188 test (walk-forward)',
      'PyTorch fine-tuning supported by timesfm library',
      'Inference stays on CPU (c6a.xlarge) — fine-tune offline',
    ],
    fix: '1. Export labeled windows from Railway. 2. Implement fine-tuning loop (HF examples). 3. Validate on holdout. 4. Deploy fine-tuned weights alongside base. 5. A/B shadow mode. Estimate: 2-3 weeks.',
    progressNotes: [],
  },
  {
    id: 'ML-11',
    category: 'ml-training-data',
    title: 'TimesFM: exogenous covariates (CoinGlass/Gamma into model, +2-7pp)',
    severity: 'MEDIUM',
    status: 'OPEN',
    file: 'novakash-timesfm-repo/app/forecaster.py',
    summary:
      'TimesFM 2.5 supports multivariate input with exogenous features. Currently: BTC price only (univariate). Missing: CoinGlass OI/funding/liquidations, Gamma prices, Tiingo bid-ask spread. Model has no regime context — must guess at cascades from price alone. Adding covariates lets it "see" liquidation pressure and funding spikes directly.',
    symptoms: [
      'forecaster.py: forecast(inputs=[single_price_series]) — univariate only',
      'CoinGlass data already in ticks_coinglass (10s cadence, 15 columns)',
      'Gamma prices in ticks_gamma (per window)',
      'Model API supports inputs=[series1, series2, ...] but unused',
    ],
    fix: 'Stack CoinGlass OI+funding+liq + Gamma prices as additional input series. Normalize to price scale. Retrain LightGBM on enriched TimesFM outputs. Estimate: 3-5 days.',
    progressNotes: [],
  },
  {
    id: 'ML-12',
    category: 'ml-training-data',
    title: 'TimesFM: multi-horizon ensemble for direction consensus (+1-3pp)',
    severity: 'MEDIUM',
    status: 'OPEN',
    file: 'novakash-timesfm-repo/app/forecaster.py',
    summary:
      'At any given point, run 2-3 forecasts at different horizons (e.g., T-30 + T-60 + T-180) in one call. When short-range and long-range agree → high conviction. When they disagree → regime uncertainty. This is a free meta-feature for LightGBM that costs ~2x inference. Note: TimesFM already predicts dynamically to close — this is about running MULTIPLE horizons simultaneously for consensus.',
    symptoms: [
      'Currently: one forecast per tick at horizon=seconds_to_close',
      'Could: 3 forecasts per tick at horizons=[30, seconds_to_close, 180]',
      'Agreement metric: direction_agreement_3h = how many agree on direction',
      'Divergence metric: when short-term says UP but long-term says DOWN → regime shift signal',
    ],
    fix: 'Add multi-horizon call to forecaster.py. Extract agreement/divergence features. Feed to LightGBM v2 as new columns. Estimate: 1-2 days.',
    progressNotes: [],
  },
  {
    id: 'ML-13',
    category: 'ml-training-data',
    title: 'TimesFM: quantile-derived features underused (spread only, 5 more possible)',
    severity: 'LOW',
    status: 'DONE',
    file: 'novakash-timesfm-repo/app/v2_scorer.py:492-499',
    summary:
      'Quantiles P10/P25/P50/P75/P90 computed and stored in ticks_timesfm but only spread=(P90-P10) used as feature. Missing: tail_risk=(P50-P10)/P50 (downside), skew=(P90+P10-2×P50)/spread (asymmetry), interval_width=(P75-P25)/P50 (uncertainty), quantile_convergence (how quantiles change over window). All computable from existing stored data — 10 lines of code.',
    symptoms: [
      'v2_scorer.py: extracts tfm_spread = P90-P10 only',
      'P10/P25/P50/P75/P90 all stored in ticks_timesfm',
      'LightGBM receives only 5 TimesFM features (direction, confidence, predicted_close, delta_vs_open, spread)',
      'Could have 10+ features from same quantile data',
    ],
    fix: 'Add tail_risk, skew, interval_width, quantile_convergence features in v2_scorer.py. Add to LightGBM feature vector. Retrain. Estimate: ~10 lines.',
    progressNotes: ['DONE: 4 quantile features added to v2_scorer.py + train_lgb_v5.py (PR #77 + PR #78). Recording live.'],
  },
  {
    id: 'ML-14',
    category: 'ml-training-data',
    title: 'v3 composite signals added to training pipeline (11 features, 756K rows)',
    severity: 'HIGH',
    status: 'DONE',
    file: 'novakash-timesfm-repo/training/queries.py',
    summary:
      'ticks_v3_composite (756K rows since Apr 9) was completely unused in LightGBM training. Now added: v3_composite, v3_elm, v3_cascade, v3_taker, v3_oi, v3_funding, v3_vpin, v3_momentum, v3_cascade_strength, v3_cascade_tau1, v3_cascade_exhaustion. Total features: 46 (was 35). Also wired serve-time in v2_scorer.py via V3CompositeScorer.score() injection.',
    symptoms: [
      'queries.py: LEFT JOIN ticks_v3_composite in all 3 training queries',
      'train_lgb_v5.py: 11 new entries in FEATURE_COLUMNS_V5',
      'v2_scorer.py: _v3_composite_features() + v3_score_getter injection',
      'main.py: v3 scorer wired into all v2 scorers',
    ],
    fix: 'Already shipped to main. Retrain tested: 60.59% acc, +9.50pp skill. Same as without v3 — features likely NaN for most training window (v3 data only 5 days). Will improve as data accumulates. v5.2 stays production — ECE 0.127 vs v5.2 ECE 0.064.',
    progressNotes: [
      'Shipped to main 2026-04-13. Retrain run: features=40, acc=60.59%, ECE=0.1268.',
      'v5.2 Sequoia still production (ECE=0.0643, calibration wins). Wait for 14d+ v3 data then retrain.',
    ],
  },
  {
    id: 'ML-15',
    category: 'ml-training-data',
    title: 'Direction-specific training flag (--direction=UP/DOWN) for vml_up/vml_down',
    severity: 'MEDIUM',
    status: 'DONE',
    file: 'novakash-timesfm-repo/training/train_lgb_v5.py',
    summary:
      'Added --direction=UP or --direction=DOWN CLI flag to train_lgb_v5.py. Filters training data to windows where actual outcome matches. Artifacts land in direction-suffixed slots (e.g., models/btc_5m_polymarket_down/). Does not affect default training path. Direction-specific runs are manual until shadow evaluation validates.',
    symptoms: [
      'train_lgb_v5.py: --direction flag, prepare_training_frame direction_filter param',
      'Artifacts: models/btc_5m_polymarket_down/ and models/btc_5m_polymarket_up/',
      'Not wired into retrain.yml matrix yet — manual runs only',
    ],
    fix: 'Already shipped. Usage: python -m training.train_lgb_v5 --direction=DOWN --timeframe=5m',
    progressNotes: ['Shipped to main 2026-04-13.'],
  },
  {
    id: 'ML-16',
    category: 'ml-training-data',
    title: 'Dashboard upgrades: equity curve, shadow compare, gate would-win %',
    severity: 'MEDIUM',
    status: 'DONE',
    file: 'novakash-timesfm-repo/dashboard/',
    summary:
      'Three dashboard upgrades shipped to Vercel (novakash-timesfm-dashboard.vercel.app): (1) Equity curve — simulated PnL + rolling 50-window accuracy on main page. (2) Shadow vs production model compare — pick two model versions, see accuracy delta. (3) Gate "would have won %" — color-coded column showing if blocked trades were correct. All needed for Billy model promotion decisions.',
    symptoms: [
      'EquityCurve.tsx + /api/equity-curve — inline SVG PnL chart',
      'ShadowCompare.tsx + /api/shadow-compare — A/B model picker',
      'compare-queries.ts + compare/page.tsx — would_have_won_pct column',
    ],
    fix: 'Already shipped (PR #78 merged to main). View at https://novakash-timesfm-dashboard.vercel.app',
    progressNotes: ['Merged 2026-04-13. Vercel auto-deploys from main.'],
  },

  // ── btc-15m-expansion ────────────────────────────────────────────────────

  {
    id: '15M-01',
    category: 'btc-15m-expansion',
    title: 'Phase 1: Fix 5 hardcoded "5m" blockers in data surface + registry + orchestrator',
    severity: 'CRITICAL',
    status: 'OPEN',
    file: 'engine/strategies/data_surface.py',
    summary:
      'DataSurfaceManager._fetch_v4() hardcodes timescale="5m" (line 253). get_surface() hardcodes ts_data from "5m" block (line 358). Registry has no timescale filter — 5m strategies fire on 15m windows. Orchestrator market_slug hardcodes "5m" (line 1736). WindowInfo has no .timeframe attribute. Full details in docs/BTC_15M_EXPANSION_PLAN.md §Blockers B1-B5.',
    symptoms: [
      'B1: data_surface.py:253 — _fetch_v4 hardcodes timescale="5m"',
      'B2: data_surface.py:358 — get_surface reads only "5m" block from V4 snapshot',
      'B3: registry.py evaluate_all() — no timescale filter, all strategies fire on all windows',
      'B4: orchestrator.py:1736 — market_slug hardcodes "-5m-"',
      'B5: WindowInfo has no .timeframe — strategy_decisions all stored as timeframe="5m"',
    ],
    fix: 'Multi-timeframe _cached_v4 dict, timeframe-aware get_surface(), timescale filter in evaluate_all(), dynamic market_slug, derive timeframe from duration_secs. See plan §Phase 1.',
    progressNotes: [],
  },
  {
    id: '15M-02',
    category: 'btc-15m-expansion',
    title: 'Phase 2: Create 5 YAML strategy configs + hooks for 15m',
    severity: 'HIGH',
    status: 'OPEN',
    file: 'engine/strategies/configs/',
    summary:
      '5 new strategies with timing gates scaled 3x: v15m_down_only (270-450s), v15m_up_asian (270-450s, session), v15m_up_basic (180-540s), v15m_fusion (custom hook), v15m_gate (15-900s, 8 gates). All start GHOST. Python hooks for down_only (CLOB sizing), fusion (timing bands), gate (confidence classifier). See plan §Proposed 15m Strategy Set.',
    symptoms: [
      'v15m_down_only.yaml — DOWN, timing 270-450, CLOB sizing hook',
      'v15m_up_asian.yaml — UP, timing 270-450, session hours [23,0,1,2]',
      'v15m_up_basic.yaml — UP, timing 180-540, global',
      'v15m_fusion.yaml — Both, custom hook (early >540, optimal 90-540, late 15-90)',
      'v15m_gate.yaml — Both, 8-gate DUNE pipeline, 15m thresholds',
    ],
    fix: 'Create 5 YAML + 3 Python hooks + 5 markdown specs in engine/strategies/configs/. Non-timing gates unchanged (delta 0.0005, confidence 0.10, spread 100bps).',
    progressNotes: [],
  },
  {
    id: '15M-03',
    category: 'btc-15m-expansion',
    title: 'Phase 3: Deploy GHOST + verify signal capture in strategy_decisions',
    severity: 'MEDIUM',
    status: 'OPEN',
    file: 'engine/strategies/orchestrator.py',
    summary:
      'Deploy with FIFTEEN_MIN_ENABLED=true, all 5 strategies GHOST. Verify strategy_decisions table has rows with timeframe="15m". Check eval_offset values are in 270-450 range (not 90-150). Confirm v2_probability_up is non-null (15m model loaded). Fix registry _send_window_summary hardcoded "5m" text.',
    symptoms: [
      'Set FIFTEEN_MIN_ENABLED=true + FIFTEEN_MIN_ASSETS=BTC',
      'Query: SELECT * FROM strategy_decisions WHERE timeframe = \'15m\' LIMIT 10',
      'Verify eval_offset in expected range per strategy',
      'Verify v2_probability_up non-null in 15m surface',
    ],
    fix: 'Deploy, monitor 24h, validate DB records. Fix cosmetic "5m" in alert text.',
    progressNotes: [],
  },
  {
    id: '15M-04',
    category: 'btc-15m-expansion',
    title: 'Phase 4: 15m model ALREADY LIVE — 7-day GHOST shadow + Billy promotion gate',
    severity: 'MEDIUM',
    status: 'IN_PROGRESS',
    file: 'novakash-timesfm-repo/.github/workflows/retrain.yml',
    summary:
      '15m model already live and scoring since Apr 8. Model "15m/nogit@v2/btc/btc_15m/..." has 427K prediction rows across 6 Δ buckets (60-720s). v3 composite has 330K rows at timescale="15m" since Apr 9. No new model training needed — just wire the strategies (15M-01 + 15M-02), deploy GHOST, and evaluate. Billy reviews dashboard after 7-day shadow, then approves first v15m_down_only → LIVE.',
    symptoms: [
      'ticks_v2_probability: 427K rows with model_version LIKE "15m/%" since Apr 8',
      'ticks_v3_composite: 330K rows at timescale="15m" since Apr 9',
      'All 6 Δ buckets populated (60/120/180/300/480/720s) with 43K-142K rows each',
      'Latest prediction: within last hour — actively scoring',
      'v15m_down_only first candidate for LIVE (if DOWN WR > 55%)',
    ],
    fix: 'No model training needed. Fix blockers (15M-01), create configs (15M-02), deploy GHOST (15M-03), 7-day shadow, Billy promotes.',
    progressNotes: ['15m model confirmed live 2026-04-13. 427K v2 rows + 330K v3 rows.'],
  },
  {
    id: '15M-05',
    category: 'btc-15m-expansion',
    title: 'Existing 15m infrastructure audit: feed + V4 snapshot + model slot all working',
    severity: 'INFO',
    status: 'DONE',
    file: 'engine/data/feeds/polymarket_5min.py',
    summary:
      'Audit confirmed: Polymarket5MinFeed(duration_secs=900) generates correct btc-updown-15m-{ts} slugs. _on_fifteen_min_window handler exists. V4 snapshot assembler supports timescale="15m". 15m model registry loaded at startup. Retrain matrix includes 15m. strategy_decisions.timeframe is VARCHAR. FullDataSurface.timescale field exists. The plumbing is there — just not connected to the registry.',
    symptoms: [
      'Polymarket5MinFeed: _build_slug() correctly handles duration_secs=900',
      'Orchestrator: _on_fifteen_min_window at lines 1863-1914',
      'V4 assembler: _WINDOW_SECONDS and _LGB_TIMESCALES include "15m"',
      'Model: _v2_15m_registry + _v2_15m_scorer loaded at startup',
    ],
    fix: 'No fix needed — infrastructure verified. Blockers are in the data surface + registry wiring (15M-01).',
    progressNotes: ['Verified 2026-04-13 via automated codebase audit.'],
  },
];

// ─── Components ───────────────────────────────────────────────────────────

function SeverityChip({ severity }) {
  const color = SEVERITY_COLOR[severity] || T.textMuted;
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        fontSize: 8,
        fontWeight: 800,
        padding: '2px 6px',
        borderRadius: 3,
        background: `${color}26`,
        color,
        border: `1px solid ${color}55`,
        fontFamily: T.mono,
        letterSpacing: '0.05em',
      }}
    >
      {severity}
    </span>
  );
}

function StatusChip({ status }) {
  const color = STATUS_COLOR[status] || T.textMuted;
  const labels = {
    OPEN: '○ OPEN',
    IN_PROGRESS: '◐ IN PROGRESS',
    DONE: '● DONE',
    BLOCKED: '■ BLOCKED',
    INFO: '◇ INFO',
  };
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        fontSize: 8,
        fontWeight: 800,
        padding: '2px 6px',
        borderRadius: 3,
        background: `${color}26`,
        color,
        border: `1px solid ${color}55`,
        fontFamily: T.mono,
        letterSpacing: '0.05em',
      }}
    >
      {labels[status] || status}
    </span>
  );
}

function FileRef({ file }) {
  const text = file.line > 1 ? `${file.path}:${file.line}` : file.path;
  return (
    <span
      style={{
        display: 'inline-block',
        fontSize: 9,
        fontFamily: T.mono,
        color: T.cyan,
        background: 'rgba(6,182,212,0.08)',
        padding: '1px 5px',
        borderRadius: 3,
        marginRight: 4,
        marginBottom: 4,
      }}
      title={`${file.repo} · ${text}`}
    >
      {text}
    </span>
  );
}

function TaskCard({ task, categoryColor }) {
  const [expanded, setExpanded] = useState(task.status === 'IN_PROGRESS');

  return (
    <div
      style={{
        background: T.card,
        border: `1px solid ${T.cardBorder}`,
        borderLeft: `3px solid ${categoryColor}`,
        borderRadius: 6,
        padding: 12,
        marginBottom: 8,
      }}
    >
      <div
        onClick={() => setExpanded(!expanded)}
        style={{
          cursor: 'pointer',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          gap: 12,
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 4, flexWrap: 'wrap' }}>
            <span
              style={{
                fontSize: 9,
                fontFamily: T.mono,
                color: T.textDim,
                fontWeight: 800,
                letterSpacing: '0.05em',
              }}
            >
              {task.id}
            </span>
            <SeverityChip severity={task.severity} />
            <StatusChip status={task.status} />
          </div>
          <div style={{ fontSize: 12, color: T.text, fontWeight: 600, lineHeight: 1.3 }}>
            {task.title}
          </div>
        </div>
        <span style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono, flexShrink: 0 }}>
          {expanded ? '▲' : '▼'}
        </span>
      </div>

      {expanded && (
        <div style={{ marginTop: 10, paddingTop: 10, borderTop: `1px solid ${T.cardBorder}` }}>
          {task.files.length > 0 && (
            <div style={{ marginBottom: 10 }}>
              <div
                style={{
                  fontSize: 8,
                  color: T.textMuted,
                  fontWeight: 800,
                  letterSpacing: '0.08em',
                  marginBottom: 4,
                }}
              >
                FILES
              </div>
              <div>
                {task.files.map((f, i) => (
                  <FileRef key={i} file={f} />
                ))}
              </div>
            </div>
          )}

          <div style={{ marginBottom: 10 }}>
            <div
              style={{
                fontSize: 8,
                color: T.textMuted,
                fontWeight: 800,
                letterSpacing: '0.08em',
                marginBottom: 4,
              }}
            >
              EVIDENCE
            </div>
            <ul style={{ margin: 0, paddingLeft: 16 }}>
              {task.evidence.map((e, i) => (
                <li
                  key={i}
                  style={{
                    fontSize: 10,
                    color: T.text,
                    marginBottom: 2,
                    lineHeight: 1.4,
                  }}
                >
                  {e}
                </li>
              ))}
            </ul>
          </div>

          <div style={{ marginBottom: task.progressNotes?.length ? 10 : 0 }}>
            <div
              style={{
                fontSize: 8,
                color: T.textMuted,
                fontWeight: 800,
                letterSpacing: '0.08em',
                marginBottom: 4,
              }}
            >
              FIX
            </div>
            <div
              style={{
                fontSize: 10,
                color: T.text,
                padding: '6px 8px',
                background: 'rgba(16,185,129,0.05)',
                border: '1px solid rgba(16,185,129,0.15)',
                borderRadius: 4,
                lineHeight: 1.4,
              }}
            >
              {task.fix}
            </div>
          </div>

          {task.progressNotes && task.progressNotes.length > 0 && (
            <div>
              <div
                style={{
                  fontSize: 8,
                  color: T.textMuted,
                  fontWeight: 800,
                  letterSpacing: '0.08em',
                  marginBottom: 4,
                }}
              >
                PROGRESS LOG
              </div>
              <div
                style={{
                  padding: '6px 8px',
                  background: 'rgba(168,85,247,0.05)',
                  border: '1px solid rgba(168,85,247,0.15)',
                  borderRadius: 4,
                }}
              >
                {task.progressNotes.map((entry, i) => (
                  <div
                    key={i}
                    style={{
                      display: 'flex',
                      gap: 8,
                      alignItems: 'flex-start',
                      marginBottom:
                        i === task.progressNotes.length - 1 ? 0 : 6,
                    }}
                  >
                    <span
                      style={{
                        fontSize: 9,
                        color: T.purple,
                        fontFamily: T.mono,
                        fontWeight: 800,
                        whiteSpace: 'nowrap',
                        flexShrink: 0,
                      }}
                    >
                      {entry.date}
                    </span>
                    <span
                      style={{
                        fontSize: 10,
                        color: T.text,
                        lineHeight: 1.4,
                      }}
                    >
                      {entry.note}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ProgressBar({ done, total }) {
  const pct = total > 0 ? (done / total) * 100 : 0;
  return (
    <div style={{ width: '100%' }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          fontSize: 9,
          color: T.textMuted,
          marginBottom: 3,
          fontFamily: T.mono,
        }}
      >
        <span>PROGRESS</span>
        <span>
          {done}/{total} · {pct.toFixed(0)}%
        </span>
      </div>
      <div
        style={{
          height: 6,
          background: 'rgba(15,23,42,0.6)',
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 3,
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            height: '100%',
            width: `${pct}%`,
            background: T.green,
            transition: 'width 0.3s ease',
          }}
        />
      </div>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────

export default function AuditChecklist() {
  const [severityFilter, setSeverityFilter] = useState('ALL');
  const [statusFilter, setStatusFilter] = useState('ALL');
  const [categoryFilter, setCategoryFilter] = useState('ALL');

  const filteredTasks = useMemo(() => {
    return TASKS.filter((t) => {
      if (severityFilter !== 'ALL' && t.severity !== severityFilter) return false;
      if (statusFilter !== 'ALL' && t.status !== statusFilter) return false;
      if (categoryFilter !== 'ALL' && t.category !== categoryFilter) return false;
      return true;
    });
  }, [severityFilter, statusFilter, categoryFilter]);

  const stats = useMemo(() => {
    const total = TASKS.length;
    const done = TASKS.filter((t) => t.status === 'DONE').length;
    const open = TASKS.filter((t) => t.status === 'OPEN').length;
    const inProgress = TASKS.filter((t) => t.status === 'IN_PROGRESS').length;
    const critical = TASKS.filter((t) => t.severity === 'CRITICAL').length;
    const high = TASKS.filter((t) => t.severity === 'HIGH').length;
    return { total, done, open, inProgress, critical, high };
  }, []);

  const tasksByCategory = useMemo(() => {
    const map = {};
    for (const cat of CATEGORIES) {
      map[cat.id] = filteredTasks.filter((t) => t.category === cat.id);
    }
    return map;
  }, [filteredTasks]);

  return (
    <div style={{ padding: '16px 20px', maxWidth: 1400, margin: '0 auto' }}>
      {/* Header */}
      <div style={{ marginBottom: 16 }}>
        <h1
          style={{
            fontSize: 16,
            fontWeight: 800,
            color: T.white,
            margin: 0,
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          {SESSION_META.title}
          <span
            style={{
              fontSize: 8,
              fontWeight: 700,
              padding: '2px 6px',
              borderRadius: 3,
              background: 'rgba(168,85,247,0.15)',
              color: T.purple,
              border: '1px solid rgba(168,85,247,0.3)',
              fontFamily: T.mono,
            }}
          >
            STATIC
          </span>
          <span style={{ fontSize: 8, fontWeight: 700, padding: '2px 6px', borderRadius: 3, background: 'rgba(6,182,212,0.12)', color: T.cyan, border: '1px solid rgba(6,182,212,0.3)', fontFamily: T.mono, letterSpacing: '0.06em' }}>POLY + PERPS</span>
        </h1>
        <p style={{ fontSize: 10, color: T.textMuted, margin: '4px 0 0', maxWidth: 900, lineHeight: 1.5 }}>
          {SESSION_META.summary}
        </p>
        <div style={{ display: 'flex', gap: 12, marginTop: 6, flexWrap: 'wrap' }}>
          {SESSION_META.repos.map((r, i) => (
            <span
              key={i}
              style={{
                fontSize: 9,
                fontFamily: T.mono,
                color: T.textMuted,
              }}
            >
              <span style={{ color: T.textDim }}>{r.name}</span>
              <span style={{ color: T.cyan, margin: '0 4px' }}>/</span>
              <span style={{ color: T.text }}>{r.branch}</span>
              <span style={{ color: T.textDim, marginLeft: 4 }}>@ {r.head}</span>
            </span>
          ))}
        </div>
        <div
          style={{
            marginTop: 10,
            padding: '8px 10px',
            borderRadius: 6,
            border: `1px solid ${T.cardBorder}`,
            background: 'rgba(15,23,42,0.55)',
            fontSize: 9,
            color: T.textMuted,
            lineHeight: 1.5,
          }}
        >
          <span style={{ color: T.cyan, fontWeight: 700, letterSpacing: '0.08em' }}>AGENT OPS</span>{' '}
          audit checklist tasks are moving to DB. New source table:{' '}
          <span style={{ color: T.text, fontFamily: T.mono }}>audit_tasks_dev</span>{' '}— use the
          Hub API <span style={{ color: T.text, fontFamily: T.mono }}>/api/audit-tasks</span> for live
          task state. This page still renders the static TASKS list until the
          DB-backed view lands.
        </div>
      </div>

      {/* Stats + Progress */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(100px, 1fr))',
          gap: 8,
          marginBottom: 12,
        }}
      >
        {[
          { label: 'TOTAL', value: stats.total, color: T.text },
          { label: 'DONE', value: stats.done, color: T.green },
          { label: 'IN PROGRESS', value: stats.inProgress, color: T.amber },
          { label: 'OPEN', value: stats.open, color: T.red },
          { label: 'CRITICAL', value: stats.critical, color: T.red },
          { label: 'HIGH', value: stats.high, color: T.amber },
        ].map(({ label, value, color }) => (
          <div
            key={label}
            style={{
              background: T.card,
              border: `1px solid ${T.cardBorder}`,
              borderRadius: 6,
              padding: '8px 10px',
            }}
          >
            <div
              style={{
                fontSize: 8,
                color: T.textMuted,
                fontWeight: 700,
                letterSpacing: '0.08em',
                marginBottom: 3,
              }}
            >
              {label}
            </div>
            <div style={{ fontSize: 18, fontWeight: 900, fontFamily: T.mono, color }}>{value}</div>
          </div>
        ))}
      </div>

      <div
        style={{
          background: T.card,
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 6,
          padding: '10px 12px',
          marginBottom: 16,
        }}
      >
        <ProgressBar done={stats.done} total={stats.total} />
      </div>

      {/* Filter bar */}
      <div
        style={{
          display: 'flex',
          gap: 12,
          marginBottom: 14,
          flexWrap: 'wrap',
          alignItems: 'center',
        }}
      >
        <FilterGroup
          label="SEVERITY"
          value={severityFilter}
          onChange={setSeverityFilter}
          options={['ALL', 'CRITICAL', 'HIGH', 'MEDIUM', 'LOW']}
        />
        <FilterGroup
          label="STATUS"
          value={statusFilter}
          onChange={setStatusFilter}
          options={['ALL', 'OPEN', 'IN_PROGRESS', 'DONE', 'INFO']}
        />
        <FilterGroup
          label="CATEGORY"
          value={categoryFilter}
          onChange={setCategoryFilter}
          options={['ALL', ...CATEGORIES.map((c) => c.id)]}
        />
      </div>

      {/* Categories */}
      {CATEGORIES.map((cat) => {
        const tasks = tasksByCategory[cat.id];
        if (!tasks || tasks.length === 0) return null;
        return (
          <div key={cat.id} style={{ marginBottom: 20 }}>
            <div
              style={{
                padding: '8px 12px',
                marginBottom: 8,
                borderRadius: 6,
                background: `${cat.color}0d`,
                border: `1px solid ${cat.color}33`,
                borderLeft: `3px solid ${cat.color}`,
              }}
            >
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  marginBottom: 2,
                }}
              >
                <span
                  style={{
                    fontSize: 11,
                    fontWeight: 800,
                    color: cat.color,
                    letterSpacing: '0.05em',
                    textTransform: 'uppercase',
                  }}
                >
                  {cat.title}
                </span>
                <span
                  style={{
                    fontSize: 9,
                    color: T.textMuted,
                    fontFamily: T.mono,
                  }}
                >
                  {tasks.filter((t) => t.status === 'DONE').length}/{tasks.length} done
                </span>
              </div>
              <div style={{ fontSize: 9, color: T.textMuted, lineHeight: 1.4 }}>
                {cat.description}
              </div>
            </div>
            {tasks.map((task) => (
              <TaskCard key={task.id} task={task} categoryColor={cat.color} />
            ))}
          </div>
        );
      })}

      {filteredTasks.length === 0 && (
        <div
          style={{
            textAlign: 'center',
            padding: 30,
            color: T.textMuted,
            fontSize: 11,
            background: T.card,
            border: `1px solid ${T.cardBorder}`,
            borderRadius: 6,
          }}
        >
          No tasks match the current filters.
        </div>
      )}
    </div>
  );
}

function FilterGroup({ label, value, onChange, options }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
      <span
        style={{
          fontSize: 8,
          color: T.textMuted,
          fontWeight: 800,
          letterSpacing: '0.08em',
          marginRight: 4,
        }}
      >
        {label}
      </span>
      {options.map((opt) => (
        <button
          key={opt}
          onClick={() => onChange(opt)}
          style={{
            padding: '4px 8px',
            borderRadius: 3,
            fontSize: 9,
            fontWeight: 700,
            fontFamily: T.mono,
            background: value === opt ? 'rgba(6,182,212,0.15)' : 'transparent',
            color: value === opt ? T.cyan : T.textMuted,
            border: `1px solid ${value === opt ? 'rgba(6,182,212,0.3)' : T.cardBorder}`,
            cursor: 'pointer',
            letterSpacing: '0.05em',
            textTransform: 'uppercase',
          }}
        >
          {opt}
        </button>
      ))}
    </div>
  );
}
