# CI/CD — Novakash System

**Status:** living document. Last rewritten 2026-04-11 after the macro-observer + data-collector Railway → AWS migration.

This is the single authoritative reference for where each service deploys from, what secrets it needs, and how to verify or roll it back. It is duplicated verbatim in `novakash-timesfm-repo/docs/CI_CD.md` (on `main`) and `novakash/docs/CI_CD.md` (on `develop`) so an operator in either repo finds it without a cross-repo hunt.

## Branch conventions

| Repo | Deploy branch | PR branch | Why |
|---|---|---|---|
| `novakash-timesfm-repo` | **`main`** | `main` (PRs target main) | Model service is a single codebase; `main` is what runs in prod |
| `novakash` | **`develop`** | `develop` (PRs target develop) | `main` is the audit/release branch, `develop` is what actually runs |

Deploys are **gated to push events**, not PRs. Merging a PR to the deploy branch is the deploy.

## Service deployment map

| Service | Repo | Target | Workflow | Fires on | Health |
|---|---|---|---|---|---|
| **timesfm-service** | novakash-timesfm-repo | EC2 Montreal `3.98.114.0:8080` | `.github/workflows/ci.yml` (lint → deploy) | push to `main` | `/health`, `/v2/health`, `/v4/snapshot` |
| **macro-observer** | novakash | EC2 Montreal `3.98.114.0` (sibling dir) | `.github/workflows/deploy-macro-observer.yml` | push to `develop`, path `macro-observer/**` | Docker healthcheck on `/tmp/observer.alive` (180s window) |
| **data-collector** | novakash | EC2 Montreal `3.98.114.0` (sibling dir) | `.github/workflows/deploy-data-collector.yml` | push to `develop`, path `data-collector/**` | Docker healthcheck on `/tmp/collector.alive` (30s window) |
| **margin-engine** | novakash | EC2 eu-west-2 (`MARGIN_ENGINE_HOST`) | `.github/workflows/deploy-margin-engine.yml` | push to `develop`, path `margin_engine/**` | systemd `margin-engine.service` active state |
| **hub (API)** | novakash | Railway (service `hub`) | Railway auto-deploy from `develop` | push to `develop` | 401 on `/api/*` = healthy (auth wall present) |
| **frontend (web)** | novakash | EC2 (`AWS_FRONTEND_HOST`) via nginx | `.github/workflows/deploy-frontend.yml` | push to `develop`, path `frontend/**` | 200 on `/` + static asset etag |
| **engine (normal trading)** | novakash | Railway (service `engine`) | **NONE — no CI/CD** | manual pushes, Railway watcher only | **unmonitored, often CRASHED** |

### ⚠ Known gap — `engine/` has no proper CI/CD

The original trading engine (`engine/` in `novakash`, Railway service name `engine`, domain `engine-staging-617d.up.railway.app`) is the only major service **without a GitHub Actions deploy workflow**. It still relies on Railway's git-watcher auto-deploy and has been observed CRASHED or FAILED in recent deploy history.

What's missing, in priority order:

1. **No import/smoke test** on push — broken imports land in prod
2. **No deploy-gated secrets check** — a missing env var silently crashes the container
3. **No post-deploy health probe** — a start-time crash is only visible by tailing Railway logs
4. **No rollback path** beyond "Railway redeploy from dashboard" — no versioned artifacts, no rsync-of-dist pattern
5. **No migration path off Railway** — the two services that were migrated this week (macro-observer, data-collector) had the same problem and now run on the Montreal EC2. The engine should follow.

Do NOT consider this document complete for the engine service. Treat it as a TODO item — the migration template is the `deploy-macro-observer.yml` + `deploy-data-collector.yml` pair; both are ~150 lines and share the same pattern (rsync + docker compose + heartbeat healthcheck). Engine is the next candidate for that template.

## Secrets reference

All values are set on `billybrichards/novakash` and `billybrichards/novakash-timesfm` (Settings → Secrets and variables → Actions). The first step of every deploy workflow (`Require runtime secrets`) fails loudly if any required secret is empty.

### Shared between both repos

