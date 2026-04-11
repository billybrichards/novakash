# Frontend Audit — 2026-04-11

**Scope:** READ-ONLY audit of every route in the novakash frontend (develop branch),
focused on live-trading readiness now that paper-mode live trading is paused and
about to resume.

**Verdict up top (critical path, 4 items):**

| Critical-path item | Status | Component |
|---|---|---|
| Gate heartbeat (10s refresh, UI-01) | **OK** | `frontend/src/pages/execution-hq/components/GateHeartbeat.jsx` |
| Manual trade panel (writes pending_live + operator_rationale) | **OK** | `frontend/src/pages/execution-hq/components/ManualTradePanel.jsx` |
| Decision snapshot DB capture on click (LT-03) | **OK** | `hub/api/v58_monitor.py::_capture_trade_snapshot` |
| Multi-market monitors (BTC/ETH/SOL/XRP × 5m/15m) | **BROKEN (not started)** | UI-02 is OPEN; today is BTC-5m only |

---

## 1. Executive summary

The novakash frontend has **31 routes** declared in `App.jsx` (29 route paths plus
an index redirect and a 404 wildcard redirect). Of the 29 user-visible routes:

- **21 are OK or partial-OK** — they call real hub endpoints, have reasonable
  loading/error states, and back onto matching FastAPI routes defined in
  `hub/api/*.py`.
- **4 are STALE** — they work, but surface legacy Polymarket-v7/v8 strategy data
  (`/signals`, `/pnl`, `/trades`, `/positions`) that is not primary for the
  Polymarket-v10.6 live-trading path. They should not block live trading but
  carry stale wording.
- **2 are placeholder / demo-heavy** — `/indicators` uses 100% mock data from
  `src/lib/mock-data.js`; `/dashboard` + `/paper` use seeded demo-data fallbacks
  if the hub returns empty arrays (not broken, but silently covers up backend
  gaps).
- **2 are mis-scoped for current state** — `/live` (LiveTrading.jsx) renders a
  v7.x-era "wallet status" view that duplicates Execution HQ and predates the
  manual-trade panel; `/v58` (V58Monitor.jsx) is a 3118-line V58 monitor that is
  still functional but largely superseded by `/execution-hq`.

The four critical-path live-trading components land cleanly end-to-end in
source: gate heartbeat is driven by `/api/v58/execution-hq → gate_heartbeat`
polled every 10s by ExecutionHQ; manual trade clicks go through
`/api/v58/manual-trade` which writes `manual_trades` AND calls
`_capture_trade_snapshot` into `manual_trade_snapshots`; the frontend
`ManualTradePanel.jsx` has the LT-03 `operator_rationale` textarea wired into
the POST body. The one remaining critical gap is multi-market (UI-02): today
the engine and frontend are both still BTC-5m scoped, and UI-02 is tracked as
OPEN in `AuditChecklist.jsx`.

**Bottom line:** the single-market critical path is ready for live trading to
resume. The multi-market scaling item is deliberately deferred to UI-02 and is
not a blocker for enabling live trading on BTC-5m. The cleanup work below is
about stale adjacent pages that a confused operator might land on.

---

## 2. Route inventory

All routes live behind `<ProtectedRoute><Layout/></ProtectedRoute>` except `/login`.

