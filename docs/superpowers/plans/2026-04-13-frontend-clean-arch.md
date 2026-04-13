# Frontend Clean Architecture Plan — Strategy Engine v2 Alignment

**Date:** 2026-04-13
**Branch:** `clean-arch-polymarket`
**Analyst:** Claude Opus 4.6

---

## 1. Current State Analysis

### 1.1 Page Inventory (37,224 LOC across 46 pages)

Total route count: **51 routes** (including parameterized, redirects, and 404 fallback).

**Polymarket Section (7 pages, primary):**

| Page | File | LOC | API Endpoints Consumed | Status |
|------|------|-----|----------------------|--------|
| Monitor | `polymarket/Monitor.jsx` | ~180 | `/v58/execution-hq`, `/dashboard/stats`, `/v4/snapshot`, `/v3/snapshot`, `/v58/accuracy`, `/v58/stats`, `/v58/outcomes` | ACTIVE, primary dashboard |
| Overview | `polymarket/Overview.jsx` | ~680 | `/v58/accuracy`, `/v58/strategy-comparison`, `/v58/outcomes` | ACTIVE |
| LiveFloor | `polymarket/LiveFloor.jsx` | ~600 | `/v58/execution-hq`, `/v58/outcomes`, `/v58/strategy-decisions`, `/dashboard/stats` | ACTIVE |
| Evaluate | `polymarket/Evaluate.jsx` | ~840 | `/v58/strategy-comparison`, `/v58/strategy-windows` | ACTIVE |
| StrategyLab | `polymarket/StrategyLab.jsx` | 1793 | `/v58/config?service=engine`, `/v58/outcomes`, `/v58/config/upsert` | ACTIVE |
| StrategyFloor | `polymarket/StrategyFloor.jsx` | ~770 | `/v58/execution-hq`, `/v58/strategy-decisions`, `/dashboard/stats` | ACTIVE, parameterized by strategyId |
| StrategyHistory | `polymarket/StrategyHistory.jsx` | ~280 | `/v58/config/history` | ACTIVE, mostly static data |

**Polymarket Shared Components (7 files):**

| Component | File | Used By |
|-----------|------|---------|
| StatusBar | `polymarket/components/StatusBar.jsx` | Monitor |
| DataHealthStrip | `polymarket/components/DataHealthStrip.jsx` | Monitor |
| SignalSurface | `polymarket/components/SignalSurface.jsx` | Monitor |
| GatePipeline | `polymarket/components/GatePipeline.jsx` | Monitor |
| RecentFlow | `polymarket/components/RecentFlow.jsx` | Monitor |
| WindowAnalysisModal | `polymarket/components/WindowAnalysisModal.jsx` | Evaluate, StrategyLab |
| theme.js | `polymarket/components/theme.js` | ALL polymarket pages |

**Data Surface Pages (5 pages):**
- V1Surface, V2Surface, V3Surface, V4Surface, Assembler1 -- informational views of prediction model outputs

**Execution HQ (1 page + 11 sub-components):**
- ExecutionHQ with LiveTab, RetroTab, ManualTradePanel, GateHeartbeat, GateAuditMatrix, etc.
- Multi-asset parameterized: `/execution-hq/:asset/:timeframe`

**Margin Engine (2 pages):**
- MarginEngine (`/margin`), MarginStrategies (`/margin-strategies`)

**System Pages (7 pages):**
- Config, Schema, Deployments, System, SignalComparison, AuditChecklist, Notes

**Legacy Pages (candidates for removal per CLEANUP-01):**
- Indicators.jsx, Recommendations.jsx, Learn.jsx, AnalysisLibrary.jsx, Changelog.jsx

**Other Legacy (still routed but superseded):**
- Dashboard.jsx (1836 LOC), FactoryFloor.jsx (1645 LOC), V58Monitor.jsx (3118 LOC), PaperDashboard.jsx, WindowResults.jsx

### 1.2 Hardcoded Strategy Metadata (3 Copies)

**Problem:** Strategy names, colors, descriptions, thresholds, and gate configurations are hardcoded in 3 separate locations with no single source of truth.

**Copy 1 — `StrategyLab.jsx` lines 20-61:**
```js
const STRATEGIES_META = [
  { id: 'v4_down_only', label: 'V4 DOWN-ONLY', configKey: 'V4_DOWN_ONLY_MODE',
    description: '...', color: '#10b981', direction: 'DOWN' },
  { id: 'v4_up_asian', ... },
  { id: 'v4_fusion', ... },
  { id: 'v10_gate', ... },
];
```
Missing: `v4_up_basic` (the 5th strategy in Engine v2).

**Copy 2 — `StrategyFloor.jsx` lines 21-53:**
```js
export const STRATEGY_CONFIGS = {
  v4_down_only: { id: 'v4_down_only', label: 'V4 DOWN-ONLY', color: '#10b981',
    direction: 'DOWN', thresholds: { minDist: 0.10, minOffset: 90, maxOffset: 150, ... } },
  v4_up_asian: { ... },
};
```
Only has 2 strategies (down_only and up_asian). Hardcodes gate thresholds.

**Copy 3 — `Evaluate.jsx` lines 38-44:**
```js
const STRATEGY_META = {
  v10_gate: { label: 'V10 Gate', color: '#a855f7' },
  v4_fusion: { label: 'V4 Fusion', color: '#06b6d4' },
  v4_down_only: { label: 'V4 Down-Only', color: '#10b981' },
  v4_up_asian: { label: 'V4 Up Asian', color: '#f59e0b' },
};
```

**Additional duplicates in:**
- `RecentFlow.jsx` lines 50-60: `STRAT_COLORS` + `STRAT_SHORT` objects
- `LiveFloor.jsx`: hardcoded V10/V4 as the two strategy cards
- `WindowAnalysisModal.jsx`: strategy name → color mapping
- `Overview.jsx`: strategy references

