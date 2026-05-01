# Forward outcome writer fix — 2026-05-01

Follow-up to `2026-04-30-outcome-backfill.md` (hub note 297). The backfill
caught up to ~50.7% of historical windows, but the **forward writer** was
still broken — every new window since the regression continued to land in
the DB with `outcome = NULL`. This document captures the diagnosis and the
PR that fixes the forward path so backfills become a one-time recovery
exercise rather than a recurring chore.

## TL;DR

The engine had **two distinct forward-writer bugs** masquerading as one
"outcome reconciler regression":

1. **Bug A — `WIN`/`LOSS` polluting `window_snapshots.outcome`.**
   `engine/execution/order_manager.py` called
   `DBClient.update_window_outcome` with `resolved.outcome` (the order
   `WIN`/`LOSS` label) in the slot meant for the directional `UP`/`DOWN`/
   `FLAT` label. The column took the wrong values; downstream queries
   filter on `outcome IN ('UP','DOWN','FLAT')` and silently dropped them.
   Net effect: 729 polluted rows + every analysis script reporting NULL
   for traded windows.

2. **Bug B — `signal_evaluations.outcome` had no writer at all.**
   No engine code path ever wrote this column. 100% NULL on every row
   inserted post-2026-04-08, and `v_signal_comparison` (the new shadow-
   strategy comparison view) returned 0 rows because both inputs to the
   join were NULL.

## Tables affected

| Table                | Column   | Type            | Pre-fix state                         |
|----------------------|----------|-----------------|---------------------------------------|
| `window_snapshots`   | `outcome`| `varchar(4)`    | 100% NULL on 2026-05-01; 729 polluted |
| `signal_evaluations` | `outcome`| `text`          | 100% NULL on 2026-05-01               |
| `market_data`        | `outcome`| `varchar(4)`    | Healthy (writer in `data-collector`)  |
| `trades`             | `outcome`| `varchar(8)`    | Healthy (`WIN`/`LOSS` — correct here) |

NULL rate on `window_snapshots.outcome` by day on Montreal RDS as of
2026-05-01 06:55 UTC (pre-fix snapshot):

```
2026-05-01  total= 7848 null=7848 (100.0%)
2026-04-30  total=28032 null=7681 ( 27.4%)   <- backfill ran ~17:30 UTC
2026-04-29  total=28272 null= 989 (  3.5%)
2026-04-28  total=18095 null= 872 (  4.8%)
2026-04-27  total=30034 null=2065 (  6.9%)
2026-04-26  total=27034 null=24723 (91.5%)
…
2026-04-15  total=22818 null=22818 (100.0%)
…
2026-04-08  total= 8756 null=8698 ( 99.3%)   <- regression starts
2026-04-07  total=  893 null= 731 ( 81.9%)
```

## Root cause

`engine/persistence/db_client.py::update_window_outcome` is called from
`engine/execution/order_manager.py::poll_resolutions` with these args:

```python
await self._db.update_window_outcome(
    window_ts, asset, tf,
    resolved.outcome,            # 'WIN' or 'LOSS'  ← wrong type!
    resolved.pnl_usd or 0.0,
    poly_winner,                 # 'Up' or 'Down'   ← the real direction
)
```

The implementation then wrote `resolved.outcome` directly into the
`outcome` column. The column is `varchar(4)`, so `WIN` (3) and `LOSS` (4)
both fit and the UPDATE succeeded. No exception. No telemetry. The
analysis side filtered on `outcome IN ('UP','DOWN','FLAT')` and silently
dropped 729 rows; the rest of post-Apr-8 was NULL because the
`shadow_resolution_loop` (which knows `oracle_outcome` for skipped
windows) never wrote to `outcome` either.

`signal_evaluations.outcome` was simpler: `git log -S "UPDATE
signal_evaluations"` returns zero commits across the entire repo
history. The column was added but never wired to a writer.

## Fix scope

