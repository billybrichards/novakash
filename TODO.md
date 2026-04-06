# TODO — Novakash Frontend/Engine

## 🔴 HIGH PRIORITY

### Retry Order ID Mismatch (Resolution Callback Bug)
**Status:** TODO — causes resolution loop error on retry orders
**What:** When FOK fails and GTC retry is placed, the retry order gets a NEW CLOB order ID.
But the order_manager only knows the original ID. When Polymarket resolves the retry order,
`resolve_order()` raises KeyError because the new ID isn't registered.
**Impact:** Wins/losses from retry orders don't get recorded to DB automatically.
Had to manually record the 08:50 WIN (+$7.70).
**Fix:** Register retry order IDs in order_manager, or map retry→original ID.
**Files:** `engine/execution/order_manager.py`, `engine/strategies/five_min_vpin.py` (retry logic)

### Redemption Timing Issue
**Status:** TODO — redemption submits but doesn't execute for fresh positions
**What:** Redeemer successfully submits via Builder Relayer (PROXY type) but fresh live trade
positions don't redeem immediately. Older positions (Apr 4) redeemed fine.
**Theory:** Fresh positions need more time to settle on-chain before redemption works.
**Fix:** Add retry with delay (try again after 5-10 min) or check settlement status first.
**Files:** `engine/execution/redeemer.py`

### V2 Probability Gate (TimesFM v2 LightGBM)
**Status:** TODO — data validated, API live, needs engine integration
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

### VPIN Warm Start on Engine Restart
**Status:** TODO — avoids missing 3-4 trades on each restart
**What:** On engine startup, load last N ticks from `ticks_binance` table to pre-fill VPIN buckets
- VPIN needs ~500 volume buckets (VPIN_BUCKET_SIZE_USD=500000)
- Currently takes 3-5 minutes of live data to warm up
- During warm-up, VPIN reads 0 → all trades gated → missed opportunities

**Fix:** In VPIN calculator `__init__`, query `ticks_binance` for last 30 minutes of data and replay through the bucket algorithm. VPIN will be warm within seconds of startup.

**Files:** `engine/signals/vpin.py` (add `warm_start(db)` method)

### V1 TimesFM Disagreement Gate
**Status:** TODO — proven by data analysis
**What:** Block trades when v1 TimesFM strongly disagrees (>90% confidence in opposite direction)
**Evidence:** DISAGREE trades = 40% WR (-$15.20), AGREE trades = 84.6% WR (+$22.77)
**Files:** `engine/strategies/five_min_vpin.py`

---

## ✅ COMPLETED (2026-04-05 19:31 UTC)

### Orchestrator Notification Wiring
**Status:** DONE — Dual-AI notifications live in production
**Commits:** c0c48f7, d22c3a5
**Files Updated:**
- `engine/strategies/five_min_vpin.py` — TRADE/SKIP decisions call `send_trade_decision_detailed()`
- `engine/strategies/orchestrator.py` — Trade resolution calls `send_outcome_with_analysis()`

**What's live:**
- Every trade decision generates: decision message (mandatory) + AI prediction (separate)
- Every trade outcome generates: outcome message (mandatory) + AI analysis (separate)
- Claude primary, Qwen122b fallback, no data loss on timeout

---

## ✅ VERIFIED WORKING

### V7.1 Config
- ✅ Gate: 0.45 (from 0.628) 
- ✅ Min delta: 0.02% (all regimes)
- ✅ DB backfilled: 120 v7.1-eligible windows, 73.3% WR
- ✅ Retroactive calculation in API (v71_* columns)
- ✅ WindowTimeline shows dual decision (Legacy / v7.1)
- ✅ V7.1 LIVE DECISION panel on V58Monitor

### Win Streak Display
- ✅ Computed from trades table (real outcomes)
- ✅ Updates per trade resolution
- ✅ Accurate to Polymarket resolution

### Frontend
- ✅ WindowResults: v7.1 filter + retroactive WR stat
- ✅ V58Monitor: timeline, live decision panel
- ✅ Window time formatting (e.g., "19:10 UTC") on all notifications

### Notification Improvements
- ✅ Window UTC time on all cards
- ✅ Dual AI (Claude + Qwen fallback)
- ✅ Separated decision/outcome from analysis (no data loss on timeout)

---

## 🚀 READY TO DEPLOY

Code is stable, tested, ready for Montreal restart.

**Last commit:** 3359c6b (dual-AI system)
**Status:** Push to Montreal, restart engine

---

## 📝 Notes

- Qwen122b fallback requires `QWEN_HOST=ollama-ssh1` env var
- Claude maxed out? Qwen will take over automatically
- Raw trade data always preserved (AI analysis in separate messages)
- v7.1 WR: 73.3% on backfilled 120 windows

### Macro Observer — Engine Integration (Phase 2)
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
