# Canonical Methodology — Novakash Engine Operations

> **Single-source-of-truth reference**. Methodology + access recipes + table reference + script index + tricks + traps.
>
> Stop re-discovering this stuff. Cite this doc in every session.
>
> Last updated 2026-05-08. Mirrors Hub note (TBD) + memory entry `reference_canonical_methodology.md`.

---

## 1. Database access — 3 tiers (use highest-tier-that-works)

### Connection identity (canonical)
- **DB:** `novakash-pg-prod.cpmisy2asv71.ca-central-1.rds.amazonaws.com:5432/novakash` (AWS RDS Postgres 16, ca-central-1)
- **Password:** in `engine/.env` on Montreal box (also stamped in memory `reference_canonical_access.md`)
- **Hub box** (`16.54.141.121`, ubuntu) is NOT a DB — it has a stale `novakash_local` clone. Don't use for live data.

### Tier 1 — Hub API (preferred for canned queries)
```python
import httpx
HUB = "http://16.54.141.121:8091"   # direct, NOT nginx 99.79.41.246
TOKEN = httpx.post(f"{HUB}/auth/login",
                   json={"username":"billy","password":"novakash2026"},
                   timeout=15).json()["access_token"]
h = {"Authorization": f"Bearer {TOKEN}"}
```
Endpoints: `/api/notes` (CRUD, no per-id GET — use list+filter), `/api/audit-tasks` (POST 500-broken — use direct INSERT), `/api/strategy-decisions`, `/api/trades`, `/api/system/status`, `/api/system/{kill,resume,paper-mode}`.

### Tier 2 — Montreal SSH + psql (fastest, VPC-local)
```bash
# EC2 Instance Connect — 60s key TTL, regen each session
ssh-keygen -t rsa -b 2048 -f /tmp/ec2ic_key -N "" -q -y || \
  (rm -f /tmp/ec2ic_key /tmp/ec2ic_key.pub && \
   ssh-keygen -t rsa -b 2048 -f /tmp/ec2ic_key -N "" -q)
aws ec2-instance-connect send-ssh-public-key \
  --region ca-central-1 \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user novakash \
  --availability-zone ca-central-1b \
  --ssh-public-key file:///tmp/ec2ic_key.pub
ssh -i /tmp/ec2ic_key -o StrictHostKeyChecking=no novakash@15.222.138.228 'CMD'
```
- IP `15.222.138.228` (with user `ubuntu` or `novakash`) AND `15.223.247.178` (with `novakash`) both work — same instance `i-0785ed930423ae9fd`, AZ `ca-central-1b`.
- Hub box key (separate): `/tmp/novakash-infra-v2.pem` for `ubuntu@16.54.141.121`.

```bash
# Run SQL on Montreal (VPC-local, fastest path):
ssh -i /tmp/ec2ic_key novakash@15.222.138.228 \
  'PGPASSWORD="GuZbKkezYSzX4qGTxnkTynH0AIJy" psql \
    -h novakash-pg-prod.cpmisy2asv71.ca-central-1.rds.amazonaws.com \
    -U postgres -d novakash -f /tmp/your_query.sql'
```

### Tier 3 — pg_dump → local DuckDB (for heavy analytics)
For multi-day GROUP BY / cross-tab. **Single dump = single 5-15 min RDS connection** (predictable, low engine impact).
```bash
# Run on Montreal (VPC). 14d slice ~400-700 MB compressed.
PGPASSWORD="$DBP" pg_dump -Fc --data-only \
  -h novakash-pg-prod.cpmisy2asv71.ca-central-1.rds.amazonaws.com \
  -U postgres -d novakash \
  -t signal_evaluations -t market_data -t trades -t strategy_decisions \
  -t strategy_runtime_overrides \
  -t ticks_v3_composite -t ticks_coinglass -t ticks_gamma -t ticks_timesfm \
  -t ticks_v2_probability -t ticks_chainlink -t ticks_tiingo -t ticks_binance \
  -t ticks_clob \
  --where="evaluated_at > NOW() - INTERVAL '14 days'" \
  > /tmp/canonical_14d_$(date +%Y%m%d).dump

# scp to Mac
scp novakash@15.222.138.228:/tmp/canonical_14d_*.dump ~/Downloads/

# DuckDB (no Postgres server needed):
duckdb /tmp/mine.duckdb <<'SQL'
INSTALL postgres; LOAD postgres;
ATTACH 'host=localhost dbname=novakash_mining' AS pg (TYPE postgres, READ_ONLY);
COPY (SELECT * FROM pg.signal_evaluations) TO '/tmp/se.parquet' (FORMAT parquet);
SQL
```