**Impact:** Adding `v4_up_basic` (the 5th Engine v2 strategy) requires editing 6+ files. Strategy mode/threshold changes in YAML configs have no path to the frontend.

### 1.3 Hardcoded Gate Names (2 Copies)

**Copy 1 — `theme.js` lines 60-70:**
```js
export const GATE_NAMES = {
  eval_offset: 'EvalOffset', gate_agreement: 'SrcAgree',
  gate_delta: 'Delta', gate_taker: 'Taker',
  gate_cg_veto: 'CGConfirm', gate_dune: 'DUNE',
  gate_spread: 'Spread', gate_cap: 'DynCap',
};
```
This is the V10 8-gate pipeline. Engine v2 has **16 gates** with different names.

**Copy 2 — `Evaluate.jsx` lines 18-27:**
```js
const GATE_PIPELINE = [
  { key: 'eval_offset_bounds', label: 'EvalOffset' },
  { key: 'source_agreement', label: 'SrcAgree' },
  // ... 8 gates
];
```

**Copy 3 — `GatePipeline.jsx` lines 75-78:**
```js
const gateOrder = [
  'eval_offset', 'gate_agreement', 'gate_delta', 'gate_taker',
  'gate_cg_veto', 'gate_dune', 'gate_spread', 'gate_cap',
];
```

**Impact:** Engine v2 gate library has 16 gates (timing, direction, confidence, session_hours, clob_sizing, source_agreement, delta_magnitude, taker_flow, cg_confirmation, spread, dynamic_cap, regime, macro_direction, v3_alignment, trade_advised). Frontend shows only 8 with different key names. Each strategy now has its own gate pipeline defined in YAML, not a shared 8-gate strip.

### 1.4 Dead Components (Confirmed, per CLEANUP-01)

| File | LOC | Reason |
|------|-----|--------|
| `pages/Indicators.jsx` | 500 | Folded into Notes, no unique data |
| `pages/Recommendations.jsx` | 560 | Superseded by StrategyLab |
| `pages/Learn.jsx` | 2182 | Static content, superseded by Notes |
| `pages/AnalysisLibrary.jsx` | 420 | Folded into Notes |
| `pages/Changelog.jsx` | 780 | Static content, replaced by StrategyHistory + Notes |

Total dead: **~4,442 LOC** to remove.

Additionally these are legacy but still functional (lower priority removal):
- `Dashboard.jsx` (1836 LOC) -- superseded by Polymarket Monitor
- `FactoryFloor.jsx` (1645 LOC) -- superseded by LiveFloor
- `V58Monitor.jsx` (3118 LOC) -- superseded by Monitor + Evaluate + StrategyLab
- `PaperDashboard.jsx` (988 LOC) -- superseded by Monitor

### 1.5 Open FE Bugs (from Audit)

- **FE-MONITOR-01**: 5 bugs fixed in PR #133 (DataHealthStrip, SignalSurface, GatePipeline, etc.)
- **FE-MONITOR-01a**: RecentFlow still only shows V10 outcomes from `window_snapshots` -- does not show strategy_decisions dual-strategy data
- Gate Pipeline component renders hardcoded 8-gate strip per V10 -- needs per-strategy gate pipeline

### 1.6 Existing Hub API Endpoints

**Strategy-related endpoints (all under `/api/v58/`):**
- `GET /v58/strategy-decisions?strategy_id=&limit=` -- raw decisions with `metadata_json`
- `GET /v58/strategy-comparison?days=` -- aggregated W/L/accuracy per strategy
- `GET /v58/strategy-windows?days=&limit=` -- per-window multi-strategy comparison
- `GET /v58/strategy-analysis` -- detailed strategy analysis
- `GET /v58/gate-analysis` -- gate-level analysis
- `GET /v58/config?service=engine` -- config keys with current values
- `POST /v58/config/upsert` -- update config value
- `GET /v58/config/history` -- config change history
- `GET /v58/execution-hq?asset=&timeframe=` -- combined window/trade/gate data
- `GET /v58/window-detail/{ts}` -- single window deep dive
- `GET /v58/window-analysis/{ts}` -- per-window eval timeline
- `GET /v58/prediction-surface` -- prediction accuracy by offset

**Data surface endpoints:**
- `GET /v4/snapshot?asset=btc` -- V4 prediction snapshot (via timesfm proxy)
- `GET /v3/snapshot?asset=btc` -- V3 multi-horizon composites
- `GET /dashboard/stats` -- bankroll, engine status

---

## 2. New Screens to Build

### 2A. Strategy Config Dashboard

**Route:** `/polymarket/strategies`

**Purpose:** Central view of all loaded strategy configs. Replaces the hardcoded `STRATEGIES_META` objects across the codebase. Becomes the single source of truth for strategy display data.

**Wireframe:**

