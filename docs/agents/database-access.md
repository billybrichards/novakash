# Database access — agent/operator runbook

**Owner:** billy
**Status:** draft v1 (2026-04-20)
**Primary branch target:** `develop`
**Related:** `docs/agents/redemption-ops-agent.md`, audit-task #255 (pg SELECT grants for `novakash` user)

Self-contained guide for connecting to Novakash's databases, running safe read-only queries, and understanding what lives where. Intended for cold-start Paperclip/Claude/human — no prior context required.

---

## 0. Hard rules

1. **No writes without explicit operator approval per statement.** Read-only by default. Even `UPDATE WHERE id=1` needs an approving message in the session.
2. **No DDL.** No `CREATE`, `ALTER`, `DROP`, `TRUNCATE`. Schema changes go through the migrations folder + PR, never ad-hoc.
3. **No `DELETE FROM … WHERE true`-style full-table wipes.** If a cleanup is ever needed, bound it with `LIMIT` + operator sign-off.
4. **No exposing `DATABASE_URL` beyond Montreal.** The full credential string lives in `/home/novakash/novakash/engine/.env` only. Pulling it down to laptops = leak.
5. **Always `SELECT` a LIMIT when exploring.** `SELECT … LIMIT 100` not bare `SELECT *`. A `strategy_decisions` full scan = 500K+ rows.
6. **Don't run aggregate queries without indexes while engine is trading.** A `GROUP BY metadata->>'dedup_key'` over 48h on un-indexed columns locks hot rows. Audit #255 F2 shipped the needed GIN/B-tree indexes — verify they exist before heavy queries (`\di+ strategy_decisions` on psql).
7. **Never log query output verbatim to Hub notes/Telegram if it contains `metadata` blobs** — they include tokens, slugs, and occasionally order hashes. Summarise, don't dump.

---

## 1. Where each DB lives

| DB | Host | Purpose | Access |
|---|---|---|---|
| Railway Postgres | `hopper.proxy.rlwy.net:35772` (example; exact host in `.env`) | Primary: `trades`, `strategy_decisions`, `signal_evaluations`, `ticks_*`, `wallet_snapshots`, `outcomes`, `window_traces` | Full URL in `engine/.env` on Montreal |
| Hub Postgres (if separate) | same as Railway (single-DB setup as of 2026-04-20) | Notes, audit tasks, user table | same URL |
| SQLite test fixtures | `engine/tests/fixtures/*.db` | Unit test isolation | Checked into repo |

As of 2026-04-20 everything is **one Railway Postgres**. No separate Hub DB. Confirm via `echo $DATABASE_URL` on Montreal.

---

## 2. Getting a connection — Montreal (preferred path)

Everything you want to query lives in Railway pg, credentials baked into `engine/.env` on Montreal. SSH in (see `redemption-ops-agent.md` §3 for Instance Connect bootstrap), then:

```bash
ssh -i /tmp/ec2ic_key -o StrictHostKeyChecking=no novakash@15.223.247.178 << 'SH'
cd /home/novakash/novakash
set -a && source engine/.env && set +a
psql "$DATABASE_URL" -c "\dt"
SH
```

### Interactive psql via SSH
```bash
ssh -i /tmp/ec2ic_key -o StrictHostKeyChecking=no -t novakash@15.223.247.178 \
  'cd /home/novakash/novakash && . engine/.env && psql "$DATABASE_URL"'
```

`-t` forces a pty. Exit with `\q` as usual. `\x` for expanded output. `\d table` to describe.

### One-shot query from outside SSH
```bash
ssh -i /tmp/ec2ic_key -o StrictHostKeyChecking=no novakash@15.223.247.178 \
  'cd /home/novakash/novakash && . engine/.env && psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM trades WHERE created_at > NOW() - INTERVAL \"1 hour\";"'
```

Note: escape inner quotes carefully. Easier path is to write the SQL to a file, scp it up, run it there.

