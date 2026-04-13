# Novakash Status Memo — April 13, 2026

**Generated:** 2026-04-13  
**Primary Branch:** `develop`  
**Repos:** `novakash/develop` + `novakash-timesfm-repo/main`

---

## Executive Summary

**Trading Status:** ⏸️ **PAUSED** (paper mode only)  
**Critical Fix Applied:** ✅ TimesFM v5.2 `chainlink_price` feature missing from 4/5 call sites — **FIXED, pending engine restart**  
**Active Strategy Proposals:** 2 high-confidence edges discovered (DOWN-only 76-99% WR, Asian UP 81-99% WR)  
**Last Major Deploy:** PR #153 (CLOB sizing fix + Strategy History tab) — not yet merged to develop  

---

## Yesterday's Commits (April 12, 2026) — 87 commits on develop

### Major PRs Merged

**PR #153** — `feat/clob-sizing-fix-and-history-tab`  
- Strategy Floor tabs + CLOB sizing fix  
- "Missed?" uses model direction from metadata (not calculated)  
- Strategy History tab added  

**PR #151** — `feat/clob-sizing-fix-and-history-tab`  
- CLOB sizing audit fix + Strategy History tab implementation  

**PR #150** — `fix/engine-paper-mode-hub`  
- **CRITICAL FIX:** ENGINE LIVE toggle now reads `system_state` not heartbeat  
- Previously: toggling `.env` alone didn't stop trading (DB heartbeat override)  
- Now: UI toggle → DB update → engine respects it  

**PR #149** — `fix/down-only-threshold-010`  
- Relaxed confidence threshold to 0.10 for DOWN-only strategy  

**PR #148** — `fix/fe-all-strategies-visible`  
- All 4 strategies visible in frontend + ENGINE: LIVE + skip reasons  

**PR #147** — `fix/monitor-evaluate-ui`  
- **DOCS ADDED:** UP/DOWN strategy runbook + analysis script  
- Hub: strategy-windows endpoint + DISTINCT ON dedup + V10 gate label  

**PR #146** — `feat/four-strategy-paper-live`  
- 4-strategy setup + Evaluate redesign + CLOB/monitor fixes  
- 4-strategy paper trading enabled  

**PR #144** — `fix/clob-key-mismatch`  
- **CRITICAL FIX:** key mismatch between `get_clob_order_book` and `clob_feed`  
- CLOB feed was broken in paper mode (0% coverage)  

**PR #142** — `claude/feat/nt01-notes-page`  
- Added quick template picker to Notes page  

**PR #141** — `docs/up-strategy-research-brief`  
- UP strategy research brief — agent analysis task  

**PR #140** — `fix/window-snapshot-strategy-port`  
- Write `window_snapshots` on no-trade ticks in strategy port  

**PR #139** — `fix/v4-down-only-timing`  
- Tightened timing gate to T-90 to T-150 (previously trading at T-60 = 48.7% WR)  

**PR #138** — `feat/v4-down-only-strategy`  
- V4 DOWN-only strategy + dynamic strategy selector UI  

**PR #136** — `worktree/audit`  
- **CRITICAL:** CLOB fix + DOWN-ONLY findings to notes  
- Use real order book in paper mode (Montreal has live data)  
- Enable CLOB feed in paper mode  

**PR #135** — `claude/docs/signal-eval-runbook`  
- Full signal eval runbook + report script  

**PR #134** — `claude/fix/fe-comprehensive-review`  
- 8 issues from live screenshot review  

**PR #133** — `claude/fix/fe-monitor-sequoia-v3-sitrep`  
- Sequoia p_up fallback + V3 API endpoint + audit updates + sitrep dist display  

**PR #132** — Strategy Lab shadow comparison tab (V10 vs V4 live)  

**PR #131** — `claude/fix/v4-snapshot-dataclass`  
- V4Snapshot dataclass field ordering fix  

**PR #130** — Schema catalog: added `ticks_v4_decision`  

---

## Branches Ahead of Develop

### 1. `fix/timesfm-v5-chainlink-feature` (2 commits ahead)
```
791a54a fix: Correct indentation in five_min_vpin.py line 1751
ddac824 Session 6: V4 strategy persistence + fee wall fix + 1h/4h model support
```
**Status:** ✅ Ready to merge — contains critical `chainlink_price` fix

### 2. `claude/feat/nt01-notes-page` (1 commit ahead)
```
caa2b28 feat: add Asian UP strategy template + seed notes
```
**Status:** ✅ Ready to merge — documentation

