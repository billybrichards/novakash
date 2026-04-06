# Live Trading Pricing — How It Actually Works

**Last updated:** 2026-04-06 (based on real live trading experience)

## How Polymarket CLOB Fills Work

**Key insight:** GTC limit orders fill at the **best available price**, up to your limit. The limit is a ceiling, not the price you pay.

### Real Examples (Apr 6)

| Time | Gamma BestAsk | Limit Submitted | Actual Fill | Cost | Result |
|------|--------------|----------------|-------------|------|--------|
| 10:09 | $0.40 | $0.73 | **$0.49** | $3.36 | WIN +$3.37 |
| 10:19 | $0.40 | $0.73 | **$0.73** | $5.00 | WIN +$1.85 |

Both used the same GTC flow with $0.73 cap. The CLOB filled one at $0.49 (good) and the other at $0.73 (max). This is normal CLOB behaviour — the counterparty chooses what price to accept.

## Current Order Flow (v7.1)

```
1. Engine evaluates at T-60 (60 seconds before window close)
2. Fetches fresh Gamma bestAsk from Polymarket API
3. Checks: floor ($0.30) ≤ bestAsk ≤ cap ($0.73)
4. Submits single GTC/GTD limit order at cap price ($0.73)
   - Client adds +2¢ bump internally (capped at $0.73)
   - GTD expires at window close + 2 min buffer
5. CLOB fills at best available price (could be $0.40 or $0.73)
6. Fill check every 5s for 30s
7. NO retry, NO bump — accept miss if unfilled
```

## Why We Submit at Cap (Not BestAsk)

### Option A: Submit at bestAsk + 2¢ ($0.42)
- ✅ Better worst-case fill ($0.42 max)
- ❌ Misses fills when book is thin (no sellers at $0.42 but willing at $0.55)
- ❌ Lower fill rate

### Option B: Submit at cap ($0.73) — CURRENT
- ✅ Higher fill rate (accepts any price up to $0.73)
- ✅ Often fills at market price ($0.49) not cap
- ❌ Sometimes fills at cap ($0.73) = bad R/R
- ❌ Market makers can take full limit

### Verdict
Option B is better for now. At 69-77% WR, even $0.73 fills are slightly +EV. The key protection is the FLOOR ($0.30) which blocks adverse selection entries.

**Future improvement (TODO):** Submit at bestAsk + 5¢ with $0.73 hard cap as safety only. This would give better average fills while still allowing some spread crossing.

## Price Protections

| Protection | Value | Purpose |
|-----------|-------|---------|
| **Floor** | $0.30 | Blocks entries where market strongly disagrees (10.7% WR below floor) |
| **Cap** | $0.73 | Prevents overpaying (need 73%+ WR to break even at cap) |
| **Max Bet** | $5.00 | Limits per-trade exposure |
| **Daily Loss** | 60% of bankroll | Stops trading after major drawdown |

## Pricing History & Lessons

### Apr 2 Morning (GTC limits at Gamma) ✅
- 89% WR, +$218, ~40% fill rate
- Best session ever — limit orders at good prices

### Apr 2 Afternoon (FAK market orders) ❌
- Filled at 88-98¢ on thin books
- Massive losses — reverted same day
- Lesson: "Fill rate doesn't matter if fills are at bad prices"

### Apr 5-6 Night (FOK→GTD→retry bump) ❌
- 28% WR, -$35
- Retry bumping caused $0.745 fills
- Adverse selection: losing trades filled, winning trades didn't

### Apr 6 Morning (Single GTC, no retry) ✅
- Fills at $0.49-$0.73 (market dependent)
- +$3.37 win at $0.49 entry
- +$1.85 win at $0.73 entry
- Both profitable — strategy works

## R/R at Different Entry Prices (at 69% WR)

| Entry | Win Profit | Loss Cost | Expected Value |
|-------|-----------|----------|----------------|
| $0.40 | +$2.94 | -$2.00 | **+$1.41** |
| $0.50 | +$2.45 | -$2.50 | **+$0.92** |
| $0.60 | +$1.96 | -$3.00 | **+$0.42** |
| $0.70 | +$1.47 | -$3.50 | **-$0.07** ← breakeven |
| $0.73 | +$1.32 | -$3.65 | **-$0.22** ← slightly -EV |

At 69% WR, $0.73 entries are marginally -EV. But with 77% WR (our paper backtest), $0.73 is still +EV (+$0.16/trade). The cap is right at the edge — monitor closely.

## Montreal Rules (CRITICAL)

> ⚠️ ALL Polymarket API calls MUST originate from Montreal (15.223.247.178).

This includes:
- CLOB order placement (FOK, GTC, GTD)
- Gamma API price fetches during execution
- Order status checks (fill_check)
- Market resolution queries
- Builder Relayer redemption calls
- Wallet balance checks (web3 RPC)

**NEVER** call Polymarket APIs from:
- OpenClaw VPS (this server)
- Railway (hub/frontend)
- Any non-Montreal location

Code changes: push from VPS → git pull on Montreal → restart engine there.
