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


### v8.0 DB Migration
**Status:** TODO — run before first v8.0 deploy
**File:** `migrations/add_v8_columns.sql`
**Columns:** delta_source, execution_mode, fok_attempts, fok_fill_step, clob_fill_price,
confidence_tier, entry_time_offset, gates_passed, gate_failed

### v8.0 Telegram Notification Overhaul
**Status:** TODO — current notifications are average, need complete redesign
**What:** Redesign all 6 notification types for v8.0:
1. Window Evaluation Card — shows all source prices (Tiingo/CL/BN/CLOB), gate results
2. FOK Ladder Progress — real-time step-by-step fill attempts
3. Outcome Card — WIN/LOSS with delta source attribution, session running totals
4. Skip Card — which gate failed, would-have-won tracking
5. Session Summary (hourly) — WR, P&L, fill rate, source accuracy comparison
6. Divergence Alert — triggered when CL-BN spread spikes
**Files:** `engine/alerts/telegram.py`
**Priority:** Implement alongside Phase 1+2 code changes

### Macro Observer — Investigation (DO NOT WIRE INTO TRADING)
**Status:** TODO — collecting data, not gating
**Status:** TODO — Service built (feat/macro-observer), pending engine wiring
**What:** Engine reads latest `macro_signals` row from DB each window evaluation and applies:
- Mode 1 (Neutral <50%): no changes
- Mode 2 (Trend-Aware 50-79%): gate contrarian bets, adjust delta thresholds
- Mode 3 (Override 80%+): early entry T-120/T-180, direction flip, 1.3x sizing
**Files to update:**
- `engine/strategies/orchestrator.py` — load macro signal on startup + each heartbeat
- `engine/strategies/five_min_vpin.py` — apply gate/threshold/size modifiers pre-execution
- `engine/persistence/db_client.py` — write macro_signal_id to window_snapshots
**Blocked by:** Railway deploy of macro-observer service (Billy to trigger)

### Tiingo Integration
**Status:** TODO — API key available from earlier TODO
**Key:** 3f4456e457a4184d76c58a1320d8e1b214c3ab16
**Why critical:** Chainlink oracle uses multi-exchange LWBA median. Binance alone diverges 57%
of the time vs oracle direction. Tiingo is an oracle node input — should track much better.
**What to build:**
- Add to data-collector: record Tiingo BTC/USD at window open/close timestamps
- Compare Tiingo vs oracle resolution direction for 48h to validate
- If tracks well: replace Binance delta in signal calculation

### Gamma Balance Block (Feature Flag — Monitor First)
**Status:** TO MONITOR before implementing
**Observation:** When `abs(gamma_up_price - gamma_down_price) < 0.02`, market has zero
directional conviction. Hypothesis: correlates with losses.
**To do:** Query `window_snapshots` where Gamma is BALANCED and cross with outcome.
Only implement if data confirms the hypothesis.
**When ready:** Feature flag `gamma_balance_block` (default OFF) in runtime_config.py

### FOK Ladder (ORDER_PRICING_MODE=fokladder)
**Status:** TODO — full plan at docs/FOK_LADDER_PLAN.md
**What:** Rapid FOK attempts with fresh Gamma every 2s, fast re-eval (delta/VPIN/floor/cap) at each step, GTD fallback
**Why:** 96.9% of unfilled trades would have won. Need higher fill rate.
**Blocking:** Must investigate Polymarket oracle resolution first (BTC goes DOWN but oracle says UP)

### Signal Component Modularity
**Status:** TODO
**What:** Make each evaluator independently toggleable via env var:
- TIMESFM_ENABLED, TWAP_ENABLED, CG_VETO_ENABLED, TWAP_OVERRIDE_ENABLED
**Why:** Test combinations, disable TWAP override that flips direction wrong

### Polymarket Oracle Investigation
**Status:** TODO — BLOCKING for FOK ladder
**What:** Understand exactly how UpDown oracle resolves:
- What price source? (Binance, Chainlink, Pyth?)
- What timestamp? (window close? +4min?)
- Open→close or TWAP/VWAP?
**Why:** Multiple trades where BTC moved in our direction but oracle disagreed

### Tiingo Data Source
**Status:** TODO — API key available
**Key:** 3f4456e457a4184d76c58a1320d8e1b214c3ab16
**Endpoints:**
- Top-of-book (real-time): `https://api.tiingo.com/tiingo/crypto/top?tickers=btcusd`
- 5min candles: `https://api.tiingo.com/tiingo/crypto/prices?tickers=btcusd`
- Shows exchange source (GDAX, BULLISH, etc.)

**Why useful:**
- Multi-exchange price view — our Binance price may differ from oracle's source
- Could explain "direction correct but oracle disagreed" losses
- Cross-reference with Polymarket oracle resolution
- Add as data column in countdown_evaluations (tiingo_price at each stage)

**Integration:** Add to data collector or engine heartbeat as supplementary price feed
- All `/v58/*` frontend endpoints are valid — backed by `hub/api/v58_monitor.py`
- All `/playwright/*` endpoints are valid — backed by `hub/api/playwright.py`
- All `/trading-config/*` endpoints are valid — backed by `hub/api/trading_config.py`
- `useApi()` hook supports both `api.get()` and `api('GET', url)` calling conventions
