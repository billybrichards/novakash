# Frontend Redesign — Design Spec

**Date:** 2026-04-12
**Status:** Draft — awaiting user review
**Scope:** Complete frontend reorganization + signal infrastructure fixes + naming cleanup

---

## Problem Statement

The current frontend has 28+ sidebar entries with no clear hierarchy. Pages mix real data with stubs, dead consensus sources show green checkmarks, the macro surface is silently broken (Qwen unreachable), and there's no way to evaluate historical signal performance to optimize gate configurations. The operator needs a dashboard that:

1. Shows what the engine is doing and **honestly reports** what's working vs broken
2. Lets them evaluate which signal/gate combos produce the best outcomes
3. Supports manual trading with full context and evidence trail
4. Separates the two trading venues (Polymarket vs Margin Engine) cleanly

---

## 1. Navigation Architecture

### Top-level tabs (venue-first)

| Tab | What | Audience |
|-----|------|---------|
| **Polymarket** | The 5-min BTC binary options engine | Primary — where money is made |
| **Margin Engine** | Hyperliquid perp trader (eu-west-2) | Secondary — paper mode |
| **System** | Infrastructure, config, audit, schema | Ops / dev |

### Sub-navigation (decision-loop within each venue)

**Polymarket:**
1. **Monitor** — "Should I trade?" — live window, signals, gates, manual trade
2. **Evaluate** — "How am I doing?" — P&L, W/L, accuracy, signal-vs-outcome
3. **Strategy Lab** — "What should I change?" — historical replay, shadow configs
4. **Configure** — gate thresholds, feature flags, strategy params

**Margin Engine:**
1. **Monitor** — positions, P&L, V4 fusion surface
2. **Evaluate** — closed position history, per-timescale performance
3. **Configure** — margin-specific settings

**System:**
1. **Status** — all services health, feeds, engine state
2. **Schema** — DB tables with honest PLANNED/NOT CREATED labels
3. **Deploys** — CI/CD status per service
4. **Config** — DB-backed config browser (CFG-05)
5. **Audit** — SPARTA checklist
6. **Notes** — session journal

### Retired/Folded pages

| Old page | New home |
|----------|---------|
| `/dashboard` | Polymarket > Monitor (status bar) |
| `/factory` | Polymarket > Monitor (Recent Flow band) |
| `/execution-hq/*` | Polymarket > Monitor |
| `/signals` | Polymarket > Evaluate |
| `/trades` | Polymarket > Evaluate |
| `/pnl` | Polymarket > Evaluate |
| `/v58` | Polymarket > Monitor (gate heartbeat data) |
| `/windows` | Polymarket > Evaluate |
| `/strategy` | Polymarket > Strategy Lab |
| `/data/v1` - `/data/v4` | Polymarket > Monitor (signal surface panel, collapsible) |
| `/predict` | Polymarket > Monitor (assembler data) |
| `/margin` | Margin Engine > Monitor |
| `/composite` | Margin Engine > Monitor (signal panel) |
| `/live` | Polymarket > Evaluate (wallet & PnL section) |
| `/config`, `/legacy-config`, `/trading-config` | System > Config (unified) |
| `/system` | System > Status |
| `/schema` | System > Schema |
| `/deployments` | System > Deploys |
| `/audit` | System > Audit |
| `/notes` | System > Notes |
| `/indicators` | Remove — educational mock, no operator value |
| `/recommendations` | Remove — stub with no backend |
| `/learn` | Remove — educational, not operational |
| `/changelog` | System > Notes (fold in as a "releases" tab) |
| `/analysis` | Remove — empty stub |
| `/playwright` | System > Status (fold in as a section) |
| `/timesfm` | Polymarket > Monitor (V2 data shown in signal panel) |

---

## 2. Polymarket Monitor — "Should I Trade?"

The primary screen. Replaces Execution HQ + Factory Floor + Dashboard + V1-V4 surfaces.

### Band 1: Status Bar (always visible, pinned top)

- Mode badge: `PAPER` / `LIVE` (prominent, color-coded)
- Bankroll: `$93.06`
- Session W/L: `130W/102L = 56%`
- Ungated W/L: `9W/1L = 90%` (the signal accuracy without gate filtering)
- Current window: `07:30Z` with countdown `T-102`
- Feed health dots: Binance, Chainlink, Tiingo, CoinGlass, Gamma, CLOB, TimesFM

### Band 2: Data Health Strip

Always-visible horizontal strip showing the health of every signal source. This is the key difference from the current UI which hides broken sources behind green checkmarks.

