# SPARTA Agent Guide

> **Purpose.** Onboarding document for development agents working on the novakash trading system.
> Named SPARTA because the methodology is disciplined, minimal, and unforgiving of hand-waving.
> This doc lives in BOTH `novakash/develop:docs/SPARTA_AGENT_GUIDE.md` and
> `novakash-timesfm-repo/main:docs/SPARTA_AGENT_GUIDE.md`. Keep them byte-identical — same
> pattern as `docs/CI_CD.md`. When you change one, change the other in the matching PR.

You are landing in the middle of a live trading system. The engine is running right now, on a
real EC2 box in Montreal, processing real Polymarket orders with real money at stake. Every
code change you make either ships behind a default-off feature flag or is proven safe before it
lands. There is no third option.

This guide gives you the methodology we converged on during the 2026-04-11 clean-architect audit
(see `docs/AUDIT_PROGRESS.md`), the file pointers you need to be productive without reading
everything, and the specific incidents we've learned from so you don't repeat them.

---

## The three laws

1. **The engine is live and trading real money.** Every change must be either (a) behind a
   default-off feature flag like `V10_6_ENABLED` or `MARGIN_ENGINE_USE_V4_ACTIONS`, or (b)
   verified against a clean error-signature baseline in a CI deploy gate. Nothing ships "because
   it looks right."
2. **Evidence before assertions.** Run verification commands and read the output before claiming
   anything works. The `/audit` checklist exists to enforce this — every `DONE` task has a dated
   `progressNotes` entry citing a PR number, a commit SHA, or a log line. No "I believe this is
   fixed" entries.
3. **Branch conventions are not optional.**
   - `novakash` deploys from **`develop`**. `main` is the audit/release branch.
   - `novakash-timesfm-repo` deploys from **`main`**.
   Getting this backwards will send your PR into the void. See `docs/CI_CD.md` for the
   canonical table.

---

## The audit checklist is the single source of truth

The `/audit` page at `http://${AWS_FRONTEND_HOST}/audit` is the master project to-do list. It is
backed by a static React component — `frontend/src/pages/AuditChecklist.jsx` — with the full
taxonomy hard-coded in `SESSION_META`, `CATEGORIES`, and `TASKS` arrays.

### Task schema

Each task in `TASKS` is an object with:

| field | purpose |
|---|---|
| `id` | short code, e.g. `DQ-01`, `PE-06`, `DS-01`, `CI-01`, `FE-07` |
| `category` | references a `CATEGORIES` entry (`data-quality`, `production-errors`, `decision-surface`, `v4-adoption`, `clean-architect`, `frontend`, `ci-cd`) |
| `severity` | `CRITICAL`, `HIGH`, `MEDIUM`, `LOW` |
| `status` | `OPEN`, `IN_PROGRESS`, `DONE`, `BLOCKED`, `INFO` |
| `title` | one-line description |
| `files` | array of `{path, line, repo}` pointers — **always cite file:line** |
| `evidence` | bulleted bullet list of why this is a problem |
| `fix` | one-paragraph description of the intended change |
| `progressNotes` | dated notes, one per session iteration: `{date, note}` |

When you finish a task:

1. Flip `status` to `DONE` in `AuditChecklist.jsx`.
2. Add a `progressNotes` entry with today's date, the PR number, and a verification note.
3. Add a matching bullet to `docs/AUDIT_PROGRESS.md` in the **same commit** so the UI and the
   audit trail stay in sync.
4. Reference the task ID in the commit message: `fix(engine): PE-06 — JSON quoting in prediction recorder`.

### Audit-update discipline (MANDATORY)