| Path | Component | Status | Note |
|---|---|---|---|
| `/login` | `auth/LoginPage.jsx` | OK | Public auth screen. |
| `/` (index) | `Navigate → /dashboard` | OK | Redirect only. |
| `/dashboard` | `pages/Dashboard.jsx` | PARTIAL | Hits 11 `/api/dashboard/*` endpoints but falls back to seeded demo data if any return empty. Charts still render even with a dead backend. |
| `/paper` | `pages/PaperDashboard.jsx` | PARTIAL | Hits `/api/paper/trades,stats,equity`. Falls back to `genPaperTrades()` if trades are empty. Has a simulated VPIN ticker that drifts regardless of real feed state. |
| `/positions` | `pages/Positions.jsx` | PARTIAL | Hits `/api/trades?status=OPEN&mode=paper`. Falls back to `genPositionsDemo()` on error. Strategy filters hardcoded to `sub_dollar_arb`/`vpin_cascade` (legacy v7 taxonomy). |
| `/trades` | `pages/Trades.jsx` | STALE | Hits `/api/trades` and `/api/trades/stats`. No fallback. Page is fine but the filter taxonomy is v7 era. |
| `/signals` | `pages/Signals.jsx` | STALE | Hits `/api/signals/vpin,cascade,arb`. v7.x strategy tabs, does not surface v10.6 gate results. |
| `/pnl` | `pages/PnL.jsx` | STALE | Hits `/api/pnl/cumulative,daily,monthly,by-strategy`. Legacy "arb_pnl / vpin_pnl" split in the stats header. |
| `/risk` | `pages/Risk.jsx` | PARTIAL | Hits `/api/system/status` + `/api/trades?mode=paper`. Falls back to `genRiskDemo()` on error. Alerts computed client-side from system status. |
| `/system` | `pages/System.jsx` | OK | Hits `/api/system/status,kill,resume,paper-mode`. Kill switch + feed list. Feed list hardcoded (Binance, CoinGlass, Chainlink, Polymarket, Opinion). |
| `/config` | `Navigate → /trading-config` | OK | Redirect only. |
| `/trading-config` | `pages/TradingConfig.jsx` | OK | Full CRUD on `/api/trading-config/*` — defaults, list, live-status, create, update, clone, activate, approve, delete. Writes real state. |
| `/setup` | `pages/Setup.jsx` | OK | Form-based config of external API keys. Hits `/api/setup` + `/api/setup/test-telegram`. |
| `/learn` | `pages/Learn.jsx` | OK | Mostly static docs + one `/api/dashboard/vpin-history` canvas. No live trading concern. |
| `/changelog` | `pages/Changelog.jsx` | STALE | 100% hardcoded release notes, last entry is v7.2 (2026-04-06). No v10.6 / v11.x entries. |
| `/playwright` | `pages/PlaywrightDashboard.jsx` | OK | Hits `/api/playwright/status,balance,positions,redeemable,history,screenshot,redeem`. Real data against the Polymarket playwright bot. |
| `/timesfm` | `pages/TimesFM.jsx` | OK (direct) | Raw `fetch('/timesfm/v2/probability', '/timesfm/forecast', '/timesfm/v2/health')` via nginx proxy — bypasses hub JWT. Works but inconsistent with the rest of the app (no auth wall). |
| `/indicators` | `pages/Indicators.jsx` | PLACEHOLDER | Pure mock data from `lib/mock-data.js`. No API calls at all. |
| `/v58` | `pages/V58Monitor.jsx` | STALE | 3118-line monitor — still works, hits `/v58/windows,stats,price-history,outcomes,accuracy,gate-analysis,live-prices,manual-trade,manual-trades`. Largely superseded by `/execution-hq`. |
| `/windows` | `pages/WindowResults.jsx` | OK | Hits `/v58/outcomes` and `/v58/window-detail/{ts}`. Per-window expand + gate badges. |
| `/strategy` | `pages/StrategyAnalysis.jsx` | OK | Hits `/v58/strategy-analysis`. Explainer UI with two WR columns (oracle vs directional). |
| `/live` | `pages/LiveTrading.jsx` | STALE | Hits `/v58/wallet-status`. Wallet/trade summary view from the v7.x era. Does NOT have the manual trade panel or gate heartbeat — those moved to `/execution-hq`. Still reachable from sidebar as "Live Trading". |
| `/analysis` | `pages/AnalysisLibrary.jsx` | OK | Raw `fetch('/api/analysis')` with Bearer token from localStorage. Document reader UI. |
| `/factory` | `pages/FactoryFloor.jsx` | OK | Hits `/v58/windows,outcomes,accuracy,stats,live-prices` and `/system/status`. Pipeline visualiser. |
| `/execution-hq` | `pages/execution-hq/ExecutionHQ.jsx` | **OK (critical)** | Hits `/v58/execution-hq?limit=200` + `/dashboard/stats`. Polls every 10s on Live tab. Embeds GateHeartbeat, LiveTab, RetroTab, TradeTicker, TradeToast, ManualTradePanel. |
| `/margin` | `pages/margin-engine/MarginEngine.jsx` | OK | Hits `/margin/status`, `/v3/snapshot?asset=BTC`, `/v4/snapshot?asset=BTC&timescales=5m,15m,1h,4h&strategy=fee_aware_15m`. Polls every 5s on Live tab. V4Panel, SignalPanel, PositionsPanel, TradeTimelinePanel. |
| `/composite` | `pages/CompositeSignals.jsx` | OK (stale-naming) | Hits `/api/v3/snapshot?asset=BTC`. Renders 9 timescales with 7-signal bars. References `elm` as a signal key (SQ-01 scope). |
| `/recommendations` | `pages/Recommendations.jsx` | OK | Hits `/trades?is_live=true` + `/v58/outcomes`. Recalibration heuristics UI. |
| `/audit` | `pages/AuditChecklist.jsx` | **OK** | Static-data page (no API) — 1599 LOC. Renders 40+ audit tasks with severity/status chips + file:line citations + progress log. Updated in-file. |
| `/data/v1` | `pages/data-surfaces/V1Surface.jsx` | OK | Hits `/api/v1/forecast` + `/api/v1/health` every 4s. BTC only. "Legacy / museum exhibit" framing. |
| `/data/v2` | `pages/data-surfaces/V2Surface.jsx` | OK | Hits `/api/v2/probability` or `/api/v2/probability/15m` every 4s based on timescale. History ring buffer client-side. |
| `/data/v3` | `pages/data-surfaces/V3Surface.jsx` | OK | Hits `/api/v3/snapshot?asset=...`. 9-timescale composite heatmap. References `elm` signal key (SQ-01 scope). |
| `/data/v4` | `pages/data-surfaces/V4Surface.jsx` | OK | Hits `/api/v4/snapshot?asset=BTC&timescales=5m,15m,1h,4h&strategy=fee_aware_15m` every 4s. Richest surface in the stack. |
| `/deployments` | `pages/Deployments.jsx` | OK | Static SERVICES registry (7 services) with live 15s health probes via `/v4/snapshot`, `/margin/status`, `/api/system/status`, and direct `/` for frontend. |
| `/notes` | `pages/Notes.jsx` | **OK** | Full CRUD against `/api/notes` (GET list, POST create, PATCH update, DELETE). Polls every 30s. Filter by status/tag/search. |
| `*` | `Navigate → /dashboard` | OK | 404 fallback. |

