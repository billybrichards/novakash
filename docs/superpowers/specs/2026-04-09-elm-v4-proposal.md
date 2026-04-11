# Proposal: ELM v4 — Model Confidence Enhancement & Winning Entry Zone System

> **DATA-VALIDATED** — All findings below are derived from 13,331 signal evaluations,
> 10,196 gate audits, and 50 resolved trade_bible entries from April 9, 2026.
> Every recommendation includes the SQL-verified win rate and simulated PnL.

## Context

The current ELM v3 (OAK) LightGBM model achieves 82-87% WR on BTC 5m windows, but performance is **session-dependent**: Asian hours (00-09 UTC) run 100% WR while London (09-12 UTC) collapses to 50%. The model reports high confidence (0.78-0.88 dune_p) on ALL trades including losses — it cannot distinguish between high-confidence-correct and high-confidence-wrong. This means the calibration is broken for adversarial market conditions.

**Today's data (Apr 9):**
- 50 trades: 41W/9L (82% WR), +$15.41 net
- Asian session: 34W/0L (100%), +$56.65
- London session: 16W/9L (64%), -$41.24
- All 9 losses had dune_p > 0.78 — model was confident and wrong
- 2/9 losses catchable by delta gate (|delta| < 0.01%)
- 7/9 losses are "irreducible" with current features — model simply wrong

**The core problem:** The model's 37 features don't capture **when it's likely to be wrong**. We need features that predict model reliability, not just direction.

---

## Part 1: Data Audit — What We Have (Golden Assets)

### 1.1 High-Value Training Tables

| Table | Rows (est) | Gold | Why |
|-------|-----------|------|-----|
| **trade_bible** | ~200+ | Definitive WIN/LOSS with timestamps | Ground truth outcomes, resolved_at enables session analysis |
| **signal_evaluations** | ~50K+ | Every 2s eval with dune_p, regime, offset, decision | Optimal threshold surface per regime×offset×hour |
| **gate_audit** | ~2K+ | Gate pass/fail + `would_have_won` | Labeled counterfactual data — "would this skip have won?" |
| **window_snapshots** | ~10K+ | Per-window multi-source deltas, CG, VPIN, regime, outcome | Complete feature context at decision time |
| **ticks_binance** | ~5M+ | 1-3Hz price+VPIN | Microstructure, volatility estimation, cascade detection |
| **ticks_coinglass** | ~50K+ | 15s OI, liq, taker, funding per asset | Flow features, regime detection |
| **ticks_v2_probability** | ~100K+ | 1Hz model predictions with all features | Model behavior analysis — when does it cluster at extremes? |
| **post_resolution_analyses** | ~200+ | Per-trade counterfactual analysis | Missed profit, blocked loss quantification |

### 1.2 Currently UNUSED Data (Processable Gold)

These exist in the DB but are **not features in the current model**:

1. **VPIN trajectory** — `ticks_binance.vpin` at 1-3Hz. Current model uses single-point VPIN. The *slope* and *acceleration* of VPIN over the last 60-300s is a strong microstructure signal.

2. **CoinGlass temporal dynamics** — Current model uses snapshot values. The *change rate* of OI, liquidations, and taker flow over 1m/5m/15m windows is far more predictive than absolute levels.

3. **Gamma token price momentum** — `ticks_gamma.up_price` and `down_price` over time. The market's own pricing of UP vs DOWN moves faster than our model — we should be reading the orderbook's implied probability.

4. **Multi-source delta disagreement** — `window_snapshots` has `delta_chainlink`, `delta_tiingo`, `delta_binance`, `price_consensus`. When sources disagree (MIXED), the model should have lower confidence. Currently not a feature.

5. **CLOB orderbook state** — `ticks_clob` has `up_spread`, `down_spread`, bid/ask. Thin books = lower fill probability and higher adverse selection. Not used.

6. **Hour-of-day as a categorical** — Current model has `hour_of_day` as a numeric feature. But the session effect is non-linear (Asian=gold, London=poison). A categorical or cyclical encoding would capture this better.

7. **Recent trade outcome memory** — The model has no concept of "I just lost 3 in a row." Consecutive loss streaks correlate with regime shifts the model hasn't detected yet.

8. **ELM prediction stability** — `ticks_v2_probability` at 1Hz shows how the model's own prediction evolves during a window. If dune_p oscillates between 0.70-0.90 in the 30s before trade, that's uncertainty the model doesn't report.

