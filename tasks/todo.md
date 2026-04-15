# tasks/todo.md — BTC Trader Hub

## Gate Audit + Window Decision Trace Plan — 2026-04-15

### Plan
- [x] Add a first-class per-evaluation trace schema for every window tick, strategy, and gate result
- [x] Record raw signal surface at eval time: delta_pct, model probability/dist, VPIN, regime, source deltas, CLOB prices, spread, buy_ratio
- [x] Record per-strategy decision payload: mode, action, direction, confidence, entry cap, skip_reason, eval_offset, timeframe
- [x] Record per-gate results in structured form, not only the final failed gate: gate_name, passed, threshold/config, observed_value, explanation
- [x] Link each evaluation trace to executed trade rows and final resolved outcome for the same window
- [x] Add query/API shape for per-window review so Telegram/UI can show: eligible now, blocked by signal, off-window, traded, resolved outcome

### Clean Architecture Implementation Slices
- [x] Domain: add immutable value objects for `WindowEvaluationTrace`, `StrategyEvaluationTrace`, `GateCheckTrace`, and `WindowOutcomeTrace`
- [x] Application: add `RecordWindowTraceUseCase` that accepts one evaluation surface and all strategy/gate outcomes and persists them through ports
- [x] Application: add `GetWindowTraceUseCase` that returns a window-centric view for Telegram/UI/reporting
- [x] Ports: extend persistence contracts with a dedicated trace repository instead of adding another ad hoc JSON field
- [x] Infrastructure: add Postgres tables/repositories for window traces, strategy traces, gate checks, and outcome linkage
- [x] Presentation: replace current per-strategy skip dump with a grouped window narrative derived from the trace query

### Proposed Trace Model
- [x] `window_evaluation_traces`: one row per `(asset, window_ts, timeframe, eval_offset)` containing the shared signal surface
- [x] `strategy_evaluation_traces`: one row per strategy per eval tick containing mode, action, direction, confidence, entry cap, skip reason, and trade-advised flag
- [x] `gate_check_traces`: one row per gate check per strategy eval containing gate order, gate name, passed flag, configured threshold/config, observed values, and human explanation
- [x] `window_outcome_traces`: one row per resolved window linking oracle outcome, executed order, PnL, and post-resolution judgment

### Notification / UI Target Shape
- [x] At T-62 (or equivalent final eval), show grouped output instead of raw skip spam:
- [x] `Eligible now`: strategies whose timing window is active and which reached a real signal decision
- [x] `Blocked by signal`: confidence / spread / delta / taker-flow / cap blockers with actual observed values
- [x] `Blocked by execution timing`: e.g. `live entry requires >= T-70, current T-62`
- [x] `Inactive this offset`: strategies simply not in their configured time window
- [x] After resolution, show `What happened`: executed strategy, ghost strategies that also would have traded, gate trace summary, and final outcome/PnL