**Every PR that ships code MUST include audit updates in the same commit or a follow-up in the
same session.** This is non-negotiable. The 2026-04-11 late-night blitz shipped 22 PRs (#82-#103)
without updating the checklist, leaving CA-02/CA-03/CA-04 stuck at `IN_PROGRESS` when they were
actually `DONE`. This created a stale audit that required a catch-up PR the next day.

**At session start:**
1. Read `docs/AUDIT_PROGRESS.md` and `frontend/src/pages/AuditChecklist.jsx`.
2. Check if any `IN_PROGRESS` or `OPEN` tasks have been completed by recent PRs (check `git log`).
3. If stale entries exist, update them before starting new work.

**At session end (or every 5 PRs, whichever comes first):**
1. Scan all PRs merged in this session.
2. For each: is there a matching task in AuditChecklist.jsx? If yes, update status + progressNotes.
   If no matching task exists and the PR is non-trivial, add a new task entry.
3. Add a dated section to `docs/AUDIT_PROGRESS.md` summarising what shipped.
4. Commit the audit updates.

**The rule:** if you shipped it, track it. If you can't remember whether you tracked it, check.

### Category severity conventions

- **CRITICAL red** — trading-loss class. Stops the line.
- **HIGH amber** — data-quality or production error. Fix within the session.
- **MEDIUM cyan** — observability / architecture. Fix when you're already in the file.
- **LOW grey** — cosmetic. Batch these.
- **INFO cyan** — informational, no action required.

---

## Accessing everything — GitHub + AWS CLI

Assume you have `gh` (GitHub CLI) and `aws` CLI available.

### Frontend (http://99.79.41.246)
```bash
# Check if latest frontend is deployed
gh run list --repo billybrichards/novakash --workflow deploy-frontend.yml --limit 3

# Login at http://99.79.41.246 — credentials: billy / novakash2026
# JWT expires — re-login if API calls return 401
```

### Hub API (AWS Montreal, port 8091)
```bash
# Get fresh JWT
TOKEN=$(curl -s -X POST http://3.98.114.0:8091/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"billy","password":"novakash2026"}' | python3 -c "import json,sys; print(json.load(sys.stdin).get('access_token',''))")

# Key endpoints
curl -s "http://3.98.114.0:8091/api/v58/execution-hq?asset=btc&timeframe=5m" -H "Authorization: Bearer $TOKEN"
curl -s "http://3.98.114.0:8091/api/v58/strategy-decisions?limit=10" -H "Authorization: Bearer $TOKEN"
curl -s "http://3.98.114.0:8091/api/v58/accuracy?limit=100" -H "Authorization: Bearer $TOKEN"
curl -s "http://3.98.114.0:8091/api/v58/prediction-surface?days=7" -H "Authorization: Bearer $TOKEN"
```

### TimesFM service (Montreal, port 8080) — no auth
```bash
curl -s "http://3.98.114.0:8080/v4/snapshot?asset=btc&timescales=5m"
curl -s "http://3.98.114.0:8080/v3/snapshot?asset=btc"
curl -s "http://3.98.114.0:8080/health"
```

### Database (Railway PostgreSQL) — public URL
```bash
# Get from Railway dashboard: DATABASE_PUBLIC_URL
export PUB_URL="postgresql://postgres:PASSWORD@hopper.proxy.rlwy.net:35772/railway"
python3 docs/analysis/full_signal_report.py   # full current-state report
python3 docs/analysis/run_window_analysis.py  # window accuracy surface
```

### Engine on Montreal (EC2 Instance Connect — key expires in 60s)
```bash
# Generate temp key and push it
ssh-keygen -t ed25519 -f /tmp/montreal_key -N "" -q 2>/dev/null
aws ec2-instance-connect send-ssh-public-key \
  --region ca-central-1 \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user ubuntu \
  --ssh-public-key "$(cat /tmp/montreal_key.pub)"
ssh -i /tmp/montreal_key -o StrictHostKeyChecking=no ubuntu@15.223.247.178 "COMMAND"

# Useful commands via SSH:
# sudo tail -50 /home/novakash/engine.log | grep 'strategy\.'     # recent strategy decisions
# sudo grep -E 'V4_FUSION|V10_GATE|PAPER_MODE|LIVE_TRADING' /home/novakash/novakash/engine/.env
# sudo pgrep -a 'python3 main.py'                                  # check engine running
# sudo bash /home/novakash/novakash/scripts/restart_engine.sh </dev/null &  # restart
```

### EC2 instances (key services)
```bash
aws ec2 describe-instances --region ca-central-1 \
  --filters "Name=instance-state-name,Values=running" \
  --query 'Reservations[*].Instances[*].[Tags[?Key==`Name`].Value|[0],PublicIpAddress,InstanceId]' \
  --output table

# Key IPs:
# novakash-montreal-vnc  15.223.247.178  i-0785ed930423ae9fd  (engine + hub + timesfm)
# novakash-frontend-v3   99.79.41.246    i-0fe72a610900b5cca  (nginx, React)
# novakash-margin-engine 18.169.244.162  eu-west-2             (margin engine)
```

### Deploying
```bash
# Frontend + Hub auto-deploy on push to develop
git push origin develop

# Engine requires SSH restart (CI deploys code but restart hangs):
# SSH to Montreal and run restart_engine.sh
# OR the CI deploy-engine.yml workflow does it (when ENGINE_SSH_KEY secret works)
```

---

## Two repos, one system

| Repo | Deploy branch | Contains | Who reads it |
|---|---|---|---|
| `novakash` | `develop` | `engine/` (Polymarket trader), `margin_engine/` (Hyperliquid perp trader), `hub/` (FastAPI backend), `frontend/` (React dashboard), `macro-observer/`, `data-collector/`, `scripts/restart_engine.sh`, `docs/` | operators, engine devs, frontend devs |
| `novakash-timesfm-repo` | `main` | `app/` (FastAPI model service: v1 legacy, v2 LightGBM, v3 composite, v4 fusion surface), `training/` (auto-retrain pipelines), `docs/` (model proposals, go-live logs) | model devs, v4 fusion devs |

The two repos talk to each other via:

- **`TIMESFM_URL`** (env var read by `engine/` and `margin_engine/`) — HTTP base URL for the
  timesfm-service on Montreal (`http://3.98.114.0:8080`). Engine code fetches `/v2/probability`,
  `/v3/composite`, `/v4/snapshot` etc. directly.
- **The hub proxy** — `hub/api/margin.py` exposes every model endpoint through `/api/*` so the
  frontend can hit them with JWT auth. See `frontend/src/pages/V4Surface.jsx` for a canonical
  consumer.
- **`CrossRegionFetcher`** in `novakash-timesfm-repo/app/cross_region_fetcher.py` — the v4 fusion
  layer needs data from macro-observer, data-collector, and polymarket tables in the shared
  Postgres. See `app/main.py:43-47` and `app/v4_snapshot_assembler.py`.

**Cross-repo PR coordination.** If a change touches both repos (e.g. adding a new v4 field that
the frontend consumes), ship in this order: timesfm-repo `main` → wait for `ci.yml` to prove the
new endpoint is live → novakash `develop`. A backwards order lands dead-code frontend first and
trips up health probes.

---

## The data stack, top to bottom

| Layer | Where | What it returns | Who consumes |
|---|---|---|---|
| **v1 (legacy)** | `novakash-timesfm-repo/app/forecaster.py` + `app/main.py` | TimesFM 2.5 200M point forecast (`GET /forecast`) | legacy dashboard widgets only — not used by engine |
| **v2 (LightGBM)** | `novakash-timesfm-repo/app/v2_*.py` | `P(UP)` probability + calibrated quantile bands via `GET /v2/probability` — backed by Sequoia v5.2 model (see `docs/SEQUOIA_V5_GO_LIVE_LOG.md`). Writes to `ticks_v2_probability` | `engine/strategies/five_min_vpin.py`, `margin_engine/use_cases/open_position.py` |
| **v3 (composite)** | `novakash-timesfm-repo/app/v3_*.py` | Multi-timescale composite score with regime/cascade FSM, published on `GET /v3/composite` + `WS /v3/stream`. Writes to `ticks_v3_composite` | `engine/` (cascade signals) |
| **v4 (fusion)** | `novakash-timesfm-repo/app/v4_*.py` + `app/v4_routes.py` | 10-gate fusion surface: `recommended_action`, `consensus`, `macro`, `events`, `orderflow`, `clob`, `quantiles`, `regime`. Published on `GET /v4/snapshot` + sub-endpoints. Writes to `ticks_v4_snapshot` | `margin_engine/use_cases/open_position.py::_execute_v4()` (gated by `MARGIN_ENGINE_USE_V4_ACTIONS`), frontend `/margin` and `/audit` pages |
| **signal_evaluations** | `novakash/engine/` writes, shared Postgres | Per-evaluation row with every gate decision, trade decision, and final PnL resolution — the primary training dataset for v2/v3/v5 retrains | auto-retrain workflow, the 865-outcome V10.6 evidence base |
| **macro_signals** | `novakash/macro-observer` writes, shared Postgres | 60s-cadence Qwen 3.5 122B bias classifications with per-timescale map. `bias='NEUTRAL' AND confidence=0 AND reasoning LIKE '%Fallback%'` is the dual-writer warning signature | `v4_macro_store` in timesfm-repo, `/v4/macro` endpoint |
| **market_snapshots** / **market_data** | `novakash/data-collector` writes | ~2.3 writes/sec single-writer; 8 `(asset, timeframe)` pairs per ~3.5s Gamma-API-bound cycle | `/v4/polymarket/window`, the Polymarket engine |
| **clob_book_snapshots** / **ticks_clob** | `novakash/engine/data/feeds/clob_feed.py` writes | Polymarket CLOB book snapshots. **PE-01** ship-stopper bug: the INSERT column list was missing `ts`, dropping 1090 errors/hour for 4 days. See `docs/AUDIT_PROGRESS.md` for the fix. | Polymarket engine, `/v4/clob` |
| **margin_engine tables** | `novakash/margin_engine/` | Hyperliquid perp trades, positions, fills | `/margin` dashboard |
| **Polymarket engine** | `novakash/engine/strategies/five_min_vpin.py` (3096-line god class, `CA-01`) | binary-options on 5-minute BTC/ETH direction via Polymarket CLOB | the real money |

**Critical venue distinction:** the two trading engines use different price references.
`engine/` (Polymarket) resolves against **BTC/USD spot** via oracle. `margin_engine/` trades
Hyperliquid perps and needs **perp/mark** price references. Applying a universal "drop
delta_binance" fix breaks one or the other. This is why `DQ-01` is scoped to Polymarket only
and `DQ-05` is a separate margin_engine pricing audit. See
`docs/AUDIT_PROGRESS.md` "Rev-3: pricing clarification" section.

---

## Montreal rules: how to SSH the production engine

The Polymarket engine runs on `15.223.247.178` (instance `i-0785ed930423ae9fd`,
`novakash-montreal-vnc`, AZ `ca-central-1b`). It runs as a raw `python3 main.py` nohup process
under the `novakash` user — not systemd, not Docker. The timesfm-service, macro-observer, and
data-collector run on a different Montreal box at `3.98.114.0`.

Access uses **EC2 Instance Connect** — generate a fresh ed25519 keypair, push the public key
with a 60-second TTL, SSH in before it expires. The full incantation (verbatim from
`docs/DEPLOYMENT.md:113-137`):

```bash
# 1. Generate a fresh temp key
ssh-keygen -t ed25519 -f /tmp/ec2_temp_key -N "" -q

# 2. Push to EC2 Instance Connect (60s window)
#    Option A — as ubuntu (has sudo; use for permission fixes or systemctl)
aws ec2-instance-connect send-ssh-public-key \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user ubuntu \
  --ssh-public-key file:///tmp/ec2_temp_key.pub \
  --availability-zone ca-central-1b \
  --region ca-central-1

ssh -o StrictHostKeyChecking=no -o IdentitiesOnly=yes \
  -i /tmp/ec2_temp_key ubuntu@15.223.247.178

#    Option B — as novakash (the app user; use for git pull, env edits, tailing logs)
aws ec2-instance-connect send-ssh-public-key \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user novakash \
  --ssh-public-key file:///tmp/ec2_temp_key.pub \
  --availability-zone ca-central-1b \
  --region ca-central-1

ssh -o StrictHostKeyChecking=no -o IdentitiesOnly=yes \
  -i /tmp/ec2_temp_key novakash@15.223.247.178
```

### Layout on the host

```
/home/novakash/novakash/              # the cloned repo (novakash user owns it)
├── engine/                           # code base
│   ├── main.py                       # what the nohup process runs
│   └── .env                          # gitignored; the only source of runtime config
├── scripts/
│   └── restart_engine.sh             # canonical restart + log rotation
/home/novakash/engine.log             # live engine log — appended, never truncated
/home/novakash/engine-YYYYMMDD-HHMMSS.log  # rotated archives (KEEP_N=20)
```

### Restarting the engine

**Always** use `scripts/restart_engine.sh`. Never use a raw `nohup python3 main.py > engine.log`
— the `>` redirect truncates the log on each run, obliterating pre-crash history (we've been
burnt by this, which is why the script exists). The script does:

1. `sudo chown -R novakash:novakash /home/novakash/novakash/` — fix permissions if engine
   crashed root-owned
2. Rotate `/home/novakash/engine.log` → `engine-YYYYMMDD-HHMMSS.log` then truncate the current
   log (preserves archive, doesn't lose history)
3. Prune archives beyond `KEEP_N=20`
4. `sudo pkill -9 -f 'python3 main.py'` and sleep 4
5. Assert no stragglers
6. `sudo -u novakash bash -c 'cd /home/novakash/novakash/engine && nohup python3 main.py >> /home/novakash/engine.log 2>&1 & disown'` (note the `>>` append)
7. Sleep 6, then `pgrep -f 'python3 main.py'` — **must return exactly one PID**
8. Tail 10 lines of the new log for smoke verification

```bash
# Manual restart from your machine
ssh novakash@15.223.247.178 'cd /home/novakash/novakash && git pull origin develop'
ssh ubuntu@15.223.247.178 'sudo bash /home/novakash/novakash/scripts/restart_engine.sh'
```

Once `CI-01` is live (`.github/workflows/deploy-engine.yml`, currently `IN_PROGRESS` pending
host secret config), a push to `develop` will re-rsync the engine code and fire the same
restart script remotely. Until then, restart is a manual step.

> ⚠ This WILL wipe the engine state if you get it wrong. The engine holds in-memory VPIN
> buckets, WebSocket connections, and orchestrator heartbeats. A restart resets those. If the
> restart happens mid-evaluation the current window is lost. Always plan restarts for a
> between-window moment (the log ticks `window.change` every 5 minutes).

---

## Engine monitoring

The engine is **not** monitored by default. There is no alerting, no systemd unit, no Docker
healthcheck (yet — see `docs/CI_CD.md` "Known gap" section). Operators tail `engine.log` and
grep for error signatures. The `CI-01` error-signature gate in `.github/workflows/deploy-engine.yml`
codifies the known-bad list so every future deploy catches regressions automatically.

### The error-signature gate (canonical thresholds)

From `.github/workflows/deploy-engine.yml:211-264`, step 12 — this is the gold-standard "how
do you know a deploy is safe" pattern. Copy these thresholds into any new monitoring you add:

```bash
check_signature "clob_feed.write_error"              0   # PE-01 fix — must stay 0
check_signature "reconciler.resolve_db_error"        0   # PE-02 + PE-05 fix — must stay 0
check_signature "reconciler.orphan_fills_error"      5   # PE-03 transient Poly API noise
check_signature "evaluate.price_source_disagreement" 30  # pre-DQ-01; tighten after fix to <5
check_signature "evaluate.no_current_price"          2   # cold-start tolerated
check_signature "reconciler.no_trade_match"          5   # orphan reconciler noise
```

The gate greps `sudo tail -n 10000 /home/novakash/engine.log`, counts each signature, and fails
the deploy if any count exceeds its threshold.

**When the DQ-01 rollout lands**, tighten `evaluate.price_source_disagreement` from 30 to <5
and gate it behind `V11_POLY_SPOT_ONLY_CONSENSUS=true` for rollback.

**When `PE-06` (ELM/prediction_recorder JSON quoting) is replaced by the SQ-01 rename**, add
a `check_signature "prediction_recorder.write_error" 0` line — see `docs/AUDIT_PROGRESS.md`
"Agent E (SQ-01)" for the CI-02 follow-up task.

### Live health spot-check (run after any deploy or restart)

```bash
# SSH as novakash via EC2 IC (see Montreal rules above), then:
sudo tail -n 5000 /home/novakash/engine.log | grep -c "clob_feed.write_error"
sudo tail -n 5000 /home/novakash/engine.log | grep -c "reconciler.resolve_db_error"
sudo tail -n 5000 /home/novakash/engine.log | grep -c "orphan_fills_error"
sudo tail -n 5000 /home/novakash/engine.log | grep -c "binance_ws.disconnected"
sudo tail -n 5000 /home/novakash/engine.log | grep -c "polymarket_ws.disconnected"

# Process count — must be exactly 1
pgrep -fa "python3 main.py"

# Live writes to the DB (run locally)
psql "$DATABASE_URL" -c "
  SELECT COUNT(*) FROM signal_evaluations WHERE created_at > now() - interval '5 minutes';
  SELECT COUNT(*) FROM ticks_clob WHERE ts > now() - interval '1 minute';
  SELECT COUNT(*) FROM clob_book_snapshots WHERE ts > now() - interval '1 minute';
  SELECT bias, confidence, reasoning FROM macro_signals ORDER BY id DESC LIMIT 3;
"
```

The post-INC-01 clean baseline (2026-04-11 12:30 UTC) was: all zero-threshold gates holding at
0, `clob_feed.prices` every 2-3s, `chainlink_feed.written` 4 rows every 5s, engine PID stable.

---

## CI/CD conventions

The canonical reference is **`docs/CI_CD.md`**. Read it before touching any deploy workflow.
This section summarises the pattern so you can recognise when to port it.

### Service deployment map (condensed)

| Service | Repo | Target | Workflow | Fires on |
|---|---|---|---|---|
| **timesfm-service** | novakash-timesfm-repo | EC2 Montreal `3.98.114.0:8080` | `.github/workflows/ci.yml` | push to `main` |
| **macro-observer** | novakash | EC2 Montreal `3.98.114.0` (sibling dir, Docker) | `.github/workflows/deploy-macro-observer.yml` | push to `develop`, path `macro-observer/**` |
| **data-collector** | novakash | EC2 Montreal `3.98.114.0` (sibling dir, Docker) | `.github/workflows/deploy-data-collector.yml` | push to `develop`, path `data-collector/**` |
| **margin-engine** | novakash | EC2 eu-west-2 (systemd) | `.github/workflows/deploy-margin-engine.yml` | push to `develop`, path `margin_engine/**` |
| **hub (API)** | novakash | Railway | Railway auto-deploy | push to `develop` |
| **frontend (web)** | novakash | EC2 AWS_FRONTEND_HOST (nginx) | `.github/workflows/deploy-frontend.yml` | push to `develop`, path `frontend/**` |
| **engine (Polymarket)** | novakash | EC2 Montreal `15.223.247.178` (raw python3, novakash user) | `.github/workflows/deploy-engine.yml` | **drafted, not active** — CI-01 pending host secret config |

(Mirrored in the static data in `frontend/src/pages/Deployments.jsx` at the `SERVICES` array.)

### The deploy-*.yml pattern (13 steps)

Every AWS deploy workflow follows the same shape. The canonical template is
`deploy-macro-observer.yml` (~150 lines, the cleanest commented example). `deploy-engine.yml`
is a port of it for the non-Docker engine case.

1. `actions/checkout@v4`
2. **`Require runtime secrets`** — fails loud if any required secret is empty. This catches
   "I forgot to add it to GH Secrets" BEFORE any host contact.
3. **Write SSH key** — try base64-decode first, fall back to raw PEM text. Add host to
   `known_hosts` via `ssh-keyscan`.
4. **Ensure host directories exist** — idempotent `mkdir -p` + `chown`.
5. **Rsync code to host** — `rsync -avz --delete --exclude '.env' --exclude '.git' ...`.
   `.env` is NEVER rsynced; host secrets are templated in step 7.
6. **(optional)** Rsync `scripts/` directory — needed for `deploy-engine.yml` so
   `restart_engine.sh` lands on the host.
7. **Template `.env` from GitHub Actions secrets** — idempotent `sed`-replace-or-append. The
   secret is passed as a streamed bash script over stdin so it never appears on the remote
   command line (prevents it showing up in `ps`).
8. **Rebuild/recreate** — `docker compose build && docker compose up -d --force-recreate`
   (for Docker services) OR `systemctl restart margin-engine` (for systemd) OR
   `sudo bash scripts/restart_engine.sh` (for raw python3 engine).
9. **Wait for startup** — 45-90s sleep depending on service.
10. **Health probe (process-level)** — `pgrep -f 'python3 main.py'` returns exactly 1, OR
    `docker ps --filter name=X --format ...` matches `Up X minutes (healthy)`, OR
    `systemctl is-active` returns `active`.
11. **Health probe (log-level / error-signature gate)** — for the engine, the canonical gate
    above. For macro-observer, grep for `llm.call_ok`. For data-collector, grep for the
    writes-per-second bucket.
12. **Tail recent logs** — dumps the last 30-50 lines on success for run-page diagnostics.
13. **(optional)** HTTP probe against the new endpoint — `curl -sf http://$HOST:$PORT/health`.

### Injection-defence pattern (mandatory)

All GHA workflows pull secrets into a job-level `env:` block:

```yaml
env:
  SSH_KEY: ${{ secrets.ENGINE_SSH_KEY }}
  HOST: ${{ secrets.ENGINE_HOST }}
  DEPLOY_DATABASE_URL: ${{ secrets.DATABASE_URL }}
```

Then `run:` steps use plain bash variables (`$SSH_KEY`, `$HOST`) — never
`${{ secrets.X }}` directly inside a `run:` block. This protects against the GitHub Actions
workflow-injection class. Reference:
<https://github.blog/security/vulnerability-research/how-to-catch-github-actions-workflow-injections-before-attackers-do/>

---

## Incident log — what we've broken and fixed

Condensed one-liners — the full story is in `docs/AUDIT_PROGRESS.md`.

- **INC-01** (2026-04-11 11:05 UTC, HIGH DONE) — Montreal host networking wedged, DNS broken,
  engine died at 12:00:54 UTC. Smoking gun:
  `chainlink_feed.asset_error: Temporary failure in name resolution`. AWS SSM agent
  unregistered, so reboot via `aws ec2 reboot-instances i-0785ed930423ae9fd` was the only path.
  Recovery at 12:11:11 UTC, SSH responsive at 12:17, engine restarted at 12:22:57 via
  `scripts/restart_engine.sh`. Lesson: enable SSM agent; add CloudWatch alarm on engine.log
  write silence > 120s.
- **PE-01** (CRITICAL DONE, PR #26) — `engine/data/feeds/clob_feed.py` `clob_book_snapshots`
  INSERT missed the `ts` column, so 11 columns lined up against `NOW() + $1..$10` but the
  Python call passed 11 positional args. **1090 errors/hour, 4-day data gap.** Fix: add `ts`
  first and a new `$11` parameter, matching the working `ticks_clob` INSERT immediately above.
- **PE-02** (HIGH DONE, PR #26) — `engine/reconciliation/reconciler.py:765` bidirectional
  prefix-match LIKE using `$1` and `$2` in both sides. asyncpg: `inconsistent types deduced for
  parameter $1 — text vs character varying`. Fix: single `$1::text` parameter matching the
  working startup-backfill pattern at lines 185-186. 4 errors/hour regression from PR #18.
- **PE-05** (MEDIUM DONE, PR #29) — same bug class as PE-02, second instance at
  `reconciler.py:824-834`. `UPDATE SET outcome = $1 ... CASE WHEN $1 = 'WIN'` — `$1` used in
  two incompatible type contexts. Fix: drop the inline CASE WHEN, pass a fourth parameter with
  the pre-computed `status`. **Lesson: bug classes come in pairs — always grep the sibling
  patterns after fixing one.**
- **PE-06** (HIGH DONE, PR #30) — `engine/data/feeds/elm_prediction_recorder.py:129`
  `str(result.get("feature_freshness_ms", {}))` emits Python repr single quotes, Postgres
  JSONB rejects `Token ' is invalid`. Silent prediction drops — matters because the V10.6
  decision surface uses 865 recorded predictions as its backtest evidence base. Fix:
  `json.dumps()`. Added 5-test coverage. **Do NOT rename the ELM file in the same PR** —
  tracked as SQ-01 for a separate rename PR.
- **DQ-05** (HIGH DONE as investigation, 2026-04-11) — READ-ONLY audit by Agent D confirmed
  the margin_engine's `_execute_v4()` only reads `v4.last_price` for pricing, not
  `consensus.reference_price`. The ratio math `(last_price - p10) / last_price` is
  dimensionless, so even though `v4.last_price` is Binance spot the PnL numbers remain
  internally consistent. False alarm — **but Agent D found DQ-06 while looking.**
- **DQ-06** (HIGH OPEN, discovered during DQ-05) — `margin_engine/main.py:84-97` `paper + binance`
  branch constructs `PaperExchangeAdapter` with **no `price_getter`**, so `_last_price` stays at
  the `80000.0` default forever. Every `get_mark()` and `get_current_price()` returns bid/ask
  around a frozen $80k constant. User clarification: the paper venue should be Hyperliquid. Fix
  is a CI template update: add `set_env MARGIN_EXCHANGE_VENUE hyperliquid` to
  `deploy-margin-engine.yml`, flip the `settings.py:31` default, add a startup assertion.
- **CI-01** (IN_PROGRESS, PR #27) — the error-signature gate described above. First GHA deploy
  workflow for `engine/`. Flips to DONE when (a) `ENGINE_HOST` + `ENGINE_SSH_KEY` are added to
  `billybrichards/novakash` Actions secrets, (b) first `workflow_dispatch` run succeeds
  end-to-end, (c) error-signature gate passes against live `engine.log`.

---

## How to work like Claude did on 2026-04-11

The methodology we converged on during the big audit day. This is the SPARTA playbook.

### 1. Read before write

Before touching any file:

- Read the audit checklist entry for the task you're picking up.
- Read the `files[]` array from the task — those are the file:line pointers.
- Read the `evidence[]` bullets. Those are why the task exists.
- Read the `fix` paragraph. That's what success looks like.
- Grep the codebase for sibling bug-class instances. If PE-02 had a type-deduction bug in one
  reconciler method, PE-05 had the same bug 60 lines below. Always check.

Never jump to Write/Edit without at least 3 Read calls first.

### 2. Dispatch parallel agents for independent work

The 2026-04-11 session shipped **PE-06, DS-01, FE-04/05/06, DQ-05, SQ-01** in parallel by
dispatching 5 background agents into isolated git worktrees. See the `superpowers:dispatching-parallel-agents`
skill for the framework.

**When parallel dispatch IS safe:**

- Tasks touch disjoint files (Agent A: `engine/data/feeds/elm_prediction_recorder.py` vs
  Agent B: `engine/signals/gates.py` — no overlap).
- Tasks are READ-ONLY investigations (Agents D and E — zero file changes, zero merge risk).
- Tasks are additive-only surfaces (Agent C: new `V1Surface.jsx`, `V2Surface.jsx`,
  `V3Surface.jsx`, no edits to existing `V4Panel.jsx` or `AuditChecklist.jsx`).
- Each agent has an explicit "DO NOT TOUCH these files" list in its prompt.

**When parallel dispatch is NOT safe:**

- Engine-touching trading logic (DQ-01, V4-01) — serialise in ONE engine-edits worktree.
- Architectural refactors (CA-01..CA-04 god-class splits) — need a plan before dispatch.
- Anything that could collide on `AuditChecklist.jsx` or `docs/AUDIT_PROGRESS.md` — only one
  agent can edit those at a time.

### 3. Every code change has a task ID

Commit messages follow the pattern `<type>(<scope>): <task-id> — <one-line description>`:

- `fix(engine): PE-06 — json.dumps for feature_freshness_ms`
- `feat(gates): DS-01 — V10.6 EvalOffsetBoundsGate (default OFF)`
- `feat(frontend): FE-04/05/06 — V1/V2/V3 data surface pages (Sequoia v5.2)`
- `ci: CI-01 — Montreal deploy workflow for engine/ with error-signature gate`

The task ID links the commit back to the `/audit` checklist entry and the matching
`docs/AUDIT_PROGRESS.md` bullet.

### 4. Every DONE task has a progressNotes entry

The format is fixed:

```js
progressNotes: [
  { date: '2026-04-11', note: 'PR #30 merged at c9f341b. Fix verified by 5-test coverage: test_feature_freshness_serializes_with_json_dumps passes on fix, fails on str(dict). Engine restart + log tail shows elm_recorder.write_error = 0 over 5000 lines post-deploy.' },
]
```

Date + PR number + verification. No "looks good" entries.

### 5. Default-off feature flag for any trading-logic change

Two canonical examples:

- **`V10_6_ENABLED`** (DS-01) — master flag for the V10.6 `EvalOffsetBoundsGate`. See
  `engine/signals/gates.py:201`: `self._enabled = os.environ.get("V10_6_ENABLED", "false").lower() == "true"`.
  When `false`, the gate is a pure pass-through returning `reason="disabled (V10_6_ENABLED=false)"`.
- **`MARGIN_ENGINE_USE_V4_ACTIONS`** (PR #16) — master flag for the 10-gate v4 stack inside
  `margin_engine/use_cases/open_position.py::_execute_v4()`. When `false`, the use case falls
  back to the pre-v4 path.

**Pattern:** read the env var at class construction, short-circuit to pass-through when false,
log the short-circuit reason so operators can see which path is live. Flip to `true` via
`.env` on the host (hand-managed, NOT templated from GHA secrets — see `CI_CD.md` for the
convention).

### 6. Verify before claiming success

Reference: the `superpowers:verification-before-completion` skill.

Before marking a task DONE:

- Run the tests (`pytest engine/tests/test_X.py -v`).
- Build the frontend (`cd frontend && npm run build` — catches import errors before deploy).
- For engine-touching work: SSH to Montreal, tail the log, grep for the error signature.
- For CI changes: trigger a `workflow_dispatch` run and read the full log.
- Record what you observed in the `progressNotes` entry.

Never mark something DONE on the basis of "the code looks correct." The engine has 3096 lines
of five_min_vpin.py — what looks correct to you might be trading five turns of VPIN ahead of
where you think.

---

## Common pitfalls

- **Worktree interference.** If you're in a worktree at
  `/Users/billyrichards/Code/novakash/.claude/worktrees/<name>`, do NOT `git checkout` another
  branch — it will wedge the parent checkout. Use `git worktree add` for every new branch.
- **Branch direction.** `novakash` → PR targets `develop`. `novakash-timesfm-repo` → PR
  targets `main`. Not the other way around. Railway and the AWS deploy workflows are both
  gated on the deploy branch; targeting the wrong one means your PR merges and nothing
  deploys.
- **Hub session staleness.** The frontend's JWT expires; if `/api/*` starts returning 401, you
  probably need to re-login. A 401 on the hub health probe is *expected* — it proves the auth
  wall is present.
- **The $80k paper bug (DQ-06).** If you wire a new paper exchange adapter, ALWAYS pass
  `price_getter`. The default constant is `80000.0` which will silently work forever and
  produce bogus P&L numbers.
- **The spot-vs-perp confusion (DQ-05 false alarm).** Don't assume a price reference bug without
  tracing the usage. `v4.last_price` is Binance spot in the timesfm-repo, but the margin_engine
  only uses it as a dimensionless ratio denominator. False alarms waste cycles — trace the
  actual read path.
- **asyncpg type deduction (PE-02 / PE-05).** If you see `inconsistent types deduced for
  parameter $1`, you're using the same parameter in two contexts where Postgres infers
  different types (e.g. `outcome` column vs a string literal comparison). Fix: use separate
  parameters or a `::text` cast. Always grep the sibling patterns — bug classes come in pairs.
- **Dual-writer races.** If you migrate a service to a new host, REMOVE the old
  `railway.toml` from that directory BEFORE pushing. Railway's git-watcher will keep
  auto-deploying the old container. The signature in the DB is
  `bias='NEUTRAL' AND confidence=0 AND reasoning LIKE '%Fallback%'` (for macro-observer) or
  write-rate that's 2× the expected single-writer rate (for data-collector). See
  `docs/CI_CD.md` "dual-writer class of bug" section.
- **Railway environment-specific sleep.** `serviceInstanceUpdate` with `sleepApplication: true`
  takes `environmentId` as a required argument. Novakash's live env is `develop`
  (`05c003e2-4ccf-4a6d-8ab7-148519bb1209`), NOT `production`. Sleeping the wrong env looks
  successful but leaves the real writer running. We hit this twice on 2026-04-11.
- **Log truncation on restart.** Never `nohup python3 main.py > engine.log` — the `>` wipes
  history. Use `scripts/restart_engine.sh` which rotates first and then appends with `>>`.
- **"Update the checklist, not just the code."** Every code change for a task needs a
  matching status flip + `progressNotes` entry in `AuditChecklist.jsx` + bullet in
  `docs/AUDIT_PROGRESS.md` — in the SAME commit. The UI and the audit trail MUST stay in sync.

---

## Data Tables Reference

Full tick save system — every table the engine and timesfm-service write to, with cadence and
content. Use this when writing a new analysis script, wiring a new consumer, or checking why a
table is empty.

| Table | Written by | Frequency | Contains |
|---|---|---|---|
| `signal_evaluations` | Engine (`EvaluateStrategiesUseCase`) | Every 2s per eval | V10 gate results, VPIN, deltas, Sequoia p_up, CLOB prices |
| `strategy_decisions` | Engine (`EvaluateStrategiesUseCase`) | Every 2s per eval | V10+V4 LIVE/GHOST decisions, full `_ctx` JSON with all signals |
| `window_snapshots` | Engine (reconciler) | Per 5-min window close | Window outcome, prices, V10 decision, regime |
| `trades` | Engine (`OrderManager`) | Per trade placed | Paper + live trade records, paper resolved against Chainlink oracle |
| `ticks_v3_composite` | timesfm-service (`V3DBWriter`) | Every 5s | V3 composite score × 9 timescales × 4 assets |
| `ticks_v4_decision` | timesfm-service (`V4DBWriter`) | Every 5s | Full V4 snapshot: HMM regime, conviction, quantiles, macro bias, `sub_signals` JSONB |
| `ticks_elm_predictions` | Engine (`PredictionRecorder`) | Every 30s | Sequoia v5.2 probability + quantiles (legacy table name — rename tracked as SQ-01 PR4, deferred forever) |
| `ticks_chainlink` | Engine | Every 5s | Chainlink oracle prices BTC/ETH/SOL/XRP on Polygon |
| `ticks_tiingo` | Engine | Every 2s | Top-of-book bid/ask with exchange attribution — BTC/ETH/SOL/XRP |
| `ticks_clob` | Engine (`clob_feed.py`) | Every 10s | Ground-truth Polymarket CLOB bid/ask prices per active window |
| `clob_book_snapshots` | Engine (`clob_feed.py`) | Every 10s | Full order-book snapshot (bids+asks array) — PE-01 was a silent 4-day gap here |
| `macro_signals` | macro-observer | ~Every 60s | Qwen/LightGBM macro bias classification per timescale |
| `market_snapshots` | data-collector | ~2-3 writes/sec | Gamma API Polymarket token prices per active window |
| `manual_trade_snapshots` | Engine (`execute_manual_trade`) | Per manual trade | v4_snapshot, v3_snapshot, last-5 resolved windows, engine decision, operator rationale |

**Write-rate sanity checks** (run from the hub DB or locally with `$DATABASE_URL`):

```sql
-- V4 decision ticks (should be ~12/min)
SELECT COUNT(*), MAX(created_at) FROM ticks_v4_decision WHERE created_at > now() - interval '5 minutes';

-- Strategy decisions (should be ~60/min when engine is running)
SELECT COUNT(*), MAX(created_at) FROM strategy_decisions WHERE created_at > now() - interval '5 minutes';

-- Chainlink oracle ticks (should be ~60/min = 4 assets × 15/min)
SELECT COUNT(*), MAX(ts) FROM ticks_chainlink WHERE ts > now() - interval '1 minute';

-- Paper trades OPEN for > 10 minutes (should be 0 after the stale-trade fix in PR #128)
SELECT COUNT(*) FROM trades WHERE status = 'OPEN' AND is_live = false AND created_at < now() - interval '10 minutes';
```

---

## Analysis Scripts

**Full reference:** `docs/analysis/SIGNAL_EVAL_RUNBOOK.md` — complete agent guide for signal evaluation analysis. Read this before running any analysis.

### Quick start (any agent can run this)

```bash
# Get DB URL from Railway dashboard (DATABASE_PUBLIC_URL) or Montreal SSH:
# sudo grep '^DATABASE_URL=' /home/novakash/novakash/engine/.env | sed 's/postgresql+asyncpg/postgresql/'
export PUB_URL="postgresql://postgres:PASSWORD@hopper.proxy.rlwy.net:35772/railway"

# Full current-state report — all signals, all strategies, config recommendations:
python3 docs/analysis/full_signal_report.py

# Window accuracy surface (magic window analysis):
python3 docs/analysis/run_window_analysis.py
```

### What `full_signal_report.py` covers

1. **Data coverage** — windows available, date range, SE eval count
2. **Current regime** — last 4h UP% vs DOWN%, VPIN, HMM regime
3. **Ungated signal accuracy** — by eval_offset bucket (T-30 to T-240), by confidence band
4. **V4 paper trade performance** — TRADE count, W/L, skip reason distribution
5. **V10 ghost performance** — gate failure breakdown, would-have W/L
6. **CLOB divergence check** — was Sequoia ahead of CLOB at trade points?
7. **Config recommendations** — threshold changes, regime filters, timing adjustments

### Key schema gotchas (always check these)

- `window_snapshots.actual_direction` **DOESN'T EXIST** — use `CASE WHEN close_price > open_price THEN 'UP' WHEN close_price < open_price THEN 'DOWN' END`
- `window_snapshots.oracle_outcome` is NULL — not populated by reconciler
- `strategy_decisions.metadata_json::jsonb->'_ctx'` holds the full signal vector (VPIN, regime, V4 surface, CLOB ask etc)
- `ROUND(double precision, int)` fails in PostgreSQL — cast to `::numeric` first
- `text()` with `::timestamptz` SQL cast confuses SQLAlchemy — use `CAST(:param AS timestamptz)` instead
- `ticks_v3_composite` joins to `window_snapshots` by time bucket, not exact match

### Config decision framework

| Last 4h ungated accuracy | Action |
|--------------------------|--------|
| > 65% | Keep config, can increase position size |
| 55-65% | Keep config, reduce position size |
| 45-55% | Tighten confidence threshold |
| < 45% | Pause, investigate regime change |

### Analysis script: `run_window_analysis.py`

`docs/analysis/run_window_analysis.py` — window-level accuracy surface analysis.

Queries `signal_evaluations` and produces:
- Accuracy-by-eval-offset table (T-60, T-90, T-120, T-150, T-180)
- Confidence-distance threshold sweep (WR at each `confidence_distance` cut)
- CLOB ask asymmetry check (DOWN + cheap NO cross-tab)
- Session-level regime breakdown

**Confirmed findings (70,272 windows, 2026-04-12):**
- Sweet spot: **T-120 to T-150** (55.5% accuracy, peaks at T-135)
- Cliff at T-90: drops to **48.7%** — CLOB has priced outcome, signal lags
- Only trade: `confidence_distance >= 0.12` (strong/high bands = 64-65% WR)
- CLOB asymmetry: DOWN + NO ask <= $0.58 = 90%+ WR (but 84% DOWN dataset — bearish bias caveat)

Key findings as of 2026-04-12 Session 4:
- **T-120–T-150 sweet spot** for eval offset (highest out-of-sample accuracy).
- **confidence_distance >= 0.12** is the practical gate floor — below this the signal is noise.
- **CLOB ask asymmetry WR ~82%** (DOWN + cheap NO) but bearish-dataset caveat applies — revalidate once 200+ mixed-regime V4 windows accumulate.

---

## Appendix A — Canonical commands

### EC2 Instance Connect one-liner (Montreal engine)

```bash
ssh-keygen -t ed25519 -f /tmp/ec2_temp_key -N "" -q && \
aws ec2-instance-connect send-ssh-public-key \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user novakash \
  --ssh-public-key file:///tmp/ec2_temp_key.pub \
  --availability-zone ca-central-1b \
  --region ca-central-1 && \
ssh -o StrictHostKeyChecking=no -o IdentitiesOnly=yes \
  -i /tmp/ec2_temp_key novakash@15.223.247.178
```

### Vite build + playwright render check (frontend)

```bash
cd frontend && npm run build
# If the build is green, smoke-test a page render against the dev server:
npm run dev &
sleep 3
npx playwright test --headed  # or hit /audit with a browser
```

### Deploy verification (after merge to develop)

```bash
# Poll GHA runs for the workflow(s) that fired
gh run list -R billybrichards/novakash --workflow=deploy-engine.yml --limit 5
gh run list -R billybrichards/novakash --workflow=deploy-macro-observer.yml --limit 5
gh run list -R billybrichards/novakash --workflow=deploy-margin-engine.yml --limit 5

# Tail the most recent run
gh run view --log -R billybrichards/novakash <run-id>

# Manually re-fire a deploy
gh workflow run deploy-engine.yml         -R billybrichards/novakash       --ref develop
gh workflow run deploy-macro-observer.yml -R billybrichards/novakash       --ref develop
gh workflow run deploy-data-collector.yml -R billybrichards/novakash       --ref develop
gh workflow run deploy-margin-engine.yml  -R billybrichards/novakash       --ref develop
gh workflow run deploy-frontend.yml       -R billybrichards/novakash       --ref develop
gh workflow run ci.yml                    -R billybrichards/novakash-timesfm --ref main
```

### PR merge with cleanup

```bash
gh pr merge <pr-number> -R billybrichards/novakash --squash --delete-branch
```

### Remote engine restart

```bash
# Via EC2 IC (fresh key + ssh in one call, then restart)
ssh ubuntu@15.223.247.178 'sudo bash /home/novakash/novakash/scripts/restart_engine.sh'

# Then verify from the same SSH session
sudo tail -n 100 /home/novakash/engine.log
pgrep -fa 'python3 main.py'
```

### Database health queries

```bash
psql "$DATABASE_URL" -c "
  -- Recent signal evaluations (should be steady)
  SELECT COUNT(*), MAX(created_at) FROM signal_evaluations WHERE created_at > now() - interval '5 minutes';

  -- CLOB book snapshots (must be > 0 — if zero, PE-01 has regressed)
  SELECT COUNT(*) FROM clob_book_snapshots WHERE ts > now() - interval '1 minute';

  -- Macro signals cadence (should be ~1/minute)
  SELECT id, created_at, bias, confidence, direction_gate
  FROM macro_signals ORDER BY id DESC LIMIT 5;

  -- Dual-writer check for macro-observer
  SELECT COUNT(*) FROM macro_signals
  WHERE created_at > now() - interval '10 minutes'
    AND reasoning LIKE '%Fallback%';
  -- Expected: 0

  -- Data collector write rate
  SELECT date_trunc('second', snapshot_at) AS sec, COUNT(*) AS writes
  FROM market_snapshots WHERE snapshot_at > now() - interval '15 seconds'
  GROUP BY 1 ORDER BY 1 DESC;
  -- Expected: 2-3 per second; 5-6+ means dual-writer
"
```

### Parallel-agent dispatch checklist

Before using `superpowers:dispatching-parallel-agents` for a batch of work:

- [ ] Each agent has an explicit list of files it OWNS (can edit).
- [ ] Each agent has an explicit list of files it MUST NOT TOUCH (e.g. `AuditChecklist.jsx`,
      `V4Panel.jsx`, `docs/AUDIT_PROGRESS.md`).
- [ ] No two agents touch the same file.
- [ ] Read-only investigations (`Agent D`, `Agent E`) are clearly marked READ-ONLY in the prompt.
- [ ] Any trading-logic change is gated behind a default-off feature flag and the prompt cites
      `V10_6_ENABLED` or `MARGIN_ENGINE_USE_V4_ACTIONS` as the precedent.
- [ ] Each agent opens its own PR, never pushing directly to `develop`/`main`.

---

## Appendix B — Quick reference to other docs

| Doc | What it is |
|---|---|
| **`docs/AUDIT_PROGRESS.md`** | **THE PROGRESS TRACKER.** Chronological session log, one section per distinct work block. Every `/audit` `progressNotes` entry has a matching bullet here. **Read this FIRST when starting a session** — it tells you what's been shipped, what's in flight, and what's next. |
| **`/audit` page in the frontend** | Master project to-do list (open / in-progress / done) with filter + severity + category. Edit `frontend/src/pages/AuditChecklist.jsx` `TASKS` array in-file, no backend writes. The UI is the authoritative status view; this doc is the rationale log. |
| `docs/CI_CD.md` | **Authoritative** CI/CD reference. Branch conventions, service map, secrets, deploy mechanics, rollback paths, dual-writer defence. Read before touching any `.github/workflows/` file. Mirrored in both repos. |
| `docs/CLEAN_ARCHITECT_MIGRATION_PLAN.md` | **CA-01..04 plan.** 9-phase migration from the 3,109-LOC `five_min_vpin.py` god class to the margin_engine-style clean architecture. Phase 0 (ports.py) is the zero-risk entry point. Read this before touching anything in `engine/strategies/` or `engine/signals/`. |
| `docs/CONFIG_MIGRATION_PLAN.md` | **CFG-01 plan.** Full env-var-to-DB migration across all 6 services, 142 keys inventoried, 11 CFG-* sub-tasks scheduled. Read this before adding new runtime config. |
| `docs/FRONTEND_AUDIT_2026-04-11.md` | Read-only audit of every frontend route. 21 pages OK, 5 partial, 5 stale, 1 pure-mock. Proposed FE-08..FE-13 remediation tasks. Read this before touching any frontend page you're not already working on. |
| `docs/POST_MERGE_AUDIT_2026-04-11.md` | Verification of every PR from the 2026-04-11 afternoon batch. Pass/fail per PR, AST parse + test results, P0 findings. Consult before declaring any of those PRs "landed safely". |
| `docs/V10_6_DECISION_SURFACE_PROPOSAL.md` | (timesfm repo) V10.6 decision surface spec grounded in 85,805 Sequoia v4 predictions + 198 backfilled live trades. The 865-outcome evidence base. |
| `docs/SEQUOIA_V5_GO_LIVE_LOG.md` | (timesfm repo) Sequoia v5.2 promotion timeline. ECE 0.0643 (35% better than v4), skill +7.23pp, live since 2026-04-10 13:21:33 UTC. |
| `docs/CHANGELOG-DQ01-POLY-SPOT-ONLY-CONSENSUS.md` | DQ-01 motivation + activation plan + rollback. Reference for the `V11_POLY_SPOT_ONLY_CONSENSUS` flag. |
| `frontend/src/pages/AuditChecklist.jsx` | The `/audit` page. Edit `TASKS`, `CATEGORIES`, `SESSION_META` in-file. No backend writes. |
| `frontend/src/pages/Deployments.jsx` | The `/deployments` page. `SERVICES` array mirrors `docs/CI_CD.md`. Live probes every 15s for services with HTTP health. |
| `frontend/src/pages/Schema.jsx` | (SCHEMA-01 in flight) The `/schema` page — all DB tables, purposes, writers, readers, active/legacy status. Authoritative DB inventory view. |
| `frontend/src/pages/Config.jsx` | (CFG-02/03 in flight) The `/config` page — read-only view of all 142 runtime config keys across every service. Write access ships in CFG-04. |
| `.github/workflows/deploy-engine.yml` | Canonical error-signature gate example (`CI-01`, step 12). |
| `.github/workflows/deploy-macro-observer.yml` | Canonical template for every other AWS deploy workflow. |
| `scripts/restart_engine.sh` | Canonical engine restart. Rotates the log, kills existing, starts new, verifies one PID. Never replace with a raw `nohup`. |
| `engine/config/runtime_config.py` | Env-var convention. DB `trading_configs` > env vars > code defaults. Singleton `runtime` synced every heartbeat (~10s). **Being superseded** by CFG-01 `config_keys` tables — read the migration plan first. |
| `engine/signals/gates.py` | Where gate logic lives. `V10_6_ENABLED` short-circuit at line 201 is the canonical feature-flag pattern. `V11_POLY_SPOT_ONLY_CONSENSUS` in `SourceAgreementGate` is the 2026-04-11 addition. |
| `margin_engine/use_cases/open_position.py` | The clean-architecture reference. `_execute_v4()` is the 10-gate v4 stack with the DQ-07 `mark_divergence` gate at 9.5. This is what `engine/` should look like after CA-01..CA-04 land. |

---

## Appendix D — Future task queue (pick these up in any order, with the rules below)

These are the tasks that were scoped and **not yet shipped** as of 2026-04-11 evening. Each one
is well-defined enough that a fresh agent can pick it up without re-reading the whole repo.
**Rules every agent must follow**:

1. **Check `docs/AUDIT_PROGRESS.md` first** to confirm the task isn't already done.
2. **Read the task's cited prior-art files before writing any new code.**
3. **Default-off feature flag for any trading-logic change.** No exceptions.
4. **`/audit` checklist row updated in the SAME commit** as the code change, with a
   `progressNotes` entry citing the PR number and file:line pointers.
5. **Scope strictness.** If your PR touches files outside the task's declared scope, either
   justify it in the PR body or abort and re-scope. `git diff origin/develop -- UNSCOPED_DIRS`
   must be empty.
6. **Both repos when applicable.** SPARTA + CI_CD + shared docs live in both `novakash` and
   `novakash-timesfm-repo`. If you touch one, touch the other in the matching PR.
7. **No nested agent dispatches from inside a background agent.** The main session is the only
   dispatch root. Agents that spawn sub-agents fragment the work and make worktrees unrecoverable.

### CA-01..04 Phase 0 — port protocols in `engine/domain/ports.py`

- **What**: Add a new file `engine/domain/ports.py` containing the 8 port protocols defined in
  `docs/CLEAN_ARCHITECT_MIGRATION_PLAN.md` §4 (MarketFeedPort, ConsensusPricePort,
  SignalRepository, PolymarketClient, AlerterPort, Clock, WindowStateRepository, ConfigPort).
  No concrete implementations, no use-case extraction — just the protocols, sibling to existing
  code. Zero runtime risk.
- **Rules**: Read `margin_engine/domain/ports.py` first. Match that style. Use `typing.Protocol`.
- **Scope**: exactly `engine/domain/ports.py` (new file). Zero other files touched. Adds a new
  `domain/` directory alongside existing `strategies/`, `signals/`, etc.
- **Verification**: `python3 -c "from engine.domain import ports; print([p.__name__ for p in dir(ports)])"` lists all 8 protocols.
- **Audit row**: `CA-01` progressNotes "Phase 0 shipped — ports.py as read-only scaffold".

### CA-01..04 Phase 1 — value objects in `engine/domain/value_objects.py`

- **What**: Extract `Window`, `Tick`, `DeltaSet`, `SignalEvaluation`, `ClobSnapshot`,
  `GateContext`, `GateResult` into a new `engine/domain/value_objects.py` as immutable dataclasses.
  `GateContext` and `GateResult` already exist in `engine/signals/gates.py` — move them and
  re-export from the old location for backward compat.
- **Rules**: Immutable (`@dataclass(frozen=True)` where the field isn't mutated), no methods
  beyond `from_dict` / `to_dict`. No I/O. Follows `margin_engine/domain/value_objects.py` pattern.
- **Scope**: `engine/domain/value_objects.py` (new), `engine/signals/gates.py` (re-export),
  `engine/tests/test_gates.py` or similar (update imports if any).
- **Verification**: All existing engine tests still pass.
- **Audit row**: `CA-01` progressNotes "Phase 1 shipped — value objects extracted".

### CFG-04 — write endpoints + config hot-apply

- **What**: Extend `hub/api/config.py` (shipped by CFG-02/03) with write endpoints:
  - `POST /api/v58/config` — upsert a single key (service, key, value, comment), writes to
    `config_values` + `config_history`
  - `POST /api/v58/config/bulk` — apply multiple changes atomically in one transaction
  - `POST /api/v58/config/rollback/{history_id}` — restore a prior value from `config_history`
- **Rules**: JWT `Depends(get_current_user)` enforced. Every write MUST insert a `config_history`
  row in the same transaction as the `config_values` upsert — never orphan a write. Return
  `{restart_required: true}` in the response body for any key with `restart_required=TRUE` so
  the frontend can warn the operator.
- **Scope**: `hub/api/config.py`, `hub/tests/test_config_api.py`, `frontend/src/pages/Config.jsx`
  (add the edit widgets), `frontend/src/pages/AuditChecklist.jsx` (CFG-04 row DONE).
- **Verification**: unit tests for each endpoint + round-trip test (POST a value, GET it back,
  POST a different value, GET the history, rollback).
- **Audit row**: `CFG-04` progressNotes "Write endpoints + config hot-apply shipped".

### CFG-07 — engine service-side loader with TTL cache + safe degrade

- **What**: Add a `ConfigLoader` class in `engine/config/loader.py` that replaces the inline
  `os.environ.get` calls with a DB-backed lookup. TTL cache refresh every 10s (same cadence as
  the existing heartbeat). Fallback chain: DB → .env → code default. If the DB query fails
  (transient outage), use the last-known-good cache for up to 5 minutes before fail-closing.
- **Rules**: **Never fail-close a trading-hot-path lookup on a transient DB error.** The loader
  must degrade gracefully. Gate flags that require a restart must be loaded ONCE at startup and
  cached immutably — the loader must NOT hot-swap those mid-window (see CFG-01 plan §9 hot-reload
  risk matrix).
- **Scope**: `engine/config/loader.py` (new), `engine/strategies/five_min_vpin.py` (call sites),
  `engine/signals/gates.py` (call sites). **Do not touch `runtime_config.py`** — it stays as the
  legacy sync for now, to be removed in Phase 3 after cutover validation.
- **Verification**: Characterisation tests pinning pre-cutover behaviour, then post-cutover
  tests pinning new loader behaviour, then a toggle flag `ENGINE_USE_DB_CONFIG_LOADER=true` on
  a single dev box for 24h before flipping in prod.
- **Audit row**: `CFG-07` progressNotes with the 24h validation window notes.

### SCHEMA-01b — auto-discovery of new DB tables (follow-up to SCHEMA-01)

- **What**: Extend the `/schema` page with a "discover" button that runs `SELECT table_name FROM
  information_schema.tables WHERE table_schema = 'public'` and diffs the live table list against
  `hub/db/schema_catalog.py`. Any tables in the DB but NOT in the catalog appear as a "drift"
  warning. Any tables in the catalog but NOT in the DB appear as "missing in prod".
- **Rules**: Read-only. Does NOT auto-add entries to the catalog — the catalog is intentionally
  hand-curated. The warning is the signal that a human needs to add the entry.
- **Scope**: `hub/api/schema.py`, `frontend/src/pages/Schema.jsx`, `frontend/src/pages/AuditChecklist.jsx`.
- **Verification**: Create a throwaway table in a test DB, confirm it shows up as "drift".
- **Audit row**: `SCHEMA-01b` progressNotes.

### SQ-01 PR 1 — cosmetic ELM → unbranded rename

- **What**: The 4-PR rollout plan from Agent E (see `docs/AUDIT_PROGRESS.md`). PR 1 is
  **cosmetic only**: rename `engine/data/feeds/elm_prediction_recorder.py` →
  `prediction_recorder.py`, `class ELMPredictionRecorder` → `PredictionRecorder`, ctor kwarg
  `elm_client` → `model_client`, update all 4 call sites. DO NOT touch log event names
  (`elm_recorder.*`), DB table name (`ticks_elm_predictions`), or the `signal_evaluations`
  gate column names — those are PR 2, 3, 4 respectively.
- **Rules**: The CI-02 gate (shipped PR #49) has zero-tolerance signatures on
  `elm_recorder.write_error` and `elm_recorder.query_error`. PR 1 must NOT touch these log
  strings or the deploy will fail loudly. PR 2 is the one that ships those rename plus a
  matching CI gate signature update in the same commit.
- **Scope**: `engine/data/feeds/elm_prediction_recorder.py` → renamed file, `engine/strategies/orchestrator.py`
  lines 665-676, `engine/tests/test_elm_prediction_recorder.py` → renamed file.
- **Verification**: Engine tests still pass. Log events are unchanged (grep for `elm_recorder.`
  in a post-rename diff — must still appear).
- **Audit row**: `SQ-01` progressNotes "PR 1 of 4 (cosmetic class rename)".

### POLY-SOT-b — extend reconciler to automatic engine trades ✅ SHIPPED (PR #66)

- **Status**: DONE. Shipped 2026-04-11 alongside POLY-SOT-c below.
- **What shipped**: 8 new columns on the `trades` table mirroring `manual_trades_sot`. New
  `reconcile_trades_sot()` method that walks the trades table on every 2-minute reconciler
  pass. Single `_sot_reconciler_loop` asyncio task now walks BOTH tables in each pass —
  preferred over a sibling loop because it keeps the asyncio surface area minimal. Shared
  `_compare_to_polymarket(row, poly_status)` helper extracted so the decision matrix lives in
  exactly one place (no copy-paste drift between the two passes).
- **Alert dedupe key**: Namespaced by table (`manual_trades:42` vs `trades:42`) so the same
  numeric ID across tables doesn't collide. Telegram message tagged MANUAL or AUTO prefix
  so the operator can tell at a glance which surface fired.
- **Hub**: new `GET /api/v58/trades-sot?limit=50` endpoint mirrors `/manual-trades-sot`.
- **Frontend**: `TradeTicker.jsx` accepts a new `sotRows` prop (separate from `manualSotRows`)
  and renders an `AUTO`-prefixed chip. `ExecutionHQ.jsx` fetches `/v58/trades-sot` in parallel.
- **Tests**: 12 new cases in `engine/tests/test_reconcile_trades_sot.py` mirroring the Phase 1
  suite, plus a cross-table dedupe test verifying `manual_trades #42` and `trades #42` are
  independent. Existing 12 Phase 1 tests still pass unmodified.
- **Scope verified**: `git diff origin/develop -- margin_engine/` → empty.

### POLY-SOT-c — one-shot historical backfill ✅ SHIPPED (PR #66)

- **Status**: DONE. Shipped 2026-04-11 alongside POLY-SOT-b above, but **requires an explicit
  operator run** — merging the PR does not run the backfill.
- **What shipped**: `engine/scripts/backfill_sot_reconciliation.py` — a one-shot script that
  walks every NULL-state row in both `manual_trades` and `trades`, calls
  `poly_client.get_order_status_sot(order_id)` for rows that persist an order ID, and tags
  each row using the same `_compare_to_polymarket` helper as the forward reconciler. Rows
  older than 24h with no order ID get a new terminal state `no_order_id`. Rate-limited
  100 ms between Polymarket calls. Dry-run mode (`--dry-run`) prints decisions without
  writing. Idempotent — re-runs are no-ops because the query filter is `WHERE
  sot_reconciliation_state IS NULL`. Exit codes: 0 success / 1 fatal / 2 partial.
- **Required operator run** (ONE-SHOT, Montreal box only because Polymarket geo-blocks
  everywhere else):
  ```
  cd /home/novakash/novakash/engine
  python3 scripts/backfill_sot_reconciliation.py --table both --dry-run
  # Review the output, then:
  python3 scripts/backfill_sot_reconciliation.py --table both
  ```
- **Why it must run on Montreal specifically**: the script calls the Polymarket CLOB REST API,
  which is geo-restricted. Running it from anywhere else returns 451/403. The same reason
  the engine itself runs on Montreal (see "Montreal rules" earlier in this doc).
- **Audit row**: `POLY-SOT-c` DONE in AuditChecklist with the operator command in progressNotes.

### LT-05 — click-to-execute latency SLA dashboard

### LT-05 — click-to-execute latency SLA dashboard

- **What**: Surface p50 / p95 / p99 click-to-execute latency on the ExecutionHQ Live tab,
  measured from the hub's INSERT commit to the engine's `polymarket_client.place_order` return.
  Uses the `manual_trades.created_at` and `manual_trades.executed_at` timestamps. Alert if p95
  exceeds 1s (the LT-04 SLA).
- **Rules**: Read-only page. Calls a new `/api/v58/manual-trade-latency?window=1h` hub endpoint.
- **Scope**: `hub/api/v58_monitor.py` (new endpoint), `frontend/src/pages/execution-hq/components/*` (new
  chip or sub-panel), `frontend/src/pages/AuditChecklist.jsx` (LT-05 row OPEN/DONE).
- **Audit row**: `LT-05`.

### UI-03 — multi-market ManualTradePanel (conditionally enable for ETH 5m)

- **What**: Once UI-02 is live (shipped PR #55) and POLY-SOT is live, the ManualTradePanel can
  be safely enabled for a second market. Start with ETH 5m. Behind a feature flag
  `UI_ENABLE_MULTI_MARKET_MANUAL_TRADE=false` so the default stays BTC-only.
- **Rules**: Flag default-off. Needs end-to-end test: place a paper trade on ETH 5m, confirm
  the decision snapshot DB captured it, confirm the reconciler tagged it correctly. Only
  enable on real money AFTER the paper round-trip is clean.
- **Scope**: `frontend/src/pages/execution-hq/components/ManualTradePanel.jsx`, `hub/api/v58_monitor.py`
  (if it hardcodes BTC anywhere).
- **Audit row**: `UI-03`.

### FE-09..FE-13 — remediate the frontend audit findings

- `FE-09` MEDIUM — retire / wire `/indicators` (currently 100% mock data from
  `src/lib/mock-data.js`). Options: delete the route, or wire it to real indicator data.
- `FE-10` HIGH — fix silent demo-data fallbacks on Dashboard / Paper / Positions / Risk. Add a
  visible "demo data" banner when the hub returns empty.
- `FE-11` MEDIUM — wire real candles into Execution HQ price chart (currently locally simulated).
- `FE-12` LOW — retire / rev the Changelog page (9 versions behind).
- `FE-13` LOW — legacy `elm` signal key on `/composite` and `/data/v3`. Blocked on SQ-01 PR 3.

### Each FE-0X task:
- **Read `docs/FRONTEND_AUDIT_2026-04-11.md`** for the specific file:line evidence
- **Scope**: only the pages cited in the evidence, and `AuditChecklist.jsx` for the row flip
- **Audit row**: status flip + progressNotes

---

---

## Appendix C — Repo heads at time of writing

| Repo | Deploy branch | Typical `HEAD` when this guide was written |
|---|---|---|
| `novakash` | `develop` | `d071d04` — Agent D+E integration merge (PR #34) |
| `novakash-timesfm-repo` | `main` | `af51523` — CI_CD.md env correction + paths-ignore for docs-only pushes |

When you start a session, run `git fetch origin && git log --oneline -10 origin/develop` (and
`origin/main` on the timesfm side) to see what's changed since this guide was committed. If
the 2026-04-11 incident list has grown, check `docs/AUDIT_PROGRESS.md` for the new entries and
update the relevant sections of this guide in a follow-up PR.

---

*End of guide. Good hunting.*
