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

The engine reads from `/home/novakash/novakash/engine/.env` (or shell environment). To update secrets:
```bash
ssh novakash@15.223.247.178
nano /home/novakash/novakash/engine/.env
# Edit values
# Then restart engine
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
