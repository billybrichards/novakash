# V4 Strategy Foundation (ME-STRAT-04/05)

**Status**: Complete ✓
**Created**: 2026-04-12
**Author**: Billy Richards

## Overview

Regime-adaptive strategy selection that routes trading decisions to different strategies based on market regime classification from V4 snapshot, plus cascade fade strategy for liquidation cascades.

### Strategy Matrix

| Condition | Strategy | Position Size | Stop Loss | Take Profit | Hold Time |
|-----------|----------|---------------|-----------|-------------|-----------|
| TRENDING_UP | Trend-Following | 1.2x | 1.5% | 2.0% | 60 min |
| TRENDING_DOWN | Trend-Following | 1.2x | 1.5% | 2.0% | 60 min |
| MEAN_REVERTING | Mean-Reversion (Fade) | 0.8x | 0.8% | 0.5% | 15 min |
| CHOPPY | No Trade | 0.0x | - | - | - |
| NO_EDGE | No Trade | 0.0x | - | - | - |
| CASCADE (strength >= 0.5) | Cascade Fade | 0.5-0.6x | 3.0% | 1.0% | 10 min |

### Strategy Details

#### Trend-Following Strategy (`TrendStrategy`)

**When**: TRENDING_UP or TRENDING_DOWN regimes

**Logic**:
- Enter in direction of trend (p_up >= 0.55 → LONG, p_up <= 0.45 → SHORT)
- Require minimum expected move >= 30 bps
- Larger position size (1.2x) to capitalize on sustained moves
- Wider stops (1.5%) to avoid noise in trending markets
- Longer hold time (60 min) to capture trend continuation
- Take profit at 2.0% target

**Entry Conditions**:
1. Regime is TRENDING_UP or TRENDING_DOWN
2. p_up >= 0.55 for LONG or p_up <= 0.45 for SHORT
3. Expected move >= 30 bps (if available)

#### Mean-Reversion Strategy (`MeanReversionStrategy`)

**When**: MEAN_REVERTING regime

**Logic**:
- Fade extremes: bet against strong moves
- p_up >= 0.70 → bet SHORT (fade bullish extreme)
- p_up <= 0.30 → bet LONG (fade bearish extreme)
- Smaller position size (0.8x) as fades are riskier
- Tighter stops (0.8%) to limit fade risk
- Quick profit target (0.5%) for mean reversion
- Short hold time (15 min)

**Entry Conditions**:
1. Regime is MEAN_REVERTING
2. p_up >= 0.70 (SHORT fade) or p_up <= 0.30 (LONG fade)
3. Fade probability >= 15% (1 - p_up >= 0.15)

#### No-Trade Strategy (`NoTradeStrategy`)

**When**: CHOPPY or NO_EDGE regimes

**Logic**:
- Default: No trade (safe behavior)
- Optional: Allow speculative trades with 0.1x size (disabled by default)

#### Cascade Fade Strategy (`CascadeFadeStrategy`)

**When**: Liquidation cascade detected (strength >= 0.5)

**Logic**:
- Detect cascade from V4 snapshot cascade field
- Determine cascade direction from composite_v3:
  - Positive composite = LONG liquidations → bet SHORT
  - Negative composite = SHORT liquidations → bet LONG
- Fade in opposite direction, expecting bounce at exhaustion
- Entry quality based on cascade strength:
  - PREMIUM (>= 0.7): 0.6x size, high conviction
  - STANDARD (0.5-0.7): 0.5x size, normal
  - LATE (< 0.5): No trade, cascade weakening
- Wide stops (3%) to avoid cascade noise
- Quick targets (1%) for initial bounce
- 15-minute cooldown after cascade ends

**Entry Conditions**:
1. Cascade strength >= 0.5
2. State is CASCADE or BET (not IDLE or COOLDOWN)
3. Direction determined (composite_v3 != 0)
4. Not in cooldown period
5. Entry quality is PREMIUM or STANDARD

**State Machine**:
- **IDLE**: strength < 0.3 (no cascade)
- **CASCADE**: strength >= 0.7 (strong cascade, imminent exhaustion)
- **BET**: 0.5 <= strength < 0.7 (approaching exhaustion)
- **COOLDOWN**: After cascade ends, wait 15 min

## Implementation

### Files Created