PR `fix/forward-outcome-writer` against `develop`:

1. **`DBClient.update_window_outcome`**:
   - New `_coerce_directional_outcome(outcome, poly_winner)` helper
     returns `UP`/`DOWN`/`FLAT` or `None`. `poly_winner` wins; `WIN`/
     `LOSS` without a `poly_winner` returns `None` (silently dropped —
     never pollutes the column again).
   - SQL is now `COALESCE(outcome, $1)` (and same for `pnl_usd` /
     `poly_winner`) so re-deliveries cannot overwrite a real resolution.
   - Error path now logs `window_ts`/`asset`/`timeframe` so the next
     regression is visible in `docker logs`.

2. **`DBClient.update_signal_evaluations_outcome`** (new method):
   - Bulk UPDATE of every `signal_evaluations` row for `(window_ts,
     asset, timeframe)`, gated by `outcome IS NULL`.
   - Returns row count.

3. **`DBClient.update_shadow_resolution`**:
   - Now also writes `outcome` (derived from `oracle_outcome`) wrapped
     in `COALESCE`. Skipped windows now self-fill the canonical column.

4. **`engine/execution/order_manager.py`**:
   - After `update_window_outcome`, calls
     `update_signal_evaluations_outcome` with the derived directional
     label.

5. **`engine/infrastructure/runtime.py`**:
   - `_shadow_resolution_loop` calls
     `update_signal_evaluations_outcome` after a successful oracle
     resolve.
   - The secondary `prediction_resolution` sweep (Polymarket scan for
     unresolved `window_predictions`) now also fills
     `window_snapshots.outcome` and `signal_evaluations.outcome`.

6. **`engine/adapters/persistence/pg_window_repo.py`**:
   - Mirrored changes (this file documents itself as
     "byte-for-byte parity with `db_client.py`").

7. **`engine/tests/unit/persistence/test_outcome_writer.py`** (new):
   - 21 cases covering the coercion logic, COALESCE wrapping,
     idempotency, and the new `signal_evaluations` writer.

## Verification

Pre-deploy (local, against `develop` HEAD):

```
$ pytest engine/tests/unit/persistence/test_outcome_writer.py -v
21 passed in 0.25s
```

Post-deploy verification (will be done after rsync):

* Take 5 most recent windows with `trade_placed=TRUE` from last 30 min
  → `outcome IN ('UP','DOWN','FLAT')` and `pnl_usd` non-NULL.
* Take 5 most recent windows with `trade_placed=FALSE` (shadow-only)
  → `outcome` populated within 5 min of resolution (after the
  shadow_resolution_loop runs).
* `signal_evaluations.outcome` for all rows tied to those windows is
  non-NULL.
* `v_signal_comparison` view returns rows.

## Backfill

`scripts/ops/backfill_outcomes.py` is unchanged and remains idempotent.
Re-run after deploy to clean up any windows that fell into the gap
between the previous backfill (2026-04-30 17:30 UTC) and the writer
deploy. Expected result: a few hundred windows newly filled by the
`trade` and `wsnap_oracle` paths.

The 729 polluted `WIN`/`LOSS` rows in `window_snapshots.outcome` are
left as-is; they sit between the `IN ('UP','DOWN','FLAT')` filter and a
direct re-resolve via the backfill (which skips non-NULL). If we want
them cleaned up later, an explicit `UPDATE window_snapshots SET
outcome = NULL WHERE outcome IN ('WIN','LOSS')` followed by
`backfill_outcomes.py --execute` will recover them — out of scope for
this PR.

## Constraints honoured

* Engine is LIVE — no semantic change to any column type or new schema
  required. Pure forward-writer wiring + COALESCE.
* Manual deploy via `scripts/ops/deploy_engine_manual.sh` (the GitHub
  Actions deploy-engine.yml has a silent rollback bug).
* Unit tests gate the writer; the next refactor that breaks
  WIN/LOSS-vs-direction semantics will fail loudly.
