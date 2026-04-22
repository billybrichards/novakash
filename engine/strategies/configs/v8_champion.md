# v8_champion — primary LIVE strategy (2026-04-21)

**Status**: LIVE (per Billy, skips 72h GHOST warmup)
**Version**: 8.0.0
**Source**: Hub note #209 (v8_agent proposal) + three refinements derived
from recent shadow data.

## Rationale

v8_agent proposed a HIGH-conviction, fill-aware sniper targeting the
ensemble sweet spot (bucket ∈ {agree_strong, pegged_path1}, fill-band
[0.30, 0.65], volatile_trend + chop regimes, ex-h00/h05/h14 UTC).
Baseline proposal treated UP/DOWN symmetrically.

## Refinements applied on top of the base proposal

### R1 — inherit v6.1.4's UP fill-floor
Shadow data: v5_ensemble since 2026-04-18 shows UP at 18% WR across all
strategies. v6.1.4 (n=50 resolved UP trades) quantified the leak:

| Bucket | Trades | WR | PnL | ROI |
| --- | --- | --- | --- | --- |
| UP fill < 0.55, volatile_trend | 2W/8L | 20% | -$18.54 | -60% |
| UP fill ≥ 0.55, volatile_trend | 12W/3L | 80% | +$8.39 | +12% |

v8 inherits `up_min_fill_price: 0.55` verbatim.

### R2 — asymmetric UP/DOWN direction handling
DOWN accepts either conviction bucket at fill [0.30, 0.65]. UP requires
BOTH `agree_strong` AND `pegged_path1` **and** fill ≥ 0.55. Rationale:
LIVE UP has been the systemic losing side — push UP selectivity sharply.

### R3 — regime field fix (paired PR)
Pre-v8, `shadow_decisions` rows had `regime: None` for every strategy.
The paired fix in `engine/strategies/registry.py` injects
`surface.v4_regime` into decision metadata at the
`StrategyDecisionRecord` construction site so v8's shadow data is
analysable from day one.

## Gate stack (TRADE path)

```
1. timing               — eval_offset ∈ [30, 200]
2. utc_hour_block       — hour ∉ {0, 5, 14}
3. v4_regime            — ∈ {volatile_trend, chop}
4. source_agreement     — chainlink + tiingo both non-null
5. vpin                 — ∈ [0.40, 0.85]
6. ensemble_bucket      — agree_strong OR pegged_path1
7. fill_band            — CLOB ask ∈ [0.30, 0.65]
8. up_fill_floor        — if UP: fill ≥ 0.55
9. up_both_buckets      — if UP: agree_strong AND pegged_path1
```

## Sizing

Custom `clob_sizing` hook with quarter-Kelly fraction (0.025) and 5.8%
max collateral per trade. Ladder weighted toward HIGH conviction
(modifier 2.0 at conviction_score ≥ 0.55). DOWN-skew follows naturally
from the UP-side gates above.

## Rollback

Edit `mode: LIVE` → `mode: GHOST` in `v8_champion.yaml` and restart the
engine. Pair with restoring `v6_sniper.yaml` to `mode: LIVE` to fully
revert the lineup.

## Monitoring signals (first 20 minutes post-deploy)

- v8 TRADE decisions firing at least 1–2 times within a window cycle.
- UP v8 trades: fill_price ≥ 0.55 (confirms `up_fill_floor` is live).
- SKIP reasons consistent with gate order (timing / hour / regime
  dominant at current conditions).
- No duplicate stake with v6_sniper (which is now GHOST — should not
  execute).
- `strategy_decisions.metadata` (or `shadow_decisions.metadata`) rows
  contain a non-null `regime` field — confirms R3 fix is live.

## Auto-rollback trip-wires

- v8 fires TRADE with stake > $15 → ROLLBACK.
- 3 consecutive RESOLVED_LOSS on v8 → flip v8 GHOST, restore v6 LIVE.
- Engine crash → restore `.rollback/engine.prev`.
- UP v8 trade with fill < 0.55 → indicates gate broken, ROLLBACK.
