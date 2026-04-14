# Margin Engine Clean Architecture Refactoring Plan

**Created:** 2026-04-14  
**Based on:** `docs/margin_engine/clean-architecture-audit-2026-04-14.md`  
**Status:** Ready for Implementation

---

## Quick Summary

The margin engine has **solid port-based architecture** but violates clean architecture in 3 key areas:

1. **Domain contamination** - v4 API models live in `domain/value_objects.py` (should be in adapter)
2. **Services layer misplacement** - Business logic in `services/` should be in `application/services/`
3. **Missing presentation layer** - HTTP status server buried in `infrastructure/`

---

## Phase 1: Domain Cleanup (Critical) ⚡

**Effort:** 2-3 days  
**Priority:** P0 - Blocks all other work

### Tasks

#### 1.1 Move v4 Value Objects to Adapter
```bash
# Create new file
margin_engine/adapters/signal/v4_models.py

# Move these classes from domain/value_objects.py:
- V4Snapshot
- Consensus
- MacroBias
- TimescalePayload
- Quantiles
- Cascade
- RegimeTimescale
```

**Files to update:**
- `adapters/signal/v4_snapshot_http.py` - import from local `v4_models.py`
- `use_cases/open_position.py` - import from adapter (not domain)

#### 1.2 Move FillResult to ExchangePort
```python
# Remove from domain/value_objects.py:
- FillResult

# Define in domain/ports.py:
@dataclass
class FillResult:
    order_id: str
    fill_price: Price
    filled_notional: float
    commission: float
    commission_asset: str
    commission_is_actual: bool

# In ExchangePort:
async def place_market_order(...) -> FillResult: ...
```

#### 1.3 Split value_objects.py
```bash
# Create:
margin_engine/domain/value_objects/
├── money.py         # Money, Price
├── enums.py         # TradeSide, ExitReason, PositionState
└── position.py      # Position-related VOs

# Keep in single file: Simple VOs only
```

#### 1.4 Create Domain Exceptions
```python
# Create: margin_engine/domain/exceptions.py

class DomainException(Exception): ...
class DomainValidationError(DomainException): ...
class EntityNotFoundError(DomainException): ...
class BusinessRuleViolationError(DomainException): ...
```

**Files to update:**
- `domain/entities/position.py` - use DomainValidationError
- `domain/entities/portfolio.py` - use DomainValidationError

---

## Phase 2: Services to Application (High) 🔥

**Effort:** 2-3 days  
**Priority:** P1 - After domain cleanup

### Tasks

#### 2.1 Create Application Layer Structure
```bash
# Current structure:
margin_engine/
├── domain/
├── use_cases/
├── services/          # ← Move this
├── adapters/
└── infrastructure/

# New structure:
margin_engine/
├── domain/
├── application/       # ← New
│   ├── services/      # ← Moved from root
│   │   ├── regime/
│   │   └── cascade/
│   └── use_cases/     # ← Rename existing
├── adapters/
└── infrastructure/
```

#### 2.2 Move Strategy Pattern
```bash
# Move domain/strategy.py → application/services/strategy.py

# Update imports in:
- application/services/regime/router.py
- application/services/regime/trend.py
- application/services/regime/mean_reversion.py
```

#### 2.3 Update Use Case Imports
```python
# Before:
from margin_engine.domain.strategy import Strategy, TradeDecision

# After:
from margin_engine.application.services.strategy import Strategy, TradeDecision
```

---

## Phase 3: Infrastructure/Presentation Separation (Medium) 🏗️

**Effort:** 3-4 days  
**Priority:** P2 - After services migration

### Tasks

#### 3.1 Create Presentation Layer
```bash
# Create:
margin_engine/presentation/
├── api/
│   ├── routes/
│   │   ├── status.py        # Move from infrastructure/
│   │   └── __init__.py
│   ├── schemas/
│   │   └── status_schemas.py
│   └── dependencies.py
└── dto/
```

#### 3.2 Setup Alembic Migrations
```bash
# In margin_engine/
alembic init alembic

# Move migrations:
margin_engine/adapters/persistence/pg_repository.py:ADDITIVE_MIGRATIONS_SQL
  → alembic/versions/001_initial_schema.py
  → alembic/versions/002_add_commission_columns.py

# Remove from pg_repository.py:
- ADDITIVE_MIGRATIONS_SQL
- ensure_table() migration logic
```

#### 3.3 Create Database Models Module
```bash
# Create:
margin_engine/infrastructure/database/
├── models.py          # SQLAlchemy models
├── pool.py            # Connection pool
└── migrations/        # Alembic (moved from adapters/)
```

