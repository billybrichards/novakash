# TODO — Novakash Frontend/Engine

## 🔴 HIGH PRIORITY

### V2 Probability Gate (TimesFM v2 LightGBM)
**Status:** OPEN — data validated, API live, needs engine integration
**Endpoint:** `http://3.98.114.0:8080/v2/probability?asset=BTC&seconds_to_close=60`
**Data:** 1,083 predictions at T-60, 30-day window

**What:** Add v2 calibrated probability as a gate at T-60 evaluation:
- If engine direction = DOWN but v2 probability_up > 0.60 → BLOCK
- If engine direction = UP but v2 probability_up < 0.40 → BLOCK
- Otherwise → PASS

**Why:** 
- V2 calibration is far better than v1 (ECE 0.18 vs 0.40)
- When v2 says probability=1.0, empirical rate is 97.6%
- V2 would have blocked 2 of today's losses (20:30, 19:20)
- TimesFM v1 disagreement trades have 40% WR vs 84.6% when agreeing
- V2 at T-30 has 75.7% accuracy (v1: 59.8%)

**Risk:** V2 model is still smoke-test (53 training rows). Monitor false block rate.
**Files:** `engine/strategies/five_min_vpin.py` (add v2 call at T-60 eval)

### V1 TimesFM Disagreement Gate
**Status:** OPEN — proven by data analysis, highest-ROI fix available
**What:** Block trades when v1 TimesFM strongly disagrees (>90% confidence in opposite direction)
**Evidence:** DISAGREE trades = 40% WR (-$15.20), AGREE trades = 84.6% WR (+$22.77)
**Files:** `engine/strategies/five_min_vpin.py`

### Redemption Timing Issue
**Status:** PARTIALLY FIXED — infrastructure fixed, settlement timing still open
**What:** Redeemer successfully submits via Builder Relayer (PROXY type) but fresh live trade
positions don't redeem immediately. Older positions (Apr 4) redeemed fine.
**Fixed (commits 6eb6a3d, c0e9681, ca41566):**
- Wallet type fixed (PROXY tx type, not SafeTransaction)
- Redeemer now starts on PAPER→LIVE mode switch (not just boot)
- paper_mode flag set correctly before connect()
**Still open:** Fresh positions may need more settlement time on-chain before redemption.
**Fix needed:** Add retry with delay (try again after 5-10 min) or check settlement status first.
**Files:** `engine/execution/redeemer.py`, `engine/strategies/orchestrator.py`

### Paper Mode Resolution Fallback (Win Rate Corruption)
**Status:** OPEN — corrupts win rate data when Polymarket API unavailable
**What:** `order_manager.py:470-603` falls back to Binance BTC price comparison when
Polymarket Gamma API is unavailable. This produces different WIN/LOSS outcomes than
the actual oracle resolution, corrupting historical win rate stats.
**Fix:** Remove fallback entirely. If oracle unavailable, mark trade as PENDING and retry
resolution on next poll cycle. Never guess outcomes from Binance price.
**Files:** `engine/execution/order_manager.py` (lines 470-603, `_determine_paper_outcome`)

---

## 🟡 MEDIUM PRIORITY

### Frontend: Missing Error Handling on API Calls
**Status:** OPEN
**What:** Multiple pages swallow API errors silently, leaving components in empty/loading state:
- `Signals.jsx:17-22` — `.then()` with no `.catch()`
- `PnL.jsx:16-19` — same pattern
- `Indicators.jsx:341` — uses raw `fetch()` instead of `useApi()` hook
- `System.jsx` — no error feedback to user
**Fix:** Add `.catch()` blocks with user-visible error messages on all API calls.

### Frontend: Dead/Stale Pages
**Status:** OPEN
**Pages to clean up:**
- `PaperTrading.jsx` (41KB) — Duplicate of Dashboard functionality, likely superseded
- `Learn.jsx` (93KB) — Hardcoded tutorial content, no API calls, enormous dead weight
- `Changelog.jsx:245-251` — Known issues list references old fixed bugs ("Bankroll resets on deploy", "Telegram crash on 5-min orders")
**Fix:** Remove PaperTrading.jsx, trim Learn.jsx or move to static docs, update Changelog known issues.

