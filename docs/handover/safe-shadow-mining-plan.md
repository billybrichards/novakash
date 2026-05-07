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

## Canonical RDS schema (verified 2026-05-07)

**Connection:** `novakash-pg-prod.cpmisy2asv71.ca-central-1.rds.amazonaws.com:5432/novakash`

### Primary tables — analytics + outcomes

| Table | Size | Role | priceToBeat-aware? |
|---|---|---|---|
| `strategy_decisions` | **17 GB** | Engine decision log (skip_reason, action, confidence). Biggest table. Indexed on `(strategy_id, evaluated_at)`. | n/a |
| `window_snapshots` | 5.4 GB | Engine **gate evaluations** — **NOT canonical** for v9.1 features (per audit #391). `actual_direction` stale post-Apr-27. | partial |
| `window_evaluation_traces` | 2.1 GB | JSONB-heavy traces. Slow to scan. AVOID for mining. | partial |
| **`signal_evaluations`** | **1 GB** | **Canonical training corpus** — action history + 30+ inline features + `delta_*`. **Best mining target.** 14d slice ~500 MB. | **YES (post-PR #464)** |
| **`market_data`** | 67 MB | **Canonical priceToBeat source** — `open_price`, `close_price`, `outcome`, `resolved`. Use this for outcomes, not window_snapshots.actual_direction. | **YES** |
| `strategy_comparison` | 35 MB | Pre-aggregated cell stats. Use first if WR/PnL columns aren't NULL (audit #340). | n/a |
| `trades` | 11 MB | Real orders + `pnl_usd` (inflated — use `wallet_truth.py` canonical). | n/a |
| `strategy_runtime_overrides` | 96 KB | Live config (kelly, floors, offsets, mode). | n/a |
| `audit_tasks_dev` | 520 KB | Audit task store. POST endpoint broken — direct INSERT only. | n/a |

### Sidecar tick tables (joined via `training/queries.py` LATERAL joins)

| Table | Size | Feature family | Key columns |
|---|---|---|---|
| `ticks_binance` | **888 MB** | binance_price, vpin | `quantity`, `is_buyer_maker` |
| `ticks_v3_composite` | 500 MB | `v3_*` | `composite_score`, `elm_signal`, `cascade_signal`, `taker_signal`, `oi_signal`, `funding_signal`, `vpin_signal`, `momentum_signal`, `cascade_strength`, `cascade_tau1`, `cascade_exhaustion` |
| `ticks_gamma` | 373 MB | `gamma_*` | `up_price`, `down_price`, `slug`, `up_token_id`, `down_token_id` |
| `ticks_tiingo` | 219 MB | `tiingo_close` | bid/ask/last prices |
| `ticks_clob` | 114 MB | `clob_*` | orderbook bid/ask, `spread`, `mid_price` |
| `ticks_chainlink` | 103 MB | `chainlink_price` | `round_id`, `updated_at` |
| `ticks_coinglass` | 70 MB | `cg_*` | `oi_usd`, `oi_delta_pct`, `liq_long_usd`, `liq_short_usd`, `long_pct`, `short_pct`, `taker_buy_usd`, `taker_sell_usd`, `funding_rate`, `long_short_ratio`, `top_position_ratio` |
| `ticks_elm_predictions` | 25 MB | (legacy ELM, mostly unused) | `probability_up` |
| `ticks_v2_probability` | **40 KB** ⚠️ | `lgb_v12` | `model_version`, `probability_up`, `probability_raw`, `features` (jsonb) — **suspiciously tiny, may be sparse/recent** |
| `ticks_timesfm` | **32 KB** ⚠️ | derived `tfm_*` | `tfm_tail_risk`, `tfm_skew`, `tfm_spread_norm`, `tfm_quantile_ratio` (per `queries.py:143-146`) — **suspiciously tiny** |

**Sidebar total:** ~2.3 GB raw across 10 sidecar tables. Compressed 14d dump = ~400-700 MB.

⚠️ `ticks_v2_probability` (40 KB) and `ticks_timesfm` (32 KB) sizes are alarming — either (a) they're freshly-created and only have recent data, or (b) writer regression. **File audit-task to investigate.** v9.1 retrains may be silently feature-starved if these are broken.

### Canonical join pattern (per `training/queries.py`)

```sql
-- Per-tick training row = signal_evaluations LEFT LATERAL each sidecar
-- joined on (window_ts, evaluated_at proximity)
SELECT se.*,
       v3.composite_score, v3.elm_signal, v3.cascade_strength, ...,
       cg.oi_usd, cg.liq_long_usd, cg.taker_buy_usd, ...,
       gamma.up_price, gamma.down_price,
       tfm.tfm_tail_risk, tfm.tfm_skew, ...,
       v12.probability_up AS lgb_v12,
       cl.chainlink_price, ti.tiingo_close, b.binance_price, b.vpin,
       clob.clob_mid_price, clob.clob_spread,
       md.open_price AS pricetobeat,    -- canonical priceToBeat
       md.close_price, md.outcome
FROM signal_evaluations se
LEFT JOIN LATERAL (SELECT * FROM ticks_v3_composite     WHERE ... ORDER BY ts DESC LIMIT 1) v3   ON true
LEFT JOIN LATERAL (SELECT * FROM ticks_coinglass        WHERE ... ORDER BY ts DESC LIMIT 1) cg   ON true
LEFT JOIN LATERAL (SELECT * FROM ticks_gamma            WHERE ... ORDER BY ts DESC LIMIT 1) gamma ON true
LEFT JOIN LATERAL (SELECT * FROM ticks_timesfm          WHERE ... ORDER BY ts DESC LIMIT 1) tfm  ON true
LEFT JOIN LATERAL (SELECT * FROM ticks_v2_probability   WHERE ... ORDER BY ts DESC LIMIT 1) v12  ON true
LEFT JOIN LATERAL (SELECT * FROM ticks_chainlink        WHERE ... ORDER BY ts DESC LIMIT 1) cl   ON true
LEFT JOIN LATERAL (SELECT * FROM ticks_tiingo           WHERE ... ORDER BY ts DESC LIMIT 1) ti   ON true
LEFT JOIN LATERAL (SELECT * FROM ticks_binance          WHERE ... ORDER BY ts DESC LIMIT 1) b    ON true
LEFT JOIN LATERAL (SELECT * FROM ticks_clob             WHERE ... ORDER BY ts DESC LIMIT 1) clob ON true
JOIN market_data md ON md.window_ts = se.window_ts        -- canonical priceToBeat + outcome
WHERE se.evaluated_at > NOW() - INTERVAL '14 days';
```

**This pattern is EXPENSIVE on prod RDS** — 11 LATERAL subqueries × ~5M rows. **NEVER run against prod.** Always pg_dump → DuckDB.

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

### Tier 3 — Partial pg_dump → local DuckDB (FULL canonical corpus)

For multi-day mining + retrain corpus assembly. Dumps **all canonical analytics tables** (primary + sidecar) for 14d slice. Compressed ~400-700 MB. Restore + DuckDB-grind on Mac.

```bash
# === Run on Montreal (VPC-local) ===
TS=$(date +%Y%m%d_%H%M)
DUMP_FILE=/tmp/canonical_14d_${TS}.dump

PGPASSWORD="$DBP" pg_dump \
  -h novakash-pg-prod.cpmisy2asv71.ca-central-1.rds.amazonaws.com \
  -U postgres -d novakash \
  --data-only --format=c --jobs=2 \
  -t signal_evaluations \
  -t market_data \
  -t trades \
  -t strategy_decisions \
  -t strategy_runtime_overrides \
  -t ticks_v3_composite \
  -t ticks_coinglass \
  -t ticks_gamma \
  -t ticks_timesfm \
  -t ticks_v2_probability \
  -t ticks_chainlink \
  -t ticks_tiingo \
  -t ticks_binance \
  -t ticks_clob \
  --where="evaluated_at > NOW() - INTERVAL '14 days'"  \
  > "$DUMP_FILE"

# Note: --where applies per-table by column — only signal_evaluations has evaluated_at.
# For tick tables, use a wrapper script that does per-table date filtering via TS column.
# Alternative: pg_dump separately per table with table-specific --where.

# scp to Mac
scp novakash@15.222.138.228:/tmp/canonical_14d_*.dump ~/Downloads/

# Restore to local Postgres@15 (Postgres@15 client is forward-compat with PG16 server dumps)
createdb novakash_mining
pg_restore -d novakash_mining -j 4 ~/Downloads/canonical_14d_*.dump

# Or skip Postgres entirely — DuckDB reads PG dumps via the postgres extension:
duckdb /tmp/mine.duckdb <<'SQL'
INSTALL postgres; LOAD postgres;
ATTACH 'host=localhost dbname=novakash_mining user=postgres' AS pg (TYPE postgres, READ_ONLY);
-- Materialize hot tables to parquet for 10x faster mining queries
COPY (SELECT * FROM pg.signal_evaluations) TO '/tmp/se.parquet' (FORMAT parquet);
COPY (SELECT * FROM pg.market_data)        TO '/tmp/md.parquet' (FORMAT parquet);
COPY (SELECT * FROM pg.trades)             TO '/tmp/tr.parquet' (FORMAT parquet);
-- Now mine away on parquet (DuckDB columnar, blazing fast)
.read /tmp/shadow_mine_query.sql
SQL
```

✅ Bypasses Hub-box constraint entirely (Billy: hub box not powerful enough)
✅ Single dump = single 5-15 min RDS connection (low live-engine impact)
✅ All canonical tables in one dump → ad-hoc cell-mining + retrain corpus from same artefact
⚠️ Dump must run 02:00-06:00 UTC (low-trade-volume window)
⚠️ For per-tick-table date filtering: wrap each `pg_dump -t TABLE --where="ts > ..."` in its own call, concatenate dumps. Sample script: `scripts/ops/canonical_dump.sh` (TBD).

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
2. **If that's not enough, Tier 3** — full canonical pg_dump 14d (signal_evaluations + market_data + 10 sidecar tables) → local DuckDB. Run dump at 04:00 UTC. ~3 hr total wall (dump 5-15 min + scp 5-10 min + restore 10-20 min + DuckDB grind on parquets).
3. Don't use Tier 2 unless you specifically need a single-strategy fast lookup. It hits prod RDS directly.
4. Don't use raw multi-day GROUP BY queries on prod RDS — especially the LATERAL join pattern from `training/queries.py`.

## Two follow-up audits to file

1. **`ticks_v2_probability` (40 KB) and `ticks_timesfm` (32 KB) sizes are alarming.** Either freshly-created or writer regression. v9.1 retrains may be silently feature-starved if these are broken. Compare row counts vs `signal_evaluations` (1 GB / millions of rows) to confirm.
2. **Materialised cell-stats matview (Tier 4)** as a daily 04:00 UTC refresh. Single scan beats N ad-hoc scans. ~5-10 min RDS impact/day.

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