---

## Phase 4: Use Case Refactoring (Medium) 🔄

**Effort:** 2-3 days  
**Priority:** P3 - After infrastructure cleanup

### Tasks

#### 4.1 Extract Entry Strategies
```bash
# Create:
margin_engine/application/use_cases/entry_strategies/
├── __init__.py
├── base.py          # EntryStrategy ABC
├── v4_strategy.py   # V4 entry logic (840 lines → ~300)
└── v2_strategy.py   # Legacy v2 logic (~200)
```

**Files to update:**
- `application/use_cases/open_position.py` - delegate to strategy

#### 4.2 Extract Position Management Logic
```bash
# Create:
margin_engine/application/use_cases/position_management/
├── __init__.py
├── stop_loss.py
├── take_profit.py
├── trailing.py
└── expiry.py
```

---

## Phase 5: DTO Layer (Low) 📦

**Effort:** 1-2 days  
**Priority:** P4 - Optional polish

### Tasks

#### 5.1 Create DTOs
```bash
# Create:
margin_engine/application/dto/
├── open_position.py
│   - OpenPositionInput
│   - OpenPositionOutput
└── manage_positions.py
    - ManagePositionsInput
    - ManagePositionsOutput
```

---

## Implementation Checklist

### Phase 1: Domain Cleanup
- [ ] Create `adapters/signal/v4_models.py`
- [ ] Move `V4Snapshot`, `Consensus`, `MacroBias` etc. to adapter
- [ ] Update `v4_snapshot_http.py` imports
- [ ] Update `open_position.py` imports
- [ ] Move `FillResult` to `domain/ports.py`
- [ ] Create `domain/value_objects/` directory
- [ ] Split `value_objects.py` into modules
- [ ] Create `domain/exceptions.py`
- [ ] Update entity imports
- [ ] Run tests: `python3 -m pytest tests/`
- [ ] Deploy to staging

### Phase 2: Services Migration
- [ ] Create `application/` directory structure
- [ ] Move `services/` → `application/services/`
- [ ] Move `domain/strategy.py` → `application/services/strategy.py`
- [ ] Update all imports
- [ ] Run tests: `python3 -m pytest tests/`
- [ ] Deploy to staging

### Phase 3: Infrastructure/Presentation
- [ ] Create `presentation/` directory structure
- [ ] Move `infrastructure/status_server.py` → `presentation/api/routes/status.py`
- [ ] Setup Alembic: `alembic init alembic`
- [ ] Convert additive migrations to Alembic
- [ ] Remove migration code from `pg_repository.py`
- [ ] Create `infrastructure/database/models.py`
- [ ] Run tests: `python3 -m pytest tests/`
- [ ] Deploy to staging

### Phase 4: Use Case Refactoring
- [ ] Create `entry_strategies/` package
- [ ] Extract v4 logic to `v4_strategy.py`
- [ ] Extract v2 logic to `v2_strategy.py`
- [ ] Refactor `open_position.py` to use strategy pattern
- [ ] Create `position_management/` package
- [ ] Run tests: `python3 -m pytest tests/`
- [ ] Deploy to staging

### Phase 5: DTO Layer
- [ ] Create `application/dto/` package
- [ ] Add DTOs for use cases
- [ ] Update use case signatures
- [ ] Run tests: `python3 -m pytest tests/`
- [ ] Deploy to staging

---

## Testing Strategy

### Unit Tests
```bash
# Run all tests
python3 -m pytest tests/ -v

# Test specific modules
python3 -m pytest tests/unit/test_regime_adaptive.py -v
python3 -m pytest tests/use_cases/test_open_position.py -v
```

### Integration Tests
```bash
# Test with real database
export DATABASE_URL=postgresql+asyncpg://user:pass@localhost/margin_test
python3 -m pytest tests/integration/ -v
```

### Manual Testing
1. **Paper mode**: `PAPER_MODE=true python3 main.py`
2. **Live mode**: `PAPER_MODE=false LIVE_TRADING_ENABLED=true python3 main.py`
3. **Monitor logs**: Check for errors in terminal
4. **Verify signals**: `/v4/snapshot` endpoint returns correct data
5. **Check positions**: `SELECT * FROM margin_positions;`

---

## Risk Mitigation

### Rollback Plan
- Each phase is **self-contained** - can rollback individually
- **Feature flags** for major changes (e.g., `NEW_ARCH=true`)
- **Staging deploy** before production
- **Monitor logs** for 24h after deploy