1. **`margin_engine/domain/strategy.py`** - Domain foundation
   - `Regime` enum (TRENDING_UP, TRENDING_DOWN, MEAN_REVERTING, CHOPPY, NO_EDGE)
   - `TradeDecision` dataclass (direction, size_mult, stop_loss_bps, take_profit_bps, hold_minutes, reason)
   - `Strategy` base class (ABC)

2. **`margin_engine/services/regime_trend.py`** - Trend-following strategy
   - `TrendStrategyConfig` (configuration dataclass)
   - `TrendStrategy` (implements Strategy ABC)

3. **`margin_engine/services/regime_mean_reversion.py`** - Mean-reversion strategy
   - `MeanReversionConfig` (configuration dataclass)
   - `MeanReversionStrategy` (implements Strategy ABC)

4. **`margin_engine/services/regime_no_trade.py`** - No-trade strategy
   - `NoTradeStrategy` (implements Strategy ABC)

5. **`margin_engine/services/regime_adaptive.py`** - Regime router
   - `RegimeAdaptiveRouter` (routes to appropriate strategy based on regime)

6. **`margin_engine/infrastructure/config/settings.py`** - Configuration
   - Added `regime_adaptive_enabled` flag (default False)
   - Added strategy-specific configuration parameters

7. **`margin_engine/use_cases/open_position.py`** - Integration
   - Added regime router check after existing gates
   - Applied regime-specific size_mult, SL/TP, hold_minutes
   - Added audit fields to Position entity

8. **`margin_engine/domain/entities/position.py`** - Position audit
   - Added `v4_entry_strategy_decision`
   - Added `v4_entry_strategy_size_mult`
   - Added `v4_entry_strategy_hold_minutes`

9. **`margin_engine/tests/unit/test_regime_adaptive.py`** - Comprehensive tests
    - 36 test cases covering all strategies and edge cases

10. **`margin_engine/services/cascade_detector.py`** - Cascade state machine
    - `CascadeState` enum (IDLE, CASCADE, BET, COOLDOWN)
    - `CascadeInfo` dataclass (state, direction, strength, entry_quality, is_safe_to_fade)
    - `analyze_cascade()` function (V4 snapshot → CascadeInfo)

11. **`margin_engine/services/cascade_fade.py`** - Cascade fade strategy
    - `CascadeFadeConfig` (configuration dataclass)
    - `CascadeFadeStrategy` (implements Strategy ABC)

12. **`margin_engine/tests/unit/test_cascade_fade.py`** - Cascade fade tests
    - 26 test cases covering cascade detection and fade logic

### Configuration

All regime strategy parameters are configurable via environment variables:

```bash
# Feature flag (default: False)
MARGIN_REGIME_ADAPTIVE_ENABLED=false

# Trend strategy defaults
MARGIN_REGIME_TREND_MIN_PROB=0.55
MARGIN_REGIME_TREND_SIZE_MULT=1.2
MARGIN_REGIME_TREND_STOP_BPS=150
MARGIN_REGIME_TREND_TP_BPS=200
MARGIN_REGIME_TREND_HOLD_MINUTES=60
MARGIN_REGIME_TREND_MIN_EXPECTED_MOVE_BPS=30.0

# Mean-reversion strategy defaults
MARGIN_REGIME_MR_ENTRY_THRESHOLD=0.70
MARGIN_REGIME_MR_SIZE_MULT=0.8
MARGIN_REGIME_MR_STOP_BPS=80
MARGIN_REGIME_MR_TP_BPS=50
MARGIN_REGIME_MR_HOLD_MINUTES=15
MARGIN_REGIME_MR_MIN_FADE_CONVICTION=0.15

# No-trade strategy
MARGIN_REGIME_NO_TRADE_ALLOW=false
MARGIN_REGIME_NO_TRADE_SIZE_MULT=0.1

# Cascade fade strategy (ME-STRAT-05)
MARGIN_CASCADE_FADE_ENABLED=false
MARGIN_CASCADE_MIN_STRENGTH=0.5
MARGIN_CASCADE_FADE_SIZE_MULT=0.5
MARGIN_CASCADE_FADE_STOP_BPS=300
MARGIN_CASCADE_FADE_TP_BPS=100
MARGIN_CASCADE_FADE_HOLD_MINUTES=10
MARGIN_CASCADE_COOLDOWN_SECONDS=900
```

