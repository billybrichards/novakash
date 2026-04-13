# Frontend Audit — 2026-04-13

**Branch:** `audit/frontend-clean-arch-2026-04-13`  
**Context:** Strategy Engine v2 design spec (CA-07) written; auditing frontend readiness.

---

## 1. All Pages Found

### Polymarket (Primary)

| Route | Component | Purpose | Strategy-Related |
|-------|-----------|---------|:---:|
| `/polymarket/monitor` | `Monitor.jsx` | Primary trading dashboard — 5-band live view (status, data health, signals, gates, recent flow) | Yes |
| `/polymarket/overview` | `Overview.jsx` | Prediction accuracy surface by eval_offset, strategy performance cards | Yes |
| `/polymarket/floor` | `LiveFloor.jsx` | Live price chart + active strategy decisions (V10/V4 side-by-side) + recent windows | Yes |
| `/polymarket/evaluate` | `Evaluate.jsx` | Performance analysis — strategy comparison table, gate impact, P&L charts | Yes |
| `/polymarket/down-only` | `StrategyFloor.jsx` (prop: `v4_down_only`) | Dedicated single-strategy floor for DOWN-ONLY | Yes |
| `/polymarket/up-asian` | `StrategyFloor.jsx` (prop: `v4_up_asian`) | Dedicated single-strategy floor for UP ASIAN | Yes |
| `/polymarket/strategy-lab` | `StrategyLab.jsx` | Historical replay, gate impact analysis, strategy mode toggle (LIVE/GHOST/OFF) | Yes |
| `/polymarket/strategy-history` | `StrategyHistory.jsx` | Changelog of strategy config changes | Yes |
| `/signal-comparison` | `SignalComparison.jsx` | Side-by-side signal source comparison | Yes |

### Polymarket Sub-Components

| File | Purpose |
|------|---------|
| `components/DataHealthStrip.jsx` | Signal source health indicators |
| `components/GatePipeline.jsx` | 8-gate strip visualization |
| `components/SignalSurface.jsx` | Direction + market context + V4 action |
| `components/StatusBar.jsx` | Mode, bankroll, W/L, countdown |
| `components/WindowAnalysisModal.jsx` | Per-window drill-down modal |
| `components/RecentFlow.jsx` | Last 20 windows timeline |
| `components/theme.js` | Shared theme constants + formatters |

### Data Surfaces

| Route | Component | Purpose |
|-------|-----------|---------|
| `/data/v1` | `V1Surface.jsx` | V1 data surface viewer |
| `/data/v2` | `V2Surface.jsx` | V2 data surface viewer |
| `/data/v3` | `V3Surface.jsx` | V3 data surface viewer |
| `/data/v4` | `V4Surface.jsx` | V4 data surface viewer |
| `/data/assembler1` | `Assembler1.jsx` | Data assembler viewer |

### Margin Engine

| Route | Component | Purpose |
|-------|-----------|---------|
| `/margin` | `MarginEngine.jsx` | Margin engine monitor |
| `/margin-strategies` | `MarginStrategies.jsx` | Margin strategy management |

### System/Admin

| Route | Component | Purpose |
|-------|-----------|---------|
| `/system` | `System.jsx` | Engine status, kill switch, resume |
| `/config` | `Config.jsx` | DB-backed config browser (CFG-05) |
| `/legacy-config` | `LegacyConfig.jsx` | Legacy 13-key config page |
| `/trading-config` | `TradingConfig.jsx` | Bundle editor |
| `/schema` | `Schema.jsx` | DB schema catalog |
| `/deployments` | `Deployments.jsx` | Deployment tracking |
| `/audit` | `AuditChecklist.jsx` | This audit page |
| `/notes` | `Notes.jsx` | Notes page |

### Legacy (in LEGACY_ITEMS, not in primary nav)

