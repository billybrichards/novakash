# Strategy promotion checklist

Before promoting any strategy from GHOST → LIVE, all items below MUST pass.

## Pre-flight

- [ ] Strategy has ≥7 days of GHOST decision data
- [ ] `min_consecutive_pass_ticks` ≥ 2 (entry confirmation gate; this is NOT
      dedup — see `feedback_consec_ticks_not_dedup.md`)
- [ ] `min_offset_sec` / `max_offset_sec` set explicitly in YAML
- [ ] Per-strategy `max_position_usd` configured (typically $5 first 7d)
- [ ] Realized P&L computed via `fill_price` math, NOT `trades.pnl_usd`
      (memory `feedback_wallet_truth_authority.md` — DB pnl_usd is inflated)
- [ ] WR audit covers ≥30 first-fires across UP and DOWN

## Regression suite — MUST run

```bash
cd engine
python -m pytest tests/regression/dedup_hard/ -v
python -m pytest tests/ -k execute_trade -v
```

Paste both outputs in the PR description. ALL tests must pass before merge.

## Deploy-time health check

```bash
python3 scripts/ops/check_multi_fire.py 168  # last 7 days
```

Must report 0 multi-fire rows. Paste output in PR.

## Live flip

- [ ] Billy explicitly approves via PR comment or session message
  (NEVER auto-promote — memory `feedback_no_auto_model_promotion.md`)
- [ ] First 24h post-flip monitored: WR ≥ break-even-for-fill-cap, no
      multi-fire alerts
- [ ] If WR regresses or multi-fire detected → demote to GHOST immediately

## Lessons baked in (incidents this rule prevents)

- 2026-05-20: ETH `v9_2_eth_raw_lgb` fired DOWN 3x on window 1779312000
  in 40s, −$21.91. Every TTL mechanism cleared.
- 2026-05-01: BTC `v12_combo` fired UP 2x on same window, ~$11 phantom.
  Memory: `feedback_single_strategy_double_fire.md`.
- 2026-04-26: `v10_lgb_only` fired 6x on a single window (audit #322).

## Authoritative dedup invariant (post 2026-05-20 fix)

`PgTradeRepository.has_fill_for_strategy_window_direction(...)` queries the
trades table directly. **If any non-CANCELLED/SKIPPED/FAILED_EXECUTION row
exists for `(strategy_id, window_ts, direction, asset, timeframe, is_live)`,
the second fire IS blocked.** This invariant is independent of every
TTL-based or marker-conditional mechanism.

If a new dedup mechanism is added later, this checklist MUST be updated to
include its regression test.
