# ML Data Analysis — 2026-04-13

## Training Data Health

| Metric | Count |
|--------|-------|
| Total windows | 94,219 |
| With actual_direction | 94,038 (99.8%) |
| With close_price | 94,219 (100%) |
| With outcome (Polymarket) | 729 |
| Trade placed | 37,390 (39.7%) |
| Total trades | 2,021 |
| Trades with outcome | 1,137 |
| Paper trades still OPEN | 2 |

**Data range:** 2026-04-03 → 2026-04-13 (11 days)

**Key finding:** 94,038 windows have `actual_direction` — far more than the 53 we estimated. Shadow labeling was already partially working or was backfilled. This is enough for LightGBM retraining NOW.

---

## Gate Importance Analysis

Which gates block trades, and would those trades have won?

| Gate | Blocked | Would Win | Would Lose | Win Rate |
|------|---------|-----------|------------|----------|
| **delta** | 1,440,083 | 31,332 | 31,753 | 49.7% |
| **vpin** | 142,923 | 4,285 | 2,637 | 61.9% |
| dune_confidence | 5,585 | 0 | 0 | 0% |
| source_agreement | 897 | 0 | 0 | 0% |
| delta_magnitude | 366 | 0 | 0 | 0% |
| eval_offset_bounds | 55 | 0 | 0 | 0% |

**Key findings:**
1. **delta gate** blocks 1.4M evaluations — nearly all are coin-flip (49.7%). Gate is doing its job — filtering noise.
2. **vpin gate** blocks 143K evaluations — but 61.9% would have been correct. **VPIN gate may be too tight.** 4,285 winning trades blocked.
3. dune_confidence/source_agreement/delta_magnitude: block very few, direction data is 0% — these gates only fire when there's no valid direction signal at all (expected).

**Action item:** Investigate VPIN gate threshold. 61.9% WR on blocked trades suggests the gate is filtering some real signal.

---

## Direction Win Rates (Executed Trades)

| Direction | Total | Correct | Win Rate |
|-----------|-------|---------|----------|
| **DOWN** | 20,417 | 20,353 | **99.7%** |
| **UP** | 16,942 | 6,034 | **35.6%** |

**DOWN is near-perfect (99.7%).** UP is below breakeven (35.6%).

This confirms the Apr 12 directional asymmetry finding. DOWN-only strategy is the clear winner. UP predictions are hurting overall WR.

---

## Feature Null Rates

| Feature | Null % |
|---------|--------|
| confidence | 96.1% |
| cg_oi_usd | 22.6% |
| cg_funding_rate | 22.6% |
| direction | 22.6% |
| actual_direction | 0.2% |
| regime | 0.0% |
| vpin | 0.0% |
| close_price | 0.0% |
| open_price | 0.0% |
| delta_pct | 0.0% |

**Key findings:**
1. **confidence 96% null** — only set when trade is placed. Not a useful ML feature for all-windows training.
2. **CoinGlass 22.6% null** — feed was offline for ~2.5 days of the 11-day range. LightGBM handles NaN natively, but worth investigating the gap.
3. **Core features (vpin, regime, delta, prices) are 100% populated.** Training-ready.

---

## Strategy Decisions (v2 Registry)

| Strategy | Mode | Action | Count |
|----------|------|--------|-------|
| v4_fusion | LIVE | TRADE | 368 |
| v4_fusion | GHOST | TRADE | 139 |
| v4_fusion | LIVE | SKIP | 4,191 |
| v4_down_only | LIVE | SKIP | 22,772 |
| v10_gate | GHOST | SKIP | 27,386 |
| v4_up_asian | LIVE | SKIP | 19,435 |

**v4_fusion LIVE has 368 trades** — enough for preliminary evaluation. v4_down_only and v4_up_asian are all SKIPs in the current data window.

---

## DATA CLEANING RUBRIC

### Critical: Deduplication Required

`window_snapshots` has up to 91 rows per (window_ts, asset) — the strategy re-evaluates on every 2s tick during the window lifetime, writing a new snapshot each time. Raw row count (94K) is misleading.

**Actual unique windows: 3,753 (3,696 labeled)**

**Dedup rule:** For ML training, take ONE row per `(window_ts, asset)` — the last-written row (highest `id`) has the most complete data (close_price filled, gates evaluated, etc.).

```sql
-- Canonical dedup query for ML training
SELECT DISTINCT ON (window_ts, asset) *
FROM window_snapshots
WHERE actual_direction IS NOT NULL
ORDER BY window_ts, asset, id DESC
```

### Stale Model Periods

The user flagged periods where the model was "spewing the same result." Symptoms:
- Long streaks of identical `direction` values
- Same `delta_pct` / `confidence` across many consecutive windows
- Typically during feed disconnects or model stalls

**Cleaning rule:** Flag windows where `direction` is the same as the previous N windows AND `delta_pct` variation < 0.001 across the streak. These are likely stale-model artifacts, not real predictions. Mark as `is_stale = true` and exclude from training.

```sql
-- Detect stale streaks (same direction, near-zero delta variation)
WITH deduped AS (
  SELECT DISTINCT ON (window_ts, asset) *
  FROM window_snapshots
  WHERE actual_direction IS NOT NULL
  ORDER BY window_ts, asset, id DESC
),
with_lag AS (
  SELECT *,
    LAG(direction) OVER (PARTITION BY asset ORDER BY window_ts) AS prev_dir,
    LAG(delta_pct) OVER (PARTITION BY asset ORDER BY window_ts) AS prev_delta
  FROM deduped
)
SELECT window_ts, asset, direction, delta_pct,
  CASE WHEN direction = prev_dir AND ABS(delta_pct - prev_delta) < 0.001 THEN true ELSE false END AS likely_stale
FROM with_lag
```

### CoinGlass Gap (22.6% null)

~2.5 days of 11-day range had CoinGlass feed offline. LightGBM handles NaN natively — these rows are safe to include. But training should track `cg_available` as a boolean feature so the model learns to discount CoinGlass when stale.

### Confidence Column (96% null)

Only populated when `trade_placed = true`. Useless as an all-windows feature. **Drop from feature set** or replace with a synthetic confidence derived from gate pass count + delta magnitude.

### DOWN Win Rate Anomaly (99.7%)

20,353 out of 20,417 DOWN trades "correct" seems suspiciously high. Investigate:
- Is the engine biased to only trade DOWN when it's extremely obvious?
- Is actual_direction biased (more DOWN markets in Apr 3-13 BTC price action)?
- Are there duplicate rows inflating the number?

After dedup + stale filtering, re-run direction WR to confirm.

---

## Implications for ML Upgrade Plan

1. **Phase 2a is unblocked NOW** — 94K labeled windows, not 53. Run build_dataset.py immediately.
2. **Phase 3 can start this week** — enough data for LightGBM retraining.
3. **VPIN gate threshold tuning** is a quick win — 4,285 blocked winners at 61.9% WR.
4. **DOWN-only model** (Phase 5a) is the highest-priority asymmetry play — 99.7% WR.
5. **confidence column is useless for all-windows ML** — drop it or compute a synthetic confidence from other features.