### Test Coverage
- **Before**: Run full test suite, record pass rate
- **After each phase**: Run tests, ensure 100% pass
- **After all phases**: Full regression test

### Monitoring
- **Logs**: Watch for import errors, attribute errors
- **Metrics**: Track trade execution success rate
- **Alerts**: Telegram alerts for critical errors

---

## Notes

### What's Already Good ✅
- Port-based dependency inversion (well implemented)
- Use cases have clear single responsibility
- Adapters properly implement ports
- Test structure is solid

### What Needs Work ⚠️
- Domain layer has infrastructure concerns (v4 models)
- Services layer should be application layer
- Missing presentation layer separation
- Database migrations in repository

### Migration Notes
- **Do NOT change behavior** during refactoring
- **Keep tests passing** at every step
- **Deploy incrementally** - one phase at a time
- **Document** any deviations from plan

---

## Related Documents

- **Audit Report:** `docs/margin_engine/clean-architecture-audit-2026-04-14.md`
- **Clean Architecture Guide:** `/Users/billyrichards/Downloads/clean_architecture_python_guide.md`
- **Main Engine Docs:** `docs/DATA_FEEDS.md`, `docs/IMPLEMENTATION_COMPLETE.md`

---

*Last updated: 2026-04-14*

---

## Strategy Coverage Analysis

### All Strategies Covered ✅

This refactoring plan **does cover all strategies** in the margin engine. Here's the complete inventory:

### Current Strategy Structure (`services/`)

| File | Purpose | Lines | Target Location |
|------|---------|-------|-----------------|
| `regime_adaptive.py` | Route to strategy based on regime | 143 | `application/services/regime/router.py` |
| `regime_trend.py` | Trend-following strategy | 479 | `application/services/regime/trend.py` |
| `regime_mean_reversion.py` | Mean reversion strategy | 472 | `application/services/regime/mean_reversion.py` |
| `regime_no_trade.py` | No-trade regime handler | 185 | `application/services/regime/no_trade.py` |
| `cascade_detector.py` | Detect liquidation cascades | 407 | `application/services/cascade/detector.py` |
| `cascade_fade.py` | Fade cascade movements | 544 | `application/services/cascade/fade.py` |
| `continuation_alignment.py` | Price continuation alignment | 801 | `application/services/entry/continuation.py` |
| `fee_aware_continuation.py` | Fee-aware continuation logic | 1406 | `application/services/entry/fee_aware_continuation.py` |
| `quantile_var_sizer.py` | Position sizing via VaR | 719 | `application/services/sizing/quantile_var.py` |

### Orchestration (`use_cases/`)

| File | Purpose | Lines | Target Location |
|------|---------|-------|-----------------|
| `open_position.py` | Entry decision orchestration | 840 | `application/use_cases/open_position.py` (refactored) |
| `manage_positions.py` | Exit/management orchestration | ~600 | `application/use_cases/manage_positions.py` (refactored) |

### Refactoring Mapping

```
Phase 2: Services → Application
├── regime/
│   ├── router.py              (regime_adaptive.py)
│   ├── trend.py               (regime_trend.py)
│   ├── mean_reversion.py      (regime_mean_reversion.py)
│   └── no_trade.py            (regime_no_trade.py)
├── cascade/
│   ├── detector.py            (cascade_detector.py)
│   └── fade.py                (cascade_fade.py)
├── entry/
│   ├── continuation.py        (continuation_alignment.py)
│   └── fee_aware_continuation.py (fee_aware_continuation.py)
└── sizing/
    └── quantile_var.py        (quantile_var_sizer.py)
```

### Strategy Decision Flow

```
v4_snapshot
    │
    ▼
RegimeAdaptiveRouter.decide()
    │
    ├── TrendRegime → TrendStrategy.decide()
    ├── MeanReversionRegime → MeanReversionStrategy.decide()
    └── NoTradeRegime → NoTradeStrategy.decide()
            │
            ▼
    EntryDecision (with continuation alignment + fee awareness)
            │
            ▼
    PositionSizing (quantile_var_sizer)
            │
            ▼
    ExecuteOrder
```

### Coverage Confirmation

✅ **All 9 service files** moved to `application/services/`  
✅ **All 2 use cases** refactored with strategy pattern  
✅ **Entry logic** extracted from 840-line use case  
✅ **Position management** extracted into submodules  
✅ **Sizing logic** preserved in `application/services/sizing/`

**No strategies are lost or omitted** — this is a structural reorganization only.


