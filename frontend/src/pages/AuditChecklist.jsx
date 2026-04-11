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
      'Root-cause bugs in how the Polymarket engine computes deltas across Binance/Tiingo/Chainlink sources. These contaminate every v5 signal regardless of model quality.',
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
];

const TASKS = [
  // ── data-quality ─────────────────────────────────────────────────────────
  {
    id: 'DQ-01',
    category: 'data-quality',
    severity: 'CRITICAL',
    status: 'OPEN',
    title: 'Binance spot/futures reference mismatch in delta_binance',
    files: [
      { path: 'engine/data/feeds/binance_ws.py', line: 26, repo: 'novakash' },
      { path: 'engine/data/feeds/polymarket_5min.py', line: 464, repo: 'novakash' },
      { path: 'engine/strategies/five_min_vpin.py', line: 344, repo: 'novakash' },
    ],
    evidence: [
      'WS feed connects to wss://fstream.binance.com (Binance Futures perp)',
      'window.open_price is fetched from api.binance.com (Binance Spot)',
      'delta_binance = (futures_price - spot_open) / spot_open — this is basis, not movement',
      'Measured last 2h, n=1393: avg_binance = -0.0551% (systematic bearish bias)',
      'Sign distribution: binance=DOWN in 93% of evals, primary=UP in 59%',
      'Root cause of 280+/hr evaluate.price_source_disagreement warnings',
    ],
    fix: 'Drop delta_binance from SourceAgreementGate consensus vote. Keep futures WS for VPIN/liquidations (they are futures-correct), use only Tiingo 5m candle + Chainlink for direction. Add feature flag V11_BINANCE_SPOT_REF for rollback.',
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

  // ── decision-surface ────────────────────────────────────────────────────
  {
    id: 'DS-01',
    category: 'decision-surface',
    severity: 'HIGH',
    status: 'OPEN',
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
    status: 'OPEN',
    title: 'five_min_vpin.py is a 3096-line god class',
    files: [
      { path: 'engine/strategies/five_min_vpin.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      '3096 LOC, 28 methods, 328 self._ fields',
      '_evaluate_window() alone is ~1500 lines',
      'Embeds Tiingo REST, Chainlink RPC, CoinGlass, DUNE, FOK ladder, Telegram alerts',
      '13-parameter constructor (7 optional None defaults)',
      'No tests for the strategy itself (too coupled to mock)',
    ],
    fix: 'Phase 4 refactor: extract entry logic → engine/use_cases/open_five_min_position.py. Target <500 LOC for the orchestration class. Use margin_engine/use_cases/open_position.py as template.',
  },
  {
    id: 'CA-02',
    category: 'clean-architect',
    severity: 'HIGH',
    status: 'OPEN',
    title: 'No ports/adapters layer in engine/',
    files: [
      { path: 'engine/strategies/five_min_vpin.py', line: 360, repo: 'novakash' },
      { path: 'margin_engine/domain/ports.py', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'engine/ imports concrete OrderManager, PolymarketClient directly',
      'Tiingo HTTP call inline at five_min_vpin.py:360',
      'Chainlink RPC inline at five_min_vpin.py:525',
      'margin_engine/domain/ports.py defines 6 abstract ports with adapters implementing each',
    ],
    fix: 'Create engine/domain/ports.py with PriceFeedPort, WindowStatePort, V4SnapshotPort. Extract current inline HTTP into adapters.',
  },
  {
    id: 'CA-03',
    category: 'clean-architect',
    severity: 'MEDIUM',
    status: 'OPEN',
    title: 'Gate context is mutable (ordering dependencies)',
    files: [
      { path: 'engine/signals/gates.py', line: 45, repo: 'novakash' },
    ],
    evidence: [
      'Each gate mutates ctx.cg_confirms, ctx.cg_modifier, ctx.cg_bonus in place',
      'DuneConfidenceGate reads modifiers set by TakerFlowGate',
      'Implicit ordering dependency: if gates run in different order, result differs',
    ],
    fix: 'Make GateContext frozen. Each gate returns a GateResult with its own deltas. Use case composes results explicitly.',
  },
  {
    id: 'CA-04',
    category: 'clean-architect',
    severity: 'MEDIUM',
    status: 'OPEN',
    title: 'Window dedup state has two owners',
    files: [
      { path: 'engine/strategies/five_min_vpin.py', line: 138, repo: 'novakash' },
      { path: 'engine/reconciliation/reconciler.py', line: 65, repo: 'novakash' },
    ],
    evidence: [
      'Strategy owns _traded_windows (in-memory set)',
      'Reconciler owns _known_resolved (separate tracking)',
      'No invariant guaranteeing both stay consistent',
    ],
    fix: 'Create WindowStateRepositoryPort with DB-backed adapter. Both strategy and reconciler depend on the port, not their own state.',
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
    status: 'OPEN',
    title: 'No observability surface for Polymarket (legacy) engine',
    files: [
      { path: 'frontend/src/pages/Dashboard.jsx', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'margin_engine has rich /margin page with V4Panel',
      'Polymarket engine surfaces are split across /dashboard, /signals, /v58, /execution-hq',
      'No single "engine_state + v4_snapshot_if_used + gate_failures" panel',
      'Operator has to cross-check logs to understand why a trade was skipped',
    ],
    fix: 'After V4-01 lands, build a Polymarket mirror of V4Panel showing what the engine saw when it skipped/entered a window.',
  },
  {
    id: 'FE-04',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'OPEN',
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
  },
  {
    id: 'FE-05',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'OPEN',
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
  },
  {
    id: 'FE-06',
    category: 'frontend',
    severity: 'MEDIUM',
    status: 'OPEN',
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

  // ── ci-cd ───────────────────────────────────────────────────────────────
  {
    id: 'CI-01',
    category: 'ci-cd',
    severity: 'HIGH',
    status: 'OPEN',
    title: 'Montreal CI/CD automation for engine/ (port deploy-macro-observer.yml pattern)',
    files: [
      { path: 'docs/CI_CD.md', line: 20, repo: 'novakash' },
      { path: '.github/workflows/deploy-macro-observer.yml', line: 1, repo: 'novakash' },
      { path: '.github/workflows/deploy-engine.yml', line: 1, repo: 'novakash' },
    ],
    evidence: [
      'docs/CI_CD.md (6816f86) flags engine/ as "the only major service without a GitHub Actions deploy workflow"',
      'Engine currently relies on Railway git-watcher auto-deploy with no smoke test, no secrets check, no post-deploy health probe, no rollback path',
      'docs/CI_CD.md: "has been observed CRASHED or FAILED in recent deploy history"',
      'deploy-macro-observer.yml is the canonical template (~200 lines, well-commented)',
      'Same Montreal box (3.98.114.0) already used by timesfm-service + macro-observer + data-collector; proven deploy pattern',
      'scripts/restart_engine.sh already encapsulates the process restart — CI can just SSH and run it',
    ],
    fix: 'Create .github/workflows/deploy-engine.yml mirroring deploy-macro-observer.yml: (1) Require runtime secrets step; (2) base64 SSH key decode; (3) rsync engine/ to /home/novakash/novakash/engine with --exclude .env; (4) template .env from GitHub Actions secrets via sudo tee; (5) invoke scripts/restart_engine.sh via ssh; (6) post-deploy health probe checking sudo systemctl is-active OR pgrep python3.*engine.*main.py; (7) grep last 5 min of engine.log for known error signatures (clob_feed.write_error, reconciler.resolve_db_error, evaluate.price_source_disagreement) and fail the deploy if counts exceed thresholds; (8) tail 30 log lines for success diagnostics. Gate to push events on develop with path filter `engine/**`. Add concurrency group to prevent racing rsyncs. Add ENGINE_HOST + ENGINE_SSH_KEY secrets to billybrichards/novakash.',
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