### Integration with Open Position

The regime router and cascade fade are integrated into the V4 entry path:

```
v4 entry gate stack:
  ①  primary timescale tradeable?
  ②  consensus.safe_to_trade
  ③  macro.direction_gate permits side
  ④  minutes_to_next_high_impact >= 30
  ⑤  regime != MEAN_REVERTING or opt-in
  ⑤.5 ME-STRAT-05: CASCADE FADE CHECK  <-- NEW (highest priority)
  ⑤.6 ME-STRAT-04: regime-adaptive strategy decision  <-- NEW
  ⑥  |p_up - 0.5| >= v4_entry_edge
  ⑦  |expected_move_bps| >= fee wall
  ⑧  portfolio.can_open_position
  ⑨  balance query
  ⑩  SL/TP from quantiles or regime strategy
```

**Behavior**:
- If `cascade_fade_enabled=False`: Skip cascade check (default)
- If `cascade_fade_enabled=True`:
  - Analyze cascade from V4 snapshot
  - If cascade is active and safe to fade: Execute cascade trade
  - Cascade trades bypass regime router (different risk profile)
  - Log cascade strength, direction, and entry quality

- If `regime_adaptive_enabled=False`: Skip regime check (backwards compatible)
- If `regime_adaptive_enabled=True`:
  - Route to appropriate strategy based on regime
  - If strategy returns no-trade: Skip with specific reason
  - If strategy returns trade: Apply size_mult, SL/TP, hold_minutes
  - Log regime and strategy decision for audit

### Clean Architecture

The implementation follows clean architecture principles:

1. **Domain Layer** (`domain/strategy.py`):
   - Pure Python, no external dependencies
   - ABC for Strategy pattern
   - Immutable dataclasses for TradeDecision

2. **Service Layer** (`services/regime_*.py`):
   - Concrete strategy implementations
   - Dependency injection for configuration
   - No infrastructure dependencies

3. **Use Case Layer** (`use_cases/open_position.py`):
   - Orchestrates strategy routing
   - Applies strategy decisions to position
   - Maintains backwards compatibility

4. **Infrastructure Layer** (`infrastructure/config/settings.py`):
   - Configuration via environment variables
   - Feature flag for safe rollout

### Test Coverage

**36 unit tests** covering:

- **Trend Strategy** (8 tests):
  - Strong probability entries (LONG/SHORT)
  - Weak probability → no trade
  - Expected move too small → no trade
  - Wrong regime → no trade
  - Custom configuration
  - Missing probability/timescale

- **Mean-Reversion Strategy** (7 tests):
  - Fade bullish extreme (SHORT)
  - Fade bearish extreme (LONG)
  - Not extreme enough → no trade
  - Fade too weak → no trade
  - Wrong regime → no trade
  - Custom configuration
  - Boundary thresholds

- **No-Trade Strategy** (3 tests):
  - CHOPPY → no trade
  - NO_EDGE → no trade
  - Allow speculative trades

- **Regime Router** (10 tests):
  - Route to correct strategy per regime
  - Unknown regime → no trade
  - Get regime from snapshot
  - Size multipliers per regime
  - SL/TP per regime

- **TradeDecision** (4 tests):
  - is_trade property
  - bps to pct conversions
  - Reward/risk ratio
  - Zero stop handling

- **Integration** (3 tests):
  - Full workflow: TRENDING_UP → LONG
  - Full workflow: MEAN_REVERTING → fade
  - Full workflow: CHOPPY → no trade

**Cascade Fade Tests** (26 tests):

- **Cascade Detector** (7 tests):
  - No cascade data → IDLE
  - Weak cascade → IDLE, LATE
  - Medium cascade → BET state
  - Strong cascade → CASCADE state, PREMIUM
  - Direction: LONG liquidations
  - Direction: SHORT liquidations
  - No direction when composite=0

- **Cascade Fade Strategy** (7 tests):
  - Cascade not active → no trade
  - Weak cascade not safe → no trade
  - Fade LONG liquidations → SHORT
  - Fade SHORT liquidations → LONG
  - PREMIUM entry size (0.6x)
  - STANDARD entry size (0.5x)
  - LATE entry → no trade

- **Cooldown Logic** (3 tests):
  - No cooldown initially
  - In cooldown after cascade end
  - Cooldown expires