### Saving SQL + running it
```bash
# On your host:
cat > /tmp/q.sql <<'SQL'
SELECT strategy_id, COUNT(*) AS n, SUM(pnl_usd) AS pnl
FROM trades
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY 1 ORDER BY 3 DESC;
SQL

# Push + run:
scp -i /tmp/ec2ic_key /tmp/q.sql novakash@15.223.247.178:/tmp/q.sql
ssh -i /tmp/ec2ic_key novakash@15.223.247.178 \
  'cd /home/novakash/novakash && . engine/.env && psql "$DATABASE_URL" -f /tmp/q.sql'
```

---

## 3. Local psql (tunnelling, only if Montreal path is too slow)

Last resort. Montreal already has network proximity to Railway pg (both US-East adjacent); going via your laptop adds latency.

### SSH tunnel
```bash
# Push key first (60s window), then:
ssh -i /tmp/ec2ic_key -o StrictHostKeyChecking=no \
  -L 5433:hopper.proxy.rlwy.net:35772 \
  -N novakash@15.223.247.178 &
TUNNEL_PID=$!

# Now pg is reachable at localhost:5433
psql "postgresql://novakash:<password>@localhost:5433/railway"

# Clean up when done
kill $TUNNEL_PID
```

**Do NOT copy DATABASE_URL to your laptop.** Grab just the username + password from it (SSH in, `grep '^DATABASE_URL' engine/.env | sed 's/.*:\/\///; s/@.*//'`), use those for the tunnel.

### Read-only pg user (`novakash`)
Audit #255 F6 granted `SELECT` on `strategy_decisions`, `trades`, `signals`, `signal_evaluations`, `ticks_chainlink`, `ticks_tiingo`, `outcomes`, `window_traces` to a read-only role `novakash`. Use this role for analysis work, not the full-access engine role.

```bash
# Verify grants
psql "$DATABASE_URL" -c "\dp strategy_decisions"
```

---

## 4. Schemas you actually want

### 4.1 `trades` — actual fills + outcomes
```sql
\d trades
```