**Totals:**
- **OK:** 21 (includes 4 critical-path + 4 data-surface + margin + audit + deployments + notes + trading-config + etc.)
- **PARTIAL (works, but demo-data fallback or legacy taxonomy):** 5 (Dashboard, PaperDashboard, Positions, Risk, Learn)
- **STALE (works but surfaces v7/v8 era data):** 5 (Trades, Signals, PnL, Changelog, V58Monitor, LiveTrading)
- **PLACEHOLDER:** 1 (Indicators)

Actual breakdown: 29 user-visible routes audited.

---

## 3. Per-page findings

### Polymarket critical path

#### `/execution-hq` — Execution HQ
- Tabs: `live` + `retro`. Polls `/v58/execution-hq?limit=200` every 10s on Live.
- Live tab renders (in order from the source):
  1. `GateHeartbeat` — 8-gate V10.6 pipeline strip (G0..G7) + recent rail (20 mini strips) + aggregate stats (trade/skip + blocked-by breakdown). Reads `hqData.gate_heartbeat` which the hub fills from the last 50 rows of `signal_evaluations`. Fully wired.
  2. Current Eval Window panel — live T-countdown, DUNE cap, DUNE P, source agreement badge, V10 min-eval indicator.
  3. 6 Continuous Feeds panel — Chainlink / Tiingo / CLOB / Binance / CoinGlass / DUNE, fed from `hqData.windows[0]` deltas. Shows `--` when the feed is null.
  4. v10 Gate Pipeline / Price chart / Risk surface / AI gatekeeper stack column.
  5. v10 configuration right panel + Execution log (last 6 trades).
- Floating `ManualTradePanel` portal mounts regardless of tab. Always available.
- **NB: the countdown in LiveTab is CLIENT-SIDE SIMULATED** — a setInterval ticks `currentT` every 1s from 240→60 and synthesises candles with a `Math.random()` drift. `CanvasPriceChart` uses these simulated prices if `hqData.candles` is unset. The hub DOES return `hqData.windows[0]` which wires the feeds and DUNE cap, but the actual live price chart on the Live tab is a local animation. Not a blocker for trading decisions (which come from DUNE cap + gate heartbeat), but the "Real-Time Price & Window History" label is misleading.
- Retro tab shows shadow stats + missed opportunities + recent trades table + window history + synthetic retrospective chart (`retroData` is generated from `window.delta_pct * progress * random()` — not real per-checkpoint data). The header text says "When countdown_evaluations are wired up, this will use real per-checkpoint data" — so this is acknowledged-stub.

#### `GateHeartbeat.jsx` (UI-01)
- Canonical 8-gate pipeline order hardcoded: `eval_offset_bounds, source_agreement, delta_magnitude, taker_flow, cg_confirmation, dune_confidence, spread_gate, dynamic_cap`.
- No-data fallback: renders "No signal_evaluations rows yet. Engine must run at least one window evaluation." inside a Panel wrapper. Clean empty state.
- Pinned-row interaction works (click a recent mini-strip → the main strip locks on that entry until UNPIN).
- Data contract matches `hub/api/v58_monitor.py::get_execution_hq` which builds the `gate_heartbeat` array from `SELECT ... FROM signal_evaluations ORDER BY evaluated_at DESC LIMIT 50` and maps raw `gate_failed` into the canonical pipeline order. Gate aliases (`cg_veto`, `cg`, `cg_confirm` etc.) are all normalised server-side. Fully wired.

