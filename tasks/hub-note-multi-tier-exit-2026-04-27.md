# Hub Note — Multi-Tier Exit + Signal-Flip Deploy

**Date:** 2026-04-27
**PR:** #402
**Branch:** feat/multi-tier-exit
**Engine PID:** 1146590 (LIVE since 05:22:21 UTC)
**Log:** /home/novakash/engine.log.20260427_052221

## What Shipped

### Multi-tier exit ladder (v9 + v10)

Replaced single 18-second exit window (T-48..T-30) with three contiguous tiers:

| Tier | Window | mark_pct | Ticks | Behaviour |
|------|--------|----------|-------|-----------|
| tier1_early | T-200..T-120 | 0.50 | 5 (~10s) | Far from close — broken thesis only |
| tier2_mid | T-120..T-60 | 0.55 | 4 (~8s) | Halfway — mean-reversion fading |
| tier3_aggressive | T-60..T-30 | 0.70 | 3 (~6s) | Current prod setting; book thinning |
| (none) | past T-30 | — | — | Book too thin to sell |

`mark_loss_tick_count` resets on tier transition (each tier has own threshold).
Stale-mark guard skips eval if CLOB feed >5s stale.

### Signal-flip detector (v1, exit-only)

- Trigger: `lgb_p_opposite >= 0.85` AND `lgb_dist >= 0.20` for 3 consecutive ticks
- Active window: T-240..T-60
- Action: exit current position via SELL FAK
- **Reverse re-entry NOT enabled** (deferred to v2 — needs schema change)

### Both strategies real-sell now

- v9_lgb_only: `exit_shadow_mode: false` (was already)
- v10_lgb_only: `exit_shadow_mode: false` (was shadow-only — flipped)

### Why

Tonight (2026-04-26) -$117 PnL: most losers crashed to ~0% mark within 60s of fill (T-240..T-180), but old monitor was gated off until T-48. By T-48 opposite token had rallied 0.95+ and our token bid was dust. Multi-tier catches the crash early.

## Tonight's Multi-Fill Investigation

Investigation by bg agent a90466c2acaf9566b found:

- 6 multi-fills since 23:23 UTC; **all v9 + v10 on same window same direction**
- NOT intra-strategy double-fill (PR #401 fixed that)
- `try_claim_fill_slot` PK is `(asset, window_ts, timeframe, strategy_id)` — per-strategy by design
- Cross-strategy fills were never blocked

**Decision: keep per-strategy fill-slot.** v9 and v10 are independent strategies; consensus-stake is acceptable. Treat as feature, not bug. Document in invariants.

## Further Work / Open TODOs

### Urgent

1. **Orphan trade reconciler bug** — 04:23 UTC v10 fill on-chain (`0x79ec757f5ec2c259b013e3d3cb94b8462a155b96662b1a920f65c5bb5fd556df` $7.47, won @ $0.75) but NO `trades` row. Order stuck in `order_manager.filled_resolving` poll. PnL/WR understated. Likely cause: parallel v10 task hit `already_filled_this_window` after first task's `place_market_order` returned but trade-persist call attached to wrong task. **Action:** add reconciliation script that checks on-chain fills against trades table, backfills missing rows.

### Soon

2. **Backtest harness for tier params** — current values (0.50/0.55/0.70 × 5/4/3 ticks) are starting hypotheses. Run parameter sweep against last 2 weeks of `ticks_clob` + `strategy_window_fills`, optimize for `net_counterfactual_pnl` subject to `premature_exit_rate < 10%`. Caveat: counterfactual fills assume liquidity at observed bid — haircut by 0.95 or replay L2 book.

3. **Shadow-mode validation** — once stable, run for 1 week with `exit_shadow_mode=true`, log would-have-exited events with counterfactuals. If aggregate metrics agree with backtest within 10%, no further action. If disagree, investigate book-replay assumptions.

4. **Hub kill switch** — add `exit_tiers_enabled` to trading-config (DB-overridable) so we can disable from Hub without engine restart. Current state: hardcoded to true in YAML.

### v2

5. **Reverse re-entry** — currently exits on signal flip but doesn't re-enter opposite. Requires schema change to `strategy_window_fills`:
   ```sql
   ALTER TABLE strategy_window_fills ADD COLUMN leg_seq SMALLINT NOT NULL DEFAULT 1;
   DROP CONSTRAINT strategy_window_fills_strat_window_uniq;
   ADD CONSTRAINT strategy_window_fills_strat_window_leg_uniq UNIQUE (strategy_id, window_ts, leg_seq);
   ```
   Reversal inserts `leg_seq=2`. PnL aggregation groups by `(strategy_id, window_ts)` summing across legs.

6. **Continuous adaptive threshold** — if backtest shows tier-boundary effects (positions surviving tier1 by 1 tick then immediately exiting tier2), replace discrete tiers with piecewise-linear mark_pct/mark_ticks function over T-200..T-30.

7. **Per-asset / per-regime tier tuning** — start with one set across BTC + all 4 assets + both strategies; tune per-strategy from shadow data later.

8. **Sell-failure repop** — current code pops position from monitor before placing sell. If sell fails after retries, position is lost from monitor. Need to re-add on final-failure.

### Non-Goals (Decisions)

- ML-based exit policy — long-term, not v1/v2
- Partial exits (sell 50% at tier1, rest at tier2) — explicitly avoided to keep state simple

## Tests

23 unit tests in `engine/tests/test_position_monitor_tiers.py`:
- Tier resolution (4 tests)
- Current tier lookup (5 tests)
- Mark-based tier exits (7 tests)
- Legacy YAML fallback (1 test)
- Stale-mark guard (1 test)
- Signal-flip detector (5 tests)

All passing. Existing `test_position_monitor_wiring.py` still passing (signature stability).

## Files Changed

- `engine/execution/position_monitor.py` — `ExitTier` dataclass, multi-tier `evaluate_exit`, flip detector, stale-mark guard
- `engine/strategies/registry.py` — pass `exit_tiers` + flip params from gate_params
- `engine/strategies/configs/v9_lgb_only.yaml` — add tiers + flip + real-sell
- `engine/strategies/configs/v10_lgb_only.yaml` — add tiers + flip + real-sell
- `engine/tests/test_position_monitor_tiers.py` — new test file (23 tests)
- `tasks/multi-tier-exit-design.md` — full design doc

## Rollback

Multi-tier behaviour gates on YAML `exit_tiers:` block. To revert: remove the block from v9/v10 YAML and restart engine. Legacy `exit_eval_*`/`exit_mark_*` keys still present; falls back to single-tier behaviour automatically.
