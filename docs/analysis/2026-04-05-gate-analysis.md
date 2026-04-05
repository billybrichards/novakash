# Gate Analysis — Is the Gate Too Tight?

**ID:** `analysis-2026-04-05-gate-001`
**Date:** 2026-04-05 15:40 UTC
**Author:** Novakash
**Period:** Apr 3–5 2026 (1,940 windows, 4 assets)
**Status:** ⚠️ CRITICAL — Gate is blocking profitable trades

---

## Executive Summary

The VPIN/delta gate is **catastrophically too tight**. It blocked **$1,493.50 of profit** across 4 assets over 2.5 days. The blocked trades had **higher accuracy** than the ones that passed. The gate is actively making performance worse.

## Data

| Asset | Windows | v5.7c Accuracy | Ungated P&L | Traded | Gated Acc | Gate Missed P&L |
|-------|---------|----------------|-------------|--------|-----------|-----------------|
| BTC   | 557     | 98.4%          | +$892.64    | 260    | 96.5%     | **+$685.98**    |
| ETH   | 455     | 99.8%          | +$301.84    | 134    | 99.3%     | +$290.08        |
| SOL   | 456     | 100.0%         | +$282.24    | 183    | 100.0%    | +$239.12        |
| XRP   | 472     | 100.0%         | +$297.92    | 164    | 100.0%    | +$278.32        |
| **Total** | **1,940** | **99.5%** | **+$1,774.64** | **741** | **98.9%** | **+$1,493.50** |

*All P&L at $4 stake, real Gamma entry prices from Polymarket, binary resolution $1/$0, 2% fee on wins.*

## Key Findings

### 1. Gate is blocking correct trades
- **Blocked trades: 99.5% accuracy** (gated windows)
- **Passed trades: 98.9% accuracy** (traded windows)
- The gate isn't filtering out bad trades — it's filtering out ALL trades below a delta threshold, and those small-delta trades are overwhelmingly correct

### 2. Small delta ≠ wrong direction
- Most blocked trades have delta < 0.08% — the price move is small but the direction is right
- On Polymarket, a correct direction call at $0.50 entry pays $1.96 (after 2% fee) regardless of HOW MUCH the price moved
- The gate's delta threshold is optimised for "big moves" but the market is binary

### 3. BTC is the exception
- BTC gated accuracy (96.5%) is slightly lower than ungated (98.4%)
- But 96.5% on $4 bets is still massively profitable: +$892 ungated vs ~$207 gated
- Even BTC's gate is too tight

### 4. ETH/SOL/XRP are nearly perfect
- 99.8-100% accuracy with or without gate
- Gate blocked $800+ of pure profit across these 3 assets

## Root Cause

The gate uses two thresholds:
1. **VPIN gate** (0.45): requires informed trading flow — too high for ranging markets
2. **Delta threshold** (0.08-0.12%): requires minimum price move — but any move is enough for binary prediction

These thresholds were designed for a world where bigger moves = more confident predictions. But the actual data shows v5.7c is right ~99% of the time regardless of move size.

## Recommendation

### Option A: Remove the gate entirely
- Expected P&L: +$1,774/2.5 days = **+$710/day** at $4 stake
- Risk: If accuracy drops below ~52% on any asset, loses money
- This period may be unusually easy (strong trend)

### Option B: Loosen the gate significantly
- VPIN gate: 0.45 → 0.20 (or remove)
- Delta threshold: 0.08% → 0.02%
- This keeps some protection against truly random noise

### Option C: Use accuracy-based gate
- Track rolling 50-window accuracy per asset
- If accuracy > 90%: trade everything (no gate)
- If accuracy 70-90%: use current thresholds
- If accuracy < 70%: pause trading for that asset

### ⚠️ CAVEAT
This analysis covers a **bearish 2.5-day period** where BTC dropped steadily. In a ranging/choppy market, accuracy could be significantly lower. Need 7+ days of data including regime changes before committing to removing the gate.

## TimesFM Comparison

| Metric | v5.7c | TimesFM |
|--------|-------|---------|
| BTC Accuracy | 98.4% (557 windows) | 72.7% (88 windows) |
| Sample size | Large | Small |
| Direction bias | Balanced | Heavy DOWN |

TimesFM is not yet reliable enough to gate on. v5.7c alone is the stronger signal.

## Next Steps

1. Run ungated paper mode for 24h to validate in real-time
2. Collect accuracy data across a regime change (bullish reversal)
3. If accuracy holds >95% ungated for 24h, consider Option B for live
4. Build rolling accuracy tracker to auto-tighten/loosen gate