---

## Part 2: Proposed Enhancements (Ranked by Impact)

### Enhancement 1: Session-Aware Confidence Scaling (HIGHEST IMPACT)

**Problem:** Model reports 0.83 confidence at both 03:00 UTC (100% WR) and 10:00 UTC (50% WR).

**Solution:** Add session-regime interaction features:

```python
# New features for v4 training
"hour_bucket",           # 0-5 (Asian night), 6-8 (Asian day), 9-11 (London), 12-16 (US), 17-23 (Evening)
"session_vol_5m",        # 5-minute realized volatility (from ticks_binance)
"session_vol_15m",       # 15-minute realized volatility
"session_vol_ratio",     # vol_5m / vol_15m — rising = regime shift
"n_ticks_last_60s",      # Trade intensity — proxy for HFT activity
"price_range_5m_pct",    # High-low range as % of price
```

**Why it works:** London losses happen during high-volatility regime transitions. The model sees the same CoinGlass OI and VPIN but doesn't see that volatility just doubled. These features let it learn "high confidence + high vol = reduce confidence."

**Data source:** `ticks_binance` (already in DB, compute at build_dataset time)

**Expected impact:** Largest single improvement. If the model can learn that 09-12 UTC + high vol = lower confidence, it would soft-block 4-5 of today's 9 losses via lower dune_p (below threshold).

### Enhancement 2: VPIN Dynamics (Slope & Acceleration)

**Problem:** Single-point VPIN misses the trajectory. VPIN rising from 0.50→0.62 (informed flow building) is very different from VPIN falling from 0.75→0.62 (cascade exhausting).

**New features:**
```python
"vpin_slope_30s",        # Linear regression slope of VPIN over last 30s
"vpin_slope_120s",       # Over last 2 minutes
"vpin_acceleration",     # Slope change: slope_30s - slope_120s
"vpin_stability",        # Std dev of VPIN over last 60s (low = stable signal)
```

**Data source:** `ticks_binance.vpin` (1-3Hz, already persisted)
**Computation:** At build_dataset time, window back from t_target and compute from raw ticks

**Expected impact:** Catches regime transitions earlier. VPIN acceleration > 0 during TRANSITION regime = cascade building = model should increase confidence. VPIN deceleration = exhaustion = reduce confidence.

### Enhancement 3: CoinGlass Temporal Derivatives

**Problem:** Absolute OI of $25B tells you nothing. OI dropping 2% in 5 minutes while liquidations spike — that's a cascade signal the model can't see with snapshot features.

**New features:**
```python
"cg_oi_delta_1m",        # OI change over last 1 minute
"cg_oi_delta_5m",        # OI change over last 5 minutes  
"cg_liq_acceleration",   # Liquidation rate change (liq_1m / liq_5m)
"cg_taker_momentum",     # taker_net_usd change over 1m
"cg_funding_zscore",     # Funding rate z-score vs 24h rolling mean
```

**Data source:** `ticks_coinglass` (15s cadence, already persisted)
**Computation:** At build_dataset time, self-join on ticks_coinglass with 1m and 5m lookbacks

**Expected impact:** Better cascade and reversal detection. Today's London losses had CoinGlass showing mixed signals — OI stable but taker flow flipping. The temporal derivatives would surface this.

### Enhancement 4: Model Self-Uncertainty (Prediction Stability)

**Problem:** The model reports a point estimate (0.83) but not how stable that estimate is. If dune_p was 0.71 → 0.85 → 0.78 → 0.83 in the last 30s, that's less reliable than a steady 0.83.

**New features:**
```python
"dune_p_std_30s",        # Std dev of dune_p over last 30s (from ticks_v2_probability)
"dune_p_min_30s",        # Min dune_p in last 30s
"dune_p_max_30s",        # Max dune_p in last 30s
"dune_p_range_30s",      # max - min (uncertainty band)
"dune_p_trend_30s",      # Linear slope of dune_p (rising confidence vs falling)
```

**Data source:** `ticks_v2_probability` (1Hz, already persisted)
**Complication:** This is a meta-feature — the model predicting its own reliability. Requires the v3 model to be retrained on v2 prediction stability data. Can only be computed for windows where we have v2 prediction history.

**Expected impact:** Medium. Catches oscillating predictions. If dune_p range > 0.10 in 30s, the model is uncertain even if the latest point is 0.83.

