# Railway Deployment Config

**Project:** Novakash  
**Environment:** develop (auto-deploys on push to `develop` branch)  
**Project ID:** a163a5f6-16f1-4784-9695-8f9582fea6d2

---

## Services

| Service | URL | Role |
|---------|-----|------|
| Engine | engine-develop.up.railway.app | Python trading engine |
| Hub | hub-develop-0433.up.railway.app | FastAPI backend |
| Frontend | frontend-develop-2bdf.up.railway.app | React dashboard |
| PostgreSQL | postgres.railway.internal:5432 | Database |

---

## Environment Variables (2026-04-03)

### Trading Config
| Variable | Value | Notes |
|----------|-------|-------|
| PAPER_MODE | true | Paper trading (no real orders) |
| LIVE_TRADING_ENABLED | false | Disabled (Billy's request) |
| STARTING_BANKROLL | 160 | Starting bankroll USD |
| PAPER_BANKROLL | 160 | Paper mode bankroll |
| BET_FRACTION | 0.10 | 10% max stake per trade |
| MAX_POSITION_USD | 120 | Max single position |
| MAX_OPEN_EXPOSURE_PCT | 0.45 | 45% max open exposure |
| DAILY_LOSS_LIMIT_PCT | 0.30 | 30% daily loss halt |
| CONSECUTIVE_LOSS_COOLDOWN | 10 | Cooldown after losses |
| COOLDOWN_SECONDS | 300 | 5-min cooldown |

### Strategy Config
| Variable | Value | Notes |
|----------|-------|-------|
| FIVE_MIN_ENABLED | true | 5-min BTC strategy |
| FIVE_MIN_ASSETS | BTC | Assets to trade |
| FIVE_MIN_MODE | safe | Trading mode |
| FIVE_MIN_ENTRY_OFFSET | 60 | T-60s entry |
| FIVE_MIN_MIN_DELTA_PCT | 0.05 | Min delta threshold |
| FIFTEEN_MIN_ENABLED | true | 15-min strategy |
| FIFTEEN_MIN_ASSETS | BTC | Assets |
| VPIN_CASCADE_THRESHOLD | 0.50 | VPIN cascade trigger |
| VPIN_INFORMED_THRESHOLD | 0.40 | VPIN informed flow |
| ARB_MIN_SPREAD | 0.001 | Min arb spread |

### Polymarket Credentials
| Variable | Notes |
|----------|-------|
| POLY_API_KEY | CLOB API key |
| POLY_API_SECRET | CLOB API secret |
| POLY_API_PASSPHRASE | CLOB passphrase |
| POLY_PRIVATE_KEY | Wallet private key |
| POLY_FUNDER_ADDRESS | 0x330ec13...710b6b |
| POLY_SIGNATURE_TYPE | 1 |
| POLYGON_RPC_URL | https://polygon-bor-rpc.publicnode.com |

### External APIs
| Variable | Notes |
|----------|-------|
| BINANCE_API_KEY | Binance data feed |
| COINGLASS_API_KEY | OI/liquidation data |
| GMAIL_ADDRESS | bbrichards123@gmail.com |
| GMAIL_APP_PASSWORD | For 2FA code retrieval |
| TELEGRAM_BOT_TOKEN | Alert bot token |
| TELEGRAM_CHAT_ID | Billy's chat |

### Runtime
| Variable | Value |
|----------|-------|
| SKIP_DB_CONFIG_SYNC | false |
| PLAYWRIGHT_ENABLED | true |
| RAILWAY_ENVIRONMENT | develop |

---

## Deployment

```bash
# Push to deploy (auto-triggers Railway build)
git push origin develop

# Manual restart
cd /root/.openclaw/workspace-novakash/novakash
railway restart --yes

# Upload and deploy
railway up -d -m "description"

# Check logs
railway logs --tail 50

# Set variable
railway variable set KEY=VALUE
```
