# Safe shadow-mining plan — interim until read-replica lands

> Companion to handover doc 2026-05-07. Hub note #359 has the long-term analytics offload (RDS read replica, 2-3hr setup, $300/mo). This doc is the **safe interim** — what to do TODAY, before that lands, when you need to mine alpha cells without endangering the live engine.

---

## Threat model: why naïve shadow mining is dangerous

Prod RDS serves the live trading engine + Hub backend. A shadow-mine query like:

```sql
SELECT direction, t_band, regime, session, dist_bucket,
       COUNT(*), SUM(CASE WHEN actual=predicted THEN 1 ELSE 0 END)
FROM signal_evaluations se JOIN window_snapshots ws USING(window_ts)
WHERE evaluated_at > NOW() - INTERVAL '14 days'
GROUP BY 1,2,3,4,5;
```

…can starve the engine of connections, hold locks, slow trade fills, or page out the RDS buffer cache so subsequent live queries hit disk. This is what hammered RDS yesterday (3 rogue queries, 19+ min stuck).

## Table sizes (verified 2026-05-07)

| Table | Total size | Notes |
|---|---|---|
| `strategy_decisions` | 17 GB | Biggest. Indexed on `(strategy_id, evaluated_at)`. Single-strat queries OK. |
| `window_snapshots` | 5.4 GB | Outcome data (`actual_direction` ⚠️ stale post-Apr-27 per memory). |
| `window_evaluation_traces` | 2.1 GB | JSONB-heavy, slow to scan. Avoid for mining. |
| `signal_evaluations` | **1 GB** | Per-tick predictions. Smallest — best mining target. 14d slice ~500 MB. |
| `strategy_comparison` | 35 MB | Pre-aggregated. Use directly when possible. |
| `trades` | 11 MB | Trivial size. Always safe. |

## Four-tier strategy (use highest tier that satisfies the use case)

### Tier 1 — Use the pre-aggregated `strategy_comparison` table

Already-summarised. ~35 MB. Costs RDS ~zero. Use first; if it doesn't have what you need, escalate.

```sql
-- t_band convention is INVERTED here (sec-from-open via 300-eval_offset)
-- per memory feedback_strategy_comparison_tband_convention.md
SELECT strategy_id, t_band, n_fires, wr, pnl_total
FROM strategy_comparison
WHERE updated_at > NOW() - INTERVAL '6 hours'
ORDER BY pnl_total DESC LIMIT 50;
```

⚠️ Memory says `wr` and `pnl_total` may be NULL pending audit #340 fix. Verify `n_fires` is reliable; the metric columns may be stale.

### Tier 2 — Chunked query with hard timeouts

For ad-hoc queries that must hit raw `signal_evaluations`. Run **one strategy at a time** to use the indexed plan, **chunk by day**, **bound by `statement_timeout`** so a runaway can't camp.

```sql
-- Set BEFORE every analytical session on prod RDS:
SET LOCAL statement_timeout = '30s';
SET LOCAL lock_timeout = '5s';
SET LOCAL idle_in_transaction_session_timeout = '60s';
SET LOCAL work_mem = '64MB';        -- avoid OOM-spilling to disk
SET LOCAL enable_seqscan = off;     -- force index plan; abort if no index

-- Day-by-day chunk (loop in shell). 14 invocations.
SELECT date_trunc('hour', evaluated_at) AS hr,
       direction, vpin_regime,
       WIDTH_BUCKET(ABS(probability_lgb_v9-0.5), 0.10, 0.40, 6) AS dist_bucket,
       COUNT(*) AS n
FROM signal_evaluations
WHERE evaluated_at >= '2026-05-06 00:00:00 UTC'  -- one day at a time
  AND evaluated_at <  '2026-05-07 00:00:00 UTC'
  AND probability_lgb_v9 IS NOT NULL
GROUP BY 1, 2, 3, 4;
```

Run from Montreal `psql` (VPC-local, fastest). NEVER from off-VPC. 5s sleep between chunks. Budget: ~2-3 min per day chunk × 14 days = ~30 min wall, ~10s of cumulative RDS CPU per day.

### Tier 3 — Partial pg_dump → local DuckDB

For multi-day GROUP BY scans without timeouts, dump to local Mac and query in DuckDB (zero infra).

```bash
# On Montreal (VPC-local), single dump of 14d × narrow column set:
PGPASSWORD=$DBP pg_dump \
  -h novakash-pg-prod.cpmisy2asv71.ca-central-1.rds.amazonaws.com \
  -U postgres -d novakash \
  --data-only --format=c \
  -t signal_evaluations \
  -t window_snapshots \
  -t trades \
  --where="evaluated_at > NOW() - INTERVAL '14 days'" \
  > /tmp/mine_14d_$(date +%Y%m%d).dump

# scp back. ~150-300 MB compressed.
scp novakash@15.222.138.228:/tmp/mine_14d_*.dump ~/Downloads/

# DuckDB query (no Postgres server needed locally):
duckdb /tmp/mine.duckdb <<'SQL'
INSTALL postgres; LOAD postgres;
-- Convert pg dump → parquet for fast OLAP
ATTACH 'host=localhost dbname=novakash_local' AS pg (TYPE postgres);
COPY (SELECT * FROM pg.signal_evaluations) TO '/tmp/se.parquet' (FORMAT parquet);
-- ... GROUP BY runs in seconds vs minutes on prod
SQL
```

