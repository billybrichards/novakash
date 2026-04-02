# Changelog — 2 April 2026

## What Worked ✅ (Morning 6-10am ET)

**Simple T-60s single-shot strategy:**
- Delta + VPIN evaluated once at T-60s before window close
- MODERATE confidence (65%) at 0.03-0.09% delta
- VPIN range: 0.65-0.93 (always high)
- GTC limit orders at exact Gamma API price
- Token prices: 38-52¢
- **Result: +$93 profit, 67% win rate on resolved trades**

## What We Changed & What Broke ❌

### Change 1: FAK Market Orders (14:00)
- **Why:** Fill rate was ~40% with limit orders
- **What happened:** Bought tokens at 88-98¢ (terrible risk/reward)
- **Loss:** ~$100
- **Fix:** Reverted to limit orders + added 65¢ price cap

### Change 2: PR #2 Continuous Evaluator (17:27)
- **Why:** Monitor full window T-300s→T-5s instead of single shot
- **What happened:** 
  - Delta weights were 7.0 (should be 3.0) → inflated confidence
  - CoinGlass weights all zero → normalisation broken (÷7 when max was 6)
  - Everything fired as DECISIVE 95-100% on weak 0.05% deltas
  - Token IDs not found (different code path)
- **Loss:** ~$150+
- **Fix attempts:** v3, v3.1 recalibration — still overconfident

### Change 3: Various fill improvements
- +2¢ bump, smart retry — marginal improvements but added complexity
- Each deploy = engine restart = missed windows

## What We're Reverting To 🔄

**Morning strategy (single-shot T-60s):**
1. Feed signals at T-60s before window close
2. Simple delta + VPIN evaluation
3. HIGH: |delta| > 0.10%, MODERATE: |delta| > 0.02%
4. GTC limit orders at Gamma API price
5. 30-65¢ price cap (5m), 30-70¢ (15m)
6. Price-scaled stake sizing (kept — good improvement)

**Kept from afternoon work:**
- Trade persistence to DB ✅
- Real Polymarket position monitor for WIN/LOSS ✅
- 5-minute sitrep ✅
- Price cap (30-65¢) ✅
- Bankroll sync from portfolio value ✅
- Fill verification before Telegram alerts ✅

## Lessons

1. **Never iterate on live money.** Paper test first.
2. **Simple beats complex.** Morning's 3-line evaluation beat the 300-line evaluator.
3. **VPIN is the signal.** Delta confirms direction, VPIN confirms conviction.
4. **Fill rate < signal quality.** Better to miss trades than take bad ones.
5. **Token price is king.** 30-50¢ = profitable. 70¢+ = guaranteed loss.

## Financial Impact

| Period | Strategy | P&L |
|---|---|---|
| Morning (6-10am ET) | Simple T-60s | **+$93** |
| Afternoon (10am-8pm ET) | Various changes | **-$258** |
| **Net** | | **-$165** |
| Deposit: $209 | Balance: ~$44 | |
