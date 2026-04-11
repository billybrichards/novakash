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

### Category severity conventions

- **CRITICAL red** — trading-loss class. Stops the line.
- **HIGH amber** — data-quality or production error. Fix within the session.
- **MEDIUM cyan** — observability / architecture. Fix when you're already in the file.
- **LOW grey** — cosmetic. Batch these.
- **INFO cyan** — informational, no action required.

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
| `docs/CI_CD.md` | **Authoritative** CI/CD reference. Branch conventions, service map, secrets, deploy mechanics, rollback paths, dual-writer defence. Read before touching any `.github/workflows/` file. Mirrored in both repos. |
| `docs/AUDIT_PROGRESS.md` | Living session log for the 2026-04-11 clean-architect audit. Chronological order. One bullet per action, one paragraph per decision. Every `/audit` `progressNotes` entry has a matching bullet here. |
| `docs/V10_6_DECISION_SURFACE_PROPOSAL.md` | (timesfm repo) V10.6 decision surface spec grounded in 85,805 Sequoia v4 predictions + 198 backfilled live trades. The 865-outcome evidence base. |
| `docs/SEQUOIA_V5_GO_LIVE_LOG.md` | (timesfm repo) Sequoia v5.2 promotion timeline. ECE 0.0643 (35% better than v4), skill +7.23pp, live since 2026-04-10 13:21:33 UTC. |
| `frontend/src/pages/AuditChecklist.jsx` | The `/audit` page. Edit `TASKS`, `CATEGORIES`, `SESSION_META` in-file. No backend writes. |
| `frontend/src/pages/Deployments.jsx` | The `/deployments` page. `SERVICES` array mirrors `docs/CI_CD.md`. Live probes every 15s for services with HTTP health. |
| `.github/workflows/deploy-engine.yml` | Canonical error-signature gate example (`CI-01`, step 12). |
| `.github/workflows/deploy-macro-observer.yml` | Canonical template for every other AWS deploy workflow. |
| `scripts/restart_engine.sh` | Canonical engine restart. Rotates the log, kills existing, starts new, verifies one PID. Never replace with a raw `nohup`. |
| `engine/config/runtime_config.py` | Env-var convention. DB `trading_configs` > env vars > code defaults. Singleton `runtime` synced every heartbeat (~10s). |
| `engine/signals/gates.py` | Where gate logic lives. `V10_6_ENABLED` short-circuit at line 201 is the canonical feature-flag pattern. |
| `margin_engine/use_cases/open_position.py` | The clean-architecture reference. `_execute_v4()` is the 10-gate v4 stack. This is what `engine/` should look like after CA-01..CA-04 land. |

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