| Route | Component | Purpose | Still Used? |
|-------|-----------|---------|:-----------:|
| `/dashboard` | redirects to `PolymarketOverview` | Legacy dashboard redirect | Redirect only |
| `/factory` | `FactoryFloor.jsx` | Old factory floor | Replaced by LiveFloor |
| `/execution-hq/:asset/:timeframe` | `ExecutionHQ.jsx` | Multi-asset execution HQ | Replaced by Monitor |
| `/signals` | `Signals.jsx` | Legacy signal history | Superseded by Monitor |
| `/trades` | `Trades.jsx` | Trade history with filters | Still useful |
| `/pnl` | `PnL.jsx` | Equity curve, daily/monthly | Still useful |
| `/v58` | `V58Monitor.jsx` | V58 monitor | Superseded by Evaluate |
| `/windows` | `WindowResults.jsx` | Window results viewer | Superseded by LiveFloor |
| `/live` | `LiveTrading.jsx` | Old live trading page | Replaced by Floor |
| `/timesfm` | `TimesFM.jsx` | TimesFM viewer | Niche but active |

### Potentially Dead Pages (in router but not in any nav section)

| Route | Component | Notes |
|-------|-----------|-------|
| `/paper` | `PaperDashboard.jsx` | Not in nav sections or legacy items |
| `/positions` | `Positions.jsx` | Not in nav |
| `/risk` | `Risk.jsx` | Not in nav |
| `/setup` | `Setup.jsx` | Not in nav |
| `/learn` | `Learn.jsx` | Not in nav |
| `/changelog` | `Changelog.jsx` | Not in nav |
| `/playwright` | `PlaywrightDashboard.jsx` | Not in nav |
| `/indicators` | `Indicators.jsx` | Not in nav |
| `/strategy` | `StrategyAnalysis.jsx` | Not in nav |
| `/analysis` | `AnalysisLibrary.jsx` | Not in nav |
| `/recommendations` | `Recommendations.jsx` | Not in nav |
| `/composite` | `CompositeSignals.jsx` | Not in nav |

### Unused Shared Components (not imported anywhere)

| Component | Notes |
|-----------|-------|
| `ArbMonitor.jsx` | Arb strategy deactivated, never imported |
| `CascadeIndicator.jsx` | Cascade strategy deactivated, never imported |
| `ForecastPanel.jsx` | Never imported |
| `ForecastChart.jsx` | Never imported |
| `OAKModelPanel.jsx` | OAK model discontinued, never imported |

---

## 2. Issues Identified

### 2.1 Hardcoded Strategy Names

**10 files** contain hardcoded strategy IDs (`v4_down_only`, `v4_up_asian`, `v4_fusion`, `v10_gate`):

- `App.jsx` (routes: `/polymarket/down-only`, `/polymarket/up-asian`)
- `StrategyFloor.jsx` (`STRATEGY_CONFIGS` object)
- `StrategyLab.jsx` (`STRATEGIES_META` array)
- `Evaluate.jsx` (`STRATEGY_META` object)
- `LiveFloor.jsx` (hardcoded V10/V4 references)
- `Overview.jsx` (strategy performance cards)
- `StrategyHistory.jsx` (code changelog)
- `RecentFlow.jsx` (strategy coloring)
- `WindowAnalysisModal.jsx` (strategy references)
- `Layout.jsx` (nav items for DOWN Floor / UP Floor)

**Impact:** When Strategy Engine v2 (CA-07) adds new strategies via YAML config (e.g., `v4_up_basic`), the frontend needs manual updates in all 10 files. The design should move toward:
- A `/api/strategies` endpoint returning registered strategies
- Frontend reading strategy metadata from API, not hardcoded constants

### 2.2 Duplicate Strategy Metadata

Three separate strategy metadata objects exist:

1. `StrategyFloor.jsx` line 21: `STRATEGY_CONFIGS` (id, label, color, direction, gateLabel, thresholds)
2. `StrategyLab.jsx` line 20: `STRATEGIES_META` (id, label, configKey, description, color, badge, direction)
3. `Evaluate.jsx` line 38: `STRATEGY_META` (label, color)

These define the same strategies with overlapping but inconsistent fields. Should be consolidated into a shared `strategyMeta.js` module.

### 2.3 Gate Pipeline Hardcoded (V10.6)

