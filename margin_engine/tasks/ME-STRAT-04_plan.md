# Regime-Adaptive Strategy Selection (ME-STRAT-04)

**Status**: Complete ✓
**Created**: 2026-04-12
**Completed**: 2026-04-12

## Overview

Implement different trading strategies based on market regime classification from V4 snapshot.

- **TRENDING_UP/TRENDING_DOWN**: Trend-following (larger size, wider stops, hold longer)
- **MEAN_REVERTING**: Fade extremes (smaller size, tighter stops, quick profits)
- **CHOPPY/NO_EDGE**: No trade or very small size

## Implementation Plan

### Phase 1: Domain Foundation
- [x] Review existing V4Snapshot structure ✓
- [x] Create `margin_engine/domain/strategy.py` (Strategy ABC, Regime enum, TradeDecision) ✓

### Phase 2: Strategy Implementations
- [x] `margin_engine/services/regime_trend.py` (TrendStrategy) ✓
- [x] `margin_engine/services/regime_mean_reversion.py` (MeanReversionStrategy) ✓
- [x] `margin_engine/services/regime_no_trade.py` (NoTradeStrategy) ✓
- [x] `margin_engine/services/regime_adaptive.py` (Router) ✓

### Phase 3: Configuration
- [x] Add settings to `margin_engine/infrastructure/config/settings.py` ✓

### Phase 4: Integration
- [x] Integrate with `open_position.py` (add regime router after existing gates) ✓
- [x] Update `manage_positions.py` if needed (hold time, trailing stops) - N/A

### Phase 5: Testing
- [x] Create `margin_engine/tests/unit/test_regime_adaptive.py` (20+ test cases) ✓
- [x] Run pytest to verify all tests pass ✓ (36 tests passed)

### Phase 6: Documentation
- [x] Create `docs/V4_STRATEGY_FOUNDATION.md` ✓

## Deliverables

1. ✓ Strategy domain layer (`domain/strategy.py`)
2. ✓ Three strategy implementations (trend, mean-reversion, no-trade)
3. ✓ Regime router (`services/regime_adaptive.py`)
4. ✓ Configuration settings
5. ✓ Integration with existing use cases
6. ✓ Comprehensive unit tests (36 tests)
7. ✓ Documentation

## Test Results

```
36 passed in 0.07s
106 total tests passed (including existing tests)
```

## Notes

- Feature flag: `regime_adaptive_enabled` (default False for safe rollout)
- Maintain backwards compatibility ✓
- Regime router decisions ADD TO existing gates (not replace) ✓
- Log regime + strategy choice for each trade ✓

## Usage

Enable via environment variable:
```bash
export MARGIN_REGIME_ADAPTIVE_ENABLED=true
```

Or pass to OpenPositionUseCase:
```python
uc = OpenPositionUseCase(
    ...
    regime_adaptive_enabled=True,
    ...
)
```

See `docs/V4_STRATEGY_FOUNDATION.md` for full documentation.