### Frontend: V58Monitor.jsx Issues
**Status:** OPEN — audit complete, see `docs/V58_MONITOR_AUDIT.md`
**What:** 3,113-line single component (16 internal components). Key issues:
- `windowStatus()` (line 49-59) determines WIN/LOSS from `delta_pct` (directional), NOT Polymarket oracle — contributes to win rate confusion
- Hardcoded `$4.00` stake in TradeButtons (lines 1904, 1926, 2037) — should be configurable
- Hardcoded `$0.70` entry cap display (line 2743) — should come from config
- Missing `api` in useEffect deps in TradeButtons (line 1820)
- 4 silent `catch {}` blocks swallowing errors (lines 1814, 2310, 2396, 2398)
- Stale "v5.8" naming in section headers (should be v7)
- Duplicate StatCard component (already exists in components/)
- 6 API calls every 15s + 1 every 2s when page is open (performance concern)
**Decomposition:** Could split into ~8 files under `V58Monitor/` directory — LOW priority since it works
**Files:** `frontend/src/pages/V58Monitor.jsx`

### Frontend: useEffect Dependency Array Issues
**Status:** OPEN
**What:** Missing `api` dependency in useEffect hooks causes stale closures:
- `Signals.jsx:23` — `[activeTab]` should include `api`
- `PnL.jsx:20` — `[]` should include `api`
- `System.jsx:15` — `[]` should include `api`
**Fix:** Add `api` to dependency arrays.

### Frontend: Duplicate Routes
**Status:** OPEN
**What:** `/config` and `/trading-config` both route to `TradingConfig` component in App.jsx.
Old `Config.jsx` page still exists but uses hardcoded defaults instead of API fetch.
**Fix:** Remove duplicate route, archive or delete `Config.jsx`.

### Win Rate Source Reconciliation
**Status:** OPEN — documented, needs architectural decision
**What:** Three independent win rate sources that don't reconcile:
1. **Polymarket WR** — `trades.outcome` (WIN/LOSS from oracle) — displayed on Dashboard, Trades, PnL
2. **Directional WR** — `window_snapshots.v71_correct` — displayed on StrategyAnalysis
3. **Backtest WR** — stale JSON files with different methodologies
**Gap explained:** StrategyAnalysis.jsx:77-87 documents the gap (entry spread, oracle timing, price reversions).
**Issues:**
- `v71_correct` population logic unclear — column is READ by v58_monitor.py but write path undocumented
- Resolution timing race: orders resolve at 240s, oracle at 280-300s
- Backtest JSON files at root are stale and misleading
**Fix:** Document the three metrics clearly. Increase resolve_after from 240s to 280s. Clarify v71_correct write path.

---

## ✅ COMPLETED

### Retry Order ID Mismatch
**Status:** FIXED — retry logic removed entirely (commit 2b0a5a1)
**What:** FOK→retry created new CLOB IDs; resolution callback couldn't match.
Fixed with ID alias mapping in commit 8c680ab, then retry strategy itself was removed
(reverted to single GTC order) after poor results (28% WR, -$35).

### VPIN Warm Start on Engine Restart
**Status:** FIXED — commit 88c71e8
**What:** VPIN calculator now has `warm_start(db_pool)` method that loads last 30 minutes
of ticks from `ticks_binance` table and replays through bucket algorithm.
Integrated into orchestrator startup sequence. VPIN warms within seconds.

### Orchestrator Notification Wiring
**Status:** DONE — Dual-AI notifications live in production
**Commits:** c0c48f7, d22c3a5
**What's live:**
- Every trade decision generates: decision message + AI prediction
- Every trade outcome generates: outcome message + AI analysis
- Claude primary, Qwen122b fallback, no data loss on timeout

### V7.1 Config
- ✅ Gate: 0.45 (from 0.628) 
- ✅ Min delta: 0.02% (all regimes)
- ✅ DB backfilled: 120 v7.1-eligible windows, 73.3% WR
- ✅ Retroactive calculation in API (v71_* columns)

### Win Streak Display
- ✅ Computed from trades table (real outcomes)
- ✅ Accurate to Polymarket resolution

### Price Floor Check
- ✅ Block entries below $0.30 (commit 73a636d)

### Polymarket-Only Resolution (Live Mode)
- ✅ Live trades resolve ONLY from Polymarket oracle (commit 5af81b5)
- ⚠️ Paper mode still has Binance fallback (see HIGH PRIORITY above)

---

## 📝 Notes

- Market data (30 days Polymarket resolutions) lives in PostgreSQL `market_data` table, NOT a local directory
- Populated by `data-collector/backfill.py` (historical) and `data-collector/collector.py` (continuous)
- Qwen122b fallback requires `QWEN_HOST=ollama-ssh1` env var
- v7.1 WR: 73.3% on backfilled 120 windows
- All `/v58/*` frontend endpoints are valid — backed by `hub/api/v58_monitor.py`
- All `/playwright/*` endpoints are valid — backed by `hub/api/playwright.py`
- All `/trading-config/*` endpoints are valid — backed by `hub/api/trading_config.py`
- `useApi()` hook supports both `api.get()` and `api('GET', url)` calling conventions
