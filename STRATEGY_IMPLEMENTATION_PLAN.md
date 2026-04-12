# V4 Strategy Implementation Plan

**Branch**: `v4-strategies-work`
**Worktree**: `../novakash-strategies`
**Goal**: Paper trade on Hyperliquid overnight with V4-enhanced strategies

---

## Overview

We have **8 new strategy proposals** based on the V4 underutilization audit (70% of V4 data is unused):

| ID | Strategy | Priority | Complexity | Status |
|----|----------|----------|------------|--------|
| ME-STRAT-01 | Enable v4 path in margin_engine | HIGH | LOW | Dark-deployed, needs activation |
| ME-STRAT-02 | Multi-timescale alignment | HIGH | LOW | 3/4 timescales agree filter |
| ME-STRAT-03 | Quantile-VaR position sizing | MEDIUM | MEDIUM | Risk-parity sizing |
| ME-STRAT-04 | Regime-adaptive strategy selection | HIGH | MEDIUM | Trend vs mean-reversion |
| ME-STRAT-05 | Cascade fade strategy | MEDIUM | HIGH | Fade liquidations |
| ME-STRAT-06 | CLOB book imbalance scalp | LOW | HIGH | Microstructure alpha |
| ME-STRAT-07 | MacroV2 calibration | MEDIUM | MEDIUM | Replace anti-predictive Qwen |
| ME-STRAT-08 | Event-driven pre-positioning | LOW | MEDIUM | Earnings/Fed pre-position |

---

## Clean Architecture Structure

Following the existing `margin_engine/` pattern:

```
margin_engine/
├── adapters/
│   ├── v4/
│   │   ├── snapshot_http.py       # V4SnapshotPort implementation
│   │   ├── consensus_adapter.py   # V4ConsensusPort
│   │   ├── macro_adapter.py       # MacroV2Port
│   │   └── cascade_adapter.py     # CascadeDetectorPort
│   └── exchange/
│       ├── hyperliquid.py         # HyperliquidMarginAdapter
│       └── paper.py               # PaperExchangeAdapter
├── domain/
│   ├── value_objects.py           # V4Snapshot, StrategyDecision, etc.
│   ├── ports.py                   # StrategyPort, SizerPort, ExitPort
│   └── strategy.py                # Strategy ABC
├── services/
│   ├── timescale_alignment.py     # ME-STRAT-02
│   ├── quantile_var_sizer.py      # ME-STRAT-03
│   ├── regime_adaptive.py         # ME-STRAT-04
│   ├── cascade_fade.py            # ME-STRAT-05
│   └── clob_scalp.py              # ME-STRAT-06
├── use_cases/
│   ├── open_position_v4.py        # V4-enabled position opener
│   └── manage_positions_v4.py     # V4-aware position manager
└── tests/
    ├── unit/
    │   ├── test_timescale_alignment.py
    │   ├── test_quantile_var_sizer.py
    │   ├── test_regime_adaptive.py
    │   └── test_cascade_fade.py
    └── integration/
        └── test_v4_position_flow.py
```

---

## Implementation Phases

### Phase 1: Foundation (Subagent A)
**Goal**: Enable v4 path + basic infrastructure

**Tasks**:
1. Activate `MARGIN_ENGINE_USE_V4_ACTIONS=true` in settings
2. Wire V4SnapshotPort to existing gate dispatcher
3. Add V4-specific fields to `strategy_decisions` table
4. Update `open_position.py` to consume full V4 data
5. Tests: Verify V4 snapshot flows through gates

**Deliverables**:
- [ ] V4 path enabled in production (paper mode)
- [ ] All V4 fields logged to DB
- [ ] Unit tests for V4 data flow
- [ ] PR #1: "feat: enable v4 path in margin_engine"

---

### Phase 2: Multi-Timescale Alignment (Subagent B)
**Goal**: Filter signals by timescale agreement

**Strategy Logic**:
```python
def should_trade(v4_snapshot: V4Snapshot) -> bool:
    # 15m is primary - must agree
    primary_dir = v4_snapshot.primary.p_up > 0.5
    
    # Count agreement across all 4 timescales
    aligned = sum([
        timescale.p_up > 0.5 == primary_dir
        for timescale in v4_snapshot.timescales.values()
    ])
    
    # Trade only if 3/4 or 4/4 agree
    return aligned >= 3


def size_position(v4_snapshot: V4Snapshot) -> float:
    # Base size from Kelly
    base_size = bet_fraction * equity
    
    # Multiplier based on alignment strength
    alignment = v4_snapshot.consensus.alignment_score
    if alignment >= 0.75:  # 4/4
        return base_size * 1.4
    elif alignment >= 0.5:  # 3/4
        return base_size * 1.2
    else:
        return base_size  # 2/4 - no trade (filtered above)
```

