# Deployment

## Overview

There are three deployment targets:

1. **Montreal Engine** — manual git pull and restart
2. **Railway Services** (hub, frontend, data-collector) — auto-deploy via git push
3. **TimesFM Service** — Docker container management on AWS

---

## Montreal Engine Deployment

### Rules

- **Push commits from the OpenClaw VPS** (or your local machine)
- **Pull and restart on Montreal**
- ⚠️ **Never push from Montreal directly** — keep the server clean
- ⚠️ **Never run `git push` from the Montreal engine server**
- ⚠️ **Always restart via `scripts/restart_engine.sh`** — it rotates
  the log to a timestamped archive before starting the new process.
  Using raw `nohup python3 main.py > engine.log` TRUNCATES the log and
  loses history. The script uses `>>` append after its initial rotation.
- ⚠️ **Prefer restart at a quiet moment** in the 5-min trading window
  (e.g. T-180 to T-240 seconds remaining). Never restart mid window-close
  (T<60) while orders are actively resolving.

### Workflow

**Step 1: Make changes on OpenClaw VPS (or locally)**
```bash
cd /root/.openclaw/workspace-novakash2/novakash-repo
# Make your changes
git add -p
git commit -m "feat: describe your change"
git push origin develop
```

**Step 2: SSH to Montreal via EC2 Instance Connect**

EC2 Instance Connect doesn't require a static PEM on your local machine —
you generate a temp SSH key, push it to the instance via AWS, then have
a 60-second window to connect. See the "SSH access" section below for
the canonical command. You need either the `ubuntu` user (for
`sudo` permission fixes and to run the restart script) or the
`novakash` user (for git-pull-only flows without a restart).

**Step 3: Pull latest develop and restart the engine**

The canonical restart procedure is `scripts/restart_engine.sh`. It
handles ownership fixes, log rotation, safe kill, and start verification
in one call. Do NOT restart via raw `nohup` commands — they truncate
`/home/novakash/engine.log` and lose history.

```bash
# Connect as ubuntu (has passwordless sudo)
ssh -i /tmp/ec2_temp_key ubuntu@15.223.247.178

# On the box: pull latest develop via the novakash user
sudo -u novakash bash -c 'cd /home/novakash/novakash && git pull origin develop'

# Then restart via the canonical script:
cd /home/novakash/novakash
./scripts/restart_engine.sh
```

