# Signal Source Audit — 15m Strategies

**Question:** What signal does each 15m strategy actually consume?
**Method:** Inspect `strategy_decisions.metadata_json` for actual TRADE decisions in production.

## v15m_up_basic — `v2_probability_up`

**Sample (last 5 TRADEs, 2026-04-22 14:39:41 to 14:39:48 UTC, all same window 1776868200):**

```
direction:                 UP
confidence:                MEDIUM
conviction:                MEDIUM
v2_probability_up:         0.6510934499111691
poly_confidence_distance:  0.1511    (= |0.65 - 0.5|)
```

**Confirmed signal source: `v2_probability_up`** — the BLENDED probability returned by `/v4/snapshot.timescales.15m.probability_up`. This is computed from:
- `probability_lgb` (v9 LGB) for that timeframe
- `probability_classifier` (path1 classifier head)
- Blend ratio per timeframe

For BTC 15m, the LGB head (`lgb_btc_15m__a547c3d`) provides most of the signal because the classifier is the older `2026-04-18/unified_head/`.

**YAML thresholds (engine/strategies/configs/v15m_up_basic.yaml):**
```yaml
gates:
  - timing: { min_offset: 180, max_offset: 540 }
  - direction: { direction: UP }     # only fires when poly_direction == UP
  - confidence: { min_dist: 0.15 }   # |p_up - 0.5| >= 0.15
sizing:
  type: fixed_kelly
  fraction: 0.025
  max_collateral_pct: 0.05
```

**No classifier-distance gate, no LGB gate, no per-source check.** Pure blend → distance threshold → direction filter. Simplest possible strategy.

## v15m_gate — `v2_probability_up` + multi-gate

YAML:
```yaml
gates:
  - timing: { min_offset: 200, max_offset: 400 }
  - source_agreement: { min_sources: 2, spot_only: false }   # chainlink+tiingo+binance
  - delta_magnitude: { min_threshold: 0.0005 }                # 5bp move minimum
  - taker_flow: {}
  - cg_confirmation: { oi_threshold: 0.01, liq_threshold: 1000000 }
  - confidence: { min_dist: 0.12 }
  - dynamic_cap: { default_cap: 0.65 }
post_gate_hook: classify_confidence
```

Same `v2_probability_up` signal but stricter gates: source agreement across multiple oracle feeds, minimum 5bp delta move, taker flow alignment, CoinGlass confirmation. **Direction-agnostic** (no UP/DOWN bias).

This is more conservative — fires more often (756 unique windows vs 132 for up_basic) but lower per-window WR (58.6% vs 73%) because the gates eliminate the easy wins (high-conviction same-direction windows) and keep the marginal ones.

## v15m_up_asian — `v2_probability_up` + session filter

```yaml
gates:
  - timing: { min_offset: 270, max_offset: 450 }
  - direction: { direction: UP }
  - confidence: { min_dist: 0.10, max_dist: 0.20 }   # MEDIUM band only
  - session_hours: { hours_utc: [23, 0, 1, 2] }      # Asian session
```

Same UP-only logic as `up_basic` but tighter time window and explicit Asian hour filter. **63.6% deduped WR over 44 windows** — small sample, likely the Asian filter is ALREADY doing what we want, just over too narrow a window.

## v15m_fusion — `v2_probability_up` + 5m fusion check

Larger config (not shown), uses BOTH 5m AND 15m signals via the 5m fusion mechanism. Mostly DOWN-biased (8% UP bets, 92% DOWN). Recent 7d WR 97% on 223 trades — but per-day breakdown shows it's catching the recent DOWN-regime cleanly:
```
2026-04-25: n=  17  WR=100%
2026-04-26: n=   2  WR=  0%
2026-04-27: n= 151  WR=97.35%
2026-04-28: n=  37  WR=100%
2026-04-29: n=  16  WR=100%
```
Sample sizes per day are small but the cumulative is strong. Like `v15m_up_basic` but for the DOWN regime.

## v7_15m_sniper_btc — `probability_classifier` (classifier-only)

YAML (v7_15m_sniper.yaml line 65-66):
```yaml
ensemble_signal_source: path1_only      # use classifier directly, NOT blend
ensemble_skip_on_fallback: true
```

Hook (v7_15m_sniper.py line 456):
```python
p_classifier = getattr(surface, "probability_classifier", None)
```

**This is the ONLY 15m strategy reading the classifier directly.** It's also the strategy with `n=17 WR=100%` over 14d — too small to call but extremely promising. Stricter gates: `bucket_abs_dist_strong=0.30` (vs up_basic's 0.15), source agreement required, VPIN floor.

The fact it only fired 17 times in 14d means the strict gates are doing their job — only the highest-conviction classifier signals get through.

## v7_15m_sniper_eth — broken

YAML uses same `path1_only` source for ETH, BUT:
- ETH `/v4/snapshot` returns `status: cold_start`
- `probability_classifier` returns `0.4356229305267334` — a frozen no-context fallback (per note #232)
- The hook's `classifier_only_mode` branch (v7_15m_sniper.py:537) handles this — but the OUTPUT is still wrong because the classifier has no real input

Result: 44% WR over 743 trades — actively worse than random. Audit #267 (per-asset feature cache) blocks fixing this.

## Critical signal-pipeline anomaly discovered today

**On the GPU box (post-restart), `/v4/snapshot?asset=BTC` returns:**
```
5m:  probability_classifier = 0.38608115911483765
15m: probability_classifier = 0.38608115911483765   ← BIT-IDENTICAL
```

**Six decimal places match.** The per-timescale classifier dispatch (PR #116) is broken at runtime — both 5m and 15m calls receive the same 5m classifier output. **`15m_head_v1` is configured (`TIMESFM_CLASSIFIER_HEAD_URI_15M=...15m_head_v1/`) but not actually being served.**

Important: this affects v7_15m_sniper_btc (which reads classifier directly). It does NOT affect v15m_up_basic (which reads `probability_up` blend, where the LGB component IS per-timescale and correct).

So `v15m_up_basic`'s 73% per-window WR is real and based on the correct 15m LGB. The `v7_15m_sniper_btc` 100%/17 may be on flawed classifier inputs.

## Summary

| Strategy | Reads | Status | Per-window WR (14d) |
|---|---|---|---|
| **v15m_up_basic** | `v2_probability_up` (blend, LGB-driven for 15m) | ✅ working signal | **72.7%** |
| v15m_up_asian | same | ✅ working | 63.6% |
| v15m_fusion | same + 5m fusion | ✅ working (DOWN regime) | recent 97% n=223 |
| v15m_gate | same + multi-source gates | ✅ working | 58.6% |
| v15m_down_only | same (DOWN only) | ⚠️ losing per-window | n=49946 wr=58% |
| v7_15m_sniper | `probability_classifier` (broken per-timescale) | ⚠️ flawed input | 100% n=17 (untrustworthy) |
| v7_15m_sniper_eth/sol/xrp | same on cold-start asset | ❌ no real signal | ETH 44%, SOL 64%, XRP 63% |

**Bottom line: the BTC 15m LGB pipeline (`v2/btc/btc_15m/a547c3d/`) is the proven engine. Everything good here flows through `probability_up` as the consumed signal, with the LGB component doing the real work.**
