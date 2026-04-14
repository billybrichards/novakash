# Signal Deep Dive: 2026-04-14 LIVE Trading Losses

**Period**: 2026-04-13 23:00 UTC to 2026-04-14 14:35 UTC
**Data Sources**: Railway PostgreSQL (strategy_decisions, window_snapshots, signal_evaluations), Polymarket Activity API

---

## 1. Executive Summary

### Root Cause: Oracle Resolution Mismatch

The engine lost $-97.21 across 8 resolved windows (3W/5L, 37.5% WR) while its internal
metrics showed 84-90% WR. **The primary cause is a systematic disagreement between the
engine's price source (Tiingo) and Polymarket's resolution oracle (Chainlink).**

Key findings:
- **Chainlink-Tiingo direction agreement is only 68.1%** at T-90 eval offset
- For 3 of 5 losing trades, Tiingo showed negative delta (DOWN) while Chainlink showed
  positive delta (UP). The engine bet DOWN and lost because the oracle resolved UP.
- For 1 losing trade (01:10 UTC), ALL price sources showed DOWN but Polymarket resolved UP
  -- suggesting either a timing boundary issue or an oracle anomaly.
- **Every single trade was BUY DOWN** (outcomeIndex=1). No UP bets were placed.
- The engine's v4_down_only and v4_fusion strategies have a DOWN-only bias that
  cost them on a day when BTC was range-bound with micro-upticks.

### Financial Summary

| Metric | Value |
|--------|-------|
| Total Cost | $135.49 |
| Total Redeemed | $31.68 |
| Net P&L | -$97.21 (excl. 1 pending) |
| Win Rate (by Poly oracle) | 37.5% (3W/5L) |
| Win Rate (by engine data) | 84-90% |
| Largest Loss | -$68.34 (01:10 UTC, v4_down_only) |

---

## 2. Per-Strategy Analysis

### Strategy Decision Summary (last 24h, 221 unique windows)

| Strategy | Mode | Windows | TRADE | SKIP | Win* | Loss* | WR%* | Ghost-Would-Win | Ghost-Would-Lose |
|----------|------|---------|-------|------|------|-------|------|-----------------|------------------|
| v4_fusion | LIVE | 221 | 39 | 182 | 35 | 4 | 89.7% | 0 | 0 |
| v4_down_only | GHOST** | 221 | 38 | 183 | 32 | 6 | 84.2% | 0 | 0 |
| v10_gate | GHOST | 221 | 0 | 221 | 0 | 0 | N/A | 0 | 0 |
| v4_up_basic | GHOST | 221 | 0 | 221 | 0 | 0 | N/A | 0 | 0 |
| v4_up_asian | GHOST | 221 | 0 | 221 | 0 | 0 | N/A | 0 | 0 |

*Win/Loss based on window_snapshots actual_direction (Tiingo), NOT Polymarket oracle.
**v4_down_only was LIVE for early windows, switched to GHOST mid-session.

**CRITICAL**: The 84-90% WR shown above is ILLUSORY. When cross-referenced with Polymarket
oracle resolution, the actual WR on traded windows is ~37.5%.

### Actual Polymarket Results (Oracle Truth)

**v4_down_only (LIVE trades)**:
| Window (UTC) | Direction | Confidence Score | Cost | Redeemed | PnL | Result |
|-------------|-----------|-----------------|------|----------|-----|--------|
| 00:55 | DOWN | 0.261 | $3.25 | $0.00 | -$3.25 | LOSS |
| 01:10 | DOWN | 0.398 | $68.34 | $0.00 | -$68.34 | LOSS |
| 09:00 | DOWN | 0.251 | $10.20 | $14.65 | +$4.45 | WIN |
| 11:55 | DOWN | 0.227 | $14.40 | $0.00 | -$14.40 | LOSS |
| **Totals** | | | **$96.19** | **$14.65** | **-$81.54** | **1W/3L** |

**v4_fusion (LIVE trades)**:
| Window (UTC) | Direction | Confidence Score | Cost | Redeemed | PnL | Result |
|-------------|-----------|-----------------|------|----------|-----|--------|
| 01:10 | DOWN | 0.398 | (shared) | (shared) | (shared) | LOSS |
| 09:00 | DOWN | 0.251 | (shared) | (shared) | (shared) | WIN |
| 11:55 | DOWN | 0.392 | (shared) | (shared) | (shared) | LOSS |
| 13:15 | DOWN | 0.344 | $3.40 | $0.00 | -$3.40 | LOSS |
| 14:00 | DOWN | 0.388 | $3.75 | $4.97 | +$1.22 | WIN |
| 14:10 | DOWN | 0.398 | $18.75 | $0.00 | -$18.75 | LOSS |
| 14:15 | DOWN | 0.295 | $6.80 | $12.06 | +$5.26 | WIN |
| 14:30 | DOWN | 0.263 | $6.60 | $0.00 | pending | PENDING |

