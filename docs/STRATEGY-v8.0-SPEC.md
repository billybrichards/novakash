# Strategy v8.0 — Full Specification

**Date:** April 6, 2026
**Status:** PROPOSED — awaiting Billy's review before implementation
**Supersedes:** v7.1 (live since Apr 5)

---

## Summary of Changes from v7.1

| Component | v7.1 (current) | v8.0 (proposed) |
|---|---|---|
| **Delta source** | Binance BTC/USDT klines (71.6% accuracy) | Tiingo multi-exchange candles (96.9% accuracy) |
| **Entry pricing** | Gamma `outcomePrices` (stale, sometimes None) | CLOB orderbook direct query (real-time) |
| **Execution** | Single GTC/GTD, 30s poll, +2¢ bump once | FOK ladder: 5 attempts every 2s, escalating to cap |
| **Fill detection** | 30s poll (misses YES fills) | 60s poll + 3s first check (deployed in v7.2 fix) |
| **Floor check** | Bypassed when Gamma returns None | Skip trade if no price available (deployed in v7.2 fix) |
| **Macro awareness** | None | Macro observer service reads macro_signals table |
| **Oracle divergence** | Not tracked | Chainlink-Binance spread gates DOWN bets |
| **TWAP override** | Active — flips direction based on TWAP+Gamma agreement | **Removed** — Tiingo delta is already oracle-aligned |
| **CG veto threshold** | 3+ signals | 2+ signals (tightened in v7.1, keeping) |
| **TimesFM** | Collecting data, not gating | **Disabled entirely** — 47.8% accuracy, worse than coin flip |
| **Confidence tiers** | NONE/LOW/MODERATE/HIGH | Same + DECISIVE tier for early entry |

---

## Signal Flow (v8.0)