```
┌─────────────────────────────────────────────────────────────┐
│  STRATEGY REGISTRY                         5 strategies     │
│  ─────────────────────────────────────────────────────────  │
│                                                             │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐        │
│  │ v4_down_only │ │ v4_up_basic  │ │ v4_up_asian  │        │
│  │ ● LIVE       │ │ ○ GHOST      │ │ ○ GHOST      │        │
│  │ BTC · 5m     │ │ BTC · 5m     │ │ BTC · 5m     │        │
│  │ DOWN         │ │ UP           │ │ UP           │        │
│  │ 4 gates      │ │ 3 gates      │ │ 4 gates      │        │
│  │ 90.3% WR     │ │ — WR         │ │ 0 trades     │        │
│  └──────────────┘ └──────────────┘ └──────────────┘        │
│                                                             │
│  ┌──────────────┐ ┌──────────────┐                          │
│  │ v4_fusion    │ │ v10_gate     │                          │
│  │ ○ GHOST      │ │ ○ GHOST      │                          │
│  │ BTC · 5m     │ │ BTC · 5m     │                          │
│  │ UP+DOWN      │ │ UP+DOWN      │                          │
│  │ custom hook  │ │ 8 gates      │                          │
│  │ — WR         │ │ 51% WR       │                          │
│  └──────────────┘ └──────────────┘                          │
│                                                             │
│  ── EXPAND: v4_down_only ──────────────────────────────     │
│  │ Version: 2.0.0                                    │      │
│  │ Gates:                                            │      │
│  │   1. TimingGate     min=90, max=150  ✅ 98.2%    │      │
│  │   2. DirectionGate  direction=DOWN   ✅ 100%     │      │
│  │   3. ConfidenceGate min_dist=0.10    ✅ 73.1%    │      │
│  │   4. TradeAdvisedGate                ✅ 89.4%    │      │
│  │                                                    │      │
│  │ Sizing: custom (CLOB), fraction=0.025, max=0.10  │      │
│  │ Hooks: v4_down_only.py (clob_sizing)              │      │
│  │                                                    │      │
│  │ Documentation (from .md):                          │      │
│  │   "Trades DOWN signals at dist>=0.10 during..."   │      │
│  └────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────┘
```

**Data sources:**
- NEW: `GET /api/strategies` -- list all loaded configs (name, version, mode, asset, timescale, gates, sizing, direction)
- EXISTING: `GET /v58/strategy-comparison` -- W/L/accuracy per strategy
- EXISTING: `GET /v58/strategy-decisions` -- recent decisions per strategy

**Key behaviors:**
- Cards show mode badge (LIVE=green, GHOST=purple, DISABLED=grey)
- Click card to expand: shows YAML config, gate pipeline with pass rates, .md documentation
- Gate pass rates computed from `strategy_decisions.metadata_json` (contains gate results)
- Mode toggle display only (actual mode changes via Config page or YAML redeploy)

### 2B. Gate Pipeline Monitor

**Route:** `/polymarket/gates`

**Purpose:** Real-time gate evaluation viewer. Shows which gates passed/failed for each strategy per window.

**Wireframe:**

```
┌──────────────────────────────────────────────────────────────┐
│  GATE PIPELINE MONITOR                  Last 50 windows      │
│  ─────────────────────────────────────────────────────────── │
│                                                              │
│  Strategy: [v4_down_only ▼]   Time: [24h ▼]                │
│                                                              │
│  ── Gate Pass Rates ──────────────────────────────────────── │
│  TimingGate      ████████████████████░░ 98.2%  (982/1000)   │
│  DirectionGate   ████████████████████░░ 47.1%  (471/1000)   │
│  ConfidenceGate  ██████████████░░░░░░░░ 73.1%  (346/474)    │
│  TradeAdvisedGate████████████░░░░░░░░░░ 89.4%  (226/253)    │
│  ────                                                        │
│  Pipeline → TRADE: 22.6% of evaluations (226/1000)          │
│                                                              │
│  ── Per-Window Gate Results ─────────────────────────────── │
│  TIME      OFFSET  TIMING  DIR   CONF    ADVISED  ACTION    │
│  21:05     T-102   ✅      ✅    ✅ 0.18  ✅       TRADE     │
│  21:00     T-95    ✅      ❌ UP  —       —        SKIP      │
│  20:55     T-108   ✅      ✅    ❌ 0.07  —        SKIP      │
│  20:50     T-182   ❌      —     —       —        SKIP      │
│  ...                                                         │
│                                                              │
│  ── Skip Reason Breakdown ───────────────────────────────── │
│  DirectionGate: direction=UP    52.9%                        │
│  TimingGate: offset=182 > 150    1.8%                        │
│  ConfidenceGate: dist=0.07 < 0.10  24.2%                    │
│  TradeAdvisedGate: false    21.1%                            │
└──────────────────────────────────────────────────────────────┘
```

**Data sources:**
- EXISTING: `GET /v58/strategy-decisions?strategy_id=X&limit=100` -- decisions with `metadata_json` containing gate results
- NEW: `GET /api/strategies/:id/gate-stats?hours=24` -- aggregated gate pass rates (can be computed client-side from decisions)

**Key behaviors:**
- Strategy selector dropdown (populated from `/api/strategies`)
- Gate results extracted from `metadata_json` field in strategy_decisions
- Short-circuit visualization: when a gate fails, subsequent gates show "---"
- Skip reason breakdown: parsed from `skip_reason` field (format: `"{gate_name}: {reason}"`)
- Historical pass rate bars per gate

### 2C. Data Surface Health

**Route:** `/polymarket/data-health`

**Purpose:** Shows freshness of each data source mapped to FullDataSurface fields.

**Wireframe:**

