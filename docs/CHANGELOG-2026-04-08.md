# Changelog — 8 April 2026

---

## v10.0 — DUNE-Gated Dynamic Pricing + Architecture Cleanup

### Summary

Major overhaul: replaced v9's inline VPIN gates with a clean, composable ML-driven gate pipeline powered by the DUNE (CEDAR) model. Deleted 619 lines of dead code. Added 2s continuous polling. Created `trade_bible` source-of-truth table.

### Core Changes

| Component | v9.0 | v10.0 |
|-----------|------|-------|
| Gate system | Inline code (~200 lines in strategy) | **`signals/gates.py`** — 4 composable gate classes |
| ML model | OAK (binary 0/1 output) | **DUNE/CEDAR** (continuous 0.04-0.92, 11 calibration levels) |
| Confidence gate | VPIN >= 0.45 (golden) | **DUNE P(direction) >= 0.65** |
| Entry cap | Fixed $0.65 | **Dynamic: DUNE_P - 5pp** ($0.30-$0.75 range) |
| Eval interval | 10s (19 offsets) | **2s (91 offsets)** |
| Delta floor | 0.02% minimum | **Removed** — DUNE trained on oracle outcomes handles noise |
| TWAP override | Removed (v10 cleanup) | Removed |
| TimesFM gate | Removed (47.8% accuracy) | Removed |
| Dead code | 619 lines present | **Deleted** |

### v10 Gate Pipeline

```
G1: Source Agreement (CL+TI must agree) — HARD GATE, kept from v9
G2: DUNE Confidence (P(direction) >= 0.65) — NEW, replaces VPIN
G3: CoinGlass Veto (3+ opposing signals) — kept from v9
G4: Dynamic Cap (DUNE_P - 5pp margin) — NEW, replaces fixed tiers
```

Each gate is a class in `engine/signals/gates.py`:
- `SourceAgreementGate` — evaluates CL vs TI delta direction
- `DuneConfidenceGate` — queries DUNE API, checks P(direction) threshold
- `CoinGlassVetoGate` — checks 5 micro-structure opposing signals
- `DynamicCapGate` — computes cap from DUNE probability

### DUNE Model Accuracy (Test Set)

| Offset | DUNE | OAK | Improvement |
|--------|------|-----|-------------|
| T-30 | 83.5% | 77.7% | +5.8pp |
| T-60 | 75.9% | 69.6% | +6.3pp |
| T-90 | 73.2% | 61.7% | +11.5pp |
| T-120 | 70.6% | 59.4% | +11.2pp |
| T-180 | 67.9% | 58.3% | +9.6pp |

### Architecture Cleanup

**Deleted (619 lines):**
- `_execute_from_signal()` (423 lines) — dead continuous evaluator, never called
- `place_market_order_legacy()` (47 lines) — duplicate method
- TWAP Gamma Gate (35 lines) — feature-flagged OFF since v8.0
- TWAP Direction Override (56 lines) — blocked 8 winners, net harmful
- TWAP Confidence Adjustment (36 lines) — feature-flagged OFF
- TimesFM Agreement Check (76 lines) — 47.8% accuracy, worse than coin flip

**New files:**
- `engine/signals/gates.py` — Clean gate system (4 gates + pipeline + dataclasses)

**Modified:**
- `engine/signals/timesfm_v2_client.py` — Added `model='cedar'` param for DUNE
- `engine/config/constants.py` — Dynamic offset generation from `FIVE_MIN_EVAL_INTERVAL`
- `engine/config/runtime_config.py` — v10 config vars
- `engine/strategies/five_min_vpin.py` — v10 pipeline integration + dead code removal

### Database

**New table: `trade_bible`** — Definitive source of truth for all trade outcomes.

```sql
-- Query the bible
SELECT * FROM trade_bible WHERE is_live = true ORDER BY resolved_at DESC;

-- By config
SELECT config_version, eval_tier, count(*),
       count(*) FILTER (WHERE trade_outcome='WIN') as wins,
       ROUND(SUM(pnl_usd)::numeric, 2) as pnl
FROM trade_bible WHERE is_live = true
GROUP BY config_version, eval_tier;
```

### Environment Variables

```env
# v10 (enable for DUNE-gated pipeline)
V10_DUNE_ENABLED=true
V10_DUNE_MIN_P=0.65
V10_DUNE_CAP_MARGIN=0.05
V10_DUNE_CAP_FLOOR=0.30
V10_DUNE_CAP_CEILING=0.75
FIVE_MIN_EVAL_INTERVAL=2

# Kept from v9
V9_SOURCE_AGREEMENT=true
ORDER_TYPE=FAK
```

### Rollback

```env
V10_DUNE_ENABLED=false
FIVE_MIN_EVAL_INTERVAL=10
```

Falls back to v9 golden-zone-only with 10s polling. Zero behavior change.

### Bug Fixes from v9 Session

- position_monitor now links resolutions to trades table
- FAK "no orders found to match" logged as info, not error
- FAK precision: 2dp price in size calc
- Pi bonus threshold unified with effective max price
- SITREP shows recent 3 wins + 3 losses
- SITREP recent skips deduplicated by window_ts
- `_snap_regime` NameError fixed
- Early-zone VPIN skip logic fixed
- Gate audit boolean comparison fixed (`== "PASS"` → `bool()`)

### v9 Corrected Results (from trade_bible)

| Tier | W | L | WR | PnL |
|------|---|---|------|-----|
| v9 GOLDEN | 16 | 5 | 76% | +$21.30 |
| v9 EARLY CASCADE | 4 | 10 | 29% | -$41.71 |
| v8 old | 7 | 4 | 64% | -$4.78 |

### Files Modified

| File | Lines | Change |
|------|-------|--------|
| `five_min_vpin.py` | 3,254 → 2,693 | -561 lines, v10 pipeline wired |
| `polymarket_client.py` | 1,245 → 1,201 | -44 lines, legacy removed |
| `fok_ladder.py` | 354 → 228 | Rewritten as 2-shot |
| `gates.py` | NEW | 290 lines, clean gate system |
| `timesfm_v2_client.py` | +16 lines | DUNE/cedar support |
| `constants.py` | Updated | Dynamic offset generation |
| `runtime_config.py` | Updated | v10 env vars |

---

**Last Updated:** 2026-04-08 12:15 UTC
**Version:** v10.0 (DUNE-gated dynamic pricing)

---

## v10.1 - Gate Calibration (Apr 8, 17:20 UTC)

---

## v10.1 - Gate Calibration (Apr 8, 17:20 UTC)

**Problem:** 5/6 v10 losses were TRANSITION regime. Threshold 0.85 too high for volume.

**Changes:**
- V10_DUNE_MIN_P: 0.85 -> 0.75
- V10_TRANSITION_ENABLED: true -> false
- V10_CASCADE_MIN_P: 0.80 (new)
- V10_LOW_VOL_MIN_P: 0.80 (new)
- V10_TRENDING_MIN_P: 0.82 (new)

**Expected:** 3-5 trades/day at 70-80% WR vs 0-1 trades at 14% WR

**Updated:** 2026-04-08 17:20 UTC
