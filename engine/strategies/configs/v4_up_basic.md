# v4_up_basic Strategy

**Version:** 1.0.0
**Mode:** GHOST (paper trading for validation)
**Direction:** UP only

## Problem

Current `v4_up_asian` has 0 trades from 19,490 decisions:
- Confidence threshold dist >= 0.12 eliminates ALL signals (100% in 0.60-0.65 range)
- Asian-only restriction is counterproductive (non-Asian has 5x more signals)
- Timing window T-90 to T-150 rejects 31.6% of potential trades

## Solution

Global UP strategy with relaxed thresholds to complement v4_down_only.

## Gate Pipeline

1. **TimingGate** (60-180) -- wider window than DOWN's T-90-150
2. **DirectionGate** (UP) -- skip all DOWN predictions
3. **ConfidenceGate** (min=0.10) -- captures 88.9% of signals

## Expected Performance

| Metric | Value |
|--------|-------|
| Daily trades | 5-15 |
| Win rate | 70-80% |
| PnL/day | +2-5% bankroll |

## Validation Plan

1. Deploy in GHOST mode
2. After 3-5 days: check WR
3. If WR >= 70%: promote to LIVE with 50% sizing
4. If WR < 70%: add stricter gates (trade_advised, TimesFM)

## References

- `docs/V4_UP_BASIC_STRATEGY.md`
- Audit: SIG-06