---

## Phase 6: YAML-Configurable Strategies (Optional Enhancement) 💡

**Effort:** 3-4 days  
**Priority:** P5 - After core refactoring complete  
**Inspired by:** Main engine's strategy registry (`engine/strategies/registry.py`)

### Goal

Make margin engine strategies **YAML-configurable** like the main engine, allowing:
- **Hot-reload** without code changes
- **A/B testing** of parameter variations
- **Non-dev trading input** (parameters in YAML)
- **Git-tracked strategy versions**

### Current State (Margin Engine)

```python
# Hard-coded in Python files:
services/regime_trend.py:
    def decide(self, v4: V4Snapshot) -> TradeDecision:
        if ts.conf is None:
            direction = 1 if ts.conf.mid > 0 else -1
            confidence = abs(ts.conf.mid)
```

Parameters like `min_dist`, `confidence_thresholds` are **hard-coded**.

### Target State (Like Main Engine)

```yaml
# margin_engine/strategies/configs/regime_trend.yaml
name: regime_trend
version: "1.0.0"
mode: ACTIVE
asset: BTC

# Regime detection
regime:
  type: trend
  params:
    min_confidence: 0.15          # Strong trend threshold
    direction_gate: true          # Require positive confidence
    regime_window: 3              # Lookback windows

# Entry gates (optional additional filters)
gates:
  - type: continuation_alignment
    params: { min_alignment: 0.6 }
  - type: fee_aware_continuation
    params: { min_expected_pnl: 0.005 }

# Position sizing
sizing:
  type: quantile_var
  params:
    quantile: 0.95
    var_multiplier: 1.0
    max_position_usd: 5000
```

### Implementation Plan

#### 6.1 Create Strategy Registry

```python
# application/strategy_registry.py

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
import yaml

from application.services.strategy import Strategy
from application.services.regime.router import RegimeAdaptiveRouter

@dataclass
class StrategyConfig:
    name: str
    version: str
    mode: str  # ACTIVE | DRY_RUN | DISABLED
    asset: str
    regime: dict
    gates: list[dict]
    sizing: dict

class StrategyRegistry:
    """Load YAML configs, instantiate strategies, evaluate."""
    
    def __init__(self, config_dir: Path):
        self._config_dir = config_dir
        self._strategies: dict[str, StrategyConfig] = {}
        self._load_all_configs()
    
    def _load_all_configs(self) -> None:
        """Load all YAML configs from config_dir."""
        for yaml_file in self._config_dir.glob("*.yaml"):
            config = self._parse_config(yaml_file)
            self._strategies[config.name] = config
    
    def _parse_config(self, yaml_file: Path) -> StrategyConfig:
        """Parse YAML into StrategyConfig."""
        with open(yaml_file) as f:
            data = yaml.safe_load(f)
        
        return StrategyConfig(
            name=data['name'],
            version=data['version'],
            mode=data['mode'],
            asset=data['asset'],
            regime=data.get('regime', {}),
            gates=data.get('gates', []),
            sizing=data.get('sizing', {})
        )
    
    def get_active_strategies(self) -> list[StrategyConfig]:
        """Return strategies with mode=ACTIVE."""
        return [s for s in self._strategies.values() if s.mode == 'ACTIVE']
    
    def evaluate(self, v4: V4Snapshot) -> dict[str, Any]:
        """Evaluate all active strategies."""
        results = {}
        for config in self.get_active_strategies():
            strategy = self._build_strategy(config)
            decision = strategy.decide(v4)
            results[config.name] = decision
        return results
    
    def _build_strategy(self, config: StrategyConfig) -> Strategy:
        """Build strategy from config."""
        # Map config to actual strategy classes
        if config.regime.get('type') == 'trend':
            return TrendStrategy(config.regime['params'])
        elif config.regime.get('type') == 'mean_reversion':
            return MeanReversionStrategy(config.regime['params'])
        # ... etc
```

#### 6.2 Update Services to Accept Config

```python
# Before:
class TrendStrategy:
    def decide(self, v4: V4Snapshot) -> TradeDecision:
        if ts.conf is None:
            direction = 1 if ts.conf.mid > 0 else -1

# After:
@dataclass
class TrendConfig:
    min_confidence: float = 0.15
    direction_gate: bool = True
    regime_window: int = 3

class TrendStrategy:
    def __init__(self, config: TrendConfig):
        self._config = config
    
    def decide(self, v4: V4Snapshot) -> TradeDecision:
        if ts.conf is None:
            direction = 1 if ts.conf.mid > 0 else -1
        # Use config parameters
        if abs(ts.conf.mid) < self._config.min_confidence:
            return TradeDecision(...)  # Skip weak signals
```