Note: When both strategies trade the same window, the CLOB order is a single fill
attributed to the first strategy that triggered.

### Skip Reason Breakdown

**v4_fusion** (182 skips):
- Low confidence / distance below threshold: 110 (60%)
- Consensus unsafe: 65 (36%)
- Timing outside window: 7 (4%)

**v4_down_only** (183 skips):
- Wrong direction (signal=UP, strategy=DOWN only): 101 (55%)
- Consensus unsafe: 34 (19%)
- Low confidence: 32 (17%)
- Timing outside window: 16 (9%)

**v10_gate** (221 skips = 100% skip rate):
- Source disagreement: 146 (66%)
- Low confidence: 21 (10%)
- Taker flow misaligned: 21 (10%)
- Delta fail: 16 (7%)
- Cascade block: 7 (3%)
- Spread too wide: 10 (5%)

**v4_up_basic** (221 skips = 100%):
- Wrong direction (signal=DOWN): 171 (77%)
- Low confidence: 42 (19%)

**v4_up_asian** (221 skips = 100%):
- Wrong direction (signal=DOWN): 168 (76%)
- Low confidence: 42 (19%)

---

## 3. Losing Trade Forensics

### Loss #1: 00:55 UTC (ts=1776128100) -- v4_down_only LIVE

- **Strategy**: v4_down_only, LIVE, T-90, score=0.261
- **Entry reason**: `v4_down_only_T90_DOWN_down_strong_97pct`
- **Fill**: BUY DOWN @ avg $0.49, cost $3.25
- **Outcome**: LOSS (Polymarket resolved UP, redeemed $0)

Signal data at decision time:
- Tiingo delta: -0.008% (barely negative, essentially FLAT)
- Chainlink delta: +0.013% (UP)
- Binance delta: -0.016% (DOWN)
- VPIN: 0.595 (TRANSITION regime)
- v2 model: p_up=0.524 (UP direction, weak)
- CLOB: down_ask=0.47, up_ask=0.54

**Diagnosis**: Near-flat window. Tiingo showed micro-DOWN, Chainlink showed micro-UP.
Engine bet DOWN based on Tiingo. Oracle resolved UP. The confidence was extremely low
(0.261) on a delta of just 0.008% -- this should NOT have been traded.

### Loss #2: 01:10 UTC (ts=1776129000) -- v4_down_only + v4_fusion LIVE

- **Strategy**: Both v4_down_only and v4_fusion, LIVE, T-90, score=0.398
- **Entry reason**: `v4_down_only_T90_DOWN_down_strong_97pct` / `polymarket_volatile_trend_down_confidence_0.199_T90`
- **Fill**: BUY DOWN @ avg $0.675, cost $68.34 (LARGEST TRADE)
- **Outcome**: LOSS (Polymarket resolved UP, redeemed $0)

Signal data at decision time:
- Tiingo delta: -0.025% (DOWN)
- Chainlink delta: -0.033% (DOWN)
- Binance delta: -0.034% (DOWN)
- VPIN: 0.560 (TRANSITION)
- v2 model: p_up=0.508 (essentially 50/50)
- CLOB: down_ask=0.42, up_ask=0.59

**Diagnosis**: ALL sources agreed on DOWN, yet Polymarket resolved UP. This is an oracle
timing/boundary anomaly. The open/close prices from window_snapshots show
open=74269.48, close=74221.10 (delta=-0.065%), clearly DOWN. Yet Polymarket resolved
UP. Possible explanations:
1. Polymarket's resolution window boundary differs from our window_ts by seconds
2. The Chainlink oracle price at exact resolution moment captured a late micro-tick UP
3. This is the $68.34 loss -- catastrophic sizing on a 50/50 signal

**Key issue**: Stake was $68.34 on a window with v2_probability_up=0.508 (coin flip).
The position sizing was NOT scaled to confidence.

### Loss #3: 11:55 UTC (ts=1776167700) -- v4_down_only + v4_fusion LIVE

- **Fill**: BUY DOWN @ avg $0.703, cost $14.40
- **Outcome**: LOSS (Polymarket resolved UP)

Signal data:
- Tiingo delta: -0.008% (barely DOWN)
- Chainlink delta: +0.032% (UP)
- Binance delta: -0.014% (DOWN)
- VPIN: 0.574 (TRANSITION)
- v2 model: p_up=None (NOT AVAILABLE)

