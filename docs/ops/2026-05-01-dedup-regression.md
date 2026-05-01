# Per-window dedup regression — 2026-05-01

## TL;DR

The user reported tids `6582` + `6584` (`v12_lgb_combo`, both `UP`,
both resolved at `08:25:37 UTC`) as a same-window double-fire.
**They are NOT a double-fire** — the trades are on two different
consecutive 5-minute windows (`1777622700` and `1777623000`). They
just happened to be resolved by Polymarket in the same indexing batch.

But the wider audit surfaced a real regression: **10 actual same-
window double-fires in the past week**, all on `v9_lgb_only` and
`v10_lgb_only`, post-dating the pessimistic-claim PRs (#394, #396).
Each pair is two REAL on-chain transactions with distinct `tx_hash`
values — the engine paid double the intended stake on each.

Root cause: the DB-backed `try_claim_fill_slot` placeholder has a 25s
`STALE_PLACEHOLDER_TTL_SECONDS` self-takeover (audit #401, defending
against engine-SIGKILL leaks) and the FAK ladder + DB write round-trip
exceeds 25s under DB-pool contention. The next eval-offset tick
(typically ~30s later) finds the placeholder stale, takes it over,
and fires its own FAK. Both eventually fill on chain.

Fix proposed in PR — first-barrier in-process
`set[(strategy_id, window_ts, direction)]` in `ExecuteTradeUseCase`,
held for the full lifetime of an in-flight `execute()`. Closes the
cross-coroutine race window without touching the DB-level placeholder
semantics.

## User-reported pair (NOT a double-fire)

```
6582  v12_lgb_combo  UP  fill 0.66  stake 2.78  pnl  8.6860
      dedup_key=v12_lgb_combo:1777622700:UP   window 08:05–08:10 UTC
      tx 0x396823dae20015a7…  token 11863178…
6584  v12_lgb_combo  UP  fill 0.66  stake 2.55  pnl  8.5716
      dedup_key=v12_lgb_combo:1777623000:UP   window 08:10–08:15 UTC
      tx 0xcfd10c22dca41067…  token 82591945…
```

`window_ts` differs by exactly 300 seconds, `token_id` differs, slug
differs (`btc-updown-5m-1777622700` vs `btc-updown-5m-1777623000`).
Both windows have a corresponding row in `strategy_window_fills` with
the matching real `order_id`. Per-window dedup is doing exactly what
it's supposed to here.

The shared `resolved_at = 08:25:37` is a Polymarket batch-resolution
artefact — both 5-min markets fall in the same Chainlink update
window for the redeemer.

## Real double-fires (10 groups, all post-fix)

| Strategy | Window TS | Trades | Gap | Status | DB pnl_usd (sum) |
|---|---|---|---|---|---|
| `v10_lgb_only` | `1777245000` | 5483, 5484 | 8.9s | WIN/WIN | +9.98 |
| `v10_lgb_only` | `1777394400` | 5899, 5900 | 69.3s | EXPIRED/EXPIRED | 0 |
| `v10_lgb_only` | `1777395600` | 5884, 5885 | 28.8s | LOSS/LOSS | -15.00 |
| `v10_lgb_only` | `1777470900` | 6202, 6219 | 39.6s | LOSS/LOSS | -25.44 |
| `v10_lgb_only` | `1777480500` | 6252, 6253 | 45.6s | LOSS/LOSS | -44.02 |
| `v10_lgb_only` | `1777488900` | 6269, 6270 | 38.8s | WIN/WIN | +16.37 |
| `v10_lgb_only` | `1777492800` | 6282, 6284 | 31.6s | WIN/WIN | +9.48 |
| `v9_lgb_only` | `1777411200` | 5960, 5961 | 26.1s | WIN/WIN | +12.32 |
| `v9_lgb_only` | `1777416600` | 5984, 5985 | 29.1s | WIN/WIN | +9.24 |
| `v9_lgb_only` | `1777470000` | 6196, 6198 | 23.9s | WIN/WIN | +14.63 |

**Total intended stake = ~$120 (across 10 windows).
Actual stake = ~$240. Excess on-chain spend ≈ $120.**

Net wallet impact for the 10 double-fires (sum of all 20 rows): the
DB `pnl_usd` is correct — both rows reflect real on-chain pnl since
each tx_hash settled independently.

## Why it slipped past the existing dedup

The dedup pipeline today (audit #321 + #322 + #401):

1. **Step 0.5** `has_filled(window_key, sid)` — DB query of
   `strategy_window_fills`. Returns False if the row is missing OR
   if it's a `'pending'` placeholder older than
   `STALE_PLACEHOLDER_TTL_SECONDS = 25`.
2. **Step 1** `try_claim_trade` — 15s lease on `window_claims`
   (in-flight only).
3. **Step 5.5** `try_claim_fill_slot` — pessimistic INSERT on
   `strategy_window_fills` with `order_id = 'pending'`.
   `ON CONFLICT DO UPDATE … WHERE order_id = 'pending' AND filled_at
   < NOW() - 25s` — the **stale-takeover** that's the leak.
4. **Step 6** FAK ladder (bounded at `FAK_LADDER_MAX_ELAPSED_S = 20s`).
5. **Step 7** `record_trade` — INSERT into `trades`.
6. **Step 8** `mark_traded` — UPDATE `strategy_window_fills`
   placeholder → real `order_id`.

The 25s TTL was sized for "FAK 20s + 5s buffer" (PR #396 commit msg).
But empirically:

- Pair gaps span 24s to 70s — multiple cases above the TTL even at
  best-case timing.
- The DB write to flip placeholder → real `order_id` happens in
  Step 8, AFTER Step 7's `record_trade`. Under DB-pool saturation
  (10-conn pool, ~3-4 concurrent strategies on a CLOSING tick) the
  write can queue behind other awaits.
- Multiple eval offsets (T-180, T-150, T-120, …) fire on the same
  window via separate `_emit_window_signal` tasks
  (`asyncio.create_task` with cap=4 inflight). Two of these can
  enter `evaluate_all` for the same (strategy, window) seconds apart.

When the second concurrent attempt arrives:

- Registry's `_already_executed` is False (only set on
  `result.success`, which hasn't returned yet).
- `has_filled` returns False (placeholder is stale per the TTL).
- `try_claim_fill_slot` succeeds via stale-takeover.
- FAK fires. Both confirm. Both `record_trade` rows land. Only the
  first `mark_traded` UPDATEs the placeholder to a real order_id —
  the second mark_traded's WHERE clause (`order_id = 'pending'`)
  no-ops because the first won. Result: one `strategy_window_fills`
  row, two `trades` rows.

Some windows in the table above have NO `strategy_window_fills` row
at all — likely both attempts' `mark_traded` were cancelled by
`window_signal_dropped_oldest` after `committed=True` was set,
leaving the placeholder lingering until janitor cleanup (PR #416)
deleted it as orphaned.

## Why `v12_lgb_combo` is NOT in the dupe list

`v12_lgb_combo` is much newer (PR #437 / #438 / commits May 1) and
was deployed under engine `v8.0` after the FAK retry timing
adjustments. The race window has narrowed but not closed — every
strategy that uses the same `ExecuteTradeUseCase` is exposed to the
same TTL leak whenever DB-pool saturation extends FAK + DB-write
latency past 25s.

## Fix

`engine/use_cases/execute_trade.py` — add an in-process
`set[(strategy_id, window_ts, direction)]` to `ExecuteTradeUseCase`.
Acquired at the top of `execute()` BEFORE any DB call; released in a
`finally` block. A second concurrent `execute()` for the same key
returns `already_executing_in_process` immediately.

Properties:

- **No DB round-trip**: cheaper than the existing pessimistic claim,
  fires before any awaitable.
- **Cross-coroutine, single-process**: closes the dominant failure
  mode (concurrent eval offsets in one Montreal engine).
- **Cross-process unaffected**: the DB placeholder remains the
  authoritative cross-process barrier; we don't change its semantics.
- **No new failure mode on engine SIGKILL**: the in-process set is
  tied to the process — a fresh engine starts with an empty set, so
  legitimate retry on the same window after a crash still goes
  through the (now self-healing) DB placeholder.

Tests:

- `test_concurrent_evals_during_in_flight_fak_blocks_second` —
  reproduces the 2026-04-29 production data with `asyncio.gather` and
  a slow FAK; asserts exactly one `execute_order` call.
- `test_sequential_evals_both_proceed_to_dedup_layer` — guarantees
  the set clears between calls and we don't accidentally turn it
  into a permanent per-window lock.
- `test_sibling_strategies_concurrent_each_fire_once` — keys are
  per-strategy, so v9 and v10 don't contend.
- `test_in_flight_set_clears_on_exception` — the `finally` releases
  the key on raised exceptions, so a CLOB outage doesn't lock the
  strategy out of the window.

## Backfill / cleanup

The 10 double-fired pairs are settled on chain. Wallet-level pnl is
already accurate (both rows reflect real fills). No DB cleanup
required — leaving both rows is the most honest representation of
what happened.

For audit completeness we could:

1. Add a `dedup_status: 'double_fire_pre_audit461'` flag to all 20
   trade rows so reports can identify and exclude them when
   computing strategy WR (otherwise the "doubled" win-rate denominator
   inflates).
2. Or accept as one-time loss and lean on PR-#461's tests to prevent
   any new double-fires.

User's call.

## Smoking-gun queries

```sql
-- All same-strategy / same-(window, direction) double-fires in last 14d
SELECT strategy_id,
       metadata->>'dedup_key' AS dk,
       COUNT(*) AS n,
       ARRAY_AGG(id ORDER BY id) AS ids,
       SUM(pnl_usd) AS total_pnl
FROM trades
WHERE created_at > NOW() - INTERVAL '14 days'
  AND metadata->>'dedup_key' IS NOT NULL
GROUP BY strategy_id, metadata->>'dedup_key'
HAVING COUNT(*) > 1
ORDER BY n DESC;

-- Verify each pair has distinct on-chain tx_hashes (= real double-fire)
SELECT id, polymarket_tx_hash
FROM trades
WHERE id IN (5483, 5484, …)
ORDER BY id;
```

## References

- PR #394 (commit `577e8ad`) — per-strategy filled marker (audit #321).
  Fixed the pre-04-26 3x-fill bug on window 1777234800.
- PR #396 (commit `773f680`) — pessimistic fill-slot claim (audit #322).
  Fixed the 04-26 6x-fill bug on window 1777238100.
- PR #401 (audit #401, in `pg_window_repo.py` doc) — stale-pending
  self-recovery. Introduced the 25s TTL escape hatch that this leak
  exploits.
- PR #416 (commit `2052f77`) — periodic janitor for stale pending
  rows. Cleans up leaked placeholders post-mortem; doesn't prevent
  the race.