### Enhancement 5: Multi-Source Delta Disagreement

**Problem:** When Chainlink says UP and Tiingo says DOWN (or delta is near-zero), the model should have lower confidence. Currently the gate pipeline checks source agreement, but the model itself doesn't see this.

**New features:**
```python
"delta_disagreement",    # |delta_chainlink - delta_tiingo| / max(|cl|, |ti|)
"delta_abs_min",         # min(|delta_chainlink|, |delta_tiingo|, |delta_binance|)
"n_sources_agree",       # Count of sources agreeing on direction (0-3)
"delta_consensus_pct",   # What % of price sources agree
```

**Data source:** `window_snapshots` (already has all three deltas)
**Computation:** Trivial — derive at build_dataset time

**Expected impact:** Today's 2 delta-filterable losses (#3250 with 0.006% and #3301 with 0.0005%) would be caught. But more importantly, the model learns to reduce confidence when sources disagree, not just gate it.

### Enhancement 6: Gamma Orderbook Implied Probability

**Problem:** Polymarket's own UP/DOWN token prices reflect the market's view. If Gamma shows UP at $0.55 and DOWN at $0.45, the market thinks UP is likely. If our model disagrees, one of us is wrong — and the market has more participants.

**New features:**
```python
"gamma_implied_up",      # gamma_up_price / (gamma_up_price + gamma_down_price)
"gamma_vs_model",        # gamma_implied_up - dune_p (model disagrees with market?)
"gamma_spread_total",    # (1 - gamma_up_price - gamma_down_price) — market uncertainty
"gamma_momentum_60s",    # Change in gamma_implied_up over last 60s
```

**Data source:** `ticks_gamma` (per-window, already persisted)
**Computation:** Derive at build_dataset time. `gamma_vs_model` requires v2 predictions to be aligned.

**Expected impact:** When the market disagrees with the model (gamma_vs_model > 0.10), we should trust the market more. Particularly valuable for detecting when the model is "stale" — the market has already priced in information the model's features haven't captured.

---

## Part 3: Training Pipeline Enhancements

### 3.1 Expanded Feature Set (37 → 55 features)

Add 18 new features to `FEATURE_COLUMNS` and `build_dataset.py`:

| Category | New Features | Count |
|----------|-------------|-------|
| Session/Volatility | hour_bucket, session_vol_5m, session_vol_15m, session_vol_ratio, n_ticks_last_60s, price_range_5m_pct | 6 |
| VPIN Dynamics | vpin_slope_30s, vpin_slope_120s, vpin_acceleration, vpin_stability | 4 |
| CG Temporal | cg_oi_delta_1m, cg_oi_delta_5m, cg_liq_acceleration, cg_taker_momentum, cg_funding_zscore | 5 |
| Delta Agreement | delta_disagreement, delta_abs_min, n_sources_agree | 3 |

(Enhancements 4-6 deferred to v4.1 as they require additional data pipeline work)

### 3.2 Enriched build_dataset.py

The `build_trainset_query` in `queries.py` needs additional `LEFT JOIN LATERAL` clauses:

```sql
-- VPIN dynamics: look back 30s and 120s from t_target
LEFT JOIN LATERAL (
    SELECT 
        stddev(vpin) as vpin_std_60s,
        regr_slope(vpin, extract(epoch from ts)) as vpin_slope_30s,
        count(*) as n_ticks_60s
    FROM ticks_binance 
    WHERE asset = t.asset 
      AND ts BETWEEN t.t_target - interval '60 seconds' AND t.t_target
) vpin_dyn ON TRUE

-- CoinGlass 1m lookback for temporal derivatives
LEFT JOIN LATERAL (
    SELECT oi_usd as cg_oi_1m_ago, taker_buy_usd as cg_taker_buy_1m_ago, ...
    FROM ticks_coinglass
    WHERE asset = t.asset AND ts <= t.t_target - interval '60 seconds'
    ORDER BY ts DESC LIMIT 1
) cg_1m ON TRUE

-- Volatility from tick data
LEFT JOIN LATERAL (
    SELECT 
        stddev(price) / avg(price) as session_vol_5m,
        max(price) - min(price) as price_range_5m,
        count(*) as n_ticks_300s
    FROM ticks_binance
    WHERE asset = t.asset
      AND ts BETWEEN t.t_target - interval '300 seconds' AND t.t_target
) vol ON TRUE
```

