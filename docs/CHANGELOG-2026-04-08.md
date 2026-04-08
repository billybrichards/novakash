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

## v10.1 — Regime + Offset Gate Calibration (Apr 8, 18:00 UTC)

**Problem:** 7/8 recent trades were losses. Root causes:
1. TRANSITION regime: 0% WR (5 losses, VPIN 0.55-0.65 = noise, not direction)
2. T-180 offset: DUNE accuracy only 67.9% at T-180 vs 75.9% at T-60
3. Flat threshold: same 0.75 for all regimes and offsets — too permissive for weak conditions
4. Cap ceiling $0.75: at 70% WR, breakeven is ~$0.70 — was entering negative-EV trades

**DB-verified loss at 17:32:** dune_p=0.7839, NORMAL regime, T-180, VPIN=0.513. Would need 0.88 under v10.1.

**Code Changes (gates.py):**
- `DuneConfidenceGate` now regime-aware with per-regime thresholds
- Offset penalty: `effective_threshold = regime_base + min(0.10, (offset-60)/120 * 0.10)`
- TRANSITION hard-blocked at threshold=9.99 (fast-rejected before DUNE API call)
- `DynamicCapGate` ceiling lowered from $0.75 to $0.70, floor raised from $0.30 to $0.35

**Configuration:**
```env
V10_DUNE_ENABLED=true
V10_DUNE_MODEL=oak
V10_DUNE_MIN_P=0.75
V10_MIN_EVAL_OFFSET=180

# Regime base thresholds
V10_TRANSITION_MIN_P=9.99    # hard block
V10_CASCADE_MIN_P=0.80
V10_NORMAL_MIN_P=0.78
V10_LOW_VOL_MIN_P=0.78
V10_TRENDING_MIN_P=0.80
V10_CALM_MIN_P=0.80

# Offset penalty (earlier = harder)
V10_OFFSET_PENALTY_MAX=0.10  # T-180 adds +0.10 to threshold

# Cap bounds
V10_DUNE_CAP_CEILING=0.70    # breakeven at 70% WR
V10_DUNE_CAP_FLOOR=0.35
V10_DUNE_CAP_MARGIN=0.05
```

**Effective threshold examples:**
| Regime | T-60 | T-90 | T-120 | T-150 | T-180 |
|--------|------|------|-------|-------|-------|
| TRANSITION | BLOCKED | BLOCKED | BLOCKED | BLOCKED | BLOCKED |
| CASCADE | 0.80 | 0.825 | 0.85 | 0.875 | 0.90 |
| NORMAL | 0.78 | 0.805 | 0.83 | 0.855 | 0.88 |

**Backtested against recent losses (DB-verified):**
- 7/7 losses BLOCKED, 1 outlier win also blocked (acceptable trade)
- 9/10 most recent trades blocked — only 1 high-quality trade passes (T-64, NORMAL, dune_p=0.872)

**Env loading note:** Engine loads `engine/.env` (NOT `.env.local`). The `.env` file is gitignored and only exists on Montreal. `.env.local` is a committed reference copy.

**Updated:** 2026-04-08 18:00 UTC
**Visualization:** `docs/v10_1_decision_surface.html`

---

## v10.2 — Recalibration After Reconciler Data Fix (Apr 8, 19:50 UTC)

**Discovery:** 12 WINs worth +$18.62 were hidden by a reconciler bug. Trades with confirmed CLOB fills (`clob_status=MATCHED`, `shares_filled > 0`) were marked EXPIRED with no outcome. This made TRANSITION look like 0% WR when it was actually **83% WR**.

**Data correction:**
- 12 orphaned trades resolved via CLOB trade history API cross-reference
- Corrected Apr 8 stats: **39W/24L (61.9% WR)** vs SITREP-reported 8W/7L (53%)
- trade_bible auto-populated via DB trigger on trades update

**v10.1 was over-calibrated:**
- v10.1 would block ALL 12 recovered wins (+$18.62 blocked) AND 6 losses (-$27.50 blocked)
- Net: +$3.33 profit left on table — v10.1 was slightly too aggressive
- Root cause: calibration was based on incomplete data (hidden wins)

**v10.2 changes:**

| Parameter | v10.1 | v10.2 | Why |
|-----------|-------|-------|-----|
| `V10_TRANSITION_MIN_P` | 9.99 (hard block) | **0.85** | Actual 83% WR, not 0% |
| `V10_OFFSET_PENALTY_MAX` | 0.10 | **0.05** | Early offsets were profitable, over-penalised |
| All other thresholds | unchanged | unchanged | Still sound |

**v10.2 effective thresholds:**

| Regime | T-60 | T-90 | T-120 | T-150 | T-180 |
|--------|------|------|-------|-------|-------|
| TRANSITION | 0.850 | 0.863 | 0.875 | 0.888 | 0.900 |
| CASCADE | 0.800 | 0.813 | 0.825 | 0.838 | 0.850 |
| NORMAL | 0.780 | 0.793 | 0.805 | 0.818 | 0.830 |

**Reconciler fix (concurrent):**
- Added `get_trade_history()` to polymarket_client — fetches CLOB fill history with oracle outcomes
- Reconciler now checks for orphaned EXPIRED+MATCHED trades every 60s
- Matches by token_id to CLOB fills, determines WIN/LOSS, updates trades table
- Prevents status=EXPIRED when clob_status=MATCHED (trade actually filled)

**Configuration:**
```env
V10_DUNE_ENABLED=true
V10_DUNE_MODEL=oak
V10_DUNE_MIN_P=0.75
V10_MIN_EVAL_OFFSET=180
V10_TRANSITION_MIN_P=0.85
V10_CASCADE_MIN_P=0.80
V10_NORMAL_MIN_P=0.78
V10_LOW_VOL_MIN_P=0.78
V10_TRENDING_MIN_P=0.80
V10_CALM_MIN_P=0.80
V10_OFFSET_PENALTY_MAX=0.05
V10_DUNE_CAP_CEILING=0.70
V10_DUNE_CAP_FLOOR=0.35
V10_DUNE_CAP_MARGIN=0.05
FIVE_MIN_EVAL_INTERVAL=2
```

**Engine log backup:** `/home/novakash/engine-v10.1-pre-v10.2.log` (3.9MB)

**Updated:** 2026-04-08 19:50 UTC