### 3. `fix/eval-offset-display` (0 commits ahead — already in develop)

---

## Critical Issues & Status

### 🔴 CRITICAL — TimesFM v5.2 Broken (FIXED, PENDING DEPLOY)

**Problem:** Model always returns `p_up=0.60614485` (constant UP at 10.2% conviction)

**Root Cause:** `chainlink_price` missing from 4/5 `build_v5_feature_body` calls. Model requires 25 features, was only receiving 1 (`eval_offset`).

**Fix Applied (PR #153+):**
- `engine/strategies/five_min_vpin.py:1759` — Added `chainlink_price=window_snapshot.get("chainlink_open")`
- `engine/use_cases/evaluate_window.py:238` — Added `chainlink_price=None`  
- `engine/use_cases/evaluate_window.py:851` — Added `chainlink_price=window_snapshot.get("chainlink_open")`
- `engine/signals/gates.py:952` — Already present (no change needed)

**Expected After Restart:**
- `v2_probability_up` varies (0.3-0.9, not constant 0.606)
- `v2_direction` mixed (~50/50, not always UP)
- ~20-25 trades/day (currently 0)
- 70-80% accuracy (currently 17.6%)

**Deployment Required:**
```bash
# SSH to Montreal
ssh ubuntu@15.223.247.178
cd /home/novakash/novakash
git pull origin develop  # or main
git checkout fix/timesfm-v5-chainlink-feature
git merge develop
python -m engine.main
```

**Verification:**
```sql
SELECT 
    COUNT(*) as n,
    MIN(v2_probability_up) as min_p,
    MAX(v2_probability_up) as max_p
FROM signal_evaluations
WHERE evaluated_at >= NOW() - INTERVAL '1 hour'
  AND asset = 'BTC';
-- Expected: min_p < 0.5, max_p > 0.7
```

---

### 🟡 HIGH — Fee Wall Analysis (INVESTIGATED, WORKING CORRECTLY)

**Finding (Apr 13, ~22:00 UTC):** Fee wall is **NOT** killing good trades

**Before Fix:** 100% of entry skips were `expected_move_below_fee_wall`

**After Continuation Divisor (3.0x):** Effective fee wall = 5 bps  
- Fee wall NO LONGER the bottleneck
- Current blockers: `conviction_below_threshold` (p_up=0.596, needs >=0.60), `consensus_fail` (macro/signal misaligned)
- Expected move range: -3.1 to +3.2 bps typical, spikes to +11.3 bps pass easily

**Verdict:** System is working correctly — filtering by conviction/consensus as designed

---

### 🟡 HIGH — DOWN-Only Strategy (DISCOVERED, NOT IMPLEMENTED)

**Analysis:** 897,503 samples over ~5 days

| Direction | CLOB Ask Band | N | Win Rate |
|-----------|---------------|---|----------|
| DOWN | > 0.75 | 175,261 | **99.0%** |
| DOWN | 0.55-0.75 | 112,371 | **97.8%** |
| DOWN | 0.35-0.55 | 86,821 | **92.1%** |
| DOWN | < 0.35 | 177,435 | **76.2%** |
| UP | any | 346,000 | **1.5-53%** |

**Implementation Status:**  
- ✅ Strategy proposed in `docs/analysis/DOWN_ONLY_STRATEGY_2026-04-12.md`  
- ✅ Gate logic defined (`DirectionFilterGate` + `CLOBSizingGate`)  
- ❌ Not yet added to `engine/signals/gates.py`  
- ❌ Not yet deployed

**Recommendation:** Implement as paper trading gate first (SIG-03 + SIG-04 from audit checklist)

---

### 🟢 MEDIUM — Asian Session UP Strategy (DISCOVERED, NOT IMPLEMENTED)

**Analysis:** 5,543 windows (Apr 10-12)

**Gate Condition:**
```python
if (v2_direction == 'UP' and
    0.15 <= abs(v2_probability_up - 0.5) <= 0.20 and
    hour_utc in [23, 0, 1, 2]):  # 11PM-2AM UTC
    return TRADE_UP
```

**Results:**
- 01:00 UTC: **98.9%** WR (1,916 samples)
- 23:00 UTC: **91.8%** WR (1,207 samples)
- 02:00 UTC: **85.6%** WR (549 samples)
- 00:00 UTC: **81.2%** WR (1,921 samples)

**Implementation Status:**  
- ✅ Documented in `docs/analysis/UP_STRATEGY_DISCOVERY_2026-04-12.md`  
- ✅ Research brief created (`docs/UP_STRATEGY_RESEARCH_BRIEF.md`)  
- ❌ Not yet implemented in engine

---

## Audit Checklist Status

**Total Items:** 50+ tracked across categories

**Completed (Recent):**
- ✅ PE-01 — CLOB feed column mismatch (1090 errors/hour → 0)
- ✅ PE-02 — Reconciler LIKE query type dedup (4 errors/hour → 0)
- ✅ STOP-01 — Live trading pause procedure documented
- ✅ DQ-06 — Paper+binance broken default (documented, fix pending)
- ✅ NT-01 — Notes page template picker
- ✅ FE-MONITOR-01a-e — Monitor page fixes (5 items)

**Open (High Priority):**
- 🔴 SIG-03 — DirectionFilterGate (skip all UP predictions)
- 🔴 SIG-04 — CLOBSizingGate (size based on clob_down_ask)
- 🟡 SIGNAL-CLOB-EDGE-GATE — Gate on Sequoia vs CLOB divergence
- 🟡 V4-TIMING-BUG — Verify T-90 to T-150 gate working
- 🟡 CA-EXEC-INDEPENDENCE — Extract `_execute_trade` use case
- 🟡 Macro Phase C — Replace Qwen with LightGBM MacroV2

**Next Up (from AUDIT_PROGRESS.md):**
1. FE-MONITOR-01 remaining (Sequoia p_up NO DATA, V3 join via API, gate double render)
2. SIGNAL-CLOB-EDGE-GATE (most impactful improvement)
3. V4-TIMING-BUG verification
4. CA-EXEC-INDEPENDENCE
5. Macro Phase C (LightGBM replacement)

---

## Active Workstreams

### 1. TimesFM v5.2 Recovery (BLOCKING)
- **Status:** Fix applied, pending engine restart
- **Owner:** Billy
- **ETA:** Immediate after deploy
- **Impact:** Restores all model predictions (currently broken → 0 trades)

### 2. DOWN-Only Strategy Implementation
- **Status:** Analysis complete, implementation pending
- **Files:** `docs/analysis/DOWN_ONLY_STRATEGY_2026-04-12.md`
- **ETA:** 1-2 hours to implement + test
- **Impact:** ~40 trades/day at 75-80% WR (currently 0 trades)

### 3. Asian UP Strategy Implementation  
- **Status:** Discovery complete, implementation pending
- **Files:** `docs/analysis/UP_STRATEGY_DISCOVERY_2026-04-12.md`
- **ETA:** 1-2 hours to implement + test
- **Impact:** ~10-15 trades during Asian session (23:00-02:59 UTC) at 80-90% WR

### 4. V4 Strategy Port Architecture (IN PROGRESS)
- **Status:** Phases A-D implemented, testing ongoing
- **PRs:** #114 (StrategyPort), #117 (Shadow comparison tab)
- **Next:** Enable v4 path in production (ME-STRAT-01 from strategy proposals)

### 5. Retrain Pipeline (ACTIVE)
- **Workflow:** Running for 1h/4h/15m slots (5m protected)
- **URL:** https://github.com/billybrichards/novakash-timesfm/actions/runs/24317357688
- **Status:** 5m Sequoia v5.2 stays untouched (calibration guard active)
- **Promotion:** 1h/4h/15m promote through quality gate (ECE <= 0.10, skill >= +0.5pp)

---

## Frontend Status

**Deployed Pages:**
- ✅ `/monitor` — Polymarket Monitor (5-band trading dashboard)
- ✅ `/evaluate` — Polymarket Evaluate (performance analysis)
- ✅ `/strategy-lab` — Strategy Lab (shadow comparison, historical replay)
- ✅ `/audit` — Audit Checklist (full taxonomy + progress tracking)
- ✅ `/margin` — Margin Dashboard (V4 strategies, V10 GHOST)
- ✅ `/window-analysis` — Window Analysis modal + Live Floor page

**Recent Fixes (PR #133, #134, #135):**
- Sequoia p_up NO DATA issue
- V3 composite join using API not DB
- Gate pipeline double render
- SRC Agreement source display
- Bankroll label
- Direction toggle for manual trades
- Live Floor BTC price fix

---

## Data Tables Reference

**Active:**
- `ticks_v2_probability` — Sequoia v5.2 predictions (currently broken → constant 0.606)
- `ticks_v4_decision` — V4 consensus decisions (NEW, PR #130)
- `signal_evaluations` — All model evaluations at T-90 to T-150
- `strategy_decisions` — V4 paper + V10 GHOST decisions per window
- `clob_book_snapshots` — Order book data (FIXED, was 0% coverage)
- `window_snapshots` — Window open/close data (FIXED, now written on no-trade ticks)

**Legacy:**
- `ticks_elm_predictions` — ELM model (deprecated, SQ-01 rename pending)
- `ticks_v3_composite` — V3 ensemble (still active)

---

## Production Environment

**Engine:**
- **Host:** Montreal (15.223.247.178)
- **Mode:** PAPER (toggle via UI at `/system`)
- **PID:** Running (verified)
- **Wallet:** `0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10`
- **Balance:** $101.21 USDC deposited

**TimesFM Service:**
- **Host:** 3.98.114.0:8080
- **Model:** Sequoia v5.2 (15a4e3e)
- **Status:** Loaded, but broken due to missing `chainlink_price` feature

**Database:**
- **Host:** Railway PostgreSQL
- **Schema:** Full v4/v10 surfaces live
- **Migrations:** Up to date (Alembic)

---

## Immediate Action Items (Prioritized)

### 🔴 BLOCKING — Do First

1. **Deploy TimesFM v5.2 fix**  
   ```bash
   cd /home/novakash/novakash
   git pull origin develop
   git checkout fix/timesfm-v5-chainlink-feature
   git merge develop
   python -m engine.main
   ```
   **Verify:** `v2_probability_up` varies (not constant 0.606) in `signal_evaluations`

2. **Verify DOWN-only gate implementation**  
   Check if SIG-03 + SIG-04 added to `engine/signals/gates.py`  
   If not: implement from `docs/analysis/DOWN_ONLY_STRATEGY_2026-04-12.md`

### 🟡 HIGH — Next

3. **Implement Asian UP strategy**  
   Add Asian session filter to `engine/signals/gates.py`  
   Gate: `hour_utc in [23, 0, 1, 2]` + `conviction 0.15-0.20`

4. **Enable v4 path in production**  
   Currently dark-deployed. Flip feature flag to enable ME-STRAT-01

5. **Implement SLOB-EDGE-GATE**  
   Gate on `(sequoia_p_up - clob_implied_prob) >= 0.04` for late_window trades

### 🟢 MEDIUM — Deferred

6. **Macro Phase C** — Replace Qwen with LightGBM MacroV2
7. **CA-EXEC-INDEPENDENCE** — Extract `_execute_trade` use case
8. **SQ-01 rename** — `elm_prediction_recorder` → `prediction_recorder` (low priority)

---

## Key Documents

**Analysis:**
- `docs/analysis/DOWN_ONLY_STRATEGY_2026-04-12.md` — 99% WR DOWN contrarian
- `docs/analysis/UP_STRATEGY_DISCOVERY_2026-04-12.md` — 91% WR Asian UP
- `docs/analysis/TIMESFM_V5_FIX_APPLIED_2026-04-13.md` — Fix summary
- `docs/analysis/SIGNAL_EVAL_RUNBOOK.md` — Full analysis guide
- `docs/analysis/trading_window_analysis_2026-04-12.md` — T-120 to T-150 sweet spot

**Audit:**
- `docs/AUDIT_PROGRESS.md` — Living audit log (582 lines, 10+ sessions)
- `frontend/src/pages/AuditChecklist.jsx` — UI dashboard (synced with AUDIT_PROGRESS.md)

**Architecture:**
- `docs/ARCHITECTURE.md` — System overview
- `docs/CI_CD.md` — Deploy workflows (engine/ still missing CI)
- `docs/SPARTA_AGENT_GUIDE.md` — Access + analysis scripts

**Runbooks:**
- `docs/V9_RUNBOOK.md` — V10 gate pipeline
- `docs/analysis/SIGNAL_EVAL_RUNBOOK.md` — Signal evaluation guide

---

## Questions/Decisions Needed

1. **Deploy strategy:** Should DOWN-only + Asian UP be implemented in paper mode first, or go live directly?

2. **V4 enablement:** Enable v4 path (ME-STRAT-01) now or wait for DOWN-only implementation?

3. **Retrain promotion:** Current workflow running with `force_promote=no`. 1h/4h/15m will promote if they pass quality gate. Any concerns?

4. **SQ-01 rename:** Proceed with `elm` → `prediction` rename (PR 1: low-risk cosmetic, PR 2-4: higher coordination)?

---

**Memo Generated:** 2026-04-13  
**Next Update:** After TimesFM v5.2 deployment verification + DOWN-only implementation