```
EVERY 5 MINUTES:

┌─ 1. DATA COLLECTION ────────────────────────────────────────┐
│                                                              │
│  Tiingo 5m candle:  open + close → delta direction           │
│  Chainlink price:   oracle-proxy → divergence risk calc      │
│  Binance kline:     VPIN calculation only (not direction)    │
│  CoinGlass:         OI, funding, L/S, taker → veto system   │
│  CLOB book:         real best ask/bid → entry pricing        │
│  Macro observer:    latest macro_signals row → bias/gate     │
└──────────────────────────────────────────────────────────────┘
              │
              ▼
┌─ 2. DIRECTION DECISION ─────────────────────────────────────┐
│                                                              │
│  Tiingo delta > 0 → UP signal                               │
│  Tiingo delta < 0 → DOWN signal                             │
│                                                              │
│  NO Binance delta for direction (was 71.6%, now irrelevant)  │
│  NO TWAP override (Tiingo IS the multi-exchange average)     │
│  NO TimesFM gating (47.8% = coin flip)                      │
└──────────────────────────────────────────────────────────────┘
              │
              ▼
┌─ 3. GATE CHAIN (any gate fails → SKIP) ────────────────────┐
│                                                              │
│  Gate 1: VPIN ≥ 0.45                                        │
│    → Below = no informed flow, skip                          │
│    → Same as v7.1                                            │
│                                                              │
│  Gate 2: Delta magnitude ≥ threshold (regime-scaled)         │
│    → CASCADE (VPIN ≥ 0.65): dynamic floor based on VPIN     │
│      VPIN 0.65-0.75: 0.01%                                  │
│      VPIN 0.75-0.85: 0.005%                                 │
│      VPIN 0.85+: near-zero (mega cascade)                   │
│    → TRANSITION (0.55-0.65): 0.02%                          │
│    → NORMAL (< 0.55): 0.02%                                 │
│    → NOTE: these thresholds may need recalibration for       │
│      Tiingo deltas (may be systematically different from     │
│      Binance deltas). Run 48h comparison first.              │
│                                                              │
│  Gate 3: CoinGlass veto (≥ 2 opposing signals)              │
│    → Smart money opposing (>52% other side)                  │
│    → Funding opposing (annualised rate threshold)            │
│    → Crowd overleveraged opposing (>60%)                     │
│    → Taker flow opposing (>60%)                              │
│    → CASCADE + taker divergence (VPIN≥0.65, taker >55%)     │
│    → Same as v7.1                                            │
│                                                              │
│  Gate 4: Macro observer gate (NEW)                           │
│    → Read latest macro_signals row from DB                   │
│    → If direction_gate = SKIP_DOWN and signal = DOWN → skip  │
│    → If direction_gate = SKIP_UP and signal = UP → skip      │
│    → Apply threshold_modifier to delta thresholds            │
│    → If override_active: apply size_modifier to stake        │
│                                                              │
│  Gate 5: Oracle divergence gate (NEW)                        │
│    → Chainlink price - Binance price = spread                │
│    → Spread > $30 for DOWN bets: raise delta threshold 2x   │
│    → Spread > $50 for DOWN bets: skip entirely              │
│    → Spread < -$30 for UP bets: same logic (reversed)        │
│                                                              │
│  Gate 6: Floor check (v7.2 fix deployed)                     │
│    → If no price source available → skip                     │
│    → If CLOB best ask < $0.30 → skip                        │
│    → If CLOB best ask > cap → skip                           │
│                                                              │
│  REMOVED GATES:                                              │
│    ✖ TWAP override — Tiingo already multi-exchange           │
│    ✖ TWAP Gamma gate — replaced by CLOB book check          │
│    ✖ TimesFM agreement — coin flip, disabled                 │
└──────────────────────────────────────────────────────────────┘
              │
              ▼
┌─ 4. CONFIDENCE TIER ────────────────────────────────────────┐
│                                                              │
│  Same VPIN × delta formula as v7.1, plus:                    │
│                                                              │
│  NONE:      blocked                                          │
│  LOW:       blocked                                          │
│  MODERATE:  standard execution at T-70                       │
│  HIGH:      standard execution at T-70                       │
│  DECISIVE:  NEW — early entry eligible at T-120/T-180        │
│    → Requires: VPIN ≥ 0.75, delta > 2x threshold,           │
│      CG veto = 0, macro confidence ≥ 70%                    │
│    → Benefit: tokens are cheaper earlier in window           │
│                                                              │
│  CG modifier: OI delta >0.10% still boosts LOW→MODERATE     │
│  (only CG signal backed by 30-day data)                      │
└──────────────────────────────────────────────────────────────┘
              │
              ▼
┌─ 5. FOK EXECUTION LADDER ───────────────────────────────────┐
│                                                              │
│  Signal committed. Now pure execution.                       │
│                                                              │
│  Step 1 (T-70 or T-120 if DECISIVE):                        │
│    → Query CLOB book for target token                        │
│    → FOK at best ask                                         │
│    → Filled? → done ✅                                       │
│                                                              │
│  Step 2 (+2s):                                               │
│    → Fresh CLOB book query                                   │
│    → FOK at best ask + 1¢                                    │
│    → Filled? → done ✅                                       │
│                                                              │
│  Step 3 (+2s):                                               │
│    → FOK at best ask + 2¢                                    │
│                                                              │
│  Step 4 (+2s):                                               │
│    → FOK at best ask + 3¢                                    │
│                                                              │
│  Step 5 (+2s):                                               │
│    → FOK at cap price ($0.73)                                │
│    → Last attempt — filled or accept miss                    │
│                                                              │
│  Total time: ~10 seconds                                     │
│  Only price check: still under cap?                          │
│  No signal re-evaluation during ladder                       │
│                                                              │
│  Fill recorded from CLOB response (actual_fill_price)        │
│  If no fill after 5 attempts → accept miss, log reason       │
└──────────────────────────────────────────────────────────────┘
              │
              ▼
┌─ 6. POST-TRADE ─────────────────────────────────────────────┐
│                                                              │
│  Fill detection: 60s poll, first check at 3s (v7.2 fix)     │
│  Record: actual fill price, shares, CLOB order ID            │
│  Telegram: fill notification + AI analysis                   │
│  DB: write to trades + update window_snapshots               │
│  Macro signal logged: macro_bias, macro_confidence           │
│  Resolution: Polymarket oracle only (v7.1, correct)          │
│  Redemption: Builder Relayer auto-redeem                     │
└──────────────────────────────────────────────────────────────┘
```

---

## Environment Variables (v8.0)

### Changed from v7.1

| Variable | v7.1 value | v8.0 value | Reason |
|---|---|---|---|
| `DELTA_PRICE_SOURCE` | N/A (hardcoded Binance) | `tiingo` | 96.9% vs 71.6% accuracy |
| `ORDER_PRICING_MODE` | `bestask` (Gamma) | `clob` | Real-time CLOB book |
| `FOK_ENABLED` | N/A | `true` | FOK ladder execution |
| `FOK_ATTEMPTS` | N/A | `5` | 5 attempts over 10s |
| `FOK_INTERVAL_S` | N/A | `2` | 2s between attempts |
| `FOK_PRICE_CAP` | `0.73` | `0.73` | Keep current cap |
| `TWAP_OVERRIDE_ENABLED` | implicit `true` | `false` | Tiingo replaces TWAP |
| `TIMESFM_ENABLED` | `true` | `false` | Coin flip, disable |
| `FILL_POLL_MAX_WAIT` | `30` | `60` | Catch YES fills (v7.2) |
| `MACRO_OBSERVER_ENABLED` | N/A | `true` | Read macro_signals DB |
| `ORACLE_DIVERGENCE_GATE` | N/A | `true` | Chainlink spread check |

