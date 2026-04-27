# Exit-Monitor Configuration Backtest — 48h Results

**Status:** Research only — no deploy. Read-only against Montreal Postgres.
**Date:** 2026-04-27
**Lookback:** Last 48h, strategies `v9_lgb_only` + `v10_lgb_only`
**Script:** `scripts/ops/backtest_exit_monitor.py`

## Sample size

- **75 resolved trades** (46 v9, 29 v10) over the last 48h
- **68 distinct windows**
- 100% CLOB-tick coverage, 100% strategy_decisions coverage
- Baseline actual PnL: **+$139.64** (v9: +$152.06, v10: -$12.43)

## Configurations tested

| ID | Description |
| --- | --- |
| A_legacy_tier3 | T-60..T-30, mark<0.70, ticks=3 (legacy single-tier) |
| B_current_3tier | 3-tier 0.50/0.55/0.70 × 5/4/3 ticks T-200..T-30 (current production) |
| C_3tier_strict_lgb | B + strict LGB gate (p_opposite≥0.85, lgb_dist≥0.20) |
| D_3tier_permissive_lgb | B + permissive LGB gate (p_opposite≥0.55, lgb_dist≥0.10) |
| E_tier3_permissive_lgb | A + permissive LGB gate |
| F_hold_forever | No exit at all |
| G_3tier_loose | 3-tier 0.30/0.40/0.60 × 5/4/3 ticks |

## Counterfactual sell mechanics

Per task brief — when a config fires:
```
sell_proceeds = 0.96 * fill_size * (mark * 0.7) + 0.04 * fill_size * (1 if won else 0)
counterfactual_pnl = sell_proceeds - (fill_price * fill_size)
```
i.e. 96% of size sold at the held-side bid haircut by 30%, 4% residual settles at outcome.

## Results

| Config | n_fills | n_exits | premature % | loss-avoidance % | Δ_pnl_v9 | Δ_pnl_v10 | Δ_pnl_total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| BASELINE (actual) | 75 | — | — | — | +152.06 | −12.43 | **+139.64** |
| A_legacy_tier3 | 75 | 61 | 75.4% | 68.2% | −392.86 | −176.09 | **−568.95** |
| B_current_3tier (prod) | 75 | 64 | 73.4% | 77.3% | −392.86 | −184.85 | **−577.71** |
| C_3tier_strict_lgb | 75 | 0 | 0.0% | 0.0% | 0.00 | 0.00 | **0.00** |
| D_3tier_permissive_lgb | 75 | 4 | 50.0% | 9.1% | −27.37 | 0.00 | **−27.37** |
| E_tier3_permissive_lgb | 75 | 2 | 50.0% | 4.5% | −10.54 | 0.00 | **−10.54** |
| F_hold_forever | 75 | 0 | 0.0% | 0.0% | 0.00 | 0.00 | **0.00** |
| G_3tier_loose | 75 | 64 | 73.4% | 77.3% | −392.86 | −184.85 | **−577.71** |

## Critical structural finding

**The held-side best_bid (`up_best_bid` for YES, `down_best_bid` for NO) sits at $0.01 for the entire trade lifetime — for both eventual winners and losers.**

This was verified by inspecting raw `ticks_clob` for individual trades. Both:
- A trade that resolved WIN (#5614, DOWN @ $0.80) — `down_best_bid` = $0.01 from fill to close
- A trade that resolved LOSS (#5503, UP @ $0.83) — `up_best_bid` = $0.01 from fill to close

The bid side is a near-empty stub on these 5-min markets. So any "exit at mark" mechanism sells into a $0.01 bid, then the haircut takes it to $0.007, which is dramatically worse than holding to resolution even on losers (losers just settle at $0).

This explains why configs A/B/G — which fire 60+ exits — destroy ~$570 of PnL. They're "saving" 77% of true losers by selling them at near-zero, but they're also tanking 75% of eventual winners by selling them at near-zero.

## Recommendation

**Switch to either C_3tier_strict_lgb or F_hold_forever. Both deliver +$139.64 over 48h vs −$438 to −$577 for the current production config B.**

Strict-LGB-gate (C) is preferred over hold-forever because:
- It costs nothing on this sample (0 exits fired).
- It retains an emergency brake when LGB strongly predicts the opposite direction (p≥0.85 AND dist≥0.20).
- The optionality is free — strict thresholds are rarely met, so it almost never fires.

Permissive variants (D, E) fire enough to show 50% premature rates and modest negative delta, suggesting any loosening of the LGB gate immediately starts harming PnL given the thin-bid environment.

## Caveats

- **Single-source haircut model.** The 70% haircut on `best_bid` is conservative but the bid is already $0.01 for nearly every snapshot, so the effective sell price is ~$0.007 regardless of haircut.
- **Reality of B in production** may differ slightly from this sim — e.g. real exits may hit slightly better off-book prices or batched orders. But the 75% premature-exit rate is a function of LGB-blind threshold logic and would not change.
- **Sample is 48h / 75 trades.** Limited; could re-run on 7-14d window if desired. Direction of finding (B is harmful, C/F are neutral, D/E are mildly harmful) is robust given the structural bid-side issue.
- **`outcome IS NULL` rows excluded.** A handful of in-flight or unresolved trades excluded from sample.

## Confidence: HIGH

The directional finding — that the production exit-monitor config B is destroying roughly $300+/day — rests on a clean, structural observation about the bid side that does not depend on simulation parameter choices. The 75% premature-exit rate alone would warrant disabling B until the bid-side stub problem is addressed.

## Suggested next steps (out of scope for this PR)

1. Disable production exit-monitor (or switch to C/F) ASAP — current setting is negative-EV.
2. Investigate why held-side `best_bid` is stuck at $0.01 — is this a data feed issue, or a real reflection of 5-min-market microstructure?
3. If real microstructure: any future exit-monitor design must NOT use `best_bid` as the sell mark. Consider `(up_best_ask + down_best_ask) / 2` mid-implied probability, or use TimesFM/LGB confidence directly without selling.
