# v7_15m_sniper Strategy

**Version:** 7.0.0
**Mode:** GHOST (shadow only -- logs decisions, no execution)
**Direction:** ALL (UP/DOWN determined by classifier)
**Timescale:** 15m

## Overview

15-minute Polymarket sniper using the Path 1 classifier head as the
sole signal source. Fork of `v6_sniper` adapted for 15m windows.

**Multi-asset:** Three configs share the same hook (`v7_15m_sniper.py`):
- `v7_15m_sniper.yaml` — BTC (66.7% high-conviction accuracy, N=12)
- `v7_15m_sniper_eth.yaml` — ETH (80.8% high-conviction accuracy, N=26)
- `v7_15m_sniper_sol.yaml` — SOL (85.7% high-conviction accuracy, N=21)

ETH and SOL are the strongest performers at high conviction. The hook is
asset-agnostic — it reads `surface.probability_classifier` which the
TimesFM service computes per-asset. All three configs are GHOST mode.

There is no 15m LGB model yet, so the v6 ensemble blend
(`surface.poly_confidence`) is not used. Instead, v7 reads
`surface.probability_classifier` directly and derives direction and
conviction from it alone.

## Entry Window

**T-400 to T-300** (seconds before 15m window close).

Rationale: classifier evaluation simulation on 15m windows shows 83.9%
high-conviction accuracy at T-400. The T-400 to T-300 band is the
sweet spot:
- Early enough that the classifier has consumed most of the window's
  price action (10+ minutes of data).
- Late enough that the market hasn't fully priced in the move but
  there is still time for CLOB order placement and fill.
- Outside this band accuracy degrades: earlier has insufficient data,
  later has insufficient time for execution.

## Conviction Buckets

| Bucket | Condition | Accuracy | Action |
|--------|-----------|----------|--------|
| `pegged_classifier` | p >= 0.90 OR p <= 0.10 | Highest | TRADE |
| `classifier_strong` | \|p - 0.5\| >= 0.30 | ~83.9% | TRADE |
| `mid_conf` | \|p - 0.5\| < 0.30 | < 70% | SKIP |
| `no_eval` | classifier NULL | -- | SKIP |

The 0.30 threshold is higher than v6's 0.20 because:
1. Only one signal (no LGB confirmation to cross-validate).
2. 15m windows have more noise than 5m windows.
3. Classifier accuracy drops below 70% under this threshold for 15m.

## Defensive Gates

All gates inherited from v6_sniper:

1. **Feature staleness** -- chainlink + tiingo must be present
2. **UTC hour block** -- disabled by default for shadow data collection
3. **Timing** -- T-400 to T-300 (15m sweet spot)
4. **VPIN floor** -- vpin >= 0.45
5. **Classifier freshness** -- age <= 120s (wider than v6's 90s, polls every ~30s)
6. **Source agreement** -- chainlink + tiingo directional agreement (with high-VPIN bypass)
7. **trade_advised** -- with bucket-aware risk_off override
8. **Health badge** -- block on degraded/unsafe
9. **Regime** -- calm_trend, volatile_trend, risk_off tradeable; chop blocked

## Dependencies

- `timesfm-repo` must serve `probability_classifier` for 15m timescale
  in the `/v4/snapshot` response.
- Engine data surface must populate `probability_classifier` and
  `probability_classifier_inferred_at` for the 15m timescale block.

## Promotion Path

1. Deploy as GHOST alongside v6 LIVE.
2. Collect 72h+ shadow data.
3. Validate: bucket accuracy, regime/hour patterns, CLOB pricing.
4. Billy evaluates shadow data and approves promotion.
5. Flip `mode: GHOST` to `mode: LIVE` in YAML.
