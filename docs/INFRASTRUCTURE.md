# Infrastructure

## Servers

### Montreal Engine Server

| Property | Value |
|----------|-------|
| **IP** | `15.223.247.178` |
| **Provider** | AWS EC2, ca-central-1 |
| **OS** | Ubuntu (latest LTS) |
| **User** | `novakash` |
| **Engine path** | `/home/novakash/novakash/engine` |
| **Repo path** | `/home/novakash/novakash/` |
| **Start script** | `/home/novakash/novakash/start_engine.sh` |

**SSH access:**
```bash
ssh novakash@15.223.247.178
# Uses SSH key (add your public key to ~/.ssh/authorized_keys on the server)
```

**Engine process:**
```bash
# Check if engine is running
ps aux | grep python

# Start engine (as novakash user)
cd /home/novakash/novakash
./start_engine.sh

# Or directly
cd engine && python main.py
```

> ⚠️ **CRITICAL: All Polymarket API calls must originate from this server (15.223.247.178).** Polymarket geo-blocks many regions including where OpenClaw VPS and Railway run. Never execute Polymarket CLOB orders from anywhere else.

---

### TimesFM Forecast Service

| Property | Value |
|----------|-------|
| **IP** | `16.52.148.255` |
| **Port** | `8080` |
| **Provider** | AWS EC2, ca-central-1 |
| **Instance type** | t3.xlarge |
| **Runtime** | Docker container |
| **Endpoint** | `http://16.52.148.255:8080` |

**SSH access:**
```bash
ssh ubuntu@16.52.148.255
# Uses ec2 key pair
```

**Docker management:**
```bash
# Check status
docker ps

# View logs
docker logs -f <container_name>

# Restart
docker restart <container_name>
```

**Health check:**
```bash
curl http://16.52.148.255:8080/health
curl http://16.52.148.255:8080/forecast
```

---

### Frontend (AWS — Montreal)

| Property | Value |
|----------|-------|
| **Instance ID** | `i-0fe72a610900b5cca` |
| **IP** | `99.79.41.246` |
| **Region** | ca-central-1b (Montreal) |
| **Instance type** | t3.small, Ubuntu 24.04 |
| **Key pair** | `novakash-local-rsa` (`~/.ssh/novakash-local-rsa.pem`) |
| **Security group** | `sg-05606e7fee858ca86` (SSH/HTTP/HTTPS open) |
| **Web root** | `/var/www/frontend` |
| **Nginx config** | `/etc/nginx/conf.d/frontend.conf` |

**Why Montreal:** Polymarket geo-blocks EU and UK. Canada (Montreal) is confirmed unblocked. See `docs/polymarket_ban_avoidance.md` Rule 2.

**Proxy routing:**
- `/api/*` → `hub-develop-0433.up.railway.app` (Railway hub)
- `/auth/*` → same
- `/ws/*` → same (WebSocket upgrade supported)

**SSH access:**
```bash
ssh -i ~/.ssh/novakash-local-rsa.pem ubuntu@99.79.41.246
```

**Health check:**
```bash
curl -sI http://99.79.41.246            # Should return index.html
curl -s http://99.79.41.246/api/health  # Should proxy to Railway hub
```

---

### Railway Services

All Railway services share the same project and PostgreSQL instance.

| Service | URL | Notes |
|---------|-----|-------|
| **Hub (API)** | `https://<hub>.railway.app` | FastAPI, auto-deploys from `main` branch |
| **Frontend** | `https://<frontend>.railway.app` | React/Vite, auto-deploys from `main` branch |
| **Data Collector** | (background, no public URL) | Polls Gamma API continuously |
| **PostgreSQL** | `hopper.proxy.rlwy.net:35772` | Shared by all services |

**Database connection:**
```bash
# Direct psql access (read-only for debugging)
PGPASSWORD=<DB_PASSWORD> psql -h hopper.proxy.rlwy.net -p 35772 -U postgres -d railway

# Connection string format (for applications)
postgresql+asyncpg://postgres:<PASSWORD>@hopper.proxy.rlwy.net:35772/railway
```

**Deploy via Railway CLI:**
```bash
railway up        # Deploy current directory
railway logs      # Stream logs
railway run ...   # Run command in Railway environment
```

---

### OpenClaw VPS

The VPS where AI agents (Claude, Qwen) run. Used for:
- Running subagents for analysis, code review, documentation
- Git operations (push commits to trigger Railway deploys)
- Monitoring and alerting via Telegram

**Important:** Do NOT run Polymarket trading code or make CLOB API calls from this server.

---

## Ports Summary

| Service | Host | Port | Protocol |
|---------|------|------|----------|
| Engine (no public port) | 15.223.247.178 | — | Internal only |
| TimesFM API | 16.52.148.255 | 8080 | HTTP |
| Railway PostgreSQL | hopper.proxy.rlwy.net | 35772 | TCP/PostgreSQL |
| Hub API | Railway | 443 (HTTPS) | REST + WebSocket |
| Frontend | Railway | 443 (HTTPS) | HTTPS |
| Binance WS | External | 443 | WebSocket |
| Polymarket CLOB | External | 443 | HTTPS + WebSocket |
| CoinGlass API | External | 443 | HTTPS |

---

## Environment Variables

All secrets are stored in environment variables. Never commit secrets to the repo.

**Engine `.env` variables:**

```env
# Database
DATABASE_URL=postgresql+asyncpg://postgres:<PASSWORD>@hopper.proxy.rlwy.net:35772/railway

# Polymarket
POLY_PRIVATE_KEY=<ethereum_private_key>
POLY_API_KEY=<polymarket_clob_api_key>
POLY_API_SECRET=<polymarket_clob_secret>
POLY_API_PASSPHRASE=<polymarket_clob_passphrase>
POLY_FUNDER_ADDRESS=<wallet_address>

# Binance (data only, no trading)
BINANCE_API_KEY=<key>
BINANCE_API_SECRET=<secret>

# CoinGlass
COINGLASS_API_KEY=<key>

# Anthropic (Claude evaluator)
ANTHROPIC_API_KEY=<key>

# Telegram
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_CHAT_ID=<chat_id>

# TimesFM
TIMESFM_URL=http://16.52.148.255:8080

# Risk / Mode
PAPER_MODE=false
STARTING_BANKROLL=500.0
```

See `.env.example` in the repo root for the full list with descriptions.

---

## Deploy Procedures

See [`DEPLOYMENT.md`](DEPLOYMENT.md) for step-by-step deployment instructions.