Key columns:
| Column | Type | Notes |
|---|---|---|
| `id` | bigint | PK |
| `strategy_id` | text | `v6_sniper`, `v4_fusion`, etc. |
| `market_slug` | text | e.g. `btc-updown-5m-1776697800` — suffix = unix close ts |
| `direction` | text | `UP` / `DOWN` |
| `fill_price` | numeric | per-token, 0-1 |
| `fill_size` | numeric | tokens acquired |
| `stake_usd` | numeric | USDC committed |
| `pnl_usd` | numeric | realised, populated on resolution |
| `outcome` | text | `WIN` / `LOSS` / null (still open) |
| `status` | text | `OPEN` / `RESOLVED_WIN` / `RESOLVED_LOSS` |
| `transport` | text | `relayer` / `onchain_matic` / null (post PR #298) |
| `initiator` | text | `engine_auto` / `manual_billy` / `polymarket_sweeper` |
| `created_at` | timestamptz | order-placement time |
| `resolved_at` | timestamptz | resolution time |
| `metadata` | jsonb | gate snapshot + dedup_key |

Common queries:
```sql
-- Per-strategy P&L last 24h
SELECT strategy_id,
       COUNT(*) FILTER (WHERE outcome='WIN')  AS w,
       COUNT(*) FILTER (WHERE outcome='LOSS') AS l,
       ROUND(SUM(pnl_usd)::numeric, 2) AS pnl
FROM trades
WHERE created_at > NOW() - INTERVAL '24 hours'
  AND outcome IS NOT NULL
GROUP BY 1 ORDER BY pnl DESC;

-- Fill rate
SELECT
  strategy_id,
  COUNT(*) FILTER (WHERE status IN ('RESOLVED_WIN','RESOLVED_LOSS')) AS filled,
  COUNT(*) FILTER (WHERE status = 'UNFILLED') AS unfilled
FROM trades
WHERE created_at > NOW() - INTERVAL '48 hours'
GROUP BY 1;
```

**Gotcha:** `outcome='WIN' AND metadata->>'redemption_state'` is the flawed path in audit #259 — do NOT use that filter to determine "unredeemed" (it misses on-chain truth). For real pending wins, query the on-chain way via `scripts/ops/check_pending.py`.

### 4.2 `strategy_decisions` — every tick of every strategy
```sql
\d strategy_decisions
```

Heavy table. ~500K rows / week per strategy. **Always dedup by dedup_key before aggregating**:

```sql
-- CORRECT (deduped): distinct windows
SELECT skip_reason, COUNT(*) AS n
FROM (
  SELECT DISTINCT ON (metadata->>'dedup_key', strategy_id)
    skip_reason
  FROM strategy_decisions
  WHERE strategy_id='v6_sniper'
    AND action='SKIP'
    AND evaluated_at > NOW() - INTERVAL '48 hours'
  ORDER BY metadata->>'dedup_key', strategy_id, evaluated_at DESC
) d
GROUP BY 1 ORDER BY n DESC;

-- WRONG (inflated by re-eval factor): raw row counts
SELECT skip_reason, COUNT(*) FROM strategy_decisions
WHERE strategy_id='v6_sniper' AND action='SKIP'
  AND evaluated_at > NOW() - INTERVAL '48 hours'
GROUP BY 1;  -- returns numbers ~91× too big per audit #255
```

Short-cut: audit #255 F1 shipped matview `strategy_skip_resolved` which is pre-deduped + joined with outcomes:
```sql
SELECT * FROM strategy_skip_resolved
WHERE strategy_id='v6_sniper'
ORDER BY window_ts DESC LIMIT 100;
```

### 4.3 `signal_evaluations` — raw per-tick model outputs
```sql
\d signal_evaluations
```
Join via `(timestamp, timeframe)` or `(window_id, asset)`. Has `p_up`, `vpin`, regime classification, etc. Use this for feature-importance work (vs `strategy_decisions.metadata` which is copy-snapshot per strategy).

### 4.4 `ticks_chainlink` — authoritative price feed
```sql
\d ticks_chainlink
```
Chainlink is the oracle Polymarket uses for 5-min BTC resolution. `actual_direction` on an outcome = `sign(close_price - open_price)` computed from here.

```sql
-- Resolution direction for a specific window
WITH w AS (
  SELECT '1776697800'::bigint AS close_ts  -- from dedup_key suffix
)
SELECT
  (SELECT price FROM ticks_chainlink
   WHERE asset='BTC' AND ts = to_timestamp((SELECT close_ts FROM w) - 300)
   ORDER BY ts DESC LIMIT 1) AS open_price,
  (SELECT price FROM ticks_chainlink
   WHERE asset='BTC' AND ts = to_timestamp((SELECT close_ts FROM w))
   ORDER BY ts DESC LIMIT 1) AS close_price;
```

### 4.5 `wallet_snapshots` — reconciler-written USDC snapshots
```sql
SELECT recorded_at, balance_usdc FROM wallet_snapshots
ORDER BY recorded_at DESC LIMIT 10;
```
Populated every 90s by engine's CLOB reconciler loop. **Trust the freshest row for DB-view; cross-check against on-chain `USDC.balanceOf(proxy)` if decision is financial.** Per audit #259, the wallet v2 FE page trusts this column and then returns $0 when it's stale — don't make that mistake.

### 4.6 `outcomes`
```sql
\d outcomes
```
Canonical resolution store. As of 2026-04-20 has coverage gap for v6 LIVE era — fix shipped in PR #300 (audit #255 F4). If `actual_direction` is null for a window, fall back to computing from `ticks_chainlink`.

### 4.7 `notes` + `audit_tasks` (Hub, same DB)
```sql
SELECT id, title, created_at FROM notes ORDER BY id DESC LIMIT 10;
SELECT id, title, severity, status FROM audit_tasks WHERE status='OPEN' ORDER BY priority DESC;
```

Prefer the Hub REST API for writes (`POST /api/notes`, `PATCH /api/audit-tasks/:id`) — it enforces auth + dedupe and emits WS events. Direct SQL writes to these tables bypass those.

---

## 5. Safe-query patterns

### `EXPLAIN ANALYZE` on anything costly
Before a big aggregate:
```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT ... FROM strategy_decisions WHERE ...;
```
Abort if `rows` > 1M in the plan OR runtime > 5s. Use indexes from audit #255 F2 — they cover the common paths.

### Use the matviews
Audit #255 F1 shipped `strategy_skip_resolved`. Refreshes every 5 min. Query it, not raw `strategy_decisions`, unless you need per-eval-tick detail.

Refresh on demand:
```sql
REFRESH MATERIALIZED VIEW CONCURRENTLY strategy_skip_resolved;
```
(Hub also exposes `POST /api/v58/skip-bucket-analysis/refresh`.)

### Timestamp hygiene
```sql
-- RIGHT
WHERE evaluated_at > NOW() - INTERVAL '48 hours'
-- WRONG (text compare):
WHERE to_char(evaluated_at, 'YYYY-MM-DD') > '2026-04-18'
```

### JSONB path vs ->>
```sql
-- Fast (uses jsonb_path_ops GIN index from audit #255 F2):
WHERE metadata @> '{"conviction_bucket": "pegged_path1"}'

-- Slower (string cast, needs B-tree on the expression):
WHERE metadata->>'conviction_bucket' = 'pegged_path1'
```

Prefer containment queries when you can.

---

## 6. Migration flow (if you MUST change schema)

1. New SQL file under `hub/db/migrations/versions/YYYYMMDD_NN_description.sql`.
2. Conform to existing pattern: `DO $$ BEGIN … END $$;` wrapper + idempotent (re-running should no-op).
3. Run migration locally against a pg instance (docker-compose has one) to verify.
4. PR against `develop`.
5. Hub auto-applies migrations on boot (`hub/main.py` lifespan hook). Railway deploy picks it up automatically.
6. After deploy, tail `hub` logs for `hub.migration_*_applied` events.

Never run migrations manually against prod Railway pg.

---

## 7. Backup / recovery

Railway takes automatic daily snapshots (7-day retention). If a bad write happens:
1. Stop the engine (`POST /api/system/kill`).
2. Message operator immediately.
3. Do NOT attempt recovery yourself — operator triggers Railway restore.

There's no hot-standby. Recovery = operator manual action + ~30 min downtime.

---

## 8. Observability

Every meaningful SQL run from this agent/runbook should emit a log line to `/home/novakash/db_query.log` on Montreal:

```bash
echo "$(date -u +%FT%TZ)  $USER  $0  $(wc -c < /tmp/q.sql) bytes" >> /home/novakash/db_query.log
```

Purpose: if something looks weird in the DB later, we have a record of what queries ran.

Hub: `POST /api/notes` with tag `db-query` for anything non-trivial (anything that touches the `metadata` blob or runs `UPDATE`/`DELETE`).

---

## 9. Known issues to work around

| Issue | Impact | Workaround |
|---|---|---|
| `outcomes` backfill gap for v6 LIVE era (pre-PR #300) | WR/P&L on v6 skipped windows is uncomputable | Use `ticks_chainlink` + matview. Post-#300 all good. |
| `strategy_decisions.executed` / `fill_price` / `fill_size` NULL | Fill-rate analysis can't join directly | Join via `metadata->>'dedup_key'` to `trades`. Post-PR #300 populated going-forward. |
| `wallet_snapshots.balance_usdc` lag | Hub snapshot returns $0 | Compute via on-chain `USDC.balanceOf(proxy)` via RPC. Audit #259. |
| `metadata->>'redemption_state'` unreliable | Stale "pending" flags | Use `check_pending.py` (on-chain `payoutDenominator`). Audit #259. |
| Re-eval inflation on `strategy_decisions` | 91× row count per window | ALWAYS dedup by `metadata->>'dedup_key'`. |
| Schema drift on `metadata` jsonb | Older rows miss newer fields | Analyser code must tolerate NULL; never silently zero-fill. |

---

## 10. Minimum connection-check snippet

Paste this into any session to confirm end-to-end:

```bash
ssh-keygen -t rsa -b 2048 -f /tmp/ec2ic_key -N "" -q 2>/dev/null
aws ec2-instance-connect send-ssh-public-key \
  --region ca-central-1 \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user novakash \
  --ssh-public-key file:///tmp/ec2ic_key.pub > /dev/null
ssh -i /tmp/ec2ic_key -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
  novakash@15.223.247.178 \
  'cd /home/novakash/novakash && . engine/.env && \
   psql "$DATABASE_URL" -c "SELECT NOW() AS pg_now, version() AS pg_version;"'
```

Expected output: current UTC timestamp + Postgres version string.

If this fails, do NOT try to run queries. Escalate to operator.
