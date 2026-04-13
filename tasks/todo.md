# tasks/todo.md — BTC Trader Hub

## Codebase Audit — 2026-04-06

### Plan
- [x] Full frontend page-by-page audit (22 pages, 18 components)
- [x] Win rate calculation audit (3 sources identified, inconsistencies documented)
- [x] Market data audit (DB-first, no local directory, backfill.py populates)
- [x] Production issues status review (2 fixed, 1 partial, 2 open)
- [x] Verify backend endpoints for all frontend API calls
- [x] V58Monitor.jsx deep audit — see `docs/V58_MONITOR_AUDIT.md`
- [ ] Implement fixes from audit findings

### Findings Summary

**Production Issues:**
| Issue | Status |
|-------|--------|
| Retry Order ID Mismatch | ✅ FIXED (retry removed) |
| Redemption Timing | ⚠️ PARTIALLY FIXED |
| TimesFM v2 Gate | ❌ STILL OPEN |
| VPIN Warm Start | ✅ FIXED |
| V1 TimesFM Disagreement Gate | ❌ STILL OPEN |
| Paper Mode Resolution Fallback | ❌ NEW — corrupts win rates |

**Frontend Issues Found:**
- 2 dead pages (PaperTrading.jsx 41KB, Learn.jsx 93KB)
- Missing .catch() on API calls in Signals, PnL, System, Indicators
- useEffect dependency array issues in 3 pages
- Duplicate routes (/config + /trading-config)
- Stale known issues in Changelog.jsx
- V58Monitor.jsx needs decomposition (122KB single file)

**Win Rate Inconsistencies:**
- 3 independent sources: trades.outcome, window_snapshots.v71_correct, backtest JSONs
- Paper fallback to Binance price corrupts data
- v71_correct write path undocumented
- Backtest JSON files at repo root are stale