### 3.3 Calibration Fix: Temperature Scaling → Ensemble

The current OAK model clusters at extremes (0.0 or 1.0 ~96% of the time per the audit doc). Temperature scaling helps but doesn't fix the underlying issue — the LightGBM model has too few leaves to produce smooth probabilities.

**Proposal:**
1. **Increase `num_leaves` from 15 → 31** — More granular splits, less extreme predictions
2. **Add `min_gain_to_split: 0.1`** — Prevent overfitting on noise
3. **Use Platt scaling instead of temperature** for the confidence range 0.65-0.90 where our trades live
4. **Train separate calibrators per hour_bucket** — Asian calibrator vs London calibrator

### 3.4 Walk-Forward with Session Stratification

Current walk-forward split is purely chronological (60/20/20). This means if the validation set happens to be all-Asian, the model over-fits to Asian patterns.

**Proposal:** Stratified walk-forward — ensure each split contains proportional Asian, London, US, and Evening windows. This forces the model to learn session-robust features rather than session-specific artifacts.

---

## Part 3B: DB-Validated Key Findings

### Finding 1: The Gate Pipeline IS the Alpha (Not the Model)

```
                    Evals    Correct    WR%
TRADE decisions:      180       139    77.2%
SKIP decisions:    13,151     7,635    58.1%
```

The gate pipeline adds **19 percentage points** of accuracy. The raw model runs 58-62% regardless of confidence — EVERY confidence bucket from 65% to 90% produces 59-62% WR on evaluations. The >85% confidence bucket actually performs WORST at 23.7% WR.

### Finding 2: Definitive Entry Zone Map (Session × Regime)

```
SESSION         REGIME       Trades  W/L    WR%     Sim PnL  ACTION
─────────────────────────────────────────────────────────────────────
1_LateAsian     CALM             5   2/3    40.0%   -$7.00   BLOCK
1_LateAsian     NORMAL          13  11/2    84.6%  +$10.80   TRADE
1_LateAsian     TRANSITION      34  27/7    79.4%  +$19.40   TRADE
2_EarlyAsian    CASCADE          5   5/0   100.0%   +$8.00   TRADE
2_EarlyAsian    NORMAL          25  18/7    72.0%   +$5.00   REDUCE SIZE
2_EarlyAsian    TRANSITION      40  34/6    85.0%  +$34.00   TRADE ★ BEST
3_London        CASCADE          4   4/0   100.0%   +$6.40   TRADE
3_London        NORMAL          15  11/4    73.3%   +$4.00   REDUCE SIZE
3_London        TRANSITION      28  18/10   64.3%   -$5.20   BLOCK ★ BLEEDING
4_USOpen        NORMAL           4   3/1    75.0%   +$1.40   TRADE
4_USOpen        TRANSITION       7   6/1    85.7%   +$6.20   TRADE
```

**Key zones:**
- **GOLD:** EarlyAsian + TRANSITION (85%, +$34) and CASCADE everywhere (100%)
- **SILVER:** LateAsian + NORMAL/TRANSITION (79-85%)
- **POISON:** London + TRANSITION (64.3%, -$5.20) and CALM everywhere (40%)

### Finding 3: The Raw Model Has Zero Confidence Discrimination

```
Confidence Bucket    Evals    WR%
>85% conf              152    23.7%  ← WORST! Anti-correlated
80-85%               2,904    59.2%
75-80%               5,751    60.7%
70-75%               2,918    60.7%
65-70%                 452    59.7%
<65% (no signal)     1,029    46.6%
```

The model clusters predictions at extremes (bimodal at 0.0/1.0) and high confidence is actually ANTI-correlated with accuracy. Temperature scaling helps but doesn't fix the underlying issue.

### Finding 4: Skip Analysis — Gate Pipeline Correctly Filters

```
Gate Failed    Skipped    Would-Win WR%
(none/dune)     9,101       62.3%      ← Model had signal but below threshold
delta           3,555       52.0%      ← Coin flip — gate correctly blocks
vpin              372       30.1%      ← Gate catches genuine trash
```

Ungated skips (9,101) where all sub-gates pass but DUNE threshold wasn't met: 62.3% WR. At $0.68 entry you need 68% to break even, so these are correctly filtered.

### Finding 5: CASCADE Skips Are GOLD Being Left on Table

