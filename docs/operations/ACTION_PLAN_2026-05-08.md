# Action plan + runbook — 2026-05-08

> Writeup of corrected 3-strat refinement plan after #372 + #354 + #375 mining.
> Mirror: Hub note (TBD). Canonical methodology: `docs/operations/CANONICAL_METHODOLOGY.md` + Hub #373.

---

## TL;DR

3 strategies × 2 lever-types (block + amp) = ratify-ready PR-A. **+$1,422/14d est** = +$101/day.
Tier 3 relaxation analysis deferred to Billy's local desktop dump (runbook in §4 below).
v_v12_extreme deferred ~18d (sample too thin).

| Strat | Block | Amp | Relax | $/14d |
|---|---|---|---|---|
| v12_lgb_combo | +$1,004 | +$82 | (PR-B) | **+$1,086** |
| v9_1_lgb_only | +$311 | +$25 | (Tier 3 → PR-B) | **+$336** |
| v_v12_extreme_dn | NO | NO (n<10) | — | **$0** |
| **Total ratify-ready** | | | | **+$1,422/14d** |

---

## §1 — PR-A ratify-ready SQL (runtime-only, no engine restart)

Single transaction. Engine picks up <60s. Rollback = restore current configs (saved before apply).

```sql
BEGIN;

-- ──── 1. SAVE current state (rollback safety) ────────────────────
CREATE TABLE IF NOT EXISTS strategy_runtime_overrides_backup_2026_05_08 AS
SELECT * FROM strategy_runtime_overrides
WHERE strategy_id IN ('v12_lgb_combo','v9_1_lgb_only','v_v12_extreme_dn_btc_5m');

-- ──── 2. v12_lgb_combo ────────────────────────────────────────────

-- Replace block_cells with #372 + #354 narrow predicates
UPDATE strategy_runtime_overrides
SET params = params 
  || jsonb_build_object('block_cells', jsonb_build_array(
       -- #354 t_band × conf bleed (preventable: -$526/14d)
       jsonb_build_object('direction','UP',  't_band','T-121-180','conf_min',0.25,'conf_max',0.35),
       jsonb_build_object('direction','DOWN','t_band','T-61-90',  'conf_min',0.45),
       jsonb_build_object('direction','DOWN','t_band','T-31-60',  'conf_min',0.45),
       jsonb_build_object('direction','DOWN','t_band','T-91-120', 'conf_min',0.35,'conf_max',0.45),
       jsonb_build_object('direction','UP',  't_band','T-61-90',  'conf_min',0.25,'conf_max',0.35),
       -- #372 narrow additions (+$243/14d)
       jsonb_build_object('direction','DOWN','regime','TRANSITION'),  -- +$95
       -- #350 session-band bleed (+$148)
       jsonb_build_object('direction','UP','session','asian_early'),
       jsonb_build_object('direction','UP','session','us_pm')
     ))
  || '{"blocked_utc_hours_down": [0, 1, 9, 15, 16, 17, 18, 19]}'::jsonb  -- #372: +[0,1,15] to existing
  || jsonb_build_object('cell_size_multipliers', jsonb_build_object(
       'up:T-91-120',   1.3,  -- #375: 70.8% WR n=65
       'down:T-91-120', 1.5,  -- #375: 80.0% WR n=35
       'up:T-61-90',    1.3   -- #375: 71.4% WR n=21 (exploratory)
     ))
WHERE strategy_id = 'v12_lgb_combo';

-- ──── 3. v9_1_lgb_only ───────────────────────────────────────────

UPDATE strategy_runtime_overrides
SET params = params
  || jsonb_build_object('block_cells', jsonb_build_array(
       -- #354 t_band × conf bleed (preventable: -$192/14d)
       jsonb_build_object('direction','DOWN','t_band','T-31-60',  'conf_min',0.45),
       jsonb_build_object('direction','DOWN','t_band','T-61-90',  'conf_min',0.45),
       jsonb_build_object('direction','DOWN','t_band','T-121-180','conf_min',0.45),
       -- #372 narrow additions (+$119)
       jsonb_build_object('direction','DOWN','conf_min',0.25)  -- anti-predictive high-conf
     ))
  || jsonb_build_object('cell_size_multipliers', jsonb_build_object(
       'down:T-121-180', 1.5  -- #375: 83.0% WR n=47
     ))
WHERE strategy_id = 'v9_1_lgb_only';

-- ──── 4. v_v12_extreme_dn_btc_5m: NO CHANGES ────────────────────
-- Per Hub #354: no bleed cells found.
-- Per Hub #347: CASCADE block self-defeating (CASCADE is its alpha).
-- Per Hub #375: 24 trades/14d, per-cell n<10. Below ratify floor.
-- Re-mine ~2026-05-26 when sample size accumulates.

-- ──── 5. Verify ──────────────────────────────────────────────────
\echo === AFTER ===
SELECT strategy_id, jsonb_pretty(params) FROM strategy_runtime_overrides
WHERE strategy_id IN ('v12_lgb_combo','v9_1_lgb_only','v_v12_extreme_dn_btc_5m')
ORDER BY strategy_id;

COMMIT;
```

