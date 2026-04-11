# Order Execution Strategy — FOK, FAK, GTC

> **Reference:** [Polymarket CLOB Docs - Create Order](https://docs.polymarket.com/trading/orders/create)

## Order Type Definitions

| Type | Behavior | Use Case | Our Usage |
|------|----------|----------|-----------|
| **GTC** | Good-Til-Cancelled — rests on book until filled/cancelled | Default limit orders | ✅ GTC fallback when FOK exhausts |
| **GTD** | Good-Til-Date — auto-expire at timestamp | Auto-expire before known events | ✅ Window expiry (T-60) |
| **FOK** | Fill-Or-Kill — must fill 100% at ≤ limit price, or cancel | All-or-nothing market orders | ✅ FOK ladder (5 retries) |
| **FAK** | Fill-And-Kill — fills what exists, cancels remainder | Partial-fill market orders | ❌ Not used (see below) |

**Critical:** The `price` field on FOK/FAK market orders is a **worst-price limit** (slippage protection), not a target execution price.

> *Source: [Polymarket Docs - Market Orders](https://docs.polymarket.com/trading/orders/create#market-orders)*

---

## Our Implementation: FOK Ladder + GTC Fallback

### Why FOK Over FAK?

**FAK with proper caps is safe from disaster** (e.g., Apr 2 88-98¢ fills), but introduces complexity:

| FOK | FAK |
|-----|-----|
| Full fill OR nothing | Partial fill guaranteed |
| Predictable position size | Variable position size |
| Simpler risk management | Need to track partial fills |
| "All or nothing" clarity | Could fill 30%, wait 70% forever |

**Our choice:** FOK + GTC fallback provides the safety of FOK with the fill assurance of GTC.

### FOK Ladder Logic (v8.1.2)

```
T-240 to T-60: 5 retries at 2s intervals
  ↓
If CLOB ≤ cap: FOK at cap
  ↓
If CLOB within π% (3.14%) of cap: FOK at cap+π cents (0.0314)
  ↓
If FOK exhausts (5 failed attempts): GTC at cap+π cents
```

**Key behaviors:**
- FOK at $0.55 = "Fill $100 at ≤ $0.55, or kill"
- If CLOB has $0.53 asks → fills at $0.53
- If CLOB has $0.58 asks → killed (exceeds worst-price limit)
- Retry loop waits for CLOB to drop or hidden liquidity to appear

### π Bonus Feature (v8.1.3)

When CLOB is within π% (3.14%) of the cap, we allow FOK to attempt up to **cap+π cents** (0.0314):

```
Cap = $0.55 (T-240)
CLOB = $0.56 (1.8% above cap → within π%)
FOK attempts up to $0.58 (capped to 2dp)
GTC fallback at $0.58
```

**Environment variables:**
- `FOK_PI_BONUS_CENTS=0.0314` (π cents)
- `FOK_PI_PERCENT_THRESHOLD=3.14` (π%)
- `FOK_ATTEMPTS=5` (max retries)
- `FOK_INTERVAL_S=2.0` (seconds between retries)

---

## Dynamic Price Caps (v8.1)

Caps enforced per eval offset to ensure +EV entries:

| Offset | Time Window | Cap |
|--------|-------------|-----|
| T-240 to T-180 | First 60s | $0.55 |
| T-180 to T-120 | 60-120s | $0.60 |
| T-120 to T-60 | 120-180s | $0.65 |
| T-60 | Final 60s | $0.65 |

**Enforcement:**
- FOK ladder starts at `min(CLOB, cap)`
- FOK retries at `min(CLOB+bump, cap)`
- GTC fallback at `cap` (or `cap+π` if FOK exhausted)

---

## Montreal VPS Rules (CLOB Execution)

**Server:** `15.223.247.178` (Montreal, Canada)

**Critical:** ALL Polymarket CLOB API calls must originate from Montreal IP.

### Execution Flow

```bash
# Engine runs on Montreal
cd /home/novakash/novakash/engine

# Check engine status
tail -100 /home/novakash/engine.log | grep -E 'place_order|fok_ladder|regime_signal'

# Check current window
tail -50 /home/novakash/engine.log | grep 'five_min.window_signal'

# Restart engine (if needed)
pkill -9 python3
nohup python3 main.py > /home/novakash/engine.log 2>&1 &
```

### CLOB Feed Logging

Every 2s, the CLOB feed polls and logs:
- `up_ask`, `up_bid` (UP token)
- `dn_ask`, `dn_bid` (DOWN token)
- Timestamp in UTC

**Note:** CLOB audit tables (`clob_book_snapshots`, `fok_execution_attempts`) are **not yet migrated**. Run migration to enable:

```bash
cd /home/novakash/novakash/engine
psql $DATABASE_URL < ../../migrations/add_clob_execution_audit_tables.sql
```

---

## Safety Guards

### 1. Price Caps Prevent Disaster

**Apr 2 Lesson:** Market orders without caps bought at 88-98¢.

**Current caps:** $0.55-$0.65 → even FAK/FOK at worst-price limit cannot exceed cap.

### 2. v2.2 Gate Blocks Bad Trades

```
v2.2 HIGH confidence (p>0.65 or p<0.35)
  AND v8 direction agreement
  → Trade allowed
  → Else: SKIP
```

**Accuracy:** 75% block accuracy on resolved windows.

### 3. CLOB Floor Check

```
PRICE_FLOOR=0.30
If CLOB < floor → Skip trade (manipulation risk)
```

### 4. Circuit Breakers

- **Max bet:** $999 (ABSOLUTE_MAX_BET)
- **Kill switch:** 80% drawdown (kills at ~$20 remaining)
- **Daily loss limit:** $80 (60% of bankroll)

---

## Monitoring Commands

```bash
# Check FOK execution
tail -100 /home/novakash/engine.log | grep 'fok_ladder'

# Check GTC submissions
tail -100 /home/novakash/engine.log | grep 'gtc_submit'

# Check current CLOB prices
tail -50 /home/novakash/engine.log | grep 'clob_feed.prices'

# Check regime signals
tail -100 /home/novakash/engine.log | grep 'regime_signal'

# Check v2.2 gate
tail -100 /home/novakash/engine.log | grep 'v81.early_gate'

# Check fills
tail -100 /home/novakash/engine.log | grep 'trade.fill_check'
```

---

## Files Modified

| File | Purpose |
|------|---------|
| `engine/execution/fok_ladder.py` | FOK retry logic, π bonus |
| `engine/strategies/five_min_vpin.py` | GTC fallback with π bonus |
| `engine/execution/polymarket_client.py` | FOK/GTC execution |
| `engine/data/feeds/clob_feed.py` | CLOB polling every 2s |
| `migrations/add_clob_execution_audit_tables.sql` | Audit schema (pending migration) |

---

## References

- [Polymarket CLOB Docs - Create Order](https://docs.polymarket.com/trading/orders/create)
- [Polymarket CLOB Docs - Cancel Orders](https://docs.polymarket.com/trading/orders/cancel)
- [CHANGELOG-2026-04-07.md](./CHANGELOG-2026-04-07.md) — v8.1.2 FOK ladder fix
- [CLOB_AUDIT_LOGGING.md](./CLOB_AUDIT_LOGGING.md) — Audit table schema

---

**Last Updated:** 2026-04-07 21:36 UTC
**Version:** v8.1.3 (π bonus)
