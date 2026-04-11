# Macro Observer — Deployment Runbook

## Where it runs

**EC2 Montreal (`3.98.114.0`, ca-central-1)** — same box as `timesfm-service`. Sibling directory layout:

```
/home/ubuntu/
├── timesfm-service/        # /v4 endpoint surface, scorers, forecaster
│   ├── docker-compose.yml
│   ├── .env
│   └── ...
└── macro-observer/         # this service
    ├── docker-compose.yml
    ├── Dockerfile
    ├── observer.py
    ├── requirements.txt
    └── .env                # templated by GitHub Actions on every deploy
```

Migrated from Railway → AWS on 2026-04-11 to:
1. Unify ops with the timesfm service (one box, one log location, one set of credentials)
2. Eliminate Railway's silent build-context misrouting that was keeping the new Qwen 3.5 122B + per-timescale code from actually reaching production for ~24h
3. Drop the ~$10/mo Railway hosting cost (the observer is a ~30 MB Python loop — invisible alongside TimesFM)

## What it does

Polls market conditions every 60 seconds, calls a self-hosted Qwen 3.5 122B endpoint via OpenAI-compatible HTTP, writes a `MacroSignal` row to the shared Railway Postgres `macro_signals` table.

The signal carries **per-timescale bias** (5m / 15m / 1h / 4h) plus an `overall` synthesis. Downstream consumers (the timesfm service's `/v4/macro` endpoint, the margin engine, dashboards) read from the DB.

A second background loop ("evaluator") posts AI-generated commentary to Telegram after each resolved Polymarket window — runs only when `TELEGRAM_BOT_TOKEN` and `QWEN_API_KEY` are both set.

## Deploy mechanism

Push to `develop` → GitHub Actions runs `.github/workflows/deploy-macro-observer.yml` → rsync to EC2 → rebuild Docker image → recreate container → healthcheck.

The workflow only fires on changes inside `macro-observer/**` or to the workflow file itself. PRs targeting `main` do NOT deploy — only push events on `develop` (consistent with the rest of the novakash repo's deploy convention).

You can also manually re-run the workflow from the Actions tab via `workflow_dispatch`.

## Required GitHub Actions secrets

Set these on the `novakash` repo (Settings → Secrets and variables → Actions):

| Secret | What it is | Where to get it |
|---|---|---|
| `MACRO_OBSERVER_HOST` | EC2 host IP (`3.98.114.0`) | Same as timesfm-service |
| `MACRO_OBSERVER_SSH_KEY` | SSH private key for `ubuntu@MACRO_OBSERVER_HOST`, base64 encoded | Same key as timesfm-service's `DEPLOY_SSH_KEY` |
| `DATABASE_URL` | Shared Railway Postgres connection string | Existing — already used by margin_engine deploy |
| `QWEN_API_KEY` | Bearer token for the Qwen 3.5 122B endpoint | From the Vast.ai openclaw-vast ops doc |
| `QWEN_BASE_URL` | Qwen endpoint base URL | `http://194.228.55.129:39633/v1` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (optional — disables evaluator if unset) | Existing — was on Railway |
| `TELEGRAM_CHAT_ID` | Telegram chat ID for evaluator commentary | Existing — was on Railway |

The workflow's first step (`Require runtime secrets`) fails the deploy with a loud error if any of these are missing, so you can't accidentally template an empty `.env` onto the host.

### Encoding the SSH key

The workflow tries `base64 -d` first and falls back to raw PEM. Recommended: encode locally to avoid newline-mangling in the GitHub UI:

```bash
base64 < ~/.ssh/your-deploy-key.pem | gh secret set MACRO_OBSERVER_SSH_KEY
```

Or set it raw if your key is short and you trust the GitHub editor not to mangle newlines.

## Health check

The container has a Docker healthcheck that reads `/tmp/observer.alive`, which `observer.py` touches at the top of every poll loop. If the file is older than 180 seconds, the container is unhealthy and Docker bounces it via `restart: unless-stopped`.

This means: a stuck DB pool, a hanging asyncio.gather, or any failure mode that wedges the main loop will self-heal within ~3 minutes.

## Verify a deploy worked

Three independent signals to confirm the observer is happy:

### 1. Container is running and healthy

```bash
ssh ubuntu@3.98.114.0
docker ps --filter name=macro-observer --format "{{.Names}} {{.Status}}"
# Expected: macro-observer Up X minutes (healthy)

docker logs --tail 30 macro-observer
# Expected: structured `info` log lines from `llm.call_ok` with bias/confidence/latency
# Bad: `Fallback - Qwen LLM endpoint unreachable` repeating
```

### 2. `macro_signals` table is getting fresh non-fallback rows

```sql
SELECT created_at, bias, confidence, direction_gate, reasoning, timescale_map IS NOT NULL AS has_per_ts
FROM macro_signals
ORDER BY created_at DESC
LIMIT 5;
```

Expected: most recent row within 60s, real reasoning text (not "Fallback"), `has_per_ts = true`.

### 3. `/v4/macro` on the timesfm service shows the new bias

```bash
curl -s http://3.98.114.0:8080/v4/macro?asset=BTC | jq '{bias, confidence, direction_gate, reasoning, age_s}'
```

Expected: real bias/confidence, reasoning text quoting actual payload features (not "Fallback").

## Rollback

If the new container is broken and the healthcheck doesn't catch it:

```bash
ssh ubuntu@3.98.114.0
cd /home/ubuntu/macro-observer
docker compose down
# Edit .env if a credential is wrong, OR git revert the bad commit
# on develop and push to trigger a fresh deploy
docker compose up -d
```

Or roll back via the Actions tab: re-run the previous successful "Deploy Macro Observer" workflow run.

The Railway service is **disabled but not deleted** as an emergency fallback. Re-enable it via `railway up --service macro-observer` if EC2 has a sustained outage.

## Architecture notes

- **Why no port exposure?** The observer is a pure background poller. It doesn't accept HTTP requests. Logs go to `docker logs`.
- **Why default bridge network?** Need outbound egress to Binance, Coinbase, Kraken, CoinGlass, the Vast.ai Qwen endpoint, and the shared Railway Postgres. The default bridge gives unrestricted outbound. We deliberately do NOT use `network_mode: host`.
- **Why the heartbeat file instead of an HTTP healthcheck?** Adding HTTP would require running a sidecar HTTP server inside the observer process just for healthchecks, which is more code and another failure mode. Touching `/tmp/observer.alive` at the top of each loop is one line and catches the actual failure mode (loop wedged) more reliably.
- **Why is the workflow on `novakash` and not `novakash-timesfm-repo`?** The macro-observer source code lives in the `novakash` monorepo. Cross-repo workflows would couple the two deploys; keeping each repo's deploy workflow on its own repo keeps them independent.
- **Why does the per-deploy `.env` template hardcode some values like `QWEN_MODEL=qwen35-122b-abliterated`?** These are operational constants that change rarely and don't need to be GitHub secrets. If you swap to a different Qwen model, edit the workflow file, push, deploy.

## Logs

```bash
# tail live
ssh ubuntu@3.98.114.0 'docker logs -f --tail 100 macro-observer'

# json structured queries
ssh ubuntu@3.98.114.0 'docker logs --tail 1000 macro-observer 2>&1 | grep llm.call_ok | tail -20'
```

## Cost

- **Hosting**: $0 marginal (rounding error on the existing c6a.xlarge)
- **Qwen API**: $0 (self-hosted on Vast.ai)
- **Was on Railway**: ~$10/mo for the macro-observer service alone