| Signal Source | Status | Value | Freshness |
|---|---|---|---|
| **Sequoia v5.2** (model) | GREEN/RED | p_up: 0.622 | 2s ago |
| **VPIN** (volume clock) | GREEN/YELLOW | 0.488 (below/above threshold) | live |
| **Source Agreement** | GREEN/YELLOW | CL+TI agree / disagree | live |
| **Consensus** (6 sources) | YELLOW | 3/6 sources (tiingo, chainlink, coinglass dead) | per-source age |
| **Macro** (Qwen / MacroV2) | RED/GREEN | "unreachable" or bias + confidence | age |
| **V3 Composite** | GREEN | composite score (9 timescales) | live |
| **V4 Conviction** | GREY/GREEN | NOT WIRED / conviction level | shadow |

Status logic: GREEN = healthy + fresh. YELLOW = degraded (stale, partial, below threshold). RED = broken/unreachable. GREY = not wired into engine (shadow-only).

### Band 3: Signal Surface Panel

Left column — **Direction & Confidence:**
- Direction: `UP` / `DOWN` (large, clear)
- Source agreement: `CL+TI agree` with historical WR chip
- Sequoia v5.2 probability: gauge showing p_up
- V3 composite: score with 9-timescale sparkline

Center column — **Market Context:**
- Consensus: 6 price sources with divergence bps, safe_to_trade verdict
- Macro: bias (NEUTRAL/LONG/SHORT) + direction gate + size modifier. Prominently shows "FALLBACK" when Qwen is down
- Regime: current classification (NO_EDGE/CHOPPY/TRENDING_UP/etc) with the regime classifier inputs visible
- VPIN: current value with threshold line
- Sub-signals: 7 mini-bars (Sequoia v5.2, Cascade, Taker Flow, OI, Funding, VPIN, Momentum)

Right column — **V4 Recommended Action:**
- Side: LONG/SHORT/SKIP
- Conviction: NONE/LOW/MEDIUM/HIGH with score
- Reason: human-readable (e.g., "regime_chop_skip")
- Quantiles: compact P10-P50-P90 fan chart
- "V4 says X, engine decided Y" comparison when they disagree

**Multi-timescale pills:** `5m` | `15m` | `1h` | `4h` at top of Band 3. Default 5m. Click to see same panel for other timescales. 1h/4h will show "no_model" honestly.

**Collapsible raw V4 JSON drawer** at the bottom for full inspection.

### Band 4: Gate Pipeline + Trade Action

Left: **8-gate pipeline** (horizontal strip)
- Each gate: name + pass/fail + **actual value vs threshold** (not just a checkmark)
- Example: `VPIN: 0.392 < 0.45 FAIL` vs `SrcAgree: CL+TI PASS`
- Blocked reason highlighted in red
- Clean-architecture gate order clearly visible

Right: **Manual Trade Panel**
- `TRADE` button (large, enabled for BTC 5m)
- Auto-filled snapshot showing: direction, delta, all gate states, Sequoia p_up, consensus, V4 action
- Auto-filled rationale: `"Override: VPIN at 0.392 (threshold 0.45). Signal UP with CL+TI agreement (94.7% WR). Ungated accuracy 90%."`
- Editable — operator can modify before confirming
- One-click confirm after reviewing snapshot
- Recent manual trades strip with SOT reconciliation chips (green/yellow/red)

### Band 5: Recent Flow (Factory Floor table, embedded)

The current Factory Floor RECENT FLOW TIMELINE, embedded directly:
- Last 20 windows: TIME | SIGNAL | ACTUAL | SRC | GATES | REASON | RESULT
- Tooltips on every header (per FACTORY-01 work)
- Click any row to expand full gate values + resolution details

---

## 3. Polymarket Evaluate — "How Am I Doing?"

### Section A: Performance Summary

Top cards:
- Total P&L (cumulative)
- Win rate (gated trades only)
- Ungated win rate (what the signal would have done without gates)
- Gate value: cumulative PnL difference between gated and ungated
- Current streak

### Section B: Signal vs Outcome Analysis

For every resolved window, show:

| Window | Signal | Actual | Sequoia p_up | VPIN | Consensus | Macro | Gate Decision | Would-Have PnL |
|--------|--------|--------|-------------|------|-----------|-------|--------------|----------------|

Filters: date range, gate that blocked, direction, outcome, source agreement.

### Section C: Accuracy by Signal Component

- Source agreement accuracy (agree vs disagree)
- Sequoia accuracy by confidence bucket
- VPIN accuracy by bucket
- Regime accuracy by classification

### Section D: P&L Charts

- Equity curve (cumulative)
- Daily P&L bars
- Monthly summary
- Gated vs ungated overlay

---

## 4. Polymarket Strategy Lab — "What Should I Change?"

### Tab A: Historical Replay

Controls: time range + per-gate toggle + threshold sliders.
Output: W/L comparison (your config vs modified vs ungated), equity curves, per-gate kill count.