### Rollback (if engine bleeds post-apply)

```sql
BEGIN;
UPDATE strategy_runtime_overrides s
SET params = b.params
FROM strategy_runtime_overrides_backup_2026_05_08 b
WHERE s.strategy_id = b.strategy_id;
COMMIT;
```

### Verify pickup
- 30-60s after COMMIT, engine reloads runtime overrides
- Look for log line `runtime_override.refresh.applied strategy=v12_lgb_combo`
- Or query `strategy_runtime_overrides.updated_at` matches your COMMIT time

---

## §2 — Cell counts (sample-size guardrails)

For each predicate, `n` from #354 + #372 + #375. Wilson_LB ≥ floor for action:
- n ≥ 30: ratify (95% confidence)
- 20 ≤ n < 30: exploratory (flagged in SQL comments)
- n < 20: defer

Two cells exploratory in PR-A:
- v12 UP T-61-90 conf 0.25-0.35 amp 1.3x (n=21, Wilson_LB 56.4%)
- v9_1 risk_off+CASCADE+UP block (n=18, Wilson_LB borderline) — **drop from PR-A if Billy wants strict floor**

---

## §3 — Watchpoints first 24h post-apply

1. **wallet_truth.py 4h cadence**: -$200/24h drawdown = pause + investigate
2. **cell_pauses table**: any auto-pause = investigate the cell, do not auto-resume
3. **Strategy WR by direction**:
   ```sql
   SELECT strategy_id, direction, COUNT(*) AS n,
     ROUND(100.0*SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END)::numeric/COUNT(*), 1) AS wr
   FROM trades WHERE created_at > '<COMMIT_TS>' GROUP BY 1,2 ORDER BY n DESC;
   ```
4. **Stake-up firing check**: confirm `cell_size_multipliers` is ACTUALLY scaling — query trades.stake_usd > 20 (will be ~$26-30 on amped cells at 1.3x-1.5x of $20 base)

---

## §4 — Tier 3 dump runbook (Billy's local desktop)

You said you have a local db dump on your desktop. Use it for **Q2 just-below-gate relaxation analysis**.

### What to look for
- v9_1 UP cells at dist 0.17-0.21 (currently blocked by `lgb_dist_min_up=0.21`)
- v9_1 DOWN cells at dist 0.15-0.20 (currently blocked by `lgb_dist_min_down=0.20`)
- v12_combo all dist <0.17 (blocked by `combo_min_dist=0.17` floor)

These cells have **0 rows in trades** (never fired). Use `window_evaluation_traces.surface_json->>'probability_lgb_v9'` (or `_v12`) joined to `market_data.outcome` for SHADOW WR.

### Methodology (run on local DuckDB or Postgres)

