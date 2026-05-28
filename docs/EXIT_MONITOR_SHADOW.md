# Exit Monitor Shadow Layer

**Status:** Detection-only. No exits executed.  
**Feature flag:** `EXIT_MONITOR_SHADOW_ENABLED=true` (default `false`)  
**Branch:** `feat/exit-monitor-shadow`

---

## Purpose

This is a shadow data collection layer for the future CTF `mergePositions` execution path.

The TickFormer v20 probability signal is a genuine leading indicator: 86% of losing trades show a full probability reversal at a median of 128 seconds before close (per `magic-model/EXIT_MONITOR_FEASIBILITY.md`). The execution path is the bottleneck, not the signal. Polymarket 5-minute YES/NO CLOB markets have a near-empty held-side bid throughout window lifetime ($0.01 stub) — CLOB exits are structurally negative EV.

**This layer banks months of "would-have-exited" markers paired with live CLOB context** so when the CTF `mergePositions` execution path lands, we already have real data to validate thresholds and EV estimates.

This is detection-only. No exits are executed. Negative EV via CLOB is structural per `magic-model/EXIT_MONITOR_FEASIBILITY.md`; this shadow layer banks data for the CTF mergePositions path.

---

## What Is Logged

Each row in `exit_monitor_shadow` represents one threshold crossing for one open trade on one eval tick.

| Column | Description |
|---|---|
| `decision_id` | Loose FK to `strategy_decisions.id` (novakash no-hard-FK convention) |
| `asset` | 'BTC', 'ETH', 'SOL', 'XRP' |
| `window_ts` | 5-minute bar unix timestamp |
| `strategy_id` | e.g. 'tickformer_v18_t180' |
| `side` | 'UP' or 'DN' — the direction the trade is holding |
| `entry_p` | TickFormer P(UP) at trade entry |
| `entry_eval_offset` | seconds-to-close at entry |
| `trigger_eval_offset` | seconds-to-close when shadow exit fired |
| `trigger_threshold` | 0.500, 0.550, 0.600, or 0.650 |
| `p_against_at_trigger` | TickFormer p for the WRONG side at trigger |
| `p_for_at_trigger` | TickFormer p for the held side at trigger |
| `tickformer_model` | 'v18' or 'v20' |
| `clob_best_bid_held` | Live CLOB best bid for the held side (hypothetical exit price) |
| `clob_best_ask_held` | For spread / feasibility analysis |
| `clob_best_bid_against` | Opposite side |
| `clob_best_ask_against` | |
| `clob_book_depth_usd` | Resting liquidity on held bid (NULL — see TODO in adapters/clob_snapshot_reader.py) |
| `realized_outcome` | NULL at trigger; backfilled on window close: 'WIN', 'LOSS', 'PUSH' |
| `realized_pnl_held_to_close` | Net P&L if held to settlement |
| `realized_pnl_shadow_exit` | Net P&L if exited at CLOB bid at trigger time |
| `ev_delta` | GENERATED: `realized_pnl_shadow_exit - realized_pnl_held_to_close`. Positive = shadow exit would have saved money |

### Multi-threshold design

All four thresholds (0.50, 0.55, 0.60, 0.65) are evaluated per tick. Up to 4 rows are written per position per crossing event (one per threshold that fires). This lets you re-tune thresholds post-hoc without re-running the engine.

**First-cross-only:** Each threshold fires at most once per trade lifetime. Repeat ticks above the same threshold don't produce duplicate rows.

### Primary threshold

`p_against >= 0.55` — 77.8% loser recall, 8.2% winner false-exit rate, median 97s lead time (EXIT_MONITOR_FEASIBILITY.md, Table 1). This is the recommended primary threshold for the CTF path when it lands.

---

## How to Enable

Set in the engine's environment:

```bash
EXIT_MONITOR_SHADOW_ENABLED=true
```

Default is `false`. With `false`, the entire code path is gated out — zero overhead.

Apply the migration before enabling:

```bash
psql $DATABASE_URL < migrations/add_exit_monitor_shadow_table.sql
```

---

## Architecture

```
StrategyRegistry.evaluate_all()   (per-tick)
    |
    ├── [existing] position_monitor exit evaluation
    |
    └── [new] run_shadow_monitor()   ← EXIT_MONITOR_SHADOW_ENABLED gate
            |
            ├── read PositionMonitor.get_open_positions()
            ├── ExitSignalDetector.should_shadow_exit()  [pure domain]
            └── ShadowExitGateway.insert_trigger()       [async pg write]

EngineRuntime._poll_position_outcomes()   (window resolution)
    |
    └── run_backfill()   ← EXIT_MONITOR_SHADOW_ENABLED gate
            |
            └── BackfillRealizedOutcomeUseCase.execute()
                    └── ShadowExitGateway.backfill_outcome()
```

