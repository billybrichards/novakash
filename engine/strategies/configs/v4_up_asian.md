# v4_up_asian Strategy

**Version:** 2.0.0 (Strategy Engine v2 port -- thresholds relaxed)
**Mode:** GHOST
**Direction:** UP only, Asian session

## What Changed from v1

v1 had 0 trades from 19,490 decisions because:
- Confidence band 0.15-0.20 was too narrow
- Combined with parent V4FusionStrategy's 0.12 threshold, eliminated everything

v2 fixes:
- Relaxed min_dist from 0.15 to 0.10 (matches signal distribution)
- Each strategy owns its own gates (no parent override to fight)
- max_dist kept at 0.20 to avoid over-confident signals priced into CLOB

## Analysis

From 5,543 samples (Apr 10-12):
- Asian session (23:00-02:00 UTC): lower liquidity, Asian retail accumulation
- Medium conviction band (dist 0.10-0.20): filters weak signals, avoids priced-in
- WR: 81-99% in the target band
- UP predictions outside Asian session are near-random (50% WR)

## Gate Pipeline

1. **TimingGate** (90-150) -- validated sweet spot
2. **DirectionGate** (UP) -- skip DOWN predictions
3. **ConfidenceGate** (min=0.10, max=0.20) -- medium conviction band
4. **SessionHoursGate** ([23, 0, 1, 2]) -- Asian session UTC

## References

- `docs/analysis/UP_STRATEGY_RESEARCH_BRIEF.md`
- Audit: SIG-05
