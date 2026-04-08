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

### Workflow

**Step 1: Make changes on OpenClaw VPS (or locally)**
```bash
cd /root/.openclaw/workspace-novakash2/novakash-repo
# Make your changes
git add -p
git commit -m "feat: describe your change"
git push origin develop
```

**Step 2: SSH into Montreal and pull**
```bash
ssh novakash@15.223.247.178
cd /home/novakash/novakash
git pull origin develop
```

**Step 3: Restart the engine**
```bash
# Find and kill the current engine process
pkill -f "python main.py"
# or
ps aux | grep python
kill <PID>

# Start engine (in a screen/tmux session so it persists)
screen -S engine
cd /home/novakash/novakash
./start_engine.sh
# Detach: Ctrl+A, D
```

**Or use tmux:**
```bash
tmux new-session -s engine -d
tmux send-keys -t engine "cd /home/novakash/novakash && ./start_engine.sh" Enter
```

**Check it's running:**
```bash
# From Montreal
screen -r engine        # Attach to screen session
tail -f /home/novakash/novakash/engine.log

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

```bash
ssh novakash@15.223.247.178
nano /home/novakash/novakash/engine/.env
# Edit values
# Then restart engine
```

**v10.1 env vars (must be in Montreal's engine/.env):**
```env
V10_DUNE_ENABLED=true
V10_DUNE_MODEL=oak
V10_DUNE_MIN_P=0.75
V10_MIN_EVAL_OFFSET=180
V10_TRANSITION_MIN_P=9.99
V10_CASCADE_MIN_P=0.80
V10_NORMAL_MIN_P=0.78
V10_LOW_VOL_MIN_P=0.78
V10_TRENDING_MIN_P=0.80
V10_CALM_MIN_P=0.80
V10_OFFSET_PENALTY_MAX=0.10
V10_DUNE_CAP_CEILING=0.70
V10_DUNE_CAP_FLOOR=0.35
V10_DUNE_CAP_MARGIN=0.05
FIVE_MIN_EVAL_INTERVAL=2
```

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
| Restart engine | `pkill -f "python main.py"` then `./start_engine.sh` |
| Check engine heartbeat | Query `system_state` table |
| View engine logs | `screen -r engine` on Montreal |
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