#### `ManualTradePanel.jsx` (LT-03 + LT-02 wiring)
- Portal-mounted floating panel with a fixed bottom-right trigger button.
- Live price polling every 4s when open, via `/api/v58/live-prices`.
- Direction toggle (UP/DOWN) pre-fills `priceOverride` from live Gamma.
- Order type FAK/FOK/GTC, stake USD, LT-03 `rationale` textarea (optional).
- POST body includes `operator_rationale: trimmedRationale || null` — clean "empty string → null" handling so the DB stores NULL for unset rationales.
- Pending-live trades: `status=pending_live` mode writes the row the Montreal engine polls. After LT-02 (PR #42) the engine has a DB fallback for token_id lookup.
- UI shows result state (ok/error) inline; on success, `rationale` is cleared so the next click isn't contaminated.

#### `hub/api/v58_monitor.py::_capture_trade_snapshot` (LT-03 backend)
- Runs after the `INSERT INTO manual_trades` commit. Wrapped in its own try/except so a snapshot failure never blocks the trade.
- Fetches concurrent `/v4/snapshot` and `/v3/snapshot` via httpx direct to TIMESFM_URL (not through the hub's own route — that's fine, it's the same service).
- Reads last 5 resolved windows from `market_data` for operator context.
- Reads the engine's actual decision for this `window_ts` from `signal_evaluations` (what it would have done + gate_failed reason).
- Extracts `macro.bias` and `macro.confidence` from v4 payload.
- Inserts one row into `manual_trade_snapshots` with JSONB blobs + operator direction/rationale + engine-would-have/reason + vpin + macro_bias. Commits.
- The endpoint to read them back is `GET /v58/manual-trade-snapshots` (exists, line 1839).
- Verdict: fully wired for capture. No side-by-side operator-vs-engine VIEWER page exists yet — phase 4 of LT-03 is still OPEN in the audit checklist.

### Polymarket secondary

#### `/dashboard`
- 11 `/api/dashboard/*` endpoints via `Promise.allSettled`. Each chart has a `genXxxDemo()` generator that runs if the API returns empty. **This means an operator viewing this page with a dead engine will see convincing-looking demo data without any visual cue other than the Balance card showing `—`.** Not a live-trading blocker but a notable UX trap.
- All 11 hub routes exist in `hub/api/dashboard.py`.

#### `/paper` (PaperDashboard)
- Hits `/api/paper/trades,stats,equity`. Same demo-data fallback pattern. Also has a simulated VPIN ticker (`setInterval` drift on top of hub data) which is independent of real feed state.

#### `/positions`
- Hits `/api/trades?status=OPEN&mode=paper`. Strategy filter taxonomy hardcoded to `sub_dollar_arb` / `vpin_cascade` — v7.x strategies. The Polymarket-v10.6 `five_min_vpin` strategy will not match either filter, so the v10 open trades list will render as "all other strategies" in the bottom table.

#### `/trades`
- Hits `/api/trades` (real) and `/api/trades/stats` (real). Filter dropdowns: strategy, outcome, market_slug. OK for pagination. v10-era trades show up.

#### `/signals`
- Hits `/api/signals/vpin,cascade,arb`. v7.x tab taxonomy. No surface for v10.6 gate results (that's on Execution HQ's gate heartbeat).

#### `/pnl`
- Hits `/api/pnl/*`. Header stats show "Arb P&L / VPIN P&L" — v7.x split. Net P&L and equity curve still correct for all strategies combined.

#### `/risk`
- Hits `/api/system/status` + `/api/trades?mode=paper`. Demo-data fallback. Alerts computed client-side from status (daily loss threshold, drawdown, consecutive losses, max position). Functional but demo-fallback is silent.

#### `/system`
- Hits `/api/system/status,kill,resume,paper-mode`. Feed list is hardcoded (5 feeds). Kill switch has a two-click confirm. Works.

#### `/trading-config`
- Full CRUD against `/api/trading-config/*`. Supports GET defaults/list/active/mode/live-status, POST create/clone/activate/approve, PUT edit, DELETE. All backing routes exist in `hub/api/trading_config.py`. This is the page operators use to edit thresholds — fully functional.

#### `/setup`
- Form page for external API keys and Telegram. Hits `/api/setup` GET+PUT and `/api/setup/test-telegram`. Also reads wallet from `/api/setup/derive-poly-keys`. Works.

#### `/changelog`
- Hardcoded `RELEASES` array. Last entry is v7.2 (2026-04-06). No v8/v9/v10/v11 entries. Needs a rev to match the AuditChecklist timeline.

#### `/playwright`
- Hits `/api/playwright/status,balance,positions,redeemable,history,screenshot,redeem`. The playwright bot is a separate service, routes exist. Works.

#### `/timesfm`
- Raw `fetch('/timesfm/v2/probability')` etc — bypasses hub JWT via nginx proxy. Works but inconsistent with the rest of the app. Deletion-candidate once `/data/v2` is stable (which it is).

#### `/indicators`
- `import { generateTWAPDeltaSeries, generateSignals, generateVPIN, generateGammaPrices, generateBTCTick } from '../lib/mock-data.js'`. **100% mock data.** No `useApi`, no `fetch`. Page loads and renders a deterministic demo. Dead placeholder from early dev.

#### `/v58` (V58Monitor)
- 3118 LOC. Functional — hits ~8 `/v58/*` endpoints. Includes its own manual-trade POST, its own windows table, its own live price poll. Largely superseded by `/execution-hq` + `/windows` + `ManualTradePanel`. The inline countdown timer, price chart, and manual trade POST logic are duplicated across this and `/execution-hq`.

#### `/live` (LiveTrading)
- Hits `/api/v58/wallet-status`. Renders engine status + trade summary + today's breakdown + recent trades table. No manual trade panel. No gate heartbeat. The label "💰 Live Trading" in the sidebar is misleading — this is a wallet/summary view, not the trade-execution entry point. Operators going to "Live Trading" expecting to place trades will not find the trade button here — they have to go to `/execution-hq`.

#### `/windows`
- Hits `/v58/outcomes?limit=100` and `/v58/window-detail/{ts}`. Expandable window cards. Works.

#### `/strategy`
- Hits `/v58/strategy-analysis`. Two-column WR explainer (Polymarket oracle WR vs directional WR). Works.

#### `/analysis`
- Raw `fetch('/api/analysis', { headers: { Authorization: Bearer ... } })` + `fetch('/api/analysis/{docId}')`. Document reader for static markdown docs. Works.

#### `/factory` (FactoryFloor)
- Hits `/v58/windows,outcomes,accuracy,stats,live-prices,system/status`. Pipeline visualisation with animated flow dots. Works.

### Data surfaces (FE-04..07)

#### `/data/v1`
- `/api/v1/forecast` + `/api/v1/health` every 4s. BTC only (asset selector is disabled for non-BTC). "Legacy / superseded by v2" callout. The hub proxies to `TIMESFM_URL/forecast` — confirmed in `hub/api/margin.py`. Has a 404/502/503 handler that flips to an "endpoint not available" card. Clean.

#### `/data/v2`
- Two timescale configs (`5m → /v2/probability`, `15m → /v2/probability/15m`). Both backed by `hub/api/margin.py::v2_probability(_15m)`. Client-side 20-slot history ring. Calibrated-vs-raw probability split, quantile fan, model_version chip. No-error-path display for now.

#### `/data/v3`
- `/api/v3/snapshot?asset=BTC` every 4s. 9-timescale composite heatmap. 7-signal radar with `elm` as one of the keys — inherits SQ-01 legacy naming (tracked in `AuditChecklist.jsx`).

#### `/data/v4`
- Richest surface. `/api/v4/snapshot` with timescales=5m,15m,1h,4h and strategy=fee_aware_15m. Consensus strip with per-source chips, macro card, events timeline, per-timescale grid with quantile fan. Auto-refreshes every 4s. Fully wired.

### Margin engine

#### `/margin`
- Hits `/margin/status`, `/v3/snapshot?asset=BTC`, `/v4/snapshot?asset=BTC&timescales=5m,15m,1h,4h&strategy=fee_aware_15m`. Polls every 5s on Live tab.
- Renders: status bar (engine online, signal feed, v4 fusion, price feed, kill switch), stats row (balance, exposure, leverage, open, total P&L, WR, daily P&L, trades, fee RT, spread), V4Panel, SignalPanel, PositionsPanel. History and Trade Timeline tabs.
- Venue-aware: Hyperliquid vs Binance labelling, leverage chip only shown for margin venues.
- **Has a V4 panel already** (V4Panel.jsx wired in the components folder). This is the reference surface that `FE-03` says the Polymarket engine needs a mirror of.

### Notes

#### `/notes` (NT-01)
- Full CRUD against `/api/notes`: GET list with `status+tag+limit+offset` params, POST create with `{title, body, tags, status, author}`, PATCH `/notes/{id}` with patch object, DELETE `/notes/{id}`. All four verbs exist server-side in `hub/api/notes.py`. Optimistic updates for PATCH and DELETE with rollback on error. Polls every 30s silently. Filter strip (open/all/archived + tag + search). Cmd+Enter submits. Works end-to-end.

### Audit

#### `/audit` (FE-02)
- Static data (no backend). 1599 LOC, ~40 tasks across 7 categories. Each task: id, category, severity, status (OPEN/IN_PROGRESS/DONE/BLOCKED/INFO), title, files[{path,line,repo}], evidence[], fix, progressNotes[{date,note}].
- Filters: severity, status, category. Progress bar.
- Currently tracks: DQ-01..07, PE-01..06, INC-01, DS-01..03, V4-01..03, CA-01..04, SQ-01, CI-01..02, DEP-01..02, LT-01..04, FE-01..07, NT-01, STOP-01, UI-01, UI-02.
- Status chips are colour-coded correctly and render expandable/collapsible cards with file:line refs.

### Deployments

#### `/deployments` (DEP-01)
- Static `SERVICES` registry (7 services: timesfm, macro-observer, data-collector, margin-engine, hub, frontend, engine). Each card has a workflow status chip (active/drafted/legacy).
- Live probes every 15s: timesfm via `/v4/snapshot?asset=BTC&timescales=5m`, margin-engine via `/margin/status`, frontend via direct `fetch('/')`, hub via `/api/system/status`. Services without a reachable health endpoint (engine, macro-observer, data-collector) show "static-only". Hub endpoint routes all exist.

---

## 4. Legacy tabs flagged for retirement

The operator wants a "proper live view — no stale legacy pages". The following
are either fully superseded or ship demo data in a way that misleads on
live-trading:

1. **`/live` (LiveTrading.jsx)** — labelled "💰 Live Trading" in the sidebar
   which an operator will read as "click here to place a live trade". It is
   actually a wallet/summary view that predates the manual trade panel and the
   8-gate heartbeat. Execution HQ now owns trading. Retire the route and redirect
   `/live → /execution-hq`, or rename the sidebar entry to "Wallet / PnL" if the
   wallet columns are still useful.

2. **`/indicators` (Indicators.jsx)** — 100% mock data from `lib/mock-data.js`.
   Pure demo. Either wire it to the real `/api/v3/snapshot` like
   `/composite`, or retire the route entirely and point the sidebar entry at
   `/data/v3`.

3. **`/v58` (V58Monitor.jsx)** — 3118 LOC duplicate of functionality now owned
   by `/execution-hq` + `/windows` + `ManualTradePanel`. Keep for one more
   release as an escape hatch, then retire. The sidebar label "Trade Monitor"
   could be repointed at `/execution-hq` with no loss.

4. **`/changelog` (Changelog.jsx)** — last entry is v7.2 (2026-04-06). Nothing
   from v8/v9/v10/v11 or the DQ-01/DS-01/PE-0x/LT-02/LT-03 work. Either retire
   (point to `/audit` + `git log`) or rev the hardcoded data in the same PR as
   the next release. Low priority.

5. **`/dashboard` demo-data fallbacks** — not a page retirement, but: the 11
   `genXxxDemo()` functions in `Dashboard.jsx` should either be removed OR the
   fallback should be visually flagged with a "DEMO DATA — hub returned empty"
   banner. Same for PaperDashboard, Positions, Risk. Silent fallback is
   dangerous because a dead hub renders the same as a healthy-but-idle one.

6. **`/composite` and `/data/v3`** — both pages use `elm` as a composite signal
   key, inheriting the SQ-01 legacy brand that the engine is migrating away from.
   Not urgent; tracked in SQ-01 PR 3.

---

## 5. Operator critical-path checklist

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | Gate heartbeat visible + refreshing every 10s | **OK** | `GateHeartbeat.jsx` at top of `LiveTab.jsx`; `ExecutionHQ.jsx` polls `/v58/execution-hq` every 10s on Live tab; hub returns `gate_heartbeat` from last 50 `signal_evaluations` rows with canonical 8-gate mapping (`hub/api/v58_monitor.py` lines 2882–2988). Empty-state fallback: "No signal_evaluations rows yet." |
| 2 | Manual trade panel callable, writes `pending_live` | **OK** | `ManualTradePanel.jsx::handleExecute` → POST `/v58/manual-trade` → `hub/api/v58_monitor.py::post_manual_trade` writes `manual_trades` row with `status='pending_live'` when `mode='live'`. LT-02 DB fallback (PR #42) for token_id lookup landed on the engine side. |
| 3 | Decision snapshot captured on each click | **OK** | `hub/api/v58_monitor.py::post_manual_trade` calls `_capture_trade_snapshot` after the manual_trades commit. Snapshot writes `manual_trade_snapshots` row with v4_snapshot, v3_snapshot, last_5_window_outcomes, operator_rationale (LT-03), operator_direction, engine_would_have_done, engine_gate_reason, vpin, macro_bias. Wrapped in try/except so capture failure can't break trade. |
| 4 | Multi-market monitors (BTC/ETH/SOL/XRP × 5m/15m) | **BROKEN (not started)** | UI-02 in `AuditChecklist.jsx` is OPEN, explicitly deferred: "Phase 0 (now): bear this in mind while working on UI-01 (gate heartbeat upgrade)". Today `/execution-hq` hardcodes BTC 5m; the `/v58/execution-hq` endpoint accepts an optional `asset` param but the frontend never sends it. No asset/timeframe switcher in the UI. `UI-02` is explicitly gated behind LT-02 (done) — UI scaffolding is now unblocked but not started. |

Live-trading readiness: **3 of 4 critical-path items PASS**. The 4th is a
forward-scaling feature that the user has explicitly said to "bear in mind"
rather than build now — not a blocker for resuming BTC-5m live trading.

---

## 6. Proposed new audit tasks

Add the following `FE-` tasks to `AuditChecklist.jsx` TASKS[]. All follow the
existing schema shape `{id, category, severity, status, title, files, evidence,
fix}`.

```js
{
  id: 'FE-08',
  category: 'frontend',
  severity: 'MEDIUM',
  status: 'OPEN',
  title: '/live route is a stale wallet view — retire or redirect to /execution-hq',
  files: [
    { path: 'frontend/src/pages/LiveTrading.jsx', line: 1, repo: 'novakash' },
    { path: 'frontend/src/App.jsx', line: 78, repo: 'novakash' },
    { path: 'frontend/src/components/Layout.jsx', line: 24, repo: 'novakash' },
  ],
  evidence: [
    'LiveTrading.jsx hits /v58/wallet-status and shows wallet+trade summary — no manual trade panel, no gate heartbeat, no DUNE cap.',
    'Execution HQ (/execution-hq) is now the canonical live trading surface (gate heartbeat + ManualTradePanel + decision snapshot).',
    'Sidebar label "Live Trading" misleads operators into thinking this is where trades are placed.',
  ],
  fix: 'Either (a) redirect /live → /execution-hq and remove the sidebar entry, OR (b) rename the sidebar label to "Wallet / PnL" and keep the page as a summary view. Option (a) is cleaner. No data loss either way — /execution-hq also shows recent trades.',
},
{
  id: 'FE-09',
  category: 'frontend',
  severity: 'MEDIUM',
  status: 'OPEN',
  title: '/indicators uses 100% mock data from src/lib/mock-data.js',
  files: [
    { path: 'frontend/src/pages/Indicators.jsx', line: 1, repo: 'novakash' },
    { path: 'frontend/src/lib/mock-data.js', line: 1, repo: 'novakash' },
  ],
  evidence: [
    'Page imports generateTWAPDeltaSeries, generateSignals, generateVPIN, generateGammaPrices, generateBTCTick from lib/mock-data.js.',
    'No useApi, no fetch, no real data path. It is a pure demo that predates the v3 composite snapshot.',
    '/composite and /data/v3 both render the real 7-signal breakdown via /api/v3/snapshot.',
  ],
  fix: 'Either wire Indicators.jsx to /api/v3/snapshot (duplicate of /composite) or retire the route and delete the file. Prefer retirement — /composite already owns the real surface.',
},
{
  id: 'FE-10',
  category: 'frontend',
  severity: 'HIGH',
  status: 'OPEN',
  title: 'Dashboard/Paper/Positions/Risk silently fall back to genXxxDemo() on empty hub data',
  files: [
    { path: 'frontend/src/pages/Dashboard.jsx', line: 1668, repo: 'novakash' },
    { path: 'frontend/src/pages/PaperDashboard.jsx', line: 780, repo: 'novakash' },
    { path: 'frontend/src/pages/Positions.jsx', line: 334, repo: 'novakash' },
    { path: 'frontend/src/pages/Risk.jsx', line: 511, repo: 'novakash' },
  ],
  evidence: [
    'Dashboard.jsx line 1668: `setVpinData(rawVpin.length ? rawVpin : genVpinDemo());` — 11 similar fallbacks across the page.',
    'When the hub returns empty arrays (dead engine, DB migration mid-flight), the pages render convincing synthetic data with no banner distinguishing real from demo.',
    'An operator landing on /dashboard with a dead hub sees charts move and may misread it as healthy.',
  ],
  fix: 'Either (a) remove the demo fallbacks and render a "No data yet" placeholder when the hub returns empty, OR (b) keep the demo data but render a persistent "DEMO DATA — hub returned empty" banner across the top of the page whenever any endpoint falls back. Option (a) is safer for live trading.',
},
{
  id: 'FE-11',
  category: 'frontend',
  severity: 'MEDIUM',
  status: 'OPEN',
  title: 'Execution HQ "Real-Time Price" chart is locally simulated, not wired to real candles',
  files: [
    { path: 'frontend/src/pages/execution-hq/components/LiveTab.jsx', line: 44, repo: 'novakash' },
    { path: 'frontend/src/pages/execution-hq/components/CanvasPriceChart.jsx', line: 1, repo: 'novakash' },
  ],
  evidence: [
    'LiveTab.jsx lines 44–72: setInterval ticks currentT from 240→60 and synthesises prices with `const movement = (Math.random() - 0.5) * 0.008;`',
    'hqData.candles IS read at line 38 when present — so the chart prefers real data when available, but the hub endpoint does not currently return a candles field (checked hub/api/v58_monitor.py::get_execution_hq).',
    'Label on the Panel reads "Real-Time Price & Window History" which an operator will read as "this is the live Binance/Polymarket price".',
  ],
  fix: 'Either (a) add a `candles` field to /v58/execution-hq that reads the last 15 rows from `ticks_binance` or `ticks_clob` and returns OHLC candles aligned to the 5m window, OR (b) remove the price chart from LiveTab entirely and let /v58 Monitor own that surface. Option (a) is worth it because the countdown+price visual is central to the live experience.',
},
{
  id: 'FE-12',
  category: 'frontend',
  severity: 'LOW',
  status: 'OPEN',
  title: 'Changelog.jsx last entry is v7.2 — 9 versions behind',
  files: [
    { path: 'frontend/src/pages/Changelog.jsx', line: 28, repo: 'novakash' },
  ],
  evidence: [
    'RELEASES array ends at v7.2 (2026-04-06). No v8/v9/v10.x/v11.x entries.',
    '/audit page already tracks in-flight tasks so the changelog duplication is low-value.',
  ],
  fix: 'Either (a) retire /changelog and redirect to /audit + a CHANGELOG.md link, OR (b) rev the hardcoded RELEASES array in the same PR as the next release tag. Option (a) is lower maintenance.',
},
{
  id: 'FE-13',
  category: 'frontend',
  severity: 'LOW',
  status: 'OPEN',
  title: 'Legacy "elm" signal key surfaces on /composite and /data/v3',
  files: [
    { path: 'frontend/src/pages/CompositeSignals.jsx', line: 19, repo: 'novakash' },
    { path: 'frontend/src/pages/data-surfaces/V3Surface.jsx', line: 46, repo: 'novakash' },
    { path: 'frontend/src/pages/margin-engine/components/constants.js', line: 23, repo: 'novakash' },
  ],
  evidence: [
    'CompositeSignals.jsx SIGNAL_COLORS has `elm: "#a855f7"` — the ELM family has been superseded by Sequoia v5.2 but the wire-format key is still "elm".',
    'V3Surface.jsx SIGNAL_KEYS array leads with "elm".',
    'SQ-01 in AuditChecklist tracks the full rename plan — PR 3 is the cross-repo dual-emit that removes "elm" from the v3 snapshot payload.',
  ],
  fix: 'No independent fix — this resolves when SQ-01 PR 3 ships. Track it here so a frontend reader knows the stale naming is known and scheduled.',
},
```

---

## 7. Recommendations (priority-ordered)

1. **Before resuming live trading — verify the gate heartbeat end-to-end on the
   real host.** Source code is clean (above), but verification on the real data
   path means: load `/execution-hq`, confirm the 8 chips light up, confirm the
   10s poll interval is ticking, confirm at least one pipeline step is
   returning `false` on a visibly SKIPPED window (not all "all passed"). The
   empty-state "No signal_evaluations rows yet" must not be showing once the
   engine has run at least one evaluation after the current restart.

2. **Retire `/live` OR rename the sidebar entry.** The label "Live Trading"
   pointing at a wallet-summary view is the most dangerous mis-label in the
   current sidebar — an operator looking to place a trade will land on a page
   with no trade button. Pick one: redirect `/live → /execution-hq` and drop
   the sidebar item, or rename the sidebar item to "Wallet / PnL".

3. **Fix the silent demo-data fallbacks on `/dashboard`, `/paper`,
   `/positions`, `/risk`.** These four pages render convincing synthetic data
   when the hub returns empty, with no banner. At minimum, add a persistent
   "DEMO DATA — hub returned empty" banner so an operator can tell a healthy
   idle state from a dead backend. FE-10 captures this.

4. **Wire real candles into the Execution HQ price chart OR remove that
   panel's "Real-Time" label.** The locally-simulated price chart in LiveTab
   (lines 44–72) is the most likely single thing to confuse an operator during
   a live session — it animates at 1Hz regardless of real price activity.
   FE-11 captures this.

5. **Start UI-02 scaffolding even before multi-market trading lands.** UI-02
   is currently waiting on "LT-02 (live trade execution) works end-to-end" —
   LT-02 is now DONE. The scaffolding (per-asset/per-timeframe route,
   `asset + timeframe` query params on `/v58/execution-hq`, sidebar structure
   for 8 market pairs) can ship independently of engine-side multi-market
   support, wired to BTC-5m only as a dry run. This is the single biggest
   outstanding critical-path item and the user explicitly asked for it to
   "just bear that in mind whilst you get all this working".

---

## Appendix A — Hub API coverage check

Every `/api/*` path the frontend calls, mapped to its backing FastAPI route:

| Frontend fetch | Hub file | Backing route |
|---|---|---|
| `/api/dashboard/*` (11 endpoints) | `hub/api/dashboard.py` | matches |
| `/api/paper/trades,stats,equity,strategy-breakdown,log,positions,status` | `hub/api/paper.py` | matches |
| `/api/trades`, `/api/trades/stats`, `/api/trades/{id}` | `hub/api/trades.py` | matches |
| `/api/signals/vpin,cascade,arb,regime` | `hub/api/signals.py` | matches |
| `/api/pnl/cumulative,daily,monthly,by-strategy` | `hub/api/pnl.py` | matches |
| `/api/system/status,kill,resume,paper-mode` | `hub/api/system.py` | matches |
| `/api/config` (GET/PUT) | `hub/api/config.py` | matches (orphaned Config.jsx — file is not routed) |
| `/api/trading-config/*` (9 verbs) | `hub/api/trading_config.py` | matches |
| `/api/setup`, `/api/setup/test-telegram`, `/api/setup/derive-poly-keys` | `hub/api/setup.py` | matches |
| `/api/forecast/*` (latest, history, accuracy, twap-history, window-detail) | `hub/api/forecast.py` | matches (currently referenced in components not audited) |
| `/api/playwright/*` | `hub/api/playwright.py` | matches |
| `/api/v58/windows,countdown,stats,price-history,outcomes,accuracy,execution-hq,live-prices,manual-trade,manual-trades,manual-trade-snapshots,window-detail,gate-analysis,strategy-analysis,wallet-status,wallet/live` | `hub/api/v58_monitor.py` | matches |
| `/api/analysis`, `/api/analysis/{id}` | `hub/api/analysis.py` | matches |
| `/api/margin/status,logs,positions/history` | `hub/api/margin.py` | matches |
| `/api/v1/forecast,health` | `hub/api/margin.py` (proxy to TIMESFM) | matches |
| `/api/v2/probability,probability/15m,health,models` | `hub/api/margin.py` (proxy) | matches |
| `/api/v3/snapshot,health` | `hub/api/margin.py` (proxy) | matches |
| `/api/v4/snapshot,macro,recommendation` | `hub/api/margin.py` (proxy) | matches |
| `/api/notes` (list/get/create/update/delete) | `hub/api/notes.py` | matches |

All frontend hub calls resolve to a matching FastAPI route. No 404 risks
introduced by missing backend endpoints.

**One exception:** `/timesfm/*` (raw fetch in `TimesFM.jsx`) is an nginx proxy
directly to the TimesFM service and does not go through the hub. Works, but
bypasses JWT.

---

## Appendix B — Audit methodology

- Enumerated routes from `frontend/src/App.jsx` (single source of truth for React
  Router paths) and sidebar nav from `frontend/src/components/Layout.jsx`.
- Opened every page source file under `frontend/src/pages/`, focusing on:
  (a) imports of `useApi` / `fetch` / `mock-data`, (b) presence of
  `genXxxDemo()` fallbacks, (c) empty-state and error-state rendering, (d)
  hardcoded constants that could indicate a placeholder or legacy scar.
- Cross-referenced every `api('GET', '/xxx')` and `fetch('/api/xxx')` against
  `hub/api/*.py` to confirm the hub serves what the frontend expects.
- Spot-checked `hub/api/v58_monitor.py::get_execution_hq` and
  `::post_manual_trade` + `_capture_trade_snapshot` to verify the LT-03 snapshot
  and UI-01 gate heartbeat data contracts.
- Grepped the full frontend tree for `elm|ELM|80000|Sequoia v4|TODO|hardcoded` —
  flagged SQ-01 legacy references and confirmed no `$80,000` scars outside
  `AuditChecklist.jsx` (where it is correctly referenced as DQ-06 context).

No code was edited. Only this doc was written.
