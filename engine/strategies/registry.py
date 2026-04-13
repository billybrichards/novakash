"""Strategy Registry -- loads YAML configs, builds gate pipelines, evaluates.

Config-first strategy system. Each strategy defined in YAML with optional
custom Python hooks. No inheritance chain.

Audit: CA-07.
"""

from __future__ import annotations

import importlib.util
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import structlog
import yaml

from domain.value_objects import StrategyDecision
from strategies.data_surface import DataSurfaceManager, FullDataSurface
from strategies.gates.base import Gate, GateResult

log = structlog.get_logger(__name__)

# Gate type -> class mapping
_GATE_REGISTRY: dict[str, type] = {}


def _register_gates() -> None:
    """Populate _GATE_REGISTRY from the gates package."""
    if _GATE_REGISTRY:
        return
    from strategies.gates.timing import TimingGate
    from strategies.gates.direction import DirectionGate
    from strategies.gates.confidence import ConfidenceGate
    from strategies.gates.session_hours import SessionHoursGate
    from strategies.gates.clob_sizing import CLOBSizingGate
    from strategies.gates.source_agreement import SourceAgreementGate
    from strategies.gates.delta_magnitude import DeltaMagnitudeGate
    from strategies.gates.taker_flow import TakerFlowGate
    from strategies.gates.cg_confirmation import CGConfirmationGate
    from strategies.gates.spread import SpreadGate
    from strategies.gates.dynamic_cap import DynamicCapGate
    from strategies.gates.regime import RegimeGate
    from strategies.gates.macro_direction import MacroDirectionGate
    from strategies.gates.trade_advised import TradeAdvisedGate

    _GATE_REGISTRY.update({
        "timing": TimingGate,
        "direction": DirectionGate,
        "confidence": ConfidenceGate,
        "session_hours": SessionHoursGate,
        "clob_sizing": CLOBSizingGate,
        "source_agreement": SourceAgreementGate,
        "delta_magnitude": DeltaMagnitudeGate,
        "taker_flow": TakerFlowGate,
        "cg_confirmation": CGConfirmationGate,
        "spread": SpreadGate,
        "dynamic_cap": DynamicCapGate,
        "regime": RegimeGate,
        "macro_direction": MacroDirectionGate,
        "trade_advised": TradeAdvisedGate,
    })


@dataclass
class StrategyConfig:
    """Parsed strategy YAML configuration."""

    name: str
    version: str
    mode: str  # LIVE | GHOST | DISABLED
    asset: str
    timescale: str
    gates: list[dict]
    sizing: dict
    hooks_file: Optional[str] = None
    pre_gate_hook: Optional[str] = None
    post_gate_hook: Optional[str] = None


@dataclass
class SizingResult:
    """Position sizing output from the registry."""

    fraction: float = 0.025
    max_collateral_pct: float = 0.10
    entry_cap: Optional[float] = None
    size_modifier: float = 1.0
    label: str = "default"


