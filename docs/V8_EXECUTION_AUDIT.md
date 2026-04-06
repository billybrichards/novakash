# v8.0 Execution Path Audit

**Date:** April 6, 2026, 21:46 UTC
**Auditor:** Novakash2
**Engine version:** v8.0 (commit aaeb24e)

---

## Executive Summary

v8.0 execution path deployed to Montreal with 15 commits today. Multiple bugs found and fixed during deployment. Current state: **operational with FOK→GTC fallback**, but dead code remains and should be cleaned.

---

## Execution Flow (Current)

```
Feed tick (every 1s)
  → Window reaches T-70: CLOSING signal emitted
  → Orchestrator: direct _evaluate_window() call (no queue delay)
  
_evaluate_window():
  1. Fetch Tiingo 5m candle (REST) → delta direction
  2. Fetch Chainlink price, Binance price → multi-source comparison
  3. Calculate VPIN from live Binance ticks
  4. TWAP result computed (monitoring only, not gating)
  5. _evaluate_signal():
     - VPIN gate: < 0.45 → SKIP
     - Delta gate: abs(delta) < threshold → SKIP
       CASCADE (VPIN ≥ 0.65): threshold = 0.01%
       TRANSITION/NORMAL: threshold = 0.02%
     - CG veto: 3+ opposing signals → SKIP
     - TWAP override: DISABLED (feature flag off)
     - TimesFM: DISABLED (feature flag off)
  6. If SKIP at T-70 → T-60 re-evaluation fires with fresh data
  7. If TRADE → _execute_trade()

_execute_trade():
  1. Risk check (bankroll %, max stake $5)
  2. Direction → token_id mapping
  3. Guardrails: geoblock, circuit breaker, rate limit
  4. FOK ladder (if book has liquidity):
     - Query CLOB best ask
     - If floor ($0.30) ≤ best_ask ≤ cap ($0.73): FOK at best_ask
     - Up to 5 attempts, +$0.02 bump, 2s interval
     - If filled: instant confirmation
  5. GTC fallback (if FOK fails — empty book, cap exceeded):
     - Fetch Gamma indicative price
     - Try RFQ first (market makers match off-book)
     - If RFQ fails: GTC limit at Gamma price
     - 60s fill poll
  6. Register order → order_manager
  7. Post-trade: fill notification
```

---

## Feature Flags (Montreal .env)

| Flag | Value | Effect |
|------|-------|--------|
| `FOK_ENABLED` | `true` (default) | FOK ladder runs first |
| `FOK_PRICE_CAP` | `0.73` | Max entry price |
| `FOK_BUMP` | `0.02` | Price increment per retry |
| `TWAP_OVERRIDE_ENABLED` | `false` | TWAP does not flip direction |
| `TWAP_GAMMA_GATE_ENABLED` | `false` | TWAP does not block trades |
| `TIMESFM_ENABLED` | `false` | No TimesFM HTTP calls |
| `TIMESFM_AGREEMENT_ENABLED` | `false` | TimesFM does not gate trades |
| `FIVE_MIN_EVAL_OFFSETS` | `70,60` | Dual eval: T-70 first, T-60 retry |
| `FIVE_MIN_ENTRY_OFFSET` | `70` | Legacy (overridden by EVAL_OFFSETS) |

---

## AI Models

| Evaluator | Model | Max Tokens | Used For |
|-----------|-------|------------|----------|
| Trade decision assessment | claude-sonnet-4-6 | 200 | Pre-trade 1-sentence risk note |
| Outcome assessment | claude-sonnet-4-6 | 200 | Post-trade 1-sentence analysis |
| Rich trade evaluator | claude-sonnet-4-6 | 100 | Resolution analysis with full context |
| Fallback | qwen35-122b-abliterated | 200 | When Claude times out |

---

## Bugs Found & Fixed (Today)

| # | Bug | Impact | Fix Commit |
|---|-----|--------|------------|
| 1 | `_get_runtime_config()` doesn't exist on strategy | Every trade crashed silently after signal eval | `156fd49` |
| 2 | FOK added to `_execute_from_signal` (unused method) instead of `_execute_trade` | FOK never executed | `5a235af` |
| 3 | `runtime` referenced before assignment in `_execute_trade` | Execution crashed at pricing stage | `9d685a2` |
| 4 | Gamma API pricing block (250 lines) ran before FOK | Stale prices, unnecessary HTTP call | `c88c0e4` |
| 5 | T-60 retry inside ACTIVE-only block | T-60 never fired after T-70 skip | `1e90ac2` |
| 6 | Feed `break` prevented second offset emission | T-60 couldn't fire in same tick as T-70 | `8c0961d` |
| 7 | FOK exhausted → `return` instead of GTC fallback | Empty book = no trade (should fallback) | `3771712` |
| 8 | Notification version showed v7.1 | Misleading | `11832b4` |
| 9 | AI evaluators using Opus (expensive) | ~10x cost vs Sonnet | `acd441c` |
| 10 | Staggered execution queue adding 2-3.5s delay | Slower FOK execution | `be21e2e` |