### Unchanged from v7.1

| Variable | Value | Notes |
|---|---|---|
| `FIVE_MIN_VPIN_GATE` | `0.4500` | Core gate, well-calibrated |
| `FIVE_MIN_MIN_DELTA_PCT` | `0.0200` | May need Tiingo recalibration |
| `FIVE_MIN_CASCADE_MIN_DELTA_PCT` | `0.0100` | May need Tiingo recalibration |
| `VPIN_CASCADE_DIRECTION_THRESHOLD` | `0.6500` | Regime boundary |
| `VPIN_INFORMED_THRESHOLD` | `0.5500` | Regime boundary |
| `BET_FRACTION` | `0.10` | Conservative |
| `ABSOLUTE_MAX_BET` | `5.0` | Safety cap |
| `MAX_ORDERS_PER_HOUR` | `20` | Rate limit |
| `FIVE_MIN_MAX_ENTRY_PRICE` | `0.73` | Cap |

---

## Implementation Phases

### Phase 1: Delta source swap (highest impact, lowest risk)
- Add Tiingo REST API call to `five_min_vpin.py` for 5m candle at window open/close
- Fall back to Binance if Tiingo unavailable
- Feature flag: `DELTA_PRICE_SOURCE=tiingo|binance|chainlink`
- **Expected WR improvement: +25pp (71.6% → 96.9%)**
- **Risk: LOW** — additive, Binance fallback preserved
- ⚠️ Recalibrate delta thresholds for Tiingo (deltas may be slightly different magnitude)

### Phase 2: FOK ladder execution
- New `FOKLadder` class in `engine/execution/fok_ladder.py`
- CLOB book query at each step
- Replace GTC submission in `five_min_vpin.py`
- Feature flag: `FOK_ENABLED=true|false`
- **Expected fill rate improvement: 40% → 90%+**
- **Risk: MEDIUM** — new execution path, test in paper first

### Phase 3: Macro observer wiring
- Read `macro_signals` table in orchestrator
- Apply gate + threshold modifiers in `five_min_vpin.py`
- Feature flag: `MACRO_OBSERVER_ENABLED=true|false`
- **Expected improvement: ~$15-20/day from blocked wrong-direction bets**
- **Risk: LOW** — purely additive gating

### Phase 4: TWAP removal + cleanup
- Remove TWAP override code path
- Remove TimesFM gating
- Simplify signal evaluation
- **Risk: LOW** — removing dead/harmful code paths

---

## Risk Assessment

| Risk | Mitigation |
|---|---|
| Tiingo API goes down | Fall back to Binance (automatic) |
| CLOB book empty / no asks | Fall back to Gamma pricing |
| FOK fills at bad price | Cap still enforced ($0.73 max) |
| Macro observer disagrees with good signal | Macro only blocks, never forces trades |
| Chainlink divergence gate too aggressive | Configurable threshold ($30 default) |
| Tiingo delta magnitudes differ from Binance | Run 48h comparison, recalibrate thresholds |

---

## Expected P&L Impact (based on today's data)

| Change | Expected impact |
|---|---|
| Tiingo delta (71.6% → 96.9% WR) | +$50-80/day |
| FOK ladder (40% → 90% fill rate) | +$40-80/day from unfilled winners |
| Macro observer | +$15-20/day from blocked losses |
| Floor fix (deployed) | Prevents -$10 incidents |
| Fill detection fix (deployed) | Correct P&L tracking |
| **Combined** | **+$100-180/day estimated** |

---

## What Gets Removed

1. **TWAP direction override** — Tiingo IS the multi-exchange average. TWAP override was trying to fix the Binance mismatch problem by using a smoothed price. Tiingo solves it at the source.

2. **TimesFM gating** — 47.8% accuracy, statistically worse than coin flip. Data collection continues for future analysis but no trading decisions use it.

3. **Gamma `outcomePrices` for entry pricing** — Replaced by direct CLOB book queries. Gamma was stale and sometimes returned None (causing the floor bypass bug).

4. **Single GTC/GTD orders** — Replaced by FOK ladder. GTC sat unfilled for 30s then expired. FOK attempts fill immediately or moves on.

---

## Monitoring Checklist (first 24h after deploy)

- [ ] Tiingo delta matches oracle direction on resolved windows (target: >90%)
- [ ] FOK fill rate on first attempt (target: >60%)
- [ ] FOK fill rate across all 5 attempts (target: >85%)
- [ ] Macro observer signal correlates with outcomes (track for 48h)
- [ ] No floor bypass incidents (should be zero with v7.2 fix)
- [ ] YES/UP orders now show as FILLED in DB (v7.2 fix)
- [ ] Delta thresholds not causing too many/few trades (compare window counts vs v7.1)
- [ ] CLOB book queries not rate-limited (5 queries per window × every 5 min = 1/min)
