# Polymarket Bot Ban Avoidance Guide
## Novakash Engine — Lessons Learned & Rules for Going Live Again
### Updated: April 4, 2026

---

## 1. What Caused the Ban

### Primary Trigger: FAK Order Burst (April 2)

The ban was NOT caused by the v5 deploy. It happened on **April 2** when the engine switched from GTC limit orders to **FAK (Fill And Kill) market orders**:

| Factor | Detail | Risk |
|---|---|---|
| **FAK order type** | Aggressive market-taking, not maker | 🔴 CRITICAL |
| **Order velocity** | 20+ orders in rapid succession, no throttle | 🔴 CRITICAL |
| **Machine-speed timing** | Exact intervals, zero jitter between orders | 🔴 CRITICAL |
| **Terrible pricing** | Buying at 88-98¢ tokens on thin books | 🟡 HIGH |
| **$258 loss cluster** | Rapid consecutive losses looks like manipulation | 🟡 HIGH |
| **Multi-asset sweep** | Simultaneous orders across BTC/ETH/SOL | 🟡 HIGH |

The morning session (GTC limits at Gamma price) was fine: +$93 profit, 89% WR. The ban came after the FAK switch.

### Geographic Factor

- **UK is on Polymarket's geoblock list** (confirmed from their docs)
- Railway CDN edge: VIE (Vienna/Europe)
- **Montreal (Canada) is confirmed unblocked** — use this for hosting

### CLOB Rate Limits (from Polymarket docs)
- Relayer: 25 req/min
- Data API: 150 req/10s
- Order placement: undocumented but likely flags >10 orders/min from same address

---

## 2. The 12 Rules for Bot Survival

### Rule 1: NEVER Use FAK/FOK Orders
- ✗ FAK (Fill And Kill) = aggressive market-taking, classic bot fingerprint
- ✗ FOK (Fill Or Kill) = same problem
- ✓ **GTC only** — passive maker orders add liquidity, Polymarket wants this
- ✓ **GTD with auto-expiry** at window close — cleaner than cancel-and-replace

### Rule 2: NEVER Trade from a Blocked Country
- ✗ Don't deploy to Railway Europe (routes through DE/NL/AT)
- ✗ Don't use a UK residential IP
- ✓ **Montreal, Canada confirmed unblocked**
- ✓ Check `GET https://polymarket.com/api/geoblock` from your server BEFORE going live
- ✓ Add a startup geoblock check — refuse to start in live mode if blocked

### Rule 3: ONE Asset, ONE Order at a Time
- ✗ Don't fire 5 orders across 3 assets in 2 seconds
- ✗ Don't trade multiple timeframes (5m + 15m) simultaneously
- ✓ Start with 1-2 assets only (not 4 × 2 timeframes)
- ✓ Pick the BEST signal per window, trade only that one
- ✓ Add 2-5 second random delay between any orders

### Rule 4: Humanise Your Order Pattern
- ✗ Don't stake $3.431912785 (7 decimal precision = bot)
- ✗ Don't always use the exact same entry timing (T-60.000s)
- ✓ Round stakes to $0.50 increments ($3.50, $4.00, $5.00)
- ✓ Add 2-5s random jitter to evaluation timing
- ✓ Vary order sizes ±15% randomly

### Rule 5: Rate Limit EVERYTHING
- ✗ Don't retry immediately on failure
- ✗ Don't place >5 orders per minute
- ✓ Maximum 1 order per 30 seconds
- ✓ Maximum 5 orders per hour initially
- ✓ Add exponential backoff on any 4xx error
- ✓ If order fails, wait 5+ minutes before retrying

### Rule 6: No Cancel-and-Replace Pattern
- ✗ Don't cancel and re-submit at +2¢ — classic bot signal
- ✗ Don't auto-bump prices in a tight loop
- ✓ Place one order at a fair price. If it doesn't fill, accept it.
- ✓ Use GTD auto-expiry instead of cancel-and-replace
- ✓ 30s poll + dynamic bump is OK but keep it gentle (v5.2 approach)

### Rule 7: Price Cap — Never Buy Expensive Tokens
- ✗ Don't buy tokens above 65¢ (terrible R/R on thin books)
- ✓ 65¢ price cap prevents the 88-98¢ disaster from April 2
- ✓ Already implemented in engine