**Deliverables**:
- [ ] `services/timescale_alignment.py` module
- [ ] Integration with gate dispatcher
- [ ] Configurable threshold (default 3/4)
- [ ] Tests: 4/4, 3/4, 2/4 scenarios
- [ ] PR #2: "feat: add multi-timescale alignment filter"

---

### Phase 3: Quantile-VaR Sizing (Subagent C)
**Goal**: Risk-parity position sizing using TimesFM quantiles

**Strategy Logic**:
```python
def calculate_var(v4_snapshot: V4Snapshot) -> float:
    # VaR = p10 downside (90% confidence)
    p10 = v4_snapshot.primary.timesfm_p10
    p50 = v4_snapshot.primary.timesfm_p50
    return (p50 - p10) / p50  # % downside


def size_position(target_risk: float = 0.005) -> float:
    # target_risk = 0.5% of equity per trade
    var_pct = calculate_var(v4_snapshot)
    
    # Inverse-VaR sizing: larger position when VaR is small
    size_mult = min(2.0, max(0.5, target_risk / var_pct))
    
    return bet_fraction * equity * size_mult
```

**Deliverables**:
- [ ] `services/quantile_var_sizer.py` module
- [ ] Configurable target_risk (default 0.5%)
- [ ] Cap at 2.0x, floor at 0.5x
- [ ] Tests: VaR calculations, size caps
- [ ] PR #3: "feat: add quantile-VaR position sizing"

---

### Phase 4: Regime-Adaptive Selection (Subagent D)
**Goal**: Different strategies for different regimes

**Strategy Logic**:
```python
def select_strategy(regime: str, v4_snapshot: V4Snapshot) -> Strategy:
    if regime == "TRENDING_UP" or regime == "TRENDING_DOWN":
        # Trend-following: wider stops, larger size
        return TrendStrategy(
            stop_mult=1.5,  # wider stops
            size_mult=1.2   # larger size
        )
    elif regime == "MEAN_REVERTING":
        # Mean-reversion: fade extremes
        return MeanReversionStrategy(
            entry_threshold=0.8,  # wait for extremes
            stop_mult=0.8,        # tighter stops
            take_profit=0.02      # quick exits
        )
    elif regime == "CHOPPY":
        # No trade or very small size
        return NoTradeStrategy()  # or size_mult=0.25
    else:  # NO_EDGE
        return NoTradeStrategy()
```

**Deliverables**:
- [ ] `services/regime_adaptive.py` module
- [ ] Strategy ABC with concrete implementations
- [ ] Backtest framework for regime performance
- [ ] Tests: Each regime path
- [ ] PR #4: "feat: add regime-adaptive strategy selection"

---

### Phase 5: Cascade Fade (Subagent E)
**Goal**: Fade liquidation cascades (inverse betting)

**Strategy Logic**:
```python
def cascade_fade_signal(cascade_state: CascadeState, v4_snapshot: V4Snapshot) -> Optional[Trade]:
    if cascade_state != CascadeState.CASCADE:
        return None  # Only trade during cascades
    
    # Fade the cascade (bet against the liquidation wave)
    # If cascade is LONG liquidations → bet SHORT
    # If cascade is SHORT liquidations → bet LONG
    
    cascade_direction = cascade_state.direction  # LONG or SHORT
    opposite_direction = "SHORT" if cascade_direction == "LONG" else "LONG"
    
    return Trade(
        direction=opposite_direction,
        size=0.5 * bet_fraction * equity,  # Half size (higher risk)
        stop_loss=0.03,  # Wide stop (cascades can continue)
        take_profit=0.02  # Quick target
    )
```

**Deliverables**:
- [ ] `services/cascade_fade.py` module
- [ ] CascadeDetectorPort integration
- [ ] Risk controls (max 1 cascade trade at a time)
- [ ] Tests: Cascade detection, fade logic
- [ ] PR #5: "feat: add cascade fade strategy"

---

### Phase 6: CLOB Book Imbalance (Subagent F)
**Goal**: Microstructure alpha from order book

**Strategy Logic**:
```python
def clob_imbalance_signal(clob_book: CLOBBook) -> Optional[Trade]:
    bid_volume = sum([order.size for order in clob_book.bids[:10]])
    ask_volume = sum([order.size for order in clob_book.asks[:10]])
    
    imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume)
    
    # Strong imbalance (>0.7) suggests short-term direction
    if imbalance > 0.7:
        return Trade(direction="LONG", size=0.25 * bet_fraction * equity, hold_minutes=5)
    elif imbalance < -0.7:
        return Trade(direction="SHORT", size=0.25 * bet_fraction * equity, hold_minutes=5)
    else:
        return None
```