class StrategyRegistry:
    """Loads strategy configs, builds pipelines, evaluates all strategies.

    Each strategy has:
    - A YAML config defining its gate pipeline
    - Optional Python hooks for custom logic
    - A documentation .md file (not loaded, for humans)
    """

    def __init__(
        self,
        config_dir: str,
        data_surface: DataSurfaceManager,
    ):
        self._config_dir = Path(config_dir)
        self._data_surface = data_surface
        self._configs: dict[str, StrategyConfig] = {}
        self._pipelines: dict[str, list[Gate]] = {}
        self._hooks: dict[str, dict[str, Callable]] = {}

    def load_all(self) -> None:
        """Scan config_dir for *.yaml, build pipelines, load hooks."""
        _register_gates()

        for yaml_file in sorted(self._config_dir.glob("*.yaml")):
            try:
                config = self._parse_yaml(yaml_file)
                gates = self._build_pipeline(config)
                hooks = self._load_hooks(config) if config.hooks_file else {}
                self._configs[config.name] = config
                self._pipelines[config.name] = gates
                self._hooks[config.name] = hooks
                log.info(
                    "registry.loaded",
                    strategy=config.name,
                    version=config.version,
                    mode=config.mode,
                    gates=len(gates),
                    hooks=list(hooks.keys()),
                )
            except Exception as exc:
                log.error(
                    "registry.load_error",
                    file=str(yaml_file),
                    error=str(exc)[:200],
                )

    def _parse_yaml(self, path: Path) -> StrategyConfig:
        """Parse a YAML strategy config file."""
        with open(path) as f:
            data = yaml.safe_load(f)

        return StrategyConfig(
            name=data["name"],
            version=data["version"],
            mode=data.get("mode", "GHOST"),
            asset=data.get("asset", "BTC"),
            timescale=data.get("timescale", "5m"),
            gates=data.get("gates", []),
            sizing=data.get("sizing", {"type": "fixed_kelly", "fraction": 0.025}),
            hooks_file=data.get("hooks_file"),
            pre_gate_hook=data.get("pre_gate_hook"),
            post_gate_hook=data.get("post_gate_hook"),
        )

    def _build_pipeline(self, config: StrategyConfig) -> list[Gate]:
        """Build a gate pipeline from the config's gate list."""
        pipeline = []
        for gate_def in config.gates:
            gate_type = gate_def["type"]
            params = gate_def.get("params", {})

            gate_cls = _GATE_REGISTRY.get(gate_type)
            if gate_cls is None:
                raise ValueError(
                    f"Unknown gate type '{gate_type}' in strategy '{config.name}'"
                )

            gate = gate_cls(**params)
            pipeline.append(gate)

        return pipeline

    def _load_hooks(self, config: StrategyConfig) -> dict[str, Callable]:
        """Load Python hooks from the strategy's .py file."""
        hooks_path = self._config_dir / config.hooks_file
        if not hooks_path.exists():
            log.warning("registry.hooks_missing", file=str(hooks_path))
            return {}

        spec = importlib.util.spec_from_file_location(
            f"strategy_hooks.{config.name}", hooks_path
        )
        if spec is None or spec.loader is None:
            return {}

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        hooks: dict[str, Callable] = {}
        # Collect all callable attributes as potential hooks
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if callable(attr) and not attr_name.startswith("_"):
                hooks[attr_name] = attr

        return hooks

    @property
    def strategy_names(self) -> list[str]:
        """Return all registered strategy names."""
        return list(self._configs.keys())

    @property
    def configs(self) -> dict[str, StrategyConfig]:
        """Return all strategy configs."""
        return dict(self._configs)

    async def evaluate_all(
        self,
        window: Any,
        state: Any,
    ) -> list[StrategyDecision]:
        """Evaluate all enabled strategies on the current data surface."""
        eval_offset = getattr(window, "eval_offset", None)
        surface = self._data_surface.get_surface(window, eval_offset)

        decisions = []
        for name, config in self._configs.items():
            if config.mode == "DISABLED":
                continue
            try:
                decision = self._evaluate_one(name, config, surface)
                decisions.append(decision)
            except Exception as exc:
                log.warning(
                    "registry.evaluate_error",
                    strategy=name,
                    error=str(exc)[:200],
                )
                decisions.append(
                    StrategyDecision(
                        action="ERROR",
                        direction=None,
                        confidence=None,
                        confidence_score=None,
                        entry_cap=None,
                        collateral_pct=None,
                        strategy_id=name,
                        strategy_version=config.version,
                        entry_reason="",
                        skip_reason=f"registry_error: {str(exc)[:200]}",
                        metadata={},
                    )
                )
        return decisions

    def _evaluate_one(
        self,
        name: str,
        config: StrategyConfig,
        surface: FullDataSurface,
    ) -> StrategyDecision:
        """Run one strategy's gate pipeline on the surface."""

        # Pre-gate hook (e.g., v4_fusion custom evaluation)
        if config.pre_gate_hook:
            hook_fn = self._hooks.get(name, {}).get(config.pre_gate_hook)
            if hook_fn:
                result = hook_fn(surface)
                if result is not None:
                    return result  # Hook handled it (TRADE or SKIP)

        # Run gate pipeline
        gate_results: list[GateResult] = []
        for gate in self._pipelines[name]:
            result = gate.evaluate(surface)
            gate_results.append(result)
            if not result.passed:
                return StrategyDecision(
                    action="SKIP",
                    direction=None,
                    confidence=None,
                    confidence_score=None,
                    entry_cap=None,
                    collateral_pct=None,
                    strategy_id=name,
                    strategy_version=config.version,
                    entry_reason="",
                    skip_reason=f"{result.gate_name}: {result.reason}",
                    metadata={
                        "gate_results": [
                            {"gate": r.gate_name, "passed": r.passed, "reason": r.reason}
                            for r in gate_results
                        ]
                    },
                )

        # All gates passed -- determine direction + sizing
        direction = self._determine_direction(config, surface)
        sizing = self._calculate_sizing(config, surface, gate_results)

        # Post-gate hook (e.g., v10 confidence classification)
        if config.post_gate_hook:
            hook_fn = self._hooks.get(name, {}).get(config.post_gate_hook)
            if hook_fn:
                sizing = hook_fn(surface, sizing)

        # Build confidence from surface
        confidence = surface.v4_conviction
        confidence_score = surface.v4_conviction_score
        if surface.poly_confidence_distance is not None:
            confidence_score = surface.poly_confidence_distance * 2.0

        return StrategyDecision(
            action="TRADE",
            direction=direction,
            confidence=confidence,
            confidence_score=confidence_score,
            entry_cap=sizing.entry_cap,
            collateral_pct=sizing.max_collateral_pct * sizing.size_modifier,
            strategy_id=name,
            strategy_version=config.version,
            entry_reason=(
                f"{name}_T{surface.eval_offset}_{direction}"
                f"_{sizing.label}"
            ),
            skip_reason=None,
            metadata={
                "gate_results": [
                    {"gate": r.gate_name, "passed": r.passed, "reason": r.reason}
                    for r in gate_results
                ],
                "sizing": {
                    "fraction": sizing.fraction,
                    "modifier": sizing.size_modifier,
                    "label": sizing.label,
                    "entry_cap": sizing.entry_cap,
                },
                "poly_direction": surface.poly_direction,
                "poly_confidence_distance": surface.poly_confidence_distance,
                "v2_probability_up": surface.v2_probability_up,
            },
        )

    def _determine_direction(
        self,
        config: StrategyConfig,
        surface: FullDataSurface,
    ) -> Optional[str]:
        """Determine trade direction from config + surface.

        Priority: config fixed direction > poly_direction > v2_probability_up.
        """
        # Check if direction gate fixed it
        for gate_def in config.gates:
            if gate_def["type"] == "direction":
                d = gate_def.get("params", {}).get("direction", "ANY")
                if d != "ANY":
                    return d

        # From polymarket outcome
        if surface.poly_direction:
            return surface.poly_direction

        # From v2 probability
        if surface.v2_probability_up is not None:
            return "UP" if surface.v2_probability_up > 0.5 else "DOWN"

        return None

    def _calculate_sizing(
        self,
        config: StrategyConfig,
        surface: FullDataSurface,
        gate_results: list[GateResult],
    ) -> SizingResult:
        """Calculate position sizing from config + gate data."""
        sizing_cfg = config.sizing
        result = SizingResult(
            fraction=sizing_cfg.get("fraction", 0.025),
            max_collateral_pct=sizing_cfg.get("max_collateral_pct", 0.10),
        )

        # Check gate results for sizing data
        for gr in gate_results:
            if "size_modifier" in gr.data:
                result.size_modifier = gr.data["size_modifier"]
                result.label = gr.data.get("label", "gate_sized")
            if "entry_cap" in gr.data:
                result.entry_cap = gr.data["entry_cap"]

        # Custom sizing hook
        if sizing_cfg.get("type") == "custom" and sizing_cfg.get("custom_hook"):
            hook_fn = self._hooks.get(config.name, {}).get(sizing_cfg["custom_hook"])
            if hook_fn:
                custom = hook_fn(surface, result)
                if isinstance(custom, SizingResult):
                    result = custom

        # Use V4 recommended collateral if available
        if result.max_collateral_pct == 0.10 and surface.v4_recommended_collateral_pct:
            result.max_collateral_pct = surface.v4_recommended_collateral_pct

        return result
