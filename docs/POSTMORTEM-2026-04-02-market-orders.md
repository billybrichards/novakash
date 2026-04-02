# Post-Mortem: FAK Market Order Disaster — 2 April 2026

## What happened
Live trading went from +$150 profit to -$49 loss in the afternoon.

## Timeline
- **10:00-13:00** — Limit orders at Gamma API prices (38-52¢). 89% win rate. +$218 profit.
- **13:29** — Fill rate was ~40%. Billy asked to improve fills.
- **13:30** — Changed to +2¢ price bump. 0% fill rate. Reverted.
- **13:59** — Changed to FOK market orders. Rejected (all-or-nothing).
- **14:00** — Changed to FAK market orders (Fill and Kill). Orders filled at 88-98¢.
- **14:00-16:30** — Engine bought tokens at terrible prices. Losses mounted.
- **16:32** — Live trading paused.

## Why it failed
FAK market orders cross the spread and take whatever liquidity exists. On thin 5-min books, the only sellers were at 88-98¢.

| Token Price | Win Profit | Loss Cost | Break-even accuracy |
|---|---|---|---|
| 49¢ (morning) | +51¢ (104%) | -49¢ | 49% |
| 98¢ (afternoon) | +2¢ (2%) | -98¢ | 98% |

Even with 89% accuracy, buying at 98¢ is guaranteed to lose money.

## The fix
1. **Reverted to GTC limit orders** at Gamma API price (what worked)
2. **Added 65¢ max token price cap** — engine refuses to buy above this
3. **Removed all market order (FAK/FOK) code**

## Lesson
Fill rate doesn't matter if the fills are at bad prices. Better to fill 40% of trades at 49¢ (great risk/reward) than 90% at 98¢ (terrible risk/reward).

## Impact
- Lost approximately $100 in the afternoon from bad fills
- Starting deposit: $209, ending balance: ~$160
- Morning strategy was genuinely profitable and validated
