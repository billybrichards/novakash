"""
Margin engine services — pure business logic modules.

This package contains stateless services that operate on domain objects.
No infrastructure dependencies; all configuration is injected.
"""

from margin_engine.application.services.strategy import (
    Strategy,
    TradeDecision,
    Regime,
)
from margin_engine.application.services.cascade_detector import (
    analyze_cascade,
    CascadeState,
    CascadeInfo,
)
from margin_engine.application.services.cascade_fade import (
    CascadeFadeStrategy,
    CascadeFadeConfig,
)

__all__ = [
    "Strategy",
    "TradeDecision",
    "Regime",
    "analyze_cascade",
    "CascadeState",
    "CascadeInfo",
    "CascadeFadeStrategy",
    "CascadeFadeConfig",
]
