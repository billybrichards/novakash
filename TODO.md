# TODO — Novakash Frontend/Engine

## 🔴 HIGH PRIORITY

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