✅ Bypasses the Hub-box CPU/disk bottleneck (Billy: hub not powerful enough)
✅ pg_dump runs single-pass, low connection footprint (1 conn for dump duration ~2-5 min)
⚠️ Run dump during low-trade hours (02:00-06:00 UTC, fewer market events) for lowest live-engine impact

### Tier 4 — Materialised cell-stats refresh (engineered, recommended next-week)

Build a `cell_stats_daily` materialized view on prod RDS, REFRESH CONCURRENTLY at 04:00 UTC daily. Mining queries hit the matview, never raw tables.

```sql
CREATE MATERIALIZED VIEW cell_stats_daily AS
SELECT
  DATE(evaluated_at) AS bucket_date,
  CASE WHEN probability_lgb_v9 > 0.5 THEN 'UP' ELSE 'DOWN' END AS direction,
  vpin_regime AS regime,
  -- canonical 7-bucket t_band (eval_offset = sec-to-close)
  CASE
    WHEN eval_offset BETWEEN 0   AND 30  THEN 'T-0-30'
    WHEN eval_offset BETWEEN 31  AND 60  THEN 'T-31-60'
    WHEN eval_offset BETWEEN 61  AND 90  THEN 'T-61-90'
    WHEN eval_offset BETWEEN 91  AND 120 THEN 'T-91-120'
    WHEN eval_offset BETWEEN 121 AND 180 THEN 'T-121-180'
    WHEN eval_offset BETWEEN 181 AND 240 THEN 'T-181-240'
    ELSE 'T-241-300'
  END AS t_band,
  -- canonical 7-bucket session
  CASE
    WHEN EXTRACT(hour FROM evaluated_at) BETWEEN 0  AND 2  THEN 'asian_early'
    WHEN EXTRACT(hour FROM evaluated_at) BETWEEN 3  AND 5  THEN 'asian_late'
    WHEN EXTRACT(hour FROM evaluated_at) BETWEEN 6  AND 8  THEN 'eu_open'
    WHEN EXTRACT(hour FROM evaluated_at) BETWEEN 9  AND 11 THEN 'eu_am'
    WHEN EXTRACT(hour FROM evaluated_at) BETWEEN 12 AND 14 THEN 'us_open'
    WHEN EXTRACT(hour FROM evaluated_at) BETWEEN 15 AND 17 THEN 'us_pm'
    ELSE 'us_late'
  END AS session,
  WIDTH_BUCKET(ABS(probability_lgb_v9 - 0.5), 0.10, 0.40, 6) AS dist_bucket_v9,
  WIDTH_BUCKET(ABS(probability_lgb_v12 - 0.5), 0.10, 0.40, 6) AS dist_bucket_v12,
  COUNT(*) AS n,
  -- outcome derived from window_snapshots actual_direction (with regression caveat)
  COUNT(*) FILTER (WHERE ws.actual_direction IS NOT NULL) AS n_resolved,
  COUNT(*) FILTER (WHERE ws.actual_direction = 'UP'   AND probability_lgb_v9 > 0.5) +
  COUNT(*) FILTER (WHERE ws.actual_direction = 'DOWN' AND probability_lgb_v9 < 0.5) AS wins_v9,
  -- ... v12 mirror
FROM signal_evaluations se
LEFT JOIN window_snapshots ws USING (window_ts)
WHERE evaluated_at > NOW() - INTERVAL '30 days'
GROUP BY 1,2,3,4,5,6,7
WITH NO DATA;

CREATE UNIQUE INDEX ON cell_stats_daily (bucket_date, direction, regime, t_band, session, dist_bucket_v9, dist_bucket_v12);

-- Daily refresh cron (04:00 UTC, lowest market activity)
-- REFRESH MATERIALIZED VIEW CONCURRENTLY runs without blocking reads
```

**Cost:** one daily 5-10 min scan at 04:00 UTC. Net WIN — single scan beats N ad-hoc scans.

**File this as audit-task** to implement when bandwidth allows.

## Recommended sequence for IMMEDIATE shadow mining (this week)

1. **First try Tier 1** — query `strategy_comparison` for everything you can.
2. **If that's not enough, Tier 3** — partial pg_dump 14d → local DuckDB. Run dump at 04:00 UTC. ~3 hr total wall (dump + scp + DuckDB grind).
3. Don't use Tier 2 unless you specifically need a single-strategy fast lookup.
4. Don't use raw multi-day GROUP BY queries on prod RDS.

## Standing rules (codify in CLAUDE.md if not already)

- **Always `SET LOCAL statement_timeout = '30s'`** at the top of any psql session against prod RDS for analysis.
- **Always filter by `strategy_id`** when querying `strategy_decisions` — use the `(strategy_id, evaluated_at)` index.
- **Never `SELECT *`** from `window_evaluation_traces` (JSONB column blows up).
- **Run dumps at 04:00-06:00 UTC** when fewer trade windows are live.
- **No ad-hoc query > 5s wall on prod RDS** without explicit Billy ack.

## Long-term: see Hub note #359

Read replica + DuckDB on spot EC2 retires this entire concern. ~$300/mo, 2-3hr setup, all existing scripts unmodified.

---

*Companion: `docs/handover/2026-05-07-billy.md` (session handover) + Hub note #359 (analytics offload long-term plan).*