Backend: new API endpoint for server-side replay against signal_evaluations + window_snapshots.

### Tab B: Shadow Configs (Live A/B)

2-3 alternative gate configs running alongside production. Engine evaluates each shadow config per window but doesn't execute trades. Live W/L accumulating.

### Tab C: Gate Impact Analysis

Per-gate counterfactual: "if I removed only this gate, what would my W/L be?"
Gate correlation matrix. Recommended config search.

---

## 5. System Pages

### Schema (fix S7)
Distinguish: `active` / `active_empty` / `planned` / `legacy` / `deprecated`.
No more "ACTIVE + NOT IN DB".

### Other System pages
Status, Deploys, Config, Audit, Notes — keep existing, nest under System tab.

---

## 6. Signal Naming — elm -> Sequoia v5.2

### Frontend display (part of redesign)
```javascript
const SIGNAL_DISPLAY_NAMES = {
  elm: 'Sequoia v5.2', cascade: 'Cascade', taker: 'Taker Flow',
  oi: 'Open Interest', funding: 'Funding Rate', vpin: 'VPIN', momentum: 'Momentum',
};
```

### Wire format (SQ-01, separate workstream)
Dual-emit `"elm"` + `"sequoia"` in V3/V4 JSON, then deprecate `"elm"` after 1 week.

### Engine rename (SQ-01 PR 1)
`elm_prediction_recorder.py` -> `prediction_recorder.py` + CI-02 gate update in same commit.

---

## 7. Data Health Strip — Status Rules

| Source | GREEN | YELLOW | RED |
|--------|-------|--------|-----|
| Sequoia v5.2 | p_up populated, age < 30s | age 30-120s | null or age > 120s |
| VPIN | value > 0, populated | value = 0 or stale | null |
| Source Agreement | 2+ sources agree | disagree | < 2 sources |
| Consensus | 5-6/6 sources, < 15bps | 3-4/6 sources | < 3 or > 15bps |
| Macro | ok, not fallback, confidence > 0 | advisory, low confidence | unreachable/fallback |
| V3 Composite | populated, age < 10s | age 10-60s | null or > 60s |
| V4 Conviction | != NONE, engine consuming | NONE | not wired |

---

## Appendix A: Signal Infrastructure Fix List

| # | Fix | Repo | Severity | Status |
|---|---|---|---|---|
| S1 | elm sub-signal null < 48h | timesfm | RED | **DONE** (PR #64) |
| S2 | BTC VPIN calibration | timesfm | YELLOW | **DONE** (PR #65) |
| S3 | Alt-coin consensus broken (coinbase/kraken not asset-aware) | timesfm | RED | OPEN |
| S4 | 3/6 BTC consensus sources dead | timesfm + engine | YELLOW | OPEN |
| S5 | V4 quantiles not in envelope | timesfm | YELLOW | OPEN |
| S6 | 1h/4h models missing, 4h lies about status | timesfm + training | RED | OPEN |
| S7 | Schema page honest labels | hub + frontend | MEDIUM | OPEN |
| S8 | Hub migration nginx cutover | frontend | MEDIUM | IN PROGRESS (PR #104) |
| S9 | SQ-01 PR 1: engine cosmetic rename | novakash | LOW | OPEN |
| S10 | SQ-01 PR 3: wire-format dual-emit | timesfm + novakash | LOW | OPEN |
| S11 | Frontend display labels (elm -> Sequoia v5.2) | frontend | LOW | Part of redesign |

---

## Appendix B: Macro + Regime Upgrade Path

### Macro Phase C — Replace Qwen with LightGBM
Full plan: `~/.claude/plans/sleepy-forging-cerf.md`
- Phase A (DONE): Flip to 5m primary + demote Qwen to advisory
- Phase B (OPEN): Fix 15m retrain pipeline
- Phase C (OPEN): Train per-horizon LightGBM MacroV2Classifier. ~1-2 days.

### HMM Regime Classifier
Full plan: `docs/superpowers/plans/2026-04-11-vpin-advanced-ensemble.md` Section 4
- Replace deterministic `_classify_regime()` with 4-state HMM
- Features: realised vol, Hurst exponent, VPIN percentile, funding, momentum
- Evaluate need after macro fix — deterministic classifier may improve once inputs are healthy

---

## Implementation Order

1. Frontend redesign — new nav + Monitor + Evaluate + Strategy Lab
2. Signal fixes S3-S8 — each fix turns a red indicator green
3. SQ-01 naming cleanup (S9-S11) — parallel
4. Strategy Lab backend — replay engine + shadow config evaluation
5. Macro Phase C — replace Qwen with LightGBM
6. HMM regime classifier — evaluate need after macro lands