What `scripts/restart_engine.sh` does, in order:
1. `sudo chown -R novakash:novakash /home/novakash/novakash/` — fixes
   any root-owned files left behind by a crash (see "Common permission
   issue" below).
2. Copies `/home/novakash/engine.log` → `/home/novakash/engine-YYYYMMDD-HHMMSS.log`
   and truncates the original. History is preserved as a timestamped
   archive, and the current log stays at a known path for the engine
   to append to.
3. Prunes old `engine-*.log` archives beyond `KEEP_N` (default 20) so
   disk doesn't bloat forever.
4. `sudo pkill -9 -f 'python3 main.py'` + 4 second verify. Exits non-zero
   if anything is still running after SIGKILL.
5. Starts the engine with `nohup python3 main.py >> /home/novakash/engine.log 2>&1 & disown`
   (append, not truncate).
6. Verifies exactly 1 python3 main.py process is running. Exits 1 if not.

Flags:
- `--keep-running` rotates the log without touching the process. Useful
  for "snapshot the current log for incident analysis" without a restart.
- `KEEP_N=N ./scripts/restart_engine.sh` overrides the archive count
  (e.g. `KEEP_N=50` to keep 50 old logs instead of 20).

**Check it's running:**
```bash
# On Montreal — verify process + tail live log
pgrep -af 'python3 main.py'
tail -f /home/novakash/engine.log

# Historical archives — most recent first
ls -lht /home/novakash/engine-*.log | head

# Or via DB heartbeat (from anywhere)
PGPASSWORD=<DB_PASSWORD> psql -h hopper.proxy.rlwy.net -p 35772 -U postgres -d railway \
  -c "SELECT engine_status, last_heartbeat FROM system_state WHERE id = 1;"
```

### Environment Variables on Montreal

The engine reads from `/home/novakash/novakash/engine/.env` (loaded by `python-dotenv` in `main.py`).

**IMPORTANT:** `engine/.env` is gitignored and only exists on Montreal. The committed file `engine/.env.local` is a REFERENCE COPY — it is never loaded by the engine. When adding new env vars:
1. Add to `engine/.env.local` (reference, committed to git)
2. SSH to Montreal and add to `engine/.env` (actual config, gitignored)
3. Restart the engine

**SSH access (EC2 Instance Connect — requires fresh temp key):**
```bash
# Generate temp key and push (60s window)
ssh-keygen -t ed25519 -f /tmp/ec2_temp_key -N "" -q
aws ec2-instance-connect send-ssh-public-key \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user ubuntu \
  --ssh-public-key file:///tmp/ec2_temp_key.pub \
  --availability-zone ca-central-1b \
  --region ca-central-1

# SSH as ubuntu (has sudo) — for permission fixes
ssh -o StrictHostKeyChecking=no -o IdentitiesOnly=yes \
  -i /tmp/ec2_temp_key ubuntu@15.223.247.178

# SSH as novakash (app user) — for git pull, env edits
aws ec2-instance-connect send-ssh-public-key \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user novakash \
  --ssh-public-key file:///tmp/ec2_temp_key.pub \
  --availability-zone ca-central-1b \
  --region ca-central-1
ssh -o StrictHostKeyChecking=no -o IdentitiesOnly=yes \
  -i /tmp/ec2_temp_key novakash@15.223.247.178
```

**Common permission issue:** Files in `engine/reconciliation/` may become root-owned if the engine crashes. `scripts/restart_engine.sh` fixes this automatically as step 1. If you only need the fix without a restart, do it manually:
```bash
# SSH as ubuntu
sudo chown -R novakash:novakash /home/novakash/novakash/
```

**Full deploy script (run via ubuntu SSH):**
```bash
# Pull latest develop as novakash (the app user that owns the repo)
sudo -u novakash bash -c 'cd /home/novakash/novakash && git pull origin develop'

# Restart via the canonical script — handles chown, log rotation,
# kill, start, and verification. NEVER replace this with a raw nohup
# command — the raw command uses `>` which truncates engine.log and
# loses all history from before the restart.
cd /home/novakash/novakash && ./scripts/restart_engine.sh
```

**Deprecated pattern — do NOT use:**
```bash
# ❌ The `>` redirect truncates /home/novakash/engine.log on each run,
#   destroying everything written since the last restart. This is how
#   we lost logs during the v10.1 → v10.2 transition before scripts/
#   restart_engine.sh was written.
nohup python3 main.py > /home/novakash/engine.log 2>&1 &
```

**v10.3 env vars (current production on Montreal's engine/.env):**
```env
# Core
V10_DUNE_ENABLED=true
V10_DUNE_MODEL=oak
V10_DUNE_MIN_P=0.65
V10_MIN_EVAL_OFFSET=180

# Regime thresholds (ELM v3 calibrated)
V10_TRANSITION_MIN_P=0.70
V10_CASCADE_MIN_P=0.72
V10_NORMAL_MIN_P=0.65
V10_LOW_VOL_MIN_P=0.65
V10_TRENDING_MIN_P=0.72
V10_CALM_MIN_P=0.72

# Offset + direction penalties
V10_OFFSET_PENALTY_MAX=0.06
V10_DOWN_PENALTY=0.03

# CoinGlass (taker gate disabled by default)
V10_CG_TAKER_GATE=false
V10_CG_TAKER_OPPOSING_PCT=55
V10_CG_SMART_OPPOSING_PCT=52
V10_CG_TAKER_OPPOSING_PENALTY=0.05
V10_CG_TAKER_ALIGNED_BONUS=0.02
V10_CG_MAX_AGE_MS=120000
V10_CG_CONFIRM_BONUS=0.02
V10_CG_CONFIRM_MIN=2
V10_MAX_SPREAD_PCT=8

# Dynamic cap + sizing
V10_DUNE_CAP_MARGIN=0.05
V10_DUNE_CAP_FLOOR=0.35
V10_DUNE_CAP_CEILING=0.68
V10_KELLY_ENABLED=false
BET_FRACTION=0.075
ABSOLUTE_MAX_BET=10.0
FIVE_MIN_EVAL_INTERVAL=2
```

**Key files on Montreal:**
| File | Purpose |
|------|---------|
| `/home/novakash/novakash/engine/.env` | **ACTUAL config** (gitignored, only on Montreal) |
| `/home/novakash/engine.log` | Current engine log |
| `/home/novakash/engine-v10.1-pre-v10.2.log` | v10.1 log backup (3.9MB) |

**Instance details:**
- Instance ID: `i-0785ed930423ae9fd`
- Region: `ca-central-1`, AZ: `ca-central-1b`
- IP: `15.223.247.178`
- Key pair name: `novakash-montreal` (PEM not available locally — use EC2 Instance Connect)

---

## Railway Deployment

Railway auto-deploys when changes are pushed to the `main` branch.

### Branches

| Branch | Deploys To |
|--------|-----------|
| `main` | Railway production (hub, frontend, data-collector) |
| `develop` | Development (no auto-deploy) |
| `staging` | Staging environment (if configured) |

### Workflow

```bash
# Deploy to Railway (from OpenClaw VPS or local)
git checkout main
git merge develop
git push origin main
# Railway will auto-detect and deploy within ~2 minutes
```

### Monitoring Railway Deploys

```bash
# Railway CLI
railway logs --service hub
railway logs --service frontend
railway logs --service data-collector
```

Or check the Railway dashboard.

### Railway Service Configuration

Each service has its own `railway.toml` or uses defaults:
- **hub**: `hub/Dockerfile` → starts FastAPI on `$PORT`
- **frontend**: `frontend/` → builds React/Vite, serves static files
- **data-collector**: `data-collector/Dockerfile` → runs `collector.py`

Railway injects `DATABASE_URL` and other environment variables automatically.

---

## TimesFM Service Deployment

The TimesFM service runs as a Docker container on the AWS EC2 t3.xlarge instance.

### Connect
```bash
ssh ubuntu@16.52.148.255
```

### Check Status
```bash
docker ps
docker logs -f <container_id>
```

### Restart (if crashed)
```bash
docker restart <container_id>
# or rebuild and restart
cd /home/ubuntu/timesfm-service
docker build -t timesfm .
docker run -d -p 8080:8080 --name timesfm timesfm
```

### Verify Health
```bash
curl http://16.52.148.255:8080/health
curl http://16.52.148.255:8080/forecast | python3 -m json.tool
```

---

## Frontend (AWS — Montreal)

The frontend is a static Vite/React build served by nginx on a t3.small EC2 instance in Montreal (`99.79.41.246`). Nginx proxies `/api/`, `/auth/`, `/ws/` to the Railway hub.

### Connect
```bash
ssh -i ~/.ssh/novakash-local-rsa.pem ubuntu@99.79.41.246
```

### Redeploy (from local machine)
```bash
# 1. Build
cd frontend && npm run build

# 2. Package and upload
tar czf /tmp/frontend-dist.tar.gz -C dist .
scp -i ~/.ssh/novakash-local-rsa.pem /tmp/frontend-dist.tar.gz ubuntu@99.79.41.246:/tmp/

# 3. Deploy on instance
ssh -i ~/.ssh/novakash-local-rsa.pem ubuntu@99.79.41.246 \
  "sudo rm -rf /var/www/frontend/* && \
   sudo tar xzf /tmp/frontend-dist.tar.gz -C /var/www/frontend && \
   sudo chown -R www-data:www-data /var/www/frontend && \
   sudo systemctl restart nginx"
```

### Verify
```bash
curl -sI http://99.79.41.246            # 200 OK, index.html
curl -s http://99.79.41.246/api/health  # Proxied to Railway hub
```

### Nginx Config
Location: `/etc/nginx/conf.d/frontend.conf`

To edit: `ssh ... then sudo nano /etc/nginx/conf.d/frontend.conf`
After edits: `sudo nginx -t && sudo systemctl restart nginx`

---

## Database Migrations

Migrations are in `hub/db/migrations/`. To run a migration:

```bash
# Connect to Railway PostgreSQL
PGPASSWORD=<DB_PASSWORD> psql -h hopper.proxy.rlwy.net -p 35772 -U postgres -d railway

# Apply a migration file
\i /path/to/migration.sql
```

For Alembic migrations:
```bash
cd hub
alembic upgrade head
```

The engine also auto-creates tables on startup if they don't exist.

---

## Quick Reference

| Task | Command |
|------|---------|
| Push engine changes | `git push origin develop` then SSH pull on Montreal |
| Deploy to Railway | `git push origin main` |
| Pull on Montreal | `sudo -u novakash bash -c 'cd /home/novakash/novakash && git pull origin develop'` |
| Restart engine (canonical) | `./scripts/restart_engine.sh` (rotates log, restarts, verifies) |
| Snapshot log without restart | `./scripts/restart_engine.sh --keep-running` |
| Check engine heartbeat | Query `system_state` table |
| View engine logs | `tail -f /home/novakash/engine.log` on Montreal |
| List archived logs | `ls -lht /home/novakash/engine-*.log` |
| Check TimesFM | `curl http://16.52.148.255:8080/forecast` |
| View Railway logs | `railway logs --service <name>` |

---

## Post-Deploy Checklist

After deploying engine changes:

- [ ] Engine process started successfully (no crash on import)
- [ ] Binance WS connected (check `system_state.binance_connected`)
- [ ] CoinGlass connected (`system_state.coinglass_connected`)
- [ ] TimesFM reachable (check engine logs for "TimesFM forecast")
- [ ] First window snapshot written to `window_snapshots`
- [ ] Telegram notification received for first evaluated window
- [ ] No error logs in first 5 minutes
