# Things to Monitor — Live Trading

**Last updated:** 2026-04-06

## Price Cap ($0.73)

Cap-blocked trades analysis (Apr 6 morning):
- 3 wins, 1 loss blocked by $0.73 cap
- If cap was $0.80: +$2.84 extra but at thin R/R margins ($0.77-0.78 entries)
- At 77% WR, $0.77 entries barely break even
- **Decision:** Keep at $0.73 for safety. Revisit if WR consistently >77%

## Fill Price vs Gamma BestAsk

The CLOB fills at market, not our limit. Monitor:
- Gamma bestAsk at T-70 vs actual CLOB fill price
- Market can move drastically in 5-10 seconds (saw $0.49 → $0.10 in 6s)
- bestAsk pricing mode fills near market — much better than cap mode ($0.73)
- Track via `countdown_evaluations` table (T-70 gamma vs trade entry_price)

## Adverse Selection

When our token is cheap (<$0.30), the market is 70-90% against us:
- $0.04 entries: 10.7% WR — market knows something we don't
- $0.30 floor blocks these, but market can move AFTER we submit
- Monitor: how many fills end up below $0.30 despite passing the floor at T-70?

## TWAP Override

TWAP can flip our direction when it disagrees with delta:
- Apr 6 10:20: Delta +0.055% (UP) but TWAP overrode to DOWN → LOSS
- When TWAP overrides and delta strongly disagrees, we bet against current price movement
- **Consider:** Disable TWAP override when delta > 0.05% in opposite direction

## TimesFM Disagreement

When TimesFM strongly disagrees (>90% confidence opposite):
- 40% WR on disagreement trades vs 85% on agreement
- Not yet a blocking gate — logged as "timesfm_agreement=False"
- **Consider:** Add as soft gate — reduce bet size or skip when TsFM disagrees

## Oracle vs BTC Price

Polymarket oracle can disagree with BTC spot price direction:
- BTC went DOWN -0.097% but oracle resolved UP (Apr 6 11:00)
- Engine now resolves ONLY from Polymarket oracle (never Binance)
- Monitor: how often does oracle disagree with spot? Is there a pattern?

## Entry Timing (T-70 offset)

Changed from T-60 to T-70 on Apr 6:
- 10 seconds earlier submission → orders hit book before last-minute rushes
- Monitor: do T-70 fills get better prices than T-60?
- Check countdown_evaluations: gamma at T-90 vs T-70 vs actual fill

## Redeemer

Auto-redemption via Builder Relayer (PROXY type):
- Working for most positions
- Some "Failed" attempts on already-settled or stale positions
- Monitor: are all live wins getting redeemed within 5 min?

## Daily Loss Limit

Set to 60% of bankroll (~$66 at current $111):
- Was 20% ($24) — too tight, blocked 15 profitable trades for 1.5 hours
- At $5 max bet, takes 13 consecutive losses to hit 60% limit
- Monitor: if we hit the limit, was it justified or did we miss profitable windows?

## Multi-Asset Expansion

Currently BTC only (5-min). Potential additions:
- ETH 5-min — same strategy, doubles window count
- SOL/XRP 5-min — more volume but may have different dynamics  
- BTC 15-min — FIFTEEN_MIN_ENABLED=false, wider windows, better liquidity
- Monitor: check market_data for ETH/SOL/XRP WR before enabling

## DB Accuracy

Reconciler runs every 5 min syncing with Polymarket activity API:
- Fixes entry_price mismatches (engine records limit, PM shows fill)
- Monitor: how many reconcile.price_mismatch events per day?
- Target: zero mismatches once fill_price recording is stable

## Notification Accuracy

Reporter bot (position_monitor) shows real Polymarket data.
Engine notifications may lag or show different prices.
- Reporter is source of truth for P&L
- Engine notifications are for lifecycle tracking
- Monitor: do they converge after reconciler runs?