### File layout

```
engine/exit_monitor/
├── domain/
│   ├── shadow_trigger.py          # ShadowTrigger value object
│   └── exit_signal_detector.py   # ExitSignalDetector pure domain service
├── use_cases/
│   ├── monitor_open_trades.py     # MonitorOpenTradesUseCase
│   └── backfill_realized_outcome.py  # BackfillRealizedOutcomeUseCase
├── adapters/
│   ├── shadow_exit_gateway.py    # pg INSERT + UPDATE
│   ├── clob_snapshot_reader.py   # FullDataSurface → CLOBSnapshot
│   └── tickformer_prob_reader.py # surface.probability_tickformer_* → P(UP)
└── wireup.py                     # Module-level singleton + engine hooks

migrations/
└── add_exit_monitor_shadow_table.sql

docs/
└── EXIT_MONITOR_SHADOW.md         # this file

scripts/
└── exit_monitor_shadow_rollup.sql # analysis queries
```

---

## Safety Properties

1. **Engine never crashes from monitor errors.** Every hook is wrapped in `try/except`, logged at WARNING, and swallowed.
2. **Feature flag is the kill switch.** `EXIT_MONITOR_SHADOW_ENABLED=false` (default) gates the entire path. No overhead when disabled.
3. **No existing table modifications.** Only `exit_monitor_shadow` is written to.
4. **No exits executed.** The word "exit" in the code refers only to detection — no orders placed, no CTF calls made.
5. **DB pool resolved lazily.** The gateway resolves the asyncpg pool at write time, mirroring the existing `PositionMonitor._resolve_pool()` pattern. Startup-order safe.

---

## Rollup / Analysis

See `scripts/exit_monitor_shadow_rollup.sql` for the full analysis query suite.

### Quick cross-threshold EV delta distribution (after 1 week)

```sql
SELECT
    trigger_threshold,
    COUNT(*)                                          AS trigger_count,
    ROUND(100.0 * COUNT(*) FILTER (WHERE realized_outcome IS NOT NULL)
          / NULLIF(COUNT(*), 0), 1)                  AS pct_backfilled,
    ROUND(AVG(ev_delta)::numeric, 4)                  AS avg_ev_delta,
    ROUND(PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY ev_delta)::numeric, 4) AS p10_ev_delta,
    ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY ev_delta)::numeric, 4) AS p50_ev_delta,
    ROUND(PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY ev_delta)::numeric, 4) AS p90_ev_delta
FROM exit_monitor_shadow
WHERE realized_outcome IS NOT NULL
GROUP BY trigger_threshold
ORDER BY trigger_threshold;
```

### Recall + false-exit rate by threshold

```sql
WITH resolved AS (
    SELECT
        ems.trigger_threshold,
        sd.outcome                                       AS actual_outcome,
        COUNT(*)                                         AS trigger_count
    FROM exit_monitor_shadow ems
    JOIN strategy_decisions sd ON sd.id = ems.decision_id
    WHERE ems.realized_outcome IS NOT NULL
    GROUP BY ems.trigger_threshold, sd.outcome
),
totals AS (
    SELECT
        sd.outcome,
        COUNT(DISTINCT sd.id) AS total_trades
    FROM strategy_decisions sd
    WHERE sd.created_at > NOW() - INTERVAL '7 days'
    GROUP BY sd.outcome
)
SELECT
    r.trigger_threshold,
    r.actual_outcome,
    r.trigger_count,
    t.total_trades,
    ROUND(100.0 * r.trigger_count / NULLIF(t.total_trades, 0), 1) AS pct_caught
FROM resolved r
JOIN totals t ON t.outcome = r.actual_outcome
ORDER BY r.trigger_threshold, r.actual_outcome;
```

---

## Explicit Out-of-Scope Notes

- **No exits executed.** Do not modify this layer to place orders without implementing the CTF `mergePositions` path and running shadow greenlight validation (n ≥ 200 rows, ≥ 60% loser recall, ≤ 8% winner false-exit, median trigger offset ≥ 60s).
- **No CTF mergePositions.** That's a separate PR requiring Polymarket CTF client implementation and testnet validation.
- **CLOB depth unavailable.** `clob_book_depth_usd` is always NULL until the CLOB sidecar table exposes per-side held depth at tick level. See TODO in `adapters/clob_snapshot_reader.py`.
- **decision_id is a surrogate.** The `decision_id` column currently stores a hash-based surrogate (MD5 of strategy_id + window_ts) because `MonitoredPosition` in the existing `PositionMonitor` does not carry the database `strategy_decisions.id`. Thread the real PK through `on_fill()` when convenient — the loose FK convention means there's no hard constraint to migrate.
