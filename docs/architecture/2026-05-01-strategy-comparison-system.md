# Strategy Comparison System — Design Doc

**Date**: 2026-05-01
**Status**: Draft / design review (NO IMPL)
**Author**: Background agent
**Target branch**: `develop`

## Motivation

Every "how is strategy X doing vs Y at T-band Z" question currently spawns a 10-minute background agent that runs ad-hoc SQL on RDS, computes wallet-truth P&L, and writes a one-off `docs/analysis/*.md` file. That is unsustainable:

- Memory `feedback_wallet_truth_authority.md` — DB `pnl_usd` is corrupted (PR #433/#435/#439 forward-fix only). Agents have to re-derive real P&L every time.
- Memory `feedback_payoff_math.md` — the break-even bar is ~70% WR at current fill regimes. We need to see this lens automatically, not derive it.
- Memory `project_strategy_ledger.md` (Hub note #288) — 8+ strategies × 7 T-bands × 4 regimes × 3 directions = 672 cells of interest. SQL each time wastes context.

Goal: **make the comparison a persistent, refreshable surface** — open a Hub page, hit an API, or query a table. No more agent spin-ups.

## Audit — what already exists

| Surface                                  | Status        | Notes                                                                                           |
|------------------------------------------|---------------|-------------------------------------------------------------------------------------------------|
| `v_signal_comparison` (RDS view)         | EXISTS        | Per-window signal-level (v9/v12/blend correctness + dist) — no P&L, no T-band, no aggregates    |
| `strategy_decisions` (RDS table)         | EXISTS        | 874k rows / 24h / 36 strategies — raw fires, no aggregates                                      |
| `strategy_decisions_resolved` (RDS view) | EXISTS        | Joins decisions to outcomes — but no T-band rollup, no real-P&L math                            |
| `signal_evaluations` (RDS table)         | EXISTS        | 24k rows / 24h — per-window signal snapshot                                                     |
| `GET /api/v58/strategy-comparison`       | EXISTS, BROKEN | `hub/api/v58_monitor.py:3954`. Computes ad-hoc per-request. Uses `(1-entry)*4` heuristic, not real P&L. No T-band, no Wilson, no regime filter. Recomputes 874k×N rows on every request. |
| `GET /api/v58/strategy-analysis`         | EXISTS        | `v58_monitor.py:2551`. 30-day overview, single timeframe                                        |
| `GET /api/v58/signal-comparison`         | EXISTS        | `hub/api/margin.py:311`. Margin-strategy focused, not 5-min                                     |
| `GET /api/v58/strategy-decisions`        | EXISTS        | Raw decisions list                                                                              |
| `GET /api/strategies`                    | EXISTS        | Registry only (no perf)                                                                         |
| FE `Strategies.jsx`                      | EXISTS        | YAML param table, derives perf from decisions feed (slow, partial)                              |
| FE `StrategyAnalysis.jsx`                | EXISTS        | 30-day overview cards                                                                           |
| FE `SignalComparison.jsx`                | EXISTS        | Margin-focused, not 5-min strategy lens                                                         |
| FE `polymarket/StrategyCommand.jsx`      | EXISTS        | Consumes `/v58/strategy-comparison`                                                             |
| FE `polymarket/StrategyLab.jsx`          | EXISTS        | Has fallback comment "endpoint may not exist" — explicitly acknowledges current state           |
| Engine periodic aggregation job          | **MISSING**   | No cron / async task computes rollups                                                           |
| `strategy_comparison` table              | **MISSING**   | The persistent rollup itself                                                                    |
| T-band breakdown anywhere                | **MISSING**   |                                                                                                 |
| Real-P&L math anywhere                   | **MISSING**   | Existing endpoint uses `(1-entry)*4` constant-stake heuristic                                   |

**Conclusion**: do NOT build a parallel system. Replace the broken `/api/v58/strategy-comparison` endpoint's compute path with a table read, fed by a new aggregation job. Existing FE consumers (StrategyCommand, StrategyLab, Evaluate, StrategyConfigs) keep working but get better data.

## Recommendation: real table, not materialized view

Three options were considered:

1. **Materialized view** (`REFRESH MATERIALIZED VIEW CONCURRENTLY strategy_comparison`)
   - Pro: declarative, self-healing, query-pure
   - Con: full recompute every refresh; no Wilson interval (Postgres can't do `wilson(wins, n)` natively without a custom func); fixed schema makes parameter changes (T-band edges, period windows) require migration; can't easily store "snapshot at time T" history
2. **Real table populated by an engine job** ← **CHOSEN**
   - Pro: incremental possible, custom math (Wilson, real P&L payoff math), snapshot history for trend lines, change parameters without DDL
   - Con: ~30 lines of aggregation code to maintain
3. **Compute on every request** (status quo)
   - Pro: always fresh
   - Con: 874k rows scanned per page-load is what we're trying to escape

**Choice rationale**: real P&L payoff math (`(1 - fill_price) × (stake / fill_price) − fee` for wins, `−stake` for losses) is non-trivial in pure SQL when `fill_price` and `stake` come from `metadata_json`. Wilson confidence interval needs a UDF or arithmetic. Snapshot history (compare today to yesterday) is free with a table, painful with a matview. Cost of the job is ~36 strategies × 7 T-bands × 4 windows × 3 directions × 4 regimes ≈ 12k rows per snapshot — trivial.

## Schema

`migrations/strategy_comparison.sql`:

```sql
CREATE TABLE IF NOT EXISTS strategy_comparison (
    snapshot_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strategy_id        TEXT        NOT NULL,
    asset              TEXT        NOT NULL DEFAULT 'BTC',
    timeframe          TEXT        NOT NULL DEFAULT '5m',
    window_period      TEXT        NOT NULL,  -- '15h' | '24h' | '7d' | '30d'
    t_band             TEXT        NOT NULL,  -- 'all' | 'T-24-30' | 'T-31-60' | 'T-61-90' | 'T-91-120' | 'T-121-180' | 'T-181-240'
    direction_filter   TEXT        NOT NULL DEFAULT 'all',  -- 'all' | 'UP' | 'DOWN'
    regime_filter      TEXT        NOT NULL DEFAULT 'all',  -- 'all' | 'volatile_trend' | 'chop' | 'calm_trend' | 'risk_off'
    -- counts
    n_fires            INTEGER     NOT NULL,
    n_wins             INTEGER     NOT NULL,
    n_losses           INTEGER     NOT NULL,
    n_pending          INTEGER     NOT NULL,
    -- rates
    wr_pct             NUMERIC(5,2),
    wilson_low         NUMERIC(5,2),
    wilson_high        NUMERIC(5,2),
    -- fills
    avg_fill           NUMERIC(6,4),
    median_fill        NUMERIC(6,4),
    avg_stake_usd      NUMERIC(10,2),
    -- real P&L (wallet-truth math, see use_case below)
    real_net_pnl_usd   NUMERIC(12,2),
    real_pnl_per_fire  NUMERIC(10,2),
    daily_run_rate_usd NUMERIC(12,2),
    -- meta
    PRIMARY KEY (snapshot_at, strategy_id, window_period, t_band, direction_filter, regime_filter)
);

CREATE INDEX IF NOT EXISTS idx_strat_cmp_recent
    ON strategy_comparison (snapshot_at DESC, strategy_id);
CREATE INDEX IF NOT EXISTS idx_strat_cmp_strategy_period
    ON strategy_comparison (strategy_id, window_period, snapshot_at DESC);

-- Convenience view: latest snapshot only
CREATE OR REPLACE VIEW v_strategy_comparison_latest AS
SELECT DISTINCT ON (strategy_id, window_period, t_band, direction_filter, regime_filter) *
FROM strategy_comparison
ORDER BY strategy_id, window_period, t_band, direction_filter, regime_filter, snapshot_at DESC;
```

**Cardinality estimate**: 36 strategies × 4 periods × 7 T-bands × 3 directions × 4 regimes = ~12,096 rows per snapshot. At 5-min cadence over 30d retention ≈ 100M rows total. Add a daily `DELETE FROM strategy_comparison WHERE snapshot_at < NOW() - INTERVAL '30 days'` job (or convert to partitioned table by `snapshot_at::date`).

**Real P&L math** (from `feedback_wallet_truth_authority.md`):
- WIN: `real_pnl = (1 − fill_price) × (stake_usd / fill_price) − POLYMARKET_CRYPTO_FEE_MULT × stake_usd` (fee = 0.072)
- LOSS: `real_pnl = − stake_usd`
- PENDING: excluded from `real_net_pnl_usd`

`fill_price` and `stake_usd` come from `strategy_decisions.fill_price` and `strategy_decisions.metadata_json->>'stake_usd'` (with `fill_size × fill_price` fallback).

## Clean architecture layout

Per repo CLAUDE.md (Domain → Use cases → Adapters → Infrastructure):

```
engine/
  domain/
    strategy_comparison/
      __init__.py
      value_objects.py       # WindowPeriod, TBand, DirectionFilter, RegimeFilter (frozen enums)
      metrics.py             # StrategyMetrics (immutable @dataclass) + Wilson() helper
      entities.py            # StrategyComparison entity (one row per cell)
      pnl_math.py            # real_pnl(fill, stake, win) — SINGLE source of truth
  use_cases/
    compute_strategy_comparison.py   # ComputeStrategyComparison — pure, no IO
    ports/
      strategy_comparison_repo.py    # StrategyComparisonRepoPort (write)
      decisions_query_repo.py        # DecisionsQueryRepoPort (read fires + outcomes)
  adapters/
    persistence/
      pg_strategy_comparison_repo.py # SQLA writer, INSERT...ON CONFLICT DO NOTHING
      pg_decisions_query_repo.py     # SELECT joining strategy_decisions + window_snapshots
  infrastructure/
    schedulers/
      strategy_comparison_scheduler.py  # asyncio loop: every 5min call use case
hub/
  api/
    strategy_comparison.py   # GET /api/strategy-comparison + filters; reads table, no compute
                             # Replace the body of /api/v58/strategy-comparison to read this
                             # table (preserve old route for FE backward-compat).
frontend/
  src/pages/
    StrategyComparison.jsx   # NEW canonical page: sortable table + filter pills + sparkline
                             # Existing Strategies.jsx / StrategyAnalysis.jsx kept; could call
                             # the new endpoint for unified data later.
migrations/
  strategy_comparison.sql    # the DDL above (NOT applied in this PR)
```

**Why a separate scheduler, not engine main loop**: keeps the trading hot path zero-affected. The aggregator is read-mostly; if it crashes, trading continues. Run as `asyncio.create_task` in `infrastructure/composition.py` alongside the existing janitor task (PR #416 pattern).

## API contract

```
GET /api/strategy-comparison
  ?strategy_id=v12_lgb_combo            (optional, default: all)
  &window_period=15h                    (15h | 24h | 7d | 30d, default: 24h)
  &t_band=T-61-90                       (default: all)
  &direction_filter=UP                  (all | UP | DOWN, default: all)
  &regime_filter=volatile_trend         (default: all)
  &snapshot_at=latest                   (latest | <ISO8601>, default: latest)
→ 200 { rows: StrategyComparisonRow[], snapshot_at: <ISO> }

GET /api/strategy-comparison/leaderboard?window_period=15h&top=10&sort=real_net_pnl_usd
→ 200 { rows: StrategyComparisonRow[] (top N) }

GET /api/strategy-comparison/snapshot/latest
→ 200 { snapshot_at: <ISO>, n_strategies: int, computed_in_ms: int }

GET /api/strategy-comparison/history?strategy_id=v12_lgb_combo&hours=24
→ 200 { points: [{ snapshot_at, real_net_pnl_usd, wr_pct, n_fires }] }
  # for sparkline / trend line

# Backward compat (existing FE consumers — Evaluate.jsx, StrategyCommand.jsx, etc.):
# /api/v58/strategy-comparison?days=N keeps responding, but body now reads from the
# table instead of recomputing. Same response shape preserved.
```

## UI sketch — `/strategies/comparison`

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Strategy Comparison                          Snapshot: 2026-05-01 14:35 UTC │
├─────────────────────────────────────────────────────────────────────────────┤
│ Period: [15h] 24h 7d 30d   T-band: [all] T-24-30 T-31-60 T-61-90 ...        │
│ Direction: [all] UP DOWN   Regime: [all] volatile_trend chop calm_trend ... │
├─────────────────────────────────────────────────────────────────────────────┤
│ Strategy           Fires  WR%   Wilson    AvgFill  Net P&L   $/fire  Trend  │
│ ─────────────────  ─────  ───   ────────  ───────  ───────   ──────  ────── │
│ v12_lgb_combo      48     77.1  [62..87]  0.745    +$28.40   +$0.59  ▁▂▃▄▅ │
│ v8_v12_strong      14     85.7  [54..98]  0.71     +$11.20   +$0.80  ▁▃▂▅▅ │
│ v9_basic           212    66.0  [59..72]  0.78     -$31.50   -$0.15  ▅▄▂▁▁ │
│ ...                                                                         │
│                                                                             │
│ Click a strategy row → /strategies/comparison/v12_lgb_combo                 │
│   T-band breakdown bar chart + per-direction split + 24h equity curve       │
└─────────────────────────────────────────────────────────────────────────────┘
```

Cells colour-coded: WR red <55, amber 55-65, green 65+, with break-even dashed at 70 (per `feedback_payoff_math.md`).

## Refresh mechanism

**Chosen**: asyncio task in engine `infrastructure/composition.py`, every 300 s.

```python
async def strategy_comparison_loop(repo, query_repo, use_case, interval=300):
    while True:
        try:
            snapshot = await use_case.execute(now=utcnow())
            await repo.save(snapshot)
        except Exception:
            log.exception("strategy_comparison.tick_failed")
        await asyncio.sleep(interval)
```

**Cost estimate per tick**:
- Read: `SELECT … FROM strategy_decisions sd JOIN window_snapshots ws … WHERE sd.evaluated_at > NOW()-30d` — ~26M rows over 30d, but indexed on `(strategy_id, evaluated_at)` so the planner uses index + nested loop. Practical: ~2-5 s on RDS for 30d window. Most ticks only need delta from last snapshot for incremental aggregation; we do full recompute for simplicity in v1.
- Compute: pure Python over rows, O(n) — ~200 ms for the aggregation buckets at current volume.
- Write: 12k rows per snapshot, single INSERT, ~150 ms.
- Total: ~3-6 s per tick, every 5 min = ~2% engine CPU sustained. Acceptable.

**Alternatives rejected**:
- *systemd timer on Hub box*: Hub doesn't have engine ports / DB write creds in the same path. Avoid.
- *Trigger on `strategy_decisions` insert*: ~25 inserts/sec — would death-spiral.
- *Materialized view + concurrent refresh*: see "table vs matview" above.

## What this PR contains (DESIGN ONLY)

- `docs/architecture/2026-05-01-strategy-comparison-system.md` — this doc
- `migrations/strategy_comparison.sql` — DDL (NOT applied)
- Empty/skeleton files for the 4 layers (signatures, no logic)
- No tests
- No FE page (sketch only — implementation in follow-up)
- Existing endpoints unchanged

## Open questions for review

1. **Retention policy**: 30 days at 5-min cadence ≈ 100M rows. Acceptable, or partition by date? Recommend: partitioned table from day 1.
2. **T-band edges**: I used `T-24-30 / T-31-60 / T-61-90 / T-91-120 / T-121-180 / T-181-240`. Confirm these match the bands used in your shadow analyses? (Hub note #288 implies yes.)
3. **`window_period` set**: `15h / 24h / 7d / 30d`. Add `1h` for live / "right now" view, or stick to longer-window buckets only? Recommend: add `1h`.
4. **Scheduler home**: engine vs hub. I picked engine (already has DB write path, async loop, composition.py). User may prefer hub for separation of concerns. Defer to user.
5. **Real-P&L source of truth**: my `pnl_math.py` will live in `engine/domain/strategy_comparison/`. `wallet_truth.py` (script) computes the same math. Reuse one or duplicate? Recommend: extract `wallet_truth.py`'s math into the new `pnl_math.py`, and `wallet_truth.py` imports it. Single source.
6. **Backward compat of `/api/v58/strategy-comparison`**: existing FE callers (StrategyCommand / StrategyLab / Evaluate / StrategyConfigs) expect the old shape. New endpoint at `/api/strategy-comparison` (no v58 prefix) gets the rich shape; old route is shimmed to read from the new table but emit old fields. Confirm OK.
7. **Per-strategy detail page**: scope creep. Defer to follow-up PR after the table page lands?

## Follow-up PRs (not in this design PR)

1. Implement the use case + repos + scheduler (real code).
2. Apply migration on RDS via Montreal SSH.
3. Backfill 30d of historical rollups (one-shot script).
4. Wire up `/api/strategy-comparison` and shim `/api/v58/strategy-comparison`.
5. Build `frontend/src/pages/StrategyComparison.jsx`.
6. Per-strategy detail page with sparkline + T-band bar chart.
7. Cypress / Playwright smoke test (Railway only — see `feedback_no_local_playwright.md`).