**Deliverables**:
- [ ] `services/clob_scalp.py` module
- [ ] CLOB feed integration (10s polls)
- [ ] Very short holding periods (5-10 min)
- [ ] Tests: Imbalance calculations
- [ ] PR #6: "feat: add CLOB book imbalance scalp"

---

### Phase 7: MacroV2 Calibration (Subagent G)
**Goal**: Replace anti-predictive Qwen with calibrated MacroV2

**Status**: Already done (PR #71) - just need to wire it in

**Deliverables**:
- [ ] Wire MacroV2Classifier to V4 snapshot
- [ ] Use macro.direction_gate in gate dispatcher
- [ ] Tests: MacroV2 integration
- [ ] PR #7: "feat: wire MacroV2 calibration"

---

### Phase 8: Event Pre-Positioning (Subagent H)
**Goal**: Pre-position before known events

**Strategy Logic**:
```python
def event_pre_position(event: Event, v4_snapshot: V4Snapshot) -> Optional[Trade]:
    # Events: earnings, Fed meetings, CPI, etc.
    # Pre-position 30-60 min before event
    
    if event.time_to_event < timedelta(hours=1):
        return None  # Too close to event
    
    # Historical bias for this event type
    bias = event.historical_bias  # LONG/SHORT/NEUTRAL
    
    if bias == "NEUTRAL":
        return None
    
    return Trade(
        direction=bias,
        size=0.5 * bet_fraction * equity,  # Half size
        hold_until=event.time,
        stop_loss=0.04  # Wider stop (event volatility)
    )
```

**Deliverables**:
- [ ] `services/event_pre_position.py` module
- [ ] Event calendar integration
- [ ] Historical bias tracking
- [ ] Tests: Event timing logic
- [ ] PR #8: "feat: add event pre-positioning"

---

## Testing Strategy

### Unit Tests
- Each service module has comprehensive unit tests
- Mock V4 snapshots, cascade states, CLOB books
- Target: 90%+ coverage

### Integration Tests
- Full position lifecycle with V4 data
- Paper trade simulation
- Database writes verified

### Backtests
- Historical V4 data (if available)
- Compare strategies against baseline (15m only)
- Metrics: WR, PnL, Sharpe, max drawdown

---

## Deployment Checklist

- [ ] All PRs merged to `v4-strategies-work`
- [ ] CI/CD passes (lint, typecheck, tests)
- [ ] Manual review of each PR
- [ ] Deploy to Montreal (paper mode)
- [ ] Monitor for 24 hours
- [ ] Verify DB writes and Telegram alerts
- [ ] Check PnL tracking accuracy
- [ ] If stable, consider flip to live (optional)

---

## Risk Controls

All strategies inherit from base risk manager:

- **Max drawdown kill switch**: 45%
- **Daily loss limit**: 10%
- **Max open exposure**: 30%
- **Min bet**: $2
- **Consecutive loss cooldown**: 3 losses → 15 min pause
- **Cascade fade max size**: 0.5x normal
- **CLOB scalp max size**: 0.25x normal

---

## Timeline

| Phase | Subagent | Duration | Target |
|-------|----------|----------|--------|
| 1: Foundation | A | 1 hour | v4 path enabled |
| 2: Timescale Alignment | B | 1 hour | 3/4 filter live |
| 3: Quantile-VaR | C | 1 hour | Risk-parity sizing |
| 4: Regime-Adaptive | D | 1.5 hours | Regime selection |
| 5: Cascade Fade | E | 1.5 hours | Fade strategy |
| 6: CLOB Scalp | F | 1 hour | Microstructure alpha |
| 7: MacroV2 | G | 30 min | Wiring complete |
| 8: Event Pre-Position | H | 1 hour | Event calendar |
| **Review & PRs** | - | 1 hour | All PRs created |
| **Deploy & Monitor** | - | Ongoing | Paper trade |

**Total**: ~8-9 hours of work + review

---

## Success Criteria

1. **Technical**: All strategies implemented with clean architecture
2. **Testing**: 90%+ unit test coverage, all tests passing
3. **CI/CD**: All PRs pass CI checks
4. **Paper Trading**: Strategies running on Hyperliquid overnight
5. **Observability**: DB writes, Telegram alerts, dashboard visibility
6. **Performance**: V4 strategies outperform baseline (15m only)

---

## Next Steps

1. **Create subagents** A-H for each phase
2. **Execute in parallel** where possible (A, B, C can run together)
3. **Review code** carefully before PRs
4. **Merge to v4-strategies-work** branch
5. **Deploy to Montreal** for paper trading
6. **Monitor overnight** and report results

---

*Plan created: 2026-04-12*
*Based on V4 underutilization audit: 70% of V4 data unused*
*Goal: Paper trade enhanced strategies on Hyperliquid overnight*