| Secret | Who reads it | What it is |
|---|---|---|
| `DATABASE_URL` | margin-engine, macro-observer, data-collector, hub, timesfm-service | `postgresql://...@hopper.proxy.rlwy.net:35772/railway` — shared Postgres |
| `COINGLASS_API_KEY` | timesfm-service v2 poller | CoinGlass API v3/v4 bearer |
| `TIINGO_API_KEY` | timesfm-service (when v2 Tiingo poller lands) | Tiingo crypto feed |
| `POLYGON_RPC_URL` | timesfm-service (when v2 Chainlink poller lands) | Polygon mainnet RPC for Chainlink aggregators |
| `ANTHROPIC_API_KEY` | legacy macro-observer path (unused since Qwen migration) | Kept for fallback |

### Montreal EC2 (`3.98.114.0`) — shared across timesfm, macro-observer, data-collector

| Secret | Repo | Notes |
|---|---|---|
| `DEPLOY_HOST` | novakash-timesfm-repo | `3.98.114.0` |
| `DEPLOY_SSH_KEY` | novakash-timesfm-repo | Original SSH key used by ci.yml for timesfm-service |
| `MACRO_OBSERVER_HOST` | novakash | `3.98.114.0` (same box) |
| `MACRO_OBSERVER_SSH_KEY` | novakash | Fresh ED25519 key bootstrapped 2026-04-11. Reused by `deploy-data-collector.yml` — same user, same box, meaningful isolation would be ceremony |

### eu-west-2 margin-engine

| Secret | Repo |
|---|---|
| `MARGIN_ENGINE_HOST` | novakash |
| `MARGIN_ENGINE_SSH_KEY` | novakash |

### macro-observer Qwen + Telegram

| Secret | Repo |
|---|---|
| `QWEN_API_KEY` | novakash |
| `QWEN_BASE_URL` | novakash — `http://194.228.55.129:39633/v1` |
| `TELEGRAM_BOT_TOKEN` | novakash |
| `TELEGRAM_CHAT_ID` | novakash |

### Frontend EC2

| Secret | Repo |
|---|---|
| `AWS_FRONTEND_HOST` | novakash |
| `AWS_FRONTEND_SSH_KEY` | novakash |

## Deploy mechanics

All five AWS deploy workflows (timesfm ci.yml, deploy-margin-engine, deploy-macro-observer, deploy-data-collector, deploy-frontend) follow the same shape. Read the macro-observer one as the canonical example — it is the cleanest and the most commented.

### The pattern

1. **`Require runtime secrets`** step — fails the run with an explicit missing-secret list before any host contact
2. **SSH key write** — base64 decode first (recommended), fall back to raw PEM
3. **`mkdir -p` on host** — first-deploy bootstrap is idempotent
4. **`rsync -avz --delete --exclude '.env'`** — `.env` is NEVER rsynced (host secrets are templated in the next step)
5. **Template `.env` on host** — every deploy re-templates from GitHub Actions secrets, so credential rotation lands automatically
6. **`docker compose build && up -d --force-recreate`** — `--force-recreate` is critical; without it compose can skip the recreate
7. **Health probe** — 45s for fast healthchecks (data-collector), 60-90s for slower ones (macro-observer), HTTP probe for timesfm
8. **Tail recent logs** — dumped on success for visibility in the run page

### The injection-defence pattern