```
┌───────────────────────────────────────────────────────────────┐
│  DATA SURFACE HEALTH                    Surface age: 1.2s     │
│  ─────────────────────────────────────────────────────────── │
│                                                               │
│  ── Price Layer ─────────────────────────────────────────── │
│  ● Binance WS      $84,231.40    0.3s ago   current_price   │
│  ● Tiingo REST      $84,228.10    1.8s ago   delta_tiingo    │
│  ● Chainlink Oracle  $84,225.00    4.2s ago   delta_chainlink │
│                                                               │
│  ── CLOB Layer ──────────────────────────────────────────── │
│  ● CLOB Feed        UP bid 0.42   2.1s ago   clob_up_bid     │
│  ●                  DN bid 0.58              clob_down_bid   │
│  ○ Gamma            UP $0.43      5m ago     gamma_up_price  │
│                                                               │
│  ── Prediction Layer ────────────────────────────────────── │
│  ● V2 Probability   p_up: 0.381   3.2s ago   v2_probability  │
│  ● V3 5m Composite  +0.023        3.2s ago   v3_5m_composite │
│  ○ V3 15m+          (6 timescales)  stale    v3_15m..v3_2w   │
│  ● V4 Regime        calm_trend    3.2s ago   v4_regime        │
│  ● V4 Macro         BULL          3.2s ago   v4_macro_bias    │
│  ● V4 Consensus     safe=true     3.2s ago   v4_consensus     │
│  ● V4 Conviction    HIGH (0.82)   3.2s ago   v4_conviction    │
│  ● Poly Outcome     DOWN advised  3.2s ago   poly_direction   │
│                                                               │
│  ── Market Context Layer ────────────────────────────────── │
│  ● CoinGlass OI     $18.2B        8.4s ago   cg_oi_usd       │
│  ● CoinGlass Fund   +0.0021%      8.4s ago   cg_funding_rate │
│  ● VPIN             0.623         0.8s ago   vpin             │
│  ● Regime           NORMAL        0.8s ago   regime           │
│  ● TWAP Delta       +0.012%       1.5s ago   twap_delta       │
│                                                               │
│  ── TimesFM Layer ───────────────────────────────────────── │
│  ● Expected Move     +12 bps      3.2s ago   timesfm_exp_move│
│  ● Vol Forecast      28 bps       3.2s ago   timesfm_vol     │
│                                                               │
│  Legend: ● fresh (<10s)  ◉ warm (10-30s)  ○ stale (>30s)     │
│          ◌ unavailable                                        │
└───────────────────────────────────────────────────────────────┘
```

**Data sources:**
- NEW: `GET /api/data-surface/health` -- per-field freshness (assembled_at, per-source last_updated timestamps)
- EXISTING: `GET /v4/snapshot?asset=btc` -- V4 data surface fields (partial)
- EXISTING: `GET /v3/snapshot?asset=btc` -- V3 composites
- Could also use: `GET /v58/execution-hq` which already returns some health data

**Key behaviors:**
- Polls every 5s
- Each row maps to specific FullDataSurface fields
- Color-coded freshness indicators (green <10s, yellow 10-30s, red >30s)
- Groups by layer (Price, CLOB, Prediction, Market Context, TimesFM)
- Shows actual field names from FullDataSurface for developer reference

### 2D. Strategy Lab Enhancement

**Route:** `/polymarket/strategy-lab` (existing page, enhanced)

**Current state:** StrategyLab has Tab A (Historical Replay), Tab B (Gate Impact), Tab C (Shadow Comparison). Strategy selector panel at top with LIVE/GHOST/OFF toggles.

**Enhancements:**

1. **Add v4_up_basic to STRATEGIES_META** -- currently missing from all 3 copies
2. **Side-by-side strategy comparison per window:**
   - Show all 5 strategies' decisions for the same window in one row
   - Columns: window_ts, direction, action, confidence, skip_reason per strategy
3. **Confidence distribution chart per strategy:**
   - Histogram of confidence_score values grouped by strategy
   - Data from `/v58/strategy-decisions?strategy_id=X`
4. **Timing distribution chart:**
   - Which eval_offset each strategy trades at
   - Shows sweet spot vs actual trade timing
5. **Win rate by strategy with confidence intervals:**
   - From `/v58/strategy-comparison` (already exists)
   - Add error bars based on sample size
6. **Make gate definitions dynamic:**
   - Currently `GATE_DEFS` in StrategyLab.jsx is a hardcoded 8-gate list
   - Should load gate definitions from strategy config API

---

## 3. Existing Screens to Improve

### 3.1 Dynamic Strategy Names (HIGH Priority)

**Problem:** 6+ files have hardcoded strategy metadata objects.

**Solution:** Create a shared `strategyRegistry.js` module that:
1. Exports a `useStrategies()` hook that fetches strategy configs from API
2. Falls back to a static default when API unavailable
3. Provides: `getStrategyColor(id)`, `getStrategyLabel(id)`, `getStrategyGates(id)`, `getStrategyThresholds(id)`
4. Replaces all 3 copies of `STRATEGIES_META`/`STRATEGY_META`/`STRATEGY_CONFIGS`

### 3.2 Consolidate Gate Display

**Problem:** `GATE_NAMES` in theme.js has 8 V10 gates. Engine v2 has 16 gates. Each strategy has its own subset.

**Solution:**
1. Create `gateRegistry.js` with full 16-gate catalog (name, label, description)
2. `GatePipeline.jsx` reads strategy-specific gate list from API (or from strategy config)
3. `GateChip` component reused across Monitor, Evaluate, Gate Pipeline Monitor
4. Gate order is per-strategy (from YAML config), not global

### 3.3 Monitor Page (`polymarket/Monitor.jsx`)

- **StatusBar:** Pull strategy modes from API, show LIVE strategy name dynamically
- **GatePipeline (Band 4):** Show gates for the LIVE strategy (not fixed 8-gate V10 strip)
- **RecentFlow (Band 5):** Show strategy_decisions for multiple strategies per window (FE-MONITOR-01a)
- **DataHealthStrip (Band 2):** Already partially maps to FullDataSurface -- extend with V3 and CoinGlass fields

### 3.4 LiveFloor Page (`polymarket/LiveFloor.jsx`)

- Currently hardcodes V10 + V4 as the two strategy cards (lines 59-73)
- Should dynamically render cards for all non-DISABLED strategies
- Show gate results per strategy from `metadata_json`

### 3.5 Evaluate Page (`polymarket/Evaluate.jsx`)

- Replace `STRATEGY_META` (4 strategies, hardcoded) with `useStrategies()` hook
- Replace `GATE_PIPELINE` (8 gates, hardcoded) with strategy-specific gate lists
- Strategy comparison table already supports dynamic strategies via fallback (`strategyMeta()` function)

### 3.6 StrategyFloor Page (`polymarket/StrategyFloor.jsx`)

