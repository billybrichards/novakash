"""
Strategy Registry -- loads YAML configs, builds strategy instances, evaluates.

Config-first strategy system for margin engine. Each strategy defined in YAML
with parameters. No inheritance chain - direct parameter injection.

Audit: ME-STRAT-01.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from margin_engine.application.services.strategy import TradeDecision
from margin_engine.domain.value_objects import V4Snapshot

logger = logging.getLogger(__name__)


class StrategyRegistry:
    """Loads strategy YAML configs, builds strategy instances, evaluates.

    Each strategy has:
    - A YAML config defining its parameters
    - A mode (ACTIVE | DRY_RUN | DISABLED)
    - A timescale (5m | 15m | 1h | 4h)
    - Strategy-specific params

    Features:
    - Hot-reload: Optional (watch for file changes)
    - A/B testing: Multiple config files per strategy
    - Mode control: ACTIVE strategies evaluated, DRY_RUN logged, DISABLED skipped
    """

    def __init__(
        self,
        config_dir: str,
        v4_port: Any = None,
        execute_trade_uc: Any = None,
    ):
        """
        Initialize strategy registry.

        Args:
            config_dir: Directory containing YAML strategy configs
            v4_port: V4SnapshotPort for fetching snapshots (optional)
            execute_trade_uc: ExecuteTrade use case for live trading (optional)
        """
        self._config_dir = Path(config_dir)
        self._v4_port = v4_port
        self._execute_uc = execute_trade_uc
        self._configs: dict[str, dict] = {}
        self._strategies: dict[str, Any] = {}
        self._modes: dict[str, str] = {}

    def load_all(self) -> None:
        """Scan config_dir for *.yaml, parse configs, build strategies."""
        if not self._config_dir.exists():
            logger.warning(
                "strategy_registry.config_dir_missing dir=%s", str(self._config_dir)
            )
            return

        for yaml_file in sorted(self._config_dir.glob("*.yaml")):
            try:
                config = self._parse_yaml(yaml_file)
                strategy = self._build_strategy(config)
                mode = config.get("mode", "DISABLED")

                self._configs[config["name"]] = config
                self._strategies[config["name"]] = strategy
                self._modes[config["name"]] = mode

                logger.info(
                    "strategy_registry.loaded",
                    name=config["name"],
                    version=config.get("version", "?"),
                    mode=mode,
                    timescale=config.get("timescale", "?"),
                )
            except Exception as exc:
                logger.error(
                    "strategy_registry.load_error file=%s error=%s",
                    str(yaml_file),
                    str(exc)[:200],
                )

    def _parse_yaml(self, path: Path) -> dict:
        """Parse a YAML strategy config file."""
        try:
            import yaml
        except ImportError:
            raise ImportError("PyYAML is required for YAML config loading")

        with open(path) as f:
            data = yaml.safe_load(f)

        if not data:
            raise ValueError(f"Empty or invalid YAML: {path}")

        if "name" not in data:
            raise ValueError(f"Missing required field 'name' in {path}")

        return data

    def _build_strategy(self, config: dict) -> Any:
        """Build a strategy instance from config."""
        name = config["name"]
        params = config.get("params", {})

        # Map strategy names to classes
        strategy_map = {
            "regime_trend": (
                "margin_engine.application.services.regime_trend",
                "TrendStrategy",
                "TrendStrategyConfig",
            ),
            "regime_mean_reversion": (
                "margin_engine.application.services.regime_mean_reversion",
                "MeanReversionStrategy",
                "MeanReversionConfig",
            ),
            "regime_no_trade": (
                "margin_engine.application.services.regime_no_trade",
                "NoTradeStrategy",
                None,
            ),
            "cascade_fade": (
                "margin_engine.application.services.cascade_fade",
                "CascadeFadeStrategy",
                "CascadeFadeConfig",
            ),
        }

        if name not in strategy_map:
            logger.warning("strategy_registry.unknown_strategy", name=name)
            return None

        module_name, strategy_cls, config_cls = strategy_map[name]

        try:
            # Import module
            module = __import__(module_name, fromlist=[strategy_cls])
            strategy_class = getattr(module, strategy_cls)

            # Use from_dict if available, else use config class directly
            if config_cls:
                config_class = getattr(module, config_cls)
                if hasattr(config_class, "from_dict"):
                    strategy_config = config_class.from_dict(params)
                    return strategy_class(config=strategy_config)
                else:
                    # Map YAML params to config fields
                    config_fields = {}
                    for field_name in config_class.__dataclass_fields__:
                        if field_name in params:
                            config_fields[field_name] = params[field_name]
                    strategy_config = config_class(**config_fields)
                    return strategy_class(config=strategy_config)
            else:
                # No config class - pass params directly
                return strategy_class(**params)

        except Exception as e:
            logger.error(
                "strategy_registry.build_error",
                name=name,
                error=str(e)[:200],
            )
            return None

    def get_strategy(self, name: str) -> Optional[Any]:
        """Get strategy by name."""
        return self._strategies.get(name)

    def get_config(self, name: str) -> Optional[dict]:
        """Get config by name."""
        return self._configs.get(name)

    def get_mode(self, name: str) -> str:
        """Get strategy mode (ACTIVE | DRY_RUN | DISABLED)."""
        return self._modes.get(name, "DISABLED")

    def get_active_strategies(self) -> list[str]:
        """Get names of all ACTIVE strategies."""
        return [name for name, mode in self._modes.items() if mode == "ACTIVE"]

    def evaluate(self, v4: V4Snapshot) -> dict[str, TradeDecision]:
        """Evaluate all active strategies on v4 snapshot.

        Args:
            v4: V4 snapshot

        Returns:
            Dict mapping strategy name to TradeDecision
        """
        results = {}
        for name in self.get_active_strategies():
            strategy = self._strategies.get(name)
            if strategy:
                try:
                    decision = strategy.decide(v4)
                    results[name] = decision
                    logger.debug(
                        "strategy_registry.evaluated",
                        name=name,
                        direction=decision.direction,
                        reason=decision.reason,
                    )
                except Exception as e:
                    logger.error(
                        "strategy_registry.evaluate_error",
                        name=name,
                        error=str(e)[:200],
                    )
        return results

    def reload(self) -> None:
        """Reload all configs (hot-reload support)."""
        logger.info("strategy_registry.reloading")
        self._configs.clear()
        self._strategies.clear()
        self._modes.clear()
        self.load_all()
        logger.info("strategy_registry.reloaded")

    def get_strategy_names(self) -> list[str]:
        """Get all registered strategy names."""
        return list(self._configs.keys())

    def get_all_modes(self) -> dict[str, str]:
        """Get all strategy modes."""
        return dict(self._modes)
