# Fine-tune Shadow Evaluation — 2026-04-14

**Objective:** Evaluate whether a domain-fine-tuned Sequoia v5 model (trained on
109K+ Polymarket window outcomes from `signal_evaluations`) outperforms the
current production model (Sequoia v5.2, commit 15a4e3e).

**Status:** SHADOW ONLY. No model promotion. No production changes.

---

## 1. Current Production Model: Sequoia v5.2

### Live Accuracy (from signal_evaluations, 2026-04-13 18:00 to 2026-04-14 12:30 UTC)

**Overall: 73.3% at dist >= 0.12 (T-90..150 eval offset)**

| Direction | Band | Evals | Win Rate | Verdict |
|-----------|------|------:|:--------:|---------|
| DOWN | STRONG (dist >= 0.15) | 53,845 | **78.3%** | Tradeable |
| DOWN | MODERATE (0.12-0.15) | 9,311 | **99.0%** | Tradeable |
| DOWN | MILD (0.08-0.12) | 38,909 | **89.7%** | Tradeable |
| DOWN | WEAK (< 0.08) | 32,381 | **78.9%** | Tradeable |
| UP | STRONG (dist >= 0.15) | 19,315 | **66.6%** | Tradeable |
| UP | MODERATE (0.12-0.15) | 8,003 | 26.3% | Anti-predictive |
| UP | MILD (0.08-0.12) | 36,110 | 19.0% | Anti-predictive |
| UP | WEAK (< 0.08) | 66,925 | 14.9% | Noise |

**Key finding:** Sequoia v5.2 has excellent DOWN prediction across all bands,
but UP predictions are only profitable at STRONG confidence (dist >= 0.15).
This calibration profile is what the live strategies depend on.

### Paper Trade Performance (overnight, deduped)

| Strategy | Trades | Wins | WR | PnL |
|----------|-------:|-----:|---:|-----|
| v4_down_only | 16 | 11 | 73.3% | +$3.52 |
| v4_fusion | 26 | 19 | 73.1% | -$0.16 |
| five_min_vpin | 4 | 2 | 50.0% | -$4.53 |

---

## 2. Retrain Workflow Analysis

**Workflow:** `billybrichards/novakash-timesfm/.github/workflows/retrain.yml`
**Schedule:** Every 4 hours (:13 past the hour)
**Runner:** GitHub-hosted ubuntu-latest (not Montreal -- no inference latency impact)

### Pipeline

1. Pull 30 days of `signal_evaluations x market_data` via `training/novakash_dataset.py`
2. Train LightGBM (Sequoia v5) with 25 features including eval_offset, vpin, delta_pct, chainlink_price, etc.
3. Compare candidate vs production baseline (naive v2_probability_up passthrough)
4. Promotion gate: ECE <= 0.10, ECE improvement, skill improvement >= +0.5pp
5. Upload to S3 (always); promote current.json (only if gate passes AND not blocked)

### Safety guardrails (already in place)

- **5m polymarket slot auto-promote is BLOCKED** (added 2026-04-12) -- candidate uploads to S3 but current.json is never flipped without `force_promote=yes`
- `skip_promote=yes` input prevents promotion for ALL matrix cells
- `factual_parity_check` gate catches feature drift between training and serving
- Staging scorer reload always runs -- shadow predictions go to `ticks_v2_probability`

### Matrix cells

| Timeframe | Label Source | Model Slot | ECE Ceiling |
|-----------|-------------|------------|:-----------:|
| 5m | polymarket | current | 0.10 |
| 15m | polymarket | current_15m | 0.08 |
| 15m | binance | current_15m_binance | 0.08 |
| 1h | binance | current_1h_binance | 0.08 |
| 4h | binance | current_4h_binance | 0.08 |

---

## 3. Latest Retrain Results (Run #24411210384, 2026-04-14 16:38 UTC)

### 5m Polymarket Cell (the live trading slot)

**Training data:** 109,279 rows (55,848 UP / 53,431 DOWN) -- 5 regimes

| Metric | Candidate (v5 retrain) | Baseline (v2_prob passthrough) | Delta |
|--------|:----------------------:|:------------------------------:|:-----:|
| Accuracy | 65.90% | 59.89% | **+6.01pp** |
| Skill | +15.48pp | +9.47pp | **+6.01pp** |
| ECE | 0.0436 | 0.0451 | **+0.0015 better** |

**Gate result:** HOLD (not promoted)
**Reason:** `factual_parity_check` gate failed -- `No module named 'training'` import error in the gate script's parity check. The quality metrics themselves PASSED (ECE 0.0436 < 0.10, skill improvement +6.01pp > +0.5pp). Even if parity had passed, the 5m polymarket slot has auto-promote blocked.

### Per-Offset Breakdown (from PHASE_2_REPORT_V5_BTC.md, last saved)

| Bucket | n | Accuracy | Skill | ECE | UP acc | DOWN acc |
|--------|--:|--------:|---------:|------:|-------:|---------:|
| 60-120s | 909 | 61.28% | +8.87pp | 0.1333 | 72.16% | 56.15% |
| 120-180s | 2,244 | 59.22% | +6.82pp | 0.0589 | 64.11% | 55.86% |
| 180-240s | 2,537 | 55.58% | +3.17pp | 0.0849 | 59.03% | 52.30% |
| 240-600s | 84 | 46.43% | -5.98pp | 0.1283 | 50.00% | 43.18% |

**Sweet spot:** T-60 to T-120 (eval_offset 120-180s = 2 to 3 minutes before close). This aligns with the live strategy's T-90..150 configuration.