- Currently only supports `v4_down_only` and `v4_up_asian` in `STRATEGY_CONFIGS`
- Should be parameterized by any strategy ID
- Gate thresholds should come from API, not hardcoded `thresholds` object
- Add routes for all 5 strategies: `/polymarket/strategy/:id`

### 3.7 Remove Dead Components (per CLEANUP-01)

Delete these 5 files + remove routes from App.jsx + remove nav items from Layout.jsx:
1. `pages/Indicators.jsx`
2. `pages/Recommendations.jsx`
3. `pages/Learn.jsx`
4. `pages/AnalysisLibrary.jsx`
5. `pages/Changelog.jsx`

---

## 4. Hub API Endpoints Needed

### 4.1 New Endpoints Required

**`GET /api/strategies`** -- List all loaded strategy configs
```json
{
  "strategies": [
    {
      "id": "v4_down_only",
      "version": "2.0.0",
      "mode": "LIVE",
      "asset": "BTC",
      "timescale": "5m",
      "direction": "DOWN",
      "gates": [
        { "type": "timing", "params": { "min_offset": 90, "max_offset": 150 } },
        { "type": "direction", "params": { "direction": "DOWN" } },
        { "type": "confidence", "params": { "min_dist": 0.10 } },
        { "type": "trade_advised", "params": {} }
      ],
      "sizing": { "type": "custom", "fraction": 0.025, "max_collateral_pct": 0.10 },
      "hooks_file": "v4_down_only.py",
      "description": "Trades DOWN signals at dist>=0.10 during T-90-150 window.",
      "color": "#10b981"
    },
    // ... 4 more strategies
  ]
}
```

**Implementation:** Read YAML configs from `engine/strategies/configs/` and return as JSON. Initially can be a static read; later, StrategyRegistry can expose its loaded configs via an internal API.

**Two implementation approaches:**
- **Approach A (simple):** Hub reads YAML files directly at startup from a shared config path
- **Approach B (proper):** Engine exposes an internal endpoint (e.g., `GET /internal/strategies`), Hub proxies it. This is cleaner since the engine owns the registry.

Recommend **Approach B** since the engine's StrategyRegistry already has this data in memory.

---

**`GET /api/strategies/:id/config`** -- Single strategy detail
```json
{
  "id": "v4_down_only",
  "version": "2.0.0",
  "yaml_raw": "name: v4_down_only\nversion: '2.0.0'\n...",
  "documentation_md": "# v4_down_only\n\nTrades DOWN signals...",
  "gates": [...],
  "sizing": {...},
  "hooks": { "file": "v4_down_only.py", "pre_gate": null, "post_gate": null, "sizing_hook": "clob_sizing" }
}
```

---

**`GET /api/strategies/:id/decisions`** -- Recent decisions with gate results

This is essentially the existing `/v58/strategy-decisions?strategy_id=X` but with guaranteed gate result parsing from `metadata_json`. The existing endpoint already returns `metadata` (parsed JSON). **No new endpoint needed** -- just ensure the metadata_json always includes gate results.

The engine v2 `StrategyRegistry._evaluate_one()` should write gate results into `metadata_json`:
```json
{
  "gate_results": [
    { "gate": "timing", "passed": true, "reason": "offset=102 in [90,150]" },
    { "gate": "direction", "passed": true, "reason": "DOWN matches config" },
    { "gate": "confidence", "passed": true, "reason": "dist=0.18 >= 0.10" },
    { "gate": "trade_advised", "passed": true, "reason": "trade_advised=true" }
  ],
  "surface_snapshot": { "delta_pct": -0.0023, "vpin": 0.623, "v2_probability_up": 0.381 }
}
```

---

**`GET /api/data-surface/health`** -- Feed freshness metrics
```json
{
  "assembled_at": 1681400000.123,
  "surface_age_ms": 1200,
  "sources": {
    "binance_ws": { "status": "green", "last_tick_age_ms": 340, "value": 84231.40 },
    "tiingo": { "status": "green", "last_poll_age_ms": 1800, "value": 84228.10 },
    "chainlink": { "status": "green", "last_poll_age_ms": 4200, "value": 84225.00 },
    "clob": { "status": "green", "last_poll_age_ms": 2100, "up_bid": 0.42, "down_bid": 0.58 },
    "gamma": { "status": "yellow", "last_poll_age_ms": 298000, "up_price": 0.43 },
    "coinglass": { "status": "green", "last_poll_age_ms": 8400, "oi_usd": 18200000000 },
    "v4_snapshot": { "status": "green", "last_fetch_age_ms": 3200, "v2_prob_up": 0.381 },
    "v3_composites": { "status": "green", "available_timescales": ["5m", "15m", "1h", "4h"], "missing": ["24h", "48h", "72h", "1w", "2w"] },
    "vpin": { "status": "green", "age_ms": 800, "value": 0.623 },
    "twap": { "status": "green", "age_ms": 1500, "delta": 0.012 }
  }
}
```

**Implementation:** DataSurfaceManager exposes freshness data. Engine serves it on an internal endpoint. Hub proxies.

---

**`GET /api/gates`** -- Gate library catalog
```json
{
  "gates": [
    { "type": "timing", "label": "Timing Gate", "description": "Checks eval_offset is in window", "params_schema": { "min_offset": "int", "max_offset": "int" } },
    { "type": "direction", "label": "Direction Gate", "description": "Filters by prediction direction", "params_schema": { "direction": "UP|DOWN|ANY" } },
    // ... 14 more gates
  ]
}
```

This could be static (generated from gate class metadata) or served from registry at startup.

### 4.2 Existing Endpoints — Sufficient

These existing endpoints already provide what the new screens need:

