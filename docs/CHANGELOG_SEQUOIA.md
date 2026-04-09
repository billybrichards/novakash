# SEQUOIA v4 Deployment — Apr 9, 2026

## What Changed

### Model: SEQUOIA (11191d7) replaces OAK/ELM
- **Architecture:** LightGBM with 65 features (was 37), `num_leaves=31` (was 15)
- **Calibration:** Temperature scaling T~1.0 — smooth probabilities, no bimodal extremes
- **New features:** session_bucket, VPIN dynamics (slope, range, std), price volatility, gamma implied probability, source divergence, CG flow momentum
- **Training data:** 9,797 windows, 3,036 with Binance coverage, walk-forward split
- **Test accuracy:** 81.7% at Δ=30s, 77.0% at Δ=60s, 73.7% at Δ=90s, 71.9% at Δ=120s

### Gate Thresholds: Recalibrated for SEQUOIA output range
| Parameter | Old (OAK) | New (SEQUOIA) | Why |
|-----------|-----------|---------------|-----|
| `V10_DUNE_MIN_P` | 0.65 | 0.60 | SEQUOIA outputs lower absolute values |
| `V10_TRANSITION_MIN_P` | 0.75 | 0.70 | Was unreachable at Δ≥120s |
| `V10_CASCADE_MIN_P` | 0.80 | 0.67 | Same issue |
| `V10_NORMAL_MIN_P` | 0.65 | 0.60 | SEQUOIA p10=0.65 at Δ=120 |
| `V10_LOW_VOL_MIN_P` | 0.65 | 0.60 | Match NORMAL |
| `V10_TRENDING_MIN_P` | 0.72 | 0.67 | Match CASCADE |
| `V10_OFFSET_PENALTY_MAX` | 0.06 | 0.04 | Old penalty exceeded SEQUOIA max |
| `V10_DOWN_PENALTY` | 0.05 | 0.03 | SEQUOIA has direction in features |

### v10.5 Gates: Carried forward
- DeltaMagnitudeGate: `V10_MIN_DELTA_PCT=0.005`, `V10_TRANSITION_MIN_DELTA=0.010`
- Bet sizing: `BET_FRACTION=0.050`, `ABSOLUTE_MAX_BET=6.0`, `STARTING_BANKROLL=63`

## Deployment Sequence
1. SEQUOIA trained locally from Railway PostgreSQL data
2. Artifacts uploaded to S3 (`bbrnovakash-models-do-not-delete/v2/*/current.json`)
3. TimesFM Docker container restarted on 3.98.114.0 — loaded SEQUOIA for BTC/ETH/SOL/XRP
4. Engine `.env` updated on Montreal 15.223.247.178 with new thresholds
5. Engine restarted — first SEQUOIA trade executed at 13:24 UTC

## Key Improvement: Confidence Now Correlates with Accuracy
Old (OAK/ELM): >85% confidence = 23.7% WR (anti-correlated!)
New (SEQUOIA at Δ=60s):
- 80-90% conf → 86.2% WR
- 70-80% conf → 72.8% WR
- 60-70% conf → 51.2% WR
- 50-60% conf → 44.4% WR

## First Trade
```
13:24:02 UTC — dune.evaluated: dune_p=0.8379, threshold=0.633, NORMAL T-100
  All 7 gates passed. Direction=DOWN, stake=$3.15, token=$0.645
```

## Files Modified
- `engine/.env.local` — SEQUOIA threshold reference
- `engine/.env` (Montreal) — Live runtime config
- `docs/superpowers/specs/2026-04-09-elm-v4-proposal.md` — Full analysis
- TimesFM repo: `training/queries.py`, `training/build_dataset.py`, `training/train_lgb.py`, `app/v2_scorer.py`, `app/v2_routes.py`

## Rollback
If SEQUOIA underperforms:
1. SSH to TimesFM host (3.98.114.0): restore OAK via S3 `current.json` revert
2. SSH to Montreal: `cp engine/.env.backup.pre-sequoia engine/.env` and restart

---

## v10.6 — Confidence-Scaled Pricing + T-200 Entry Window (Apr 9, ~15:00 UTC)

### Confidence-Scaled Entry Cap (DynamicCapGate)
**Problem:** Flat $0.68 cap on every trade regardless of confidence. A 0.84 and 0.73 trade pay the same price.

**Solution:** Linear interpolation: `cap = base + (ceiling - base) × (conf - min) / (max - min)`

| Confidence | Entry Cap | Breakeven WR | SEQUOIA Actual WR |
|-----------|----------|-------------|-------------------|
| 0.65 | $0.48 | ~48% | ~52% (marginal) |
| 0.70 | $0.53 | ~53% | ~61% |
| 0.75 | $0.58 | ~58% | ~73% |
| 0.80 | $0.64 | ~64% | ~78% |
| 0.84 | $0.68 | ~68% | ~86% |
| 0.88 | $0.72 | ~72% | ~86% |

**Impact:** At $0.58 entry, WIN = +$2.30 (was +$1.60). Losses unchanged at -$3.40. Recovery ratio drops from 2.1 wins per loss to 1.5.

### T-200 Entry Window
**Was:** V10_MIN_EVAL_OFFSET=180 (3 min before close)
**Now:** V10_MIN_EVAL_OFFSET=200 (3 min 20s before close)

**Safeguards for T-180..200:**
- **Hard confidence gate:** dir_conf ≥ 0.90 required (V10_EARLY_ENTRY_MIN_CONF)
- **Hard cap override:** max $0.63 entry (V10_EARLY_ENTRY_CAP_MAX)
- **Steeper offset penalty:** +0.04 additional (V10_OFFSET_PENALTY_EARLY_MAX)

Effective thresholds at T-200:
- NORMAL: 0.60 + 0.08 = 0.68 (passable, but also needs 0.90 conf gate)
- TRANSITION: 0.70 + 0.08 = 0.78 (demanding + 0.90 conf gate)

### New Env Vars
```
V10_CAP_SCALE_BASE=0.48          # cap at minimum confidence
V10_CAP_SCALE_CEILING=0.72       # cap at maximum confidence
V10_CAP_SCALE_MIN_CONF=0.65      # bottom of SEQUOIA range
V10_CAP_SCALE_MAX_CONF=0.88      # top of SEQUOIA range
V10_OFFSET_PENALTY_EARLY_MAX=0.04 # additional penalty T-180..200
V10_EARLY_ENTRY_MIN_CONF=0.90    # hard min confidence for T-180..200
V10_EARLY_ENTRY_CAP_MAX=0.63     # hard max cap for T-180..200
V10_EARLY_ENTRY_OFFSET=180       # where early zone starts
V10_MIN_EVAL_OFFSET=200          # global max (was 180)
```

### Files Modified
- `engine/signals/gates.py` — DynamicCapGate: confidence-scaled formula. DuneConfidenceGate: two-tier penalty + early conf gate.
- `engine/.env.local` — New config params

### Rollback
Set `V10_CAP_SCALE_BASE=0.68` + `V10_CAP_SCALE_CEILING=0.68` for flat cap. Set `V10_MIN_EVAL_OFFSET=180` + `V10_OFFSET_PENALTY_EARLY_MAX=0.0` + `V10_EARLY_ENTRY_MIN_CONF=0.0` for old window.