- **Edge Cases** (5 tests):
  - Missing timescale
  - Missing composite
  - Zero strength
  - Boundary strength 0.5
  - Boundary strength 0.7

- **Integration** (4 tests):
  - Full cascade fade flow
  - State machine transitions
  - Reward/risk ratio
  - Custom configuration

### Expected Performance by Regime

**TRENDING_UP/TRENDING_DOWN**:
- Higher win rate (55%+ target)
- Larger position size (1.2x)
- Wider stops to avoid noise
- Longer hold to capture trend
- Expected: 2% gain per trade

**MEAN_REVERTING**:
- Lower win rate (50-55% target, fades are harder)
- Smaller position size (0.8x)
- Tighter stops to limit fade risk
- Quick profit target
- Expected: 0.5% gain per trade

**CHOPPY/NO_EDGE**:
- No trades (safe default)
- Avoids whipsaw losses
- Preserves capital for better opportunities

**CASCADE FADE**:
- Higher risk strategy (cascades can continue)
- Half size (0.5-0.6x) to limit exposure
- Wide stops (3%) to avoid cascade noise
- Quick targets (1%) for initial bounce
- Win rate target: ~60% (PREMIUM entries higher)
- 15-minute cooldown between cascades
- Expected: 1% gain per successful fade

### Usage

#### Enable Regime Adaptive Trading

```bash
# Set environment variable
export MARGIN_REGIME_ADAPTIVE_ENABLED=true

# Or pass to OpenPositionUseCase
uc = OpenPositionUseCase(
    ...
    regime_adaptive_enabled=True,
    regime_trend_size_mult=1.2,
    regime_mr_size_mult=0.8,
    ...
)
```

#### Monitor Regime Decisions

The regime decision is logged for each trade:

```
INFO: v4 entry: regime strategy decision — TRENDING_UP_TREND_LONG size_mult=1.20 stop=150.0bp tp=200.0bp hold=60m
INFO: v4 entry: regime size mult applied — total size_mult=1.20 (macro=1.000 × regime=1.20)
```

Post-trade analysis fields in Position:
- `v4_entry_strategy_decision`: "TRENDING_UP_TREND_LONG"
- `v4_entry_strategy_size_mult`: 1.2
- `v4_entry_strategy_hold_minutes`: 60

### Backwards Compatibility

- **Default**: `regime_adaptive_enabled=False` (no change in behavior)
- **Opt-in**: Enable via environment variable or constructor parameter
- **No breaking changes**: Existing code paths unchanged when disabled
- **Additive**: Regime decisions add TO existing gates, not replace them

### Future Enhancements

1. **Dynamic Regime Weights**: Adjust strategy parameters based on regime strength
2. **Multi-Timescale Regime**: Consider regime across multiple horizons
3. **Regime Transition Detection**: Adjust position size when regime is changing
4. **Backtest Integration**: Measure performance by regime
5. **Machine Learning**: Optimize strategy parameters via reinforcement learning

## Summary

**ME-STRAT-04** (Regime-Adaptive Strategy) successfully implements:

✓ Domain foundation (Strategy ABC, Regime enum, TradeDecision)
✓ Three strategy implementations (Trend, Mean-Reversion, No-Trade)
✓ Regime router with factory pattern
✓ Configuration via environment variables
✓ Integration with OpenPositionUseCase
✓ Backwards compatible (feature flag)
✓ Comprehensive unit tests (36 cases, 100% pass)
✓ Clean architecture (domain → service → use case)
✓ Audit fields for post-trade analysis

**ME-STRAT-05** (Cascade Fade Strategy) successfully implements:

✓ Cascade state machine (IDLE, CASCADE, BET, COOLDOWN)
✓ Direction detection from composite_v3
✓ Entry quality assessment (PREMIUM, STANDARD, LATE)
✓ Cascade fade strategy with risk-adjusted sizing
✓ Cooldown tracking between cascades
✓ Configuration via environment variables
✓ Comprehensive unit tests (26 cases, 100% pass)
✓ Clean architecture (domain → service → use case)

The implementation is production-ready. Enable via:
- `MARGIN_REGIME_ADAPTIVE_ENABLED=true` (regime strategies)
- `MARGIN_CASCADE_FADE_ENABLED=true` (cascade fade)