| Endpoint | Used By | Notes |
|----------|---------|-------|
| `GET /v58/strategy-decisions` | Gate Pipeline Monitor, StrategyLab | Already returns metadata_json with gate results |
| `GET /v58/strategy-comparison` | Strategy Config Dashboard, Evaluate | Aggregated W/L per strategy |
| `GET /v58/strategy-windows` | Evaluate, StrategyLab | Per-window multi-strategy comparison |
| `GET /v58/config?service=engine` | StrategyLab config panel | Strategy mode values |
| `GET /v4/snapshot` | Data Surface Health | V4/V2/V3 prediction data |
| `GET /v3/snapshot` | Data Surface Health | V3 multi-horizon composites |
| `GET /v58/execution-hq` | Monitor, LiveFloor | Combined window/gate/trade data |

### 4.3 Summary of New API Work

| Endpoint | Priority | Backend Effort | Notes |
|----------|----------|---------------|-------|
| `GET /api/strategies` | P0 | Medium | Core dependency for all new screens. Needs engine internal API. |
| `GET /api/strategies/:id/config` | P1 | Low | Extension of above, returns YAML + .md |
| `GET /api/data-surface/health` | P1 | Medium | Needs DataSurfaceManager to expose freshness |
| `GET /api/gates` | P2 | Low | Static catalog, can be hardcoded initially |
| metadata_json gate results | P0 | Low | Engine v2 registry already designed to write this |

---

## 5. Component Architecture

### 5.1 Shared Data Hooks

**`/src/hooks/useStrategies.js`**
```js
// Fetches strategy configs from /api/strategies
// Returns: { strategies, loading, error, getStrategy(id), getColor(id), getLabel(id) }
// Caches in React context (StrategyRegistryContext) to avoid redundant fetches
// Falls back to STATIC_DEFAULTS when API unavailable
```

**`/src/hooks/useGateResults.js`**
```js
// Fetches gate results for a strategy from /v58/strategy-decisions
// Parses metadata_json.gate_results
// Returns: { decisions, gatePassRates, skipReasons, loading }
// Accepts: strategyId, limit, hours
```

**`/src/hooks/useDataSurfaceHealth.js`**
```js
// Fetches from /api/data-surface/health
// Polls every 5s
// Returns: { sources, surfaceAge, loading }
```

**`/src/hooks/useStrategyComparison.js`**
```js
// Fetches from /v58/strategy-comparison?days=N
// Returns: { strategies: [{ id, wins, losses, accuracy, pnl }], loading }
// Already partially exists in Evaluate.jsx — extract to shared hook
```

### 5.2 Shared Components

**`/src/pages/polymarket/components/StrategyBadge.jsx`**
- Renders strategy name with color dot
- Props: `strategyId`, optionally `mode`
- Uses `useStrategies()` for color/label lookup

**`/src/pages/polymarket/components/ModeBadge.jsx`**
- Renders LIVE/GHOST/DISABLED/OFF pill
- Props: `mode`
- Colors: LIVE=green, GHOST=purple, DISABLED/OFF=grey

**`/src/pages/polymarket/components/GateResultChip.jsx`**
- Renders a single gate result (pass/fail with reason)
- Props: `gate`, `passed`, `reason`, `value`, `threshold`
- Replaces the existing `GateChip` in GatePipeline.jsx

**`/src/pages/polymarket/components/GatePipelineStrip.jsx`**
- Renders a horizontal strip of GateResultChips for a strategy
- Props: `gates[]` (from metadata_json.gate_results)
- Per-strategy gate order (not global 8-gate)
- Replaces the hardcoded gate strip in `GatePipeline.jsx`

**`/src/pages/polymarket/components/FreshnessIndicator.jsx`**
- Green/yellow/red dot + age label
- Props: `ageMs`, `thresholds: { green, yellow }`

### 5.3 Context Providers

**`/src/contexts/StrategyRegistryContext.jsx`**
```jsx
// Wraps the app (inside AuthProvider)
// Fetches strategy configs once on mount
// Provides: strategies[], getStrategy(id), gates[], isLoading
// Refreshes on config change (optional)
```

### 5.4 Shared Constants to Centralize

**`/src/pages/polymarket/components/strategyDefaults.js`**
```js
// Static fallback when /api/strategies is unavailable
// Contains the 5 known strategies with colors, labels, directions
// Used by useStrategies() hook as fallback
// SINGLE SOURCE OF TRUTH — replaces all 3 copies
export const STRATEGY_DEFAULTS = {
  v4_down_only: { label: 'V4 DOWN-ONLY', color: '#10b981', direction: 'DOWN' },
  v4_up_basic:  { label: 'V4 UP BASIC',  color: '#3b82f6', direction: 'UP' },
  v4_up_asian:  { label: 'V4 UP ASIAN',  color: '#f59e0b', direction: 'UP' },
  v4_fusion:    { label: 'V4 FUSION',    color: '#06b6d4', direction: null },
  v10_gate:     { label: 'V10 GATE',     color: '#a855f7', direction: null },
};

export const GATE_CATALOG = {
  timing: { label: 'Timing', description: 'Eval offset within window' },
  direction: { label: 'Direction', description: 'Prediction direction filter' },
  confidence: { label: 'Confidence', description: 'Distance from 0.5 threshold' },
  session_hours: { label: 'Session Hours', description: 'UTC hour filter' },
  clob_sizing: { label: 'CLOB Sizing', description: 'Position size from CLOB data' },
  source_agreement: { label: 'Source Agreement', description: 'Price sources agree' },
  delta_magnitude: { label: 'Delta Magnitude', description: 'Minimum price delta' },
  taker_flow: { label: 'Taker Flow', description: 'Taker buy/sell alignment' },
  cg_confirmation: { label: 'CG Confirmation', description: 'CoinGlass OI + liquidations' },
  spread: { label: 'Spread', description: 'CLOB spread reasonable' },
  dynamic_cap: { label: 'Dynamic Cap', description: 'Entry cap from confidence' },
  regime: { label: 'Regime', description: 'HMM regime filter' },
  macro_direction: { label: 'Macro Direction', description: 'Macro bias alignment' },
  v3_alignment: { label: 'V3 Alignment', description: 'Cross-timescale agreement' },
  trade_advised: { label: 'Trade Advised', description: 'V4 trade_advised flag' },
};
```