---

## Dead Code (To Clean)

| Item | Location | Lines | Notes |
|------|----------|-------|-------|
| `_execute_from_signal()` | five_min_vpin.py:1556 | ~250 | Unused — FOK code was added here by mistake. Entire method is dead. |
| Duplicate FOK import | five_min_vpin.py:2117 | 1 | Already imported at line 37 |
| TWAP/TimesFM references | five_min_vpin.py | ~85 refs | Feature-flagged off but code still evaluates TWAP, computes agreement scores, logs results. Could be trimmed. |
| TWAP/TimesFM in telegram.py | telegram.py | ~25 refs | Old notification references to TWAP direction, TimesFM confidence |
| Old 30s poll + bump retry code | Removed in `7189eb6` | -194 lines | ✅ Already cleaned |
| Old Gamma pricing block | Removed in `c88c0e4` | -248 lines | ✅ Already cleaned |
| Staggered execution loop | orchestrator.py:2332 | ~100 lines | Still present but bypassed. Keep for future multi-asset. |

---

## Notification Cards (v8.0)

| Card | Method | Status |
|------|--------|--------|
| Window Open | `send_window_open` | ✅ v8.0 format |
| T-90/T-120 Snapshot | `send_window_snapshot` | ✅ v8.0 format, Tiingo delta |
| Trade Decision | `send_trade_decision_detailed` | ✅ Gates, confidence, multi-source |
| FOK → GTC | `send_fok_exhausted` | ✅ Shows reason + fallback |
| Fill | `send_order_filled` | ✅ FOK step info |
| Outcome | `send_outcome_with_analysis` | ✅ Session totals, oracle direction |
| Resolution | `send_window_resolution` | ✅ v8.0 format |
| Session Summary | `send_session_summary` | ✅ New method |
| Skip | via `send_trade_decision_detailed` | ✅ Shows failed gate |

---

## Data Integrity Issues (Pre-v8.0)

From the live data audit earlier today:

| Issue | Count | Cause |
|-------|-------|-------|
| Orphaned trades (trade exists, window says skip) | 803 (74%) | Old bump retry creating duplicate orders |
| Zero CLOB order IDs | All trades | Order IDs not recorded in old path |
| `trade_placed=FALSE` on all windows | 16/16 today | Snapshot written before execution, never updated |
| UP trades stuck as OPEN | 26 | No CLOB liquidity for UP tokens, fill poll gave up |
| Dual orders per window | Common | Old GTC + bump retry = 2 orders |

**v8.0 fixes going forward:**
- FOK fills recorded instantly (no poll needed)
- GTC fallback has 60s poll (no bump retry creating duplicates)
- `trade_placed` should be updated after successful order placement

**Needs migration:** Historical data before v8.0 should be marked as legacy or reconciled.

---

## CLOB Liquidity Reality

The 5-min UP/DOWN token CLOB books are **essentially empty most of the time**:

```
Typical book: dn_ask=$0.99 dn_bid=$0.01 (99¢ spread)
Sometimes:    dn_ask=—     dn_bid=$0.01 (no asks at all)
Rare:         dn_ask=$0.49 dn_bid=$0.46 (real liquidity)
```

**Implication:** FOK will almost always abort → GTC fallback at Gamma price is the real execution path. FOK becomes valuable only when real liquidity appears (e.g., near window close, high-volume markets).

**Recommendation:** Monitor FOK fill rate over 24h. If FOK never fills, consider making GTC the primary path with FOK as an opportunistic upgrade.

---

## v8.1 Backlog

1. **DECISIVE early entry (T-120/T-180)** — multi-step evaluation pipeline with conviction re-checks
2. **trade_placed flag fix** — update window_snapshot after order placed
3. **Dead code cleanup** — remove _execute_from_signal, duplicate imports, trim TWAP/TimesFM code
4. **Historical data migration** — reconcile orphaned trades, mark pre-v8.0 as legacy
5. **FOK fill rate monitoring** — if <5% FOK fill rate, swap primary to GTC
6. **Activity API reconciliation** — cross-check CLOB matched orders vs DB