### Why This Replaces Current Confusion
- [x] Current `strategy_decisions` is decision-centric and stores a lot of context, but only one final skip reason per strategy eval
- [x] `gate_audit` was too coarse and not strategy-scoped — retired 2026-04-15 (PR #192), superseded by `gate_check_traces`
- [x] The new model is window-centric and can answer: what data we had, what each gate saw, what failed, what passed, what traded, and whether that was correct later

### Delivery Order
- [x] Phase 1: schema + value objects + repository + recorder use case
- [x] Phase 2: wire trace recording into strategy evaluation and v10 gate pipeline
- [x] Phase 3: add query use case and Telegram formatter based on grouped traces
- [x] Phase 4: add resolution linkage + post-window review card / API endpoint

## Redeemer Controls + Reconciliation Fix Plan — 2026-04-15

### Goal
- [ ] Fix live reconciliation so resolved positions map back to trades reliably
- [ ] Make redeemer quota-aware for 100 relayer tx/day
- [ ] Expose manual redeem controls and wallet stats through Hub API and frontend top bar

### Clean Architecture Slices
- [ ] Domain: keep redemption policy decisions as pure rules (wins frequent, losses deferred, quota budget)
- [ ] Application: add explicit redeem request / status use cases instead of wiring UI directly to persistence flags
- [ ] Infrastructure: implement PG trade lookup by token_id and redemption control storage
- [ ] Presentation: Hub API endpoints + frontend top bar controls only call use cases / repos, no embedded trading logic

### Backend Implementation
- [ ] Add `PgTradeRepository.find_by_token_id` to fix reconciler live resolution bug
- [ ] Extend redeem control storage with request type (`wins`, `losses`, `all`) and quota/status metadata
- [ ] Add daily quota accounting from redeem events / attempts
- [ ] Change auto policy: redeem wins every 15m, max 2 per sweep, losses daily/manual only
- [ ] Add structured redeemer status payload: cash, portfolio, open positions, redeemable wins, redeemable losses, cooldown, quota used today

### Hub API
- [ ] `GET /api/system/redeemer-status`
- [ ] `POST /api/system/redeem/wins`
- [ ] `POST /api/system/redeem/losses`
- [ ] `POST /api/system/redeem/all`

### Frontend Top Bar
- [ ] Add compact wallet card in app top bar with cash / portfolio / open positions / redeemable wins / redeemable losses / cooldown
- [ ] Add buttons: `Redeem Wins`, `Redeem Losses`, `Redeem All`
- [ ] Surface cooldown/quota warnings clearly so operators understand when redemptions are deferred

### Review Notes
- Current Telegram summaries over-emphasize obvious timing misses because only final skip reasons are surfaced.
- The better model is window-centric: one row per eval tick, many per-strategy decision rows, many per-gate check rows, then one final outcome row.
- This should let us answer clearly: what data we had, what every gate saw, why we traded/skipped, and whether that was right after resolution.

## 15m Clean-Arch Implementation — 2026-04-14

### Plan
- [x] Step 0: Add WindowInfo.timeframe property (polymarket_5min.py)
- [x] Step 1-2: Update data_surface fetch + get_surface to be timeframe-aware
- [x] Step 3: Add strategy registry timescale filter (safety gate)
- [x] Step 4-5: Wire orchestrator market_slug + 15m CLOSING registry eval
- [x] Step 6-7: Add 15m YAML configs + hook files (v15m_*)
- [x] Verification: sanity-check diff + note V4 snapshot handling decision

### Review
- [x] Summarize files touched, note any deviations from handover spec
- V4 snapshot: single-request (`timescale` param removed) based on v4 API
- Tests: `python3 -m pytest tests/` fails (missing `database_url` env)

## Audit Tasks Dev Table Plan — 2026-04-14

### Plan
- [ ] Review schema/migration conventions for hub tables and existing audit-style tables
- [ ] Draft schema + indexes + lifecycle fields for audit_tasks_dev (claim/lease + dedupe)
- [ ] Outline hub API endpoints + integration points (router + schema catalog)
- [ ] Provide migration placement guidance (hub/db/migrations + optional runtime ensure)

## AuditChecklist Sitrep — 2026-04-14

### Plan
- [ ] Parse all checklist items in `frontend/src/pages/AuditChecklist.jsx`
- [ ] Cross-reference each item against codebase paths/docs and current repo state
- [ ] Flag mismatches (DONE but missing, OPEN but implemented, stale refs)
- [ ] Produce sitrep report (DONE/OPEN/BLOCKED/STale + key gaps)

## AuditChecklist → DB Migration Plan — 2026-04-14

### Plan
- [ ] Design target schema for audit_tasks_dev (core columns + JSONB extensions)
- [ ] Map existing AuditChecklist fields to DB columns/metadata
- [ ] Draft migration plan (seed strategy, idempotency, versioning)
- [ ] Add agent-facing banner text plan for AuditChecklist.jsx

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
- [ ] Implement V1 TimesFM disagreement gate (highest ROI)
- [ ] Remove paper mode Binance resolution fallback
- [ ] Add error handling to frontend API calls
- [ ] Clean up dead pages (PaperTrading.jsx, Learn.jsx)

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

## Completed

- [x] Phase 1: Foundation (Docker, DB schema, Auth, project skeleton)
- [x] Codebase audit — 2026-04-06
- [x] Dashboard + Margin Engine — 2026-04-10 (see section above)

---

## Margin Engine Clean Architecture Refactoring — 2026-04-14

### Audit Complete ✅

**Documents Created:**
- `docs/margin_engine/clean-architecture-audit-2026-04-14.md` — Full audit report
- `tasks/margin-engine-clean-arch.md` — Actionable implementation plan

### Findings Summary

The margin engine has **solid port-based architecture** but violates clean architecture in 3 key areas:

1. **Domain contamination** (CRITICAL) — v4 API models (`V4Snapshot`, `Consensus`, etc.) live in `domain/value_objects.py` — should be in adapter
2. **Services layer misplacement** (HIGH) — Business logic in `services/` should be in `application/services/`
3. **Missing presentation layer** (MEDIUM) — HTTP status server buried in `infrastructure/`

### Refactoring Plan (5 Phases)

| Phase | Description | Effort | Priority |
|-------|-------------|--------|----------|
| 1 | Domain Cleanup — Move v4 models to adapter, split value objects | 2-3 days | P0 ⚡ |
| 2 | Services → Application — Relocate business logic | 2-3 days | P1 🔥 |
| 3 | Infrastructure/Presentation Separation — Create presentation layer, Alembic | 3-4 days | P2 🏗️ |
| 4 | Use Case Refactoring — Extract strategies, reduce 840-line use case | 2-3 days | P3 🔄 |
| 5 | DTO Layer — Add proper I/O boundaries | 1-2 days | P4 📦 |

**Total Effort:** 10-15 days over 4 weeks  
**Risk Level:** Medium (requires careful testing at each phase)

### What's Already Good ✅

- Port-based dependency inversion (well implemented)
- Use cases have clear single responsibility
- Adapters properly implement ports
- Test structure is solid

### Next Steps

- [ ] Review audit report: `docs/margin_engine/clean-architecture-audit-2026-04-14.md`
- [ ] Review implementation plan: `tasks/margin-engine-clean-arch.md`
- [ ] Decide: Start Phase 1 implementation or defer
- [ ] If starting: Begin with moving v4 value objects to adapter

### Notes

- **Related to main engine:** The main engine (in `engine/`) recently underwent clean architecture refactor (see commits 4221581, b97655e, 6251152)
- **Margin engine is separate:** This is the legacy `margin_engine/` directory (Binance margin trading)
- **Migration approach:** Incremental refactoring, preserving working functionality at each step
- **AuditChecklist context:** As noted, `frontend/src/pages/AuditChecklist.jsx` was migrated to `audit_tasks_dev` table in DB

### Review

- Audit performed by AI Assistant using `clean_architecture_python_guide.md` (v1.0, Jan 2026)
- Subagent deep-dive completed
- Full report and actionable plan saved to docs
- Ready for implementation decision

### Strategy Coverage

**All 9 strategies are covered** in the refactoring plan:

| Strategy | File | Lines | Status |
|----------|------|-------|--------|
| Regime Router | `regime_adaptive.py` | 463 | ✅ Phase 2 |
| Trend Strategy | `regime_trend.py` | 472 | ✅ Phase 2 |
| Mean Reversion | `regime_mean_reversion.py` | 472 | ✅ Phase 2 |
| No-Trade Regime | `regime_no_trade.py` | 185 | ✅ Phase 2 |
| Cascade Detector | `cascade_detector.py` | 407 | ✅ Phase 2 |
| Cascade Fade | `cascade_fade.py` | 544 | ✅ Phase 2 |
| Continuation Alignment | `continuation_alignment.py` | 801 | ✅ Phase 2 |
| Fee-Aware Continuation | `fee_aware_continuation.py` | 1406 | ✅ Phase 2 |
| Quantile VaR Sizing | `quantile_var_sizer.py` | 719 | ✅ Phase 2 |

**Orchestration:**
- `open_position.py` (840 lines) → Phase 4 (extract entry strategies)
- `manage_positions.py` (~600 lines) → Phase 4 (extract management logic)

**Result:** All 9 strategy files moved to `application/services/` with proper package organization.

### YAML-Configurable Strategies (Phase 6 - Optional)

Like the main engine, we can make margin engine strategies **YAML-configurable**:

```yaml
# margin_engine/strategies/configs/regime_trend.yaml
name: regime_trend
version: "1.0.0"
mode: ACTIVE
asset: BTC

regime:
  type: trend
  params:
    min_confidence: 0.15
    direction_gate: true

sizing:
  type: quantile_var
  params:
    quantile: 0.95
    var_multiplier: 1.0
```

**Benefits:**
- Hot reload without code changes
- A/B testing of parameter variations
- Git-tracked strategy versions
- Non-dev trading input

**Effort:** 3-4 days (Week 5, optional)

**Status:** Planned in Phase 6 of refactoring plan

---

## 2026-04-15 Cleanup pass

Marked gate_audit/window-trace schema section (lines 6–44) as complete — all plan, clean-arch slices, trace model, notification shape, and delivery order items were delivered.

PRs shipped today:
- **PR #190** — Retire CLOBReconciler dual path (`ENGINE_USE_RECONCILE_UC` made permanent, legacy branch removed)
- **PR #191** — Fix test stubs (`fetch_trades` / `manual_trades_joined_poly_fills` signatures aligned)
- **PR #192** — Retire `gate_audit` table (no-op wrappers removed, writes migrated to `gate_check_traces`)
