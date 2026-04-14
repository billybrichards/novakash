"""
Strategies Package -- YAML-configurable strategy system.

This package provides:
- StrategyRegistry: Loads YAML configs, builds strategy instances
- YAML config files: Define strategy parameters in declarative format
- Hot-reload support: Config changes without restart
- A/B testing: Multiple config files per strategy

Usage:
    from margin_engine.strategies import StrategyRegistry

    registry = StrategyRegistry(
        config_dir="strategies/configs",
        v4_port=v4_snapshot_port,
    )
    registry.load_all()

    # Get active strategies
    active = registry.get_active_strategies()

    # Evaluate all strategies
    results = registry.evaluate(v4_snapshot)
"""

from margin_engine.strategies.registry import StrategyRegistry

__all__ = ["StrategyRegistry"]