**Verified Working (agent reports were wrong about):**
- All /v58/* endpoints exist (hub/api/v58_monitor.py)
- All /playwright/* endpoints exist (hub/api/playwright.py)
- All /trading-config/* endpoints exist (hub/api/trading_config.py)
- useApi() callable syntax api('GET', url) works correctly
- /api/ prefix stripping in useApi interceptor works correctly

### Review
- Audit documented in TODO.md (root) and tasks/todo.md
- V58Monitor.jsx deep audit pending
- Full details in TODO.md

---

## Active Tasks

- [x] V58Monitor.jsx audit — complete, see `docs/V58_MONITOR_AUDIT.md`
- [x] TimesFM v5.2 chainlink_price fix — see `docs/analysis/TIMESFM_V5_FIX_2026-04-13.md`
- [ ] Implement V1 TimesFM disagreement gate (highest ROI)
- [ ] Remove paper mode Binance resolution fallback
- [ ] Add error handling to frontend API calls
- [ ] Clean up dead pages (PaperTrading.jsx, Learn.jsx)

## TimesFM v5.2 Fix — 2026-04-13

### What Was Broken

Last 12h: 13,096 signal evaluations, ALL predicting UP at constant 10.2% conviction, 0 trades executed.

**Root cause:** TimesFM v5.2 LightGBM model requires 25 features. When `chainlink_price` was missing (NaN), model followed the "missing value" path and returned the default leaf value: 0.60614485 (10.2% conviction, always UP).

**Market reality:** BTC went DOWN 82.4% of the time during this period — the model was completely wrong.

### What Was Fixed

✅ Fixed 3 call sites adding `chainlink_price=window_snapshot.get("chainlink_open")`:
- `engine/strategies/five_min_vpin.py:1759`
- `engine/use_cases/evaluate_window.py:238`
- `engine/use_cases/evaluate_window.py:851`

✅ Note: `engine/signals/gates.py:952` already had `chainlink_price` (no change needed)

✅ Created documentation:
- `docs/analysis/TIMESFM_V5_MODEL_BROKEN_2026-04-13.md` — Root cause analysis
- `docs/analysis/TIMESFM_V5_FIX_2026-04-13.md` — Detailed fix documentation  
- `docs/analysis/TIMESFM_V5_FIX_APPLIED_2026-04-13.md` — Fix verification checklist

✅ Committed to branch `fix/timesfm-v5-chainlink-feature`

### What's Next

- [ ] PR to `develop` (already created — awaiting review)
- [ ] Deploy to Montreal (restart engine)
- [ ] Verify logs show `v2.probability.push_mode_active feature_coverage>=0.80`
- [ ] Monitor signals — P(UP) should vary 0.3-0.9 instead of constant 0.606
- [ ] Monitor trades — should resume at ~40/day for DOWN-only
- [ ] Track win rate — should return to 75-80%

### Key Learnings

1. **12 evaluations per window:** Each 5-minute window is evaluated 12 times (T-140 to T-90, every 5s), but only 1 trade executes per window after all filters pass
2. **Filter cascade:** 18,701 evaluations → 36 unique windows → 40 actual trades (0.2% filter rate)
3. **Strict filters saved us:** The 12% conviction threshold correctly blocked all trades, preventing massive losses
4. **Use correct data source:**
   - `signal_evaluations` — Signal quality analysis (does model predict well?)
   - `unique_windows` — Strategy design (how many windows have valid signal?)
   - `trade_bible` — Performance tracking (real PnL)

## Dashboard + Margin Engine Session — 2026-04-10

### What shipped
- [x] Layout.jsx — grouped nav into 4 colored sections (Polymarket/Binance Margin/Analysis/System)
- [x] MarginEngine.jsx — StatusDot activity indicators, PAPER badge, LEVERAGE card showing current/max
- [x] CompositeSignals.jsx — friendly "Connecting to signal service..." banner replacing raw 502 text, lastGoodSnapshot stale indicator
- [x] SignalPanel.jsx — header CONNECTING pulse + friendly waiting state
- [x] Diagnosed + fixed the /api/v3/snapshot 502: Hub on Railway was missing `TIMESFM_URL` + `MARGIN_ENGINE_URL`, fell through to localhost defaults
- [x] Set Railway env vars on `Novakash → develop → hub`: `TIMESFM_URL=http://3.98.114.0:8080`, `MARGIN_ENGINE_URL=http://18.169.244.162:8090`
- [x] Deleted redundant `.github/workflows/railway-deploy.yml` (Railway native GitHub integration handles deploys; workflow had been failing on every PR with old CLI syntax)
- [x] Updated `.env.example` with prod-deploy warning comment on the service URL section
- [x] Margin engine live-mode P&L fix (`FillResult` + side-aware mark + real commission from Binance `fills[]` + paper mirrors live shape) — deployed to eu-west-2 EC2 last session, NOW committing to branch to match prod
- [x] Passive signal recorder (`pg_signal_repository.py` + `margin_signals` table) — also already deployed, now committing

### Follow-ups (open)
- [ ] **Sequoia v4 promotion** — `timesfm` repo: promote `feat/v2.1-calibration` → `main`. Montreal EC2 currently runs commit `11191d7` from that branch (SEQUOIA v4 model), but main does not. A redeploy from main would silently downgrade the model.
- [ ] **ELM → Sequoia rename** — dual-emit migration across both repos:
  - Backend (timesfm): `app/v3_composite_scorer.py` emits both `"elm"` and `"sequoia"` keys in `raw_signals`
  - Frontend (novakash): `SIGNAL_COLORS` maps + display reads `signals.sequoia ?? signals.elm`
  - DB: `margin_signals.elm` column gets a `sequoia` sibling, dual-write, then rename (or drop `elm`) after stable period
  - Timeline: deploy backend first, verify `/v3/snapshot` has both keys, then deploy frontend, then clean up after ~1 stable day
- [ ] **P&L-protected reversal exit** (Option A #6 from lessons.md) — use `exchange.get_unrealised_pnl(position)` to skip SIGNAL_REVERSAL exit when position is comfortably in profit. Noted as TODO comment in `manage_positions.py`.
- [ ] **Passive signal autocorrelation analysis** — once `margin_signals` has ≥24h of data, run forward-return analysis to decide whether Option A tuning knobs (lessons.md) should be applied.
- [ ] **v58 TimesFM gate** + remove paper Binance resolution fallback (carry-over from 2026-04-06 audit)

### Review

Two hidden bugs were compounding into one symptom:

1. **502 on frontend** was caused by missing Railway env vars, NOT by TimesFM being down or by the security group. I initially misdiagnosed it twice — first as SG, then as "v3 routes don't exist" (after probing `/v3/probability`, which never existed). The actual Hub proxy calls `/v3/snapshot`, which returns 200 with full signal data when probed directly. Lesson: always grep the proxy code first before probing upstream.

2. **Redundant workflow** was creating UNSTABLE PR checks, training us to ignore failures. Deleted and documented. Lesson: kill failing-but-redundant workflows on sight.

3. **Margin engine P&L fix** was the right fix for the overnight 116-trade fee-cost trap. The FillResult pattern puts the exchange adapter in charge of money, and the position entity becomes a record not a calculator. Paper mode mirrors live shape exactly so they can never drift again.

## Margin Strategy Dashboard — 2026-04-12

### What shipped
- [x] `/frontend/src/pages/MarginStrategies.jsx` — Complete strategy dashboard page (1000+ lines)
- [x] `/frontend/src/App.jsx` — Added route `/margin-strategies`
- [x] `/frontend/src/components/Layout.jsx` — Added "Strategies" link to MARGIN ENGINE nav + mobile tab bar
- [x] `/docs/MARGIN_STRATEGY_DASHBOARD_IMPLEMENTATION.md` — Implementation report with backend endpoint specs

### Dashboard Sections Implemented
1. **Strategy Performance Cards** — 5 strategies (V4 PATH live, 4 inactive with placeholders)
2. **Real-Time V4 Data Panel** — Reuses V4Panel component, 4s refresh
3. **Position Analysis** — Fee-adjusted PnL distribution + histogram
4. **Signal Strength Distribution** — Alignment score histogram (6 bins)
5. **Hold Extension Analysis** — Actual vs expected hold times + distribution
6. **Partial Close Audit** — Table showing when/why partials happened
7. **Regime Performance** — PnL breakdown by market regime (TRENDING_UP/DOWN, MEAN_REVERTING, CHOPPY)

### Backend Endpoints Needed
- `GET /api/margin/strategy-stats` — Historical performance metrics per strategy
- `GET /api/margin/positions` — Position history with metadata (alignment_score, regime, partial_close info)
- `GET /api/margin/strategy-config` (optional) — Strategy configuration for Configure buttons

### Design Decisions
- Graceful degradation: placeholder data when APIs unavailable
- Reuses existing V4Panel component for consistency
- 4-second refresh interval (matches V4Surface)
- Follows existing color scheme and component patterns
- Mobile-responsive with bottom tab bar integration

### Follow-ups
- [ ] Implement `GET /api/margin/strategy-stats` endpoint
- [ ] Implement `GET /api/margin/positions` endpoint with real DB query
- [ ] Add strategy tracking to margin engine (track which strategy triggered each trade)
- [ ] Make "Configure" buttons functional (open modals with strategy details)
- [ ] Add backtest simulation for inactive strategies

## Completed

- [x] Phase 1: Foundation (Docker, DB schema, Auth, project skeleton)
- [x] Codebase audit — 2026-04-06
- [x] Dashboard + Margin Engine — 2026-04-10 (see section above)
- [x] Margin Strategy Dashboard — 2026-04-12 (see section above)
