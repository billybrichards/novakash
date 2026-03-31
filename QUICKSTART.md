# Quick Start Guide — btc-trader

## Prerequisites

- Docker + Docker Compose
- Node.js 20+ (for local frontend dev)
- Python 3.12 (for local engine/hub dev)

## 1. Setup Environment

```bash
cd /root/.openclaw/workspace-novakash/novakash
cp .env.example .env
# Edit .env with your API keys (see .env.example for all variables)
```

## 2. Start with Docker Compose

```bash
docker-compose up -d
```

This starts 5 services:
- **db** (PostgreSQL 16) on localhost:5432
- **engine** (Python trading engine)
- **hub** (FastAPI backend) on localhost:8000
- **frontend** (React + Vite) on localhost:3000
- **caddy** (Reverse proxy) on localhost:80, 443

## 3. Verify Services

```bash
# Check all containers running
docker-compose ps

# Check Hub API is alive
curl http://localhost:8000/health

# View logs
docker-compose logs -f engine
docker-compose logs -f hub
docker-compose logs -f frontend
```

## 4. Access the Dashboard

1. Open http://localhost:3000
2. Login: `billy` / (password from .env `ADMIN_PASSWORD`)
3. Navigate to Dashboard
4. Verify system status shows connections

## 5. Local Development (Optional)

### Engine (Python)
```bash
cd engine
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
python main.py
```

### Hub (FastAPI)
```bash
cd hub
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### Frontend (React)
```bash
cd frontend
npm install
npm run dev  # Starts on localhost:3000 with HMR
```

## 6. Database Access

```bash
# Connect to PostgreSQL
docker exec -it btc-trader-db-1 psql -U btctrader -d btc_trader

# List tables
\dt

# View trades
SELECT id, strategy, direction, stake_usd, pnl_usd, created_at FROM trades LIMIT 5;

# Exit
\q
```

## 7. API Testing

### Get Dashboard
```bash
curl -H "Authorization: Bearer <token>" http://localhost:8000/api/dashboard
```

### Get Trades
```bash
curl -H "Authorization: Bearer <token>" http://localhost:8000/api/trades?limit=10
```

### Get System Status
```bash
curl -H "Authorization: Bearer <token>" http://localhost:8000/api/system/status
```

### Update Config
```bash
curl -X PUT -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"paper_mode": true, "bet_fraction": 0.025}' \
  http://localhost:8000/api/config
```

## 8. Run Tests

```bash
cd engine
pytest tests/

# Test specific module
pytest tests/test_vpin.py -v
pytest tests/test_cascade.py -v
pytest tests/test_arb_scanner.py -v
pytest tests/test_risk_manager.py -v
```

## 9. Troubleshooting

### DB Connection Failed
```bash
# Check if Postgres is running
docker-compose ps db

# Check logs
docker-compose logs db

# Rebuild and restart
docker-compose down
docker-compose up -d
docker-compose logs -f db
```

### Frontend Can't Reach API
- Verify Hub container is running: `docker-compose ps hub`
- Check /api proxy in `frontend/vite.config.js`
- Check CORS in `hub/main.py`

### Auth Failed
- Verify `ADMIN_USERNAME` and `ADMIN_PASSWORD` in .env
- Clear browser localStorage: F12 → Application → LocalStorage → clear
- Check `JWT_SECRET` is set in .env

### Engine Won't Start
```bash
# Check logs
docker-compose logs engine

# Verify .env has all required keys:
# POLY_PRIVATE_KEY, BINANCE_API_KEY, etc.
```

## 10. Next Steps

### Phase 2: Data Layer
- [ ] Implement Binance WebSocket client
- [ ] Add CoinGlass API polling
- [ ] Hook up Chainlink RPC
- [ ] Connect Polymarket CLOB
- [ ] Test aggregator integration
- [ ] Verify paper mode works

### Check Status
```bash
# Engine logs should show feeds connecting
docker-compose logs -f engine | grep -E "connecting|connected"

# Hub should persist data
docker exec btc-trader-db-1 psql -U btctrader -d btc_trader -c \
  "SELECT COUNT(*) FROM trades;"
```

## Key Files to Edit

| Phase | File | Purpose |
|-------|------|---------|
| 2 (Feeds) | `engine/data/feeds/*.py` | Implement WebSocket/API clients |
| 3 (Signals) | `engine/signals/*.py` | Implement VPIN, cascade, etc. |
| 4 (Execution) | `engine/execution/*.py` | Order placement, fills |
| 5 (Strategies) | `engine/strategies/*.py` | Strategy logic |
| 6 (Polish) | `frontend/src/**` | UI refinements |

## Useful Commands

```bash
# Stop all services
docker-compose down

# Remove all containers + volumes (fresh start)
docker-compose down -v

# View Docker logs in real-time
docker-compose logs -f

# Rebuild a specific service
docker-compose build engine
docker-compose up -d engine

# Access Hub Python shell (for debugging)
docker-compose exec hub python

# Run migrations (if needed)
docker-compose exec hub alembic upgrade head
```

## Documentation

- **Full guide:** See `README.md`
- **Project context:** See `CLAUDE.md`
- **API reference:** See `README.md` (endpoints section)
- **Build phases:** See `README.md` (6 phases outlined)
- **Completion status:** See `/root/.openclaw/workspace-novakash/COMPLETION_CHECKLIST.md`

## Support

For detailed information on:
- Architecture: See `README.md` (Architecture section)
- Constants: See `engine/config/constants.py`
- Database schema: See `hub/db/schema.sql`
- API endpoints: See `README.md` (API Endpoints section)
- Frontend components: See `CLAUDE.md` (Design Standards)

---

**You're ready to start Phase 2!** 🚀

See `README.md` for full documentation and next steps.