All five workflows use an `env:` block at the job level to pull `${{ secrets.* }}` out of `${{ }}` interpolation context. Shell `run:` steps reference `$SSH_KEY`, `$HOST`, `$DEPLOY_DATABASE_URL` as plain bash variables. This protects against the [GitHub Actions workflow injection class](https://github.blog/security/vulnerability-research/how-to-catch-github-actions-workflow-injections-before-attackers-do/).

**Never write `${{ github.event.* }}` inside a `run:` script.** Pipe anything untrusted through `env:` first.

### The systemd pattern (margin-engine only)

margin-engine runs as a systemd service, not in Docker. The `.env` lives at `/opt/margin-engine/.env` and is read via `EnvironmentFile=` in the unit file. The "Template v4 env vars" step uses an idempotent `set_env()` bash function that `sed`-replaces an existing line or appends a new one, so the `.env` accumulates keys across deploys without wiping hand-set values. Backups (`.env.bak.<epoch>`) are kept; the oldest are pruned to last 5.

## Health verification after a deploy

### timesfm-service (Montreal 8080)

```bash
curl -sf http://3.98.114.0:8080/health          # liveness
curl -sf http://3.98.114.0:8080/v2/health       # v2 scorer state
curl -sS 'http://3.98.114.0:8080/v4/macro?asset=BTC' | jq '.bias, .timescale_map'
curl -sS 'http://3.98.114.0:8080/v4/snapshot?asset=BTC&timescales=15m' \
  | jq '.timescales."15m".recommended_action'
```

Expected: real Qwen reasoning in `/v4/macro.reasoning`, per-timescale map with BULL/BEAR/NEUTRAL divergence, `recommended_action.reason` is a named gate (not `not_tradeable` — that's the engine's collapsed log, not the snapshot field).

### macro-observer (Montreal, no HTTP)

```bash
ssh ubuntu@3.98.114.0 'docker ps --filter name=macro-observer --format "{{.Names}} {{.Status}}"'
# Expected: macro-observer Up X minutes (healthy)

ssh ubuntu@3.98.114.0 'docker logs --tail 30 macro-observer 2>&1 | grep llm.call_ok | tail -5'
# Expected: structured `llm.call_ok` lines with 5-10s latency and real per-timescale biases
```

And in the DB:

```sql
SELECT id, created_at, bias, confidence, direction_gate, timescale_map IS NOT NULL AS has_ts_map
FROM macro_signals
ORDER BY id DESC LIMIT 5;
```

Expected: rows at 60s cadence, `confidence > 0`, `has_ts_map = true`, no `reasoning LIKE '%Fallback%'`.

### data-collector (Montreal, no HTTP)

```bash
ssh ubuntu@3.98.114.0 'docker ps --filter name=data-collector --format "{{.Names}} {{.Status}}"'
# Expected: data-collector Up X minutes (healthy)
```

DB:

```sql
SELECT COUNT(*) AS last_minute_writes
FROM market_snapshots WHERE snapshot_at > now() - interval '1 minute';
-- Expected: ~360-480 (6-8/sec steady state across 8 (asset, timeframe) pairs at 1Hz)

SELECT date_trunc('second', snapshot_at) AS sec, COUNT(*) AS writes
FROM market_snapshots WHERE snapshot_at > now() - interval '15 seconds'
GROUP BY 1 ORDER BY 1 DESC;
-- Expected: 6-8 per second, NO second showing ~12-16 (that would mean dual writer)
```

### margin-engine (eu-west-2)

The systemd journal is the source of truth:

```bash
ssh ubuntu@$MARGIN_ENGINE_HOST 'sudo journalctl -u margin-engine --no-pager -n 30'
```

Expected (paper mode): repeating `v4 entry skip: reason=not_tradeable ... macro=BEAR/SKIP_UP` lines at 2s cadence. The gate stack is firing; skips are healthy when there's no edge. The specific gate that fired is in the v4 snapshot's `recommended_action.reason` field (visible on the frontend's V4Panel), not in the log line.

### hub (Railway)

```bash
curl -sI https://hub-develop-0433.up.railway.app/api/v4/snapshot  # Expect 401 (auth present = routes live)
curl -sI https://hub-develop-0433.up.railway.app/api/this/404     # Expect 404 (differential test)
```

### frontend (AWS)

```bash
curl -sI http://$AWS_FRONTEND_HOST/ | grep last-modified
# Compare to current deploy timestamp — stale last-modified = nginx cache or bundle not updated
```

## Rollback

### The three paths

1. **GitHub Actions re-run** — every deploy workflow supports `workflow_dispatch`. Find the last known-good run in the Actions tab, click "Re-run all jobs". This re-templates the `.env` and rebuilds the image from the commit that run was pinned to.
2. **Git revert** — revert the offending commit on the deploy branch (`main` for timesfm, `develop` for novakash) and push. The path filter will fire the relevant deploy.
3. **Direct SSH** (emergency only) — `docker compose down && docker compose up -d` on the Montreal or eu-west-2 box. The `.env` on the host is already there from the most recent successful deploy.

### Railway fallback for macro-observer / data-collector / frontend

The Railway service definitions for these three are retained but put to **`sleepApplication: true`** as of 2026-04-11. They can be woken as an emergency fallback if the Montreal or AWS frontend box is unreachable:

```bash
# Wake a Railway service (emergency fallback)
RT=$(python3 -c "import json; print(json.load(open('~/.railway/config.json'))['user']['token'])")
curl -sS -X POST "https://backboard.railway.app/graphql/v2" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RT" \
  -d '{"query": "mutation M($i: ServiceInstanceUpdateInput!) { serviceInstanceUpdate(serviceId: \"<SVC_ID>\", environmentId: \"606d92e2-cf21-4a22-80f8-98b6a65a7f09\", input: $i) }", "variables": {"i": {"sleepApplication": false}}}'
```

Service IDs:
- `macro-observer`: `1000d62a-2a66-47a0-b8f0-6226b8e0f95d`
- `data-collector`: `cc2a3c88-5de6-4242-889c-7e587cb6ae3a`
- `frontend`: `7501ac09-266f-4b62-b1cc-84822086ecb7`
- `engine`: `540ad8b3-c8c1-4a38-b896-9dc946ba01f0` (still primary — NOT asleep)

**Railway wake is a true fallback, not a dual-writer pattern.** The macro-observer and data-collector instances write to the same shared Postgres, so running both Railway and AWS simultaneously causes a dual-writer race (confirmed on 2026-04-11 — the Railway `macro-observer` was writing `NEUTRAL/0 — Fallback — Qwen LLM endpoint unreachable` rows at :02 seconds past every minute alongside the real Montreal writes). If you wake Railway as an emergency fallback, you MUST stop the Montreal container first.

## The dual-writer class of bug

Any service that writes to the shared Postgres can produce a dual-writer race if deploy topology is ambiguous. The defence is:

1. **One source of truth for the deploy config.** macro-observer had both `railway.toml` (watchPatterns) AND the AWS workflow at the same time — Railway silently kept auto-deploying a broken container from pre-Qwen source. Removing `railway.toml` from a migrated service directory is **mandatory**, not optional.
2. **Always verify in the DB.** The authoritative signal that a migration is complete is "write rate matches single-writer expectation". Query `SELECT COUNT(*) / window_seconds` on the target table and compare to what the expected 1-writer rate should be. If it's 2×, you have a dual writer somewhere.
3. **Sleep Railway services, don't delete them.** `serviceInstanceUpdate` with `sleepApplication: true` preserves env vars, secrets, and build history. `serviceDelete` is irreversible. Sleep is the fallback-preserving primitive.

## Common commands cheat sheet

### SSH aliases for the boxes

Add to `~/.ssh/config` for less typing:

```
Host timesfm-box
    HostName 3.98.114.0
    User ubuntu
    IdentityFile ~/.ssh/novakash-deploy.pem

Host margin-engine-box
    HostName <MARGIN_ENGINE_HOST>
    User ubuntu
    IdentityFile ~/.ssh/novakash-deploy.pem
```

### Tail logs live

```bash
# timesfm-service
ssh timesfm-box 'docker logs -f --tail 100 timesfm-api'

# macro-observer
ssh timesfm-box 'docker logs -f --tail 100 macro-observer'

# data-collector
ssh timesfm-box 'docker logs -f --tail 100 data-collector'

# margin-engine
ssh margin-engine-box 'sudo journalctl -u margin-engine -f'
```

### Manually re-fire a deploy

```bash
gh workflow run deploy-macro-observer.yml -R billybrichards/novakash --ref develop
gh workflow run deploy-data-collector.yml -R billybrichards/novakash --ref develop
gh workflow run deploy-margin-engine.yml  -R billybrichards/novakash --ref develop
gh workflow run deploy-frontend.yml       -R billybrichards/novakash --ref develop
gh workflow run ci.yml                    -R billybrichards/novakash-timesfm --ref main
```

### Force restart margin-engine on the host

```bash
ssh $MARGIN_ENGINE_HOST 'sudo systemctl restart margin-engine && sleep 3 && sudo systemctl status margin-engine --no-pager'
```

## Open items

- [ ] **`engine/` service on novakash has no CI/CD** — see "Known gap" section above. Treat as the next migration target.
- [ ] **`deploy-frontend.yml` doesn't path-filter on the workflow file itself** — a workflow-only change won't trigger a redeploy. Compare with `deploy-margin-engine.yml` and `deploy-macro-observer.yml` which both include `.github/workflows/<file>.yml` in their paths filter.
- [ ] **No smoke test in the novakash `deploy-*.yml` workflows.** The timesfm `ci.yml` has a lint job that runs on PRs; novakash only runs CI on the margin engine (`ci.yml`) and doesn't block deploys. A failing lint on `margin_engine/**` would not prevent a deploy. Deploy workflows should `needs: ci` or run their own smoke test inline.
- [ ] **Node 20 actions deprecated** — all five AWS deploy workflows use `actions/checkout@v4` which is pinned to Node 20. GitHub is forcing Node 24 from 2026-06-02. Low-urgency migration.