`Evaluate.jsx` line 18 hardcodes the V10.6 gate pipeline:
```js
const GATE_PIPELINE = [
  { key: 'eval_offset_bounds', label: 'EvalOffset' },
  { key: 'source_agreement', label: 'SrcAgree' },
  ...
];
```

Strategy Engine v2 uses a configurable gate pipeline per strategy (16 reusable gates). The frontend should read gate pipeline definitions from the API.

### 2.4 Dead Components (5 files, ~500 LOC)

- `ArbMonitor.jsx` — Arb strategy inactive, component never imported
- `CascadeIndicator.jsx` — Cascade strategy inactive, component never imported
- `ForecastPanel.jsx` — Never imported by any page
- `ForecastChart.jsx` — Never imported by any page
- `OAKModelPanel.jsx` — OAK model discontinued, never imported

### 2.5 Hidden Pages (12 routes not in navigation)

12 pages exist in the router but are absent from both primary nav sections and the legacy items list. These are reachable only by direct URL. Some are legitimately niche; others may be dead.

---

## 3. Changes Made

### 3.1 AuditChecklist.jsx — 6 New Tasks Added

| ID | Category | Severity | Title |
|----|----------|----------|-------|
| CA-07 | clean-architect | CRITICAL | Strategy Engine v2 — config-first registry replaces inheritance chain |
| CA-08 | clean-architect | HIGH | Data Surface Layer — 1Hz fresh in-memory cache eliminates blocking I/O |
| CA-09 | clean-architect | HIGH | Domain layer reconciliation — delete duplicates, merge worktree types |
| SIG-05 | signal-optimization | HIGH | v4_up_basic strategy — global UP, dist>=0.10, T-60-180, all hours |
| SIG-06 | signal-optimization | MEDIUM | v4_up_asian fix — relax thresholds via config |
| DATA-FRESH-01 | data-quality | HIGH | Enable V3 on timesfm service |

### 3.2 AuditChecklist.jsx — 3 Existing Tasks Updated

| ID | Update |
|----|--------|
| CA-01 | Progress note: Phase 4+ superseded by CA-07 Strategy Engine v2 |
| SIG-03b | Progress note: Timing override hack eliminated by CA-07 |
| SIG-04 | Progress note: CLOBSizingGate now part of CA-07 reusable gate library |

---

## 4. Recommendations for Future Work

### High Priority (Strategy Engine v2 readiness)

1. **Create `/api/strategies` endpoint** — return registered strategy metadata (id, label, direction, color, mode, gate pipeline). Frontend reads this instead of hardcoded constants.

2. **Extract shared `strategyMeta.js`** — consolidate the 3 duplicate strategy metadata objects (`STRATEGY_CONFIGS`, `STRATEGIES_META`, `STRATEGY_META`) into one shared module that can later be replaced by API-fetched data.

3. **Make StrategyFloor generic** — currently accepts `strategyId` prop but has hardcoded `STRATEGY_CONFIGS` for only 2 strategies. Should dynamically render any strategy from the registry.

4. **Add dynamic routes for new strategies** — when `v4_up_basic` is added via CA-07, the frontend needs a route and nav item automatically. Consider a dynamic route pattern: `/polymarket/strategy/:strategyId`.

### Medium Priority (Cleanup)

5. **Delete 5 unused components** — `ArbMonitor.jsx`, `CascadeIndicator.jsx`, `ForecastPanel.jsx`, `ForecastChart.jsx`, `OAKModelPanel.jsx` (combined ~500 LOC).

6. **Audit 12 hidden pages** — determine which of the 12 unreachable-via-nav pages should be removed vs. added to nav vs. kept as hidden utilities.

7. **Gate pipeline from API** — replace hardcoded `GATE_PIPELINE` in `Evaluate.jsx` with API-sourced gate definitions per strategy.

### Low Priority (Polish)

8. **Strategy decision display** — show per-gate pass/fail results, confidence values, and timing gate status in StrategyFloor and LiveFloor.

9. **Data surface freshness indicators** — show staleness of each data source (Tiingo, Chainlink, CLOB, V4 snapshot) on Monitor page, aligned with CA-08 data surface layer.
