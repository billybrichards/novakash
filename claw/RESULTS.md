# Trading Results & Analysis

## Performance Summary (2026-04-01 to 2026-04-02)

### Morning Strategy (T-60s Single-Shot) ✅
- **P&L:** +$93
- **Win Rate:** 67%
- **Strategy:** Evaluate once at T-60s, place GTC limit order at Gamma API price
- **Signal:** Delta 0.03-0.09% → MODERATE (65%), VPIN 0.65-0.93
- **Token Price Range:** 38-52¢
- **This is the WORKING strategy — currently deployed**

### Afternoon Changes ❌
- **P&L:** -$258
- **What Changed:** Continuous evaluator (PR #2), FAK market orders, fill improvements
- **Root Cause:** Over-engineering the evaluation loop, market orders getting bad fills
- **Resolution:** Reverted to morning T-60s single-shot

### Net P&L
- **Deposit:** $209
- **Current Wallet:** ~$44 USDC
- **Net Loss:** -$165
- **Positions Value:** ~$129.60

---

## Key Lessons

1. **T-60s entry timing is the sweet spot** — enough data, but tokens not yet at 95¢+
2. **GTC limit orders >> FAK market orders** — better fills, less slippage
3. **Single evaluation >> continuous loop** — the morning strategy worked because it was simple
4. **Window delta is king** — directly answers "is BTC up or down vs window open?"
5. **Don't over-engineer what's working** — the afternoon -$258 was from unnecessary changes

---

## Postmortem: Market Orders (2026-04-02)

**File:** `docs/POSTMORTEM-2026-04-02-market-orders.md`

Switching from GTC limit orders to FAK market orders caused significant losses:
- Bad fills at inflated prices
- Reverted to GTC limit at Gamma API best price
- Lesson: Always use limit orders for 5-min markets

---

## Backtest Results

**File:** `BACKTEST_RESULTS.md`  
**Reports:** `backtest_report.pdf`, various PNG equity curves

Key backtests:
- 1-day, 7-day, 14-day, 28-day periods
- Delta-based token pricing (realistic, not fixed $0.50)
- Safe mode (25% bankroll per trade) most consistent

---

## Signal Mathematics

**File:** `docs/signal-mathematics-v3.1.pdf` (818KB)

Detailed mathematical analysis of:
- VPIN calculation methodology
- Window delta signal weights
- Confidence scoring formula
- Token pricing model (delta → price mapping)

---

## Live Trading Analysis

**File:** `docs/live-trading-analysis-2026-04-02.pdf` (434KB)

Real trade-by-trade analysis from April 2 live session.