### Top Features by LightGBM Gain

1. `delta_pct` (97,134) -- dominant by 6x
2. `chainlink_price` (16,105)
3. `tiingo_close` (14,297)
4. `vpin` (9,832)
5. `delta_chainlink` (7,677)
6. `v2_logit` (6,380)
7. `delta_binance` (6,353)

---

## 4. Shadow Retrain Triggered

**Run:** https://github.com/billybrichards/novakash-timesfm/actions/runs/24413417560
**Parameters:**
```
skip_promote=yes    # NEVER promote -- shadow only
force_promote=no    # Explicit safety
include_skip=yes    # Full distribution (10x data)
```

**Trigger command (for re-running):**
```bash
gh workflow run retrain.yml \
  --repo billybrichards/novakash-timesfm \
  -f skip_promote=yes \
  -f force_promote=no \
  -f include_skip=yes
```

The shadow run will:
1. Train a new candidate on latest 30 days of data
2. Upload candidate artifacts to S3 (versioned, not current.json)
3. Reload the staging scorer so shadow predictions write to `ticks_v2_probability` with the candidate's `model_version`
4. NOT flip `current.json` -- production model stays at Sequoia v5.2

---

## 5. Comparison: Candidate vs Sequoia v5.2

### Test-set metrics (from retrain gate)

| Metric | Candidate | Sequoia v5.2 (baseline) | Improvement |
|--------|:---------:|:-----------------------:|:-----------:|
| Accuracy | 65.90% | 59.89% | +6.01pp |
| Skill | +15.48pp | +9.47pp | +6.01pp |
| ECE | 0.0436 | 0.0451 | Better by 0.0015 |
| Training rows | 109,279 | ~38,000 (est.) | 2.9x more data |

### Concerns before any promotion

1. **Conviction band stability unknown.** The live strategies depend on specific calibration bands (e.g., UP STRONG at dist >= 0.15 = 66.6% WR, DOWN MODERATE 99% WR). A retrained model may shift the `p_up` distribution, changing which windows fall into which bands. This could destroy the edge even if overall accuracy improves.

2. **UP/DOWN gap is wide.** The PHASE_2 report shows UP acc 62.30% vs DOWN acc 54.34% (+7.96pp gap). In production, DOWN is the dominant profitable direction (78-99% WR). If the candidate shifts probability mass toward UP predictions, it could dilute the DOWN edge.

3. **Parity check import failure.** The gate's `factual_parity_check` fails with a module import error, which means the feature-parity safety check between training and serving is not running. This needs fixing before any promotion.

4. **240-600s bucket is negative skill.** The candidate performs WORSE than random at eval_offset > 240s. This is expected (predictions far from close are low-signal) but confirms the T-90..150 offset window is critical.

---

## 6. Recommendation

**DO NOT PROMOTE.** Continue shadow evaluation.

### Next steps (for Billy to decide)

1. **Wait for shadow run #24413417560 to complete** (~5 min). Check results:
   ```bash
   gh run view 24413417560 --repo billybrichards/novakash-timesfm --log 2>&1 | \
     grep "retrain (5m, polymarket, current)" | grep -E "(acc=|ECE=|skill=)"
   ```

2. **After 24h of shadow predictions**, run conviction band analysis:
   ```bash
   # On a machine with DB access:
   python3 docs/analysis/up_down_strategy_report.py --model-version <candidate_version>
   ```

3. **Compare conviction bands side-by-side:**
   ```sql
   SELECT
       model_version,
       CASE
           WHEN ABS(probability_up - 0.5) >= 0.15 THEN 'STRONG'
           WHEN ABS(probability_up - 0.5) >= 0.12 THEN 'MODERATE'
           WHEN ABS(probability_up - 0.5) >= 0.08 THEN 'MILD'
           ELSE 'WEAK'
       END AS band,
       CASE WHEN probability_up > 0.5 THEN 'UP' ELSE 'DOWN' END AS direction,
       COUNT(*) AS n,
       ROUND(100.0 * SUM(CASE
           WHEN (probability_up > 0.5 AND ws.actual_direction = 'UP') OR
                (probability_up < 0.5 AND ws.actual_direction = 'DOWN')
           THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS win_rate
   FROM ticks_v2_probability t
   JOIN window_snapshots ws ON t.window_ts = ws.window_ts AND t.asset = ws.asset
   WHERE ws.actual_direction IS NOT NULL
     AND t.eval_offset BETWEEN 90 AND 150
   GROUP BY model_version, band, direction
   ORDER BY model_version, direction, band;
   ```

4. **Only promote if:**
   - DOWN STRONG WR >= 75% (currently 78.3%)
   - UP STRONG WR >= 60% (currently 66.6%)
   - No conviction band regression > 5pp
   - Parity check import error is fixed

### Promotion command (when ready -- NOT NOW)

```bash
gh workflow run retrain.yml \
  --repo billybrichards/novakash-timesfm \
  -f force_promote=yes \
  -f skip_promote=no \
  -f include_skip=yes
```

---

## 7. Summary

The retrain pipeline is already well-designed with strong safety guardrails.
The 5m polymarket slot has auto-promote explicitly blocked since 2026-04-12.
The latest candidate shows promising improvements (+6pp accuracy, better ECE)
but **conviction band stability is unverified** -- this is the critical blocker.

Shadow run #24413417560 has been triggered with `skip_promote=yes`. After it
completes and 24h of shadow predictions accumulate, run the conviction band
comparison query above to verify the calibration profile matches or improves
upon Sequoia v5.2's known bands before making any promotion decision.
