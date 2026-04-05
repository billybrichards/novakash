# TODO — Novakash Frontend/Engine

## 🔴 HIGH PRIORITY

### Orchestrator Notification Wiring (BLOCKING)
**Status:** Dual-AI system built, NOT YET WIRED INTO ORCHESTRATOR
**Files:** `engine/strategies/orchestrator.py`
**Lines:** ~1220 (trade decision), ~1260 (trade resolution)

**What:** Replace old `send_window_report` calls with new dual-AI methods:
- `send_trade_decision_detailed()` — separated decision + AI prediction
- `send_outcome_with_analysis()` — separated outcome + AI analysis

**Why:** Ensure raw trade data never blocks on Claude API timeouts (Qwen fallback active)

**Effort:** ~20 lines, straightforward replacement

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