```sql
-- Setup: load the dump into local PG or DuckDB
-- (DuckDB simpler — no server, just `duckdb local.duckdb` and ATTACH the dump)

-- Q2.1 — v9_1 UP just-below-gate (dist 0.17-0.21)
WITH se_pred AS (
  SELECT
    wet.window_ts,
    (wet.surface_json->>'probability_lgb_v9')::float AS prob_v9,
    ABS((wet.surface_json->>'probability_lgb_v9')::float - 0.5) AS dist_v9,
    -- Predicted direction (UP if prob > 0.5)
    CASE WHEN (wet.surface_json->>'probability_lgb_v9')::float > 0.5 THEN 'UP' ELSE 'DOWN' END AS pred_dir,
    -- t_band (eval_offset = sec-to-close)
    CASE
      WHEN wet.eval_offset BETWEEN 0   AND 30  THEN 'T-0-30'
      WHEN wet.eval_offset BETWEEN 31  AND 60  THEN 'T-31-60'
      WHEN wet.eval_offset BETWEEN 61  AND 90  THEN 'T-61-90'
      WHEN wet.eval_offset BETWEEN 91  AND 120 THEN 'T-91-120'
      WHEN wet.eval_offset BETWEEN 121 AND 180 THEN 'T-121-180'
      WHEN wet.eval_offset BETWEEN 181 AND 240 THEN 'T-181-240'
      ELSE 'T-241-300'
    END AS t_band,
    md.outcome,
    md.open_price,
    md.close_price
  FROM window_evaluation_traces wet
  JOIN market_data md ON md.window_ts = wet.window_ts
  WHERE wet.evaluated_at > NOW() - INTERVAL '14 days'
    AND wet.surface_json->>'probability_lgb_v9' IS NOT NULL
    AND md.resolved = true
    AND md.outcome IS NOT NULL
)
SELECT
  pred_dir,
  t_band,
  CASE
    WHEN dist_v9 < 0.05 THEN '[0.00-0.05)'
    WHEN dist_v9 < 0.10 THEN '[0.05-0.10)'
    WHEN dist_v9 < 0.15 THEN '[0.10-0.15)'
    WHEN dist_v9 < 0.20 THEN '[0.15-0.20)'
    WHEN dist_v9 < 0.25 THEN '[0.20-0.25)'
    WHEN dist_v9 < 0.30 THEN '[0.25-0.30)'
    ELSE '[0.30+)'
  END AS dist_bucket,
  COUNT(*) AS n,
  SUM(CASE WHEN pred_dir = outcome THEN 1 ELSE 0 END) AS correct,
  ROUND(100.0 * SUM(CASE WHEN pred_dir = outcome THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS wr,
  -- Wilson 95% LB
  ROUND(100.0 * (
    (SUM(CASE WHEN pred_dir = outcome THEN 1 ELSE 0 END)::float / COUNT(*) + 1.96*1.96/(2*COUNT(*))
     - 1.96*SQRT((SUM(CASE WHEN pred_dir = outcome THEN 1 ELSE 0 END)::float * (COUNT(*) - SUM(CASE WHEN pred_dir = outcome THEN 1 ELSE 0 END)::float) / COUNT(*) + 1.96*1.96/4) / COUNT(*)) / COUNT(*))
    / (1 + 1.96*1.96/COUNT(*))
  )::numeric, 1) AS wilson_lb
FROM se_pred
WHERE pred_dir = 'UP'
  AND dist_v9 BETWEEN 0.13 AND 0.25  -- just below + above current 0.21 gate
GROUP BY 1, 2, 3
HAVING COUNT(*) >= 20
ORDER BY pred_dir, t_band, dist_bucket;
```

Mirror this query for:
- `pred_dir = 'DOWN'` AND `dist BETWEEN 0.10 AND 0.22`
- `probability_lgb_v12` (replace v9 with v12 throughout)

### Identify candidates
For each row in result:
- WR ≥ 70% AND Wilson_LB ≥ 60% AND n ≥ 30 → **relaxation candidate**
- WR < 55% AND n ≥ 30 → confirmed bleed (validates current dist gate)

### Output format for Billy → Claude
Save results as CSV. Send back. I'll synthesize into PR-B spec.