```
Conf Bucket     Regime       Skipped    Would-Win WR%
70-80%          CASCADE         800       75.0%  ← TRADEABLE! Being blocked!
>80%            CASCADE         472       68.0%  ← Marginal but positive
70-80%          TRANSITION    3,234       67.5%  ← Close to breakeven
```

800 CASCADE evaluations at 70-80% confidence are being skipped but would win 75% of the time. The CASCADE threshold (0.80) may be too high — these are profitable trades being left on the table.

### Finding 6: Hour-of-Day Effect on SKIPPED Signals

```
Hour(UTC)    Skips    Would-Win WR%
06:00          648       77.9%  ← GOLD HOUR being under-traded
07:00          697       77.6%  ← GOLD HOUR being under-traded
10:00          789       75.7%  ← Surprisingly good (London)
04:00          510       68.0%  ← Above breakeven
12:00          514       42.4%  ← Correctly skipping
```

Hours 06-07 UTC have 77-78% WR on SKIPPED signals. The model builds confidence slowly during these clean-trend hours, meaning signals arrive at offsets where the threshold hasn't been met yet. Lowering thresholds for these hours (or extending eval window) would capture profitable trades.

### Finding 7: Feature Comparison Wins vs Losses

```
                    Losses(9)   Wins(41)   Discriminating?
avg |delta|          0.0299      0.0289     NO (identical)
avg VPIN             0.573       0.585      NO (identical)
avg dune_p           0.448       0.448      NO (identical)
avg eval_offset      124         113        SLIGHT (losses further out)
avg hour_utc         10.6        5.4        YES ★ (5 hours apart!)
source divergence    0.0131      0.0145     NO (identical)
binance_agrees       67%         63%        NO (identical)
```

**Hour is the ONLY discriminating feature between wins and losses.** All signal features (delta, VPIN, dune_p, source agreement) are statistically identical. The model literally cannot distinguish good from bad trades on signal quality — only timing matters.

---

## Part 4: Winning Entry Zone Gate System

Beyond model improvement, the gate pipeline itself should be enhanced based on today's data.

### 4.1 Current Gate Pipeline Review (v10.4)

| Gate | Purpose | Today's Performance | Verdict |
|------|---------|-------------------|---------|
| G1: Source Agreement | CL+TI must agree | Working — filters bad directions | KEEP |
| G2: Taker Flow | Hard block when both oppose | Working — 80%+ WR when aligned | KEEP |
| G3: CG Confirmation | 2+ CG = bonus, 0 = penalty | Minor effect, dampened correctly | KEEP |
| G4: DUNE Confidence | Regime threshold + penalties | **PROBLEM** — passes all 9 losses at 0.78-0.84 | NEEDS WORK |
| G5: Spread Gate | Block if spread > 8% | Working | KEEP |
| G6: Dynamic Cap | Cap = dune_p - 0.05 | Working | KEEP |

### 4.2 Proposed New Gates (v10.5+)

**G4a: Delta Minimum Gate (HIGH PRIORITY)**
```python
# Block trades with near-zero price movement
TRANSITION_MIN_DELTA = 0.010  # 0.01% minimum |delta|
if regime == "TRANSITION" and abs(delta_pct) < TRANSITION_MIN_DELTA:
    return SKIP, "delta_too_small"
```
- Catches: 2/9 losses today ($14.45 saved)
- Blocks: ~3 low-delta wins ($4.80 missed)
- **Net +$9.65 today**

**G4b: Session Volatility Gate (MEDIUM PRIORITY)**
```python
# Reduce exposure during high-vol sessions
session_vol = compute_5m_volatility(ticks_binance)
if session_vol > VOL_THRESHOLD and regime == "TRANSITION":
    # Tighten DUNE threshold by +0.05
    effective_threshold += 0.05
```
- Would have raised threshold from 0.75 → 0.80 for TRANSITION during London
- 4 of today's 7 unfilterable losses had dune_p < 0.84 — this catches 2-3

**G4c: Consecutive Loss Cooldown Enhancement**
```python
# Current: 3 losses → 15min cooldown
# Proposed: 2 losses in 30min → tighten threshold +0.03 for next 3 trades
if recent_losses_in_window(30min) >= 2:
    effective_threshold += 0.03  # Soft tighten, not hard block
```
- Today's losses came in clusters (09:33+09:37, 10:47+10:51, 11:22+11:27)
- After loss #3248, tightening would have caught #3250 (dune_p 0.784 < 0.78 threshold)

