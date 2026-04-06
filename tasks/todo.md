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
- [ ] Implement V1 TimesFM disagreement gate (highest ROI)
- [ ] Remove paper mode Binance resolution fallback
- [ ] Add error handling to frontend API calls
- [ ] Clean up dead pages (PaperTrading.jsx, Learn.jsx)

## Completed

- [x] Phase 1: Foundation (Docker, DB schema, Auth, project skeleton)
- [x] Codebase audit — 2026-04-06
