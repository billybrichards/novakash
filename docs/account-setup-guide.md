# BTC Trader — Account & API Key Setup Guide

Everything you need to get set up. Work through this over breakfast ☕

---

## 1. MetaMask Wallet (Foundation for Polymarket + Opinion)

You'll need a dedicated wallet. Don't use your main one.

1. Install MetaMask: https://metamask.io/download/
2. Create a new wallet (or import existing)
3. **Add Polygon network:**
   - Network Name: `Polygon Mainnet`
   - RPC URL: `https://polygon-rpc.com`
   - Chain ID: `137`
   - Symbol: `MATIC`
   - Explorer: `https://polygonscan.com`
4. **Add BNB Chain network:**
   - Network Name: `BNB Smart Chain`
   - RPC URL: `https://bsc-dataseed.binance.org`
   - Chain ID: `56`
   - Symbol: `BNB`
   - Explorer: `https://bscscan.com`
5. **Export your private key:** MetaMask → Account → ⋮ → Account Details → Export Private Key
   - This becomes `POLY_PRIVATE_KEY` and `OPINION_WALLET_KEY`
   - ⚠️ NEVER share this. Store it in .env only.

**Fund the wallet:**
- Bridge ~$200 USDC to Polygon (use https://app.uniswap.org or https://bridge.arbitrum.io)
- Bridge ~$100 USDT to BNB Chain
- Keep ~$5 MATIC on Polygon for gas
- Keep ~$2 BNB on BNB Chain for gas

---

## 2. Polymarket

**URL:** https://polymarket.com

### Steps:
1. Go to https://polymarket.com
2. Click "Connect Wallet" → select MetaMask
3. Sign the login message
4. **⚠️ IMPORTANT: Make one manual trade first!**
   - Find any BTC 5-min market
   - Buy $1 of YES or NO — doesn't matter
   - This initialises your proxy wallet (API won't work without it)
5. Go to https://polymarket.com/settings
   - Find your **Proxy Wallet Address** → this is `POLY_FUNDER_ADDRESS`

### Derive API Keys:
After your first trade, run this locally (or I'll run it for you later):
```
python scripts/setup_polymarket.py
```
This outputs:
- `POLY_API_KEY`
- `POLY_API_SECRET`
- `POLY_API_PASSPHRASE`

### What you'll have:
```
POLY_PRIVATE_KEY=0x...        (your MetaMask private key)
POLY_API_KEY=...              (from setup script)
POLY_API_SECRET=...           (from setup script)
POLY_API_PASSPHRASE=...       (from setup script)
POLY_FUNDER_ADDRESS=0x...     (from settings page)
```

**Docs:** https://docs.polymarket.com

---

## 3. Opinion Exchange

**URL:** https://opinion.trade

### Steps:
1. Go to https://opinion.trade
2. Connect your MetaMask wallet (same one, BNB Chain network)
3. Deposit USDT
4. Go to Account Settings → API section
5. Generate an API key

### What you'll have:
```
OPINION_API_KEY=...           (from account settings)
OPINION_WALLET_KEY=0x...      (same private key as Polymarket)
```

**Docs:** https://docs.opinion.trade

---

## 4. Binance (Data Feed Only)

**URL:** https://www.binance.com

No trading, just market data. Free.

### Steps:
1. Log in to https://www.binance.com (create account if needed)
2. Go to https://www.binance.com/en/my/settings/api-management
3. Click "Create API" → choose "System generated"
4. Name it something like `btc-trader-data`
5. **Permissions:** Enable "Read" only. Disable everything else.
6. **IP Whitelist:** Add your VPS IP (get it later, skip for now)
7. Copy the API Key and Secret

### What you'll have:
```
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
```

**Note:** The WebSocket data feed (`wss://fstream.binance.com/ws`) is free and doesn't even need auth for public streams, but having keys avoids rate limits.

---

## 5. CoinGlass

**URL:** https://www.coinglass.com

### Steps:
1. Go to https://www.coinglass.com
2. Sign up / log in
3. Go to https://www.coinglass.com/pricing
   - **Free tier** works for development (10 requests/min)
   - **Pro ($29.99/mo)** needed for live trading later
4. Go to Account → API → Generate API Key

### What you'll have:
```
COINGLASS_API_KEY=...
```

**API Docs:** https://docs.coinglass.com/reference/futures-open-interest-aggregated-ohlc-history

---

## 6. Telegram Bot (Alerts)

### Steps:
1. Open Telegram, search for **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g. "BTC Trader Alerts")
4. Choose a username (e.g. `btc_trader_alerts_bot`)
5. BotFather gives you a token → this is `TELEGRAM_BOT_TOKEN`
6. Now search for **@userinfobot** in Telegram
7. Send it any message
8. It replies with your user ID → this is `TELEGRAM_CHAT_ID`
9. **Start your new bot:** Search for your bot username, tap Start

### What you'll have:
```
TELEGRAM_BOT_TOKEN=1234567890:ABCdef...
TELEGRAM_CHAT_ID=1000045351
```

---

## 7. Alchemy (Polygon RPC)

**URL:** https://www.alchemy.com

### Steps:
1. Go to https://www.alchemy.com
2. Sign up (free)
3. Click "Create new app"
   - Name: `btc-trader`
   - Chain: **Polygon**
   - Network: **Polygon Mainnet**
4. Click the app → "API Key" tab
5. Copy the HTTPS endpoint

### What you'll have:
```
POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY_HERE
```

Free tier = 300M compute units/month. More than enough.

---

## 8. Hetzner VPS (Deploy Later)

**URL:** https://www.hetzner.com/cloud

Not needed yet — set this up when we're ready to deploy.

### Recommended:
- **Plan:** CX22 (2 vCPU, 4GB RAM, 40GB SSD) — €4.35/mo
- **Location:** Frankfurt (low latency to Binance EU + Polygon)
- **OS:** Ubuntu 24.04 LTS
- **DO NOT** use GPU instances. Network latency > compute.

---

## 9. Self-Generated Credentials

These you just make up yourself:

```
# Database (pick strong passwords)
DB_USER=btctrader
DB_PASSWORD=<generate: openssl rand -hex 24>

# Hub login
ADMIN_USERNAME=billy
ADMIN_PASSWORD=<your choice, strong>

# JWT signing key
JWT_SECRET=<generate: openssl rand -hex 32>

# Starting config
STARTING_BANKROLL=500
PAPER_MODE=true

# Domain (for when you deploy)
DOMAIN=trader.yourdomain.com
```

Quick generate commands (run in terminal):
```bash
echo "DB_PASSWORD=$(openssl rand -hex 24)"
echo "JWT_SECRET=$(openssl rand -hex 32)"
```

---

## Checklist

| # | Account | Keys Needed | Cost | Time |
|---|---------|------------|------|------|
| ✅ | MetaMask wallet | Private key | Free | 2 min |
| ☐ | Polymarket | 5 keys (after manual trade) | ~$200 USDC | 10 min |
| ☐ | Opinion Exchange | API key | ~$100 USDT | 5 min |
| ☐ | Binance | API key + secret | Free | 3 min |
| ☐ | CoinGlass | API key | Free (dev) | 2 min |
| ☐ | Telegram | Bot token + chat ID | Free | 3 min |
| ☐ | Alchemy | RPC URL | Free | 3 min |
| ☐ | Self-generated | DB + JWT + admin | Free | 1 min |

**Total time: ~30 minutes**
**Total cost: ~$340 in crypto deposits + ~€35/mo running**

---

## When You're Done

Send me all the keys (NOT in group chat!) and I'll wire up the `.env` file.

Or even better — create the `.env` file yourself:
```bash
cp .env.example .env
# Edit .env with your keys
nano .env
```

Then we're ready to build. 🚀