### 5.5 Data Flow Diagram

```
┌───────────────────────────────────────────────────────────────┐
│                       React App                               │
│                                                               │
│  StrategyRegistryContext ─────────────────────────────────    │
│  │  Fetches /api/strategies once                          │   │
│  │  Provides: strategies[], getStrategy(id)               │   │
│  │                                                        │   │
│  │  Used by:                                              │   │
│  │  ├─ StrategyConfigDashboard (new)                      │   │
│  │  ├─ GatePipelineMonitor (new)                          │   │
│  │  ├─ Monitor (existing, replace GATE_NAMES)             │   │
│  │  ├─ LiveFloor (existing, replace hardcoded V10/V4)     │   │
│  │  ├─ Evaluate (existing, replace STRATEGY_META)         │   │
│  │  ├─ StrategyLab (existing, replace STRATEGIES_META)    │   │
│  │  └─ StrategyFloor (existing, replace STRATEGY_CONFIGS) │   │
│  └────────────────────────────────────────────────────────    │
│                                                               │
│  useGateResults(strategyId) ─────────────────────────────    │
│  │  Fetches /v58/strategy-decisions?strategy_id=X         │   │
│  │  Parses metadata_json.gate_results                     │   │
│  │  Returns: decisions[], gatePassRates, skipReasons       │   │
│  │                                                        │   │
│  │  Used by:                                              │   │
│  │  ├─ GatePipelineMonitor (new)                          │   │
│  │  ├─ StrategyConfigDashboard (new, expanded view)       │   │
│  │  └─ StrategyFloor (existing, gate display)             │   │
│  └────────────────────────────────────────────────────────    │
│                                                               │
│  useDataSurfaceHealth() ─────────────────────────────────    │
│  │  Fetches /api/data-surface/health every 5s             │   │
│  │  Returns: sources, surfaceAge                           │   │
│  │                                                        │   │
│  │  Used by:                                              │   │
│  │  ├─ DataSurfaceHealth (new)                            │   │
│  │  └─ DataHealthStrip (existing, enhanced)               │   │
│  └────────────────────────────────────────────────────────    │
└───────────────────────────────────────────────────────────────┘
```

---

## 6. Priority Order for Implementation

### Phase 1: Foundation (must do first)

| # | Task | Effort | Dependency |
|---|------|--------|------------|
| 1.1 | Create `strategyDefaults.js` with STRATEGY_DEFAULTS + GATE_CATALOG | 1h | None |
| 1.2 | Create `useStrategies()` hook with static fallback | 2h | 1.1 |
| 1.3 | Create `StrategyRegistryContext` provider, add to App.jsx | 1h | 1.2 |
| 1.4 | Create shared components: StrategyBadge, ModeBadge, GateResultChip, GatePipelineStrip, FreshnessIndicator | 3h | 1.1 |
| 1.5 | Delete 5 dead pages + remove routes + remove nav items | 1h | None |

**Phase 1 total: ~8h**

### Phase 2: Migrate Existing Pages (can parallelize)

| # | Task | Effort | Dependency |
|---|------|--------|------------|
| 2.1 | Refactor StrategyLab.jsx: replace STRATEGIES_META with useStrategies() | 2h | Phase 1 |
| 2.2 | Refactor Evaluate.jsx: replace STRATEGY_META + GATE_PIPELINE | 1.5h | Phase 1 |
| 2.3 | Refactor StrategyFloor.jsx: replace STRATEGY_CONFIGS, make generic for any strategy | 2h | Phase 1 |
| 2.4 | Refactor LiveFloor.jsx: dynamic strategy cards instead of hardcoded V10/V4 | 1.5h | Phase 1 |
| 2.5 | Refactor GatePipeline.jsx: per-strategy gate strip | 2h | Phase 1 |
| 2.6 | Refactor RecentFlow.jsx: show multi-strategy decisions per window | 1.5h | Phase 1 |
| 2.7 | Refactor DataHealthStrip.jsx: add V3, CoinGlass, map to FullDataSurface | 1h | Phase 1 |
| 2.8 | Add `/polymarket/strategy/:id` dynamic route, update nav | 1h | 2.3 |
| 2.9 | Refactor theme.js: remove GATE_NAMES, update SIGNAL_NAMES | 0.5h | Phase 1 |

**Phase 2 total: ~13h (parallelizable to ~6h)**

### Phase 3: New Screens

| # | Task | Effort | Dependency |
|---|------|--------|------------|
| 3.1 | Build Strategy Config Dashboard (`/polymarket/strategies`) | 4h | Phase 1-2 |
| 3.2 | Build Gate Pipeline Monitor (`/polymarket/gates`) | 4h | Phase 1-2 |
| 3.3 | Build Data Surface Health (`/polymarket/data-health`) | 3h | Phase 1-2, needs backend endpoint |
| 3.4 | Enhance StrategyLab: side-by-side comparison, confidence distribution | 3h | Phase 1-2 |
| 3.5 | Add nav items for new pages to Layout.jsx | 0.5h | 3.1-3.3 |

**Phase 3 total: ~14.5h**

### Phase 4: Backend API (can run in parallel with Phase 2-3 frontend)