#### 6.3 Create Config Directory Structure

```
margin_engine/
├── strategies/
│   ├── configs/              # NEW: YAML configs
│   │   ├── regime_trend.yaml
│   │   ├── regime_mean_reversion.yaml
│   │   ├── cascade_fade.yaml
│   │   └── ...
│   ├── registry.py           # NEW: StrategyRegistry
│   └── configs/              # Keep existing YAML if any
```

#### 6.4 Example YAML Configs

```yaml
# margin_engine/strategies/configs/regime_trend.yaml
name: regime_trend
version: "1.0.0"
mode: ACTIVE
asset: BTC

regime:
  type: trend
  params:
    min_confidence: 0.15
    direction_gate: true
    regime_window: 3

gates: []  # No additional gates for basic trend

sizing:
  type: quantile_var
  params:
    quantile: 0.95
    var_multiplier: 1.0
    max_position_usd: 5000
```

```yaml
# margin_engine/strategies/configs/regime_trend_str.yaml
name: regime_trend_str
version: "1.1.0"
mode: DRY_RUN  # Test new params without live trading
asset: BTC

regime:
  type: trend
  params:
    min_confidence: 0.20  # Stricter: only strong trends
    direction_gate: true
    regime_window: 5      # Longer lookback

gates:
  - type: continuation_alignment
    params: { min_alignment: 0.7 }

sizing:
  type: quantile_var
  params:
    quantile: 0.95
    var_multiplier: 0.8   # Smaller positions
    max_position_usd: 3000
```

### Benefits

| Benefit | Description |
|---------|-------------|
| **Hot reload** | Change params without redeploy |
| **A/B testing** | Run multiple parameter sets in parallel |
| **Git tracking** | Version control for strategy params |
| **Non-dev input** | Traders can tune params in YAML |
| **Dry run mode** | Test new params before going live |
| **Clear separation** | Config vs code, like main engine |

### Migration Path

1. **Phase 6.1:** Create `StrategyRegistry` class
2. **Phase 6.2:** Update services to accept config dicts
3. **Phase 6.3:** Create YAML configs for all 9 strategies
4. **Phase 6.4:** Wire registry into `main.py`
5. **Phase 6.5:** Add hot-reload (file watcher)
6. **Phase 6.6:** Add validation (Pydantic for config schemas)

### Comparison with Main Engine

| Feature | Main Engine | Margin Engine (Current) | Margin Engine (Phase 6) |
|---------|-------------|------------------------|-------------------------|
| Config format | YAML | Python hard-coded | YAML |
| Hot reload | ✅ Yes | ❌ No | ✅ Yes |
| A/B testing | ✅ Yes | ❌ No | ✅ Yes |
| Gate pipeline | ✅ Yes | ⚠️ Partial | ✅ Yes |
| Version tracking | ✅ Yes | ❌ No | ✅ Yes |
| Mode toggle | ✅ Yes | ⚠️ Partial | ✅ Yes |

### Risk Assessment

| Risk | Mitigation |
|------|------------|
| YAML parsing errors | Validation + defaults |
| Config drift | Git tracking + review |
| Too many configs | Naming convention + cleanup |
| Performance overhead | Cache parsed configs |

**Effort:** 3-4 days  
**Risk:** Low (additive, doesn't break existing code)

---

## Updated Refactoring Timeline

```
Week 1: Phase 1 (Domain Cleanup)
  Day 1-2: Move v4 value objects to adapter
  Day 3: Split value_objects.py, create exceptions module
  Day 4-5: Test, verify, deploy

Week 2: Phase 2 (Services Migration)
  Day 1-2: Create application/services structure
  Day 3: Move strategy patterns
  Day 4-5: Test, verify, deploy

Week 3: Phase 3 (Infrastructure/Presentation)
  Day 1-2: Create presentation layer
  Day 3: Move status server
  Day 4-5: Setup Alembic, test migrations

Week 4: Phase 4-5 (Use Cases, DTOs)
  Day 1-3: Refactor use cases
  Day 4-5: Add DTOs, final cleanup

Week 5 (Optional): Phase 6 (YAML Config)
  Day 1-2: Create StrategyRegistry
  Day 3: Update services to accept config
  Day 4-5: Create YAML configs, test
```

**Total: 5 weeks** (4 weeks core + 1 week optional YAML config)

