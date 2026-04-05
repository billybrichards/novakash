# TODO — Novakash Frontend/Engine

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
