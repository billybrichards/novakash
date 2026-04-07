# v8.x Pricing & Execution Audit

**Date:** April 7, 2026, 09:25 UTC
**Auditor:** Novakash2
**Status:** 🔴 CRITICAL — Two systemic issues found

---

## Executive Summary

**We are filling every single trade at $0.73 (the cap price), regardless of market conditions.** This makes the system structurally unprofitable at any WR below ~78%. Our current WR (71%) guarantees losses.

Two root causes:
1. **FOK ladder is broken** — every FOK order fails with decimal precision errors
2. **GTC fallback always submits at cap** — `ORDER_PRICING_MODE` defaults to "cap"

---

## Finding 1: FOK Ladder Never Fills (100% failure rate)

### Evidence

Every FOK order since deployment fails with:
```
PolyApiException[status_code=400, error_message={'error': 'invalid amounts, 
the market buy orders maker amount supports a max accuracy of 2 decimals, 
taker amount a max of 4 decimals'}]
```

### Root Cause

Polymarket CLOB requires for BUY orders:
- **maker_amount** (USDC cost = price × size) → **max 2 decimal places**
- **taker_amount** (token size) → **max 4 decimal places**

Our size calculation:
```python
size = math.floor(stake_usd / price * 100) / 100  # ✅ Size has 2 decimals
# BUT: price * size produces 4+ decimal places
```

Example from the 09:12 UTC trade:
| Price | Size | maker_amount (price × size) | Decimals | Valid? |
|-------|------|---------------------------|----------|--------|
| $0.59 | 15.81 | $9.3279 | 4 | ❌ |
| $0.47 | 19.85 | $9.3295 | 4 | ❌ |
| $0.33 | 28.27 | $9.3291 | 4 | ❌ |
| $0.32 | 29.15 | $9.3280 | 3 | ❌ |
| $0.34 | 27.44 | $9.3296 | 4 | ❌ |

All 5 FOK attempts fail → ladder exhausted → falls back to GTC.

### Fix Required

Size must be calculated so `price * size` rounds to exactly 2 decimal places:
```python
# Option A: Round maker_amount to 2 decimals, derive size from that
maker_amount = round(stake_usd, 2)  # e.g. $9.33
size = math.floor(maker_amount / price * 100) / 100
# Verify: round(price * size, 2) == maker_amount

# Option B: Iterate size downward until price * size has ≤ 2 decimals
size = math.floor(stake_usd / price * 100) / 100
while round(price * size, 2) != round(price * size, 6):
    size -= 0.01
```

### Impact

**FOK has NEVER successfully filled a single order in v8.0/v8.1.** Every trade since deployment has been GTC fallback.

---

## Finding 2: GTC Fallback Always Submits at $0.73 Cap

### Evidence

From 25 resolved trades with fill data:
| Metric | Value |
|--------|-------|
| Average actual_fill_price | **$0.7299** |
| Min fill | $0.7297 |
| Max fill | $0.7302 |
| All fills within | ±$0.0003 of $0.73 |

### Root Cause

In `polymarket_client.py` `_live_place_order()`:

```python
PRICING_MODE = os.environ.get("ORDER_PRICING_MODE", "cap")

if PRICING_MODE == "bestask":
    limit_price = round(float(price) + BUMP, 4)  # price = from strategy
    limit_price = max(limit_price, PRICE_FLOOR)
    limit_price = min(limit_price, PRICE_CAP)
else:
    # "cap" mode — submit at cap (legacy behaviour)
    limit_price = PRICE_CAP  # ← ALWAYS $0.73
```

`ORDER_PRICING_MODE` is **not set in any .env file** on Montreal:
```bash
$ grep ORDER_PRICING_MODE /home/novakash/novakash/engine/.env
# (empty — not set)
```

Default is `"cap"` → every GTC order submitted at $0.73 → CLOB fills at $0.73.

### The Pricing Chain

```
Strategy decides: "entry at $0.48 based on CLOB best ask"
     ↓
entry_price field in DB: $0.48 (what we WANTED to pay)
     ↓
FOK ladder tries: $0.59, $0.47, $0.33, $0.32, $0.34 — ALL FAIL (decimal bug)
     ↓
GTC fallback: ORDER_PRICING_MODE="cap" → limit_price = $0.73
     ↓
CLOB receives: GTC BUY at $0.73
     ↓
Fills immediately at $0.73 (or $0.7299 with spread)
     ↓
actual_fill_price: $0.73 — we overpay on EVERY trade
```

### Impact

At $0.73 fill price:
- **Win pays:** $1.00 - $0.73 = $0.27 per share
- **Loss costs:** $0.73 per share  
- **Breakeven WR:** 73 / (73 + 27) = **73.0%**
- **Our WR:** 71% → **guaranteed to lose money**

R/R at different fill prices:
| Fill Price | Win/share | Loss/share | Breakeven WR | EV at 71% WR (per $9 trade) |
|------------|-----------|------------|--------------|------------------------------|
| $0.50 | $0.50 | $0.50 | 50.0% | **+$1.89** |
| $0.55 | $0.45 | $0.55 | 55.0% | **+$1.44** |
| $0.60 | $0.40 | $0.60 | 60.0% | **+$0.99** |
| $0.65 | $0.35 | $0.65 | 65.0% | **+$0.54** |
| $0.70 | $0.30 | $0.70 | 70.0% | **+$0.09** |
| **$0.73** | **$0.27** | **$0.73** | **73.0%** | **-$0.18** |
| $0.80 | $0.20 | $0.80 | 80.0% | **-$0.81** |

