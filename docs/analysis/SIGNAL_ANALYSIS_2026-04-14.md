# Signal Analysis — 2026-04-14

**Period analysed:** 2026-04-13 18:00 UTC → 2026-04-14 12:30 UTC  
**Source:** `signal_evaluations` (111,543 rows) joined to `window_snapshots` (106,240 rows)  
**Eval offset:** T-90 to T-150 (validated sweet spot from 71K window analysis on 2026-04-12)

---

## 1. Market Regime

**77% DOWN windows** overnight — heavy bearish bias with choppy bullish intraday reversals.

### Hourly direction breakdown

| Hour UTC | UP windows | DN windows | UP% |
|---|---|---|---|
| 18:00 | 193 | 434 | 31% |
| 22:00 | 140 | 37 | **79%** (brief reversal) |
| 23:00 | 193 | 688 | 22% |
| 00:00 | 221 | 920 | 19% |
| 01:00 | 47 | 694 | 6% |
| 04:00 | 64 | 777 | 8% |
| 06:00 | 334 | 710 | 32% |
| 10:00 | 615 | 1,058 | 37% |

## 2. Model Accuracy (Sequoia v5.2)

### Overall: 73.3% at dist ≥ 0.12

```
Predicted DOWN → Actual DOWN: 51,396 ✓
Predicted DOWN → Actual UP:   11,760 ✗ (81.4% DOWN accuracy)
Predicted UP   → Actual UP:   14,965 ✓
Predicted UP   → Actual DOWN: 12,353 ✗ (54.8% UP accuracy)
```

### By confidence band (T-90..150)

#### UP predictions

| Band | Evals | WR | Verdict |
|---|---|---|---|
| STRONG (dist ≥ 0.15) | 19,315 | **66.6%** | ✅ Tradeable — clears 56% breakeven |
| MODERATE (0.12-0.15) | 8,003 | **26.3%** | ❌ Anti-predictive |
| MILD (0.08-0.12) | 36,110 | 19.0% | ❌ Anti-predictive |
| WEAK (< 0.08) | 66,925 | 14.9% | ❌ Noise |

**Critical finding:** The UP model is NOT broken — it produces a 66.6% WR at STRONG confidence. But including MODERATE or lower bands destroys the edge completely. The confidence filter is the single most important gate for UP strategies.

#### DOWN predictions

| Band | Evals | WR | Verdict |
|---|---|---|---|
| STRONG (dist ≥ 0.15) | 53,845 | **78.3%** | ✅ |
| MODERATE (0.12-0.15) | 9,311 | **99.0%** | ✅ |
| MILD (0.08-0.12) | 38,909 | **89.7%** | ✅ |
| WEAK (< 0.08) | 32,381 | **78.9%** | ✅ |

DOWN predictions are strong across ALL confidence bands. No filtering needed beyond the existing min_dist: 0.10.

### Full picture summary

| Direction | Confidence | WR |
|---|---|---|
| DOWN @ HIGH (dist≥0.12) | 81.4% |
| DOWN @ LOW (dist<0.12) | 84.8% |
| UP @ HIGH (dist≥0.12) | 54.8% |
| UP @ LOW (dist<0.12) | 16.3% |

## 3. UP Accuracy by Hour (dist ≥ 0.12)

| Hour UTC | Evals | WR | Notes |
|---|---|---|---|
| **03:00** | 5,369 | **85.1%** | Asian session peak |
| **10:00** | 2,943 | **87.4%** | London morning |
| **06:00** | 4,732 | **75.3%** | EU open |
| 07:00 | 1,638 | 59.0% | |
| 02:00 | 1,547 | 49.1% | |
| 23:00 | 1,729 | 32.9% | |
| 08:00 | 1,170 | 27.8% | |
| 01:00 | 2,821 | 19.8% | |
| 04:00 | 1,638 | 22.4% | |

**Confirms the UP Research Brief finding:** Asian session (especially 03:00 UTC) and London morning (10:00 UTC) have the strongest UP accuracy. v4_up_asian's 23:00-02:00 window should be re-evaluated — 01:00 (19.8%) and 23:00 (32.9%) are weak in this overnight session. The edge shifted to 03:00 and 06:00+.

## 4. Paper Trade Performance (deduped, overnight)

| Strategy | Trades | Wins | Losses | WR | PnL@$5 |
|---|---|---|---|---|---|
| v4_down_only | 16 | 11 | 4 | **73.3%** | +$3.52 |
| v4_fusion | 26 | 19 | 7 | **73.1%** | -$0.16 |
| five_min_vpin | 4 | 2 | 2 | 50.0% | -$4.53 |

## 5. Config Changes Applied

### v4_up_basic v1.0.0 → v1.1.0

```yaml
# BEFORE
confidence: { min_dist: 0.10 }

# AFTER  
confidence: { min_dist: 0.15 }
```

**Rationale:** MODERATE band (0.12-0.15) has 26.3% WR for UP — below breakeven and anti-predictive. STRONG band (≥0.15) has 66.6% WR. Raising the threshold removes the losing band.

### v4_down_only — no change

min_dist: 0.10 is correct. DOWN predictions are 78-99% WR at ALL confidence bands.

### v4_up_asian — no change

Already configured at min_dist: 0.15, max_dist: 0.20 from the UP Research Brief.

### Stake calculation fix

`runtime.bet_fraction` is now used unconditionally (previously YAML `fraction: 0.025` was overriding via `decision.collateral_pct`, causing stakes of $0.60-$0.82 on $57 bankroll, blocked by $1.00 minimum).

## 6. Strategy Version History

| Date | Strategy | Version | Change |
|---|---|---|---|
| 2026-04-12 | v4_down_only | 2.0.0 | Initial — DOWN-only, dist≥0.10, T-90..150, CLOB sizing |
| 2026-04-12 | v4_up_asian | 2.0.0 | Initial — UP, Asian session 23-02 UTC, dist 0.15-0.20 |
| 2026-04-12 | v4_up_basic | 1.0.0 | Initial — UP, any hour, dist≥0.10 |
| 2026-04-14 | v4_down_only | 2.1.0 | Added trade_advised gate |
| **2026-04-14** | **v4_up_basic** | **1.1.0** | **min_dist 0.10→0.15 — MODERATE band anti-predictive at 26.3% WR** |

## 7. Methodology Notes

**IMPORTANT:** Always use `signal_evaluations` for accuracy analysis, NOT `window_snapshots.v2_direction`. The `v2_direction` column in window_snapshots is a single snapshot from one eval offset — it does not represent the model's accuracy across the full evaluation surface.

**Join pattern:**
```sql
FROM signal_evaluations se
JOIN window_snapshots ws ON ws.window_ts = se.window_ts AND ws.asset = se.asset
WHERE se.eval_offset BETWEEN 90 AND 150
  AND ABS(se.v2_probability_up - 0.5) >= 0.12
```

**Ground truth:** `ws.actual_direction` (populated by the reconcile labeling pass).

## 8. Architecture Improvements Needed

See separate section in this doc: architecture recommendations for data analysis infrastructure.