### Rule 8: Warm Up New Wallets
- ✗ Don't go from zero activity → 50 automated trades
- ✓ Day 1-7: Manual trades via web UI (2-3/day)
- ✓ Day 8-14: Mix manual + API (5-10/day)
- ✓ Day 15+: Gradual automation increase
- ✓ Start with paper mode for 24h — no CLOB interaction at all

### Rule 9: Use Residential/Non-Datacenter IPs
- ✗ Don't trade from AWS/Railway/GCP datacenter IPs
- ✓ Route order placement through residential proxy (Bright Data, Oxylabs)
- ✓ OR deploy to a VPS in Montreal with a residential ISP
- ✓ Read operations (Gamma API, prices) can stay on datacenter — no ban risk

### Rule 10: Don't Trade Every Window
- ✗ Don't trade 288 times per day (every 5-min window)
- ✓ Maximum 20-30 trades per day
- ✓ Prefer HIGH confidence only (skip MODERATE initially)
- ✓ Add per-hour trade limit (max 4/hour)

### Rule 11: Multiple Wallets / Key Rotation
- ✓ Maintain 2-3 funded wallets
- ✓ Rotate between them
- ✓ Never blast orders from the same wallet on multiple markets
- ✓ Fund from different source addresses

### Rule 12: Monitor and Circuit-Break
- ✓ Any 4xx from Polymarket → halt trading 15 minutes
- ✓ 3 consecutive errors → halt 1 hour + Telegram alert
- ✓ Check geoblock status every 10 minutes
- ✓ Track wallet balance — unexpected drop = halt and investigate

---

## 3. Engine Changes Already Made (v5.2+)

| Change | Status |
|---|---|
| GTC only (never FAK/market orders) | ✅ Done |
| GTD with auto-expiry at window close | ✅ Done |
| 30s poll + dynamic bump retry (not rapid-fire) | ✅ Done |
| One order per 5-min window per asset | ✅ Done |
| 65¢ price cap | ✅ Done |
| Max 8 orders per 5min (4 assets × 2 timeframes) | ✅ Done |

## 4. Engine Changes Still Needed

| Change | Priority |
|---|---|
| Geoblock startup check | 🔴 P0 |
| Order rate limiter (token bucket) | 🔴 P0 |
| Stake humanisation (round to $0.50 + jitter) | 🔴 P0 |
| Entry timing jitter (±2-5s) | 🟡 P1 |
| Single-best-signal mode (pick 1 trade per window) | 🟡 P1 |
| Residential proxy for order placement | 🟡 P1 |
| Error circuit breaker | 🟡 P1 |
| Deploy to Montreal/Canada VPS | 🔴 P0 |
| Wallet warm-up protocol | 🟢 P2 |

---

## 5. Recovery Plan

### Step 1: New Wallet
Generate new private key → derive new address. Fund via a DIFFERENT source than the banned wallet. Do NOT send directly from 0x2e42.

### Step 2: New API Credentials
Create new CLOB API key + secret + passphrase. Old credentials associated with banned address.

### Step 3: Allowed-Region Deployment
Deploy to Montreal, Canada (confirmed unblocked). Either Railway region change or standalone VPS.

### Step 4: Implement P0 Changes
Geoblock check, rate limiter, stake humanisation BEFORE any live orders.

### Step 5: Wallet Warm-Up (7-14 days)
Manual trades from web UI for at least 7 days. Build normal account history.

### Step 6: Gradual Automation
Start with 1 asset (BTC only), HIGH confidence only, max 5 trades/day. Increase weekly.

**Timeline: 2-3 weeks minimum before safe to go live again.**

---

## 6. What We Do Right (Keep These)

| Feature | Why It's Good |
|---|---|
| GTD orders (60s expiry) | Short-lived, less suspicious than stale GTC |
| Maker orders (limit, not market) | Add liquidity — Polymarket wants this |
| Oracle-based resolution | Never manipulate outcomes |
| Single direction per window | No opposing orders (wash trading) |
| Paper mode as default | Safe testing before real capital |
| 65¢ price cap | Prevents terrible R/R on thin books |

---

*⚠️ DO NOT go live until ALL P0 items are implemented and wallet is warmed up.*