### Standing rules — prod RDS hygiene
- **ALWAYS** `SET LOCAL statement_timeout = '30s'` at top of analytical sessions.
- **ALWAYS** filter `strategy_decisions` by `strategy_id` (uses index).
- **NEVER** `SELECT *` from `window_evaluation_traces` (JSONB blowup).
- Dumps run **02:00-06:00 UTC** (low-trade-volume window).
- No ad-hoc query > 5s wall on prod RDS without explicit Billy ack.
- See `~/.claude/CLAUDE.md` "NEVER hang prod RDS with heavy analytical queries".

---

## 2. Canonical RDS schema (verified 2026-05-08)

### Primary tables
| Table | Size | Role | priceToBeat-aware? |
|---|---|---|---|
| `strategy_decisions` | **17 GB** | Engine decision log (skip_reason, action, confidence). Indexed `(strategy_id, evaluated_at)`. | n/a |
| `window_snapshots` | 5.4 GB | Engine **gate evaluations** — **NOT canonical** for v9.1 features (audit #391). `actual_direction` only **22-23% resolved** (audit #398). | partial |
| `window_evaluation_traces` | 2.1 GB | JSONB-heavy traces. Slow. AVOID for mining. Useful: `surface_json->>'probability_lgb_v12'` (sparse fallback). | partial |
| **`signal_evaluations`** | **1 GB** | **Canonical training corpus** — action history + 30+ inline features + `delta_*`. Best mining target. | **YES (post-PR #464)** |
| **`market_data`** | 67 MB | **Canonical priceToBeat source** — `open_price`, `close_price`, `outcome`, `resolved`. ⚠️ `open_price` 100% NULL until PR #501 (audit #395). | **YES** |
| `strategy_comparison` | 35 MB | Pre-aggregated cell stats. ⚠️ `wr` + `pnl_total` NULL pending audit #340 fix. | n/a |
| `trades` | 11 MB | Real orders. ⚠️ `pnl_usd` 51% inflated WIN-side (audit #399). Use fill-math. | n/a |
| `strategy_runtime_overrides` | 96 KB | Live config. Edits picked up <60s. | n/a |
| `audit_tasks_dev` | 520 KB | Audit task store. **POST broken — direct INSERT only** (memory `feedback_hub_audit_tasks_500.md`). | n/a |

### Sidecar tick tables (joined via `training/queries.py:143-146` LATERAL pattern)
| Table | Size | Family | Status |
|---|---|---|---|
| `ticks_binance` | 888 MB | `binance_price`, `vpin` | ✅ live |
| `ticks_v3_composite` | 500 MB | `v3_*` (composite_score, elm/cascade/taker/oi/funding/vpin/momentum signals, cascade_strength/tau1/exhaustion) | ✅ live |
| `ticks_gamma` | 373 MB | `gamma_*` (up/down_price, slug, token_ids) | ✅ live |
| `ticks_tiingo` | 219 MB | `tiingo_close` (multi-exchange bid/ask) | ✅ live |
| `ticks_clob` | 114 MB | `clob_*` (bid/ask, spread, mid_price) | ✅ live |
| `ticks_chainlink` | 103 MB | `chainlink_price` | ✅ live |
| `ticks_coinglass` | 70 MB | `cg_*` (oi/liq/long-short/taker/funding) | ✅ live |
| `ticks_elm_predictions` | 25 MB | legacy ELM | mostly unused |
| **`ticks_v2_probability`** | **40 KB** | LGB v12 features (jsonb) | 🚨 **0 rows on RDS** — audit #396, fixed in PR #501 |
| **`ticks_timesfm`** | **32 KB** | TimesFM tfm_* derived | 🚨 **0 rows on RDS** — audit #397, fixed in PR #501 |

### Convention traps (READ THIS BEFORE QUERYING)
- **`eval_offset` = sec-to-close (T-minus)**. Per memory `reference_eval_offset_verified.md`, `engine/data/feeds/polymarket_5min.py:288-330`. eval_offset=24 = 24s before close. Engine + YAML + `signal_evaluations` all use this.
- **`strategy_comparison.t_band` is INVERTED** (sec-from-open via `300 - eval_offset`). Per memory `feedback_strategy_comparison_tband_convention.md`. Document both axes when cross-referencing.
- **DB is RDS now** (post Track-B cutover ~2026-04-30). Memory `feedback_db_is_rds.md`. Local Mac `.env` may point at stale Railway — verify with `grep DATABASE_URL` first.

---

## 3. P&L math — fill-aware breakeven (CRITICAL)

DB `trades.pnl_usd` is **structurally unreliable**:
- WIN side ~51% inflated (audit #399, residual after PR #433)
- LOSS side correct
- Reconciler partial-fill regressions

**Always use fill-math:**
```python
# 7.2% Polymarket crypto fee
WIN_PNL  = (1 - fill_price) * (stake_usd / fill_price) - 0.072 * stake_usd
LOSS_PNL = -stake_usd
```

**Breakeven WR by fill** (memory `feedback_payoff_math.md`):
| fill | breakeven WR |
|---|---|
| 0.50 | 53.6% |
| 0.60 | 65.2% |
| 0.70 | 75.4% |
| 0.74 | 76.4% |
| 0.78 | 80.0% |
| 0.82 | 83.4% |

**Source of truth for "how much money do I have":** `scripts/ops/wallet_truth.py` on Montreal. Cross-checks 2 RPCs + on-chain USDC + redemption tally.

---

## 4. Cell taxonomy (5D)

Canonical buckets per `engine/services/cell_bucketing.py`:

**direction** (2): UP / DOWN
**t_band** (7) — eval_offset = sec-to-close:
- T-0-30 / T-31-60 / T-61-90 / T-91-120 / T-121-180 / T-181-240 / T-241-300

**regime** (3): CASCADE / TRANSITION / NORMAL (vpin); also `v4_regime` (chop / volatile_trend / risk_off / calm_trend) from external TimesFM

**session** (7) by hour_utc:
- asian_early (0-2) / asian_late (3-5) / eu_open (6-8) / eu_am (9-11) / us_open (12-14) / us_pm (15-17) / us_late (18-23)

**conviction (dist)**: `abs(probability - 0.5)`. Buckets 0.05 wide from 0.10 to 0.50.

**Sample-size floors** (Wilson 95% LB):
- n ≥ 30: ratify-flip OK
- 20 ≤ n < 30: exploratory only
- n < 20: defer to next mine

---

## 5. Alpha-mining script suite (`scripts/ops/analysis/`)

Restored 2026-05-08 from commit 44586f33. Built 2026-05-01 to replace one-off bg-agent runs (~10 min each → reusable).

| Script | Purpose |
|---|---|
| `_common.py` | RDS conn, Wilson CI calc, fill-math P&L, T-band bucketer, quintile helper, markdown renderer |
| `signal_alpha_scanner.py` | Quintile sweep every numeric col + bool/categorical scan. Single-signal alpha cells (T-band × regime × dir). |
| `gate_gain_loss.py` | For each baseline gate-set, ADD/REMOVE candidate gates. WR + P&L delta. |
| `combo_miner.py` | Greedy 2-way (optional 3-way) AND-combos of atomic gates. Wilson_low > 0.6 surface. |
| `regime_cascade_specialist.py` | CASCADE drilldown — within-regime predictors + CG-liquidation buckets. |
| `coinglass_direct_alpha.py` | Does CG carry RAW directional alpha independent of LGB? |
| `time_decay_analyzer.py` | Per-signal accuracy decay across T-bands. ASCII charts. |
| `v9_alpha_analysis.sql` | One-off SQL for v9 LGB pure-signal alpha (CASCADE×DOWN sweet spot). |

Run from Montreal: `python3 scripts/ops/analysis/<name>.py --hours 87`.

⚠️ Scripts not yet on develop (live in commit 44586f33 on `design/strategy-comparison-system`). PR #448 ("alpha-mining suite") still OPEN — could close as superseded once landed via PR.

---

## 6. Operational scripts (`scripts/ops/`)

| Script | Purpose | When |
|---|---|---|
| **`wallet_truth.py`** | Canonical P&L (USDC + pUSD + redemption tally + on-chain) | **Every P&L claim** |
| `shadow_analysis.py` | Per-strategy / regime / conviction WR | Strategy review |
| `strategy_pnl_24h.py` | 24h trade summary by strategy | Quick check |
| `check_pending.py` | Pending fills inspection | Order recon |
| `cleanup_pending_fills.py` | Clear stuck pending orders | Order recon |
| `reconcile_orphan_trades.py` | Orphan reconciliation | When trade table drifts |
| `onchain_redeem.py` | Direct on-chain redemption (bypasses 100/day Builder Relayer cap) | Wins not auto-redeemed |
| `wrap_usdc_pusd.py` / `rewrap_stakes_only.py` | USDC ↔ pUSD wrapping | Capital management |
| `withdraw_usdc.py` | USDC withdrawal | Outflows |
| `backfill_*.py` / `backfill_*.sql` | Various backfills (PnL, redeemed flag, oracle outcomes, v8 outcomes, lgb_v12, clob) | One-off ops |

---

## 7. Common queries (paste-ready)

### Wallet + 9h trade breakdown
```sql
SET statement_timeout = '20s';
SELECT
  strategy_id, direction, COUNT(*) AS n,
  SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins,
  SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) AS losses,
  ROUND(100.0 * SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*),0), 1) AS wr,
  ROUND(SUM(CASE WHEN outcome='WIN' THEN (1-fill_price)*(stake_usd/NULLIF(fill_price,0))-0.072*stake_usd
                 WHEN outcome='LOSS' THEN -stake_usd END)::numeric, 2) AS pnl_correct
FROM trades
WHERE created_at > NOW() - INTERVAL '9 hours' AND fill_price > 0 AND stake_usd > 0
GROUP BY 1, 2 ORDER BY n DESC;
```

### Current LIVE overrides
```sql
SELECT strategy_id, jsonb_pretty(params)
FROM strategy_runtime_overrides
WHERE strategy_id IN ('v12_lgb_combo','v9_1_lgb_only','v_v12_extreme_dn_btc_5m','v9_2_super_lgb_only')
ORDER BY strategy_id;
```

### Active cell pauses
```sql
SELECT strategy_id, direction, t_band, regime, paused_at, pause_until - NOW() AS remaining
FROM cell_pauses WHERE released_at IS NULL ORDER BY paused_at DESC;
```

### Writer-regression diagnostic (run weekly)
```sql
SET statement_timeout = '30s';
-- Sidecar freshness — alert if any > 1h stale
SELECT relname, NOW() - max_ts AS staleness FROM (
  SELECT 'ticks_v2_probability' AS relname, MAX(ts) AS max_ts FROM ticks_v2_probability
  UNION ALL SELECT 'ticks_timesfm',     MAX(ts) FROM ticks_timesfm
  UNION ALL SELECT 'ticks_v3_composite',MAX(ts) FROM ticks_v3_composite
  UNION ALL SELECT 'ticks_coinglass',   MAX(ts) FROM ticks_coinglass
  UNION ALL SELECT 'ticks_gamma',       MAX(ts) FROM ticks_gamma
  UNION ALL SELECT 'ticks_chainlink',   MAX(ts) FROM ticks_chainlink
  UNION ALL SELECT 'ticks_clob',        MAX(ts) FROM ticks_clob
  UNION ALL SELECT 'ticks_tiingo',      MAX(ts) FROM ticks_tiingo
  UNION ALL SELECT 'ticks_binance',     MAX(ts) FROM ticks_binance) z
WHERE NOW() - max_ts > INTERVAL '1 hour';

-- Outcome write health
SELECT to_timestamp(window_ts)::date AS day, COUNT(*) AS rows,
       COUNT(actual_direction) AS resolved,
       ROUND(100.0 * COUNT(actual_direction)::numeric / COUNT(*), 1) AS pct
FROM window_snapshots
WHERE window_ts > EXTRACT(EPOCH FROM NOW() - INTERVAL '5 days')::bigint
GROUP BY 1 ORDER BY 1 DESC;

-- pnl_usd inflation check (LOSS side correct, WIN ~50% inflated as of audit #399)
SELECT outcome, COUNT(*) AS n,
  ROUND(AVG(pnl_usd)::numeric, 2) AS avg_db,
  ROUND(AVG(CASE WHEN outcome='WIN' THEN (1-fill_price)*(stake_usd/NULLIF(fill_price,0))-0.072*stake_usd
                 WHEN outcome='LOSS' THEN -stake_usd END)::numeric, 2) AS avg_correct
FROM trades WHERE created_at > NOW() - INTERVAL '7 days' AND fill_price > 0 AND stake_usd > 0
GROUP BY outcome;
```

### Window stack-aggregate (find -$60+ position-aggregate hits)
```sql
SELECT
  (EXTRACT(EPOCH FROM created_at)::bigint / 300) * 300 AS window_ts,
  direction,
  COUNT(*) AS n_strats,
  SUM(stake_usd) AS total_stake,
  STRING_AGG(strategy_id, ',') AS strategies
FROM trades
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY 1, 2 HAVING COUNT(*) > 1
ORDER BY total_stake DESC LIMIT 10;
```

---

## 8. Known traps (memory-confirmed)

| Trap | What | Memory ref |
|---|---|---|
| `eval_offset` direction | sec-to-close, NOT sec-from-open | `reference_eval_offset_verified.md` |
| `strategy_comparison.t_band` direction | INVERTED (sec-from-open) | `feedback_strategy_comparison_tband_convention.md` |
| Per-tick n inflation | per-tick rows ~88x per-window. Use DISTINCT ON for first-fire dedup | `reference_per_tick_methodology.md` |
| `signal_evaluations.probability_lgb_v9` | Doesn't exist — column is `probability_lgb_v12` only | (verified today) |
| `signal_evaluations.vpin_regime` | Column doesn't exist on this table — use `strategy_decisions` or `window_snapshots` | (verified today) |
| `trades.pnl_usd` WIN side | 51% inflated — fill-math required | `feedback_wallet_truth_authority.md` + audit #399 |
| `window_snapshots.actual_direction` | 22-23% fill rate (resolution writer broken) | audit #398 |
| `window_snapshots.clob_*_ask` | 19% fill rate | audit #336 / #400 |
| `market_data.open_price` (priceToBeat) | 100% NULL until PR #501 | audit #395 |
| `ticks_v2_probability` | 0 rows on RDS until PR #501 | audit #396 + Hub #366 |
| `ticks_timesfm` | 0 rows on RDS until PR #501 (engine had `timesfm_enabled=False`) | audit #397 + Hub #366 |
| Schema A→B regression | Engine push-mode emits 25/70 features (cg_*, gamma_*, tiingo_*, tfm_*, vol_*, ret_* all dropped) | audit #401 + Hub #366 |
| Hub `/api/audit-tasks` POST | 500-broken — direct INSERT into `audit_tasks_dev` | `feedback_hub_audit_tasks_500.md` |
| `signal_evaluations.probability_lgb_v12` sparseness | Only 34 rows total since 2026-05-01 — sister regression to ticks_v2_probability | (verified today) |
| GH Actions deploy | Silently rolls back on health-gate fail. Use `scripts/ops/deploy_engine_manual.sh` | `feedback_gh_deploy_silent_rollback.md` |
| Polygon RPC cache lag | Single-RPC misled twice. Cross-check ≥2 RPCs OR use `wallet_truth.py` | `feedback_cross_rpc_required.md` |
| `cell_pauses` partial unique idx | `WHERE released_at IS NULL` — UPSERT pattern | (PR #494) |

---

## 9. Critical Hub notes (canonical research)

| ID | Title | Use |
|---|---|---|
| #354 | Per-cell block predicates 7d cross-tab + projected impact | **Granular t_band × conf bleed map** |
| #350 | Alpha mining + auto-pause gate spec 2026-05-06 | **Session-band bleed/alpha** |
| #347 | Strategy lineup audit + conviction symmetry + ghost candidates | **Direction × regime asymmetries** |
| #345 | vpin_regime + v4_regime filter on v12_combo + v9_1 (48h) | **Regime cell map** |
| #348 | Consolidated 2026-05-06: chainlink staleness + 7d signal map + Tier 0/1/2 plan | **Tier plan + signal map** |
| #353 | Postmortem: 4 training pipeline bugs (iso schema + identity-iso + sidecar split + NaN stub) | **Calibration regressions** |
| #366 | Engine action plan — Schema A→B + sidecar writer breakage | **Writer-regression postmortem** |
| #361 | Safe shadow-mining plan v2 (canonical) | **4-tier safety framework** |
| #359 | Analytics offload stack — design + recommendation | **Long-term replica plan** |
| #357 / #358 | Handover 2026-05-07 + access reference | **Handover** |
| #372 | Block_cells refinement 14d cell-mining 2026-05-08 | **Cell refinement v1** |
| #375 | Stake-up + relaxation cell mining 2026-05-08 | **Amp cells (4) + Q2 relaxation deferred** |
| #376 | Action plan + runbook 2026-05-08 (PR-A SQL + Tier 3 runbook + daily ops) | **Live action plan** |

---

## 10. PR ledger (LIVE-touching, recent)

| PR | Title | State | Notes |
|---|---|---|---|
| #497 | per-cell block predicates | MERGED | block_cells gate |
| #494 | hour-blocks + source agreement + cell-pause | MERGED | RollingWRMonitor |
| #495 | wire RollingWRMonitor → reconciler | MERGED | |
| #496 | PR #494 follow-ups | MERGED | session bucketing |
| #493 | cell-size scaler + replay validator | MERGED | |
| #501 | writer regressions 395+396+397 | **DRAFT — DO NOT MERGE** | priceToBeat, v2_probability, timesfm |
| #499 | v9_2_super_lgb_only canary | MERGED | GHOST, depends on #501 + #401 |
| #498 | handover 2026-05-07 doc | OPEN draft | this doc lives here |
| #448 | alpha-mining suite | OPEN | scripts now restored to handover branch |
| #473 | v9_1 + v12 floors recalibration | OPEN — close as superseded | |
| #389 | window_snapshots v4 cols | OPEN | low priority |
| #444 | backfill use market_slug not condition_id | OPEN | bugfix |

---

## 11. Engine restart + deploy

```bash
# Manual deploy (bypass GH Actions silent-rollback)
bash scripts/ops/deploy_engine_manual.sh --dry-run    # always dry-run first
bash scripts/ops/deploy_engine_manual.sh

# Restart on Montreal (rotates engine.log, SIGTERM→SIGKILL→start, verifies 1 PID)
ssh -i /tmp/ec2ic_key novakash@15.222.138.228 \
  'bash /home/novakash/novakash/scripts/restart_engine.sh'

# Tail logs
ssh -i /tmp/ec2ic_key novakash@15.222.138.228 'tail -f /home/novakash/engine.log'
```

---

## 12. Daily/weekly ops sequence

### Daily (post any config change, or at-shift-start)

```bash
# 1. Wallet truth (canonical P&L)
ssh -i /tmp/ec2ic_key novakash@15.222.138.228 \
  'cd /home/novakash/novakash && python3 scripts/ops/wallet_truth.py 2>&1 | tail -120'

# 2. Active cell pauses (auto-pause history)
ssh -i /tmp/ec2ic_key novakash@15.222.138.228 \
  'PGPASSWORD="<DBP>" psql -h novakash-pg-prod.cpmisy2asv71.ca-central-1.rds.amazonaws.com \
    -U postgres -d novakash -c \
    "SELECT strategy_id, direction, t_band, regime, paused_at, pause_until - NOW() AS remaining
     FROM cell_pauses WHERE released_at IS NULL ORDER BY paused_at DESC;"'

# 3. Today trade summary (fill-math corrected)
# Use query in §7, scoped to last 24h.

# 4. Engine alive + log spot check
ssh -i /tmp/ec2ic_key novakash@15.222.138.228 \
  'ps -ef | grep "python3 main.py" | grep -v grep | head -3; \
   tail -50 /home/novakash/engine.log'
```

### Weekly

- `scripts/ops/shadow_analysis.py` — cell-level WR drift detection
- Sidecar writer freshness (query in §7)
- Wallet_truth.py 7d total — cross-check vs DB pnl_usd
- Audit-task triage: `SELECT id, title, severity, status FROM audit_tasks_dev WHERE status='OPEN' ORDER BY priority DESC LIMIT 20;`

### Pre-config-flip safety procedure

ALWAYS before applying SQL UPDATEs to `strategy_runtime_overrides`:

1. Backup current state:
   ```sql
   CREATE TABLE IF NOT EXISTS strategy_runtime_overrides_backup_$(date +%Y_%m_%d) AS
   SELECT * FROM strategy_runtime_overrides
   WHERE strategy_id IN (<targeted strats>);
   ```
2. Apply UPDATE in single transaction (BEGIN ... COMMIT)
3. Verify with `SELECT jsonb_pretty(params) WHERE strategy_id = ...`
4. Watch first 24h: wallet_truth.py 4h cadence, drawdown threshold ≥ -$200/24h triggers rollback
5. Rollback template:
   ```sql
   BEGIN;
   UPDATE strategy_runtime_overrides s SET params = b.params
   FROM strategy_runtime_overrides_backup_<date> b WHERE s.strategy_id = b.strategy_id;
   COMMIT;
   ```

---

## 13. Update protocol for this doc

When you discover a new convention trap / writer regression / data-source quirk:
1. Add a row to **§8 Known traps** + cite memory file
2. If it's a methodology — add to **§3-7** as appropriate
3. If new Hub note matters — append to **§9**
4. Push to `chore/handover-2026-05-07-billy` (or successor handover branch) + update Hub note + memory entry

This doc is the source of truth. Memory entries link **here** rather than duplicating content.
