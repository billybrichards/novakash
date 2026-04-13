# v4_down_only Strategy

**Version:** 2.0.0 (Strategy Engine v2 port)
**Mode:** LIVE
**Direction:** DOWN only

## Analysis

Based on 897K-sample analysis (2026-04-12):
- DOWN predictions: 76-99% WR across all CLOB bands
- UP predictions: 1.5-53% WR across all CLOB bands (always skip)
- Validated timing window: T-90 to T-150 (90.3% WR)
- Confidence threshold: dist >= 0.10 (relaxed from V4's 0.12, same WR)

## Gate Pipeline

1. **TimingGate** (90-150) -- validated T-90 to T-150 sweet spot
2. **DirectionGate** (DOWN) -- skip all UP predictions
3. **ConfidenceGate** (min=0.10) -- relaxed from 0.12, adds ~50K trades at same WR
4. **TradeAdvisedGate** -- V4 polymarket trade_advised must be True
5. **CLOBSizingGate** -- CLOB-based position sizing (see schedule below)

## CLOB Sizing Schedule

| CLOB Down Ask | Modifier | WR | Label |
|--------------|----------|-----|-------|
| >= 0.55 | 2.0x | 97%+ | strong_97pct |
| 0.35-0.55 | 1.2x | 88-93% | mild_88pct |
| 0.25-0.35 | 1.0x | 87% | contrarian_87pct |
| < 0.25 | SKIP | 53%/31% | skip_sub25_53pct |
| NULL | 1.5x | 99% | no_clob_99pct |

## References

- `docs/analysis/DOWN_ONLY_STRATEGY_2026-04-12.md`
- Audit: SIG-03, SIG-04