---

## Finding 3: `entry_price` in DB Is Misleading

The `entry_price` column in the `trades` table stores what the strategy WANTED to pay (CLOB best ask at eval time), NOT what we actually paid.

| Trade Time | entry_price (wanted) | actual_fill_price (paid) | Difference |
|-----------|---------------------|-------------------------|------------|
| 09:12 | $0.34 | $0.73 | +$0.39 overpay |
| 08:08 | $0.60 | $0.73 | +$0.13 overpay |
| 07:04 | $0.56 | $0.73 | +$0.17 overpay |
| 06:18 | $0.63 | $0.73 | +$0.10 overpay |

**Any analysis using `entry_price` for P&L calculations is WRONG.** Always use `metadata->>'actual_fill_price'`.

---

## Recommended Fixes (Priority Order)

### 1. 🔴 IMMEDIATE: Fix FOK decimal precision

The maker_amount (price × size) must have ≤ 2 decimal places for BUY orders.

```python
def _calculate_fok_size(price: float, stake_usd: float) -> float:
    """Calculate size so that price * size has ≤ 2 decimal places."""
    # Start with the naive size
    size = math.floor(stake_usd / price * 100) / 100
    # Reduce until maker_amount is clean
    for _ in range(100):  # safety limit
        maker = round(price * size, 6)
        if abs(maker - round(maker, 2)) < 1e-9:
            return size
        size -= 0.01
    return size
```

### 2. 🔴 IMMEDIATE: Set ORDER_PRICING_MODE=bestask

Add to Montreal .env:
```bash
ORDER_PRICING_MODE=bestask
```

This makes GTC fallback submit at `best_ask + $0.02` (capped at $0.73) instead of always $0.73.

### 3. 🟡 SHORT-TERM: Fix entry_price recording

Update the trade insertion to record actual fill price in `entry_price`, not the strategy's target price:
```python
# After fill confirmation:
entry_price = actual_fill_price  # not the CLOB best_ask from strategy
```

### 4. 🟡 SHORT-TERM: Add fill price to notifications

Current notifications show the strategy price, not the fill price. Billy sees "$0.48 entry" but we actually paid $0.73.

### 5. 🟢 VALIDATION: Monitor FOK fill rate after fix

After fixing the decimal bug, FOK may still not fill because CLOB books are thin at T-70. But at least FOK attempts won't be wasted, and if liquidity exists at $0.50, we'll actually fill there instead of always defaulting to $0.73.

---

## Appendix: Execution Path Diagram

```
Signal fires at T-{offset}
    ↓
┌─────────────────────────────┐
│ FOK Ladder (5 attempts)     │
│ Start: CLOB best_ask        │
│ Each attempt: try FOK BUY   │
│ Bump: +$0.01 per retry      │
│ Cap: $0.73                  │
│ Floor: $0.30                │
│                             │
│ ⚠️ BUG: All fail with       │
│ decimal precision error     │
└────────────┬────────────────┘
             │ exhausted
             ↓
┌─────────────────────────────┐
│ GTC Fallback                │
│                             │
│ ⚠️ BUG: ORDER_PRICING_MODE  │
│ not set → defaults to "cap" │
│ → limit_price = $0.73       │
│ → ALWAYS fills at $0.73     │
└────────────┬────────────────┘
             │ filled
             ↓
┌─────────────────────────────┐
│ DB records entry_price as   │
│ strategy target ($0.34-0.60)│
│ NOT actual fill ($0.73)     │
│ ⚠️ Misleading P&L data      │
└─────────────────────────────┘
```

---

## Appendix: Raw FOK Log (Latest Trade)

```
09:12:13 [warn] fok_ladder.order_error attempt=1 price=$0.5900 error="invalid amounts..."
09:12:16 [info] fok_ladder.attempt     attempt=2 price=$0.4700 size=19.85
09:12:16 [warn] fok_ladder.order_error attempt=2 price=$0.4700 error="invalid amounts..."
09:12:19 [info] fok_ladder.attempt     attempt=3 price=$0.3300 size=28.27
09:12:19 [warn] fok_ladder.order_error attempt=3 price=$0.3300 error="invalid amounts..."
09:12:22 [info] fok_ladder.attempt     attempt=4 price=$0.3200 size=29.15
09:12:22 [warn] fok_ladder.order_error attempt=4 price=$0.3200 error="invalid amounts..."
09:12:24 [info] fok_ladder.attempt     attempt=5 price=$0.3400 size=27.44
09:12:24 [warn] fok_ladder.order_error attempt=5 price=$0.3400 error="invalid amounts..."
09:12:24 [warn] fok_ladder.exhausted   attempts=5
→ GTC fallback at $0.73 → filled at $0.73 → LOSS
```

---

## Appendix: Actual Fill Distribution (25 resolved trades)

```
$0.7297  ██ (2)
$0.7298  ████ (4)
$0.7299  ██████████ (10)
$0.7300  ████████ (8)
$0.7301  █ (1)
$0.7302  █ (1)
```

**Every single trade fills within $0.0005 of $0.73.** There is zero price improvement.