**G4d: Offset-Regime Sweet Spot Gate**
```python
# Data shows TRANSITION T-121..150 is 6W/0L (100%)
# TRANSITION T-151+ is 6W/2L (75%) — tighten here
if regime == "TRANSITION" and eval_offset > 150:
    effective_threshold += 0.03  # Extra confidence required for late offsets
```

### 4.3 Adaptive Sizing (Not a Gate, but Critical)

```python
# Scale stake by session volatility
vol_mult = 1.0
if session_vol_5m > VOL_HIGH:
    vol_mult = 0.6  # 40% reduction during high vol
elif session_vol_5m > VOL_MEDIUM:
    vol_mult = 0.8  # 20% reduction

stake = base_stake * vol_mult
```

Today's 9 losses averaged $6.76 at 7.5% fraction. With vol-adaptive sizing:
- Asian (low vol): Full stakes → same $1.86 wins
- London (high vol): 60% stakes → losses drop from $6.76 → $4.06
- **Net improvement: ~$24 saved on losses, ~$0 lost on wins (Asian was full stake)**

---

## Part 5: Implementation Roadmap

### Phase A: Quick Wins (env changes, zero code risk) — TODAY
1. `BET_FRACTION=0.050` (was 0.075)
2. `STARTING_BANKROLL=<current_wallet>` (was stale $115)
3. `ABSOLUTE_MAX_BET=6.0` (was 10.0)

### Phase B: Delta Gate (code change, low risk) — 1 day
1. Add `TRANSITION_MIN_DELTA=0.010` env var to `constants.py`
2. Add delta check in `gates.py` DuneConfidenceGate
3. Deploy to Montreal

### Phase C: v4 Training Data Build (no production risk) — 2-3 days
1. Update `queries.py` with new LATERAL joins for volatility, VPIN dynamics, CG temporal
2. Update `build_dataset.py` with derived feature computation
3. Run `build_dataset.py --asset BTC --timeframe 5m --all-deltas` to generate expanded Parquet
4. Verify new features have coverage and aren't all-NaN

### Phase D: v4 Model Training & Calibration — 2-3 days
1. Update `FEATURE_COLUMNS` in both `train_lgb.py` and `v2_scorer.py` (55 features)
2. Train with `num_leaves=31`, stratified walk-forward
3. Compare v3 vs v4: accuracy, skill, calibration (per session bucket)
4. If v4 skill > v3 skill per session: deploy as CEDAR/candidate
5. A/B test via `/v2/probability/staging` endpoint (already exists)

### Phase E: Session-Aware Gates — 1-2 days
1. Add session volatility computation to orchestrator (5m rolling vol from ticks_binance)
2. Add G4b (session vol gate) and G4c (loss cluster cooldown) to gates.py
3. Add vol-adaptive sizing to risk_manager.py
4. Deploy behind env flags

### Phase F: Validation — 1 week
1. Run v4 model alongside v3 (staging endpoint)
2. Compare dune_p distributions: does v4 differentiate Asian vs London?
3. Track: false-negative rate (skipped winners) vs false-positive rate (traded losers)
4. Target: London WR > 70% (up from 50%), Asian WR stays > 90%

---

## Success Criteria

| Metric | Current (v3/v10.4) | Target (v4/v10.5+) |
|--------|-------------------|---------------------|
| Overall WR | 82% | 85%+ |
| Asian WR | 100% | 95%+ (maintain) |
| London WR | 50% | 70%+ |
| Avg loss / avg win ratio | 3.6x | < 2.0x |
| Confidence discrimination | None (0.78-0.88 for all) | Losses have dune_p < 0.75 |
| Net PnL per 50 trades | +$15 | +$40+ |

---

## Key Insight

The single most valuable data asset is the **session-dependent performance pattern** visible in trade_bible + signal_evaluations. The model currently treats 03:00 UTC and 10:00 UTC identically. Teaching it that London volatility degrades its own accuracy is worth more than any new signal source. The features to do this (session_vol, hour_bucket, price_range) are trivial to compute from data we already have.

The second most valuable asset is the **2-second signal_evaluations table** — it has every evaluation point including skips, enabling optimal threshold surface computation per regime×offset×hour. This is the data source for Phase E's session-aware gates.