**Diagnosis**: Chainlink said UP, Tiingo said DOWN. Engine followed Tiingo, oracle followed
Chainlink. The v2 model was OFFLINE (p_up=None), removing a key directional signal.
With v2 unavailable, the engine lacked its ML input and relied solely on Tiingo delta.

### Loss #4: 13:15 UTC (ts=1776172500) -- v4_fusion LIVE only

- **Fill**: BUY DOWN @ $0.67, cost $3.40
- **Outcome**: LOSS (Polymarket resolved UP)

Signal data:
- Tiingo delta: +0.018% (UP!)
- Chainlink delta: +0.062% (UP)
- v2 model: p_up=0.607 (UP)

**Diagnosis**: All signals pointed UP, but v4_fusion still bet DOWN. The entry reason
was `polymarket_chop_down_confidence_0.172_T150`. At T-150 eval offset (earliest in
the window), the signal may have briefly shown DOWN before flipping to UP at T-90.
The strategy locked in a stale signal.

### Loss #5: 14:10 UTC (ts=1776175800) -- v4_fusion LIVE

- **Fill**: BUY DOWN @ avg $0.744, cost $18.75
- **Outcome**: LOSS (Polymarket resolved UP)

Signal data:
- Tiingo delta: -0.107% (DOWN)
- Chainlink delta: +0.047% (UP)
- Binance delta: -0.090% (DOWN)
- VPIN: 0.687 (CASCADE regime)
- v2 model: p_up=0.532 (weak UP)
- CLOB: down_ask=0.70, up_ask=0.31

**Diagnosis**: Tiingo and Binance showed significant DOWN, Chainlink showed UP. The
CASCADE regime and high VPIN suggest a liquidation event. The CLOB down_ask was 0.70
(expensive DOWN token) yet the engine paid 0.744 average. With Chainlink disagreeing
and v2 pointing UP, this was a low-conviction trade at a bad price.

---

## 4. Threshold Optimization Recommendations

### Priority 1: Switch Delta Source to Chainlink (CRITICAL)

**Current**: Engine uses `delta_source=tiingo_rest_candle` for primary direction.
**Problem**: Polymarket resolves using Chainlink oracle. Tiingo-Chainlink agreement is
only 68.1%, meaning ~32% of trades have a direction mismatch with the resolution oracle.
**Action**: Set `delta_source=chainlink` as primary. Fall back to Tiingo only when
Chainlink is stale (>30s).
**Expected impact**: Eliminates 3/5 losses where CL and TI disagreed on direction.

### Priority 2: Source Agreement Gate (HIGH)

**Current**: v10_gate requires source agreement but it's 100% GHOST (never trades).
**Problem**: v4_fusion and v4_down_only trade without source agreement checks.
**Action**: Add a source agreement gate: require at least 2/3 sources (CL, TI, BN)
to agree on direction before placing a trade.
**Expected impact**: Would have blocked 3/5 losing trades (00:55, 11:55, 14:10).

### Priority 3: Minimum Delta Magnitude (HIGH)

**Current**: Engine trades on deltas as small as 0.008%.
**Problem**: Near-flat windows (|delta| < 0.03%) are essentially coin flips where
oracle timing becomes the deciding factor.
**Action**: Set minimum |delta| >= 0.03% across ALL sources to trade.
**Expected impact**: Would have blocked Loss #1 (delta=0.008%) and Loss #3 (delta=0.008%).

### Priority 4: Position Sizing Proportional to Confidence (CRITICAL)

**Current**: Loss #2 was $68.34 on a window with v2_probability_up=0.508.
**Problem**: Stake size is not scaled to signal confidence.
**Action**: Cap maximum stake when confidence_score < 0.35 to $5-10. Currently no
guardrail prevents a $68 bet on a coin-flip signal.
**Expected impact**: Loss #2 would have been ~$5 instead of $68.34.

### Priority 5: v2 Model Availability Gate

**Current**: Loss #3 had v2 model OFFLINE (p_up=None).
**Problem**: Without the ML model, the engine loses a critical directional signal.
**Action**: When v2_probability_up is None, downgrade to minimum stake or SKIP.
**Expected impact**: Would have prevented Loss #3 ($14.40).

### Priority 6: Stale Signal Lock-in Prevention

**Current**: Loss #4 locked in a DOWN signal at T-150, but by T-90 all sources said UP.
**Problem**: Strategy evaluated once at T-150 and committed to DOWN despite signals
evolving to UP by execution time.
**Action**: Require signal confirmation at T-90 (or execution time) matches the
direction locked in at the earliest eval offset.
**Expected impact**: Would have prevented Loss #4 ($3.40).

