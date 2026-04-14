# Plan: YAML Configurable Strategies for Margin Engine

## Summary
Add YAML-based strategy configuration layer to margin engine, mirroring the pattern from `engine/strategies/registry.py` while respecting the margin engine's simpler architecture.

## Scope

### Create New Files

1. **`strategies/__init__.py`** - Package exports
2. **`strategies/registry.py`** - StrategyRegistry class
3. **`strategies/configs/regime_trend.yaml`** - Trend strategy config
4. **`strategies/configs/regime_mean_reversion.yaml`** - Mean reversion config
5. **`strategies/configs/regime_no_trade.yaml`** - No-trade strategy config
6. **`strategies/configs/cascade_fade.yaml`** - Cascade fade config
7. **`strategies/configs/fee_aware_continuation.yaml`** - Fee-aware continuation config
8. **`strategies/configs/quantile_var_sizer.yaml`** - Quantile var sizer config
9. **`strategies/configs/continuation_alignment.yaml`** - Continuation alignment config
10. **`strategies/configs/regime_adaptive.yaml`** - Regime adaptive router config

### Update Existing Files

1. **`main.py`** - Load StrategyRegistry, pass to use cases
2. **`application/services/regime_trend.py`** - Accept config from YAML
3. **`application/services/regime_mean_reversion.py`** - Accept config from YAML
4. **`application/services/regime_adaptive.py`** - Accept config from YAML
5. **`application/services/cascade_fade.py`** - Accept config from YAML
6. **`application/use_cases/open_position.py`** - Use registry for strategy selection

## Implementation Details

### 1. StrategyRegistry (`strategies/registry.py`)

```python
class StrategyRegistry:
    """Loads YAML strategy configs, builds strategy instances, evaluates.
    
    Each strategy defined in YAML with parameters:
    - mode: ACTIVE | DRY_RUN | DISABLED
    - asset: BTC
    - timescale: 5m | 15m | 1h | 4h
    - params: Strategy-specific parameters
    
    Hot-reload: Optional (watch for file changes)
    A/B testing: Multiple config files per strategy
    """
    
    def __init__(
        self,
        config_dir: str,
        v4_port: Any = None,
        execute_trade_uc: Any = None,
    ):
        self._config_dir = Path(config_dir)
        self._v4_port = v4_port
        self._execute_uc = execute_trade_uc
        self._configs: dict[str, dict] = {}
        self._strategies: dict[str, Any] = {}
    
    def load_all(self) -> None:
        """Scan config_dir for *.yaml, build strategies."""
        for yaml_file in sorted(self._config_dir.glob("*.yaml")):
            config = self._parse_yaml(yaml_file)
            strategy = self._build_strategy(config)
            self._configs[config["name"]] = config
            self._strategies[config["name"]] = strategy
    
    def get_strategy(self, name: str) -> Optional[Any]:
        """Get strategy by name."""
        return self._strategies.get(name)
    
    def get_active_strategies(self) -> list[str]:
        """Get names of all ACTIVE strategies."""
        return [
            name for name, cfg in self._configs.items()
            if cfg.get("mode") == "ACTIVE"
        ]
    
    def evaluate(self, v4: V4Snapshot) -> dict[str, TradeDecision]:
        """Evaluate all active strategies on v4 snapshot."""
        results = {}
        for name in self.get_active_strategies():
            strategy = self._strategies.get(name)
            if strategy:
                results[name] = strategy.decide(v4)
        return results
```

### 2. YAML Config Format

```yaml
# strategies/configs/regime_trend.yaml
name: regime_trend
version: "1.0.0"
mode: ACTIVE  # ACTIVE | DRY_RUN | DISABLED
asset: BTC
timescale: 15m

params:
  # Entry threshold
  min_probability: 0.55
  
  # Position sizing
  size_mult: 1.2
  
  # Stop loss and take profit (basis points)
  stop_loss_bps: 150
  take_profit_bps: 200
  
  # Holding period
  hold_minutes: 60
  
  # Trailing stop
  trailing_stop: true
  
  # Minimum expected move
  min_expected_move_bps: 30.0
```

### 3. Integration Points

**main.py:**
```python
# Load StrategyRegistry
from margin_engine.strategies.registry import StrategyRegistry

registry = StrategyRegistry(
    config_dir=str(settings.strategy_config_dir),
    v4_port=v4_snapshot_port,
)
registry.load_all()

# Pass to use cases
open_position_uc = OpenPositionUseCase(
    input=OpenPositionInput(
        ...
        strategy_registry=registry,
    )
)
```

**open_position.py:**
```python
class OpenPositionUseCase:
    def __init__(
        self,
        input: OpenPositionInput,
    ) -> None:
        self._strategy_registry = input.strategy_registry
        
    async def execute(self) -> OpenPositionOutput:
        if self._strategy_registry:
            # Evaluate all active strategies
            results = self._strategy_registry.evaluate(v4)
            # Select best strategy or combine
            decision = self._select_best_decision(results)
```

## Testing Strategy

1. **Unit tests** for registry loading
2. **Integration tests** for strategy evaluation
3. **Hot-reload tests** for config changes
4. **A/B testing tests** for multiple configs per strategy

## Expected Result

- All strategies defined in YAML
- Hot-reload support (config changes without restart)
- A/B testing via multiple config files
- All 160 tests still pass
- No behavior change - just config layer on top

## Implementation Steps

1. ✅ Create `strategies/` directory structure
2. ✅ Create `strategies/__init__.py`
3. ✅ Create YAML configs for all 9 strategies
4. ✅ Create `StrategyRegistry` class
5. ✅ Update `main.py` to load registry
6. ✅ Update `open_position.py` to use registry
7. ✅ Run tests to verify no regressions
8. ⏳ Add hot-reload support (optional)
9. ⏳ Add A/B testing support (optional)