```
strategy,direction,t_band,dist_bucket,n,wr,wilson_lb,recommendation
v9_1,UP,T-121-180,[0.18-0.20),52,73.1,61.2,RELAX_TO_0.18
...
```

### Local DuckDB quick-start

```bash
# If dump is .dump (custom format from pg_dump -Fc):
pg_restore --list /path/to/dump  # see contents
createdb novakash_local
pg_restore -d novakash_local -j 4 /path/to/dump
psql novakash_local -f q2_relaxation.sql > q2_results.csv

# Or if dump is plain SQL:
psql novakash_local < /path/to/dump.sql

# Or DuckDB direct (no Postgres needed):
duckdb local.duckdb
> INSTALL postgres; LOAD postgres;
> ATTACH '/path/to/dump' AS pg (TYPE postgres);
> COPY (SELECT * FROM pg.window_evaluation_traces WHERE evaluated_at > NOW() - INTERVAL '14 days') TO 'wet.parquet' (FORMAT parquet);
> -- Now query the parquet with full DuckDB speed
> .read q2_relaxation.sql
```

---

## §5 — PR-B engineering work (parking lot)

Code changes needed for relaxation past current dist floor:

1. **`unblock_cells` predicate** (mirror inverse of `block_cells`)
   - Predicate matched → BYPASS other gates (hour-block, regime-block, dist-min)
   - Risk: defeats safety. Need explicit ordering.

2. **Per-cell parameter override map** (cleaner)
   - `param_overrides_by_cell: { "DOWN:T-181-240:CASCADE": {"lgb_dist_min": 0.15} }`
   - Reads at gate-evaluation time
   - Simpler than unblock_cells

3. **Time-of-day param refresh cron** (audit #380)
   - Different params per UTC hour
   - Less granular than 5D cells

PR-B deferred until Q2 results ratify which cells are real.

---

## §6 — Memory + Hub linkage

| Where | Content |
|---|---|
| **Hub #373** | Canonical methodology (master ref) |
| **Hub #374** (TBD) | This action plan + Tier 3 runbook |
| **Hub #372** | Block_cells refinement 14d cell mining (5 narrow blocks) |
| **Hub #375** | Stake-up + relaxation cell mining (4 amp cells, Q2 deferred) |
| **Hub #354** | Per-cell t_band × conf 7d cross-tab (canonical bleed/alpha map) |
| **Hub #350** | Session-band bleed/alpha + auto-pause spec |
| **Hub #347** | Strategy lineup audit + conviction symmetry |
| **Hub #361** | Safe shadow-mining 4-tier framework |
| Repo | `docs/operations/CANONICAL_METHODOLOGY.md` + `docs/operations/ACTION_PLAN_2026-05-08.md` |
| Memory | `reference_canonical_methodology.md` |

---

## §7 — Daily ops runbook (post-apply)

Once daily:
```bash
# 1. Wallet truth (canonical P&L)
ssh -i /tmp/ec2ic_key novakash@15.222.138.228 \
  'cd /home/novakash/novakash && python3 scripts/ops/wallet_truth.py 2>&1 | tail -120'

# 2. Active cell pauses
ssh ... 'PGPASSWORD=... psql ... -c "SELECT strategy_id, direction, t_band, regime, paused_at, pause_until - NOW() AS remaining FROM cell_pauses WHERE released_at IS NULL ORDER BY paused_at DESC;"'

# 3. Today's trade summary (with corrected fill-math)
ssh ... 'PGPASSWORD=... psql ... -f /tmp/daily_trade_summary.sql'

# 4. Sidecar writer freshness (catch new regressions)
# (query in CANONICAL_METHODOLOGY.md §7)

# 5. Engine alive + log spot check
ssh ... 'ps -ef | grep "python3 main.py" | head -3; tail -50 /home/novakash/engine.log'
```

Weekly:
- `scripts/ops/shadow_analysis.py` — cell-level WR drift detection
- Re-run #354 cross-tab — check if predicates still hold
- Wallet_truth.py 7d total — settle vs DB pnl_usd discrepancy