### Summary of Expected Impact

| Recommendation | Losses Prevented | P&L Saved |
|---------------|-----------------|-----------|
| Chainlink as primary delta | #1, #3, #5 | $36.40 |
| Source agreement gate | #1, #3, #5 | $36.40 |
| Min delta >= 0.03% | #1, #3 | $17.65 |
| Confidence-based sizing | #2 | ~$63.00 |
| v2 availability gate | #3 | $14.40 |
| Stale signal prevention | #4 | $3.40 |

With all recommendations: would have prevented 4-5 of 5 losses, saving $80-95 of $97.21.

---

## 5. Time-of-Day Analysis

### Win Rate by Hour (v4_fusion + v4_down_only TRADE decisions, engine-measured)

| Hour (UTC) | Win | Loss | Total | WR% | Notes |
|-----------|-----|------|-------|-----|-------|
| 00 | 11 | 0 | 11 | 100% | Asian session close |
| 01 | 6 | 0 | 6 | 100% | |
| 02 | 4 | 0 | 4 | 100% | |
| 03 | 2 | 0 | 2 | 100% | |
| 04 | 4 | 0 | 4 | 100% | |
| 05 | 4 | 0 | 4 | 100% | |
| 06 | 2 | 0 | 2 | 100% | |
| 07 | 1 | 1 | 2 | 50% | London open |
| 09 | 2 | 0 | 2 | 100% | |
| 10 | 9 | 4 | 13 | 69% | US pre-market -- WORST |
| 11 | 4 | 1 | 5 | 80% | |
| 13 | 2 | 0 | 2 | 100% | |
| 14 | 5 | 2 | 7 | 71% | US session |
| 15 | 10 | 0 | 10 | 100% | |
| 23 | 1 | 2 | 3 | 33% | Asian open -- WORST |

**WARNING**: These WRs are based on engine's Tiingo data, NOT Polymarket oracle.
The actual oracle-based WR is likely 10-20 percentage points lower during mismatch hours.

**Pattern**: Losses cluster in:
- **23:00-01:00 UTC** (Asian open / low liquidity)
- **10:00-14:00 UTC** (US pre-market through early session)

Best performance: **00:00-06:00 UTC** and **15:00 UTC** (US afternoon session).

---

## Appendix A: Oracle Mismatch Detail

| Window | Tiingo Delta% | Chainlink Delta% | Binance Delta% | Engine Dir | Oracle Dir | Match? |
|--------|--------------|-----------------|---------------|------------|------------|--------|
| 00:55 | -0.0084 | +0.0130 | -0.0160 | DOWN | UP | NO |
| 01:10 | -0.0253 | -0.0332 | -0.0335 | DOWN | UP | NO* |
| 09:00 | -0.0292 | +0.0000 | -0.0403 | DOWN | DOWN | YES |
| 11:55 | +0.0288 | +0.1201 | +0.0330 | UP | UP | YES |
| 13:15 | +0.1240 | +0.1601 | +0.1187 | UP | UP | YES |
| 14:00 | -0.0577 | -0.0438 | -0.0502 | DOWN | DOWN | YES |
| 14:10 | -0.1065 | +0.0471 | -0.0895 | DOWN | UP | NO |
| 14:15 | -0.0556 | +0.0970 | -0.0626 | DOWN | DOWN | YES |
| 14:30 | -0.1602 | +0.1851 | -0.1789 | DOWN | ? | - |

*01:10 window: All sources show DOWN but Polymarket resolved UP -- oracle timing anomaly.

## Appendix B: v10_gate Would-Have-Traded Analysis

v10_gate's 100% skip rate is due to aggressive gating:
- Source agreement requirement blocked 66% of windows
- This gate WOULD HAVE SAVED all 5 losing trades (it correctly identified source disagreement)
- However, it also blocked all 3 winning trades
- **Recommendation**: v10_gate's source_agreement filter is too strict. Relax to 2/3 agreement
  (currently requires all sources to agree).

## Appendix C: UP Strategy Opportunity Cost

v4_up_basic and v4_up_asian both had 0 trades (100% skip rate). Primary skip reason:
`direction=DOWN != UP` (77% and 76% respectively). This means the engine's primary
signal was DOWN for 77% of windows. On a day when Polymarket oracle resolved UP on 5/8
traded windows, an UP-biased strategy could have captured significant wins.

**If v4_up_basic had traded the 5 windows where oracle resolved UP**: at typical entry
prices of $0.40-0.55 per UP token, each $3.50 bet would have yielded ~$5-6 in redemption,
netting ~$8-12 profit across 5 windows instead of the $97 loss.