| # | Task | Effort | Dependency |
|---|------|--------|------------|
| 4.1 | Engine: expose `/internal/strategies` from StrategyRegistry | 2h | Engine v2 registry |
| 4.2 | Hub: `GET /api/strategies` proxy endpoint | 1.5h | 4.1 |
| 4.3 | Hub: `GET /api/strategies/:id/config` (YAML + .md) | 1h | 4.1 |
| 4.4 | Engine: DataSurfaceManager exposes freshness metrics | 2h | Engine v2 data surface |
| 4.5 | Hub: `GET /api/data-surface/health` proxy endpoint | 1h | 4.4 |
| 4.6 | Hub: `GET /api/gates` static catalog | 0.5h | None |
| 4.7 | Engine: write gate_results into strategy_decisions.metadata_json | 1h | Engine v2 registry |

**Phase 4 total: ~9h**

### Total Estimated Effort

| Phase | Hours | Can Start |
|-------|-------|-----------|
| Phase 1: Foundation | 8h | Now |
| Phase 2: Migrate Existing | 13h (6h parallelized) | After Phase 1 |
| Phase 3: New Screens | 14.5h | After Phase 1-2 |
| Phase 4: Backend API | 9h | After Engine v2 merges |
| **Total** | **~44.5h** | |

**Critical path:** Phase 1 (8h) -> Phase 2 (6h) -> Phase 3 (14.5h) = **~28.5h**

Phase 4 (backend API) can run in parallel but Phase 3 screens need it to show real data instead of static fallbacks.

---

## 7. Implementation Notes

### 7.1 Before API Exists

During Phase 1-2, the `useStrategies()` hook falls back to `STRATEGY_DEFAULTS` from `strategyDefaults.js`. This means:
- All existing pages work immediately with the new shared constants
- When `/api/strategies` becomes available, pages automatically start using real data
- No blocking on backend work for frontend refactoring

### 7.2 Gate Results in metadata_json

The engine v2 registry writes gate results into `strategy_decisions.metadata_json`. Format:
```json
{
  "gate_results": [
    { "gate": "timing", "passed": true, "reason": "..." },
    { "gate": "direction", "passed": false, "reason": "..." }
  ]
}
```

The Gate Pipeline Monitor page parses this. Until Engine v2 is deployed, the existing `metadata_json` may have different formats -- the hook should handle both gracefully.

### 7.3 Routing Changes Summary

**Add:**
- `/polymarket/strategies` -- Strategy Config Dashboard
- `/polymarket/gates` -- Gate Pipeline Monitor
- `/polymarket/data-health` -- Data Surface Health
- `/polymarket/strategy/:id` -- Generic StrategyFloor for any strategy

**Remove:**
- `/indicators` -- dead page
- `/recommendations` -- dead page
- `/learn` -- dead page
- `/analysis` -- dead page
- `/changelog` -- dead page

**Keep (but deprioritize in nav):**
- `/polymarket/down-only` and `/polymarket/up-asian` -- redirect to `/polymarket/strategy/v4_down_only` etc.

### 7.4 Nav Structure Update

Current POLYMARKET section in Layout.jsx:
```
POLYMARKET
  Overview, Monitor, Floor, Evaluate, DOWN Floor, UP Floor, Strategy Lab, History, Configure
```

Proposed:
```
POLYMARKET
  Monitor          -- primary dashboard
  Floor            -- live trading view
  Strategies       -- (NEW) strategy config dashboard
  Gates            -- (NEW) gate pipeline monitor
  Data Health      -- (NEW) data surface health
  Evaluate         -- performance analysis
  Strategy Lab     -- historical replay / what-if
  History          -- config changelog
  Configure        -- DB config browser
```

Remove: DOWN Floor, UP Floor (replaced by `/polymarket/strategy/:id` links within Strategies page).

### 7.5 Files to Create

```
frontend/src/
├── contexts/
│   └── StrategyRegistryContext.jsx        (NEW)
├── hooks/
│   ├── useStrategies.js                   (NEW)
│   ├── useGateResults.js                  (NEW)
│   ├── useDataSurfaceHealth.js            (NEW)
│   └── useStrategyComparison.js           (NEW)
├── pages/polymarket/
│   ├── StrategyConfigDashboard.jsx        (NEW)
│   ├── GatePipelineMonitor.jsx            (NEW)
│   ├── DataSurfaceHealth.jsx              (NEW)
│   └── components/
│       ├── strategyDefaults.js            (NEW)
│       ├── StrategyBadge.jsx              (NEW)
│       ├── ModeBadge.jsx                  (NEW)
│       ├── GateResultChip.jsx             (NEW)
│       ├── GatePipelineStrip.jsx          (NEW — replaces gate logic in GatePipeline.jsx)
│       └── FreshnessIndicator.jsx         (NEW)
```

### 7.6 Files to Delete

```
frontend/src/pages/
├── Indicators.jsx         (DELETE — dead)
├── Recommendations.jsx    (DELETE — dead)
├── Learn.jsx              (DELETE — dead)
├── AnalysisLibrary.jsx    (DELETE — dead)
├── Changelog.jsx          (DELETE — dead)
```

### 7.7 Files to Modify

```
frontend/src/
├── App.jsx                                (add new routes, remove dead routes)
├── components/Layout.jsx                  (update nav items)
├── pages/polymarket/
│   ├── StrategyLab.jsx                    (replace STRATEGIES_META + GATE_DEFS)
│   ├── StrategyFloor.jsx                  (replace STRATEGY_CONFIGS, make generic)
│   ├── Evaluate.jsx                       (replace STRATEGY_META + GATE_PIPELINE)
│   ├── LiveFloor.jsx                      (dynamic strategy cards)
│   ├── Overview.jsx                       (use shared strategy metadata)
│   └── components/
│       ├── theme.js                       (remove GATE_NAMES)
│       ├── GatePipeline.jsx               (per-strategy gates)
│       ├── RecentFlow.jsx                 (multi-strategy per window)
│       ├── DataHealthStrip.jsx            (map to FullDataSurface)
│       └── WindowAnalysisModal.jsx        (use shared strategy metadata)
```
